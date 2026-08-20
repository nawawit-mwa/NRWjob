"""
schema_setup.py
สร้างโครงสร้าง Sheet ทั้งหมดตามที่ออกแบบไว้ (รันครั้งเดียวตอนเริ่มต้นระบบ)
ใช้คำสั่ง: python schema_setup.py
"""

import sheets_client as sc
from constants import ROLE_LEVELS, BELOW_BRANCH_LEVEL_ROLES

# ชื่อ sheet -> รายชื่อคอลัมน์ (ตรงกับ schema ที่ออกแบบไว้ทุกจุด)
SHEET_SCHEMAS = {
    # --- Master data: โครงสร้างองค์กร (แม่แบบ ใช้ร่วมกันทุกสาขา) ---
    "BranchGroups": ["BranchGroupID", "BranchGroupName"],
    "Branches": ["BranchID", "BranchName", "BranchGroupID"],
    "DivisionTypes": ["DivisionTypeID", "DivisionTypeName"],
    "SectionTypes": ["SectionTypeID", "SectionTypeName", "DivisionTypeID", "JobTypeID"],
    "Divisions": ["DivisionID", "DivisionTypeID", "BranchID"],
    "Sections": ["SectionID", "SectionTypeID", "DivisionID"],

    # --- Master data: งาน/พื้นที่ ---
    "JobTypes": ["JobTypeID", "JobTypeName"],
    "IncidentTypes": ["IncidentTypeID", "IncidentTypeName"],
    "Zones": ["ZoneID", "ZoneName", "BranchID"],
    "RTUs": ["RTUID", "RTUName", "ZoneID", "BranchID"],

    # --- Role level อ้างอิง (ใช้แทนที่ AssignmentRules เดิม ซึ่งเลิกใช้บังคับลำดับแล้ว) ---
    "Roles": ["RoleName", "RoleLevel", "BelowBranchLevel"],

    # --- ผู้ใช้งาน ---
    "Users": [
        "UserID", "Name", "Username", "PasswordHash", "Role",
        "BranchGroupID", "BranchID", "DivisionID", "SectionID",
        "ContractorCompany", "Phone", "Status",
    ],

    # --- แจ้งเตือน MNF ที่บันทึกไว้ (จาก NRW Monitoring) ---
    "SavedAlerts": [
        "AlertID", "RTUID", "RTUName", "ZoneID", "BranchID",
        "CaseClassification", "MNFValue", "CUSUM", "TrendResult",
        "ChartImageURL", "Note", "SavedBy", "SavedAt",
        "LinkedIncidentID", "LinkedIncidentAt",
    ],

    # --- เหตุการณ์ ---
    "Incidents": [
        "IncidentID", "Source", "RTUID", "IncidentType", "ZoneID", "BranchID",
        "Description", "Severity", "Status", "ReportedBy", "ReportedAt",
        "ConversionStatus", "ConvertedBy", "ConvertedByRoleLevel", "ConvertedAt",
        "DueDate", "DueDateUpdatedAt", "ClosedBy", "ClosedAt",
    ],

    # --- งาน (แกนหลัก) ---
    "Jobs": [
        "JobID", "IncidentID", "SiblingJobGroup", "JobType", "Description",
        "ZoneID", "BranchID", "DivisionID", "SectionID", "Priority", "Status",
        "CurrentAssigneeUserID", "CurrentAssigneeRole", "AssignmentType", "TransferScope",
        "DueDate", "DueDateSetBy", "DueDateSetAt", "IsSlowestInGroup",
        "CreatedBy", "CreatedAt", "ClosedBy", "ClosedAt",
        "CancelledBy", "CancelledAt", "CancelReason", "Remarks",
    ],

    # --- ประวัติ/บันทึกการเปลี่ยนสถานะ ---
    "JobLogs": [
        "LogID", "JobID", "ActionType", "FromUserID", "FromRole",
        "ToUserID", "ToRole", "Timestamp", "Notes",
    ],

    # --- Viewer scope ---
    "ViewerScopes": ["ScopeID", "UserID", "BranchID", "ViewAsRole", "AssignedBy", "AssignedAt"],
}


def create_all_sheets():
    for sheet_name, headers in SHEET_SCHEMAS.items():
        print(f"กำลังสร้าง/ตั้งค่า sheet: {sheet_name}")
        sc.get_worksheet(sheet_name)   # สร้าง tab ถ้ายังไม่มี
        sc.set_headers(sheet_name, headers)


def seed_roles():
    """ใส่ข้อมูล Role/RoleLevel เริ่มต้นลง sheet Roles"""
    print("กำลังใส่ข้อมูล Roles เริ่มต้น")
    for role_name, level in ROLE_LEVELS.items():
        sc.append_row("Roles", {
            "RoleName": role_name,
            "RoleLevel": level,
            "BelowBranchLevel": role_name in BELOW_BRANCH_LEVEL_ROLES,
        })


if __name__ == "__main__":
    create_all_sheets()
    seed_roles()
    print("\nสร้างโครงสร้าง Sheet ทั้งหมดเรียบร้อยแล้ว")
    print("ขั้นต่อไป: ใส่ข้อมูล Master data (BranchGroups, Branches, DivisionTypes, "
          "SectionTypes, Divisions, Sections, JobTypes, Zones) และสร้าง Users ผ่าน "
          "auth_service.create_user() ก่อนเริ่มใช้งานจริง")
