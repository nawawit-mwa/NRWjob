"""
org_service.py
ฟังก์ชันช่วยเกี่ยวกับโครงสร้างองค์กร: หา Section ที่ถูกต้องจาก JobType + สาขา,
ตรวจสอบว่าสองคน "อยู่หน่วยงานเดียวกัน" หรือไม่ (ใช้ตอนโอนงานระดับเดียวกัน)
"""

import sheets_client as sc


def find_section_type_for_jobtype(job_type_id: str) -> dict:
    """1 JobType ผูกกับ 1 SectionType เท่านั้น (ตามกติกาที่ตกลงกันไว้)"""
    matches = sc.find_many("SectionTypes", "JobTypeID", job_type_id)
    if not matches:
        return None
    return matches[0]


def find_section_for_job(branch_id: str, job_type_id: str) -> dict:
    """หา Section จริงของสาขานี้ ที่รับผิดชอบ JobType นี้
    คืน dict {"SectionID":..., "DivisionID":..., "BranchID":...} หรือ None ถ้าไม่พบ
    (แปลว่าสาขานี้ยังตั้งค่าโครงสร้างกอง/ส่วนไม่ครบตามแม่แบบ)"""
    section_type = find_section_type_for_jobtype(job_type_id)
    if not section_type:
        return None

    division_type_id = section_type["DivisionTypeID"]
    section_type_id = section_type["SectionTypeID"]

    # หา Division จริงของสาขานี้ ที่สร้างจาก DivisionType นี้
    divisions = sc.find_many("Divisions", "BranchID", branch_id)
    division = next((d for d in divisions if d["DivisionTypeID"] == division_type_id), None)
    if not division:
        return None

    # หา Section จริงภายใต้ Division นี้ ที่สร้างจาก SectionType นี้
    sections = sc.find_many("Sections", "DivisionID", division["DivisionID"])
    section = next((s for s in sections if s["SectionTypeID"] == section_type_id), None)
    if not section:
        return None

    return {
        "SectionID": section["SectionID"],
        "DivisionID": division["DivisionID"],
        "BranchID": branch_id,
    }


def get_assignable_users_for_job(job: dict, assigner: dict) -> list:
    """คืนรายชื่อผู้ใช้ที่ assigner คนนี้มอบหมายงาน job นี้ให้ได้
    เงื่อนไข (ต้องผ่านทั้งหมด):
    1. ระดับต่ำกว่า assigner เสมอ
    2. ถ้างานนี้มีผู้ถือครองอยู่แล้ว (CurrentAssigneeUserID ไม่ว่าง) — ต้องเป็นระดับเดียวกัน
       หรือต่ำกว่าผู้ถือครองปัจจุบันเท่านั้น (กันไม่ให้ Admin เผลอ 'มอบหมายขึ้น' ไปสูงกว่าคนที่ถืออยู่)
       ถ้างานยังไม่มีใครถือครอง (สถานะรอมอบหมาย) ข้ามเงื่อนไขนี้ไป
    3. สังกัดหน่วยงานตรงกับ job ตามระดับของ role นั้นๆ"""
    from constants import (
        ROLE_LEVELS, ROLE_SECTION_CHIEF, ROLE_ENGINEER, ROLE_FIELD_TECH,
        ROLE_CONTRACTOR, ROLE_DIVISION_DIRECTOR,
    )

    assigner_level = ROLE_LEVELS.get(assigner.get("Role"), 999)

    holder_level = None
    holder_id = job.get("CurrentAssigneeUserID")
    if holder_id:
        holder = sc.find_one("Users", "UserID", holder_id)
        if holder:
            holder_level = ROLE_LEVELS.get(holder.get("Role"))

    all_users = sc.get_all_records("Users")
    candidates = []

    for u in all_users:
        if u.get("Status") != "Active":
            continue
        u_level = ROLE_LEVELS.get(u.get("Role"), 999)
        if u_level <= assigner_level:
            continue  # มอบหมายได้เฉพาะให้ผู้ที่ระดับต่ำกว่าตนเองเท่านั้น (Admin level=0 ผ่านเงื่อนไขนี้เสมออยู่แล้ว)
        if holder_level is not None and u_level < holder_level:
            continue  # ห้ามมอบหมายให้คนที่ระดับ 'สูงกว่า' ผู้ถือครองงานปัจจุบัน

        role = u.get("Role")
        if role in (ROLE_SECTION_CHIEF, ROLE_ENGINEER, ROLE_FIELD_TECH, ROLE_CONTRACTOR):
            if u.get("SectionID") != job.get("SectionID"):
                continue
        elif role == ROLE_DIVISION_DIRECTOR:
            if u.get("DivisionID") != job.get("DivisionID"):
                continue
        else:
            continue  # role อื่น (Admin/Viewer/ผู้บริหารระดับสูง) ไม่ใช่เป้าหมายมอบหมายงาน
        # หมายเหตุ: ไม่ตัด Section/Division matching ออกแม้ผู้มอบหมายจะเป็น Admin
        # เพราะยังต้องมอบหมายให้ตรงหน่วยงานที่รับผิดชอบ JobType นั้นจริง ไม่งั้นจะมอบหมายผิดหน่วยงานได้

        candidates.append(u)

    return candidates


def get_lateral_transfer_candidates_for_job(job: dict, viewer: dict) -> list:
    """คืนรายชื่อ 'เพื่อนร่วมระดับในกองเดียวกัน' ที่โอนงาน job นี้ให้ได้
    (Role เดียวกัน + สังกัดกองเดียวกัน ตามกฎ Lateral Transfer ฉบับล่าสุด — ไม่รวมตัวเอง)

    คำนวณจาก 'ผู้ถือครองงานปัจจุบัน' เสมอ ไม่ใช่จากตัว viewer ตรงๆ:
    - กรณีปกติ (ไม่ใช่ Admin): viewer คือผู้ถือครองงานอยู่แล้ว (job_service บังคับไว้) จึงเหมือนเดิม
    - กรณี Admin: Admin ไม่มี Role/Division เป็นของตัวเอง ต้องอิงจากผู้ถือครองงานจริงแทน
      (แก้ปัญหาที่ Admin เห็นปุ่มโอนงานแต่ dropdown ว่างเปล่าตลอด)
    """
    from constants import ROLE_ADMIN, BELOW_BRANCH_LEVEL_ROLES

    if viewer.get("Role") == ROLE_ADMIN:
        holder_id = job.get("CurrentAssigneeUserID")
        if not holder_id:
            return []
        reference = sc.find_one("Users", "UserID", holder_id)
        if not reference:
            return []
    else:
        reference = viewer

    if reference.get("Role") not in BELOW_BRANCH_LEVEL_ROLES:
        return []

    all_users = sc.get_all_records("Users")
    candidates = []
    for u in all_users:
        if u.get("UserID") == reference.get("UserID"):
            continue
        if u.get("Status") != "Active":
            continue
        if same_unit(reference, u):
            candidates.append(u)
    return candidates


def same_unit(user_a: dict, user_b: dict) -> bool:
    """เช็คว่าสองคนอยู่ 'หน่วยงานเดียวกัน' หรือไม่ สำหรับกฎ Lateral Transfer
    (ฉบับล่าสุด: ใช้ระดับ 'กอง' (DivisionID) เป็นขอบเขตเดียวกันหมดทุก Role
    ไม่จำกัดแค่ระดับ 'ส่วน' (SectionID) เหมือนเดิม — เพื่อให้มีเพื่อนร่วมงานให้โอนได้จริง
    แม้ส่วนงานนั้นจะมีคนอยู่แค่คนเดียว)"""
    if user_a.get("Role") != user_b.get("Role"):
        return False

    return (
        user_a.get("DivisionID")
        and user_a.get("DivisionID") == user_b.get("DivisionID")
    )
