"""
sheets_client.py
Wrapper กลาง สำหรับอ่าน/เขียนข้อมูลใน Google Sheets โดยใช้ gspread + Service Account
ทุก service module อื่น (auth_service, incident_service, job_service, ...) เรียกผ่านไฟล์นี้เท่านั้น

เวอร์ชันนี้เพิ่ม 2 อย่างเพื่อแก้ปัญหา 429 Quota exceeded (Read requests per minute):
1. Retry แบบ exponential backoff อัตโนมัติเมื่อโดน rate limit
2. Cache ข้อมูลทั้ง sheet ไว้ใน memory ระหว่างการรัน ลดจำนวนครั้งที่ยิง API อ่านซ้ำๆ
   (find_one / find_many / find_row_index / next_id ทั้งหมดใช้ cache นี้ ไม่ยิง API เพิ่ม
   นอกจากครั้งแรกที่อ่าน sheet นั้น หรือหลัง append/update ที่ invalidate cache)
"""

import random
import time
from collections import deque

import gspread
from gspread.exceptions import APIError
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials

import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

_client = None
_spreadsheet = None
_ws_cache = {}          # sheet_name -> worksheet object
_header_cache = {}      # sheet_name -> list of header names
_records_cache = {}     # sheet_name -> list of dict (ข้อมูลปัจจุบันของ sheet ตาม cache)
_loaded_sheets = set()  # sheet_name ที่เคยโหลดข้อมูลจริงจาก API มาไว้ใน cache แล้ว
_loaded_at = {}         # sheet_name -> timestamp (epoch) ที่โหลดข้อมูลล่าสุด (ใช้กับ TTL)

MAX_RETRIES = 6
BASE_DELAY_SECONDS = 3  # เริ่มที่ 3 วิ แล้ว double ทุกครั้งที่ retry (3,6,12,24,48,96)

# Cache หมดอายุอัตโนมัติหลังผ่านไปกี่วินาที — กันกรณี Admin แก้ Sheet ตรงๆ ผ่านเบราว์เซอร์
# (เช่น เปลี่ยนรหัสผ่าน, ปิด/เปิดบัญชี) โดยไม่ต้อง restart แอปทุกครั้ง
# ตั้งไว้ที่ 5 นาที: นานพอไม่ให้ยิง API บ่อยเกินจนชนโควตา แต่ก็ไม่ต้องรอนานเกินไปเวลาแก้ข้อมูลสด
CACHE_TTL_SECONDS = 300

# --- Proactive throttle: กันไม่ให้ยิง API เกินโควตาตั้งแต่ต้น แทนรอโดนบล็อกแล้วค่อย retry ---
# Google Sheets API ดีฟอลต์ให้ 60 requests/นาที/user เผื่อ buffer ไว้ที่ 45 ครั้งต่อ 60 วินาที
MAX_CALLS_PER_WINDOW = 45
WINDOW_SECONDS = 60
_call_timestamps = deque()


def _throttle():
    now = time.time()
    while _call_timestamps and now - _call_timestamps[0] > WINDOW_SECONDS:
        _call_timestamps.popleft()
    if len(_call_timestamps) >= MAX_CALLS_PER_WINDOW:
        sleep_time = WINDOW_SECONDS - (now - _call_timestamps[0]) + 1
        print(f"[sheets_client] ป้องกัน rate limit ล่วงหน้า — รอ {sleep_time:.1f} วินาที ก่อนเรียก API ต่อ")
        time.sleep(sleep_time)
    _call_timestamps.append(time.time())


def _with_retry(func, *args, **kwargs):
    """เรียกฟังก์ชัน gspread ใดๆ พร้อม throttle ป้องกันล่วงหน้า + retry อัตโนมัติเมื่อยังโดน 429 อยู่"""
    delay = BASE_DELAY_SECONDS
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            return func(*args, **kwargs)
        except APIError as e:
            last_error = e
            message = str(e)
            is_quota_error = "429" in message or "Quota exceeded" in message
            if is_quota_error and attempt < MAX_RETRIES:
                sleep_time = delay + random.uniform(0, 1)
                print(
                    f"[sheets_client] โดน rate limit (429) — รอ {sleep_time:.1f} วินาที "
                    f"แล้วลองใหม่ (ครั้งที่ {attempt}/{MAX_RETRIES})"
                )
                time.sleep(sleep_time)
                delay *= 2
                continue
            raise
    raise last_error


_credentials = None


def get_credentials():
    """คืน Credentials object เดียวกับที่ gspread ใช้ — ให้ drive_client.py เรียกใช้ร่วมกันได้
    (ไม่ต้องสร้าง credentials ซ้ำ/ขอสิทธิ์เพิ่ม เพราะ SCOPES มี Drive อยู่แล้ว)"""
    global _credentials
    if _credentials is None:
        _credentials = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_JSON, scopes=SCOPES
        )
    return _credentials


def get_client():
    global _client
    if _client is None:
        _client = gspread.authorize(get_credentials())
    return _client


def get_spreadsheet():
    global _spreadsheet
    if _spreadsheet is None:
        _spreadsheet = _with_retry(get_client().open_by_key, config.GOOGLE_SHEET_ID)
    return _spreadsheet


def get_worksheet(sheet_name: str):
    """คืน worksheet object พร้อม cache ไว้ (สร้างใหม่ถ้ายังไม่มี - ใช้ตอน schema_setup)"""
    if sheet_name in _ws_cache:
        return _ws_cache[sheet_name]
    ss = get_spreadsheet()
    try:
        ws = _with_retry(ss.worksheet, sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = _with_retry(ss.add_worksheet, title=sheet_name, rows=1000, cols=30)
    _ws_cache[sheet_name] = ws
    return ws


def set_headers(sheet_name: str, headers: list):
    """ตั้ง header แถวแรก (ใช้ตอน schema_setup) และ reset cache"""
    ws = get_worksheet(sheet_name)
    _with_retry(ws.update, "A1", [headers])
    _header_cache[sheet_name] = headers
    _records_cache.pop(sheet_name, None)


def get_headers(sheet_name: str):
    if sheet_name not in _header_cache:
        ws = get_worksheet(sheet_name)
        _header_cache[sheet_name] = _with_retry(ws.row_values, 1)
    return _header_cache[sheet_name]


def get_all_records(sheet_name: str, force_refresh: bool = False) -> list:
    """คืนรายการทุกแถวเป็น list of dict (key=header) — ใช้ cache เว้นแต่:
    - สั่ง force_refresh=True, หรือ
    - ยังไม่เคยอ่าน sheet นี้เลยในรอบนี้, หรือ
    - cache ของ sheet นี้หมดอายุแล้ว (เกิน CACHE_TTL_SECONDS นับจากโหลดครั้งล่าสุด)
      ป้องกันกรณี Admin แก้ Sheet ตรงๆ ผ่านเบราว์เซอร์ แล้วแอปยังใช้ข้อมูลเก่าค้างอยู่นาน"""
    is_stale = (
        sheet_name in _loaded_at
        and (time.time() - _loaded_at[sheet_name]) > CACHE_TTL_SECONDS
    )
    if not force_refresh and sheet_name in _loaded_sheets and not is_stale:
        return _records_cache[sheet_name]
    ws = get_worksheet(sheet_name)
    records = _with_retry(ws.get_all_records)
    _records_cache[sheet_name] = records
    _loaded_sheets.add(sheet_name)
    _loaded_at[sheet_name] = time.time()
    return records


def append_row(sheet_name: str, row: dict):
    """เพิ่มแถวใหม่ - row เป็น dict โดย key ต้องตรงกับ header (ที่ไม่ตรง/ไม่มีจะเว้นว่าง)
    อัปเดต cache ในหน่วยความจำทันทีแทนการอ่านซ้ำจาก API"""
    headers = get_headers(sheet_name)
    ws = get_worksheet(sheet_name)
    values = [row.get(h, "") for h in headers]
    _with_retry(ws.append_row, values, value_input_option="USER_ENTERED")

    record = {h: row.get(h, "") for h in headers}
    if sheet_name not in _loaded_sheets:
        # ยังไม่เคยโหลดข้อมูลจริงของ sheet นี้เลย ต้องโหลดก่อน 1 ครั้งเพื่อความถูกต้อง
        # (เผื่อ sheet มีแถวเดิมอยู่แล้วที่ cache ยังไม่รู้จัก)
        get_all_records(sheet_name, force_refresh=True)
    else:
        _records_cache[sheet_name].append(record)


def find_row_index(sheet_name: str, id_field: str, id_value) -> int:
    """คืนเลขแถว (1-indexed ตาม gspread, รวม header) ของ record ที่ id_field == id_value
    คำนวณจาก cache ในหน่วยความจำ ไม่ยิง API เพิ่ม — คืน None ถ้าไม่เจอ"""
    records = get_all_records(sheet_name)
    for i, r in enumerate(records):
        if str(r.get(id_field, "")) == str(id_value):
            return i + 2  # +1 เพราะ index เริ่ม 0, +1 เพราะแถว 1 คือ header
    return None


def find_one(sheet_name: str, id_field: str, id_value) -> dict:
    records = get_all_records(sheet_name)
    for r in records:
        if str(r.get(id_field, "")) == str(id_value):
            return r
    return None


def find_many(sheet_name: str, field: str, value) -> list:
    records = get_all_records(sheet_name)
    return [r for r in records if str(r.get(field, "")) == str(value)]


def update_row(sheet_name: str, id_field: str, id_value, updates: dict):
    """อัปเดตหลายคอลัมน์ของแถวที่ id_field == id_value ด้วย API call เดียว (batch_update)
    แทนการยิงทีละ field เหมือนเดิม — ช่วยลดทั้ง read และ write quota"""
    row_idx = find_row_index(sheet_name, id_field, id_value)
    if row_idx is None:
        raise ValueError(f"ไม่พบ {id_field}={id_value} ใน {sheet_name}")
    headers = get_headers(sheet_name)
    ws = get_worksheet(sheet_name)

    batch_data = []
    for field, value in updates.items():
        if field not in headers:
            continue
        col_index = headers.index(field) + 1
        a1 = rowcol_to_a1(row_idx, col_index)
        batch_data.append({"range": a1, "values": [[value]]})

    if batch_data:
        _with_retry(ws.batch_update, batch_data, value_input_option="USER_ENTERED")

    records = _records_cache.get(sheet_name)
    if records:
        for r in records:
            if str(r.get(id_field, "")) == str(id_value):
                r.update(updates)
                break


def delete_row(sheet_name: str, id_field: str, id_value) -> bool:
    """ลบทั้งแถวที่ id_field == id_value ออกจาก sheet (ลบจริง ไม่ใช่แค่ล้างข้อความ)
    คืน True ถ้าพบและลบสำเร็จ, False ถ้าไม่พบแถวให้ลบเลย (ไม่ raise — เพราะ 'ไม่พบ = ไม่มีอะไรต้องลบ' ก็ถือว่าจบงานแล้ว)
    ถ้ามีแถวซ้ำกัน id_field เดียวกันหลายแถว ลบเฉพาะแถวแรกที่เจอเท่านั้น (เหมือน find_row_index)"""
    row_idx = find_row_index(sheet_name, id_field, id_value)
    if row_idx is None:
        return False
    ws = get_worksheet(sheet_name)
    _with_retry(ws.delete_rows, row_idx)

    records = _records_cache.get(sheet_name)
    if records:
        for i, r in enumerate(records):
            if str(r.get(id_field, "")) == str(id_value):
                del records[i]
                break
    return True


def next_id(sheet_name: str, id_field: str, prefix: str) -> str:
    """สร้างรหัสถัดไปอย่างง่าย เช่น JOB-000123 (ใช้สำหรับ prototype เท่านั้น
    งานจริงควรใช้ id generator ที่ปลอดภัยกว่านี้ เผื่อการเขียนพร้อมกัน)"""
    records = get_all_records(sheet_name)
    max_n = 0
    for r in records:
        val = str(r.get(id_field, ""))
        if val.startswith(prefix):
            try:
                n = int(val.replace(prefix, ""))
                max_n = max(max_n, n)
            except ValueError:
                pass
    return f"{prefix}{max_n + 1:06d}"


def clear_cache():
    """ล้าง cache ทั้งหมด (เผื่อกรณีมีคนอื่นแก้ sheet ตรงๆ นอกระบบ แล้วอยากบังคับอ่านใหม่ทันที
    โดยไม่ต้องรอ TTL หมดอายุ)"""
    _records_cache.clear()
    _header_cache.clear()
    _loaded_sheets.clear()
    _loaded_at.clear()


def warm_up(sheet_names: list):
    """โหลด worksheet list + ข้อมูลของหลาย sheet พร้อมกัน ด้วย API call เพียง 2 ครั้ง
    (ปกติ) แทนที่จะเปิดทีละ sheet ทีละคำสั่ง (สาเหตุหลักที่ชนโควตา 429 บ่อยตอนเริ่มโปรแกรม)
    ควรเรียกฟังก์ชันนี้ครั้งเดียว ตอนเริ่มต้นสคริปต์ ก่อนเรียกใช้งานฟังก์ชันอื่นใน sheets_client"""
    ss = get_spreadsheet()

    # 1) โหลดรายชื่อ worksheet ทั้งหมดในครั้งเดียว แทนเปิดทีละ sheet
    all_ws = _with_retry(ss.worksheets)
    for ws in all_ws:
        _ws_cache[ws.title] = ws

    # 2) โหลดข้อมูล (header+records) ของหลาย sheet พร้อมกันด้วย values_batch_get
    #    (Sheets API v4 นับเป็น 1 read request แม้จะขอหลาย range พร้อมกัน)
    existing_sheet_names = [name for name in sheet_names if name in _ws_cache]
    ranges = [f"'{name}'!A1:Z5000" for name in existing_sheet_names]
    if not ranges:
        return

    result = _with_retry(ss.values_batch_get, ranges)
    value_ranges = result.get("valueRanges", [])
    for name, vr in zip(existing_sheet_names, value_ranges):
        values = vr.get("values", [])
        if not values:
            _header_cache[name] = []
            _records_cache[name] = []
            _loaded_sheets.add(name)
            _loaded_at[name] = time.time()
            continue
        headers = values[0]
        records = []
        for row in values[1:]:
            row = row + [""] * (len(headers) - len(row))
            records.append(dict(zip(headers, row)))
        _header_cache[name] = headers
        _records_cache[name] = records
        _loaded_sheets.add(name)
        _loaded_at[name] = time.time()

    print(f"[sheets_client] warm_up: โหลดข้อมูล {len(existing_sheet_names)} sheet(s) "
          f"สำเร็จด้วย API call เพียง 2 ครั้ง")
