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
from datetime import datetime
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
                         branch_scope_fn=None, user_fn=None, url_prefix="/pbc",
                         job_user_fn=None, job_branch_ids_fn=None):
    """
    login_required   : decorator ของแอปเดิม
    current_user_fn  : ฟังก์ชันคืนชื่อผู้ใช้ปัจจุบัน (ใช้บันทึกว่าใครแก้อะไร)
    branch_scope_fn  : ฟังก์ชันคืน list รหัสสาขาที่ผู้ใช้เห็นได้ (None = ทุกสาขา)
    user_fn          : ฟังก์ชันคืน object ผู้ใช้สำหรับ base_sidebar.html
                       (ต้องมี .Name และ .Role) ถ้าแอปใส่ผ่าน context_processor
                       อยู่แล้วไม่ต้องส่งมา
    job_user_fn      : ฟังก์ชันคืน dict ผู้ใช้ที่ login ระบบ Job management อยู่
                       (UserID, Name, Role) หรือ None — ใช้กับแผนงานบนกราฟและ
                       การสร้างเหตุการณ์ในระบบ Job ถ้าไม่ส่งมา ฟีเจอร์ส่งงานเข้า
                       ระบบ Job จะปิดไว้
    job_branch_ids_fn: ฟังก์ชันรับ dict ผู้ใช้ คืน list BranchID (ระบบ Job) ที่ผู้ใช้
                       สร้างเหตุการณ์ได้ — ใช้กฎเดียวกับหน้าแจ้งเหตุการณ์ของระบบ Job
    """
    guard = login_required or _passthrough
    who = current_user_fn or (lambda: "unknown")
    scope = branch_scope_fn or (lambda: None)
    user_obj = user_fn or (lambda: None)
    job_user = job_user_fn or (lambda: None)

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
        if not start:
            # ยังไม่ได้กรอกเดือนเริ่มสัญญา คำนวณอะไรไม่ได้เลย
            # แจ้งให้เห็นบนหน้าจอแทนที่จะพังทั้งหน้า
            return {
                "contract": contract, "targets": [], "rolling": [], "monthly": [],
                "interim_line": [], "latest": None, "next_target": None,
                "forecast": {"level": "none", "message": "", "points": []},
                "outlook": None, "dma_rows": [], "milestones": [], "pressure": None,
                "work_latest": None, "work_total": {}, "progress": None,
                "breakdown": {"rows": [], "summary": None, "measure_month_no": None},
                "months_available": [], "months_label": [],
                "n_dma": len(SVC.get_contract_dmas(cid)), "n_dma_no_rtu": 0,
                "data_issues": [],
                "setup_error": "ยังไม่ได้กรอกเดือนเริ่มสัญญา (start_month) "
                               "ในตาราง Contracts — ต้องเป็นรูปแบบ YYYY-MM",
            }
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

        # เป้าย่อยราย DMA อ้างอิงเป้าสุดท้ายของสัญญา ไม่ใช่จุดวัดผลถัดไป
        # เพราะการวางแผนลดน้ำสูญเสียต้องมองปลายทางทั้งสัญญา
        # ถ้าวางแผนทีละจุดวัดผล จะได้เป้าที่หลวมและต้องเร่งงานหนักช่วงท้าย
        final_target = targets[-1] if targets else None
        breakdown = {"rows": [], "summary": None, "measure_month_no": None}
        if final_target and latest:
            saved = SVC.get_dma_targets(cid, final_target["month_no"])
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
                basis, sales_month, final_target["target_rate"] / 100.0,
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
                "measure_month_no": final_target["month_no"],
                "measure_month": SVC.month_from_no(start, final_target["month_no"]),
                "target_rate": final_target["target_rate"],
                "is_final": True,
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

        # ความคืบหน้าตามสัญญา นับจากเดือนปฏิทินปัจจุบันเทียบกับเดือนเริ่มสัญญา
        # จำนวนเดือนทั้งสัญญาใช้จุดวัดผลสุดท้ายเป็นหลัก เพราะเป็นตัวที่ผูกพันจริง
        # ถ้ายังไม่มีจุดวัดผล ค่อยประมาณจากระยะเวลาสัญญาเป็นวัน
        progress = None
        if start:
            total = max([t["month_no"] for t in targets], default=0)
            if not total and contract.get("duration_days"):
                total = int(round(contract["duration_days"] / 30.44))
            now_month = datetime.now().strftime("%Y-%m")
            elapsed = SVC.month_no(start, now_month)
            if total:
                progress = {
                    "month_no": max(min(elapsed, total), 0),
                    "total_months": total,
                    "pct": round(max(min(elapsed, total), 0) / total * 100, 0),
                    # นับเฉพาะเดือนที่อยู่ในสัญญาจริง ไม่รวมเดือนก่อนเริ่มสัญญา
                    # ที่ใส่ไว้เพื่อให้รอบ 3 เดือนแรกคำนวณได้
                    "months_with_data": sum(
                        1 for m in months_all if SVC.month_no(start, m) >= 1
                    ),
                    "latest_label": (
                        SVC.month_label_th(months_all[-1]) if months_all else None
                    ),
                    "ended": elapsed > total,
                }

        # ความสดของข้อมูล MNF ใช้บอกผู้ใช้ว่าต้องรันสคริปต์ใหม่หรือยัง
        rtu_dates = [v.get("last_date") for v in rtu.values() if v.get("last_date")]
        rtu_info = {
            "n_dma": len(dmas),
            "n_with_data": sum(1 for v in rtu.values()
                               if v.get("mnf_current") is not None),
            "last_date": max(rtu_dates) if rtu_dates else None,
            "oldest_date": min(rtu_dates) if rtu_dates else None,
        }

        months_available = months_all
        return {
            "rtu_info": rtu_info,
            "progress": progress,
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
            enable_override=CFG.ENABLE_OVERRIDE_UI,
            enable_remarks=CFG.ENABLE_REMARKS,
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
        data = _build_overview(contract)
        data.update(_events_payload(contract))
        return jsonify({"ok": True, "data": data})

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
                "billed_total_m3": round(rec["billed_total_m3"], 0),
                "other_m3": round(rec["other_m3"], 0),
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
        if not CFG.ENABLE_OVERRIDE_UI:
            return jsonify({
                "ok": False,
                "error": "ปิดการปรับค่าจากหน้าเว็บไว้ "
                         "ให้แก้ตัวเลขในตาราง MonthlyRaw แทน",
            }), 403
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
        # ถ้าผู้ใช้กำหนดปริมาณรวมที่จะลดมาเอง ให้ใช้ค่านั้นแทนที่จะคิดจากอัตราเป้า
        total_reduction = SVC.to_float(data.get("total_reduction_m3"))
        rows, summary = FC.build_dma_targets(
            basis, sales, rate / 100.0, hours, CFG.DAYS_PER_MONTH,
            manual=manual, total_reduction=total_reduction,
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

    # -------------------------------------------------------------- แผนงาน/เหตุการณ์บนกราฟ

    def _editor():
        """คืน (ผู้ใช้ระบบ Job, ชื่อที่ใช้บันทึก) — ผู้ใช้เป็น None ถ้ายังไม่ login"""
        user = job_user()
        if user:
            return user, (user.get("Name") or user.get("UserID") or "")
        return None, who()

    def _incident_info(incident_id):
        """สถานะเหตุการณ์และงานลูกในระบบ Job — ห่อ try ไว้ ระบบ Job ล่มก็ไม่พังหน้า PBC"""
        try:
            import sheets_client as jsc
            inc = jsc.find_one("Incidents", "IncidentID", incident_id)
            if not inc:
                return {"id": incident_id, "found": False}
            jobs = jsc.find_many("Jobs", "SiblingJobGroup", incident_id)
            return {
                "id": incident_id, "found": True,
                "status": inc.get("Status", ""),
                "conversion": inc.get("ConversionStatus", ""),
                "due_date": str(inc.get("DueDate") or ""),
                "n_jobs": len(jobs),
                "n_closed": sum(1 for j in jobs if j.get("Status") == "ปิดงาน"),
                "n_cancelled": sum(1 for j in jobs if j.get("Status") == "ยกเลิกงาน"),
            }
        except Exception as exc:  # noqa: BLE001 — แสดงเป็นข้อความบนหน้าแทน
            return {"id": incident_id, "found": None, "error": str(exc)}

    def _events_payload(contract):
        try:
            events = SVC.get_events(contract["contract_id"])
            error = ""
        except Exception as exc:  # noqa: BLE001 — กราฟหลักต้องแสดงได้แม้ tab นี้มีปัญหา
            events, error = [], "อ่านแผนงานไม่สำเร็จ: %s" % exc
        for ev in events:
            ev["incident"] = (_incident_info(ev["linked_incident_id"])
                              if ev["linked_incident_id"] else None)
        user, name = _editor()
        return {
            "events": events,
            "event_summary": SVC.summarize_events(events),
            "event_meta": {
                "types": {k: v["label"] for k, v in CFG.EVENT_TYPES.items()},
                "status": CFG.EVENT_STATUS,
                # ถ้าแอปส่งระบบ login มาให้ ต้อง login ก่อนจึงแก้ได้
                "can_edit": bool(user) or job_user_fn is None,
                "can_send_job": bool(user),
                "job_enabled": job_user_fn is not None,
                "user_name": name if user else "",
                "error": error,
            },
        }

    def _event_input(data, contract_id):
        """ตรวจค่าจากฟอร์ม คืน (dict, ข้อความผิดพลาด)"""
        etype = (data.get("event_type") or "").strip()
        if etype not in CFG.EVENT_TYPES:
            return None, "ประเภทไม่ถูกต้อง"
        month = SVC.normalize_month(data.get("month"))
        if not month:
            return None, "ต้องระบุเดือน (YYYY-MM)"
        title = (data.get("title") or "").strip()
        if not title:
            return None, "ต้องกรอกหัวข้อ"
        status = (data.get("status") or "plan").strip()
        if status not in CFG.EVENT_STATUS:
            return None, "สถานะไม่ถูกต้อง"
        red = data.get("reduction_m3h")
        red_val = ""
        if CFG.EVENT_TYPES[etype]["mark"] == "box" and red not in (None, ""):
            red_val = SVC.to_float(red)
            if red_val is None or red_val < 0:
                return None, "ปริมาณที่คาดว่าลดได้ต้องเป็นตัวเลขไม่ติดลบ"
        items = data.get("items") or []
        if isinstance(items, str):
            items = items.splitlines()
        items = [str(x).strip() for x in items if str(x).strip()]

        # ปริมาณแยกราย DMA (เฉพาะกล่องเหลือง) — ถ้ากรอกมา ยอดรวมต้องเท่าผลรวมราย DMA
        alloc = {}
        raw_alloc = data.get("dma_alloc") or {}
        if CFG.EVENT_TYPES[etype]["mark"] == "box" and isinstance(raw_alloc, dict):
            valid = {d["dma_code"] for d in SVC.get_contract_dmas(contract_id)}
            for code, val in raw_alloc.items():
                code = str(code).strip()
                if val in (None, ""):
                    continue
                num = SVC.to_float(val)
                if num is None or num < 0:
                    return None, "ปริมาณราย DMA (%s) ต้องเป็นตัวเลขไม่ติดลบ" % code
                if code not in valid:
                    return None, "DMA %s ไม่อยู่ในสัญญานี้" % code
                if num > 0:
                    alloc[code] = num
        if alloc:
            red_val = round(sum(alloc.values()), 2)
        return {
            "event_type": etype, "month": month, "title": title,
            "items": items, "reduction_m3h": red_val, "status": status,
            "note": (data.get("note") or "").strip(), "dma_alloc": alloc,
        }, ""

    def _need_editor():
        user, name = _editor()
        if job_user_fn is not None and not user:
            return None, (jsonify({"ok": False,
                                   "error": "กรุณาเข้าสู่ระบบก่อนแก้ไขแผนงาน"}), 401)
        return name, None

    # -------------------------------------------------------------- ตารางแผนกิจกรรมราย DMA

    def _activity_plan(contract, base_no=None):
        """
        ตารางแผนกิจกรรมลดน้ำสูญเสียราย DMA (รูปแบบตาราง Excel ที่ใช้ติดตาม PBC เดิม)

        ฐาน   = ยอดรอบ 3 เดือน ณ เดือนวัดผลที่เลือก (ตรึงค่า) แปลงเป็นต่อวัน
        ต่อจุดวัดผลถัดไปแต่ละจุด:
          เป้าสะสม (ลบ.ม./ชม.)   = ปริมาณที่ต้องลดจากฐานให้ถึงอัตราเป้า กระจายราย DMA
                                    ด้วยวิธีเดียวกับแท็บเป้าหมายย่อย (ศักยภาพ MNF)
          กิจกรรมปรับปรุง/สำรวจ  = แผนงานที่แล้วเสร็จหลังจุดวัดผลก่อนหน้า ถึงจุดวัดผลนี้
          ต้องเพิ่มกิจกรรม       = เป้าสะสม − กิจกรรมสะสม (บวก = ยังขาด)
          ลดน้ำเข้า/วัน          = เป้าสะสม × ชม./วัน
          NF ตามกิจกรรม          = NF ปัจจุบัน − กิจกรรมสะสม
          น้ำเข้า/สูญเสียเป้าหมาย = ค่าฐาน − ลดน้ำเข้า/วัน (สมมติน้ำจำหน่ายคงที่)
        """
        cid = contract["contract_id"]
        start = contract["start_month"]
        hours = contract["mnf_hours_per_day"] or CFG.DEFAULT_MNF_HOURS_PER_DAY
        if not start:
            return {"error": "ยังไม่ได้กรอกเดือนเริ่มสัญญา"}
        dmas = [d["dma_code"] for d in SVC.get_contract_dmas(cid)]
        dma_set = set(dmas)
        monthly = SVC.get_monthly_effective(cid)
        targets = SVC.get_targets(cid)
        rtu = SVC.get_dma_rtu_status(dma_set)
        rolling = SVC.rolling_series(monthly, start, dma_set)
        if not rolling:
            return {"error": "ยังไม่มีข้อมูลครบรอบ 3 เดือน"}
        roll_by_no = {r["month_no"]: r for r in rolling}

        # ตัวเลือกฐาน: จุดวัดผลที่มีข้อมูลแล้ว + เดือนล่าสุดที่มีข้อมูล
        options = []
        for t in targets:
            if t["month_no"] in roll_by_no:
                options.append({"month_no": t["month_no"], "kind": "milestone",
                                "label": "จุดวัดผลเดือนที่ %d (%s)" % (
                                    t["month_no"], roll_by_no[t["month_no"]]["label"])})
        latest = rolling[-1]
        if not options or options[-1]["month_no"] != latest["month_no"]:
            options.append({"month_no": latest["month_no"], "kind": "latest",
                            "label": "ข้อมูลล่าสุด (%s)" % latest["label"]})
        valid_nos = [o["month_no"] for o in options]
        if base_no not in valid_nos:
            # ค่าเริ่มต้น = จุดวัดผลล่าสุดที่วัดได้แล้ว (ถ้ายังไม่มี ใช้ข้อมูลล่าสุด)
            ms = [o["month_no"] for o in options if o["kind"] == "milestone"]
            base_no = ms[-1] if ms else latest["month_no"]
        base = roll_by_no[base_no]
        window = base["window"]
        days = 0
        for ym in window:
            y, m = int(ym[:4]), int(ym[5:7])
            days += (datetime(y + (m == 12), m % 12 + 1, 1) - datetime(y, m, 1)).days

        # ---- ฐานราย DMA (ต่อวัน)
        rows = []
        for code in dmas:
            agg = SVC.aggregate(monthly, set(window), {code})
            if not agg["n_records"]:
                inflow = sales = loss = None
            else:
                inflow = agg["inflow_m3"] / days
                sales = agg["sales_m3"] / days
                loss = agg["loss_m3"] / days
            info = rtu.get(code, {})
            rows.append({
                "dma_code": code,
                "inflow_d": inflow, "sales_d": sales, "loss_d": loss,
                "loss_rate": (loss / inflow * 100.0) if inflow else None,
                "inflow_h": inflow / hours if inflow is not None else None,
                "nf_current": info.get("mnf_current"),
                "mnf_floor": info.get("mnf_floor"),
            })
        basis = [{
            "dma_code": r["dma_code"], "inflow_m3": r["inflow_d"],
            "sales_m3": r["sales_d"], "loss_m3": r["loss_d"],
            "mnf_current": r["nf_current"], "mnf_floor": r["mnf_floor"],
        } for r in rows if r["loss_d"] is not None]
        sales_total = sum(b["sales_m3"] for b in basis)

        # ---- แผนงาน (เฉพาะกล่องเหลือง ที่ยังไม่ยกเลิก และเสร็จหลังเดือนฐาน)
        base_month = base["month"]
        events = [e for e in SVC.get_events(cid)
                  if e["mark"] == "box" and e["status"] != "cancelled"]
        future = [t for t in targets if t["month_no"] > base_no]
        UNASSIGNED = "ไม่ระบุ DMA"

        groups = []
        prev_month = base_month
        cum_act = {}            # DMA -> กิจกรรมสะสม (ลบ.ม./ชม.)
        used_events = set()
        for t in future:
            ms_month = SVC.month_from_no(start, t["month_no"])
            # เป้าราย DMA: ใช้ค่าที่ผู้ใช้ล็อกไว้ในแท็บเป้าหมายย่อย (ถ้ามี) แปลงเป็นต่อวัน
            saved = SVC.get_dma_targets(cid, t["month_no"])
            manual = {c: v["target_loss_m3"] / CFG.DAYS_PER_MONTH
                      for c, v in saved.items()
                      if v["is_manual"] and v["target_loss_m3"] is not None}
            alloc, summary = FC.build_dma_targets(
                basis, sales_total, t["target_rate"] / 100.0, hours, 1, manual=manual)
            target_h = {a["dma_code"]: a["reduction_m3"] / hours for a in alloc}

            pipe, alc = {}, {}
            for e in events:
                if not (prev_month < e["month"] <= ms_month):
                    continue
                used_events.add(e["event_id"])
                bucket = pipe if e["event_type"] == "pipe" else alc
                parts = e["dma_alloc"] or (
                    {UNASSIGNED: e["reduction_m3h"]} if e["reduction_m3h"] else {})
                for code, val in parts.items():
                    bucket[code] = bucket.get(code, 0.0) + val
                    cum_act[code] = cum_act.get(code, 0.0) + val

            cells = {}
            for r in rows:
                code = r["dma_code"]
                tgt = target_h.get(code)
                act = cum_act.get(code, 0.0)
                cut_d = tgt * hours if tgt is not None else None
                inflow_t = r["inflow_d"] - cut_d if cut_d is not None else None
                loss_t = r["loss_d"] - cut_d if cut_d is not None else None
                cells[code] = {
                    "pipe": pipe.get(code), "alc": alc.get(code),
                    "gap": (tgt - act) if tgt is not None else None,
                    "target_cum": tgt, "cut_d": cut_d,
                    "nf_target": (r["nf_current"] - act)
                    if r["nf_current"] is not None else None,
                    "inflow_d": inflow_t, "loss_d": loss_t,
                    "loss_rate": (loss_t / inflow_t * 100.0) if inflow_t else None,
                }
            if UNASSIGNED in cum_act:
                cells[UNASSIGNED] = {"pipe": pipe.get(UNASSIGNED), "alc": alc.get(UNASSIGNED),
                                     "cum_act": cum_act[UNASSIGNED]}
            groups.append({
                "month_no": t["month_no"], "month": ms_month,
                "label": SVC.month_label_th(ms_month),
                "target_rate": t["target_rate"],
                "unallocated_h": summary["unallocated_m3"] / hours,
                "cells": cells,
            })
            prev_month = ms_month

        # แผนงานที่แล้วเสร็จก่อนเดือนฐาน (ผลควรอยู่ในค่าฐานแล้ว) หรือหลังเป้าสุดท้าย
        skipped = [{"event_id": e["event_id"], "title": e["title"], "month_label": e["month_label"]}
                   for e in events if e["event_id"] not in used_events]
        return {
            "base": {"month_no": base_no, "month": base_month, "label": base["label"],
                     "window": window, "days": days, "loss_rate": base["loss_rate"]},
            "options": options,
            "hours": hours,
            "rows": rows,
            "groups": groups,
            "unassigned_label": UNASSIGNED,
            "skipped_events": skipped,
        }

    @bp.route("/api/activity-plan")
    @guard
    def api_activity_plan():
        contract, _ = _resolve_contract(request.args.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        data = _activity_plan(contract, SVC.to_int(request.args.get("base_no")))
        if data.get("error"):
            return jsonify({"ok": False, "error": data["error"]})
        return jsonify({"ok": True, "data": data})

    @bp.route("/api/events", methods=["POST"])
    @guard
    def api_event_save():
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        name, err = _need_editor()
        if err:
            return err
        rec, msg = _event_input(data, contract["contract_id"])
        if msg:
            return jsonify({"ok": False, "error": msg}), 400
        try:
            eid = SVC.save_event(contract["contract_id"], rec, name,
                                 event_id=(data.get("event_id") or "").strip() or None)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "event_id": eid})

    @bp.route("/api/events/<event_id>/delete", methods=["POST"])
    @guard
    def api_event_delete(event_id):
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        name, err = _need_editor()
        if err:
            return err
        try:
            SVC.save_event(contract["contract_id"], {}, name,
                           event_id=event_id, deleted=True)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @bp.route("/api/events/<event_id>/incident", methods=["POST"])
    @guard
    def api_event_incident(event_id):
        """
        สร้างเหตุการณ์ (Incident) ในระบบ Job management จากแผนงานบนกราฟ
        แล้วให้ผู้ใช้ไปจ่ายงานต่อที่หน้าจ่ายงานเดิมของระบบ Job
        """
        data = request.get_json(silent=True) or {}
        contract, _ = _resolve_contract(data.get("contract_id"))
        if not contract:
            return jsonify({"ok": False, "error": "ไม่พบสัญญา"}), 404
        user, name = _editor()
        if not user:
            return jsonify({"ok": False, "error":
                            "กรุณาเข้าสู่ระบบ Job management ก่อนสร้างเหตุการณ์"}), 401
        ev = SVC.get_event(contract["contract_id"], event_id)
        if not ev:
            return jsonify({"ok": False, "error": "ไม่พบรายการนี้"}), 404
        if ev["linked_incident_id"]:
            return jsonify({"ok": False, "error": "รายการนี้สร้างเป็นเหตุการณ์ %s ไปแล้ว"
                            % ev["linked_incident_id"]}), 400

        try:
            import incident_service
            import sheets_client as jsc
        except ImportError:
            return jsonify({"ok": False, "error": "ไม่พบโมดูลระบบ Job management"}), 500

        # รหัสสาขาในสัญญาเป็นรหัส กปน. ไม่ตรงกับ BranchID ของระบบ Job
        # จึงจับคู่ด้วยชื่อย่อสาขา (เช่น "สสภ.") กับคอลัมน์ BranchName
        abbr = SVC.contract_branch_abbr(contract)
        branch = None
        for b in jsc.get_all_records("Branches"):
            if abbr and str(b.get("BranchName") or "").strip() == abbr:
                branch = b
                break
        if not branch:
            return jsonify({"ok": False, "error":
                            "จับคู่สาขาของสัญญากับระบบ Job ไม่ได้ (ชื่อย่อสาขา '%s') — "
                            "ตรวจว่าชื่อพื้นที่ของสัญญามีชื่อย่อสาขา เช่น 'สสภ.' "
                            "และตรงกับคอลัมน์ BranchName ในตาราง Branches" % (abbr or "-")}), 400
        branch_id = str(branch.get("BranchID")).strip()
        if job_branch_ids_fn is not None:
            allowed = {str(x).strip() for x in (job_branch_ids_fn(user) or [])}
            if branch_id not in allowed:
                return jsonify({"ok": False, "error":
                                "สร้างเหตุการณ์ได้เฉพาะสาขาที่อยู่ในความรับผิดชอบ "
                                "(สาขานี้คือ %s)" % abbr}), 403

        lines = ["[PBC %s — %s] %s: %s" % (contract.get("contract_no", ""),
                                            contract.get("area_name", ""),
                                            ev["type_label"], ev["title"])]
        lines += ["- " + it for it in ev["items"]]
        if ev["dma_alloc"]:
            lines.append("แยกราย DMA (ลบ.ม./ชม.): " + ", ".join(
                "%s ≈ %g" % (k, v) for k, v in sorted(ev["dma_alloc"].items())))
        detail = "กำหนดแล้วเสร็จ " + ev["month_label"]
        if ev["reduction_m3h"] is not None:
            detail = ("คาดว่าลดน้ำสูญเสียได้ ≈ %s ลบ.ม./ชม. · " %
                      ("%g" % ev["reduction_m3h"])) + detail
        lines.append(detail)
        if ev["note"]:
            lines.append("หมายเหตุ: " + ev["note"])
        lines.append("สร้างจากหน้า ติดตามรายสัญญา PBC (รายการ %s)" % ev["event_id"])

        try:
            incident_id = incident_service.create_incident(
                incident_type="ลดน้ำสูญเสีย PBC: " + ev["type_label"],
                description="\n".join(lines),
                severity=CFG.EVENT_INCIDENT_SEVERITY,
                reported_by=user["UserID"],
                source=CFG.EVENT_INCIDENT_SOURCE,
                branch_id=branch_id,
            )
        except (PermissionError, ValueError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        # กำหนดเสร็จของเหตุการณ์ = วันสุดท้ายของเดือนในแผน
        y, m = (int(p) for p in ev["month"].split("-"))
        last_day = (datetime(y + (m == 12), m % 12 + 1, 1)
                    - datetime(y, m, 1)).days
        try:
            jsc.update_row("Incidents", "IncidentID", incident_id, {
                "DueDate": "%04d-%02d-%02d" % (y, m, last_day),
                "DueDateUpdatedAt": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception:  # noqa: BLE001 — ไม่มีกำหนดเสร็จก็ยังจ่ายงานได้
            pass

        changes = {"linked_incident_id": incident_id}
        if ev["status"] == "plan":
            changes["status"] = "doing"
        SVC.save_event(contract["contract_id"], changes, name, event_id=ev["event_id"])
        return jsonify({"ok": True, "incident_id": incident_id,
                        "convert_url": "/incidents/%s/convert" % incident_id})

    @bp.route("/api/remark", methods=["POST"])
    @guard
    def api_remark():
        if not CFG.ENABLE_REMARKS:
            return jsonify({"ok": False, "error": "ปิดการบันทึกการดำเนินงานไว้"}), 403
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
