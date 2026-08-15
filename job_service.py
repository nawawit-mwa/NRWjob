"""
job_service.py
แกนหลักของการมอบหมายงาน / โอนงาน / เปลี่ยนสถานะงาน / กำหนด DueDate
ทุกฟังก์ชันตรวจสิทธิ์ตาม RoleLevel ก่อนเสมอ (ไม่พึ่ง UI ในการกันสิทธิ์)
"""

import datetime

import sheets_client as sc
import org_service
from auth_service import role_level
from constants import (
    ROLE_LEVELS, ROLE_ADMIN, ROLE_FIELD_TECH, ROLE_CONTRACTOR, ROLE_ENGINEER,
    ROLE_SECTION_CHIEF, ROLE_DIVISION_DIRECTOR,
    ASSIGNER_ROLES, BELOW_BRANCH_LEVEL_ROLES,
    STATUS_PENDING_ASSIGNMENT, STATUS_PENDING_ACCEPTANCE, STATUS_ACCEPTED,
    STATUS_REJECTED, STATUS_IN_PROGRESS, STATUS_COMPLETED_PENDING_VERIFY,
    STATUS_REOPENED, STATUS_CLOSED, STATUS_CANCELLED,
    ACTION_MANUAL_ASSIGN, ACTION_LATERAL_TRANSFER, ACTION_ACCEPT, ACTION_REJECT,
    ACTION_START_WORK, ACTION_SUBMIT_COMPLETION, ACTION_VERIFY_PASS, ACTION_VERIFY_FAIL,
    ACTION_CLOSE, ACTION_CANCEL, ACTION_SET_DUE_DATE, ACTION_DUE_DATE_WARNING,
    DUE_DATE_EDITOR_MAX_LEVEL, CLOSE_JOB_MAX_LEVEL, CANCEL_JOB_MAX_LEVEL,
)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _log(job_id, action_type, from_user=None, to_user=None, notes=""):
    log_id = sc.next_id("JobLogs", "LogID", "LOG-")
    sc.append_row("JobLogs", {
        "LogID": log_id,
        "JobID": job_id,
        "ActionType": action_type,
        "FromUserID": from_user.get("UserID") if from_user else "",
        "FromRole": from_user.get("Role") if from_user else "",
        "ToUserID": to_user.get("UserID") if to_user else "",
        "ToRole": to_user.get("Role") if to_user else "",
        "Timestamp": _now(),
        "Notes": notes,
    })


def _get_job(job_id):
    job = sc.find_one("Jobs", "JobID", job_id)
    if not job:
        raise ValueError("ไม่พบงานนี้")
    return job


def _get_user(user_id):
    user = sc.find_one("Users", "UserID", user_id)
    if not user:
        raise ValueError("ไม่พบผู้ใช้งานนี้")
    return user


# ---------------------------------------------------------------------------
# มอบหมายงาน (Manual Assign) — ระดับบนมอบหมายให้ระดับล่างชั้นใดก็ได้ (ข้ามระดับ/ข้ามกองได้)
# ---------------------------------------------------------------------------
def assign_job(job_id: str, to_user_id: str, by_user: dict):
    job = _get_job(job_id)
    to_user = _get_user(to_user_id)

    if by_user.get("Role") not in ASSIGNER_ROLES and by_user.get("Role") != ROLE_ADMIN:
        raise PermissionError("Role นี้ไม่มีสิทธิ์มอบหมายงาน")

    if by_user.get("Role") != ROLE_ADMIN and role_level(by_user["Role"]) >= role_level(to_user["Role"]):
        raise PermissionError("มอบหมายได้เฉพาะให้ผู้ที่มีตำแหน่งต่ำกว่าตนเองเท่านั้น")

    valid_from = {
        STATUS_PENDING_ASSIGNMENT, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_REOPENED,
    }
    if job["Status"] not in valid_from:
        raise ValueError(f"ไม่สามารถมอบหมายงานจากสถานะปัจจุบัน ({job['Status']}) ได้")

    sc.update_row("Jobs", "JobID", job_id, {
        "Status": STATUS_PENDING_ACCEPTANCE,
        "CurrentAssigneeUserID": to_user["UserID"],
        "CurrentAssigneeRole": to_user["Role"],
        "AssignmentType": "มอบหมายลง",
        "TransferScope": "",
    })
    _log(job_id, ACTION_MANUAL_ASSIGN, by_user, to_user)


# ---------------------------------------------------------------------------
# โอนงานระดับเดียวกัน (Lateral Transfer) — เฉพาะหน่วยงานเดียวกัน + ต่ำกว่าระดับสาขา
# ---------------------------------------------------------------------------
def lateral_transfer(job_id: str, to_user_id: str, by_user: dict):
    job = _get_job(job_id)
    to_user = _get_user(to_user_id)

    if by_user.get("Role") != ROLE_ADMIN:
        if by_user.get("Role") not in BELOW_BRANCH_LEVEL_ROLES:
            raise PermissionError("Role นี้ไม่อยู่ในเงื่อนไขที่โอนงานระดับเดียวกันได้")

        if not org_service.same_unit(by_user, to_user):
            raise PermissionError("โอนงานได้เฉพาะบุคคลในหน่วยงานเดียวกันเท่านั้น")

        if job.get("CurrentAssigneeUserID") != by_user["UserID"]:
            raise PermissionError("โอนงานได้เฉพาะงานที่ตนเองถือครองอยู่เท่านั้น")

    sc.update_row("Jobs", "JobID", job_id, {
        "Status": STATUS_PENDING_ACCEPTANCE,
        "CurrentAssigneeUserID": to_user["UserID"],
        "CurrentAssigneeRole": to_user["Role"],
        "AssignmentType": "โอนระดับเดียวกัน",
        "TransferScope": "หน่วยงานเดียวกัน",
    })
    _log(job_id, ACTION_LATERAL_TRANSFER, by_user, to_user)


# ---------------------------------------------------------------------------
# รับ / ปฏิเสธงาน
# ---------------------------------------------------------------------------
def accept_job(job_id: str, user: dict):
    job = _get_job(job_id)
    if user.get("Role") != ROLE_ADMIN and job.get("CurrentAssigneeUserID") != user["UserID"]:
        raise PermissionError("ไม่ใช่ผู้ที่ถูกมอบหมายงานนี้")
    if job["Status"] != STATUS_PENDING_ACCEPTANCE:
        raise ValueError("งานนี้ไม่ได้อยู่ในสถานะรอรับ")

    assignee_role = job.get("CurrentAssigneeRole") if user.get("Role") == ROLE_ADMIN else user.get("Role")
    is_field_worker = assignee_role in (ROLE_FIELD_TECH, ROLE_CONTRACTOR)
    new_status = STATUS_IN_PROGRESS if is_field_worker else STATUS_ACCEPTED

    sc.update_row("Jobs", "JobID", job_id, {"Status": new_status})
    _log(job_id, ACTION_ACCEPT, user, user,
         notes="ช่างเริ่มปฏิบัติงาน" if is_field_worker else "รับงานแล้ว รอมอบหมายต่อ")


def reject_job(job_id: str, user: dict, reason: str):
    job = _get_job(job_id)
    if user.get("Role") != ROLE_ADMIN and job.get("CurrentAssigneeUserID") != user["UserID"]:
        raise PermissionError("ไม่ใช่ผู้ที่ถูกมอบหมายงานนี้")
    if job["Status"] != STATUS_PENDING_ACCEPTANCE:
        raise ValueError("งานนี้ไม่ได้อยู่ในสถานะรอรับ")

    sc.update_row("Jobs", "JobID", job_id, {
        "Status": STATUS_PENDING_ASSIGNMENT,
        "CurrentAssigneeUserID": "",
        "CurrentAssigneeRole": "",
    })
    _log(job_id, ACTION_REJECT, user, notes=f"ปฏิเสธ: {reason}")


# ---------------------------------------------------------------------------
# ดำเนินงาน (ช่างสนาม/ผู้รับจ้าง)
# ---------------------------------------------------------------------------
def submit_completion(job_id: str, user: dict, remarks: str = ""):
    job = _get_job(job_id)
    if user.get("Role") != ROLE_ADMIN and user.get("Role") not in (ROLE_FIELD_TECH, ROLE_CONTRACTOR):
        raise PermissionError("เฉพาะช่างสนาม/ผู้รับจ้างเท่านั้นที่ส่งงานเสร็จได้")
    if job["Status"] != STATUS_IN_PROGRESS:
        raise ValueError("งานนี้ไม่ได้อยู่ระหว่างดำเนินการ")

    sc.update_row("Jobs", "JobID", job_id, {
        "Status": STATUS_COMPLETED_PENDING_VERIFY,
        "Remarks": remarks,
    })
    _log(job_id, ACTION_SUBMIT_COMPLETION, user, notes=remarks)


# ---------------------------------------------------------------------------
# ตรวจสอบงาน (วิศวกร)
# ---------------------------------------------------------------------------
def verify_job(job_id: str, user: dict, passed: bool, notes: str = ""):
    job = _get_job(job_id)
    if user.get("Role") != ROLE_ADMIN and user.get("Role") != ROLE_ENGINEER:
        raise PermissionError("เฉพาะวิศวกรเท่านั้นที่ตรวจสอบผลงานได้")
    if job["Status"] != STATUS_COMPLETED_PENDING_VERIFY:
        raise ValueError("งานนี้ไม่ได้อยู่ในสถานะรอตรวจสอบ")

    if passed:
        # ผ่าน -> รอให้หัวหน้าส่วนขึ้นไปกดปิดงาน (คงสถานะ 'เสร็จ รอตรวจสอบ' + log ว่าผ่านแล้ว)
        _log(job_id, ACTION_VERIFY_PASS, user, notes=notes)
    else:
        sc.update_row("Jobs", "JobID", job_id, {"Status": STATUS_REOPENED})
        _log(job_id, ACTION_VERIFY_FAIL, user, notes=notes)
        # ส่งกลับไปดำเนินการต่อทันที
        sc.update_row("Jobs", "JobID", job_id, {"Status": STATUS_IN_PROGRESS})


# ---------------------------------------------------------------------------
# ปิดงาน / ยกเลิกงาน
# ---------------------------------------------------------------------------
def close_job(job_id: str, user: dict):
    job = _get_job(job_id)
    if role_level(user.get("Role")) > CLOSE_JOB_MAX_LEVEL:
        raise PermissionError("ต้องเป็นหัวหน้าส่วนขึ้นไปเท่านั้นที่ปิดงานได้")
    if job["Status"] != STATUS_COMPLETED_PENDING_VERIFY:
        raise ValueError("ปิดงานได้เฉพาะงานที่ตรวจสอบผ่านแล้วเท่านั้น")

    sc.update_row("Jobs", "JobID", job_id, {
        "Status": STATUS_CLOSED,
        "ClosedBy": user["UserID"],
        "ClosedAt": _now(),
    })
    _log(job_id, ACTION_CLOSE, user)


def cancel_job(job_id: str, user: dict, reason: str):
    job = _get_job(job_id)
    if role_level(user.get("Role")) > CANCEL_JOB_MAX_LEVEL:
        raise PermissionError("ต้องเป็นผู้อำนวยการ (กอง) ขึ้นไปเท่านั้นที่ยกเลิกงานได้")
    if job["Status"] in (STATUS_CLOSED, STATUS_CANCELLED):
        raise ValueError("งานนี้จบสถานะแล้ว ไม่สามารถยกเลิกได้")

    sc.update_row("Jobs", "JobID", job_id, {
        "Status": STATUS_CANCELLED,
        "CancelledBy": user["UserID"],
        "CancelledAt": _now(),
        "CancelReason": reason,
    })
    _log(job_id, ACTION_CANCEL, user, notes=reason)


# ---------------------------------------------------------------------------
# DueDate: กำหนดที่ระดับ Job แล้วคำนวณ Incident.DueDate = MAX ของ Job ในกลุ่มอัตโนมัติ
# ---------------------------------------------------------------------------
def set_due_date(job_id: str, due_date: str, user: dict):
    """due_date: string รูปแบบ ISO date เช่น '2026-09-01'"""
    if role_level(user.get("Role")) > DUE_DATE_EDITOR_MAX_LEVEL:
        raise PermissionError("ต้องเป็นหัวหน้าส่วนขึ้นไปเท่านั้นที่กำหนด DueDate ได้")

    job = _get_job(job_id)
    sc.update_row("Jobs", "JobID", job_id, {
        "DueDate": due_date,
        "DueDateSetBy": user["UserID"],
        "DueDateSetAt": _now(),
    })
    _log(job_id, ACTION_SET_DUE_DATE, user, notes=f"DueDate={due_date}")

    if job.get("IncidentID"):
        _recalculate_incident_due_date(job["IncidentID"])


def _recalculate_incident_due_date(incident_id: str):
    incident = sc.find_one("Incidents", "IncidentID", incident_id)
    if not incident:
        return

    sibling_jobs = sc.find_many("Jobs", "SiblingJobGroup", incident_id)
    dated_jobs = [j for j in sibling_jobs if j.get("DueDate")]
    if not dated_jobs:
        return

    slowest = max(dated_jobs, key=lambda j: j["DueDate"])
    previous_due_date = incident.get("DueDate") or ""

    # อัปเดตธง IsSlowestInGroup ให้ตรงกับตัวที่ช้าที่สุดตัวเดียว
    for j in sibling_jobs:
        is_slowest = j["JobID"] == slowest["JobID"]
        sc.update_row("Jobs", "JobID", j["JobID"], {"IsSlowestInGroup": is_slowest})

    sc.update_row("Incidents", "IncidentID", incident_id, {
        "DueDate": slowest["DueDate"],
        "DueDateUpdatedAt": _now(),
    })

    if previous_due_date and slowest["DueDate"] > previous_due_date:
        _notify_due_date_pushed(incident, slowest)


def _notify_due_date_pushed(incident: dict, slowest_job: dict):
    """แจ้งเตือนไปยังผู้จ่ายงานตำแหน่งสูงสุด (Incidents.ConvertedBy)
    Prototype: บันทึกลง JobLogs เป็นหลักฐาน + print ออกหน้าจอ (แทนอีเมล/LINE Notify จริง)"""
    converted_by = incident.get("ConvertedBy")
    message = (
        f"งาน {slowest_job['JobID']} ประเภท {slowest_job['JobType']} "
        f"ทำให้กำหนดเสร็จของเหตุการณ์ {incident['IncidentID']} "
        f"เลื่อนเป็น {slowest_job['DueDate']}"
    )
    _log(slowest_job["JobID"], ACTION_DUE_DATE_WARNING, notes=f"แจ้งถึง {converted_by}: {message}")
    print(f"[แจ้งเตือน -> {converted_by}] {message}")
