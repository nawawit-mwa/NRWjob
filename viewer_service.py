"""
viewer_service.py
กรองรายการงานสำหรับผู้ใช้ Role = Viewer ตาม ViewerScopes ที่ Admin ตั้งค่าไว้
(Viewer ดูได้หลายสาขา + ระดับความละเอียดอิงตาม Role อื่นที่ Admin เลือกให้ ต่อ 1 สาขา)
"""

import sheets_client as sc


def get_viewer_scopes(user_id: str) -> list:
    return sc.find_many("ViewerScopes", "UserID", user_id)


def get_visible_jobs(user: dict) -> list:
    """คืนรายการ Job ที่ Viewer คนนี้มองเห็นได้ (รวมทุกสาขาที่ได้รับสิทธิ์)
    หมายเหตุ: เวอร์ชัน prototype นี้กรองตามสาขาเป็นหลัก ส่วนการจำกัดความละเอียด
    ตาม ViewAsRole (เช่น เห็นเท่าหัวหน้าส่วน = เห็นเฉพาะข้อมูลระดับส่วนงาน)
    ควรต่อยอดตอนทำ frontend จริง โดยใช้ ViewAsRole ไปกรอง field ที่แสดงผลเพิ่มเติม"""
    scopes = get_viewer_scopes(user["UserID"])
    if not scopes:
        return []

    allowed_branch_ids = {s["BranchID"] for s in scopes}
    all_jobs = sc.get_all_records("Jobs")
    return [j for j in all_jobs if j.get("BranchID") in allowed_branch_ids]


def get_visible_incidents(user: dict) -> list:
    scopes = get_viewer_scopes(user["UserID"])
    if not scopes:
        return []
    allowed_branch_ids = {s["BranchID"] for s in scopes}
    all_incidents = sc.get_all_records("Incidents")
    return [i for i in all_incidents if i.get("BranchID") in allowed_branch_ids]


def add_viewer_scope(viewer_user_id: str, branch_id: str, view_as_role: str, admin_user: dict):
    import datetime
    scope_id = sc.next_id("ViewerScopes", "ScopeID", "VS-")
    sc.append_row("ViewerScopes", {
        "ScopeID": scope_id,
        "UserID": viewer_user_id,
        "BranchID": branch_id,
        "ViewAsRole": view_as_role,
        "AssignedBy": admin_user["UserID"],
        "AssignedAt": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    return scope_id
