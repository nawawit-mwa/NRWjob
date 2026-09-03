"""
trend_remark_service.py
บันทึก/ดึงประวัติ remark ของ "ระบบเตือนแนวโน้ม MNF/ปริมาณน้ำเข้าเพิ่มขึ้นระยะกลาง" — เก็บใน Google Sheet
เดิม (sheet เดียวกับ NRW Job ผ่าน sheets_client.py เดิม) แต่คนละ tab กับ sheet "remark" เดิม
(remark_service.py) เพราะคนละ feature กัน: อันนี้เป็นประวัติสะสม ไม่ลบเมื่อปิดงาน

ต้องรัน `python schema_setup.py` อีกครั้งก่อนใช้งานจริง เพื่อสร้าง tab "TrendRemarks" ใน Google Sheet
(schema_setup.py ถูกแก้ให้มี TrendRemarks อยู่ใน SHEET_SCHEMAS แล้ว — รันซ้ำได้ปลอดภัย ไม่กระทบ tab อื่น
ที่มีอยู่แล้ว เพราะ get_or_create_worksheet สร้างเฉพาะ tab ที่ยังไม่มีเท่านั้น)

หมวดเหตุผล (ReasonCategory) ที่เสนอ — ผู้ใช้เลือกจาก dropdown ในหน้าเว็บ (แก้ list นี้ได้ตามต้องการ):
    น้ำขายเพิ่ม / ปรับประตูน้ำ DMA / อยู่ขั้นตอน ALC / อื่นๆ
"""

from datetime import datetime

import sheets_client as sc

TREND_REMARK_SHEET_NAME = "TrendRemarks"

REASON_CATEGORIES = ["น้ำขายเพิ่ม", "ปรับประตูน้ำ DMA", "อยู่ขั้นตอน ALC", "อื่นๆ"]


def save_trend_remark(rtu_id: str, reason_category: str, detail: str, recorded_by: str,
                       event_date: str = None) -> dict:
    """บันทึก remark ใหม่ 1 แถว (append เสมอ ไม่ overwrite ของเดิม — เก็บเป็นประวัติสะสมต่อ DMA)
    event_date = วันที่เหตุการณ์จริงเกิดขึ้น (ผู้ใช้กรอกเอง เช่น วันที่ปรับประตูน้ำ) แยกจาก RecordedAt
    ที่เป็นเวลาที่ "กดบันทึก" — ถ้าไม่ส่งมา fallback เป็นวันที่วันนี้
    คืน dict ของแถวที่เพิ่งบันทึก (มี RemarkID ให้ใช้อ้างอิงต่อได้)"""
    if reason_category not in REASON_CATEGORIES:
        reason_category = "อื่นๆ"
    remark_id = sc.next_id(TREND_REMARK_SHEET_NAME, "RemarkID", "TRM-")
    row = {
        "RemarkID": remark_id,
        "RTUID": rtu_id,
        "ReasonCategory": reason_category,
        "Detail": detail or "",
        "EventDate": event_date or datetime.now().strftime("%Y-%m-%d"),
        "RecordedBy": recorded_by,
        "RecordedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    sc.append_row(TREND_REMARK_SHEET_NAME, row)
    return row


def get_remarks_for_rtu(rtu_id: str) -> list:
    """คืนประวัติ remark ทั้งหมดของ DMA นี้ เรียงใหม่สุดก่อน (สำหรับโชว์ใน popup กราฟ)"""
    rows = sc.find_many(TREND_REMARK_SHEET_NAME, "RTUID", rtu_id)
    return sorted(rows, key=lambda r: r.get("RecordedAt", ""), reverse=True)


def get_latest_remark_for_rtu(rtu_id: str) -> dict:
    """คืน remark ล่าสุดของ DMA นี้ (แสดงแบบย่อในตารางหลัก) หรือ None ถ้ายังไม่เคยมีการบันทึก"""
    rows = get_remarks_for_rtu(rtu_id)
    return rows[0] if rows else None


def get_latest_remarks_map() -> dict:
    """คืน {RTUID: remark ล่าสุด} ของทุก DMA ในครั้งเดียว — ใช้ตอน render ตารางหลัก (หลายร้อย/พัน DMA)
    เพื่อเลี่ยงการเรียก find_many ทีละ DMA (ช้าเพราะยิง filter ซ้ำบน records ที่โหลดมาแล้วทุกครั้ง)"""
    all_rows = sc.get_all_records(TREND_REMARK_SHEET_NAME)
    latest_by_rtu = {}
    for row in all_rows:
        rtu_id = row.get("RTUID")
        if not rtu_id:
            continue
        prev = latest_by_rtu.get(rtu_id)
        if prev is None or row.get("RecordedAt", "") > prev.get("RecordedAt", ""):
            latest_by_rtu[rtu_id] = row
    return latest_by_rtu
