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
import trend_remark_service
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
try:
    sc.warm_up(list(SHEET_SCHEMAS.keys()))
except Exception as e:
    # warm_up ล้มเหลว (เช่น โดน rate limit ชั่วคราวตอนเริ่มโปรแกรม) ไม่ควรทำให้แอปทั้งตัวสตาร์ทไม่ได้
    # แต่ละ sheet จะถูกโหลดแบบ lazy (ตอนถูกเรียกใช้จริงครั้งแรก) แทน — ช้าลงเล็กน้อยในการ request
    # แรกๆ แต่แอปยังใช้งานได้ปกติ ไม่ต้องรอ restart service ด้วยมือ
    print(f"[app.py] warm_up ล้มเหลว (จะโหลดแบบ lazy แทน): {type(e).__name__}: {e}")


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


@app.route("/mediumterm")
def mediumterm_trend():
    """หน้าใหม่: ระบบเตือนแนวโน้ม MNF/ปริมาณน้ำเข้าเพิ่มขึ้นระยะกลาง (เทียบ 7 วันล่าสุด กับ baseline
    เดือนคงที่) — แยกออกจากตัวจับสัญญาณเฉียบพลัน Case A/B/C ในหน้า monitoring เดิมโดยเจตนา (คนละกลไก
    คนละไฟล์ข้อมูล คนละ threshold ไม่ปนกัน) ตัวเลข MNF/ปริมาณน้ำเข้าทั้งหมดโหลดจาก
    static/data/mediumterm_*.csv ฝั่ง JS เอง (เหมือน monitoring.html เดิมที่โหลด dma_status_summary.csv
    ฝั่ง client) — route นี้แค่ส่ง option ตัวกรองภาค/สาขา + สิทธิ์บันทึก remark ให้ template
    ดูได้โดยไม่ต้อง login เหมือนหน้า monitoring หลัก — ช่วงนี้ (ตามที่ผู้ใช้ขอ) เปิดให้บันทึก remark ได้
    โดยไม่ต้อง login ด้วยเช่นกัน (can_add_remark เปิดเสมอ) แต่ยังฝั่ง server ยังแยกอยู่ว่าใครล็อกอินหรือไม่
    ผ่าน user เพื่อเลือกว่าจะเชื่อชื่อจาก session หรือรับชื่อที่พิมพ์เองจากฟอร์ม (ดู mediumterm_save_remark)"""
    user = get_optional_user()
    can_add_remark = True

    branch_groups = sc.get_all_records("BranchGroups")
    branches = sc.get_all_records("Branches")

    return render_template(
        "mediumterm.html", user=user, active_page="mediumterm",
        can_add_remark=can_add_remark,
        branch_groups=branch_groups, branches=branches,
        reason_categories=trend_remark_service.REASON_CATEGORIES,
    )


@app.route("/mediumterm/rtu-branch-map")
def mediumterm_rtu_branch_map():
    """คืน {RTUID: {BranchID, BranchName, BranchGroupID, BranchGroupName}} ของทุก DMA ในครั้งเดียว —
    ให้ฝั่ง JS ใช้กรองตารางตามภาค/สาขาได้ (ตัวเลข MNF/ปริมาณน้ำเข้าอยู่ใน CSV แยกจาก Google Sheet
    จึงต้อง join กันฝั่ง client โดยจับคู่ dma_code ใน CSV กับ RTUID ใน sheet RTUs — เดียวกับที่ route
    /rtus/<rtu_id>/info ใช้อยู่แล้วสำหรับหน้า monitoring หลัก) ใช้ .get() แทน [] ทุกจุด + ห่อ try/except
    กันแถวไหนใน Sheet ไม่มีค่า RTUID (เซลล์ว่าง) แล้วทำให้ทั้ง endpoint 500 ไปเงียบๆ (พังแบบนี้เคยเจอมาแล้ว
    ตอน TrendRemarks — พอฝั่ง JS จับ error แบบเงียบ กลายเป็นตัวกรองภาค/สาขาไม่ขึ้นค่าเลยแทนที่จะเห็น error
    ชัดเจน จึงต้องกันตั้งแต่ backend ไม่ให้ error หลุดออกไปตั้งแต่ต้น)

    หมายเหตุบั๊กที่เจอจริง (ทำให้ error แบบไม่แน่นอน 'บางทีก็ได้บางทีก็ไม่ได้'): gspread.get_all_records()
    จะ numericise ค่าที่หน้าตาเป็นตัวเลขล้วนให้กลายเป็น int อัตโนมัติ (เช่นถ้า RTUID/BranchID/BranchGroupID
    แถวไหนเป็นตัวเลขล้วน จะได้ int ไม่ใช่ str) พอเอาไปเป็นคีย์ dict ตรงๆ จะได้ dict ที่คีย์ปนกันทั้ง int และ
    str แล้ว jsonify() (ใช้ json.dumps(..., sort_keys=True) เป็นค่าเริ่มต้นของ Flask) จะ error
    "'<' not supported between instances of 'int' and 'str'" ตอนเรียงคีย์ — แก้โดยบังคับ str() ทุกคีย์/
    ค่าที่ใช้จับคู่ เหมือนกับที่ sc.find_one()/find_many() ทำอยู่แล้ว (str(...) == str(...)) ด้วยเหตุผลเดียวกัน"""
    try:
        # force_refresh=True เฉพาะ endpoint นี้: ผู้ใช้เพิ่งแก้ RTUID/BranchID ใน Sheet เองสดๆ บ่อยครั้ง
        # (เจอแล้วว่าเพิ่มข้อมูลไปแล้วแต่ยังไม่ขึ้น เพราะติด cache 5 นาที) endpoint นี้ก็ยิง API ครั้งเดียว
        # ต่อการเปิดหน้าอยู่แล้ว ไม่ได้ยิงถี่จนเสี่ยงโควตา จึงยอมสละ cache เพื่อให้เห็นผลทันทีดีกว่า
        rtus = sc.get_all_records("RTUs", force_refresh=True)
        branches = {
            str(b.get("BranchID")).strip(): b
            for b in sc.get_all_records("Branches", force_refresh=True) if b.get("BranchID")
        }
        branch_groups = {
            str(g.get("BranchGroupID")).strip(): g
            for g in sc.get_all_records("BranchGroups", force_refresh=True) if g.get("BranchGroupID")
        }

        result = {}
        skipped_no_rtuid = 0
        for rtu in rtus:
            rtu_id_raw = rtu.get("RTUID")
            if not rtu_id_raw:
                skipped_no_rtuid += 1
                continue  # แถวว่าง/ไม่มี RTUID ใน Sheet — ข้ามแทนที่จะ error ทั้ง endpoint
            # .strip() กันเคสกรอกข้อมูลมีช่องว่างเกินมาโดยไม่รู้ตัว (เว้นวรรคหน้า/หลัง) ทำให้หน้าตาเหมือนกัน
            # ทุกอย่างแต่เทียบไม่ตรงกันเป๊ะๆ แบบไม่มีทางสังเกตเห็นด้วยตา — เจอปัญหานี้จริงกับ DM-14-11-18-01
            rtu_id = str(rtu_id_raw).strip()
            branch_id = str(rtu.get("BranchID")).strip() if rtu.get("BranchID") else ""
            branch = branches.get(branch_id, {})
            group_id = str(branch.get("BranchGroupID")).strip() if branch.get("BranchGroupID") else ""
            group = branch_groups.get(group_id, {})
            entry = {
                "BranchID": branch_id,
                "BranchName": branch.get("BranchName", ""),
                "BranchGroupID": group_id,
                "BranchGroupName": group.get("BranchGroupName", ""),
            }
            result[rtu_id] = entry
            # เผื่อ dma_code จาก CSV บางแถวตรงกับคอลัมน์ RTUName แทน RTUID (ข้อมูลกรอกไม่ตรงคอลัมน์ใน Sheet
            # เป็นบางแถว — เจอเคสจริงเช่น DM-14-11-18-01 หาไม่เจอตอนจับคู่ด้วย RTUID อย่างเดียว) จึงใส่คีย์
            # สำรองจาก RTUName ไว้ด้วย ถ้ามีค่าและไม่ซ้ำกับ RTUID — ให้ JS จับคู่ได้ไม่ว่า dma_code จะตรงกับ
            # คอลัมน์ไหนก็ตาม (RTUID ที่แท้จริงมาก่อนเสมอ ไม่ให้ RTUName ทับของจริง)
            rtu_name_raw = rtu.get("RTUName")
            if rtu_name_raw:
                rtu_name = str(rtu_name_raw).strip()
                if rtu_name != rtu_id and rtu_name not in result:
                    result[rtu_name] = entry
        # ถ้าจับคู่ไม่ได้เลยสักแถว แนบข้อมูลวินิจฉัยไปในคีย์สำรอง "_debug" ด้วย (RTUID จริงจะไม่ชนคีย์นี้
        # แน่นอน เพราะรูปแบบ DM-xx-xx-xx-xx) — ให้ฝั่งเว็บโชว์ตรงๆ ได้เลยโดยไม่ต้องเข้าไปดู log บนเซิร์ฟเวอร์
        # (ผู้ใช้เข้าถึง log ฝั่ง Render ไม่ได้สะดวก)
        if not result:
            result["_debug"] = {
                "rtus_total": len(rtus),
                "skipped_no_rtuid": skipped_no_rtuid,
                "sample_rtu_row_keys": list(rtus[0].keys()) if rtus else [],
                "sample_rtu_row": rtus[0] if rtus else None,
            }
        return jsonify(result)
    except Exception as e:
        print(f"[mediumterm_rtu_branch_map] โหลด branch map ไม่สำเร็จ: {e}")
        return jsonify({"_debug": {"error": str(e)}})


@app.route("/mediumterm/remarks-latest")
def mediumterm_remarks_latest():
    """คืน remark ล่าสุดของทุก DMA ในครั้งเดียว (bulk) — สำหรับโชว์แบบย่อในตารางหลัก
    ห่อ try/except ไว้เพราะถ้า tab "TrendRemarks" ยังไม่ถูกสร้าง (ยังไม่ได้รัน schema_setup.py รอบล่าสุด)
    sheets_client จะ raise ValueError ข้อความชัดเจน — ถ้าปล่อยให้หลุดออกไป Flask จะตอบเป็นหน้า error HTML
    กลับไป ทำให้ฝั่ง JS ที่ทำ fetch(...).then(r => r.json()) พังด้วย "Unexpected token '<'" (parse HTML เป็น
    JSON ไม่ได้) แล้วทั้งหน้าโหลดไม่ขึ้นเลยทั้งที่ CSV หลัก/branch map ใช้ได้ปกติ — ตอบ {} ไปแทนดีกว่า
    (หน้าเว็บยังใช้งานได้ แค่ remark ยังไม่ขึ้น จนกว่าจะรัน schema_setup ให้ครบ)"""
    try:
        return jsonify(trend_remark_service.get_latest_remarks_map())
    except Exception as e:
        print(f"[mediumterm_remarks_latest] โหลด remark ไม่สำเร็จ: {e}")
        return jsonify({})


@app.route("/mediumterm/remarks/<rtu_id>")
def mediumterm_remarks_for_rtu(rtu_id):
    """คืนประวัติ remark ทั้งหมดของ DMA นี้ เรียงใหม่สุดก่อน (สำหรับ popup กราฟ) — ห่อ try/except ด้วยเหตุผล
    เดียวกับ mediumterm_remarks_latest() ด้านบน"""
    try:
        return jsonify(trend_remark_service.get_remarks_for_rtu(rtu_id))
    except Exception as e:
        print(f"[mediumterm_remarks_for_rtu] โหลดประวัติ remark ของ {rtu_id} ไม่สำเร็จ: {e}")
        return jsonify([])


@app.route("/mediumterm/save-remark", methods=["POST"])
def mediumterm_save_remark():
    """บันทึก remark ใหม่ 1 แถว (append เข้าประวัติ ไม่ overwrite ของเดิม) — ตามที่ผู้ใช้ขอ (2026-09) เปิดให้
    ทุกคนบันทึกได้โดยไม่ต้อง login ก่อน (เดิมบังคับ @login_required) ถ้า login อยู่ ยังใช้ชื่อจาก session
    เสมือนเดิม (กันปลอมชื่อคนที่ login จริง) แต่ถ้าไม่ได้ login รับชื่อที่พิมพ์เองจากฟอร์มแทน"""
    user = get_optional_user()
    rtu_id = request.form.get("rtu_id", "").strip()
    reason_category = request.form.get("reason_category", "").strip()
    detail = request.form.get("detail", "").strip()
    event_date = request.form.get("event_date", "").strip()
    recorded_by_input = request.form.get("recorded_by", "").strip()
    if not rtu_id or not reason_category:
        return jsonify({"success": False, "error": "ข้อมูลไม่ครบ"}), 400

    recorded_by = (user.get("Name") or user.get("UserID")) if user else (recorded_by_input or "ไม่ระบุชื่อ")

    # save จริงห้ามเงียบเฉยๆ เหมือน 2 endpoint ข้างบน (ผู้ใช้ต้องรู้ว่าบันทึกไม่สำเร็จ) แต่ยัง catch ไว้กันหน้า
    # error HTML หลุดไปให้ JS parse พัง — ส่ง error message ที่อ่านออกกลับไปแทน
    try:
        row = trend_remark_service.save_trend_remark(
            rtu_id=rtu_id, reason_category=reason_category, detail=detail,
            recorded_by=recorded_by,
            event_date=event_date or None,
        )
    except Exception as e:
        print(f"[mediumterm_save_remark] บันทึก remark ไม่สำเร็จ: {e}")
        return jsonify({"success": False, "error": f"บันทึกไม่สำเร็จ: {e}"}), 500
    return jsonify({"success": True, "remark": row})


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
                return jsonify(success=True, redirect=url_for("monitoring"))
            return redirect(url_for("monitoring"))
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
    incidents = dashboard_service.filter_active_incidents(incidents)  # ซ่อนเหตุการณ์ที่ปิดแล้วออกจากตาราง
    jobs = dashboard_service.filter_jobs_with_open_incidents(jobs)  # ซ่อน Job ที่เหตุการณ์แม่ปิดแล้วออกด้วย

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


@app.route("/rtus/<rtu_id>/info")
def rtu_info(rtu_id):
    # public endpoint (ไม่บังคับ login) — ใช้แสดงชื่อสาขาใน popup ของหน้า Monitoring ที่เปิดให้คนไม่ login ดูได้อยู่แล้ว
    return jsonify(branch_name=org_service.get_branch_name_for_rtu(rtu_id))


@app.route("/alerts/linked-map")
@login_required
def alerts_linked_map():
    # เฉพาะ login แล้วเท่านั้น (ตามที่สั่ง) — ใช้เป็นตัวกรอง "เปิดเหตุการณ์แล้ว" ในหน้า Monitoring
    # ดึงทีเดียวหมดทั้งระบบ กันหน้า Monitoring ยิง API ทีละ RTU เป็นร้อยๆ ครั้ง
    return jsonify(alert_service.get_all_linked_rtu_ids())


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
            current_mnf=payload.get("current_mnf", ""),
            baseline_mean=payload.get("baseline_mean", ""),
            night_flow_window=payload.get("night_flow_window", ""),
            mnf_value=payload.get("mnf_value", ""),
            cusum=payload.get("cusum", ""),
            trend_result=payload.get("trend", ""),
            chart_date=payload.get("chart_date", ""),
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
        flash(f"สร้างเหตุการณ์ {incident_id} จากแจ้งเตือนนี้เรียบร้อยแล้ว", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("alerts_list"))


@app.route("/alerts/<alert_id>/cancel", methods=["POST"])
@login_required
def alert_cancel(alert_id):
    user = request.current_user
    try:
        alert_service.cancel_alert(alert_id, user)
        flash(f"ยกเลิกแจ้งเตือน {alert_id} เรียบร้อยแล้ว — บันทึกใหม่ได้ทันที", "info")
    except (PermissionError, ValueError) as e:
        flash(str(e), "error")
    return redirect(url_for("alerts_list"))


@app.route("/alerts/status/<rtu_id>")
def alert_status(rtu_id):
    # public endpoint (ไม่บังคับ login) — บอกว่า RTU นี้บันทึกไว้แล้วหรือยัง + เชื่อมโยงเหตุการณ์หรือไม่
    # ใช้แสดงข้อความใน popup ของหน้า Monitoring ที่เปิดให้คนไม่ login ดูได้อยู่แล้ว
    return jsonify(**alert_service.get_alert_status_for_rtu(rtu_id))


@app.route("/incidents/tree")
@login_required
def incident_tree():
    user = request.current_user
    all_visible_incidents = dashboard_service.get_dashboard_incidents(user)
    incident_ids_visible = {i["IncidentID"] for i in all_visible_incidents}  # ใช้เช็คสิทธิ์เข้าดู
    # รายละเอียด ต้องอิงรายการเต็มไม่กรอง กันลิงก์ตรงไปดูเหตุการณ์ที่ปิดแล้วพัง (เช่นจากหน้า
    # "แสดง MNF ผิดปกติ")
    incidents = dashboard_service.filter_active_incidents(all_visible_incidents)  # ตารางด้านบนซ่อนที่ปิดแล้ว

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
    
from pbc_routes import create_pbc_blueprint

app.register_blueprint(create_pbc_blueprint(
    login_required=login_required,                    # decorator ของแอปเดิม
    current_user_fn=lambda: session.get("username", ""),
    branch_scope_fn=lambda: None,                     # None = เห็นทุกสาขา
))