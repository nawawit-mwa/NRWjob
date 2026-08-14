"""
demo.py
สคริปต์สาธิต workflow แบบ end-to-end บนข้อมูลตัวอย่าง
รันหลังจาก schema_setup.py และใส่ค่า config.py (Sheet ID + service account) ถูกต้องแล้ว

ขั้นตอนที่สาธิต:
1. สร้าง Master data ตัวอย่าง (สาขา, กอง/ส่วนแม่แบบ, ประเภทงาน)
2. สร้างผู้ใช้ตัวอย่างแต่ละ Role
3. สร้างเหตุการณ์ -> แปลงเป็นงาน 2 ประเภท
4. มอบหมายงาน ข้ามระดับ (ผู้จัดการสาขา -> วิศวกรตรง)
5. วิศวกรมอบหมายให้ช่าง -> ช่างรับ -> ทำงานเสร็จ -> วิศวกรตรวจผ่าน -> หัวหน้าส่วนปิดงาน
6. กำหนด DueDate ของอีกงานหนึ่งให้ช้ากว่า -> ดู Incident.DueDate ขยับ + แจ้งเตือน
"""

import sheets_client as sc
import auth_service
import incident_service
import job_service
from schema_setup import SHEET_SCHEMAS
from constants import ROLE_BRANCH_MANAGER, ROLE_SECTION_CHIEF, ROLE_ENGINEER, ROLE_FIELD_TECH


def seed_master_data():
    sc.append_row("BranchGroups", {"BranchGroupID": "BG1", "BranchGroupName": "กลุ่มสาขา 1"})
    sc.append_row("Branches", {"BranchID": "BR1", "BranchName": "สาขา A", "BranchGroupID": "BG1"})

    sc.append_row("DivisionTypes", {"DivisionTypeID": "DT1", "DivisionTypeName": "กองปฏิบัติการ"})

    sc.append_row("JobTypes", {"JobTypeID": "JT1", "JobTypeName": "ซ่อมท่อ"})
    sc.append_row("JobTypes", {"JobTypeID": "JT2", "JobTypeName": "จัดการมิเตอร์"})

    sc.append_row("SectionTypes", {
        "SectionTypeID": "ST1", "SectionTypeName": "ส่วนซ่อมท่อ",
        "DivisionTypeID": "DT1", "JobTypeID": "JT1",
    })
    sc.append_row("SectionTypes", {
        "SectionTypeID": "ST2", "SectionTypeName": "ส่วนมิเตอร์",
        "DivisionTypeID": "DT1", "JobTypeID": "JT2",
    })

    sc.append_row("Divisions", {"DivisionID": "DV1", "DivisionTypeID": "DT1", "BranchID": "BR1"})
    sc.append_row("Sections", {"SectionID": "SC1", "SectionTypeID": "ST1", "DivisionID": "DV1"})
    sc.append_row("Sections", {"SectionID": "SC2", "SectionTypeID": "ST2", "DivisionID": "DV1"})

    sc.append_row("Zones", {"ZoneID": "Z1", "ZoneName": "โซน DMA-01", "BranchID": "BR1"})


def seed_users():
    auth_service.create_user("U-MGR", "สมชาย ผู้จัดการ", "mgr1", "pass123",
                              ROLE_BRANCH_MANAGER, skip_if_exists=True, BranchID="BR1")
    auth_service.create_user("U-CHIEF1", "สมหญิง หัวหน้าส่วนท่อ", "chief1", "pass123",
                              ROLE_SECTION_CHIEF, skip_if_exists=True, BranchID="BR1", DivisionID="DV1", SectionID="SC1")
    auth_service.create_user("U-ENG1", "วิศวกร ก", "eng1", "pass123",
                              ROLE_ENGINEER, skip_if_exists=True, BranchID="BR1", DivisionID="DV1", SectionID="SC1")
    auth_service.create_user("U-TECH1", "ช่าง ข", "tech1", "pass123",
                              ROLE_FIELD_TECH, skip_if_exists=True, BranchID="BR1", DivisionID="DV1", SectionID="SC1")
    auth_service.create_user("U-CHIEF2", "หัวหน้าส่วนมิเตอร์", "chief2", "pass123",
                              ROLE_SECTION_CHIEF, skip_if_exists=True, BranchID="BR1", DivisionID="DV1", SectionID="SC2")
    auth_service.create_user("U-ENG2", "วิศวกร ค (มิเตอร์)", "eng2", "pass123",
                              ROLE_ENGINEER, skip_if_exists=True, BranchID="BR1", DivisionID="DV1", SectionID="SC2")


def run_workflow_demo():
    mgr_token = auth_service.login("mgr1", "pass123")
    mgr = auth_service.get_current_user(mgr_token)

    incident_id = incident_service.create_incident(
        incident_type="ท่อแตกจนมิเตอร์เสียหาย",
        description="ท่อ PVC 300mm แตก กระทบมิเตอร์ใกล้เคียง",
        severity="สูง", reported_by=mgr["UserID"],
        zone_id="Z1", branch_id="BR1",
    )
    print("สร้างเหตุการณ์:", incident_id)

    job_ids = incident_service.convert_incident_to_jobs(incident_id, ["JT1", "JT2"], mgr)
    print("แปลงเป็นงาน:", job_ids)
    pipe_job_id, meter_job_id = job_ids

    # ผู้จัดการสาขา มอบหมายข้ามกอง/ข้ามระดับ ตรงไปวิศวกรของงานซ่อมท่อ
    eng1_token = auth_service.login("eng1", "pass123")
    eng1 = auth_service.get_current_user(eng1_token)
    job_service.assign_job(pipe_job_id, eng1["UserID"], mgr)
    job_service.accept_job(pipe_job_id, eng1)
    print(f"งาน {pipe_job_id}: มอบหมายตรงถึงวิศวกร + รับงานแล้ว")

    tech1_token = auth_service.login("tech1", "pass123")
    tech1 = auth_service.get_current_user(tech1_token)
    job_service.assign_job(pipe_job_id, tech1["UserID"], eng1)
    job_service.accept_job(pipe_job_id, tech1)
    print(f"งาน {pipe_job_id}: วิศวกรมอบหมายช่าง + ช่างรับงาน (เข้าสถานะกำลังดำเนินการ)")

    job_service.submit_completion(pipe_job_id, tech1, remarks="ซ่อมท่อเสร็จแล้ว")
    job_service.verify_job(pipe_job_id, eng1, passed=True, notes="ตรวจแล้วผ่าน")
    print(f"งาน {pipe_job_id}: ช่างส่งงานเสร็จ + วิศวกรตรวจผ่าน")

    chief1_token = auth_service.login("chief1", "pass123")
    chief1 = auth_service.get_current_user(chief1_token)
    job_service.close_job(pipe_job_id, chief1)
    print(f"งาน {pipe_job_id}: หัวหน้าส่วนกดปิดงานเรียบร้อย")

    # กำหนด DueDate ให้งานมิเตอร์ (ช้ากว่า) เพื่อดู Incident.DueDate ขยับ + แจ้งเตือน
    job_service.set_due_date(pipe_job_id, "2026-08-20", chief1)
    chief2_token = auth_service.login("chief2", "pass123")
    chief2 = auth_service.get_current_user(chief2_token)
    job_service.set_due_date(meter_job_id, "2026-09-05", chief2)

    incident = sc.find_one("Incidents", "IncidentID", incident_id)
    print("Incident DueDate ล่าสุด (ควรเท่ากับงานมิเตอร์ 2026-09-05):", incident["DueDate"])
    print("ConvertedBy ล่าสุด:", incident["ConvertedBy"])


if __name__ == "__main__":
    print("=== Warm up cache (โหลดข้อมูลทุก sheet ด้วย API call เพียง 2 ครั้ง) ===")
    sc.warm_up(list(SHEET_SCHEMAS.keys()))

    if sc.find_one("BranchGroups", "BranchGroupID", "BG1"):
        print("=== พบข้อมูล Master data ตัวอย่างอยู่แล้ว ข้ามขั้นตอนนี้ ===")
    else:
        print("=== Seed Master Data ===")
        seed_master_data()

    print("=== Seed Users (ข้ามอัตโนมัติถ้ามี username นี้อยู่แล้ว) ===")
    seed_users()

    print("=== Run Workflow Demo ===")
    run_workflow_demo()
