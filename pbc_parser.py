# -*- coding: utf-8 -*-
"""
pbc_parser.py — อ่านรายงาน WLMA AN/WB220 "รายงานน้ำสูญเสียของพื้นที่" (.xls)

รายงานนี้ให้ตัวเลขที่ตรงกับนิยามในสัญญาครบทุกตัว:
  ปริมาณน้ำเข้า (ข้อ 1.14), น้ำออกบิล (1.15), น้ำอื่นๆ (1.16),
  อัตราน้ำสูญเสีย (1.19), แรงดันเฉลี่ย (1.32)

หลักการที่ยึด:
  - ระบบคำนวณ % น้ำสูญเสียเองจาก (น้ำเข้า - น้ำขาย - น้ำอื่นๆ) / น้ำเข้า เสมอ
    ค่า % ในไฟล์ใช้เป็นแค่ตัวตรวจทานว่า parse ถูกตำแหน่ง
  - หัวไฟล์มีข้อความค้างจาก template เก่าปนอยู่หลายคอลัมน์
    จึงอ่านเดือนและสาขาจากคอลัมน์ที่ 3 (index 2) เท่านั้น
  - เครื่องหมาย # * ! ท้ายรหัส DMA จะถูกตัดทิ้ง (ไม่นำมาใช้)
"""

import re
import xlrd

THAI_MONTHS = {
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4,
    "พฤษภาคม": 5, "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8,
    "กันยายน": 9, "ตุลาคม": 10, "พฤศจิกายน": 11, "ธันวาคม": 12,
}

HEADER_KEY = "รหัสพื้นที่เฝ้าระวัง"
DMA_CODE_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")

# ตำแหน่งคอลัมน์ (นับจากคอลัมน์แรกของตาราง) — รูปแบบไฟล์คงที่
COL_SEQ = 0
COL_CODE = 1
COL_LOSS_PCT = 2
COL_INFLOW = 3
COL_BILLED = 4
COL_NONBILLED = 5
COL_BILLED_TOTAL = 6
COL_OTHER = 7
COL_PRESSURE_24H = 8
COL_PRESSURE_NIGHT = 9
COL_FLOW_24H = 10
COL_FLOW_NIGHT = 11

LOSS_PCT_TOLERANCE = 0.05  # ยอมให้ต่างจากที่รายงานคำนวณไว้ได้ 0.05 จุด


class ParseError(Exception):
    """รูปแบบไฟล์ไม่ตรงกับที่รองรับ — ข้อความจะถูกนำไปแสดงให้ผู้ใช้โดยตรง"""


def _num(value):
    """แปลงค่าในเซลล์เป็นตัวเลข คืน None ถ้าว่างหรือแปลงไม่ได้"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    text = text.rstrip("#*!").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _clean_code(value):
    """ตัดเครื่องหมาย # * ! และช่องว่างออกจากรหัสพื้นที่"""
    text = str(value or "").strip()
    text = text.rstrip("#*! ").strip()
    return text


def parse_month_label(label):
    """'กรกฎาคม 2569' -> '2026-07' (แปลง พ.ศ. เป็น ค.ศ.)"""
    text = str(label or "").strip()
    parts = text.split()
    if len(parts) < 2:
        raise ParseError("อ่านเดือนของรายงานไม่ได้ (พบข้อความ: %r)" % text)
    month_name = parts[0].strip()
    if month_name not in THAI_MONTHS:
        raise ParseError("ไม่รู้จักชื่อเดือน %r ในรายงาน" % month_name)
    try:
        year = int(parts[1])
    except ValueError:
        raise ParseError("อ่านปีของรายงานไม่ได้ (พบข้อความ: %r)" % parts[1])
    if year > 2400:  # พ.ศ.
        year -= 543
    return "%04d-%02d" % (year, THAI_MONTHS[month_name])


def _find_header_row(sheet):
    """หาแถวหัวตารางจากคำว่า 'รหัสพื้นที่เฝ้าระวัง' แล้วคืน (row, col เริ่มต้น)"""
    for r in range(min(sheet.nrows, 30)):
        for c in range(sheet.ncols):
            if HEADER_KEY in str(sheet.cell_value(r, c)):
                return r, c - COL_CODE
    raise ParseError(
        "ไม่พบหัวตาราง %r — ไฟล์นี้อาจไม่ใช่รายงาน AN/WB220" % HEADER_KEY
    )


def parse_wb220(path):
    """
    อ่านไฟล์รายงาน AN/WB220 คืน dict:
      {
        "month": "2026-07",
        "branch_name": "สำนักงานประปาสาขามหาสวัสดิ์",
        "rows": [ {dma_code, inflow_m3, billed_m3, ...}, ... ],
        "warnings": [str, ...],
      }
    """
    try:
        book = xlrd.open_workbook(path)
    except Exception as exc:  # ไฟล์เสีย/ไม่ใช่ .xls
        raise ParseError("เปิดไฟล์ไม่ได้: %s" % exc)

    sheet = book.sheet_by_index(0)
    header_row, base_col = _find_header_row(sheet)
    if base_col < 0:
        raise ParseError("ตำแหน่งคอลัมน์ในไฟล์ไม่ตรงกับรูปแบบ AN/WB220")

    # เดือนและสาขาอยู่เหนือหัวตาราง ในคอลัมน์เดียวกับชื่อรายงาน
    month = None
    branch_name = ""
    label_col = base_col + COL_LOSS_PCT
    for r in range(header_row):
        text = str(sheet.cell_value(r, label_col)).strip()
        if not text:
            continue
        if month is None:
            try:
                month = parse_month_label(text)
                continue
            except ParseError:
                pass
        elif not branch_name and "สาขา" in text:
            branch_name = text
    if month is None:
        raise ParseError("อ่านเดือนของรายงานไม่ได้ (ไม่พบข้อความเดือนเหนือหัวตาราง)")

    rows = []
    warnings = []
    seen = set()

    for r in range(header_row + 1, sheet.nrows):
        code = _clean_code(sheet.cell_value(r, base_col + COL_CODE))
        if not code:
            continue
        if not DMA_CODE_RE.match(code):
            # ข้ามแถวหัวตารางซ้ำ/แถวหมายเหตุท้ายรายงาน
            continue
        if code in seen:
            warnings.append("รหัส %s ซ้ำในไฟล์ — ใช้แถวแรกที่พบ" % code)
            continue
        seen.add(code)

        def cell(offset):
            return _num(sheet.cell_value(r, base_col + offset))

        inflow = cell(COL_INFLOW)
        billed = cell(COL_BILLED)
        nonbilled = cell(COL_NONBILLED)
        billed_total = cell(COL_BILLED_TOTAL)
        other = cell(COL_OTHER)

        if billed_total is None:
            billed_total = (billed or 0.0) + (nonbilled or 0.0)
        if inflow is None:
            warnings.append("%s ไม่มีปริมาณน้ำเข้าในรายงาน — ข้ามแถวนี้" % code)
            continue

        record = {
            "dma_code": code,
            "inflow_m3": inflow,
            "billed_m3": billed or 0.0,
            "nonbilled_m3": nonbilled or 0.0,
            "billed_total_m3": billed_total,
            "other_m3": other or 0.0,
            "loss_pct_report": cell(COL_LOSS_PCT),
            "avg_pressure_24h": cell(COL_PRESSURE_24H),
            "avg_pressure_night": cell(COL_PRESSURE_NIGHT),
            "avg_flow_24h": cell(COL_FLOW_24H),
            "avg_flow_night": cell(COL_FLOW_NIGHT),
        }

        # ตรวจทานว่า parse ตรงตำแหน่ง โดยคำนวณ % เองแล้วเทียบกับที่รายงานให้มา
        if inflow > 0 and record["loss_pct_report"] is not None:
            computed = (
                (inflow - record["billed_total_m3"] - record["other_m3"]) / inflow * 100.0
            )
            if abs(computed - record["loss_pct_report"]) > LOSS_PCT_TOLERANCE:
                warnings.append(
                    "%s: %% น้ำสูญเสียในรายงาน %.2f ไม่ตรงกับที่คำนวณได้ %.2f "
                    "— ตรวจตัวเลขในรายงานก่อนใช้งาน"
                    % (code, record["loss_pct_report"], computed)
                )

        if not record["avg_flow_24h"] and not record["avg_flow_night"]:
            warnings.append(
                "%s: อัตราน้ำไหลเฉลี่ยเป็น 0 ทั้งสองช่อง (ไม่มีข้อมูลจากเครื่องวัด)"
                % code
            )

        rows.append(record)

    if not rows:
        raise ParseError("ไม่พบข้อมูลพื้นที่เฝ้าระวังในไฟล์")

    return {
        "month": month,
        "branch_name": branch_name,
        "rows": rows,
        "warnings": warnings,
    }
