# -*- coding: utf-8 -*-
"""
pbc_routes.py — Blueprint ของหน้า "ติดตามพื้นที่ PBC"

ออกแบบเป็น factory เพื่อไม่ต้อง import app.py กลับมา (กัน circular import)
และเพื่อให้ใช้ระบบ login/สิทธิ์ของแอปเดิมได้โดยไม่ต้องเขียนใหม่

วิธีใช้ใน app.py (เพิ่ม 3 บรรทัด ไม่ต้องแก้ของเดิม):

    from pbc_routes import create_pbc_blueprint
    app.register_blueprint(create_pbc_blueprint(
        login_required=login_required,
        current_user_fn=lambda: session.get("username", ""),
        branch_scope_fn=lambda: None,   # None = เห็นทุกสาขา
    ))
"""

import os
import tempfile
from functools import wraps

from flask import (
    Blueprint, jsonify, render_template, request, send_file,
)

import pbc_config as CFG
import pbc_forecast as FC
import pbc_parser
import pbc_service as SVC


def _passthrough(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


def create_pbc_blueprint(login_required=None, current_user_fn=None,
                         branch_scope_fn=None, user_fn=None, url_prefix="/pbc"):
    """
    login_required   : decorator ของแอปเดิม
    current_user_fn  : ฟังก์ชันคืนชื่อผู้ใช้ปัจจุบัน (ใช้บันทึกว่าใครแก้อะไร)
    branch_scope_fn  : ฟังก์ชันคืน list รหัสสาขาที่ผู้ใช้เห็นได้ (None = ทุกสาขา)
    user_fn          : ฟังก์ชันคืน object ผู้ใช้สำหรับ base_sidebar.html
                       (ต้องมี .Name และ .Role) ถ้าแอปใส่ผ่าน context_processor
                       อยู่แล้วไม่ต้องส่งมา
    """
    guard = login_required or _passthrough
    who = current_user_fn or (lambda: "unknown")
    scope = branch_scope_fn or (lambda: None)
    user_obj = user_fn or (lambda: None)

    bp = Blueprint("pbc", __name__, url_prefix=url_prefix)

    # -------------------------------------------------------------- ตัวช่วย

    def _visible_contracts():
        return SVC.list_contracts(branch_codes=scope())

    def _resolve_contract(contract_id=None):
        contracts = _visible_contracts()
        if not contracts:
            return None, contracts
        if contract_id:
            for c in contracts:
                if c["contract_id"] == contract_id:
                    return c, contracts
            return None, contracts
        return contracts[0], contracts

    def _build_overview(contract):
        """รวบรวมทุกอย่างที่หน้าจอต้องใช้ในการเรียกครั้งเดียว"""
        cid = contract["contract_id"]
        start = contract["start_month"]
        hours = contract["mnf_hours_per_day"] or CFG.DEFAULT_MNF_HOURS_PER_DAY

        dmas = [d["dma_code"] for d in SVC.get_contract_dmas(cid)]
        dma_set = set(dmas)
        monthly = SVC.get_monthly_effective(cid)
        targets = SVC.get_targets(cid)
        rtu = SVC.get_dma_rtu_status(dma_set)

        rolling = SVC.rolling_series(monthly, start, dma_set)
        per_month = SVC.monthly_series(monthly, start, dma_set)
        months_all = SVC.available_months(monthly)

        baseline_x = contract["baseline_rate_x"]
        milestones = [(t["month_no"], t["target_rate"]) for t in targets]
        last_no = max(
            [t["month_no"] for t in targets]
            + [r["month_no"] for r in rolling]
            + [1]
        )
        interim = FC.interim_target_line(baseline_x or 0.0, milestones, last_no) \
            if baseline_x is not None else []

        latest = rolling[-1] if rolling else None
        current_no = latest["month_no"] if latest else None
        next_target = None
        for t in targets:
            if current_no is None or t["month_no"] >= current_no:
                next_target = t
                break
        if next_target is None and targets:
            next_target = targets[-1]

        forecast = FC.forecast_rate(
            [(r["month_no"], r["loss_rate"]) for r in rolling],
            [t["month_no"] for t in targets],
        )
        outlook = None
        if next_target:
            outlook = FC.gap_to_target(
                forecast, next_target["target_rate"], next_target["month_no"]
            )

        # สถานะราย DMA ในรอบล่าสุด
        rows = []
        window = latest["window"] if latest else []
        for code in dmas:
            agg = SVC.aggregate(monthly, set(window), {code}) if window else None
            months_n = max(len(window), 1)
            info = rtu.get(code, {})
            rec_latest = monthly.get((window[-1], code)) if window else None
            row = {
                "dma_code": code,
                "inflow_m3": round(agg["inflow_m3"], 0) if agg else None,
                "sales_m3": round(agg["sales_m3"], 0) if agg else None,
                "loss_m3": round(agg["loss_m3"], 0) if agg else None,
                "loss_rate": round(agg["loss_rate"], 2)
                if agg and agg["loss_rate"] is not None else None,
                "loss_m3_month": round(agg["loss_m3"] / months_n, 0) if agg else None,
                "mnf_current": info.get("mnf_current"),
                "mnf_floor": info.get("mnf_floor"),
                "n_days": info.get("n_days", 0),
                "n_meters": info.get("n_meters", 0),
                "has_rtu": info.get("mnf_current") is not None,
                "avg_pressure_night": rec_latest.get("avg_pressure_night")
                if rec_latest else None,
                "avg_pressure_24h": rec_latest.get("avg_pressure_24h")
                if rec_latest else None,
                # ตรวจทุกเดือนในรอบ ไม่ใช่แค่เดือนสุดท้าย
                # เพราะค่าที่ปรับอาจอยู่ในเดือนก่อนหน้าแต่ยังมีผลต่อยอดรวมรอบนี้
                "has_override": any(
                    (monthly.get((m, code)) or {}).get("overrides")
                    for m in window
                ),
            }
            rows.append(row)

        # เป้าย่อยราย DMA ณ จุดวัดผลถัดไป
        breakdown = {"rows": [], "summary": None, "measure_month_no": None}
        if next_target and latest:
            saved = SVC.get_dma_targets(cid, next_target["month_no"])
            manual = {
                code: t["target_loss_m3"]
                for code, t in saved.items() if t["is_manual"]
            }
            months_n = max(len(window), 1)
            basis = []
            for r in rows:
                if r["loss_m3"] is None:
                    continue
                basis.append({
                    "dma_code": r["dma_code"],
                    "inflow_m3": (r["inflow_m3"] or 0) / months_n,
                    "sales_m3": (r["sales_m3"] or 0) / months_n,
                    "loss_m3": (r["loss_m3"] or 0) / months_n,
                    "mnf_current": r["mnf_current"],
                    "mnf_floor": r["mnf_floor"],
                })
            sales_month = sum(b["sales_m3"] for b in basis)
            allocated, summary = FC.build_dma_targets(
                basis, sales_month, next_target["target_rate"] / 100.0,
                hours, CFG.DAYS_PER_MONTH, manual=manual,
            )
            for a in allocated:
                saved_row = saved.get(a["dma_code"], {})
                a["saved_at"] = saved_row.get("updated_at", "")
                for key in ("loss_m3", "target_loss_m3", "reduction_m3",
                            "potential_m3"):
                    if a.get(key) is not None:
                        a[key] = round(a[key], 0)
                if a.get("target_mnf") is not None:
                    a["target_mnf"] = round(a["target_mnf"], 1)
            breakdown = {
                "rows": allocated,
                "summary": {k: (round(v, 0) if isinstance(v, float) and k.endswith("m3")
                                else v)
                            for k, v in summary.items()},
                "measure_month_no": next_target["month_no"],
                "measure_month": SVC.month_from_no(start, next_target["month_no"]),
                "hours_per_day": hours,
                "days": CFG.DAYS_PER_MONTH,
            }

        # ตรวจคุณภาพชุดข้อมูลรายเดือน
        #   1) เดือนที่ DMA มาไม่ครบ ทำให้ยอดรวมกระโดดโดยที่ % ยังดูปกติ
        #   2) เดือนที่อัตราแกว่งแรงผิดปกติเมื่อเทียบกับเดือนก่อนหน้า
        # หน้าเว็บไม่แสดงผลส่วนนี้แล้ว (ถอดแบนเนอร์เตือนออกตามที่ผู้ใช้ขอ)
        # แต่ยังคำนวณและส่งผ่าน API ไว้ เผื่อต้องการนำกลับมาแสดงภายหลัง
        data_issues = []
        n_expected = len(dmas)
        for row in per_month:
            if n_expected and row.get("n_dma", 0) < n_expected:
                data_issues.append({
                    "month": row["month"], "label": row["label"],
                    "type": "missing_dma",
                    "detail": "มีข้อมูล %d จาก %d พื้นที่"
                              % (row.get("n_dma", 0), n_expected),
                })
        for prev, cur in zip(per_month, per_month[1:]):
            if prev["loss_rate"] is None or cur["loss_rate"] is None:
                continue
            swing = abs(cur["loss_rate"] - prev["loss_rate"])
            if swing >= 5.0:
                data_issues.append({
                    "month": cur["month"], "label": cur["label"],
                    "type": "swing",
                    "detail": "อัตราเปลี่ยนจากเดือนก่อน %.1f จุด (%.2f%% -> %.2f%%)"
                              % (swing, prev["loss_rate"], cur["loss_rate"]),
                })

        # ตารางเทียบเป้ากับผลจริงทุกจุดวัดผล (รูปแบบเดียวกับรายงานนำเสนอผลงาน)
        rolling_by_no = {r["month_no"]: r for r in rolling}
        milestones = []
        for t in targets:
            actual = rolling_by_no.get(t["month_no"])
            row = {
                "month_no": t["month_no"],
                "month": SVC.month_from_no(start, t["month_no"]),
                "label": SVC.month_label_th(SVC.month_from_no(start, t["month_no"])),
                "target_rate": t["target_rate"],
                "drop_from_x": (
                    round(baseline_x - t["target_rate"], 3)
                    if baseline_x is not None else None
                ),
                "actual_rate": actual["loss_rate"] if actual else None,
                "status": "pending",
            }
            if actual:
                row["status"] = "pass" if actual["loss_rate"] <= t["target_rate"] \
                    else "fail"
                row["diff"] = round(actual["loss_rate"] - t["target_rate"], 2)
            milestones.append(row)

        # แรงดันเฉลี่ยเดือนล่าสุด เทียบกับแรงดันฐาน (สัญญาข้อ 1.32 / 2.1.2(7))
        pressure = None
        if months_all:
            last_month = months_all[-1]
            values = [
                rec["avg_pressure_24h"]
                for (m, code), rec in monthly.items()
                if m == last_month and code in dma_set
                and rec.get("avg_pressure_24h") is not None
            ]
            if values:
                current_p = sum(values) / len(values)
                base_p = contract.get("baseline_pressure_m")
                pressure = {
                    "month": last_month,
                    "label": SVC.month_label_th(last_month),
                    "current": round(current_p, 3),
                    "baseline": base_p,
                    "diff": round(current_p - base_p, 3) if base_p else None,
                    "n_dma": len(values),
                    # ข้อ 2.1.2(7): แรงดันต้องไม่ต่ำกว่าฐาน
                    # ถ้าฐานต่ำกว่า 10 ม. กปน. คุมไม่เกิน 10 ม.
                    "below_baseline": (base_p is not None and current_p < base_p),
                }

        work = SVC.get_monthly_work(cid)
        work_latest = None
        if months_all:
            for m in reversed(months_all):
                if m in work:
                    work_latest = dict(work[m])
                    work_latest["label"] = SVC.month_label_th(m)
                    break
        work_total = {
            "alc_main_pipe": sum(w["alc_main_pipe"] for w in work.values()),
            "alc_service_pipe": sum(w["alc_service_pipe"] for w in work.values()),
            "n_months": len(work),
        }

        months_available = months_all
        return {
            "milestones": milestones,
            "pressure": pressure,
            "work_latest": work_latest,
            "work_total": work_total,
            "data_issues": data_issues,
            "contract": contract,
            "targets": targets,
            "rolling": rolling,
            "monthly": per_month,
            "interim_line": [{"month_no": m, "rate": r} for m, r in interim],
            "latest": latest,
            "next_target": next_target,
            "forecast": forecast,
            "outlook": outlook,
            "dma_rows": rows,
            "breakdown": breakdown,
            "months_available": months_available,
            "months_label": [SVC.month_label_th(m) for m in months_available],
            "n_dma": len(dmas),
            "n_dma_no_rtu": sum(1 for r in rows if not r["has_rtu"]),
        }

    # -------------------------------------------------------------- หน้าเว็บ

    @bp.route("/")
    @guard
    def pbc_page():
        contract_id = request.args.get("contract_id") or None
        contract, contracts = _resolve_contract(contract_id)
        return render_template(
            "pbc.html",
            contracts=contracts,
            selected_id=contract["contract_id"] if contract else "",
            remark_categories=CFG.REMARK_CATEGORIES,
            overridable_fields=CFG.OVERRIDABLE_FIELDS,
            # ใช้โดย base_sidebar.html — active_page ทำให้เมนูถูกไฮไลต์
            active_page="pbc",
            user=user_obj(),
            enable_upload=CFG.ENABLE_WB220_UPLOAD,
        )

    @bp.route("/overview")
    @guard
    def pbc_overview_page():
        return render_template(
            "pbc_overview.html",
            active_page="pbc_overview",
            user=user_obj(),
        )

    # -------------------------------------------------------------- JSON

    @bp.route("/api/contracts")
    @guard
    def api_contracts():
        return jsonify({"ok": True, "contracts": _visible_contracts()})

    @bp.route("/api/overview")
    @guard
    def api_overview():
        contract, _ = _resolve_contract(request.args.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา หรือไม่มีสิทธิ์เข้าถึง"}), 404
        return jsonify({"ok": True, "data": _build_overview(contract)})

    @bp.route("/api/overview-all")
    @guard
    def api_overview_all():
        """ภาพรวมทุกสัญญาที่ผู้ใช้เห็นได้ — รวมสัญญาที่ยังไม่ลงนามด้วย"""
        contracts = _visible_contracts()
        procurement = SVC.get_procurement()
        agg_points = []
        rows = []

        for contract in contracts:
            cid = contract["contract_id"]
            start = contract["start_month"]
            baseline_x = contract["baseline_rate_x"]
            started = bool(start) and baseline_x is not None

            row = {
                "contract_id": cid,
                "contract_no": contract["contract_no"],
                "branch_name": contract["branch_name"],
                "area_name": contract["area_name"],
                "contractor_name": contract["contractor_name"],
                "start_month": start,
                "start_date": contract["start_date"],
                "end_date": contract["end_date"],
                "baseline_rate_x": baseline_x,
                "status": contract["status"],
                "started": started,
                "procurement": procurement.get(cid, []),
            }

            if not started:
                # ยังไม่ลงนาม แสดงเฉพาะความคืบหน้าการจัดจ้าง
                rows.append(row)
                continue

            dmas = {d["dma_code"] for d in SVC.get_contract_dmas(cid)}
            monthly = SVC.get_monthly_effective(cid)
            targets = SVC.get_targets(cid)
            milestones = [(t["month_no"], t["target_rate"]) for t in targets]
            rolling = SVC.rolling_series(monthly, start, dmas)

            for r in rolling:
                agg_points.append({
                    "month": r["month"],
                    "inflow_m3": r["inflow_m3"],
                    "sales_m3": r["sales_m3"],
                    "loss_m3": r["loss_m3"],
                    "plan_rate": FC.plan_rate_at(baseline_x, milestones, r["month_no"]),
                    "baseline_rate": baseline_x,
                })

            latest = rolling[-1] if rolling else None
            rolling_by_no = {r["month_no"]: r["loss_rate"] for r in rolling}
            next_target = None
            for t in targets:
                if latest is None or t["month_no"] >= latest["month_no"]:
                    next_target = t
                    break

            ms = []
            for t in targets:
                actual = rolling_by_no.get(t["month_no"])
                ms.append({
                    "month_no": t["month_no"],
                    "target_rate": t["target_rate"],
                    "actual_rate": actual,
                    "status": "pending" if actual is None else
                              ("pass" if actual <= t["target_rate"] else "fail"),
                })

            work = SVC.get_monthly_work(cid)
            row.update({
                "n_dma": len(dmas),
                "latest_month": latest["month"] if latest else None,
                "latest_label": latest["label"] if latest else None,
                "latest_month_no": latest["month_no"] if latest else None,
                "current_rate": latest["loss_rate"] if latest else None,
                "loss_m3_month": (
                    round(latest["loss_m3"] / CFG.ROLLING_MONTHS, 0)
                    if latest else None
                ),
                "next_target": next_target,
                "plan_rate_now": (
                    FC.plan_rate_at(baseline_x, milestones, latest["month_no"])
                    if latest else None
                ),
                "milestones": ms,
                "alc_main_pipe": sum(w["alc_main_pipe"] for w in work.values()),
                "alc_service_pipe": sum(w["alc_service_pipe"] for w in work.values()),
            })
            if row["current_rate"] is not None and row["plan_rate_now"] is not None:
                row["vs_plan"] = round(row["current_rate"] - row["plan_rate_now"], 2)
                row["on_plan"] = row["vs_plan"] <= 0
            rows.append(row)

        series = FC.aggregate_across_contracts(agg_points)
        started_rows = [r for r in rows if r["started"]]
        latest_agg = series[-1] if series else None
        summary = {
            "n_contracts": len(rows),
            "n_started": len(started_rows),
            "n_pending": len(rows) - len(started_rows),
            "n_on_plan": sum(1 for r in started_rows if r.get("on_plan") is True),
            "n_off_plan": sum(1 for r in started_rows if r.get("on_plan") is False),
            "n_dma": sum(r.get("n_dma", 0) for r in started_rows),
            "latest": latest_agg,
            "loss_m3_month": (
                round(latest_agg["loss_m3"] / CFG.ROLLING_MONTHS, 0)
                if latest_agg else None
            ),
            "alc_main_pipe": sum(r.get("alc_main_pipe", 0) for r in started_rows),
            "alc_service_pipe": sum(r.get("alc_service_pipe", 0) for r in started_rows),
        }

        # เลขจุดวัดผลทั้งหมดที่ปรากฏ ใช้เป็นหัวคอลัมน์ของเมทริกซ์
        all_ms = sorted({m["month_no"] for r in started_rows
                         for m in r.get("milestones", [])})

        for s_row in series:
            s_row["label"] = SVC.month_label_th(s_row["month"])

        return jsonify({
            "ok": True,
            "data": {
                "summary": summary,
                "contracts": rows,
                "series": series,
                "milestone_columns": all_ms,
                "procurement_steps": CFG.PROCUREMENT_STEPS,
            },
        })

    @bp.route("/api/diagnose")
    @guard
    def api_diagnose():
        """
        ตรวจว่าทำไมสัญญาหนึ่งไม่มีข้อมูลแสดง ทั้งที่กรอก MonthlyRaw แล้ว
        ไล่ทีละชั้นตามลำดับที่ระบบใช้จริง แล้วบอกว่าขาดตรงไหน
        """
        contract, _ = _resolve_contract(request.args.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        cid = contract["contract_id"]

        raw_all = SVC.read_tab(CFG.TAB_MONTHLY_RAW)
        ids_in_raw = sorted({(r.get("contract_id") or "").strip()
                             for r in raw_all if r.get("contract_id")})
        mine = [r for r in raw_all
                if (r.get("contract_id") or "").strip() == cid]

        dma_rows = SVC.get_contract_dmas(cid)
        dma_set = {d["dma_code"] for d in dma_rows}
        raw_codes = sorted({(r.get("dma_code") or "").strip() for r in mine})
        raw_months = sorted({(r.get("month") or "").strip() for r in mine})

        bad_month = [m for m in raw_months if SVC.normalize_month(m) is None]
        converted = [m for m in raw_months
                     if SVC.normalize_month(m) is not None and len(m) != 7]
        matched = [c for c in raw_codes if c in dma_set]
        only_raw = [c for c in raw_codes if c not in dma_set]
        only_contract = sorted(dma_set - set(raw_codes))

        monthly = SVC.get_monthly_effective(cid)
        usable = {m for (m, code) in monthly if code in dma_set}
        rolling = SVC.rolling_series(monthly, contract["start_month"], dma_set)

        problems = []
        if not mine:
            problems.append(
                "ไม่มีแถวใน MonthlyRaw ที่ contract_id ตรงกับ %r เลย "
                "— รหัสที่พบในตารางคือ %s" % (cid, ids_in_raw))
        if bad_month:
            problems.append(
                "อ่านเดือนไม่ได้ %d ค่า เช่น %s — แถวเหล่านี้ถูกข้ามทั้งหมด "
                "ต้องแก้ให้เป็น YYYY-MM"
                % (len(bad_month), bad_month[:5]))
        if converted:
            problems.append(
                "Google Sheets แปลงเดือนเป็นวันที่ %d ค่า เช่น %s "
                "— ระบบตัดเฉพาะปี-เดือนมาใช้ให้แล้ว ข้อมูลยังถูกต้อง "
                "แต่ควรตั้งรูปแบบคอลัมน์เป็นข้อความก่อนวางครั้งต่อไป"
                % (len(converted), converted[:3]))
        if mine and not matched:
            problems.append(
                "รหัสพื้นที่ใน MonthlyRaw ไม่ตรงกับใน ContractDMA เลยสักตัว "
                "— ใน MonthlyRaw เช่น %s แต่ใน ContractDMA เช่น %s"
                % (raw_codes[:3], sorted(dma_set)[:3]))
        if matched and not usable:
            problems.append("จับคู่รหัสได้แต่ยังไม่มีเดือนใดใช้งานได้")
        if usable and not rolling:
            problems.append(
                "มีข้อมูล %d เดือน แต่ยังไม่มีเดือนใดที่ครบ 3 เดือนติดกัน "
                "— รอบวัดผลต้องใช้เดือนนั้นและ 2 เดือนก่อนหน้า "
                "เดือนที่มี: %s" % (len(usable), sorted(usable)))
        if not contract["start_month"]:
            problems.append("ยังไม่ได้กรอก start_month ในตาราง Contracts")
        if contract["baseline_rate_x"] is None:
            problems.append("ยังไม่ได้กรอก baseline_rate_x ในตาราง Contracts")

        return jsonify({"ok": True, "data": {
            "contract_id": cid,
            "start_month": contract["start_month"],
            "baseline_rate_x": contract["baseline_rate_x"],
            "n_rows_monthlyraw_total": len(raw_all),
            "n_rows_this_contract": len(mine),
            "contract_ids_found_in_raw": ids_in_raw,
            "n_dma_in_contractdma": len(dma_set),
            "n_dma_codes_in_raw": len(raw_codes),
            "n_codes_matched": len(matched),
            "codes_only_in_raw": only_raw[:20],
            "codes_only_in_contractdma": only_contract[:20],
            "months_in_raw": raw_months,
            "months_bad_format": bad_month[:20],
            "months_converted_by_sheets": converted[:20],
            "n_usable_months": len(usable),
            "n_rolling_points": len(rolling),
            "problems": problems or ["ไม่พบปัญหา ข้อมูลควรแสดงผลได้ปกติ"],
            "sample_rows": mine[:3],
        }})

    @bp.route("/api/dma/<dma_code>")
    @guard
    def api_dma(dma_code):
        contract, _ = _resolve_contract(request.args.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        cid = contract["contract_id"]
        monthly = SVC.get_monthly_effective(cid)
        history = []
        for month in SVC.available_months(monthly):
            rec = monthly.get((month, dma_code))
            if not rec:
                continue
            history.append({
                "month": month,
                "label": SVC.month_label_th(month),
                "inflow_m3": round(rec["inflow_m3"], 0),
                "sales_m3": round(rec["sales_m3"], 0),
                "loss_m3": round(rec["loss_m3"], 0),
                "loss_rate": round(rec["loss_rate"], 2)
                if rec["loss_rate"] is not None else None,
                "avg_pressure_24h": rec["avg_pressure_24h"],
                "avg_pressure_night": rec["avg_pressure_night"],
                "avg_flow_night": rec["avg_flow_night"],
                "overrides": rec["overrides"],
            })
        return jsonify({
            "ok": True,
            "dma_code": dma_code,
            "history": history,
            "mnf_daily": SVC.get_mnf_daily(dma_code),
            "envelope": SVC.get_hourly_envelope(dma_code),
            "meters": SVC.get_meter_map({dma_code}).get(dma_code, []),
            "remarks": SVC.get_remarks(cid, dma_code),
        })

    # -------------------------------------------------------------- อัปโหลด

    @bp.route("/api/upload", methods=["POST"])
    @guard
    def api_upload():
        if not CFG.ENABLE_WB220_UPLOAD:
            return jsonify({
                "ok": False,
                "error": "ปิดการอัปโหลดรายงาน WB220 ไว้ "
                         "ให้กรอกปริมาณน้ำเข้า/น้ำขายในตาราง MonthlyRaw แทน",
            }), 403
        contract, _ = _resolve_contract(request.form.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404

        upload = request.files.get("file")
        if not upload or not upload.filename:
            return jsonify({"ok": False, "error": "ยังไม่ได้เลือกไฟล์"}), 400
        if not upload.filename.lower().endswith(".xls"):
            return jsonify({
                "ok": False,
                "error": "รองรับเฉพาะไฟล์ .xls ที่ export จากระบบ WLMA (AN/WB220)",
            }), 400

        tmp = tempfile.NamedTemporaryFile(suffix=".xls", delete=False)
        try:
            upload.save(tmp.name)
            tmp.close()
            parsed = pbc_parser.parse_wb220(tmp.name)
        except pbc_parser.ParseError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

        cid = contract["contract_id"]
        month = parsed["month"]
        allowed = {d["dma_code"] for d in SVC.get_contract_dmas(cid, month)}
        rows = [r for r in parsed["rows"] if r["dma_code"] in allowed]
        skipped = [r["dma_code"] for r in parsed["rows"]
                   if r["dma_code"] not in allowed]
        missing = sorted(allowed - {r["dma_code"] for r in rows})

        if not rows:
            return jsonify({
                "ok": False,
                "error": "ไม่มีพื้นที่ใดในไฟล์ที่ตรงกับสัญญานี้ "
                         "(ตรวจว่าเลือกสัญญาถูกสาขาหรือไม่)",
            }), 400

        existing = SVC.get_monthly_effective(cid)
        already = sorted({c for (m, c) in existing.keys() if m == month})
        confirm = str(request.form.get("confirm", "")).lower() in ("1", "true", "yes")
        if already and not confirm:
            preview = []
            for r in rows:
                old = existing.get((month, r["dma_code"]))
                if not old:
                    continue
                preview.append({
                    "dma_code": r["dma_code"],
                    "old_inflow": round(old["inflow_m3"], 0),
                    "new_inflow": round(r["inflow_m3"], 0),
                    "old_sales": round(old["sales_m3"], 0),
                    "new_sales": round(r["billed_total_m3"] + r["other_m3"], 0),
                })
            return jsonify({
                "ok": False,
                "needs_confirm": True,
                "month": month,
                "month_label": SVC.month_label_th(month),
                "n_existing": len(already),
                "preview": preview,
                "message": "เดือน %s มีข้อมูลอยู่แล้ว %d พื้นที่ "
                           "การอัปโหลดซ้ำจะใช้ค่าจากไฟล์ใหม่แทน "
                           "(ค่าเดิมยังเก็บไว้ในประวัติ)"
                           % (SVC.month_label_th(month), len(already)),
            }), 409

        warnings = list(parsed["warnings"])
        if skipped:
            warnings.append("ข้ามพื้นที่นอกสัญญา %d รายการ: %s"
                            % (len(skipped), ", ".join(skipped)))
        if missing:
            warnings.append("ไม่พบข้อมูลของพื้นที่ในสัญญา %d รายการ: %s"
                            % (len(missing), ", ".join(missing)))

        upload_id, n = SVC.save_upload(
            cid, month, upload.filename, rows, who(), warnings
        )
        return jsonify({
            "ok": True,
            "upload_id": upload_id,
            "month": month,
            "month_label": SVC.month_label_th(month),
            "branch_name": parsed["branch_name"],
            "n_saved": n,
            "n_skipped": len(skipped),
            "n_missing": len(missing),
            "warnings": warnings,
        })

    # -------------------------------------------------------------- ปรับค่า

    @bp.route("/api/override", methods=["POST"])
    @guard
    def api_override():
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404

        field = data.get("field")
        if field not in CFG.OVERRIDABLE_FIELDS:
            return jsonify({"ok": False, "error": "ปรับค่าช่องนี้ไม่ได้"}), 400
        value = SVC.to_float(data.get("value"))
        if value is None or value < 0:
            return jsonify({"ok": False, "error": "ค่าที่กรอกต้องเป็นตัวเลขไม่ติดลบ"}), 400
        reason = (data.get("reason") or "").strip()
        if len(reason) < 5:
            return jsonify({
                "ok": False,
                "error": "ต้องระบุเหตุผลอย่างน้อย 5 ตัวอักษร "
                         "เพราะตัวเลขนี้ใช้ประเมินผลตามสัญญา",
            }), 400

        SVC.save_override(
            contract["contract_id"], data.get("dma_code"), data.get("month"),
            field, value, reason, who(),
        )
        return jsonify({"ok": True})

    # -------------------------------------------------------------- เป้าย่อย

    @bp.route("/api/targets", methods=["POST"])
    @guard
    def api_targets():
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        rows = data.get("targets") or []
        if not rows:
            return jsonify({"ok": False, "error": "ไม่มีเป้าหมายให้บันทึก"}), 400
        n = SVC.save_dma_targets(
            contract["contract_id"], data.get("measure_month_no"), rows, who()
        )
        return jsonify({"ok": True, "n_saved": n})

    @bp.route("/api/preview-targets", methods=["POST"])
    @guard
    def api_preview_targets():
        """คำนวณการเกลี่ยเป้าใหม่หลังผู้ใช้แก้บางช่อง โดยยังไม่บันทึก"""
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        basis = data.get("basis") or []
        manual = data.get("manual") or {}
        sales = SVC.to_float(data.get("sales_m3"), 0.0)
        rate = SVC.to_float(data.get("target_rate"))
        if rate is None:
            return jsonify({"ok": False, "error": "ไม่มีอัตราเป้าหมาย"}), 400
        hours = contract["mnf_hours_per_day"] or CFG.DEFAULT_MNF_HOURS_PER_DAY
        rows, summary = FC.build_dma_targets(
            basis, sales, rate / 100.0, hours, CFG.DAYS_PER_MONTH, manual=manual
        )
        for r in rows:
            for key in ("loss_m3", "target_loss_m3", "reduction_m3", "potential_m3"):
                if r.get(key) is not None:
                    r[key] = round(r[key], 0)
            r["target_mnf"] = FC.target_mnf_for(r, hours, CFG.DAYS_PER_MONTH)
            if r["target_mnf"] is not None:
                r["target_mnf"] = round(r["target_mnf"], 1)
        return jsonify({"ok": True, "rows": rows, "summary": summary})

    # -------------------------------------------------------------- บันทึกงาน

    @bp.route("/api/work", methods=["POST"])
    @guard
    def api_work():
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        month = (data.get("month") or "").strip()
        if len(month) != 7 or "-" not in month:
            return jsonify({"ok": False, "error": "รูปแบบเดือนต้องเป็น YYYY-MM"}), 400
        main_pipe = SVC.to_int(data.get("alc_main_pipe"), 0)
        service_pipe = SVC.to_int(data.get("alc_service_pipe"), 0)
        if main_pipe < 0 or service_pipe < 0:
            return jsonify({"ok": False, "error": "จำนวนต้องไม่ติดลบ"}), 400
        SVC.save_monthly_work(
            contract["contract_id"], month, main_pipe, service_pipe,
            data.get("note", ""), who(),
        )
        return jsonify({"ok": True})

    @bp.route("/api/remark", methods=["POST"])
    @guard
    def api_remark():
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "ยังไม่ได้กรอกรายละเอียด"}), 400
        SVC.save_remark(
            contract["contract_id"], data.get("dma_code"),
            data.get("event_date") or "", data.get("category") or "อื่นๆ",
            text, who(),
        )
        return jsonify({"ok": True})

    return bp
