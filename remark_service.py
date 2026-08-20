"""
remark_service.py
ลบ tag remark ของ RTU ออกจาก Sheet 'remark' — เรียกใช้อัตโนมัติเมื่อมีการบันทึกสถานะงาน
"ปิดงาน" สำเร็จ ผ่าน tab "การดำเนินการ" ในหน้า NRW Monitoring

Sheet 'remark' เป็นแท็บหนึ่งในไฟล์ Google Sheet เดียวกับ NRW Job (REMARK_SHEET_ID ตรงกับ
GOOGLE_SHEET_ID) จึง service account เดิมเขียนได้เลย ไม่ต้องขอสิทธิ์เพิ่ม
คอลัมน์จริงของแท็บนี้: ลำดับ, RTU_ID, Remark, ผู้แจ้งข้อมูล, การดำเนินการ, วันที่ update
"""

import sheets_client as sc

REMARK_SHEET_NAME = "remark"


def clear_remark_for_rtu(rtu_id: str) -> bool:
    """ลบทั้งแถวของ RTU นี้ออกจาก Sheet remark (ลบจริง ไม่ใช่แค่ล้างข้อความ ตามที่ยืนยันไว้)
    คืน True ถ้าพบและลบสำเร็จ, False ถ้าไม่มีแถว remark ของ RTU นี้อยู่แล้ว (ไม่ error)"""
    return sc.delete_row(REMARK_SHEET_NAME, "RTU_ID", rtu_id)
