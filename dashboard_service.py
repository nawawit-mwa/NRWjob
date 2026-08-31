"""
dashboard_service.py
รวม logic การกรองว่า "ผู้ใช้แต่ละ Role เห็นงานอะไรบ้าง" สำหรับหน้า Dashboard
(คนละส่วนกับ viewer_service.py ที่ใช้เฉพาะ Role=Viewer โดยเฉพาะ)
"""

import sheets_client as sc
import viewer_service
from constants import (
    ROLE_ADMIN, ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR,
    ROLE_BRANCH_MANAGER, ROLE_DIVISION_DIRECTOR, ROLE_SECTION_CHIEF,
    ROLE_ENGINEER, ROLE_FIELD_TECH, ROLE_CONTRACTOR, ROLE_VIEWER,
    INCIDENT_STATUS_CLOSED,
)


def get_dashboard_jobs(user: dict) -> list:
    """คืนรายการ Job ที่ user คนนี้ควรเห็นในหน้า Dashboard ตามขอบเขตของ Role ตนเอง"""
    role = user.get("Role")
    all_jobs = sc.get_all_records("Jobs")

    if role == ROLE_ADMIN:
        return all_jobs

    if role in (ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR):
        branch_group_id = user.get("BranchGroupID")
        branches = sc.find_many("Branches", "BranchGroupID", branch_group_id)
        branch_ids = {b["BranchID"] for b in branches}
        return [j for j in all_jobs if j.get("BranchID") in branch_ids]

    if role == ROLE_BRANCH_MANAGER:
        return [j for j in all_jobs if j.get("BranchID") == user.get("BranchID")]

    if role == ROLE_DIVISION_DIRECTOR:
        return [j for j in all_jobs if j.get("DivisionID") == user.get("DivisionID")]

    if role == ROLE_SECTION_CHIEF:
        return [j for j in all_jobs if j.get("SectionID") == user.get("SectionID")]

    if role in (ROLE_ENGINEER, ROLE_FIELD_TECH, ROLE_CONTRACTOR):
        # เห็นเฉพาะงานที่ตนเองถือครองอยู่ ณ ขณะนี้ (CurrentAssigneeUserID = ตนเอง)
        return [j for j in all_jobs if j.get("CurrentAssigneeUserID") == user.get("UserID")]

    if role == ROLE_VIEWER:
        return viewer_service.get_visible_jobs(user)

    return []


def get_dashboard_incidents(user: dict) -> list:
    role = user.get("Role")
    all_incidents = sc.get_all_records("Incidents")

    if role == ROLE_ADMIN:
        return all_incidents

    if role in (ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR):
        branch_group_id = user.get("BranchGroupID")
        branches = sc.find_many("Branches", "BranchGroupID", branch_group_id)
        branch_ids = {b["BranchID"] for b in branches}
        return [i for i in all_incidents if i.get("BranchID") in branch_ids]

    if role == ROLE_VIEWER:
        return viewer_service.get_visible_incidents(user)

    # Role อื่น (สาขา/กอง/ส่วน/ผู้ปฏิบัติงาน) ดูเหตุการณ์เฉพาะของสาขาตน
    branch_id = user.get("BranchID")
    if branch_id:
        return [i for i in all_incidents if i.get("BranchID") == branch_id]
    return []


def filter_active_incidents(incidents: list) -> list:
    """ตัดเหตุการณ์ที่ปิดแล้ว (Status == ปิดแล้ว) ออกจากรายการที่จะแสดงในตาราง
    ใช้แค่ตอน 'แสดงรายการ' เท่านั้น — ห้ามใช้แทน get_dashboard_incidents() ตรงๆ ตอนเช็คสิทธิ์เข้าดู
    รายละเอียดเหตุการณ์ใดเหตุการณ์หนึ่ง (เช่น incident_ids_visible ใน incident_tree route) เพราะจะทำให้
    ลิงก์ตรงไปดูเหตุการณ์ที่ปิดไปแล้ว (เช่นจากหน้า 'แสดง MNF ผิดปกติ') ขึ้น 'ไม่พบเหตุการณ์' ผิดพลาด"""
    return [i for i in incidents if i.get("Status") != INCIDENT_STATUS_CLOSED]


def get_my_action_jobs(user: dict) -> dict:
    """รวมงานที่ user คนนี้ต้อง 'ลงมือทำอะไรบางอย่าง' ต่อ แบ่งเป็น 3 กลุ่ม:
    - assigned_to_me: งานที่มอบหมายมาถึงตัวเอง (รอรับ/ปฏิเสธ/กำลังดำเนินการ)
    - pending_verify: งานรอตรวจสอบ ในส่วนงานของตน (เฉพาะวิศวกร)
    - pending_close: งานตรวจผ่านแล้ว รอกดปิด ในขอบเขตของตน (เฉพาะหัวหน้าส่วนขึ้นไป)
    """
    from constants import ROLE_ENGINEER, ROLE_LEVELS, ROLE_SECTION_CHIEF, STATUS_COMPLETED_PENDING_VERIFY

    all_jobs = sc.get_all_records("Jobs")
    user_id = user.get("UserID")
    role = user.get("Role")

    assigned_to_me = [j for j in all_jobs if j.get("CurrentAssigneeUserID") == user_id]

    pending_verify = []
    if role in (ROLE_ENGINEER, ROLE_ADMIN):
        if role == ROLE_ADMIN:
            pending_verify = [j for j in all_jobs if j.get("Status") == STATUS_COMPLETED_PENDING_VERIFY]
        else:
            pending_verify = [
                j for j in all_jobs
                if j.get("Status") == STATUS_COMPLETED_PENDING_VERIFY
                and j.get("SectionID") == user.get("SectionID")
            ]

    pending_close = []
    from auth_service import role_level
    if role == ROLE_ADMIN:
        pending_close = [j for j in all_jobs if j.get("Status") == STATUS_COMPLETED_PENDING_VERIFY]
    elif role_level(role) <= ROLE_LEVELS[ROLE_SECTION_CHIEF]:
        pending_close = [
            j for j in all_jobs
            if j.get("Status") == STATUS_COMPLETED_PENDING_VERIFY
            and j.get("SectionID") == user.get("SectionID")
        ]

    return {
        "assigned_to_me": assigned_to_me,
        "pending_verify": pending_verify,
        "pending_close": pending_close,
    }


def get_assignable_jobs(user: dict) -> list:
    """คืนงานในขอบเขตของ user คนนี้ ที่อยู่ในสถานะพร้อมมอบหมาย/มอบหมายต่อได้
    (รอมอบหมาย / รับงานแล้ว-รอส่งต่อ / ปฏิเสธ-รอมอบใหม่ / ตีกลับ-รอมอบใหม่)"""
    from constants import (
        STATUS_PENDING_ASSIGNMENT, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_REOPENED,
        ROLE_BRANCH_MANAGER, ROLE_DIVISION_DIRECTOR, ROLE_SECTION_CHIEF, ROLE_ENGINEER,
    )

    assignable_statuses = {
        STATUS_PENDING_ASSIGNMENT, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_REOPENED,
    }
    all_jobs = sc.get_all_records("Jobs")
    role = user.get("Role")

    if role == ROLE_ADMIN:
        scoped = all_jobs
    elif role == ROLE_BRANCH_MANAGER:
        scoped = [j for j in all_jobs if j.get("BranchID") == user.get("BranchID")]
    elif role == ROLE_DIVISION_DIRECTOR:
        scoped = [j for j in all_jobs if j.get("DivisionID") == user.get("DivisionID")]
    elif role in (ROLE_SECTION_CHIEF, ROLE_ENGINEER):
        scoped = [j for j in all_jobs if j.get("SectionID") == user.get("SectionID")]
    else:
        scoped = []

    return [j for j in scoped if j.get("Status") in assignable_statuses]


def get_job_permissions(job: dict, user: dict) -> dict:
    """เช็คว่า user คนนี้ทำอะไรกับ job นี้ได้บ้าง ณ สถานะปัจจุบัน (ใช้ตัดสินใจว่า
    Node ในผังเหตุการณ์กดได้ไหม + ปุ่มไหนควรโชว์ใน popup อัปเดต)"""
    from auth_service import role_level
    from constants import (
        ROLE_ADMIN, ROLE_ENGINEER, ROLE_FIELD_TECH, ROLE_CONTRACTOR,
        ROLE_LEVELS, ROLE_SECTION_CHIEF, BELOW_BRANCH_LEVEL_ROLES,
        STATUS_PENDING_ACCEPTANCE, STATUS_ACCEPTED, STATUS_IN_PROGRESS,
        STATUS_COMPLETED_PENDING_VERIFY,
    )

    role = user.get("Role")
    is_admin = role == ROLE_ADMIN
    is_owner = job.get("CurrentAssigneeUserID") == user.get("UserID")
    same_section = job.get("SectionID") == user.get("SectionID")

    can_accept = job.get("Status") == STATUS_PENDING_ACCEPTANCE and (is_admin or is_owner)
    can_reject = can_accept
    can_submit = job.get("Status") == STATUS_IN_PROGRESS and (
        is_admin or (is_owner and role in (ROLE_FIELD_TECH, ROLE_CONTRACTOR))
    )
    can_verify = job.get("Status") == STATUS_COMPLETED_PENDING_VERIFY and (
        is_admin or (role == ROLE_ENGINEER and same_section)
    )
    can_close = job.get("Status") == STATUS_COMPLETED_PENDING_VERIFY and (
        is_admin or (role_level(role) <= ROLE_LEVELS[ROLE_SECTION_CHIEF] and same_section)
    )
    can_transfer = job.get("Status") in (STATUS_ACCEPTED, STATUS_IN_PROGRESS) and (
        is_admin or (is_owner and role in BELOW_BRANCH_LEVEL_ROLES)
    )

    perms = {
        "can_accept": can_accept,
        "can_reject": can_reject,
        "can_submit": can_submit,
        "can_verify": can_verify,
        "can_close": can_close,
        "can_transfer": can_transfer,
    }
    perms["any"] = any(perms.values())
    return perms


def get_branch_group_options(user: dict) -> list:
    """คืนรายชื่อกลุ่มสาขา (ภาค) ที่ user คนนี้เลือกกรองได้ในหน้า Dashboard
    - Admin: เห็นทุกกลุ่มสาขาในระบบ
    - role อื่น: เห็นเฉพาะกลุ่มสาขาที่ตนเองสังกัด (ผ่าน BranchGroupID ตรงๆ หรือผ่าน BranchID -> Branches -> BranchGroupID)"""
    from constants import ROLE_ADMIN

    all_groups = sc.get_all_records("BranchGroups")
    if user.get("Role") == ROLE_ADMIN:
        return all_groups

    branch_group_id = user.get("BranchGroupID")
    if branch_group_id:
        return [g for g in all_groups if g.get("BranchGroupID") == branch_group_id]

    branch_id = user.get("BranchID")
    if branch_id:
        branch = sc.find_one("Branches", "BranchID", branch_id)
        if branch:
            return [g for g in all_groups if g.get("BranchGroupID") == branch.get("BranchGroupID")]

    return []


def get_branch_options(user: dict, branch_group_filter: str = "") -> list:
    """คืนรายชื่อสาขา ที่ user คนนี้เลือกกรองได้ในหน้า Dashboard
    จำกัดตามขอบเขตของ user เสมอ — ถ้ามี branch_group_filter จะแคบลงเหลือเฉพาะสาขาในภาคนั้น"""
    from constants import ROLE_ADMIN

    all_branches = sc.get_all_records("Branches")

    if branch_group_filter:
        return [b for b in all_branches if b.get("BranchGroupID") == branch_group_filter]

    if user.get("Role") == ROLE_ADMIN:
        return all_branches

    group_ids = {g["BranchGroupID"] for g in get_branch_group_options(user)}
    return [b for b in all_branches if b.get("BranchGroupID") in group_ids]


def get_zone_options(user: dict, branch_group_filter: str = "", branch_filter: str = "") -> list:
    """คืนรายชื่อโซน (DMA) ที่ user คนนี้เลือกกรองได้ในหน้า Dashboard
    จำกัดตามขอบเขตของ user เสมอ — ลำดับความแคบ: branch_filter > branch_group_filter > ขอบเขตปกติ"""
    from constants import ROLE_ADMIN

    all_zones = sc.get_all_records("Zones")
    all_branches = sc.get_all_records("Branches")

    if branch_filter:
        return [z for z in all_zones if z.get("BranchID") == branch_filter]

    if branch_group_filter:
        scope_branch_ids = {
            b["BranchID"] for b in all_branches if b.get("BranchGroupID") == branch_group_filter
        }
    elif user.get("Role") == ROLE_ADMIN:
        return all_zones  # Admin ไม่ระบุภาค/สาขา -> เห็นทุกโซน
    else:
        group_ids = {g["BranchGroupID"] for g in get_branch_group_options(user)}
        scope_branch_ids = {b["BranchID"] for b in all_branches if b.get("BranchGroupID") in group_ids}

    return [z for z in all_zones if z.get("BranchID") in scope_branch_ids]


def filter_by_branch_group_and_zone(records: list, branch_group_filter: str,
                                     branch_filter: str, zone_filter: str) -> list:
    """กรอง list ของ Job หรือ Incident (ต้องมีฟิลด์ BranchID/ZoneID) ตามภาค/สาขา/โซนที่เลือก (AND ทั้งหมด)"""
    if branch_group_filter:
        group_branch_ids = {
            b["BranchID"] for b in sc.get_all_records("Branches")
            if b.get("BranchGroupID") == branch_group_filter
        }
        records = [r for r in records if r.get("BranchID") in group_branch_ids]

    if branch_filter:
        records = [r for r in records if r.get("BranchID") == branch_filter]

    if zone_filter:
        records = [r for r in records if r.get("ZoneID") == zone_filter]

    return records
