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
                         branch_scope_fn=None, url_prefix="/pbc"):
    """
    login_required   : decorator ของแอปเดิม
    current_user_fn  : ฟังก์ชันคืนชื่อผู้ใช้ปัจจุบัน (ใช้บันทึกว่าใครแก้อะไร)
    branch_scope_fn  : ฟังก์ชันคืน list รหัสสาขาที่ผู้ใช้เห็นได้ (None = ทุกสาขา)
    """
    guard = login_required or _passthrough
    who = current_user_fn or (lambda: "unknown")
    scope = branch_scope_fn or (lambda: None)

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

        months_available = SVC.available_months(monthly)
        return {
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
