# -*- coding: utf-8 -*-
"""
pbc_schema_setup.py — สร้าง/อัปเดตโครง Google Sheet ของระบบ PBC
และใส่ข้อมูลตั้งต้นของสัญญา จบส.1-2568 (สาขามหาสวัสดิ์)

รันครั้งแรก:
    python pbc_schema_setup.py --create --seed

รันซ้ำเมื่อเพิ่มคอลัมน์ใหม่ (ปลอดภัย แก้แค่แถวหัวตาราง):
    python pbc_schema_setup.py --create

ข้อควรระวัง: --seed จะเพิ่มข้อมูลสัญญาตัวอย่างเข้าไป ถ้ารันซ้ำจะได้ข้อมูลซ้ำ
สคริปต์จึงเช็คก่อนว่ามี contract_id นี้อยู่แล้วหรือยัง
"""

import argparse

import pbc_config as CFG
import pbc_service as SVC

# ---------------------------------------------------------------- ข้อมูลตั้งต้น

CONTRACT_ID = "PBC-56-001"

CONTRACT_ROW = {
    "contract_id": CONTRACT_ID,
    "contract_no": "จบส.1-2568",
    "area_name": "พื้นที่เป้าหมายสาขามหาสวัสดิ์",
    "branch_code": "56",
    "branch_name": "สำนักงานประปาสาขามหาสวัสดิ์",
    "start_month": "2026-06",     # เดือนเริ่มสัญญา = เดือนที่ 1
    "duration_days": 790,
    "baseline_rate_x": 44.36,     # อัตราน้ำสูญเสียฐาน — แก้เป็นค่าที่ตกลงกับผู้รับจ้าง
    "mnf_hours_per_day": 24,      # ตัวคูณแปลง MNF เป็นปริมาณ ปรับได้ภายหลัง
    "status": "active",
    "note": "ค่า X เป็นตัวอย่างที่คำนวณจาก WB220 เดือน ก.ค. 69 "
            "ให้แก้เป็นค่าที่ตกลงกับผู้รับจ้างจริง",
}

# เป้าตามตารางในสัญญา ข้อ 2.1.1(2): X-1.50 / -2.88 / -4.25 / -5.65 / -6.90
TARGET_STEPS = [(8, 1.50), (14, 2.88), (20, 4.25), (23, 5.65), (26, 6.90)]

# 20 DMA ตามเอกสารแนบท้าย
CONTRACT_DMAS = [
    "56-01-01", "56-01-03", "56-01-05", "56-03-01", "56-03-05",
    "56-03-06", "56-04-01", "56-04-02", "56-04-03", "56-04-04",
    "56-04-05", "56-05-01", "56-06-01", "56-06-02", "56-06-03",
    "56-06-04", "56-06-05", "56-06-06", "56-07-01", "56-07-04",
]

# มาตรวัดน้ำหลักต่อ DMA — คัดจาก RTU_Mapping.xlsx เฉพาะ 20 DMA ในสัญญา
# ทุกตัวเป็นน้ำเข้า (I) ไม่มีมาตรน้ำออกที่ต้องหักลบ
METER_MAP = [
    ("56-01-01", "DM-56-01-01-01", "I"),
    ("56-01-03", "DM-56-01-03-01", "I"),
    ("56-01-03", "DM-56-01-03-02", "I"),
    ("56-01-05", "DM-56-01-05-01", "I"),
    ("56-03-01", "DM-56-03-01-01", "I"),
    ("56-03-01", "DM-56-03-01-02", "I"),
    ("56-03-05", "DM-56-03-05-02", "I"),
    ("56-03-06", "DM-56-03-06-01", "I"),
    ("56-03-06", "DM-56-03-06-02", "I"),
    ("56-03-06", "DM-56-03-06-03", "I"),
    ("56-04-01", "DM-56-04-01-01", "I"),
    ("56-04-02", "DM-56-04-02-01", "I"),
    ("56-04-03", "DM-56-04-03-01", "I"),
    ("56-04-04", "DM-56-04-04-01", "I"),
    ("56-04-04", "DM-56-04-04-02", "I"),
    ("56-04-05", "DM-56-04-05-01", "I"),
    ("56-04-05", "DM-56-04-05-02", "I"),
    ("56-05-01", "DM-56-05-01-01", "I"),
    ("56-05-01", "DM-56-05-01-02", "I"),
    ("56-05-01", "DM-56-05-01-03", "I"),
    ("56-06-01", "DM-56-06-01-01", "I"),
    ("56-06-02", "DM-56-06-02-01", "I"),
    ("56-06-03", "DM-56-06-03-01", "I"),
    ("56-06-04", "DM-56-06-04-01", "I"),
    ("56-06-05", "DM-56-06-05-01", "I"),
    ("56-06-06", "DM-56-06-06-01", "I"),
    ("56-07-01", "DM-56-07-01-01", "I"),
    ("56-07-04", "DM-56-07-04-01", "I"),
    ("56-07-04", "DM-56-07-04-02", "I"),
]


# ---------------------------------------------------------------- สร้างโครง

def create_all_tabs():
    """สร้าง tab ที่ยังไม่มี และเขียนหัวคอลัมน์ให้ตรง schema"""
    sheet = SVC.get_spreadsheet()
    existing = {ws.title: ws for ws in sheet.worksheets()}

    for tab, header in CFG.SHEET_SCHEMAS.items():
        if tab in existing:
            ws = existing[tab]
            current = ws.row_values(1)
            if current[:len(header)] == header:
                print("  - %s: หัวตารางตรงอยู่แล้ว" % tab)
                continue
            ws.update(
                values=[header],
                range_name="A1:%s1" % _col_letter(len(header)),
                value_input_option="RAW",
            )
            print("  * %s: อัปเดตหัวตาราง (%d คอลัมน์)" % (tab, len(header)))
        else:
            ws = sheet.add_worksheet(title=tab, rows=1000, cols=max(len(header), 12))
            ws.update(
                values=[header],
                range_name="A1:%s1" % _col_letter(len(header)),
                value_input_option="RAW",
            )
            print("  + %s: สร้างใหม่ (%d คอลัมน์)" % (tab, len(header)))

    SVC.invalidate_cache()
    print("สร้าง/อัปเดตโครง Sheet เรียบร้อย")


def _col_letter(n):
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


# ---------------------------------------------------------------- ข้อมูลตั้งต้น

def seed_contract():
    """ใส่ข้อมูลสัญญา เป้าหมาย รายชื่อ DMA และผังมาตร — ข้ามถ้ามีอยู่แล้ว"""
    existing = {c["contract_id"] for c in SVC.list_contracts()}
    if CONTRACT_ID in existing:
        print("มีสัญญา %s อยู่แล้ว ข้ามการใส่ข้อมูลตั้งต้น" % CONTRACT_ID)
        return

    SVC.append_rows(CFG.TAB_CONTRACTS, [CONTRACT_ROW])
    print("  + Contracts: %s" % CONTRACT_ID)

    x = CONTRACT_ROW["baseline_rate_x"]
    targets = [{
        "contract_id": CONTRACT_ID,
        "measure_month_no": no,
        "target_rate": round(x - drop, 2),
        "note": "X - %.2f (สัญญาข้อ 2.1.1)" % drop,
    } for no, drop in TARGET_STEPS]
    SVC.append_rows(CFG.TAB_TARGETS, targets)
    print("  + ContractTargets: %d จุดวัดผล" % len(targets))

    dmas = [{
        "contract_id": CONTRACT_ID,
        "dma_code": code,
        "effective_from": CONTRACT_ROW["start_month"],
        "effective_to": "",
        "note": "",
    } for code in CONTRACT_DMAS]
    SVC.append_rows(CFG.TAB_CONTRACT_DMA, dmas)
    print("  + ContractDMA: %d พื้นที่" % len(dmas))

    existing_map = SVC.read_tab(CFG.TAB_METER_MAP, use_cache=False)
    have = {(r.get("dma_code"), r.get("rtu_id")) for r in existing_map}
    meters = [{
        "dma_code": dma,
        "rtu_id": rtu,
        "direction": direction,
        "effective_from": "",
        "effective_to": "",
    } for dma, rtu, direction in METER_MAP if (dma, rtu) not in have]
    if meters:
        SVC.append_rows(CFG.TAB_METER_MAP, meters)
    print("  + DMAMeterMap: %d มาตร" % len(meters))
    print("ใส่ข้อมูลตั้งต้นเรียบร้อย")


def main():
    parser = argparse.ArgumentParser(description="ตั้งค่า Google Sheet ของระบบ PBC")
    parser.add_argument("--create", action="store_true",
                        help="สร้าง/อัปเดตโครง tab และหัวคอลัมน์")
    parser.add_argument("--seed", action="store_true",
                        help="ใส่ข้อมูลตั้งต้นของสัญญา จบส.1-2568")
    args = parser.parse_args()

    if not (args.create or args.seed):
        parser.print_help()
        return

    if args.create:
        print("กำลังสร้าง/อัปเดตโครง Sheet %s ..." % CFG.PBC_SPREADSHEET_NAME)
        create_all_tabs()
    if args.seed:
        print("กำลังใส่ข้อมูลตั้งต้น ...")
        seed_contract()


if __name__ == "__main__":
    main()
