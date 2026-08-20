"""
app.py
Flask Web App สำหรับ Smart NRW (NRW Job Management)

รันในเครื่อง (dev):   python app.py
รันบนเซิร์ฟเวอร์จริง (production): gunicorn app:app --bind 0.0.0.0:$PORT
(ดูขั้นตอน deploy ใน README.md หัวข้อ "Deploy บน Render")
"""

import os
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify

import alert_service
import auth_service
import dashboard_service
import incident_service
import job_service
import org_service
import remark_service
import sheets_client as sc
from constants import (
    ROLE_ADMIN, ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR, ASSIGNER_ROLES,
    ROLE_ENGINEER, ROLE_LEVELS,
)
from schema_setup import SHEET_SCHEMAS

app = Flask(__name__)
# ในเครื่อง (dev): ไม่ตั้ง SECRET_KEY ก็ได้ จะใช้ค่า fallback ด้านล่างแทน
# บน Render (production): ต้องตั้ง environment variable SECRET_KEY เป็นค่าสุ่มที่คาดเดาไม่ได้
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me-in-production")


def _status_badge_class(status):
    """แปลงสถานะงานเป็น CSS class ของ badge สี — ใช้เป็น Jinja filter |status_class
    เพื่อไม่ต้องประกาศ macro status_badge ซ้ำในทุกไฟล์ template"""
    progress_statuses = {"มอบหมายแล้ว รอรับ", "รับงานแล้ว", "กำลังดำเนินการ", "ตีกลับ"}
    if status == "รอมอบหมาย":
        return "status-pending"
    if status in progress_statuses:
        return "status-progress"
    if status == "เสร็จ รอตรวจสอบ":
        return "status-verify"
    if status == "ปิดงาน":
        return "status-done"
    if status == "ยกเลิกงาน":
        return "status-cancel"
    if status == "ปฏิเสธ":
        return "status-reject"
    return "status-pending"


app.jinja_env.filters["status_class"] = _status_badge_class


def _date_display(value):
    """แปลงวันที่จาก ISO (yyyy-mm-dd) ที่เก็บจริงในระบบ ให้แสดงผลเป็น dd/mm/yyyy (ค.ศ.)
    ใช้เฉพาะตอน 'แสดงผล' เท่านั้น — ค่าที่เก็บจริงยังเป็น ISO เหมือนเดิม (ใช้เทียบ string
    ในกฎ 'DueDate ใหม่ต้องไม่น้อยกว่าเดิม' ต่อไปได้ตามปกติ) ถ้าค่าไม่ตรงรูปแบบ ISO ที่คาดไว้
    คืนค่าเดิมโดยไม่แตะต้อง (กันพังกรณีข้อมูลเพี้ยน)"""
    if not value:
        return value
    parts = str(value).split("-")
    if len(parts) != 3:
        return value
    year, month, day = parts
    if not (len(year) == 4 and year.isdigit() and month.isdigit() and day.isdigit()):
        return value
    return f"{day}/{month}/{year}"


app.jinja_env.filters["date_display"] = _date_display


def _user_short_display(user_id):
    """แปลง UserID -> 'ชื่อ(คำแรก) / ตำแหน่ง' สำหรับแสดงในตาราง (เช่น ปิดงานโดย)
    ถ้าหา user ไม่เจอ (เช่นถูกลบไปแล้ว) แสดง UserID ดิบแทน ไม่ error"""
    if not user_id:
        return "—"
    user = sc.find_one("Users", "UserID", user_id)
    if not user:
        return user_id
    name = (user.get("Name") or "").strip().split(" ")[0]
    role = user.get("Role") or ""
    return f"{name} / {role}" if name else user_id


app.jinja_env.filters["user_short"] = _user_short_display

# โหลดข้อมูลจาก Google Sheets เข้า cache ครั้งเดียวตอนโมดูลนี้ถูก import
# (ต้องอยู่นอก "if __name__ == '__main__'" เพราะตอน deploy จริงจะใช้ gunicorn
#  import ตัวแปร app จากไฟล์นี้โดยตรง ไม่ได้รันไฟล์นี้เป็นสคริปต์หลัก โค้ดใน
#  if __name__ == "__main__" จะไม่ถูกเรียกเลยในกรณีนั้น)
print("=== Warm up cache ===")
sc.warm_up(list(SHEET_SCHEMAS.keys()))


def login_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        token = session.get("token")
        user = auth_service.get_current_user(token) if token else None
        if not user:
            flash("กรุณาเข้าสู่ระบบก่อนใช้งาน", "error")
            return redirect(url_for("login"))
        request.current_user = user
        return view_func(*args, **kwargs)
    return wrapped


def get_optional_user():
    """คืนข้อมูล user ถ้า login อยู่ ไม่ redirect ถ้าไม่ได้ login (ใช้กับหน้าที่ไม่บังคับ login
    แต่ต้องรู้ว่าใคร login อยู่หรือเปล่า เพื่อสลับเมนู sidebar)"""
    token = session.get("token")
    return auth_service.get_current_user(token) if token else None


@app.route("/", methods=["GET"])
def monitoring():
    user = get_optional_user()
    # จำกัด tab "การดำเนินการ" (บันทึกสถานะงาน) ให้เฉพาะวิศวกรขึ้นไปเท่านั้น (เดิมเปิดให้ทุกคน)
    can_manage_job = bool(user) and auth_service.role_level(user.get("Role")) <= ROLE_LEVELS[ROLE_ENGINEER]
    return render_template(
        "monitoring.html", user=user, active_page="monitoring", can_manage_job=can_manage_job
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            token = auth_service.login(username, password)
            session["token"] = token
            if is_ajax:
                return jsonify(success=True, redirect=url_for("dashboard"))
            return redirect(url_for("dashboard"))
        except ValueError as e:
            if is_ajax:
                return jsonify(success=False, error=str(e))
            flash(str(e), "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    token = session.get("token")
    if token:
        auth_service.logout(token)
    session.clear()
    flash("ออกจากระบบแล้ว", "info")
    return redirect(url_for("monitoring"))


@app.route("/dashboard")
@login_required
def dashboard():
    user = request.current_user
    jobs = dashboard_service.get_dashboard_jobs(user)
    incidents = dashboard_service.get_dashboard_incidents(user)

    # filter: ภาค (กลุ่มสาขา) + สาขา + DMA (โซน) — ทำงานร่วมกับ view ได้ ไม่รีเซ็ตกัน
    branch_group_filter = request.args.get("branch_group", "")
    branch_filter = request.args.get("branch", "")
    zone_filter = request.args.get("zone", "")
    jobs = dashboard_service.filter_by_branch_group_and_zone(jobs, branch_group_filter, branch_filter, zone_filter)
    incidents = dashboard_service.filter_by_branch_group_and_zone(incidents, branch_group_filter, branch_filter, zone_filter)

    # เรียงงานที่ยังไม่จบก่อน (สถานะไม่ใช่ ปิดงาน/ยกเลิกงาน) เพื่อให้เห็นงานที่ต้องติดตามก่อน
    active_jobs = [j for j in jobs if j.get("Status") not in ("ปิดงาน", "ยกเลิกงาน")]
    done_jobs = [j for j in jobs if j.get("Status") in ("ปิดงาน", "ยกเลิกงาน")]

    # view: กด card สรุปแล้วกรองว่าจะโชว์ตารางไหน (all = โชว์ครบทุกตารางเหมือนเดิม)
    view = request.args.get("view", "all")
    if view not in ("all", "active", "done", "incidents"):
        view = "all"

    summary = {
        "total_jobs": len(jobs),
        "active_jobs": len(active_jobs),
        "done_jobs": len(done_jobs),
        "total_incidents": len(incidents),
    }

    job_permissions = {j["JobID"]: dashboard_service.get_job_permissions(j, user) for j in jobs}
    lateral_candidates_map = {
        j["JobID"]: (
            org_service.get_lateral_transfer_candidates_for_job(j, user)
            if job_permissions[j["JobID"]]["can_transfer"] else []
        )
        for j in jobs
    }
    job_type_name_map = {
        jt["JobTypeID"]: jt["JobTypeName"] for jt in sc.get_all_records("JobTypes")
    }
    zone_name_map = {z["ZoneID"]: z["ZoneName"] for z in sc.get_all_records("Zones")}
    branch_name_map = {b["BranchID"]: b["BranchName"] for b in sc.get_all_records("Branches")}
    zones = sc.get_all_records("Zones")
    incident_permissions = {
        i["IncidentID"]: incident_service.get_incident_permissions(i, user) for i in incidents
    }
    branch_group_options = dashboard_service.get_branch_group_options(user)
    branch_options = dashboard_service.get_branch_options(user, branch_group_filter)
    zone_options = dashboard_service.get_zone_options(user, branch_group_filter, branch_filter)

    return render_template(
        "dashboard.html",
        user=user,
        active_page="dashboard",
        active_jobs=active_jobs,
        done_jobs=done_jobs,
        incidents=incidents,
        summary=summary,
        view=view,
        branch_group_filter=branch_group_filter,
        branch_filter=branch_filter,
        zone_filter=zone_filter,
        branch_group_options=branch_group_options,
        branch_options=branch_options,
        zone_options=zone_options,
        job_permissions=job_permissions,
        lateral_candidates_map=lateral_candidates_map,
        job_type_name_map=job_type_name_map,
        zone_name_map=zone_name_map,
        branch_name_map=branch_name_map,
        zones=zones,
        incident_permissions=incident_permissions,
    )


def _branches_visible_to(user):
    """สาขาที่ user คนนี้เลือกได้ตอนแจ้งเหตุการณ์"""
    role = user.get("Role")
    all_branches = sc.get_all_records("Branches")
    if role == ROLE_ADMIN:
        return all_branches
    if role in (ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR):
        return [b for b in all_branches if b.get("BranchGroupID") == user.get("BranchGroupID")]
    # Role อื่นๆ ผูกกับสาขาตนเองอยู่แล้ว
    return [b for b in all_branches if b.get("BranchID") == user.get("BranchID")]


@app.route("/incidents/new", methods=["GET", "POST"])
@login_required
def new_incident():
    user = request.current_user
    branches = _branches_visible_to(user)

    if request.method == "POST":
        branch_id = request.form.get("branch_id") or user.get("BranchID")
        zone_id = request.form.get("zone_id", "")
        incident_type = request.form.get("incident_type", "").strip()
        description = request.form.get("description", "").strip()
        severity = request.form.get("severity", "กลาง")

        if not branch_id or not incident_type:
            flash("กรุณาเลือกสาขาและระบุประเภทเหตุการณ์", "error")
        else:
            incident_id = incident_service.create_incident(
                incident_type=incident_type,
                description=description,
                severity=severity,
                reported_by=user["UserID"],
                zone_id=zone_id,
                branch_id=branch_id,
            )
            flash(f"แจ้งเหตุการณ์ {incident_id} เรียบร้อยแล้ว", "info")
            return redirect(url_for("convert_incident", incident_id=incident_id))

    zones = sc.get_all_records("Zones")
    incident_types = sc.get_all_records("IncidentTypes")
    return render_template("new_incident.html", user=user, active_page="new_incident",
                            branches=branches, zones=zones, incident_types=incident_types)


@app.route("/incidents/<incident_id>/convert", methods=["GET", "POST"])
@login_required
def convert_incident(incident_id):
    user = request.current_user
    incident = sc.find_one("Incidents", "IncidentID", incident_id)
    if not incident:
        flash("ไม่พบเหตุการณ์นี้", "error")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        job_type_ids = request.form.getlist("job_type_ids")
        if not job_type_ids:
            flash("กรุณาเลือกประเภทงานอย่างน้อย 1 ประเภท", "error")
        else:
            try:
                job_ids = incident_service.convert_incident_to_jobs(incident_id, job_type_ids, user)
                flash(f"จ่ายงานเรียบร้อย: {', '.join(job_ids)}", "info")
                return redirect(url_for("dashboard"))
            except (PermissionError, ValueError) as e:
                flash(str(e), "error")

    job_types = sc.get_all_records("JobTypes")
    zone_name_map = {z["ZoneID"]: z["ZoneName"] for z in sc.get_all_records("Zones")}
    branch_name_map = {b["BranchID"]: b["BranchName"] for b in sc.get_all_records("Branches")}
    zones = sc.get_all_records("Zones")
    incident_permissions = incident_service.get_incident_permissions(incident, user)
    return render_template("convert_incident.html", user=user, active_page="new_incident",
                            incident=incident, job_types=job_types,
                            zone_name_map=zone_name_map, branch_name_map=branch_name_map,
                            zones=zones, incident_permissions=incident_permissions)


@app.route("/incidents/<incident_id>/update", methods=["POST"])
@login_required
def incident_update(incident_id):
    user = request.current_user
    description = request.form.get("description")
    severity = request.form.get("severity")
    zone_id = request.form.get("zone_id")
    due_date = request.form.get("due_date")
    try:
        incident_service.update_incident_details(
            incident_id, user,
            description=description, severity=severity,
            zone_id=zone_id, due_date=due_date,
        )
        flash(f"บันทึกรายละเอียดเหตุการณ์ {incident_id} แล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("dashboard")


@app.route("/incidents/<incident_id>/close", methods=["POST"])
@login_required
def incident_close(incident_id):
    user = request.current_user
    try:
        incident_service.close_incident(incident_id, user)
        flash(f"ปิดเหตุการณ์ {incident_id} เรียบร้อยแล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("dashboard")


@app.route("/remarks/clear", methods=["POST"])
@login_required
def remark_clear():
    user = request.current_user
    # จำกัดเฉพาะวิศวกรขึ้นไป (เช็คซ้ำฝั่ง server แม้ frontend จะซ่อน tab ไว้แล้วก็ตาม)
    if auth_service.role_level(user.get("Role")) > ROLE_LEVELS[ROLE_ENGINEER]:
        return jsonify(success=False, error="ไม่มีสิทธิ์ (ต้องเป็นวิศวกรขึ้นไป)")

    payload = request.get_json(silent=True) or {}
    rtu_id = payload.get("rtu_id", "")
    if not rtu_id:
        return jsonify(success=False, error="ไม่พบ RTU ID")

    try:
        removed = remark_service.clear_remark_for_rtu(rtu_id)
        return jsonify(success=True, removed=removed)
    except Exception as e:
        return jsonify(success=False, error=f"เกิดข้อผิดพลาดไม่คาดคิด: {e}")


@app.route("/alerts/save", methods=["POST"])
@login_required
def alert_save():
    user = request.current_user
    payload = request.get_json(silent=True) or {}
    try:
        alert_id = alert_service.save_alert(
            user=user,
            rtu_id=payload.get("rtu_id", ""),
            rtu_name=payload.get("rtu_name", ""),
            case_classification=payload.get("case", ""),
            mnf_value=payload.get("mnf_value", ""),
            cusum=payload.get("cusum", ""),
            trend_result=payload.get("trend", ""),
            note=payload.get("note", ""),
            image_data_url=payload.get("image_data_url", ""),
        )
        return jsonify(success=True, alert_id=alert_id)
    except (PermissionError, ValueError) as e:
        return jsonify(success=False, error=str(e))
    except Exception as e:
        return jsonify(success=False, error=f"เกิดข้อผิดพลาดไม่คาดคิด: {e}")


@app.route("/alerts")
@login_required
def alerts_list():
    user = request.current_user
    alerts = alert_service.get_alerts_for_user(user)
    alert_permissions = {a["AlertID"]: alert_service.get_alert_permissions(a, user) for a in alerts}
    return render_template(
        "alerts.html",
        user=user,
        active_page="alerts",
        alerts=alerts,
        alert_permissions=alert_permissions,
    )


@app.route("/alerts/<alert_id>/convert", methods=["POST"])
@login_required
def alert_convert(alert_id):
    user = request.current_user
    try:
        incident_id = alert_service.convert_alert_to_incident(alert_id, user)
        flash(f"แปลงแจ้งเตือนเป็นเหตุการณ์ {incident_id} เรียบร้อยแล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("alerts_list"))


@app.route("/alerts/status/<rtu_id>")
def alert_status(rtu_id):
    # public endpoint (ไม่บังคับ login) — แค่บอกว่า RTU นี้มีเหตุการณ์เชื่อมโยงหรือไม่
    # ใช้แสดงข้อความใน popup ของหน้า Monitoring ที่เปิดให้คนไม่ login ดูได้อยู่แล้ว
    alert = alert_service.get_alert_status_for_rtu(rtu_id)
    if alert and alert.get("LinkedIncidentID"):
        return jsonify(linked=True, incident_id=alert["LinkedIncidentID"])
    return jsonify(linked=False)


@app.route("/incidents/tree")
@login_required
def incident_tree():
    user = request.current_user
    incidents = dashboard_service.get_dashboard_incidents(user)
    incident_ids_visible = {i["IncidentID"] for i in incidents}

    selected_id = request.args.get("incident_id", "")
    selected_incident = None
    jobs = []
    job_permissions = {}
    lateral_candidates_map = {}
    incident_permissions = {}

    if selected_id:
        if selected_id not in incident_ids_visible:
            flash("ไม่พบเหตุการณ์นี้ในขอบเขตของคุณ", "error")
        else:
            selected_incident = sc.find_one("Incidents", "IncidentID", selected_id)
            incident_permissions = incident_service.get_incident_permissions(selected_incident, user)
            jobs = sc.find_many("Jobs", "SiblingJobGroup", selected_id)
            job_permissions = {
                job["JobID"]: dashboard_service.get_job_permissions(job, user) for job in jobs
            }
            lateral_candidates_map = {
                job["JobID"]: (
                    org_service.get_lateral_transfer_candidates_for_job(job, user)
                    if job_permissions[job["JobID"]]["can_transfer"] else []
                )
                for job in jobs
            }

    job_type_name_map = {
        jt["JobTypeID"]: jt["JobTypeName"] for jt in sc.get_all_records("JobTypes")
    }
    zone_name_map = {z["ZoneID"]: z["ZoneName"] for z in sc.get_all_records("Zones")}
    branch_name_map = {b["BranchID"]: b["BranchName"] for b in sc.get_all_records("Branches")}
    zones = sc.get_all_records("Zones")

    return render_template(
        "incident_tree.html",
        user=user,
        active_page="incident_tree",
        incidents=incidents,
        selected_id=selected_id,
        selected_incident=selected_incident,
        jobs=jobs,
        job_type_name_map=job_type_name_map,
        job_permissions=job_permissions,
        lateral_candidates_map=lateral_candidates_map,
        zone_name_map=zone_name_map,
        branch_name_map=branch_name_map,
        zones=zones,
        incident_permissions=incident_permissions,
    )


@app.route("/my-jobs")
@login_required
def my_jobs():
    user = request.current_user
    action_jobs = dashboard_service.get_my_action_jobs(user)
    job_type_name_map = {
        jt["JobTypeID"]: jt["JobTypeName"] for jt in sc.get_all_records("JobTypes")
    }
    lateral_candidates_map = {
        job["JobID"]: org_service.get_lateral_transfer_candidates_for_job(job, user)
        for job in action_jobs["assigned_to_me"]
    }
    return render_template(
        "my_jobs.html",
        user=user,
        active_page="my_jobs",
        assigned_to_me=action_jobs["assigned_to_me"],
        pending_verify=action_jobs["pending_verify"],
        pending_close=action_jobs["pending_close"],
        job_type_name_map=job_type_name_map,
        lateral_candidates_map=lateral_candidates_map,
    )


def _safe_redirect(default_endpoint):
    """redirect กลับไปที่ 'next' ถ้ามีและเป็น path ภายในเว็บเราเท่านั้น (กัน open redirect)
    ถ้าไม่มีหรือไม่ปลอดภัย ใช้ default_endpoint แทน"""
    next_url = request.form.get("next", "")
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for(default_endpoint))


@app.route("/jobs/<job_id>/accept", methods=["POST"])
@login_required
def job_accept(job_id):
    user = request.current_user
    try:
        job_service.accept_job(job_id, user)
        flash(f"รับงาน {job_id} เรียบร้อยแล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("my_jobs")


@app.route("/jobs/<job_id>/reject", methods=["POST"])
@login_required
def job_reject(job_id):
    user = request.current_user
    reason = request.form.get("reason", "").strip()
    try:
        job_service.reject_job(job_id, user, reason or "ไม่ระบุเหตุผล")
        flash(f"ปฏิเสธงาน {job_id} แล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("my_jobs")


@app.route("/jobs/<job_id>/submit-completion", methods=["POST"])
@login_required
def job_submit_completion(job_id):
    user = request.current_user
    remarks = request.form.get("remarks", "").strip()
    try:
        job_service.submit_completion(job_id, user, remarks)
        flash(f"ส่งงาน {job_id} เสร็จแล้ว รอตรวจสอบ", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("my_jobs")


@app.route("/jobs/<job_id>/verify", methods=["POST"])
@login_required
def job_verify(job_id):
    user = request.current_user
    passed = request.form.get("passed") == "1"
    notes = request.form.get("notes", "").strip()
    try:
        job_service.verify_job(job_id, user, passed, notes)
        flash(f"บันทึกผลตรวจสอบงาน {job_id} แล้ว ({'ผ่าน' if passed else 'ไม่ผ่าน - ตีกลับ'})", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("my_jobs")


@app.route("/jobs/<job_id>/close", methods=["POST"])
@login_required
def job_close(job_id):
    user = request.current_user
    try:
        job_service.close_job(job_id, user)
        flash(f"ปิดงาน {job_id} เรียบร้อยแล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("my_jobs")


@app.route("/jobs/<job_id>/transfer", methods=["POST"])
@login_required
def job_transfer(job_id):
    user = request.current_user
    to_user_id = request.form.get("to_user_id", "")
    if not to_user_id:
        flash("กรุณาเลือกผู้รับโอนงาน", "error")
        return _safe_redirect("my_jobs")
    try:
        job_service.lateral_transfer(job_id, to_user_id, user)
        flash(f"โอนงาน {job_id} เรียบร้อยแล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return _safe_redirect("my_jobs")


@app.route("/jobs/manage")
@login_required
def manage_jobs():
    user = request.current_user
    if user.get("Role") not in ASSIGNER_ROLES and user.get("Role") != ROLE_ADMIN:
        flash("Role นี้ไม่มีสิทธิ์เข้าหน้ามอบหมายงาน", "error")
        return redirect(url_for("dashboard"))

    job_type_name_map = {
        jt["JobTypeID"]: jt["JobTypeName"] for jt in sc.get_all_records("JobTypes")
    }

    pending_jobs = dashboard_service.get_assignable_jobs(user)
    job_candidates = {
        job["JobID"]: org_service.get_assignable_users_for_job(job, user)
        for job in pending_jobs
    }

    tracking_jobs = dashboard_service.get_dashboard_jobs(user)
    tracking_permissions = {
        j["JobID"]: dashboard_service.get_job_permissions(j, user) for j in tracking_jobs
    }
    lateral_candidates_map = {
        j["JobID"]: (
            org_service.get_lateral_transfer_candidates_for_job(j, user)
            if tracking_permissions[j["JobID"]]["can_transfer"] else []
        )
        for j in tracking_jobs
    }

    return render_template(
        "manage_jobs.html",
        user=user,
        active_page="manage_jobs",
        pending_jobs=pending_jobs,
        job_candidates=job_candidates,
        tracking_jobs=tracking_jobs,
        tracking_permissions=tracking_permissions,
        lateral_candidates_map=lateral_candidates_map,
        job_type_name_map=job_type_name_map,
    )


@app.route("/jobs/<job_id>/assign", methods=["POST"])
@login_required
def job_assign(job_id):
    user = request.current_user
    to_user_id = request.form.get("to_user_id", "")
    if not to_user_id:
        flash("กรุณาเลือกผู้รับมอบหมาย", "error")
        return redirect(url_for("manage_jobs"))
    try:
        job_service.assign_job(job_id, to_user_id, user)
        flash(f"มอบหมายงาน {job_id} เรียบร้อยแล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("manage_jobs"))


if __name__ == "__main__":
    # รันแบบนี้ตอนพัฒนาในเครื่องเท่านั้น (python app.py)
    # ตอน deploy จริงบน Render จะใช้ gunicorn เรียก app:app โดยตรง ไม่ผ่านส่วนนี้เลย
    app.run(debug=True, host="127.0.0.1", port=5000)
