"""
app.py
Flask Web App สำหรับ NRW Job Management (ต้นแบบหน้าจอ)
รันด้วย: python app.py  แล้วเปิดเบราว์เซอร์ไปที่ http://127.0.0.1:5000
"""

from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify

import auth_service
import dashboard_service
import incident_service
import sheets_client as sc
from constants import ROLE_ADMIN, ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR
from schema_setup import SHEET_SCHEMAS

app = Flask(__name__)
app.secret_key = "dev-secret-key-change-me-in-production"  # เปลี่ยนก่อนใช้งานจริง


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
    return render_template("monitoring.html", user=user, active_page="monitoring")


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

    # เรียงงานที่ยังไม่จบก่อน (สถานะไม่ใช่ ปิดงาน/ยกเลิกงาน) เพื่อให้เห็นงานที่ต้องติดตามก่อน
    active_jobs = [j for j in jobs if j.get("Status") not in ("ปิดงาน", "ยกเลิกงาน")]
    done_jobs = [j for j in jobs if j.get("Status") in ("ปิดงาน", "ยกเลิกงาน")]

    summary = {
        "total_jobs": len(jobs),
        "active_jobs": len(active_jobs),
        "done_jobs": len(done_jobs),
        "total_incidents": len(incidents),
    }

    return render_template(
        "dashboard.html",
        user=user,
        active_page="dashboard",
        active_jobs=active_jobs,
        done_jobs=done_jobs,
        incidents=incidents,
        summary=summary,
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
                flash(f"แปลงเป็นงานเรียบร้อย: {', '.join(job_ids)}", "info")
                return redirect(url_for("dashboard"))
            except (PermissionError, ValueError) as e:
                flash(str(e), "error")

    job_types = sc.get_all_records("JobTypes")
    return render_template("convert_incident.html", user=user, active_page="new_incident",
                            incident=incident, job_types=job_types)


if __name__ == "__main__":
    print("=== Warm up cache ===")
    sc.warm_up(list(SHEET_SCHEMAS.keys()))
    app.run(debug=True, host="127.0.0.1", port=5000)
