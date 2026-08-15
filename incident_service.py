"""
incident_service.py
สร้างเหตุการณ์ + แปลงเหตุการณ์เป็นงาน (1 Incident -> หลาย Job อิสระต่อกัน)
"""

import datetime

import sheets_client as sc
import org_service
from auth_service import role_level
from constants import (
    CONVERSION_NOT_CONVERTED, CONVERSION_PARTIAL, CONVERSION_FULL,
    STATUS_PENDING_ASSIGNMENT, ROLE_LEVELS, ROLE_SECTION_CHIEF,
    ROLE_ADMIN, INCIDENT_EDIT_ROLES, INCIDENT_STATUS_NEW, INCIDENT_STATUS_CLOSED,
)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def create_incident(incident_type: str, description: str, severity: str, reported_by: str,
                     source: str = "Manual", rtu_id: str = "",
                     zone_id: str = "", branch_id: str = "") -> str:
    """สร้างเหตุการณ์ใหม่

    สองแบบการใช้งาน:
    1. มาจาก RTU (เช่น MNF alert): ส่ง rtu_id มา -> ระบบ resolve ZoneID/BranchID
       ให้อัตโนมัติจากตาราง RTUs โดยไม่ต้องระบุ zone_id/branch_id เอง
       (ถ้าส่ง zone_id/branch_id มาด้วยพร้อมกัน จะถูก "เขียนทับ" ด้วยค่าจริงจาก RTU เสมอ
       เพราะ RTU ถือเป็นแหล่งข้อมูลที่ถูกต้องที่สุด - single source of truth)
    2. แจ้งเหตุการณ์เอง (Manual): ไม่มี rtu_id -> ต้องระบุ zone_id และ branch_id เอง
    """
    if rtu_id:
        rtu = sc.find_one("RTUs", "RTUID", rtu_id)
        if not rtu:
            raise ValueError(f"ไม่พบ RTU รหัส {rtu_id} ในระบบ")
        zone_id = rtu["ZoneID"]
        branch_id = rtu["BranchID"]
    elif not branch_id:
        raise ValueError("ต้องระบุ rtu_id หรือ branch_id อย่างน้อยหนึ่งอย่าง (zone_id ไม่บังคับ)")

    incident_id = sc.next_id("Incidents", "IncidentID", "INC-")
    sc.append_row("Incidents", {
        "IncidentID": incident_id,
        "Source": source,
        "RTUID": rtu_id,
        "IncidentType": incident_type,
        "ZoneID": zone_id,
        "BranchID": branch_id,
        "Description": description,
        "Severity": severity,
        "Status": INCIDENT_STATUS_NEW,
        "ReportedBy": reported_by,
        "ReportedAt": _now(),
        "ConversionStatus": CONVERSION_NOT_CONVERTED,
        "ConvertedBy": "",
        "ConvertedByRoleLevel": "",
        "ConvertedAt": "",
        "DueDate": "",
        "DueDateUpdatedAt": "",
        "ClosedBy": "",
        "ClosedAt": "",
    })
    return incident_id


def convert_incident_to_jobs(incident_id: str, job_type_ids: list, user: dict) -> list:
    """แปลงเหตุการณ์เป็นงาน - เลือกได้หลาย JobType พร้อมกัน สร้าง 1 Job ต่อ 1 JobType
    สิทธิ์: หัวหน้า (ส่วน) ขึ้นไป (RoleLevel <= ROLE_SECTION_CHIEF's level)
    คืน list ของ JobID ที่สร้าง"""
    if role_level(user.get("Role")) > ROLE_LEVELS[ROLE_SECTION_CHIEF]:
        raise PermissionError("ไม่มีสิทธิ์แปลงเหตุการณ์เป็นงาน (ต้องระดับหัวหน้าส่วนขึ้นไป)")

    incident = sc.find_one("Incidents", "IncidentID", incident_id)
    if not incident:
        raise ValueError("ไม่พบเหตุการณ์นี้")

    branch_id = incident["BranchID"]
    created_job_ids = []

    for job_type_id in job_type_ids:
        section = org_service.find_section_for_job(branch_id, job_type_id)
        if section is None:
            raise ValueError(
                f"สาขา {branch_id} ยังไม่มีส่วนงานรองรับ JobType={job_type_id} "
                f"(ตรวจสอบ Divisions/Sections ให้ครบตามแม่แบบ)"
            )

        job_id = sc.next_id("Jobs", "JobID", "JOB-")
        sc.append_row("Jobs", {
            "JobID": job_id,
            "IncidentID": incident_id,
            "SiblingJobGroup": incident_id,
            "JobType": job_type_id,
            "Description": incident.get("Description", ""),
            "ZoneID": incident.get("ZoneID", ""),
            "BranchID": branch_id,
            "DivisionID": section["DivisionID"],
            "SectionID": section["SectionID"],
            "Priority": incident.get("Severity", ""),
            "Status": STATUS_PENDING_ASSIGNMENT,
            "CurrentAssigneeUserID": "",
            "CurrentAssigneeRole": "",
            "AssignmentType": "",
            "TransferScope": "",
            "DueDate": "",
            "DueDateSetBy": "",
            "DueDateSetAt": "",
            "IsSlowestInGroup": False,
            "CreatedBy": user["UserID"],
            "CreatedAt": _now(),
            "ClosedBy": "", "ClosedAt": "",
            "CancelledBy": "", "CancelledAt": "", "CancelReason": "",
            "Remarks": "",
        })
        created_job_ids.append(job_id)

    _update_converted_by(incident_id, user)
    sc.update_row("Incidents", "IncidentID", incident_id, {
        "ConversionStatus": CONVERSION_PARTIAL,  # อัปเดตเป็น FULL ทีหลังถ้าจะเช็คครบทุก JobType ที่ควรมี
    })
    return created_job_ids


def _update_converted_by(incident_id: str, user: dict):
    """เขียนทับ ConvertedBy เฉพาะเมื่อผู้แปลงรอบนี้มีตำแหน่งสูงกว่าคนเดิม (เลข RoleLevel น้อยกว่า)"""
    incident = sc.find_one("Incidents", "IncidentID", incident_id)
    new_level = role_level(user.get("Role"))
    current_level = incident.get("ConvertedByRoleLevel")

    should_overwrite = (current_level in ("", None)) or (new_level < int(current_level))
    if should_overwrite:
        sc.update_row("Incidents", "IncidentID", incident_id, {
            "ConvertedBy": user["UserID"],
            "ConvertedByRoleLevel": new_level,
            "ConvertedAt": _now(),
        })


def get_incident_permissions(incident: dict, user: dict) -> dict:
    """เช็คว่า user คนนี้แก้ไข/ปิด/จ่ายงาน Incident นี้ได้ไหม ณ ตอนนี้
    - แก้ไข/ปิด: ผู้จัดการ (สาขา) + ผู้อำนวยการ (กอง) + Admin เท่านั้น
    - จ่ายงาน (แปลงเป็นงาน): หัวหน้าส่วนขึ้นไป + Admin (สิทธิ์เดียวกับ convert_incident_to_jobs)
    Guard: ทำอะไรก็ตามได้ก็ต่อเมื่อ Status ยังไม่ใช่ 'ปิดแล้ว'"""
    role = user.get("Role")
    is_admin = role == ROLE_ADMIN
    is_authorized = is_admin or role in INCIDENT_EDIT_ROLES
    not_closed = incident.get("Status") != INCIDENT_STATUS_CLOSED

    can_edit = is_authorized and not_closed
    can_close = is_authorized and not_closed
    can_dispatch = (
        not_closed
        and incident.get("ConversionStatus") != CONVERSION_FULL
        and (is_admin or role_level(role) <= ROLE_LEVELS[ROLE_SECTION_CHIEF])
    )
    return {
        "can_edit": can_edit,
        "can_close": can_close,
        "can_dispatch": can_dispatch,
        "any": can_edit or can_close or can_dispatch,
    }


def update_incident_details(incident_id: str, user: dict, description: str = None,
                             severity: str = None, zone_id: str = None,
                             due_date: str = None):
    """แก้ไขรายละเอียดเหตุการณ์ - ส่งเฉพาะฟิลด์ที่จะแก้มา (None = ไม่แตะฟิลด์นั้น)
    Guard DueDate: ค่าใหม่ต้องไม่น้อยกว่าค่าปัจจุบัน (เลื่อนออกไปได้อย่างเดียว ห้ามร่นเข้ามา)"""
    incident = sc.find_one("Incidents", "IncidentID", incident_id)
    if not incident:
        raise ValueError("ไม่พบเหตุการณ์นี้")

    perms = get_incident_permissions(incident, user)
    if not perms["can_edit"]:
        raise PermissionError("ไม่มีสิทธิ์แก้ไขเหตุการณ์นี้ (ต้องเป็นผู้จัดการ/ผู้อำนวยการ หรือเหตุการณ์ถูกปิดไปแล้ว)")

    updates = {}
    if description is not None:
        updates["Description"] = description
    if severity is not None:
        updates["Severity"] = severity
    if zone_id is not None:
        updates["ZoneID"] = zone_id
    if due_date is not None and due_date != "":
        current_due_date = incident.get("DueDate") or ""
        if current_due_date and due_date < current_due_date:
            raise ValueError(
                f"กำหนดเสร็จใหม่ ({due_date}) ต้องไม่น้อยกว่าค่าปัจจุบัน ({current_due_date})"
            )
        updates["DueDate"] = due_date
        updates["DueDateUpdatedAt"] = _now()

    if updates:
        sc.update_row("Incidents", "IncidentID", incident_id, updates)


def close_incident(incident_id: str, user: dict):
    """ปิดเหตุการณ์ - ปิดได้ทันทีไม่ต้องรองานลูกปิดครบก่อน"""
    incident = sc.find_one("Incidents", "IncidentID", incident_id)
    if not incident:
        raise ValueError("ไม่พบเหตุการณ์นี้")

    perms = get_incident_permissions(incident, user)
    if not perms["can_close"]:
        raise PermissionError("ไม่มีสิทธิ์ปิดเหตุการณ์นี้ (ต้องเป็นผู้จัดการ/ผู้อำนวยการ หรือเหตุการณ์ถูกปิดไปแล้ว)")

    sc.update_row("Incidents", "IncidentID", incident_id, {
        "Status": INCIDENT_STATUS_CLOSED,
        "ClosedBy": user["UserID"],
        "ClosedAt": _now(),
    })
