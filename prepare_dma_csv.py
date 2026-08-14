"""
prepare_dma_csv.py  (v2 — RTU Inlet-Only Leakage & Burst Detection Engine)
---------------------------------------------------------------------------
ตามสเปก "RTU INLET-ONLY LEAKAGE & BURST DETECTION SYSTEM ARCHITECTURE"

ทำทั้งหมดฝั่ง Python:
  1. โหลด config รายตู้ RTU จาก rtu_configs.csv (มี fallback ค่า default อัตโนมัติ)
  2. เตรียมข้อมูล 15 นาที (Q, P) — clean + rolling 30-day baseline ต่อช่วงเวลา (time-of-day)
  3. คำนวณ MNF รายวัน + Mann-Kendall trend test + CUSUM (ตามพารามิเตอร์ต่อ RTU)
  4. จำแนกเหตุการณ์ 3 กรณี: 🔴 Pipe Burst / 🟠 Developing Leak / 🟡 High Usage
  5. คำนวณ hydraulic metrics เสริม: R_eff, Leak Location Proximity, FAVAD estimate
  6. Export CSV 4 ไฟล์ให้ dashboard โหลดไปแสดงผลอย่างเดียว (ไม่คำนวณเอง)

ติดตั้ง dependency:
    pip install pandas numpy pymannkendall pyarrow

⚠️ หมายเหตุความไม่แน่นอนของสเปกที่ต้องตัดสินใจเอง (โปร่งใสไว้ตรงนี้ ไม่ใช่เดาเงียบๆ):
  - "P_inlet(t)" ในสูตร R_eff/Loss_Factor: ระบบมีจุดวัดจุดเดียว (RTU ต้นทาง) จึงตีความ
    P_inlet(t) = P_avg(t) (ค่าคาดหวังทางสถิติจาก baseline) ไม่ใช่จุดวัดที่ 2 จริง
  - FAVAD "C" ในสูตร Q_leak = C·P^N1 ไม่มีค่าคาลิเบรตมาให้ในสเปก (ต่างจาก Hazen-Williams C
    ที่เป็นคนละตัวแปรกัน) — จึงประมาณปริมาณรั่วรวมจากส่วนเกิน MNF ที่วัดได้จริง แล้วใช้ P(t)^N1
    เป็น "น้ำหนักกระจายรายชั่วโมง" เท่านั้น ไม่ใช่ค่าสัมบูรณ์จากฟิสิกส์ล้วน ๆ — ควรคาลิเบรต C
    จริงจากเหตุการณ์ท่อแตกที่ยืนยันแล้วในอดีต ถ้าต้องการความแม่นระดับ production
  - Case A ต้องมีเงื่อนไข sustained-flow เกิดร่วมกับ (CUSUM step-up เร็ว หรือ pressure drop)
    ตีความว่าใช้ AND ระหว่าง sustained-flow กับอย่างน้อยหนึ่งใน (CUSUM/Pressure) เพื่อกันสัญญาณเท็จ
"""

import numpy as np
import pandas as pd
import os
import json
import time

from datetime import datetime, timedelta

try:
    import pyarrow  # noqa: F401  — จำเป็นสำหรับ parquet caching (STEP 1) และ predicate pushdown ตอนอ่าน
    _HAS_PYARROW = True
except ImportError:
    _HAS_PYARROW = False

try:
    from tqdm import tqdm
    tqdm.pandas()  # เปิดใช้ .progress_apply() บน pandas Series/DataFrame
    _HAS_TQDM = True
except ImportError:
    # เผื่อเครื่องไม่มี tqdm ติดตั้งไว้ (หรือไม่มีเน็ตให้ pip install ตอนนั้น) — ทำงานต่อได้ปกติ
    # แค่ไม่มี progress bar สวยๆ ให้ดู (แนะนำ: pip install tqdm)
    def tqdm(iterable, **kwargs):
        desc = kwargs.get("desc", "")
        if desc:
            print(f"{desc}... (ติดตั้ง 'pip install tqdm' เพื่อดู progress bar)")
        return iterable
    _HAS_TQDM = False



# ============================================================
# CONFIG
# ============================================================
N_DAYS = 30
INTERVALS_PER_DAY = 96          # ทุก 15 นาที
INTERVAL_MIN = 15
OUTPUT_FLOW_CSV = "flow_log.csv"
OUTPUT_SERIES_CSV = "dma_daily_series.csv"
OUTPUT_SUMMARY_CSV = "dma_status_summary.csv"
RTU_CONFIG_PATH = "rtu_configs.csv"
USE_DEMO_DATA = False
FLOW_LOG_ONLY_FLAGGED = True     # true = flow_log.csv เขียนเฉพาะ DMA ที่ status เป็น watch/alert (สเกลใหญ่ๆ ไฟล์เล็กลงมาก)
                                  # false = เขียนทุก DMA เหมือนเดิม (เหมาะกับ RTU น้อยๆ ที่ยังอยากดูกราฟรายวันของ DMA ปกติได้ทุกตัว)

# --- ปรับความละเอียดของ flow_log.csv (ใช้แค่วาดกราฟใน dashboard เท่านั้น ไม่กระทบการคำนวณ
#     MNF/CUSUM/Mann-Kendall จริงใน compute_metrics() ซึ่งยังใช้ข้อมูลเต็มความละเอียดเสมอ) ---
FLOW_LOG_DAYS_KEPT = 16           # เก็บเฉพาะ N วันล่าสุด/DMA ใน flow_log.csv (dashboard ใช้จริงแค่ 14 วันแรกเป็น
                                  # baseline band + วันล่าสุด 1 วันเป็นเส้นเทียบ = 15 วัน, 16 คือมีกันชน 1 วัน)
                                  # ตั้งเป็น None หรือ 0 = ไม่ตัดวัน เก็บครบ N_DAYS เหมือนเดิม
FLOW_LOG_EXPORT_INTERVAL_MIN = 30  # ความถี่ที่ export เข้า flow_log.csv (นาที) ต้องหารด้วย INTERVAL_MIN ลงตัว
                                    # 15 = ความถี่เต็มเหมือนเดิม (ไม่ลด), 30/60 = ลดขนาดไฟล์ลงอีก 2x/4x
                                    # ช่องที่หายไปฝั่ง dashboard ถูกเติมด้วย fillGaps() ที่มีอยู่แล้ว ไม่ต้องแก้ JS

# --- ปรับความไวของตัวจับ spike ใน flow (ดู fix_spikes_boundary_safe) ---
SPIKE_THRESHOLD_RATIO = 2.0   # ค่าจริงต้องสูงกว่า median ย้อนหลังกี่เท่าถึงจะถือว่าเป็น spike ที่ต้องแก้
SPIKE_WINDOW_SIZE = 5         # จำนวนจุดย้อนหลัง (ไม่รวมจุดปัจจุบัน) ที่ใช้คำนวณ median baseline

RECOVERY_LOOKBACK_DAYS = 10    # จำนวนวันย้อนหลังที่ใช้หา "พีค MNF ล่าสุด" เพื่อเทียบว่าตอนนี้ลงมาจากพีคแค่ไหนแล้ว
RECOVERY_DROP_RATIO = 0.7      # MNF วันล่าสุดต้องต่ำกว่ากี่เท่าของพีคล่าสุดถึงจะถือว่า "กำลังฟื้น/เหตุการณ์จบแล้ว"
                                # (กันเคส flow ขึ้นแล้วลงมาแล้ว เช่นซ่อมเสร็จแล้ว ไม่ให้ CUSUM ที่มี "ความจำ"
                                # ค้างขึ้นเตือนต่อ — ใช้สัดส่วนจากพีค ไม่ใช่แค่เทียบ threshold เฉยๆ เพราะ MNF
                                # อาจยังอยู่ระหว่างลงจากพีค ยังไม่ทันต่ำกว่า threshold แต่ก็เห็นชัดว่ากำลังฟื้นแล้ว)

DEAD_RTU_ZERO_DAYS = 5   # ถ้า RTU มีค่า flow = 0 ติดต่อกันนานเกินกี่วัน ให้ถือว่า RTU เสีย/สื่อสารขาด/ไม่มีข้อมูลจริง
                          # ข้ามไม่คำนวณสถิติให้ (MNF/CUSUM/Mann-Kendall จากข้อมูลนิ่ง 0 ยาวๆ ไม่มีความหมาย
                          # และจะดึง baseline/threshold ของ DMA นั้นให้เพี้ยนไปด้วย) ตั้งเป็น None = ปิดการกรองนี้

DEFAULT_RTU_CONFIG = {
    "user_type": "RESIDENTIAL",
    "pipe_material": "PVC",
    "favad_n1": 1.15,
    # mnf_start/mnf_end ตรงนี้เป็นแค่ค่า fallback ตอนยังไม่เคยรัน detect_mnf_window.py
    # ปกติค่าจริงต่อ RTU จะถูกคำนวณอัตโนมัติ (จาก median profile 28 วัน + sliding window)
    # แล้วเขียนทับกลับเข้า rtu_configs.csv โดย job แยกต่างหาก detect_mnf_window.py (รันสัปดาห์ละครั้ง)
    "mnf_start": "02:00",
    "mnf_end": "04:00",
    "cusum_k_factor": 0.5,
    "cusum_h_factor": 4.0,
    "burst_intervals": 8,
    "q_std_mult": 2.0,
    "p_drop_bar": 0.25,
    "hard_flow_threshold": 90.0,  # m3/hr — ถ้า flow ณ จุดใดๆ เกินค่านี้ เตือนทันที ไม่ต้องรอสถิติ/สะสม
    "is_active": True,
}

DMA_DEFS = [
    {"code": "DMA-01", "name": "ราชดำริ",   "base": 78,  "seed": 11, "anomaly": None},
    {"code": "DMA-02", "name": "พระราม 4",  "base": 95,  "seed": 23, "anomaly": None},
    {"code": "DMA-03", "name": "เจริญกรุง", "base": 52,  "seed": 37, "anomaly": {"type": "gradual", "start_day": 15, "max_extra": 13}},
    {"code": "DMA-04", "name": "สุขุมวิท",  "base": 110, "seed": 41, "anomaly": None},
    {"code": "DMA-05", "name": "สีลม",      "base": 64,  "seed": 53, "anomaly": None},
    {"code": "DMA-06", "name": "ลาดพร้าว",  "base": 88,  "seed": 59, "anomaly": {"type": "step", "start_day": 27, "extra": 30}},
    {"code": "DMA-07", "name": "บางนา",     "base": 70,  "seed": 61, "anomaly": None},
    {"code": "DMA-08", "name": "ปทุมวัน",   "base": 58,  "seed": 67, "anomaly": {"type": "spike", "day": 18, "extra": 34}},
    {"code": "DMA-09", "name": "อ่อนนุช",   "base": 66,  "seed": 71, "anomaly": {"type": "step", "start_day": 25, "extra": 11}},
    {"code": "DMA-10", "name": "ทองหล่อ",   "base": 82,  "seed": 79, "anomaly": None},
]

# ตัวอย่าง override รายตู้ (สาธิตความสามารถ per-RTU customization ตามสเปกข้อ 5)
CONFIG_OVERRIDES = {
    "DMA-04": {"user_type": "INDUSTRIAL", "q_std_mult": 2.5, "burst_intervals": 12},
    "DMA-08": {"user_type": "COMMERCIAL", "q_std_mult": 2.2},
    "DMA-06": {"pipe_material": "DI", "favad_n1": 0.5},  # ท่อเหล็กเหนียว N1 ต่างจาก PVC
}

DIURNAL_24 = np.array([.55, .45, .35, .32, .34, .45, .65, .95, 1.15, 1.05, .9, .85,
                        .9, .85, .8, .8, .85, .95, 1.1, 1.2, 1.15, .95, .8, .65])
# ขยายจาก 24 จุด (รายชั่วโมง) เป็น 96 จุด (ราย 15 นาที) ด้วย interpolation
_hour_centers = np.arange(24) + 0.5
_interval_centers = np.arange(INTERVALS_PER_DAY) / 4.0
DIURNAL_96 = np.interp(_interval_centers, _hour_centers, DIURNAL_24,
                        period=24)  # wrap รอบเที่ยงคืน


def hhmm_to_interval(s: str) -> int:
    h, m = str(s).split(":")
    return (int(h) * 60 + int(m)) // INTERVAL_MIN


_step_start_time = [None]
_step_count = [0]
_step_total = 6

def step(msg: str):
    """แสดงความคืบหน้าระดับขั้นตอนหลักของ pipeline พร้อมเวลาที่ใช้ในขั้นก่อนหน้า"""
    now = time.time()
    if _step_start_time[0] is not None:
        elapsed = now - _step_start_time[0]
        print(f"      ↳ เสร็จใน {elapsed:.1f} วินาที")
    _step_count[0] += 1
    _step_start_time[0] = now
    print(f"[{_step_count[0]}/{_step_total}] {msg}")


# ============================================================
# STEP 0: RTU CONFIG — โหลดพร้อม fallback ค่า default อัตโนมัติ
# ============================================================
def load_rtu_configs(path: str, dma_defs: list) -> pd.DataFrame:
    """dma_defs: ส่ง [] (list ว่าง) เมื่อใช้ข้อมูลจริง (USE_DEMO_DATA=False) เพื่อไม่ให้ไป
    เติมแถว DMA-01..10 (รหัสตัวอย่างสำหรับโหมดจำลอง) ปนเข้าไปใน config ของ RTU จริง"""
    known_ids = [d["code"] for d in dma_defs]
    known_names = {d["code"]: d["name"] for d in dma_defs}

    if not os.path.exists(path):
        # ยังไม่มีไฟล์ config -> สร้างไฟล์ template ให้ (ทุกช่องเป็น default ยกเว้นตัวอย่าง override)
        rows = []
        for code in known_ids:
            row = {"rtu_id": code, "dma_name": known_names[code], **DEFAULT_RTU_CONFIG}
            row.update(CONFIG_OVERRIDES.get(code, {}))
            rows.append(row)
        cfg = pd.DataFrame(rows) if rows else pd.DataFrame(columns=["rtu_id", "dma_name", *DEFAULT_RTU_CONFIG.keys()])
        cfg.to_csv(path, index=False, encoding="utf-8-sig")
        print(f"ไม่พบ {path} — สร้างไฟล์ config เริ่มต้นให้แล้ว (แก้ผ่าน Excel ได้เลยรอบถัดไป)")
        return cfg.set_index("rtu_id")

    cfg = pd.read_csv(path, encoding="utf-8-sig")
    cfg = cfg.loc[:, ~cfg.columns.duplicated()]  # กันคอลัมน์ชื่อซ้ำ (เช่น เปิด/บันทึกผ่าน Excel แล้วเผลอสร้างซ้ำ)
    cfg = cfg.dropna(how="all")               # ตัดแถวว่างล้วน (เช่น แถวท้ายไฟล์ที่ Excel เผลอเซฟติดมา)
    cfg = cfg[cfg["rtu_id"].notna()]          # ตัดแถวที่ rtu_id ว่าง (กัน index เป็น NaN)
    cfg = cfg.set_index("rtu_id")
    n_before = len(cfg)
    dup_ids = cfg.index[cfg.index.duplicated()].unique().tolist()
    if dup_ids:
        print(f"⚠️  พบ rtu_id ซ้ำใน {path}: {dup_ids} — ใช้แถวล่าสุดของแต่ละ rtu_id เท่านั้น กรุณาตรวจสอบไฟล์ config")
        cfg = cfg[~cfg.index.duplicated(keep="last")]
    # เติมแถวที่ config ไม่มี (เฉพาะตอน demo — ของจริงไม่ควรมี DMA-01..10 ปนเข้ามา)
    for code in known_ids:
        if code not in cfg.index:
            row = {"dma_name": known_names[code], **DEFAULT_RTU_CONFIG}
            cfg.loc[code] = row
    # Fallback: ช่องไหน Null/NaN เติม default อัตโนมัติ (กัน crash ตามสเปกข้อ 5.2)
    for key, default_val in DEFAULT_RTU_CONFIG.items():
        if key not in cfg.columns:
            cfg[key] = default_val
        cfg[key] = cfg[key].where(cfg[key].notna(), default_val)
    cfg["is_active"] = cfg["is_active"].astype(str).str.lower().isin(["true", "1", "yes"])

    # Self-heal: เขียนไฟล์ config ที่ทำความสะอาดแล้ว (ตัดซ้ำ/แถวว่างออก) กลับลงดิสก์
    # ป้องกันไม่ให้คำเตือนเรื่องแถวซ้ำขึ้นซ้ำทุกรอบที่รัน ถ้าไม่ได้ไปแก้ไฟล์เองก่อน
    if len(cfg) != n_before or dup_ids:
        cfg.reset_index().to_csv(path, index=False, encoding="utf-8-sig")
        print(f"      ↳ บันทึก {path} เวอร์ชันที่ทำความสะอาดแล้วกลับลงดิสก์ ({n_before} → {len(cfg)} แถว)")
    return cfg


def sync_rtu_configs_with_data(cfg: pd.DataFrame, dma_codes_seen, path: str) -> pd.DataFrame:
    """เพิ่ม RTU ที่เจอในข้อมูลจริง (จาก rtu_raw_export.csv) แต่ยังไม่มีอยู่ใน rtu_configs.csv ให้อัตโนมัติ
    (self-heal อีกแบบ ต่อยอดจาก load_rtu_configs) — ตั้งค่าเริ่มต้นด้วย DEFAULT_RTU_CONFIG แล้วเขียนไฟล์
    กลับลงดิสก์ทันที เหตุผลที่ต้องทำ:
      - จะได้แก้ mnf_start/mnf_end, favad_n1, user_type ฯลฯ ของ RTU ใหม่ผ่าน Excel ได้ตั้งแต่รอบถัดไป
      - detect_mnf_window.py จะเขียนช่วง MNF ที่ตรวจจับได้กลับเข้าไปให้ RTU ตัวนี้ได้ (เดิมถ้าไม่มีแถว
        ใน config จะถูกข้ามไปเฉยๆ ไม่มีวันได้ช่วง MNF ที่ปรับตามข้อมูลจริงเลย)
    ไม่กระทบ RTU ที่มีอยู่แล้วเลย แก้ไฟล์เฉพาะตอนมี RTU ใหม่จริงๆ เท่านั้น"""
    new_codes = sorted(set(dma_codes_seen) - set(cfg.index))
    if not new_codes:
        return cfg
    for code in new_codes:
        cfg.loc[code] = {"dma_name": code, **DEFAULT_RTU_CONFIG}
    cfg.reset_index().to_csv(path, index=False, encoding="utf-8-sig")
    print(f"      ↳ พบ RTU ใหม่ {len(new_codes)} ตัวที่ยังไม่มีใน {path} — เพิ่มแถว default ให้อัตโนมัติแล้ว: {new_codes}")
    return cfg


# ==========================================================
# STEP 1: ดึงข้อมูลดิบจากแหล่งจริง — Two-layer parquet caching
#   Layer 1 (cache):  parse rtu_raw_export.csv (schema จาก VIEW_METER_HIST_RTU_U: METER_CODE/LOG_DT/
#                      F/P/F_STATUS/P_STATUS) เก็บเป็น RAW_CACHE_PARQUET แบบ "high-water mark" —
#                      รอบถัดไปอ่าน parse เฉพาะแถวใหม่ที่ต่อท้ายไฟล์ดิบเท่านั้น (เทียบจาก "จำนวนแถวที่
#                      เคย parse แล้ว" ใน sidecar .meta.json)
#                      หมายเหตุ: LOG_DT เป็น ค.ศ. อยู่แล้ว (เช่น "2026-08-03 09:00:00.000") ไม่ใช่ พ.ศ.
#                      แบบแหล่งข้อมูลเดิม จึง parse ด้วย pd.to_datetime() แบบ vectorized ได้เลย ไม่ต้อง
#                      วนทีละแถวเหมือนก่อนหน้านี้ — เร็วขึ้นมากโดยเฉพาะที่สเกล 1,800+ RTU
#   Layer 2 (query):   fetch_raw_from_rtu() อ่าน cache parquet แบบกรองด้วย predicate pushdown เฉพาะ
#                      ช่วง start-end ที่ต้องการ (pyarrow ข้าม row-group ที่ไม่เข้าเกณฑ์ได้เลย)
#                      ไม่ต้องโหลดทั้งไฟล์เข้า RAM ก่อนกรองเหมือนโค้ดเดิม
#   Pattern เดียวกับ two-layer parquet caching ที่ใช้ใน LR009-Clustering-opti-YR.py
# ==========================================================
from pathlib import Path

RAW_SOURCE_CSV = "rtu_raw_export.csv"
RAW_CACHE_PARQUET = "rtu_raw_cache.parquet"
RAW_CACHE_META = "rtu_raw_cache.meta.json"
RAW_REQUIRED_COLS = ["METER_CODE", "LOG_DT", "F", "P"]   # F_STATUS/P_STATUS เป็น optional (ดูด้านล่าง)
RAW_ERROR_FLAG = "X"                                      # ค่าใน F_STATUS/P_STATUS ที่แปลว่า "อ่านค่าไม่น่าเชื่อถือ"


def _load_cache_meta(meta_path: str) -> dict:
    if not os.path.exists(meta_path):
        return {"source_rows_parsed": 0}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"source_rows_parsed": 0}


def _save_cache_meta(meta_path: str, meta: dict):
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _parse_raw_chunk(df: pd.DataFrame) -> pd.DataFrame:
    """แปลง DataFrame ดิบ (schema จาก VIEW_METER_HIST_RTU_U: METER_CODE/LOG_DT/F/P/F_STATUS/P_STATUS)
    เป็น schema มาตรฐาน (dma_code/dma_name/timestamp/flow/pressure) ใช้ร่วมกันทั้งตอนสร้าง cache
    ครั้งแรกและตอน parse เฉพาะแถวใหม่ที่ต่อท้ายไฟล์ดิบ

    F_STATUS/P_STATUS = 'E' หมายถึงค่าที่อ่านมาไม่น่าเชื่อถือ (Error flag จากมิเตอร์/RTU) — แปลงเป็น NaN
    แล้วปล่อยให้ clean_data() interpolate แทนค่าที่ขาดหาย เหมือนวิธีจัดการค่า flow/pressure ติดลบเดิม
    """
    missing = [c for c in RAW_REQUIRED_COLS if c not in df.columns]
    if missing:
        raise KeyError(f"ไฟล์ดิบขาดคอลัมน์ที่จำเป็น: {missing} "
                        f"(คาดคอลัมน์: METER_CODE, LOG_DT, F, P และ F_STATUS/P_STATUS แบบ optional)")

    out = pd.DataFrame()
    out['dma_code'] = df['METER_CODE'].astype(str)
    out['dma_name'] = df['METER_CODE'].astype(str)

    # LOG_DT เป็น ค.ศ. อยู่แล้ว (ไม่ใช่ พ.ศ.) — parse แบบ vectorized ทีเดียวทั้งคอลัมน์ ไม่ต้อง apply ทีละแถว
    out['timestamp'] = pd.to_datetime(df['LOG_DT'], errors='coerce')

    out['flow_m3hr'] = pd.to_numeric(df['F'], errors='coerce')
    out['pressure_bar'] = pd.to_numeric(df['P'], errors='coerce')

    if 'F_STATUS' in df.columns:
        bad_flow = df['F_STATUS'].astype(str).str.strip() == RAW_ERROR_FLAG
        out.loc[bad_flow, 'flow_m3hr'] = np.nan
    if 'P_STATUS' in df.columns:
        bad_pressure = df['P_STATUS'].astype(str).str.strip() == RAW_ERROR_FLAG
        out.loc[bad_pressure, 'pressure_bar'] = np.nan

    return out


def build_or_update_raw_cache(source_csv: str = RAW_SOURCE_CSV, cache_path: str = RAW_CACHE_PARQUET,
                                meta_path: str = RAW_CACHE_META) -> None:
    """
    Layer 1: ทำให้ cache parquet (schema มาตรฐาน, dtype ถูกต้องแล้ว) ตรงกับไฟล์ดิบล่าสุด
      - ยังไม่เคยมี cache        → parse ทั้งไฟล์ (ครั้งแรกครั้งเดียว) แล้วสร้าง cache
      - มี cache แล้ว + มีแถวใหม่ → เทียบจำนวนแถวในไฟล์ดิบกับที่เคย parse ไว้ (high-water mark)
                                     parse เฉพาะแถวใหม่ที่ต่อท้าย แล้ว append เข้า cache
      - มี cache แล้ว + ไม่มีแถวใหม่ → ข้ามไปเลย ไม่แตะไฟล์ดิบ/cache
      - ไฟล์ดิบมีแถวน้อยกว่าที่เคย parse (ถูกแทนที่/ตัดทอน) → rebuild cache ใหม่ทั้งหมดพร้อมแจ้งเตือน
    ไม่คืนค่า — เขียนผลลง cache_path/meta_path เท่านั้น (fetch_raw_from_rtu() เป็นคนอ่าน cache ต่อ)
    """
    if not _HAS_PYARROW:
        raise ImportError("ต้องมี pyarrow ถึงจะใช้ parquet caching ได้ — รัน: pip install pyarrow")
    if not os.path.exists(source_csv):
        raise FileNotFoundError(f"ไม่พบไฟล์ข้อมูล: {source_csv}")

    total_rows = sum(1 for _ in open(source_csv, encoding="utf-8", errors="ignore")) - 1  # ลบ header
    meta = _load_cache_meta(meta_path)
    cached_rows = meta.get("source_rows_parsed", 0)
    cache_exists = os.path.exists(cache_path)

    if cache_exists and cached_rows > total_rows:
        print(f"      ⚠️  {source_csv} มีแถวน้อยกว่าที่เคย parse ไว้ ({total_rows:,} < {cached_rows:,}) "
              f"— ไฟล์อาจถูกแทนที่ใหม่ กำลัง rebuild cache ทั้งหมด")
        cache_exists = False
        cached_rows = 0

    if not cache_exists:
        size_mb = os.path.getsize(source_csv) / (1024 * 1024)
        print(f"      ↳ ยังไม่มี cache — parse {source_csv} ทั้งไฟล์ ({size_mb:.1f} MB, ครั้งแรกเท่านั้น)...")
        raw = pd.read_csv(source_csv)
        parsed = _parse_raw_chunk(raw)
        parsed.to_parquet(cache_path, index=False, engine="pyarrow")
        _save_cache_meta(meta_path, {"source_rows_parsed": len(raw), "source_path": source_csv})
        print(f"      ↳ สร้าง cache แล้ว: {cache_path} ({len(parsed):,} แถว)")
        return

    n_new = total_rows - cached_rows
    if n_new <= 0:
        print(f"      ↳ cache เป็นปัจจุบันแล้ว ({cached_rows:,} แถว) — ข้ามการ parse ทั้งหมด")
        return

    print(f"      ↳ พบแถวใหม่ {n_new:,} แถวต่อท้ายไฟล์ดิบ — parse เฉพาะส่วนใหม่ (ไม่ re-parse ของเก่า) แล้ว append เข้า cache")
    new_raw = pd.read_csv(source_csv, skiprows=range(1, cached_rows + 1))
    parsed_new = _parse_raw_chunk(new_raw)

    existing = pd.read_parquet(cache_path, engine="pyarrow")
    combined = pd.concat([existing, parsed_new], ignore_index=True)
    combined.to_parquet(cache_path, index=False, engine="pyarrow")
    _save_cache_meta(meta_path, {"source_rows_parsed": total_rows, "source_path": source_csv})
    print(f"      ↳ อัปเดต cache แล้ว: {cache_path} ({len(combined):,} แถวรวม, +{len(parsed_new):,} แถวใหม่)")


def fetch_raw_from_rtu(start: datetime, end: datetime) -> pd.DataFrame:
    """
    Layer 2: ทำให้ cache parquet ทันสมัยก่อน (build_or_update_raw_cache — เร็วถ้าไม่มีแถวใหม่)
    แล้วอ่านเฉพาะช่วง start-end ที่ต้องการแบบ predicate pushdown ไม่โหลดทั้งไฟล์เข้า RAM
    """
    build_or_update_raw_cache()

    filters = []
    if start:
        filters.append(("timestamp", ">=", pd.to_datetime(start)))
    if end:
        filters.append(("timestamp", "<=", pd.to_datetime(end)))

    result_df = pd.read_parquet(RAW_CACHE_PARQUET, engine="pyarrow", filters=filters or None)
    print(f"      ↳ อ่านจาก cache parquet (predicate pushdown) — ได้ {len(result_df):,} แถว ในช่วง {start} ถึง {end}")

    result_df = result_df.sort_values(["dma_code", "timestamp"]).reset_index(drop=True)
    result_df['timestamp'] = result_df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
    return result_df


# ============================================================
# STEP 1b: ข้อมูลจำลอง (Q + P ราย 15 นาที, 96 จุด/วัน)
# ============================================================
def generate_demo_data(n_days: int = N_DAYS) -> pd.DataFrame:
    end_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = end_date - timedelta(days=n_days - 1)

    rows = []
    for dma in tqdm(DMA_DEFS, desc="สร้างข้อมูลจำลอง (demo)", unit="DMA"):
        rng = np.random.default_rng(dma["seed"])
        base_pressure = 3.0 + (rng.random() - 0.5) * 0.6   # bar, สมมติฐานต้นทาง ~2.7-3.3 bar

        for d in range(n_days):
            day_date = start_date + timedelta(days=d)
            dow = day_date.weekday()
            weekend_factor = 0.9 if dow in (5, 6) else 1.0

            for t in range(INTERVALS_PER_DAY):
                extra_q = 0.0
                a = dma["anomaly"]
                if a:
                    if a["type"] == "step" and d >= a["start_day"]:
                        extra_q = a["extra"]
                    elif a["type"] == "gradual" and d >= a["start_day"]:
                        prog = min(1.0, (d - a["start_day"]) / max(1, (n_days - a["start_day"])))
                        extra_q = a["max_extra"] * prog
                    elif a["type"] == "spike" and d == a["day"]:
                        extra_q = a["extra"]

                noise_q = rng.normal(0, dma["base"] * 0.05)
                flow = max(2.0, dma["base"] * DIURNAL_96[t] * weekend_factor + extra_q + noise_q)

                # ความดัน: ลดลงตามความต้องการใช้น้ำปกติ (friction loss) + ลดเพิ่มเมื่อมี extra_q (รั่ว/burst ดึงแรงดัน)
                demand_drop = (DIURNAL_96[t] - 1.0) * 0.25          # peak ชม. ดึงแรงดันลงเล็กน้อย
                leak_drop = min(0.9, extra_q / max(dma["base"], 1) * 1.4)  # extra flow มาก -> pressure ตกมาก
                noise_p = rng.normal(0, 0.03)
                pressure = max(0.3, base_pressure - demand_drop - leak_drop + noise_p)

                rows.append({
                    "dma_code": dma["code"], "dma_name": dma["name"],
                    "timestamp": day_date + timedelta(minutes=t * INTERVAL_MIN),
                    "flow_m3hr": round(flow, 2), "pressure_bar": round(pressure, 3),
                })

    return pd.DataFrame(rows)


# ============================================================
# STEP 2: Clean + validate
# ============================================================
def _mask_isolated_zero(s: pd.Series) -> pd.Series:
    """หา flow=0 ที่เกิดขึ้นแค่ 1 จุดเดียว โดยมีค่าปกติ (ไม่ใช่ 0/NaN) คั่นอยู่ทั้งสองฝั่ง — สงสัยว่าเป็น
    sensor glitch/dropout ชั่วขณะ ไม่ใช่ช่วงที่ flow หยุดจริง (ถ้า 0 ติดกันตั้งแต่ 2 จุดขึ้นไป หรืออยู่ริมสุด
    ของช่วงข้อมูล จะไม่แตะเลย เพราะอาจเป็นเหตุการณ์จริง เช่น ปิดวาล์ว/หยุดจ่ายน้ำ) คืนค่า Series เดิมที่
    แทนที่จุดต้องสงสัยด้วย NaN ให้ interpolate() ต่อได้เหมือนกรณีค่าติดลบ"""
    is_zero = s == 0
    prev_ok = s.shift(1).notna() & (s.shift(1) != 0)
    next_ok = s.shift(-1).notna() & (s.shift(-1) != 0)
    isolated = is_zero & prev_ok & next_ok
    return s.mask(isolated)


def fix_spikes_boundary_safe(s: pd.Series, threshold_ratio: float = SPIKE_THRESHOLD_RATIO,
                              window_size: int = SPIKE_WINDOW_SIZE) -> pd.Series:
    """ตรวจจับและแก้ไข spike (ค่าพุ่งขึ้นผิดปกติแบบฉับพลัน) โดยอิง baseline ย้อนหลังอย่างเดียว (shift(1) +
    rolling median) — ไม่อ้างอิงค่าถัดไปเลย จึงไม่มีปัญหาขอบขวาสุดของข้อมูล (จุดล่าสุดที่ยังไม่มี "อนาคต"
    ให้เทียบ เช่นตอนรันแบบ near-real-time ที่ยังไม่รู้ค่าของ interval ถัดไป)
    - threshold_ratio: ค่าจริงต้องสูงกว่า baseline กี่เท่าถึงจะถือว่าเป็น spike (ปรับได้ที่ SPIKE_THRESHOLD_RATIO)
    - window_size: จำนวนจุดย้อนหลังที่ใช้หา median baseline (ปรับได้ที่ SPIKE_WINDOW_SIZE)
    หมายเหตุ: รันหลัง interpolate() เสมอ เพื่อให้ rolling median คำนวณจากข้อมูลที่ไม่มีช่องว่าง (NaN) ปนอยู่แล้ว
    """
    baseline = s.shift(1).rolling(window=window_size, min_periods=1).median()
    baseline = baseline.fillna(s)  # จุดแรกสุดของช่วงข้อมูลที่ไม่มีอดีตให้อ้างอิง ใช้ค่าตัวเองไปก่อน

    is_spike = (s >= threshold_ratio * baseline) & (baseline > 0)

    s_cleaned = s.copy()
    s_cleaned[is_spike] = baseline[is_spike]
    return s_cleaned


def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    # เดิม dropna(subset=[...,"flow_m3hr"]) ตัดทิ้งทั้งแถวถ้า flow เป็น NaN ตั้งแต่ตอน parse
    # (เช่นแถวที่ F_STATUS='E') ก่อนจะได้ interpolate เลย ทำให้ E-flag กับค่าติดลบถูกจัดการไม่เหมือนกัน
    # ตัด "flow_m3hr" ออกจาก subset นี้ ให้เหลือแค่ทิ้งแถวที่ไม่มี dma_code/timestamp จริงๆ (ใช้อะไรไม่ได้แน่ๆ)
    # ส่วน flow_m3hr ที่เป็น NaN (ไม่ว่าจะมาจาก E-flag หรือ parse ไม่ได้) จะถูก interpolate ด้านล่างเหมือนกรณีติดลบ
    df = df.dropna(subset=["dma_code", "timestamp"])
    df = df.sort_values(["dma_code", "timestamp"]).drop_duplicates(subset=["dma_code", "timestamp"], keep="last")

    if "flow_m3hr" in df.columns:
        df.loc[df["flow_m3hr"] < 0, "flow_m3hr"] = np.nan
        df["flow_m3hr"] = df.groupby("dma_code")["flow_m3hr"].transform(_mask_isolated_zero)
        df["flow_m3hr"] = df.groupby("dma_code")["flow_m3hr"].transform(lambda s: s.interpolate(limit_direction="both"))
        # spike-fix รันหลัง interpolate เสมอ — ให้ rolling median baseline คำนวณจากข้อมูลที่เติมช่องว่างแล้ว
        # (ไม่งั้นถ้ายังมี NaN ปนอยู่ตอนคำนวณ baseline อาจทำให้ threshold เพี้ยนได้)
        df["flow_m3hr"] = df.groupby("dma_code")["flow_m3hr"].transform(fix_spikes_boundary_safe)

    if "pressure_bar" in df.columns:
        df.loc[df["pressure_bar"] < 0, "pressure_bar"] = np.nan
        df["pressure_bar"] = df.groupby("dma_code")["pressure_bar"].transform(lambda s: s.interpolate(limit_direction="both"))

    return df


def downsample_for_flow_log(df: pd.DataFrame, days_kept, export_interval_min: int) -> pd.DataFrame:
    """ลดขนาดข้อมูลเฉพาะก่อน export เข้า flow_log.csv (ใช้แค่วาดกราฟใน dashboard)
    ไม่กระทบ df ต้นฉบับที่ compute_metrics() ใช้คำนวณ MNF/CUSUM/Mann-Kendall — ฟังก์ชันนี้ทำงานบนสำเนา
    แยกต่างหากที่เตรียมไว้ export เท่านั้น (main pipeline เรียกหลังคำนวณสถิติเสร็จแล้ว)

    - days_kept: เก็บเฉพาะ N วันปฏิทินล่าสุดต่อ DMA (นับจากวันข้อมูลล่าสุดของ DMA นั้น) — None/0 = ไม่ตัด
    - export_interval_min: เก็บเฉพาะจุดที่ตกในทุกๆ N นาที ต้องหารด้วย INTERVAL_MIN ลงตัว
      (15 = ไม่ลดความถี่เลย) ช่องที่หายไปฝั่ง dashboard ถูกเติมด้วย fillGaps() ที่มีอยู่แล้วในตัว JS
    """
    if df.empty:
        return df

    out = df

    if days_kept:
        day = out["timestamp"].dt.normalize()
        cutoff = out.groupby("dma_code")["timestamp"].transform("max").dt.normalize() - pd.Timedelta(days=days_kept - 1)
        out = out[day >= cutoff]

    if export_interval_min and export_interval_min != INTERVAL_MIN:
        if export_interval_min % INTERVAL_MIN != 0:
            raise ValueError(f"FLOW_LOG_EXPORT_INTERVAL_MIN ({export_interval_min}) ต้องหารด้วย INTERVAL_MIN ({INTERVAL_MIN}) ลงตัว")
        step = export_interval_min // INTERVAL_MIN
        interval_idx = (out["timestamp"].dt.hour * 60 + out["timestamp"].dt.minute) // INTERVAL_MIN
        out = out[interval_idx % step == 0]

    return out


def export_long_csv(df: pd.DataFrame, path: str):
    out = df[["dma_code", "dma_name", "timestamp", "flow_m3hr", "pressure_bar"]].copy()
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"บันทึกไฟล์แล้ว: {path}  ({len(out):,} แถว)")


# ============================================================
# STEP 3: Mann-Kendall + CUSUM (เหมือนเดิม แต่รับ k/h factor จาก config ต่อ RTU)
# ============================================================
def manual_mann_kendall(x: np.ndarray):
    n = len(x)
    s = 0
    for k in range(n - 1):
        s += np.sum(np.sign(x[k + 1:] - x[k]))
    _, counts = np.unique(x, return_counts=True)
    tie_term = np.sum(counts * (counts - 1) * (2 * counts + 5))
    var_s = (n * (n - 1) * (2 * n + 5) - tie_term) / 18
    if var_s <= 0:
        return s, 0.0
    z = (s - 1) / np.sqrt(var_s) if s > 0 else ((s + 1) / np.sqrt(var_s) if s < 0 else 0.0)
    return s, z


def mann_kendall(series: np.ndarray):
    if len(series) < 4:
        return {"S": 0, "Z": 0.0, "trend": "ข้อมูลน้อยเกินไป", "significant": False}
    try:
        import pymannkendall as pmk
        r = pmk.original_test(series)
        S, Z = r.s, r.z
    except ImportError:
        S, Z = manual_mann_kendall(np.asarray(series, dtype=float))
    significant = abs(Z) >= 1.645
    if significant:
        trend = "เพิ่มขึ้นต่อเนื่อง (มีนัยสำคัญ)" if Z > 0 else "ลดลงต่อเนื่อง (มีนัยสำคัญ)"
    else:
        trend = "มีแนวโน้มเพิ่มขึ้นเล็กน้อย (ยังไม่ชัดเจนพอ)" if Z > 0 else "ไม่มีแนวโน้มชัดเจน"
    return {"S": S, "Z": float(Z), "trend": trend, "significant": significant}


def cusum(series: np.ndarray, target: float, sd: float, k_factor: float, h_factor: float):
    k_abs, h_abs = k_factor * sd, h_factor * sd
    c_plus = 0.0
    c_plus_series = []
    breach_index = -1
    for i, v in enumerate(series):
        c_plus = max(0.0, c_plus + (v - target - k_abs))
        c_plus_series.append(c_plus)
        if breach_index == -1 and c_plus > h_abs:
            breach_index = i

    # "ความเร็วของการขยับฐาน" = จำนวนวันจากจุดที่ค่าสะสมเป็น 0 ครั้งล่าสุดก่อนตัดเกณฑ์ ถึงจุดที่ตัดเกณฑ์
    # (แยก "burst ฉับพลัน" ออกจาก "รั่วสะสมค่อยๆ ไต่" โดยไม่ขึ้นกับว่าเหตุการณ์เกิดขึ้นเมื่อไหร่เทียบกับวันนี้)
    rise_days = None
    if breach_index != -1:
        j = breach_index
        while j > 0 and c_plus_series[j - 1] > 0:
            j -= 1
        rise_days = breach_index - j + 1

    return {
        "c_plus_series": c_plus_series, "h_abs": h_abs, "breach_index": breach_index,
        "breached": breach_index != -1, "rise_days": rise_days,
    }


# ============================================================
# STEP 4: Rolling time-of-day baseline (Q_avg(t), Q_std(t), P_avg(t), P_std(t))
# ============================================================
def rolling_time_of_day_baseline(dma_df: pd.DataFrame, exclude_recent_days: int = 1):
    """dma_df ต้องเรียงตามเวลาแล้ว มีคอลัมน์ interval_idx (0..95) และ day_idx
    exclude_recent_days: จำนวนวันล่าสุดที่ไม่นำมารวมใน baseline (กันข้อมูลที่อาจปนเปื้อนจาก
    เหตุการณ์ผิดปกติที่เพิ่งเริ่มเข้าไปทำให้ baseline สูงขึ้นตามไปด้วย)"""
    piv_q = dma_df.pivot(index="day_idx", columns="interval_idx", values="flow_m3hr")
    piv_p = dma_df.pivot(index="day_idx", columns="interval_idx", values="pressure_bar")
    cutoff = max(1, min(exclude_recent_days, len(piv_q) - 3))
    q_avg, q_std = piv_q.iloc[:-cutoff].mean(), piv_q.iloc[:-cutoff].std().fillna(0)
    p_avg, p_std = piv_p.iloc[:-cutoff].mean(), piv_p.iloc[:-cutoff].std().fillna(0)
    return q_avg, q_std, p_avg, p_std, piv_q.iloc[-1], piv_p.iloc[-1]


def longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def find_dead_rtus(df: pd.DataFrame, max_zero_days) -> dict:
    """หา DMA ที่มี flow_m3hr = 0 ติดต่อกันยาวนานเกิน max_zero_days วัน — สงสัยว่า RTU เสีย/สื่อสารขาด
    (ไม่ใช่แค่ isolated zero 1 จุดที่ _mask_isolated_zero() จัดการไปแล้ว และไม่ใช่แค่ปิดวาล์วชั่วคราวไม่กี่วัน)
    คำนวณจำนวนวันจากความถี่จริงของข้อมูล (ไม่ได้นับแค่จำนวนแถวหาร 96 เฉยๆ) เผื่อบาง DMA ข้อมูลขาดหายเป็นช่วงๆ
    ทำให้จำนวน interval/วันไม่เท่ากับ 96 เป๊ะ คืนค่า dict {dma_code: จำนวนวันที่ 0 ติดต่อกันยาวที่สุดที่เจอ}
    เฉพาะตัวที่เกินเกณฑ์เท่านั้น ตั้ง max_zero_days เป็น None หรือ 0 เพื่อปิดการกรองนี้ทั้งหมด"""
    if not max_zero_days:
        return {}

    dead = {}
    for dma_code, g in df.groupby("dma_code"):
        g = g.sort_values("timestamp")
        is_zero = (g["flow_m3hr"] == 0).to_numpy()
        run_len = longest_run(is_zero)
        if run_len == 0:
            continue

        n = len(g)
        span_days = (g["timestamp"].iloc[-1] - g["timestamp"].iloc[0]).total_seconds() / 86400
        intervals_per_day_est = (n / span_days) if span_days > 0 else INTERVALS_PER_DAY
        run_days = run_len / intervals_per_day_est if intervals_per_day_est > 0 else 0

        if run_days >= max_zero_days:
            dead[dma_code] = round(run_days, 1)
    return dead


# ============================================================
# STEP 5: Hydraulic metrics — R_eff, Loss Factor (proximity), FAVAD estimate
# ============================================================
def hydraulic_metrics_for_day(q_today, p_today, q_avg, p_avg, favad_n1, daily_excess_volume):
    """
    หมายเหตุ: P_inlet(t) ตีความเป็น p_avg(t) (baseline คาดหวัง) เพราะมีจุดวัดเดียว — ดูคำอธิบายบนสุดไฟล์
    """
    delta_p = (p_avg - p_today).clip(lower=0.001)      # แรงดันตกจาก baseline
    delta_q = (q_today - q_avg).clip(lower=0.1)       # flow ส่วนเกินจาก baseline
    with np.errstate(divide="ignore", invalid="ignore"):
        r_eff = delta_p / (q_today.replace(0, np.nan) ** 2)
        loss_factor = delta_p / (delta_q.replace(0, np.nan) ** 2)
    r_eff_median = float(np.nanmedian(r_eff.replace([np.inf, -np.inf], np.nan)))
    loss_factor_median = float(np.nanmedian(loss_factor.replace([np.inf, -np.inf], np.nan)))
    if np.isnan(loss_factor_median):
        loss_factor_median = None

    # FAVAD: กระจาย daily_excess_volume (ที่วัดได้จริงจาก MNF) ตามน้ำหนัก P(t)^N1
    # แทนการใช้สัมประสิทธิ์ C ที่ไม่ได้คาลิเบรต (ดูหมายเหตุบนสุดไฟล์)
    weights = p_today.clip(lower=0.05) ** favad_n1
    weights_norm = weights / weights.sum() if weights.sum() > 0 else weights
    favad_hourly = weights_norm * daily_excess_volume

    return {
        "r_eff_median": None if np.isnan(r_eff_median) else round(r_eff_median, 5),
        "loss_factor_median": None if loss_factor_median is None else round(loss_factor_median, 5),
        "favad_volume_total": round(float(favad_hourly.sum()), 2),
    }


# ============================================================
# STEP 6: Classification — Case A (Burst) / B (Developing Leak) / C (High Usage)
# ============================================================
def classify_case(cfg_row, mnf_series, mk, cs, q_today, p_today, q_avg, q_std, p_avg,
                   still_elevated: bool, current_mnf: float, watch_threshold: float, peak_recent: float):
    burst_intervals = int(cfg_row["burst_intervals"])
    q_std_mult = float(cfg_row["q_std_mult"])
    p_drop_bar = float(cfg_row["p_drop_bar"])
    hard_flow_threshold = float(cfg_row.get("hard_flow_threshold", DEFAULT_RTU_CONFIG["hard_flow_threshold"]))

    high_flow_mask = (q_today > (q_avg + q_std_mult * q_std)).to_numpy()
    run_len = longest_run(high_flow_mask)
    sustained_flow_breach = run_len >= burst_intervals

    pressure_drop_mask = (p_today < (p_avg - p_drop_bar)).to_numpy()
    pressure_drop_confirmed = bool(np.any(high_flow_mask & pressure_drop_mask))

    post_baseline_len = len(cs["c_plus_series"])
    cusum_step_rapid = cs["breached"] and cs["rise_days"] is not None and cs["rise_days"] <= 3
    cusum_recent = cs["breached"] and (post_baseline_len - cs["breach_index"]) <= 7

    # เกณฑ์ Hard threshold — ถ้า flow ตัวใดตัวหนึ่งในวันนี้เกินค่านี้ เตือนทันที ไม่ต้องรอสถิติ/สะสม
    # (ไม่แตะเกณฑ์เดิมทั้งหมดข้างล่าง แค่ตรวจก่อนเป็นด่านแรก)
    hard_breach_mask = (q_today > hard_flow_threshold).to_numpy()
    hard_threshold_breached = bool(np.any(hard_breach_mask))

    case = "C"
    reason = "ไม่พบสัญญาณผิดปกติเชิงสถิติ (ใช้น้ำตามพฤติกรรมปกติ)"

    # เส้นทาง hard_threshold_breached, sustained_flow_breach ทั้งสองแบบ อ้างอิง q_today (flow จริงวันนี้)
    # อยู่แล้ว — ถ้าเหตุการณ์จบไปแล้ว/ซ่อมเสร็จแล้ว q_today จะกลับมาปกติเอง จึงไม่ต้องกัน still_elevated ซ้ำ
    if hard_threshold_breached:
        case = "A"
        peak_val = float(q_today[hard_breach_mask].max())
        n_hits = int(hard_breach_mask.sum())
        reason = (f"Flow เกิน Hard Threshold ({hard_flow_threshold:.0f} m³/hr) จริง — สูงสุด {peak_val:.1f} m³/hr "
                  f"({n_hits} interval) เตือนทันทีโดยไม่รอผลสถิติ")
    elif sustained_flow_breach and cusum_step_rapid:
        case = "A"
        reason = f"Flow สูงกว่าเกณฑ์ต่อเนื่อง {run_len} intervals + CUSUM ขยับฐานฉับพลันใน 1-2 วันล่าสุด" + \
                 (" + แรงดันตกยืนยัน" if pressure_drop_confirmed else "")
    elif sustained_flow_breach and pressure_drop_confirmed:
        case = "B"
        reason = (f"Flow สูงกว่าเกณฑ์ต่อเนื่อง {run_len} intervals พร้อมแรงดันตก แต่ไม่ใช่การเปลี่ยนแปลงฉับพลันใน 1-2 วัน "
                   f"(น่าจะเป็นรั่วที่ดำเนินมาระยะหนึ่งแล้วและขยายจนคงที่) — ควรตรวจสอบโดยเร็วแต่ไม่ใช่เหตุฉุกเฉินแบบท่อแตกใหม่")
    elif mk["significant"] or (cs["breached"] and cs["c_plus_series"][-1] > 0):
        # เส้นทางนี้เส้นเดียวที่ไม่ได้อิง q_today เลย (มองแค่ trend/ผลสะสมย้อนหลัง) — CUSUM มี "ความจำ" คือ
        # c_plus ลดช้ากว่าที่ขึ้น ต่อให้ MNF กำลังลงจากพีคแล้วจริงๆ c_plus อาจยังค้างเป็นบวกอยู่อีกหลายวัน
        # จึงต้องเช็คเพิ่มว่า MNF ล่าสุดยังค้างใกล้พีค (ไม่ได้ลงมาต่ำกว่า RECOVERY_DROP_RATIO ของพีค) จริงไหม
        # ก่อนขึ้นเตือน — ถ้าลงมาแล้ว (เช่นซ่อมเสร็จแล้ว/เหตุการณ์จบไปแล้ว) ให้ถือว่าเป็น case ปกติแทน
        if still_elevated:
            case = "B"
            reason = "Mann-Kendall ยืนยันแนวโน้มขึ้นต่อเนื่อง" if mk["significant"] else "CUSUM สะสมเป็นบวกต่อเนื่อง (ยังไม่ Step-up ฉับพลัน)"
        else:
            reason = (f"เคยมีสัญญาณผิดปกติ (Mann-Kendall/CUSUM สะสม) แต่ MNF ล่าสุด ({current_mnf:.1f}) ลดลงมาต่ำกว่า "
                      f"{RECOVERY_DROP_RATIO*100:.0f}% ของพีคล่าสุด ({peak_recent:.1f}) แล้ว — น่าจะเป็นเหตุการณ์ที่จบไปแล้ว/"
                      f"ซ่อมเสร็จแล้ว จึงไม่ขึ้นเตือน")

    return {
        "case": case, "reason": reason, "run_len_intervals": run_len,
        "sustained_flow_breach": sustained_flow_breach,
        "pressure_drop_confirmed": pressure_drop_confirmed,
        "cusum_step_rapid": cusum_step_rapid, "cusum_recent": cusum_recent,
        "hard_threshold_breached": hard_threshold_breached,
        "still_elevated": still_elevated,
    }


# ============================================================
# MAIN PIPELINE
# ============================================================
def compute_metrics(df: pd.DataFrame, rtu_cfg: pd.DataFrame):
    df = df.copy()
    df["date"] = df["timestamp"].dt.date
    df["interval_idx"] = (df["timestamp"].dt.hour * 60 + df["timestamp"].dt.minute) // INTERVAL_MIN

    daily_rows, summary_rows = [], []

    dma_codes = df["dma_code"].unique()
    for dma_code, g in tqdm(df.groupby("dma_code"), total=len(dma_codes),
                             desc="คำนวณสถิติต่อ DMA (MNF/Mann-Kendall/CUSUM/hydraulic)", unit="DMA"):
        cfg_row = rtu_cfg.loc[dma_code] if dma_code in rtu_cfg.index else {**DEFAULT_RTU_CONFIG, "dma_name": dma_code}
        if isinstance(cfg_row, pd.DataFrame):
            # เผื่อหลุดมาถึงตรงนี้ (rtu_id ซ้ำ) — กันไม่ให้ crash โดยใช้แถวแรกไปก่อน
            cfg_row = cfg_row.iloc[0]
        if not bool(cfg_row.get("is_active", True)):
            continue  # RTU ปิดการเตือนตาม config (is_active = FALSE)

        g = g.sort_values("timestamp").reset_index(drop=True)
        day_map = {d: i for i, d in enumerate(sorted(g["date"].unique()))}
        g["day_idx"] = g["date"].map(day_map)
        n_days = len(day_map)
        if n_days < 4:
            continue

        dma_name = g["dma_name"].iloc[0]
        mnf_start_t = hhmm_to_interval(cfg_row["mnf_start"])
        mnf_end_t = hhmm_to_interval(cfg_row["mnf_end"])
        night = g[(g["interval_idx"] >= mnf_start_t) & (g["interval_idx"] < mnf_end_t)]
        mnf_by_day = night.groupby("day_idx")["flow_m3hr"].mean().reindex(range(n_days))
        total_by_day = g.groupby("day_idx")["flow_m3hr"].sum().reindex(range(n_days)) * (INTERVAL_MIN / 60)

        mnf_series = mnf_by_day.to_numpy()
        base_win_len = max(3, min(14, n_days - 3))
        baseline = mnf_series[:base_win_len]
        mean = float(np.nanmean(baseline))
        sd = float(np.nanstd(baseline)) or mean * 0.05
        threshold = mean + 2 * sd
        watch_threshold = mean + 1 * sd

        last5 = mnf_series[-5:]
        days_over_alert = int(np.sum(last5 > threshold))

        # เช็คว่า MNF "ยังค้างใกล้พีคอยู่จริง" ไหม (ไม่ใช่แค่เทียบ threshold เฉยๆ) — ใช้กันเคส flow ขึ้นแล้ว
        # ลงมาแล้ว (ซ่อมเสร็จแล้ว/เหตุการณ์จบไปแล้ว) ไม่ให้ CUSUM ที่มี "ความจำ" (c_plus ลดช้ากว่าที่ขึ้น) ค้าง
        # ขึ้นเตือนต่อ ทั้งที่ MNF กำลังลงจากพีคแล้วจริงๆ แม้จะยังไม่ทันต่ำกว่า threshold ก็ตาม
        recent_window = mnf_series[-min(n_days, RECOVERY_LOOKBACK_DAYS):]
        peak_recent = float(np.nanmax(recent_window))
        current_mnf = float(mnf_series[-1])
        declining_from_peak = peak_recent > 0 and current_mnf < peak_recent * RECOVERY_DROP_RATIO
        still_elevated = (current_mnf > watch_threshold) and not declining_from_peak

        mk = mann_kendall(mnf_series[-min(10, n_days):])
        post_baseline = mnf_series[base_win_len:]
        cs = cusum(post_baseline, mean, sd, float(cfg_row["cusum_k_factor"]), float(cfg_row["cusum_h_factor"]))
        cusum_recent_breach = cs["breached"] and (len(post_baseline) - cs["breach_index"]) <= 7

        #==============================================================================================
        DEBUG_DMA_CODE = "DM-16-04-05-01"
        if dma_code == DEBUG_DMA_CODE:
            print(f"\n{'='*70}\n🔍 DEBUG MNF: {dma_code}\n{'='*70}")
            print(f"mnf_start/mnf_end (config) = {cfg_row['mnf_start']} - {cfg_row['mnf_end']}  "
                  f"(interval_idx {mnf_start_t}-{mnf_end_t})")
            print(f"n_days = {n_days}, base_win_len = {base_win_len}")
            print(f"\n-- night rows (แถวที่ตกในช่วง MNF window ที่ใช้คำนวณ) --")
            print(night[["date", "interval_idx", "flow_m3hr"]].to_string(index=False))
            print(f"\n-- mnf_by_day (ค่าเฉลี่ย flow ช่วงกลางคืนต่อวัน) --")
            print(mnf_by_day.to_string())
            print(f"\nmnf_series = {mnf_series}")
            print(f"baseline (n={base_win_len} วันแรก) = {baseline}")
            print(f"mean={mean:.3f}  sd={sd:.3f}  threshold={threshold:.3f}  watch_threshold={watch_threshold:.3f}")
            print(f"current_mnf (mnf_series[-1]) = {mnf_series[-1]:.3f}")
            print(f"days_over_alert (last5 > threshold) = {days_over_alert}")
            print(f"mann_kendall = {mk}")
            print(f"cusum = {cs}")
            print(f"{'='*70}\n")
        #==============================================================================================

        # rolling time-of-day baseline + วันล่าสุดราย interval (สำหรับ burst detection + hydraulic metrics)
        # กันข้อมูล baseline ปนเปื้อนจากเหตุการณ์ที่เพิ่งเริ่ม โดยตัดวันล่าสุดออกเท่ากับช่วง post-baseline ที่มี
        exclude_recent = max(1, n_days - base_win_len)
        q_avg, q_std, p_avg, p_std, q_today, p_today = rolling_time_of_day_baseline(g, exclude_recent_days=exclude_recent)
        q_today = q_today.reindex(range(INTERVALS_PER_DAY)).interpolate(limit_direction="both")
        p_today = p_today.reindex(range(INTERVALS_PER_DAY)).interpolate(limit_direction="both")
        q_avg = q_avg.reindex(range(INTERVALS_PER_DAY)).interpolate(limit_direction="both")
        q_std = q_std.reindex(range(INTERVALS_PER_DAY)).fillna(0)
        p_avg = p_avg.reindex(range(INTERVALS_PER_DAY)).interpolate(limit_direction="both")

        classification = classify_case(cfg_row, mnf_series, mk, cs, q_today, p_today, q_avg, q_std, p_avg,
                                        still_elevated, current_mnf, watch_threshold, peak_recent)

        excess_per_hour = max(0.0, float(np.mean(mnf_series[-3:])) - mean)
        est_loss_per_day = excess_per_hour * 24
        hyd = hydraulic_metrics_for_day(q_today, p_today, q_avg, p_avg, float(cfg_row["favad_n1"]), est_loss_per_day)

        status = {"A": "alert", "B": "watch", "C": "ok"}[classification["case"]]

        for i in range(n_days):
            cplus = cs["c_plus_series"][i - base_win_len] if i >= base_win_len else None
            daily_rows.append({
                "dma_code": dma_code,
                "date": sorted(day_map, key=day_map.get)[i],
                "mnf": None if np.isnan(mnf_series[i]) else round(float(mnf_series[i]), 3),
                "daily_total": None if np.isnan(total_by_day.iloc[i]) else round(float(total_by_day.iloc[i]), 2),
                "cusum_cplus": round(cplus, 3) if cplus is not None else "",
            })

        summary_rows.append({
            "dma_code": dma_code, "dma_name": dma_name,
            "case": classification["case"], "status": status, "case_reason": classification["reason"],
            "current_mnf": round(float(mnf_series[-1]), 3), "baseline_mean": round(mean, 3),
            "baseline_sd": round(sd, 3), "threshold": round(threshold, 3), "watch_threshold": round(watch_threshold, 3),
            "mk_z": round(mk["Z"], 3), "mk_trend": mk["trend"], "mk_significant": mk["significant"],
            "cusum_h": round(cs["h_abs"], 3), "cusum_breached": cs["breached"],
            "cusum_recent_breach": cusum_recent_breach, "cusum_step_rapid": classification["cusum_step_rapid"],
            "sustained_flow_breach": classification["sustained_flow_breach"],
            "sustained_run_intervals": classification["run_len_intervals"],
            "pressure_drop_confirmed": classification["pressure_drop_confirmed"],
            "hard_threshold_breached": classification["hard_threshold_breached"],
            "excess_per_hour": round(excess_per_hour, 3), "est_loss_per_day": round(est_loss_per_day, 2),
            "base_win_len": base_win_len,
            "r_eff_median": hyd["r_eff_median"], "loss_factor_median": hyd["loss_factor_median"],
            "proximity_estimate": None, "favad_volume_total": hyd["favad_volume_total"],
            "user_type": cfg_row.get("user_type", "RESIDENTIAL"), "pipe_material": cfg_row.get("pipe_material", "PVC"),
        })

    daily_df = pd.DataFrame(daily_rows)
    summary_df = pd.DataFrame(summary_rows)
    summary_df = classify_proximity_relative(summary_df)
    return daily_df, summary_df


def classify_proximity_relative(summary_df: pd.DataFrame) -> pd.DataFrame:
    """
    จัดประเภท "ใกล้/ไกล RTU" แบบเทียบสัมพัทธ์ระหว่าง DMA ที่เกิดเหตุ (Case A/B)
    """
    active = summary_df[summary_df["case"].isin(["A", "B"]) & summary_df["loss_factor_median"].notna()]
    if len(active) < 2:
        return summary_df
    median_lf = active["loss_factor_median"].median()

    def label(row):
        if row["case"] not in ("A", "B") or pd.isna(row["loss_factor_median"]):
            return None
        
        # 🟢 แก้ไข: Loss Factor สูง = แรงดันตกมาก = อยู่ไกล (ปลายสาย)
        #          Loss Factor ต่ำ = แรงดันตกน้อย = อยู่ใกล้ RTU (ต้นสาย)
        return ("ไกลจาก RTU สัมพัทธ์ / ปลายสาย (> 2 กม. โดยประมาณ)" if row["loss_factor_median"] > median_lf
                else "ใกล้ RTU สัมพัทธ์ (< 2 กม. โดยประมาณ)")

    summary_df["proximity_estimate"] = summary_df.apply(label, axis=1)
    return summary_df


if __name__ == "__main__":
    _pipeline_start = time.time()
    end = datetime.now()
    start = end - timedelta(days=N_DAYS)

    step("โหลด config รายตู้ RTU (rtu_configs.csv)")
    rtu_cfg = load_rtu_configs(RTU_CONFIG_PATH, DMA_DEFS if USE_DEMO_DATA else [])

    step("ดึงข้อมูลดิบ (flow_log.csv จริง หรือข้อมูลจำลอง)")
    if USE_DEMO_DATA:
        raw = generate_demo_data(N_DAYS)
    else:
        raw = fetch_raw_from_rtu(start, end)
    print(f"      ↳ อ่านมาได้ {len(raw):,} แถว")

    step("ทำความสะอาดข้อมูล (clean + interpolate ค่าที่ขาดหาย)")
    cleaned = clean_data(raw)

    rtu_cfg = sync_rtu_configs_with_data(rtu_cfg, cleaned["dma_code"].unique(), RTU_CONFIG_PATH)

    dead_rtus = find_dead_rtus(cleaned, DEAD_RTU_ZERO_DAYS)
    if dead_rtus:
        print(f"      ⚠️  พบ {len(dead_rtus)} RTU ที่ flow=0 ติดต่อกันเกิน {DEAD_RTU_ZERO_DAYS} วัน "
              f"(สงสัย RTU เสีย/สื่อสารขาด) — ข้ามไม่คำนวณสถิติให้: {dead_rtus}")
        cleaned_for_metrics = cleaned[~cleaned["dma_code"].isin(dead_rtus.keys())]
    else:
        cleaned_for_metrics = cleaned

    step("คำนวณสถิติต่อ DMA (นี่คือขั้นตอนหลัก อาจใช้เวลาถ้ามีหลาย RTU)")
    daily_series, status_summary = compute_metrics(cleaned_for_metrics, rtu_cfg)

    if dead_rtus:
        # เพิ่มแถวขั้นต่ำให้ RTU ที่ถูกตัดออกจาก compute_metrics ไว้ใน status_summary ด้วย สถานะ "no_data"
        # เพื่อให้ dashboard ยังเห็นว่า RTU นี้ "ไม่มีข้อมูล" อยู่ ไม่ใช่หายไปเงียบๆ (คนละความหมายกับ "ปกติ")
        # คอลัมน์สถิติอื่นๆ ปล่อยว่างไว้ตั้งใจ เพราะไม่มีข้อมูลจริงให้คำนวณ ไม่ใช่ลืมใส่
        no_data_rows = pd.DataFrame([{
            "dma_code": code, "dma_name": code, "case": "N", "status": "no_data",
            "case_reason": f"flow=0 ติดต่อกัน ~{days:.1f} วัน (เกินเกณฑ์ {DEAD_RTU_ZERO_DAYS} วัน) สงสัย RTU เสีย/สื่อสารขาด — ไม่ได้คำนวณสถิติให้",
            "no_data_days": days,
            "user_type": rtu_cfg.loc[code, "user_type"] if code in rtu_cfg.index else DEFAULT_RTU_CONFIG["user_type"],
            "pipe_material": rtu_cfg.loc[code, "pipe_material"] if code in rtu_cfg.index else DEFAULT_RTU_CONFIG["pipe_material"],
        } for code, days in dead_rtus.items()])
        status_summary = pd.concat([status_summary, no_data_rows], ignore_index=True)
        print(f"      ↳ เพิ่มสถานะ \"no_data\" ให้ {len(dead_rtus)} RTU ที่ถูกตัดไว้ใน dma_status_summary.csv "
              f"(ยังเห็นบน dashboard ว่าไม่มีข้อมูล ไม่ได้หายไปเงียบๆ)")

    step("บันทึกไฟล์ดิบ (flow_log.csv)")
    if FLOW_LOG_ONLY_FLAGGED:
        flagged_codes = set(status_summary.loc[status_summary["status"].isin(["watch", "alert"]), "dma_code"])
        flow_export_df = cleaned[cleaned["dma_code"].isin(flagged_codes)]
        n_total = cleaned["dma_code"].nunique()
        print(f"      ↳ FLOW_LOG_ONLY_FLAGGED=True: ส่งออกเฉพาะ {len(flagged_codes)}/{n_total} DMA ที่สถานะ watch/alert "
              f"(DMA ที่เหลือยังดู MNF/CUSUM ได้ปกติจาก dma_daily_series.csv แค่กราฟรายวัน 15 นาทีจะไม่มีข้อมูล)")
    else:
        flow_export_df = cleaned
    n_before = len(flow_export_df)
    flow_export_df = downsample_for_flow_log(flow_export_df, FLOW_LOG_DAYS_KEPT, FLOW_LOG_EXPORT_INTERVAL_MIN)
    if len(flow_export_df) != n_before:
        print(f"      ↳ downsample_for_flow_log: {n_before:,} → {len(flow_export_df):,} แถว "
              f"(days_kept={FLOW_LOG_DAYS_KEPT or 'ไม่ตัด'}, export_interval_min={FLOW_LOG_EXPORT_INTERVAL_MIN})")
    export_long_csv(flow_export_df, OUTPUT_FLOW_CSV)

    step("บันทึกไฟล์ผลลัพธ์ (dma_daily_series.csv, dma_status_summary.csv)")
    daily_series.to_csv(OUTPUT_SERIES_CSV, index=False, encoding="utf-8-sig")
    status_summary.to_csv(OUTPUT_SUMMARY_CSV, index=False, encoding="utf-8-sig")
    print(f"      ↳ เสร็จใน {time.time() - _step_start_time[0]:.1f} วินาที")

    print(f"บันทึกไฟล์แล้ว: {OUTPUT_SERIES_CSV}   ({len(daily_series):,} แถว)")
    print(f"บันทึกไฟล์แล้ว: {OUTPUT_SUMMARY_CSV} ({len(status_summary):,} แถว)")

    for case, label in [("A", "🔴 PIPE BURST"), ("B", "🟠 DEVELOPING LEAK")]:
        hits = status_summary[status_summary.case == case]["dma_code"].tolist()
        if hits:
            print(f"{label}: {', '.join(hits)}")

    total_elapsed = time.time() - _pipeline_start
    print(f"\n✅ รันเสร็จทั้งหมดใน {total_elapsed:.1f} วินาที ({total_elapsed/60:.1f} นาที)")