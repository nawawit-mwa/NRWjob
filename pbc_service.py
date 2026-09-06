# -*- coding: utf-8 -*-
"""
pbc_service.py — ชั้นข้อมูลของโมดูลติดตามพื้นที่ PBC

หน้าที่
  - เชื่อม Google Sheet ไฟล์ NRW_PBC (แยกจาก Sheet ปฏิบัติการเดิม)
  - รวมค่าดิบจาก WB220 กับค่าที่ผู้ใช้ปรับทับ ให้ได้ "ค่าที่ใช้จริง"
  - คำนวณอัตราน้ำสูญเสียรายเดือนและรอบ 3 เดือนตามนิยามสัญญา
  - อ่านค่า MNF จาก CSV ที่ compute_pbc_daily.py สร้างไว้ (ใช้เฝ้าระวังเท่านั้น)

ข้อกำหนดที่ยึดตลอดไฟล์นี้
  - MonthlyRaw และ MonthlyOverride เขียนแบบ append-only
    ค่าที่ใช้จริงคือแถวล่าสุดของแต่ละคีย์ ไม่เคยลบของเก่าทิ้ง
  - เขียน Sheet ด้วย RAW mode เสมอ กันวันที่/รหัสถูกแปลงรูป
"""

import csv
import io
import json
import os
import threading
import time
import uuid
from datetime import datetime

import pbc_config as CFG

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:  # ให้ import ไฟล์นี้ได้แม้ยังไม่ได้ติดตั้ง gspread
    gspread = None
    Credentials = None

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_lock = threading.Lock()
_client = None
_spreadsheet = None
_cache = {}


# ------------------------------------------------------------------ เชื่อม Sheet

def _credentials():
    if CFG.GOOGLE_CREDENTIALS_JSON:
        info = json.loads(CFG.GOOGLE_CREDENTIALS_JSON)
        return Credentials.from_service_account_info(info, scopes=_SCOPES)
    return Credentials.from_service_account_file(
        CFG.GOOGLE_CREDENTIALS_PATH, scopes=_SCOPES
    )


def get_spreadsheet():
    """คืน object ของ Sheet NRW_PBC (สร้าง client ครั้งเดียวแล้วใช้ซ้ำ)"""
    global _client, _spreadsheet
    if gspread is None:
        raise RuntimeError("ยังไม่ได้ติดตั้ง gspread — ติดตั้งก่อนใช้งานโมดูล PBC")
    with _lock:
        if _spreadsheet is None:
            _client = gspread.authorize(_credentials())
            if CFG.PBC_SPREADSHEET_KEY:
                _spreadsheet = _client.open_by_key(CFG.PBC_SPREADSHEET_KEY)
            else:
                _spreadsheet = _client.open(CFG.PBC_SPREADSHEET_NAME)
        return _spreadsheet


def _worksheet(tab):
    return get_spreadsheet().worksheet(tab)


def invalidate_cache(tab=None):
    """ล้าง cache หลังเขียนข้อมูล เพื่อให้รอบถัดไปอ่านค่าใหม่"""
    with _lock:
        if tab is None:
            _cache.clear()
        else:
            _cache.pop(tab, None)


def read_tab(tab, use_cache=True):
    """
    อ่านทั้ง tab คืน list ของ dict ตามหัวคอลัมน์ใน SHEET_SCHEMAS
    ใช้ cache สั้นๆ เพื่อลดจำนวน request ต่อการโหลดหน้าหนึ่งครั้ง
    """
    now = time.time()
    if use_cache:
        hit = _cache.get(tab)
        if hit and now - hit[0] < CFG.SHEET_CACHE_TTL:
            return hit[1]

    values = _worksheet(tab).get_all_values()
    if not values:
        rows = []
    else:
        header = [h.strip() for h in values[0]]
        rows = []
        for raw in values[1:]:
            if not any(str(c).strip() for c in raw):
                continue
            padded = list(raw) + [""] * (len(header) - len(raw))
            rows.append({header[i]: padded[i] for i in range(len(header))})

    with _lock:
        _cache[tab] = (now, rows)
    return rows


def append_rows(tab, records):
    """เพิ่มแถวท้าย tab ตามลำดับคอลัมน์ใน schema (RAW mode)"""
    if not records:
        return 0
    schema = CFG.SHEET_SCHEMAS[tab]
    payload = []
    for rec in records:
        payload.append([_cell(rec.get(col, ""), col) for col in schema])
    _worksheet(tab).append_rows(payload, value_input_option="RAW")
    invalidate_cache(tab)
    return len(payload)


def _cell(value, column):
    """แปลงค่าให้เหมาะกับ Sheet — คอลัมน์รหัส/วันที่บังคับเป็น text"""
    if value is None:
        return ""
    if column in CFG.TEXT_COLUMNS:
        return str(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return value


# ------------------------------------------------------------------ ตัวช่วยทั่วไป

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def to_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def to_int(value, default=None):
    v = to_float(value, None)
    return int(v) if v is not None else default


def month_add(month, delta):
    """'2026-07' + 2 -> '2026-09'"""
    year, mon = (int(p) for p in month.split("-"))
    idx = (year * 12 + mon - 1) + delta
    return "%04d-%02d" % (idx // 12, idx % 12 + 1)


def month_diff(a, b):
    """จำนวนเดือนจาก a ถึง b (b - a)"""
    ya, ma = (int(p) for p in a.split("-"))
    yb, mb = (int(p) for p in b.split("-"))
    return (yb * 12 + mb) - (ya * 12 + ma)


def month_no(start_month, month):
    """เดือนเริ่มสัญญานับเป็นเดือนที่ 1 (นิยามข้อ 1.20)"""
    return month_diff(start_month, month) + 1


def month_from_no(start_month, no):
    return month_add(start_month, int(no) - 1)


_THAI_MONTHS_SHORT = [
    "", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
    "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.",
]


def month_label_th(month):
    """'2026-07' -> 'ก.ค. 69'"""
    try:
        year, mon = (int(p) for p in month.split("-"))
    except (ValueError, AttributeError):
        return month
    return "%s %02d" % (_THAI_MONTHS_SHORT[mon], (year + 543) % 100)


# ------------------------------------------------------------------ อ่านข้อมูลสัญญา

def list_contracts(branch_codes=None):
    """
    คืนรายการสัญญา ถ้าส่ง branch_codes มาจะกรองเฉพาะสาขานั้น
    (ใช้จำกัดสิทธิ์ให้ผู้ใช้เห็นเฉพาะสัญญาของสาขาตัวเอง)
    """
    rows = read_tab(CFG.TAB_CONTRACTS)
    out = []
    for r in rows:
        if branch_codes is not None and r.get("branch_code") not in branch_codes:
            continue
        out.append({
            "contract_id": r.get("contract_id", ""),
            "contract_no": r.get("contract_no", ""),
            "area_name": r.get("area_name", ""),
            "branch_code": r.get("branch_code", ""),
            "branch_name": r.get("branch_name", ""),
            "start_month": r.get("start_month", ""),
            "duration_days": to_int(r.get("duration_days"), 0),
            "baseline_rate_x": to_float(r.get("baseline_rate_x")),
            "mnf_hours_per_day": to_float(
                r.get("mnf_hours_per_day"), CFG.DEFAULT_MNF_HOURS_PER_DAY
            ),
            "status": r.get("status", ""),
            "note": r.get("note", ""),
        })
    return out


def get_contract(contract_id):
    for c in list_contracts():
        if c["contract_id"] == contract_id:
            return c
    return None


def get_targets(contract_id):
    """เป้าอัตราน้ำสูญเสียตามสัญญา เรียงตามเดือนวัดผล"""
    rows = read_tab(CFG.TAB_TARGETS)
    out = []
    for r in rows:
        if r.get("contract_id") != contract_id:
            continue
        no = to_int(r.get("measure_month_no"))
        rate = to_float(r.get("target_rate"))
        if no is None or rate is None:
            continue
        out.append({"month_no": no, "target_rate": rate, "note": r.get("note", "")})
    out.sort(key=lambda x: x["month_no"])
    return out


def get_contract_dmas(contract_id, month=None):
    """
    รายชื่อ DMA ในสัญญา ถ้าระบุ month จะกรองตามช่วงเวลาที่มีผล
    รองรับกรณีมีการรวม/ตัด DMA ระหว่างสัญญา
    """
    rows = read_tab(CFG.TAB_CONTRACT_DMA)
    out = []
    for r in rows:
        if r.get("contract_id") != contract_id:
            continue
        code = (r.get("dma_code") or "").strip()
        if not code:
            continue
        if month:
            eff_from = (r.get("effective_from") or "").strip()
            eff_to = (r.get("effective_to") or "").strip()
            if eff_from and month < eff_from:
                continue
            if eff_to and month > eff_to:
                continue
        out.append({"dma_code": code, "note": r.get("note", "")})
    out.sort(key=lambda x: x["dma_code"])
    return out


def get_meter_map(dma_codes=None):
    """คืน dict {dma_code: [ {rtu_id, direction}, ... ]}"""
    rows = read_tab(CFG.TAB_METER_MAP)
    out = {}
    for r in rows:
        code = (r.get("dma_code") or "").strip()
        rtu = (r.get("rtu_id") or "").strip()
        if not code or not rtu:
            continue
        if dma_codes is not None and code not in dma_codes:
            continue
        out.setdefault(code, []).append({
            "rtu_id": rtu,
            "direction": (r.get("direction") or "I").strip().upper(),
        })
    return out


# ------------------------------------------------------------------ ค่ารายเดือน

def _latest_by_key(rows, key_fn, order_fn):
    """เก็บเฉพาะแถวล่าสุดของแต่ละคีย์ (ตาราง append-only)"""
    best = {}
    for idx, r in enumerate(rows):
        key = key_fn(r)
        order = (order_fn(r), idx)
        if key not in best or order > best[key][0]:
            best[key] = (order, r)
    return {k: v[1] for k, v in best.items()}


def get_monthly_effective(contract_id):
    """
    รวมค่าดิบจาก WB220 กับค่าที่ปรับทับ คืน dict
        {(month, dma_code): record}

    record มีคีย์:
        inflow_m3, billed_total_m3, other_m3, sales_m3, loss_m3, loss_rate,
        avg_pressure_24h, avg_pressure_night, avg_flow_24h, avg_flow_night,
        overrides {field: {"raw", "value", "reason", "by", "at"}}
    """
    raw_rows = [
        r for r in read_tab(CFG.TAB_MONTHLY_RAW)
        if r.get("contract_id") == contract_id
    ]
    latest_raw = _latest_by_key(
        raw_rows,
        lambda r: (r.get("month", ""), r.get("dma_code", "")),
        lambda r: r.get("uploaded_at", ""),
    )

    ovr_rows = [
        r for r in read_tab(CFG.TAB_MONTHLY_OVERRIDE)
        if r.get("contract_id") == contract_id
    ]
    latest_ovr = _latest_by_key(
        ovr_rows,
        lambda r: (r.get("month", ""), r.get("dma_code", ""), r.get("field", "")),
        lambda r: r.get("updated_at", ""),
    )

    out = {}
    for (month, code), r in latest_raw.items():
        rec = {
            "month": month,
            "dma_code": code,
            "inflow_m3": to_float(r.get("inflow_m3"), 0.0),
            "billed_total_m3": to_float(r.get("billed_total_m3"), 0.0),
            "other_m3": to_float(r.get("other_m3"), 0.0),
            "avg_pressure_24h": to_float(r.get("avg_pressure_24h")),
            "avg_pressure_night": to_float(r.get("avg_pressure_night")),
            "avg_flow_24h": to_float(r.get("avg_flow_24h")),
            "avg_flow_night": to_float(r.get("avg_flow_night")),
            "overrides": {},
        }
        for field in CFG.OVERRIDABLE_FIELDS:
            o = latest_ovr.get((month, code, field))
            if not o:
                continue
            value = to_float(o.get("value"))
            if value is None:
                continue
            rec["overrides"][field] = {
                "raw": rec[field],
                "value": value,
                "reason": o.get("reason", ""),
                "by": o.get("updated_by", ""),
                "at": o.get("updated_at", ""),
            }
            rec[field] = value

        rec["sales_m3"] = rec["billed_total_m3"] + rec["other_m3"]
        rec["loss_m3"] = rec["inflow_m3"] - rec["sales_m3"]
        rec["loss_rate"] = (
            rec["loss_m3"] / rec["inflow_m3"] * 100.0 if rec["inflow_m3"] else None
        )
        out[(month, code)] = rec
    return out


def available_months(monthly):
    return sorted({m for m, _ in monthly.keys()})


def aggregate(monthly, months, dma_codes=None):
    """รวมยอดหลายเดือน/หลาย DMA คืนยอดรวมและอัตราน้ำสูญเสีย"""
    inflow = sales = 0.0
    n = 0
    for (month, code), rec in monthly.items():
        if month not in months:
            continue
        if dma_codes is not None and code not in dma_codes:
            continue
        inflow += rec["inflow_m3"]
        sales += rec["sales_m3"]
        n += 1
    loss = inflow - sales
    return {
        "inflow_m3": inflow,
        "sales_m3": sales,
        "loss_m3": loss,
        "loss_rate": (loss / inflow * 100.0) if inflow else None,
        "n_records": n,
    }


def rolling_window(month, size=CFG.ROLLING_MONTHS):
    """เดือนที่วัดผล + เดือนก่อนหน้า ตามนิยามข้อ 1.26"""
    return [month_add(month, -i) for i in range(size - 1, -1, -1)]


def rolling_series(monthly, start_month, dma_codes=None):
    """
    ชุดข้อมูลอัตราน้ำสูญเสียรอบ 3 เดือน ของทุกเดือนที่ข้อมูลครบ
    คืน list ของ dict {month, month_no, label, ...}
    """
    months = available_months(monthly)
    have = set(months)
    out = []
    for m in months:
        window = rolling_window(m)
        if not all(w in have for w in window):
            continue
        agg = aggregate(monthly, set(window), dma_codes)
        if agg["loss_rate"] is None:
            continue
        out.append({
            "month": m,
            "month_no": month_no(start_month, m),
            "label": month_label_th(m),
            "window": window,
            "inflow_m3": round(agg["inflow_m3"], 2),
            "sales_m3": round(agg["sales_m3"], 2),
            "loss_m3": round(agg["loss_m3"], 2),
            "loss_rate": round(agg["loss_rate"], 2),
        })
    out.sort(key=lambda x: x["month_no"])
    return out


def monthly_series(monthly, start_month, dma_codes=None):
    """อัตราน้ำสูญเสียรายเดือน (ไม่ rolling) ใช้ดูความผันผวนระหว่างทาง"""
    out = []
    for m in available_months(monthly):
        agg = aggregate(monthly, {m}, dma_codes)
        if agg["loss_rate"] is None:
            continue
        out.append({
            "month": m,
            "month_no": month_no(start_month, m),
            "label": month_label_th(m),
            "inflow_m3": round(agg["inflow_m3"], 2),
            "sales_m3": round(agg["sales_m3"], 2),
            "loss_m3": round(agg["loss_m3"], 2),
            "loss_rate": round(agg["loss_rate"], 2),
        })
    out.sort(key=lambda x: x["month_no"])
    return out


# ------------------------------------------------------------------ ข้อมูล RTU

def _read_csv(filename):
    path = os.path.join(CFG.PBC_STATIC_DIR, filename)
    if not os.path.exists(path):
        return []
    with io.open(path, "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def get_dma_rtu_status(dma_codes=None):
    """
    อ่าน pbc_dma_current.csv ที่สร้างจากข้อมูล RTU
    ใช้เฉพาะการเฝ้าระวังและใช้เป็นน้ำหนักกระจายเป้า ไม่นำมาคิดปริมาณน้ำเข้า
    """
    out = {}
    for r in _read_csv(CFG.CSV_DMA_CURRENT):
        code = (r.get("dma_code") or "").strip()
        if not code:
            continue
        if dma_codes is not None and code not in dma_codes:
            continue
        out[code] = {
            "mnf_current": to_float(r.get("mnf_current")),
            "mnf_floor": to_float(r.get("mnf_floor")),
            "mnf_baseline": to_float(r.get("mnf_baseline")),
            "n_days": to_int(r.get("n_days"), 0),
            "last_date": r.get("last_date", ""),
            "n_meters": to_int(r.get("n_meters"), 0),
            "good_days_last_month": to_int(r.get("good_days_last_month"), 0),
        }
    return out


def get_mnf_daily(dma_code, limit=90):
    rows = [
        r for r in _read_csv(CFG.CSV_DMA_MNF_DAILY)
        if (r.get("dma_code") or "").strip() == dma_code
    ]
    rows.sort(key=lambda r: r.get("date", ""))
    rows = rows[-limit:]
    return [
        {
            "date": r.get("date", ""),
            "mnf": to_float(r.get("mnf")),
            "avg_flow": to_float(r.get("avg_flow")),
            "avg_pressure": to_float(r.get("avg_pressure")),
            "n_points": to_int(r.get("n_points"), 0),
        }
        for r in rows
    ]


def get_hourly_envelope(dma_code):
    rows = [
        r for r in _read_csv(CFG.CSV_HOURLY_ENVELOPE)
        if (r.get("dma_code") or "").strip() == dma_code
    ]
    rows.sort(key=lambda r: to_int(r.get("interval_idx"), 0))
    return [
        {
            "interval_idx": to_int(r.get("interval_idx"), 0),
            "f_min": to_float(r.get("f_min")),
            "f_max": to_float(r.get("f_max")),
            "f_latest": to_float(r.get("f_latest")),
            "p_min": to_float(r.get("p_min")),
            "p_max": to_float(r.get("p_max")),
            "p_latest": to_float(r.get("p_latest")),
        }
        for r in rows
    ]


# ------------------------------------------------------------------ เขียนข้อมูล

def save_upload(contract_id, month, filename, records, user, warnings=None):
    """บันทึกผล parse WB220 ลง MonthlyRaw แบบ append-only + เขียน UploadLog"""
    upload_id = "UP-" + uuid.uuid4().hex[:10].upper()
    stamp = now_str()
    payload = []
    for rec in records:
        row = dict(rec)
        row.update({
            "upload_id": upload_id,
            "contract_id": contract_id,
            "month": month,
            "uploaded_by": user,
            "uploaded_at": stamp,
        })
        payload.append(row)
    n = append_rows(CFG.TAB_MONTHLY_RAW, payload)
    append_rows(CFG.TAB_UPLOAD_LOG, [{
        "upload_id": upload_id,
        "contract_id": contract_id,
        "month": month,
        "filename": filename,
        "n_rows": n,
        "uploaded_by": user,
        "uploaded_at": stamp,
        "status": "OK",
        "message": " | ".join(warnings or [])[:2000],
    }])
    return upload_id, n


def save_override(contract_id, dma_code, month, field, value, reason, user):
    if field not in CFG.OVERRIDABLE_FIELDS:
        raise ValueError("ปรับค่าช่อง %s ไม่ได้" % field)
    append_rows(CFG.TAB_MONTHLY_OVERRIDE, [{
        "contract_id": contract_id,
        "dma_code": dma_code,
        "month": month,
        "field": field,
        "value": value,
        "reason": reason,
        "updated_by": user,
        "updated_at": now_str(),
    }])


def get_dma_targets(contract_id, measure_month_no):
    """เป้าราย DMA ที่บันทึกไว้ (แถวล่าสุดของแต่ละ DMA)"""
    rows = [
        r for r in read_tab(CFG.TAB_DMA_TARGETS)
        if r.get("contract_id") == contract_id
        and to_int(r.get("measure_month_no")) == int(measure_month_no)
    ]
    latest = _latest_by_key(
        rows, lambda r: r.get("dma_code", ""), lambda r: r.get("updated_at", "")
    )
    out = {}
    for code, r in latest.items():
        out[code] = {
            "target_loss_m3": to_float(r.get("target_loss_m3")),
            "target_mnf": to_float(r.get("target_mnf")),
            "is_manual": str(r.get("is_manual", "")).strip().upper() in ("TRUE", "1", "YES"),
            "updated_by": r.get("updated_by", ""),
            "updated_at": r.get("updated_at", ""),
        }
    return out


def save_dma_targets(contract_id, measure_month_no, targets, user):
    """
    targets : list ของ dict {dma_code, target_loss_m3, target_mnf, is_manual}
    เขียนแบบ append-only เก็บประวัติการปรับเป้าไว้ทั้งหมด
    """
    stamp = now_str()
    payload = []
    for t in targets:
        payload.append({
            "contract_id": contract_id,
            "dma_code": t["dma_code"],
            "measure_month_no": int(measure_month_no),
            "target_loss_m3": round(t.get("target_loss_m3") or 0.0, 2),
            "target_mnf": (
                round(t["target_mnf"], 3) if t.get("target_mnf") is not None else ""
            ),
            "is_manual": bool(t.get("is_manual")),
            "updated_by": user,
            "updated_at": stamp,
        })
    return append_rows(CFG.TAB_DMA_TARGETS, payload)


def get_remarks(contract_id, dma_code=None):
    rows = [
        r for r in read_tab(CFG.TAB_REMARKS)
        if r.get("contract_id") == contract_id
        and (dma_code is None or r.get("dma_code") == dma_code)
    ]
    rows.sort(key=lambda r: (r.get("event_date", ""), r.get("recorded_at", "")),
              reverse=True)
    return rows


def save_remark(contract_id, dma_code, event_date, category, text, user):
    append_rows(CFG.TAB_REMARKS, [{
        "contract_id": contract_id,
        "dma_code": dma_code,
        "event_date": event_date,
        "category": category,
        "text": text,
        "recorded_by": user,
        "recorded_at": now_str(),
    }])
