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
