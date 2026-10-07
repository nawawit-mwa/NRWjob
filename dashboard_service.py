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


def filter_jobs_with_open_incidents(jobs: list) -> list:
    """ตัด Job ที่เหตุการณ์แม่ (SiblingJobGroup -> IncidentID) ปิดไปแล้วออกจากหน้า 'ติดตามงาน'
    ตัดออกทั้งหมดไม่ว่า Job เองจะเสร็จ (ปิดงานแล้ว) หรือยังไม่เสร็จก็ตาม (ถือว่าเหตุการณ์แม่ปิดแล้ว = จบ)
    ใช้เฉพาะหน้า Dashboard เท่านั้น — ห้ามใช้กับ /jobs/manage หรือ /my-jobs เพราะคนที่รับผิดชอบงาน
    ยังต้องเห็น/ปิดงานของตัวเองได้อยู่ ไม่ว่าเหตุการณ์แม่จะถูกปิดไปก่อนแล้วหรือไม่ก็ตาม"""
    closed_incident_ids = {
        i["IncidentID"] for i in sc.get_all_records("Incidents") if i.get("Status") == INCIDENT_STATUS_CLOSED
    }
    return [j for j in jobs if j.get("SiblingJobGroup") not in closed_incident_ids]


# ---------------------------------------------------------------------------
# หน้า "ติดตามเหตุการณ์" (หน้าหลัก Job Management): สถิติงานต่อเหตุการณ์ + งานเลยกำหนด
# ---------------------------------------------------------------------------
JOB_FINISHED_STATUSES = ("ปิดงาน", "ยกเลิกงาน")


def today_iso() -> str:
    """วันที่ปัจจุบันตามเวลาไทย (UTC+7) รูปแบบ yyyy-mm-dd — เซิร์ฟเวอร์ Render ใช้ UTC
    ถ้าใช้ date.today() ตรงๆ ช่วง 00:00-07:00 น. จะนับวันผิดไป 1 วัน"""
    import datetime
    bkk = datetime.timezone(datetime.timedelta(hours=7))
    return datetime.datetime.now(bkk).date().isoformat()


def is_job_overdue(job: dict, today: str) -> bool:
    """เลยกำหนด = มี DueDate, DueDate < วันนี้ และงานยังไม่จบ (ไม่ใช่ ปิดงาน/ยกเลิกงาน)"""
    due = str(job.get("DueDate") or "")[:10]
    return bool(due) and due < today and job.get("Status") not in JOB_FINISHED_STATUSES


def get_incident_job_stats(incidents: list, today: str) -> dict:
    """คืน {IncidentID: {total, done, waiting, overdue}} สำหรับคอลัมน์ความคืบหน้าในตารางเหตุการณ์
    นับจากงานทุกใบของเหตุการณ์นั้น (ผูกด้วย SiblingJobGroup) เหมือนที่ผังแสดง"""
    from constants import STATUS_PENDING_ASSIGNMENT

    wanted = {i["IncidentID"] for i in incidents}
    stats = {iid: {"total": 0, "done": 0, "waiting": 0, "overdue": 0} for iid in wanted}
    for j in sc.get_all_records("Jobs"):
        iid = j.get("SiblingJobGroup")
        if iid not in stats:
            continue
        st = stats[iid]
        st["total"] += 1
        if j.get("Status") in JOB_FINISHED_STATUSES:
            st["done"] += 1
        if j.get("Status") == STATUS_PENDING_ASSIGNMENT:
            st["waiting"] += 1
        if is_job_overdue(j, today):
            st["overdue"] += 1
    return stats


def get_overdue_jobs_in_scope(user: dict, today: str) -> list:
    """งานเลยกำหนดทั้งหมดในขอบเขตของ user (ข้ามทุกเหตุการณ์) เรียงจากเลยนานสุดก่อน
    ไม่รวมงานที่เหตุการณ์แม่ปิดไปแล้ว"""
    jobs = filter_jobs_with_open_incidents(get_dashboard_jobs(user))
    overdue = [j for j in jobs if is_job_overdue(j, today)]
    return sorted(overdue, key=lambda j: str(j.get("DueDate") or ""))


def get_assignment_chains(job_ids: list) -> dict:
    """สายการมอบหมายของแต่ละงาน (สร้างจาก JobLogs) สำหรับวาด node ต่อลงมาใต้การ์ดงานในผัง
    คืน {JobID: [hop, ...]} เรียงตามเวลา — hop = {kind, to, by, at, at_display, due, state, note, is_current}
    state: รอรับ / รับงานแล้ว / ปฏิเสธ / ส่งต่อแล้ว / เปลี่ยนผู้รับ / ส่งงานเสร็จ"""
    import re
    from constants import (
        ACTION_MANUAL_ASSIGN, ACTION_LATERAL_TRANSFER, ACTION_ACCEPT, ACTION_REJECT,
        ACTION_SUBMIT_COMPLETION, ACTION_SET_DUE_DATE,
    )

    wanted = set(job_ids)
    if not wanted:
        return {}
    logs_by_job = {}
    for log in sc.get_all_records("JobLogs"):
        if log.get("JobID") in wanted:
            logs_by_job.setdefault(log["JobID"], []).append(log)

    def _at_display(ts):
        ts = str(ts or "").replace("T", " ")
        if len(ts) >= 16 and ts[4] == "-":
            return f"{ts[8:10]}/{ts[5:7]}/{ts[0:4]} {ts[11:16]}"
        return ts

    chains = {}
    for job_id, logs in logs_by_job.items():
        logs.sort(key=lambda l: (str(l.get("Timestamp") or ""), str(l.get("LogID") or "")))
        hops = []
        for log in logs:
            action = log.get("ActionType")
            if action in (ACTION_MANUAL_ASSIGN, ACTION_LATERAL_TRANSFER):
                if hops:
                    prev = hops[-1]
                    if prev["state"] == "รับงานแล้ว":
                        prev["state"] = "ส่งต่อแล้ว"
                    elif prev["state"] == "รอรับ":
                        prev["state"] = "เปลี่ยนผู้รับ"
                hops.append({
                    "kind": "มอบหมาย" if action == ACTION_MANUAL_ASSIGN else "โอนงาน",
                    "to": log.get("ToUserID") or "",
                    "by": log.get("FromUserID") or "",
                    "at": log.get("Timestamp") or "",
                    "at_display": _at_display(log.get("Timestamp")),
                    "due": "",
                    "state": "รอรับ",
                    "note": "",
                    "is_current": False,
                })
            elif not hops:
                continue
            elif action == ACTION_ACCEPT:
                hops[-1]["state"] = "รับงานแล้ว"
            elif action == ACTION_REJECT:
                hops[-1]["state"] = "ปฏิเสธ"
                hops[-1]["note"] = str(log.get("Notes") or "")
            elif action == ACTION_SUBMIT_COMPLETION:
                hops[-1]["state"] = "ส่งงานเสร็จ"
            elif action == ACTION_SET_DUE_DATE:
                m = re.search(r"DueDate=(\d{4}-\d{2}-\d{2})", str(log.get("Notes") or ""))
                if m:
                    hops[-1]["due"] = m.group(1)
        # มอบหมายต่อโดยไม่ได้กำหนดวันใหม่ (เช่น วิศวกร → ช่าง) = ใช้กำหนดเสร็จเดิมของรอบก่อนหน้า
        for prev, hop in zip(hops, hops[1:]):
            if not hop["due"] and prev["due"]:
                hop["due"] = prev["due"]
        chains[job_id] = hops
    return chains


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
    )

    assignable_statuses = {
        STATUS_PENDING_ASSIGNMENT, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_REOPENED,
    }
    all_jobs = sc.get_all_records("Jobs")
    return [
        j for j in all_jobs
        if j.get("Status") in assignable_statuses and _job_in_assign_scope(j, user)
    ]


def _job_in_assign_scope(job: dict, user: dict) -> bool:
    """งานนี้อยู่ในขอบเขตที่ user คนนี้มีสิทธิ์มอบหมายหรือไม่ (ใช้ร่วมกันระหว่างหน้า
    มอบหมายงาน และปุ่มมอบหมายใน popup ของผังเหตุการณ์/dashboard ให้กติกาตรงกันเสมอ)"""
    from constants import (
        ROLE_ADMIN, ROLE_BRANCH_MANAGER, ROLE_DIVISION_DIRECTOR,
        ROLE_SECTION_CHIEF, ROLE_ENGINEER,
    )

    role = user.get("Role")
    if role == ROLE_ADMIN:
        return True
    if role == ROLE_BRANCH_MANAGER:
        return bool(user.get("BranchID")) and job.get("BranchID") == user.get("BranchID")
    if role == ROLE_DIVISION_DIRECTOR:
        return bool(user.get("DivisionID")) and job.get("DivisionID") == user.get("DivisionID")
    if role in (ROLE_SECTION_CHIEF, ROLE_ENGINEER):
        return bool(user.get("SectionID")) and job.get("SectionID") == user.get("SectionID")
    return False


def get_job_permissions(job: dict, user: dict) -> dict:
    """เช็คว่า user คนนี้ทำอะไรกับ job นี้ได้บ้าง ณ สถานะปัจจุบัน (ใช้ตัดสินใจว่า
    Node ในผังเหตุการณ์กดได้ไหม + ปุ่มไหนควรโชว์ใน popup อัปเดต)"""
    from auth_service import role_level
    from constants import (
        ROLE_ADMIN, ROLE_ENGINEER, ROLE_FIELD_TECH, ROLE_CONTRACTOR,
        ROLE_LEVELS, ROLE_SECTION_CHIEF, BELOW_BRANCH_LEVEL_ROLES,
        STATUS_PENDING_ACCEPTANCE, STATUS_ACCEPTED, STATUS_IN_PROGRESS,
        STATUS_COMPLETED_PENDING_VERIFY, STATUS_PENDING_ASSIGNMENT,
        STATUS_REJECTED, STATUS_REOPENED, ASSIGNER_ROLES,
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
    # มอบหมายลง: สถานะเดียวกับที่ job_service.assign_job ยอมรับ + อยู่ในขอบเขตของผู้มอบหมาย
    # (เดิมไม่มีสิทธิ์นี้ใน popup เลย ทำให้งาน 'รอมอบหมาย' ในผังเหตุการณ์กดไม่ได้)
    can_assign = job.get("Status") in (
        STATUS_PENDING_ASSIGNMENT, STATUS_ACCEPTED, STATUS_REJECTED, STATUS_REOPENED,
    ) and (is_admin or (role in ASSIGNER_ROLES and _job_in_assign_scope(job, user)))

    from constants import DUE_DATE_EDITOR_MAX_LEVEL
    can_set_due = can_assign and role_level(role) <= DUE_DATE_EDITOR_MAX_LEVEL

    perms = {
        "can_assign": can_assign,
        "can_set_due": can_set_due,
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
