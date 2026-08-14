"""
ค่าคงที่ของระบบ: Role / RoleLevel / Job Status / Action Type
อิงตามการออกแบบระบบ NRW Job Management ที่สรุปไว้ก่อน coding
"""

# ---- Role & ระดับชั้น (ยิ่งเลขน้อย = ตำแหน่งยิ่งสูง) ----
ROLE_ADMIN = "Admin"
ROLE_DEPUTY_GOVERNOR = "รองผู้ว่าการ"
ROLE_ASSISTANT_GOVERNOR = "ผู้ช่วยผู้ว่าการ"
ROLE_BRANCH_MANAGER = "ผู้จัดการ (สาขา)"
ROLE_DIVISION_DIRECTOR = "ผู้อำนวยการ (กอง)"
ROLE_SECTION_CHIEF = "หัวหน้า (ส่วน)"
ROLE_ENGINEER = "วิศวกร"
ROLE_FIELD_TECH = "ช่างสนาม"
ROLE_CONTRACTOR = "ผู้รับจ้าง"
ROLE_VIEWER = "Viewer"

# ระดับ (RoleLevel) — ใช้เทียบ "สูงกว่า/เท่ากับ/ต่ำกว่า"
ROLE_LEVELS = {
    ROLE_ADMIN: 0,
    ROLE_DEPUTY_GOVERNOR: 1,
    ROLE_ASSISTANT_GOVERNOR: 2,
    ROLE_BRANCH_MANAGER: 3,
    ROLE_DIVISION_DIRECTOR: 4,
    ROLE_SECTION_CHIEF: 5,
    ROLE_ENGINEER: 6,
    ROLE_FIELD_TECH: 7,
    ROLE_CONTRACTOR: 7,
    ROLE_VIEWER: 99,
}

# Role ที่ "ต่ำกว่าระดับสาขา" — ใช้กำหนดว่าใครโอนงาน (lateral transfer) กันเองได้
BELOW_BRANCH_LEVEL_ROLES = {
    ROLE_DIVISION_DIRECTOR,
    ROLE_SECTION_CHIEF,
    ROLE_ENGINEER,
    ROLE_FIELD_TECH,
    ROLE_CONTRACTOR,
}

# Role ที่มีสิทธิ์ "มอบหมายงาน" ได้ (ผู้จัดการ -> ผู้อำนวยการ -> หัวหน้าส่วน -> วิศวกร)
ASSIGNER_ROLES = {
    ROLE_BRANCH_MANAGER,
    ROLE_DIVISION_DIRECTOR,
    ROLE_SECTION_CHIEF,
    ROLE_ENGINEER,
}

# Role ที่กำหนด/แก้ไข Due Date ได้ (หัวหน้าส่วนขึ้นไป คือ level <= SECTION_CHIEF level)
DUE_DATE_EDITOR_MAX_LEVEL = ROLE_LEVELS[ROLE_SECTION_CHIEF]

# Role ที่ปิดงานได้ (หัวหน้าส่วนขึ้นไป)
CLOSE_JOB_MAX_LEVEL = ROLE_LEVELS[ROLE_SECTION_CHIEF]

# Role ที่ยกเลิกงานได้ (ผู้อำนวยการ (กอง) ขึ้นไป)
CANCEL_JOB_MAX_LEVEL = ROLE_LEVELS[ROLE_DIVISION_DIRECTOR]

# ---- สถานะงาน (Job Status) ----
STATUS_PENDING_ASSIGNMENT = "รอมอบหมาย"
STATUS_PENDING_ACCEPTANCE = "มอบหมายแล้ว รอรับ"
STATUS_ACCEPTED = "รับงานแล้ว"
STATUS_REJECTED = "ปฏิเสธ"
STATUS_IN_PROGRESS = "กำลังดำเนินการ"
STATUS_COMPLETED_PENDING_VERIFY = "เสร็จ รอตรวจสอบ"
STATUS_REOPENED = "ตีกลับ"
STATUS_CLOSED = "ปิดงาน"
STATUS_CANCELLED = "ยกเลิกงาน"

ALL_JOB_STATUSES = [
    STATUS_PENDING_ASSIGNMENT,
    STATUS_PENDING_ACCEPTANCE,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    STATUS_IN_PROGRESS,
    STATUS_COMPLETED_PENDING_VERIFY,
    STATUS_REOPENED,
    STATUS_CLOSED,
    STATUS_CANCELLED,
]

# ---- Incident conversion status ----
CONVERSION_NOT_CONVERTED = "ยังไม่แปลง"
CONVERSION_PARTIAL = "แปลงบางส่วน"
CONVERSION_FULL = "แปลงครบแล้ว"

# ---- JobLogs ActionType ----
ACTION_MANUAL_ASSIGN = "Manual Assign"
ACTION_LATERAL_TRANSFER = "Lateral Transfer"
ACTION_ACCEPT = "Accept"
ACTION_REJECT = "Reject"
ACTION_START_WORK = "Start Work"
ACTION_SUBMIT_COMPLETION = "Submit Completion"
ACTION_VERIFY_PASS = "Verify Pass"
ACTION_VERIFY_FAIL = "Verify Fail (Reopen)"
ACTION_CLOSE = "Close"
ACTION_CANCEL = "Cancel"
ACTION_SET_DUE_DATE = "Set DueDate"
ACTION_DUE_DATE_WARNING = "DueDate Warning Triggered"
