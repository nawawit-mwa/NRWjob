# -*- coding: utf-8 -*-
"""
pbc_config.py — ค่าตั้งต้นกลางของโมดูลติดตามพื้นที่ PBC

โมดูลนี้แยกจากระบบ NRW เดิมทั้งหมด:
  - ใช้ Google Sheet ไฟล์ใหม่ (NRW_PBC) ไม่ปนกับ Sheet ปฏิบัติการเดิม
  - ตัวเลขที่ผูกพันสัญญามาจากรายงาน WLMA AN/WB220 เท่านั้น
  - ข้อมูล RTU ใช้เฉพาะการเฝ้าระวัง (MNF/กราฟ) ไม่นำมาคำนวณปริมาณน้ำเข้า
"""

import os

# ---------------------------------------------------------------- Google Sheet

# ชื่อไฟล์ Google Sheet ของระบบ PBC (สร้างใหม่ แยกจากของเดิม)
PBC_SPREADSHEET_NAME = os.environ.get("PBC_SPREADSHEET_NAME", "NRW_PBC")

# ถ้าตั้ง key ไว้จะใช้ open_by_key ซึ่งเร็วและแม่นกว่าเปิดด้วยชื่อ
PBC_SPREADSHEET_KEY = os.environ.get("PBC_SPREADSHEET_KEY", "").strip()

# ไฟล์ credentials ของ service account
# ปกติใช้ตัวเดียวกับระบบเดิมได้ แต่ต้องแชร์ Sheet ใหม่ให้อีเมล service account ด้วย
GOOGLE_CREDENTIALS_PATH = os.environ.get(
    "GOOGLE_CREDENTIALS_PATH", "service_account.json"
)
# หรือใส่ JSON ทั้งก้อนใน env (สำหรับ Render)
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "").strip()

# อายุ cache ของข้อมูลที่อ่านจาก Sheet (วินาที)
SHEET_CACHE_TTL = int(os.environ.get("PBC_SHEET_CACHE_TTL", "180"))

# ---------------------------------------------------------------- ไฟล์ CSV จาก RTU

# โฟลเดอร์ static ที่ compute_pbc_daily.py คัดลอกไฟล์มาให้
PBC_STATIC_DIR = os.environ.get(
    "PBC_STATIC_DIR", os.path.join("static", "data")
)

CSV_DMA_CURRENT = "pbc_dma_current.csv"        # MNF ล่าสุด/พื้นค่า/ศักยภาพ ต่อ DMA
CSV_DMA_MNF_DAILY = "pbc_dma_mnf_daily.csv"    # MNF รายวันย้อนหลัง
CSV_HOURLY_ENVELOPE = "pbc_hourly_envelope.csv"  # แถบ min-max 96 ช่วง/วัน

# ---------------------------------------------------------------- ค่าคงที่เชิงธุรกิจ

# จำนวนเดือนที่ใช้รวมเป็นหนึ่งรอบวัดผล (นิยามสัญญาข้อ 1.26–1.29)
ROLLING_MONTHS = 3

# จำนวนวันมาตรฐานต่อเดือน ใช้แปลง MNF (ลบ.ม./ชม.) เป็นปริมาณต่อเดือน
DAYS_PER_MONTH = 30

# ชั่วโมงต่อวันที่ใช้แปลง MNF เป็นปริมาณ ถ้าไม่ได้ตั้งค่าในตาราง Contracts
DEFAULT_MNF_HOURS_PER_DAY = 24.0

# เปอร์เซ็นไทล์ที่ใช้เป็น "พื้นค่า MNF" (ค่าที่ DMA นั้นเคยทำได้จริง)
MNF_FLOOR_PERCENTILE = 10

# จำนวนวันข้อมูลคุณภาพดีขั้นต่ำต่อรอบเดือน (สัญญาข้อ 2.1.9)
MIN_GOOD_DAYS_PER_MONTH = 21

# หมวดเหตุผลของบันทึกการดำเนินงาน
REMARK_CATEGORIES = [
    "ซ่อมท่อแตกรั่ว",
    "สำรวจหาจุดรั่ว (ALC)",
    "Step Test",
    "ปรับแรงดัน/ประตูน้ำ",
    "ตรวจสอบมาตรผิดปกติ",
    "ปรับปรุงแนวท่อ",
    "อื่นๆ",
]

# ช่องที่อนุญาตให้ปรับทับค่าจาก WB220 ได้
OVERRIDABLE_FIELDS = {
    "inflow_m3": "ปริมาณน้ำเข้า",
    "other_m3": "ปริมาณน้ำอื่นๆ",
    "billed_total_m3": "ปริมาณน้ำออกบิล",
}

# ---------------------------------------------------------------- ชื่อ tab ใน Sheet

TAB_CONTRACTS = "Contracts"
TAB_TARGETS = "ContractTargets"
TAB_CONTRACT_DMA = "ContractDMA"
TAB_METER_MAP = "DMAMeterMap"
TAB_MONTHLY_RAW = "MonthlyRaw"
TAB_MONTHLY_OVERRIDE = "MonthlyOverride"
TAB_DMA_TARGETS = "DMATargets"
TAB_REMARKS = "Remarks"
TAB_UPLOAD_LOG = "UploadLog"

# โครงคอลัมน์ของแต่ละ tab — ใช้ทั้งตอนสร้าง Sheet และตอนอ่าน/เขียน
SHEET_SCHEMAS = {
    TAB_CONTRACTS: [
        "contract_id", "contract_no", "area_name", "branch_code", "branch_name",
        "start_month", "duration_days", "baseline_rate_x", "mnf_hours_per_day",
        "status", "note",
    ],
    TAB_TARGETS: [
        "contract_id", "measure_month_no", "target_rate", "note",
    ],
    TAB_CONTRACT_DMA: [
        "contract_id", "dma_code", "effective_from", "effective_to", "note",
    ],
    TAB_METER_MAP: [
        "dma_code", "rtu_id", "direction", "effective_from", "effective_to",
    ],
    TAB_MONTHLY_RAW: [
        "upload_id", "contract_id", "dma_code", "month",
        "inflow_m3", "billed_m3", "nonbilled_m3", "billed_total_m3", "other_m3",
        "loss_pct_report", "avg_pressure_24h", "avg_pressure_night",
        "avg_flow_24h", "avg_flow_night", "uploaded_by", "uploaded_at",
    ],
    TAB_MONTHLY_OVERRIDE: [
        "contract_id", "dma_code", "month", "field", "value",
        "reason", "updated_by", "updated_at",
    ],
    TAB_DMA_TARGETS: [
        "contract_id", "dma_code", "measure_month_no",
        "target_loss_m3", "target_mnf", "is_manual", "updated_by", "updated_at",
    ],
    TAB_REMARKS: [
        "contract_id", "dma_code", "event_date", "category", "text",
        "recorded_by", "recorded_at",
    ],
    TAB_UPLOAD_LOG: [
        "upload_id", "contract_id", "month", "filename", "n_rows",
        "uploaded_by", "uploaded_at", "status", "message",
    ],
}

# คอลัมน์ที่ต้องบังคับเป็น text ใน Sheet (กันเลขศูนย์นำหน้าหาย/วันที่เพี้ยน)
TEXT_COLUMNS = {
    "contract_id", "dma_code", "rtu_id", "month", "start_month",
    "effective_from", "effective_to", "event_date", "uploaded_at",
    "updated_at", "recorded_at", "upload_id", "branch_code",
}
