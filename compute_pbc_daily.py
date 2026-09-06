# -*- coding: utf-8 -*-
"""
compute_pbc_daily.py — คำนวณ MNF และกราฟรายวันของ DMA ในสัญญา PBC

ขอบเขต
  ผลลัพธ์ของสคริปต์นี้ใช้ "เฝ้าระวัง" เท่านั้น
  ปริมาณน้ำเข้าที่ใช้ประเมินผลตามสัญญามาจากรายงาน WLMA AN/WB220 อย่างเดียว
  ตัวเลขจากสคริปต์นี้ไม่เคยถูกนำไปคิดปริมาณน้ำเข้าหรืออัตราน้ำสูญเสีย

สิ่งที่ทำ
  1) อ่าน rtu_hist_cache.parquet (read-only ไม่แตะไฟล์เดิม)
  2) รวม flow ของทุกมาตรน้ำเข้าใน DMA เดียวกันก่อน แล้วจึงหา MNF
     (มาตรทิศทาง O จะถูกหักลบ — ปัจจุบันสัญญานี้ไม่มี แต่รองรับไว้)
  3) หา MNF รายวันแบบ dynamic: ค่าเฉลี่ยเคลื่อนที่ 2 ชม. ที่ต่ำที่สุดในช่วงกลางคืน
  4) สร้างแถบ min-max 96 ช่วง/วัน สำหรับกราฟ popup
  5) คำนวณพื้นค่า MNF (เปอร์เซ็นไทล์ที่ 10) เพื่อใช้เป็นน้ำหนักกระจายเป้า
  6) คัดลอกไฟล์ไป static\\data แล้ว git push (ถ้าเปิดใช้)

รัน: python compute_pbc_daily.py
"""

import argparse
import csv
import os
import shutil
import subprocess
import sys
from datetime import datetime

import numpy as np
import pandas as pd

# ==================================================================== CONFIG

# ไฟล์ parquet ของระบบเดิม — อ่านอย่างเดียว ไม่เขียนทับ
RTU_PARQUET = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\rtu_hist_cache.parquet"

# ผังมาตรต่อ DMA: อ่านจาก CSV ที่ export จาก Sheet DMAMeterMap
# (คอลัมน์: dma_code, rtu_id, direction)
METER_MAP_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "config", "dma_meter_map.csv")

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

# ปลายทางที่เว็บอ่านจริง
STATIC_DIR = r"C:\NRWjob\static\data"
COPY_TO_STATIC = True

# git push อัตโนมัติหลังสร้างไฟล์เสร็จ
GIT_REPO_DIR = r"C:\NRWjob"
GIT_AUTO_PUSH = False          # เปิดเมื่อพร้อม deploy อัตโนมัติ
GIT_COMMIT_MESSAGE = "update PBC daily data"

# ช่วงเวลากลางคืนที่ใช้หา MNF (ชั่วโมง)
NIGHT_START_HOUR = 0
NIGHT_END_HOUR = 5

# ความยาวหน้าต่าง MNF (นาที)
MNF_WINDOW_MINUTES = 120

# จำนวนวันย้อนหลังที่ประมวลผล
LOOKBACK_DAYS = 120

# ช่วงเวลาที่ใช้เป็น "ค่าล่าสุด" ของกราฟและ MNF ปัจจุบัน (วัน)
RECENT_DAYS = 7

# ช่วงเวลาที่ใช้หาพื้นค่า MNF
FLOOR_PERCENTILE = 10

# เกณฑ์วันข้อมูลคุณภาพดีสำหรับงาน PBC (เข้มกว่าของ dashboard ทั่วไป
# เพราะตัวเลขชุดนี้ใช้ประกอบการประเมินผลตามสัญญา ข้อ 2.1.9)
GOOD_DAY_MIN_RATIO = 0.90      # ต้องมีข้อมูลอย่างน้อย 90% ของช่วงเวลาในวันนั้น

INTERVALS_PER_DAY = 96         # ราย 15 นาที

# ชื่อคอลัมน์ที่เป็นไปได้ใน parquet — สคริปต์จะหาให้เองจากรายการนี้
COL_CANDIDATES = {
    "rtu": ["rtu_id", "RTU_ID", "dm_code", "DM_CODE", "meter_id", "METER_ID",
            "device_id", "rtu", "AREA_CODE_RTU"],
    "time": ["datetime", "timestamp", "READ_DT", "read_dt", "ts", "DATETIME",
             "record_time", "F_DATETIME"],
    "flow": ["flow", "flow_rate", "F_FLOW", "flow_m3h", "FLOW", "value_flow"],
    "pressure": ["pressure_bar", "pressure", "F_PRESSURE", "PRESSURE",
                 "pressure_m", "value_pressure"],
    "status": ["F_STATUS", "status", "STATUS", "quality"],
}

# ==================================================================== helpers


def log(message):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), message))


def pick_column(df, kind, required=True):
    for name in COL_CANDIDATES[kind]:
        if name in df.columns:
            return name
    if required:
        raise SystemExit(
            "หาคอลัมน์ %s ไม่เจอใน parquet — คอลัมน์ที่มี: %s\n"
            "แก้รายการ COL_CANDIDATES ที่หัวไฟล์ให้ตรงกับข้อมูลจริง"
            % (kind, list(df.columns))
        )
    return None


def load_meter_map(path):
    """คืน dict {rtu_id: (dma_code, sign)} — sign = +1 น้ำเข้า, -1 น้ำออก"""
    if not os.path.exists(path):
        raise SystemExit(
            "ไม่พบไฟล์ผังมาตร %s\n"
            "ให้ export tab DMAMeterMap จาก Google Sheet NRW_PBC "
            "เป็น CSV แล้ววางไว้ที่ตำแหน่งนี้ "
            "(คอลัมน์: dma_code, rtu_id, direction)" % path
        )
    mapping = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            rtu = (row.get("rtu_id") or "").strip()
            dma = (row.get("dma_code") or "").strip()
            if not rtu or not dma:
                continue
            direction = (row.get("direction") or "I").strip().upper()
            mapping[rtu] = (dma, -1.0 if direction == "O" else 1.0)
    if not mapping:
        raise SystemExit("ไฟล์ผังมาตรว่างเปล่า: %s" % path)
    return mapping


def interval_index(series):
    """แปลงเวลาเป็นลำดับช่วง 15 นาทีในหนึ่งวัน (0-95)"""
    return (series.dt.hour * 4 + series.dt.minute // 15).astype(int)


# ==================================================================== core


def load_rtu_data(parquet_path, meter_map):
    log("อ่าน %s" % parquet_path)
    if not os.path.exists(parquet_path):
        raise SystemExit("ไม่พบไฟล์ parquet: %s" % parquet_path)

    df = pd.read_parquet(parquet_path)
    col_rtu = pick_column(df, "rtu")
    col_time = pick_column(df, "time")
    col_flow = pick_column(df, "flow")
    col_pressure = pick_column(df, "pressure", required=False)
    col_status = pick_column(df, "status", required=False)
    log("คอลัมน์ที่ใช้: rtu=%s time=%s flow=%s pressure=%s status=%s"
        % (col_rtu, col_time, col_flow, col_pressure, col_status))

    df = df[df[col_rtu].astype(str).str.strip().isin(meter_map.keys())].copy()
    if df.empty:
        raise SystemExit(
            "ไม่พบข้อมูลของมาตรใดในผังเลย — ตรวจว่ารหัส RTU ใน parquet "
            "ใช้รูปแบบเดียวกับใน DMAMeterMap หรือไม่"
        )

    df["rtu_id"] = df[col_rtu].astype(str).str.strip()
    df["ts"] = pd.to_datetime(df[col_time], errors="coerce")
    df["flow"] = pd.to_numeric(df[col_flow], errors="coerce")
    df["pressure"] = (
        pd.to_numeric(df[col_pressure], errors="coerce")
        if col_pressure else np.nan
    )

    # ตัดแถวที่ระบบแจ้งว่าค่าอ่านเสีย
    if col_status:
        bad = df[col_status].astype(str).str.upper().isin(["E", "X"])
        n_bad = int(bad.sum())
        df = df[~bad]
        log("ตัดแถวที่ F_STATUS เป็น E/X ออก %d แถว" % n_bad)

    df = df.dropna(subset=["ts", "flow"])
    cutoff = df["ts"].max().normalize() - pd.Timedelta(days=LOOKBACK_DAYS)
    df = df[df["ts"] >= cutoff]

    df["dma_code"] = df["rtu_id"].map(lambda r: meter_map[r][0])
    df["sign"] = df["rtu_id"].map(lambda r: meter_map[r][1])
    df["flow_signed"] = df["flow"] * df["sign"]
    df["date"] = df["ts"].dt.normalize()
    df["interval_idx"] = interval_index(df["ts"])

    log("ข้อมูลที่ใช้: %d แถว, %d มาตร, %d DMA, %s ถึง %s"
        % (len(df), df["rtu_id"].nunique(), df["dma_code"].nunique(),
           df["ts"].min().date(), df["ts"].max().date()))
    return df


def combine_meters(df):
    """
    รวมมาตรทุกตัวใน DMA เดียวกันเป็นเส้นเดียวก่อนหา MNF
    รวมที่ระดับ (dma, วันที่, ช่วง 15 นาที) เพื่อให้มาตรที่ cadence ต่างกันมาตรงกัน
    """
    grouped = df.groupby(["dma_code", "date", "interval_idx"]).agg(
        flow=("flow_signed", "sum"),
        pressure=("pressure", "mean"),
        n_meters=("rtu_id", "nunique"),
    ).reset_index()

    # ช่วงที่มาตรมาไม่ครบ ถือว่าผลรวมยังไม่สมบูรณ์ ตัดทิ้งเพื่อไม่ให้ MNF ต่ำผิด
    expected = df.groupby("dma_code")["rtu_id"].nunique()
    grouped["expected_meters"] = grouped["dma_code"].map(expected)
    incomplete = grouped["n_meters"] < grouped["expected_meters"]
    if incomplete.any():
        log("ตัดช่วงที่มาตรมาไม่ครบออก %d ช่วง (จาก %d)"
            % (int(incomplete.sum()), len(grouped)))
    grouped = grouped[~incomplete]
    return grouped


def daily_mnf(combined):
    """
    หา MNF รายวันต่อ DMA: ค่าเฉลี่ยเคลื่อนที่ 2 ชม. ที่ต่ำที่สุดในช่วงกลางคืน
    ทำแบบ dynamic ต่อวัน ไม่ตรึงหน้าต่างตายตัว
    """
    window = max(int(MNF_WINDOW_MINUTES / 15), 1)
    night_lo = NIGHT_START_HOUR * 4
    night_hi = NIGHT_END_HOUR * 4

    records = []
    for (dma, date), group in combined.groupby(["dma_code", "date"]):
        group = group.sort_values("interval_idx")
        n_points = len(group)

        night = group[
            (group["interval_idx"] >= night_lo) & (group["interval_idx"] < night_hi)
        ]
        mnf = None
        mnf_interval = None
        if len(night) >= window:
            rolling = night["flow"].rolling(window=window, min_periods=window).mean()
            if rolling.notna().any():
                pos = int(rolling.idxmin())
                mnf = float(rolling.min())
                mnf_interval = int(night.loc[pos, "interval_idx"]) - window + 1

        records.append({
            "dma_code": dma,
            "date": date.strftime("%Y-%m-%d"),
            "mnf": round(mnf, 3) if mnf is not None else "",
            "mnf_start_interval": mnf_interval if mnf_interval is not None else "",
            "avg_flow": round(float(group["flow"].mean()), 3),
            "avg_pressure": (
                round(float(group["pressure"].mean()), 3)
                if group["pressure"].notna().any() else ""
            ),
            "n_points": n_points,
            "is_good_day": int(n_points >= INTERVALS_PER_DAY * GOOD_DAY_MIN_RATIO),
        })

    out = pd.DataFrame(records)
    if not out.empty:
        out = out.sort_values(["dma_code", "date"])
    return out


def build_envelope(combined, recent_days):
    """
    แถบ min-max ต่อช่วง 15 นาที จากข้อมูลทั้งช่วง
    พร้อมค่าเฉลี่ยของ RECENT_DAYS วันล่าสุดเป็นเส้นทึบ
    รูปแบบเดียวกับกราฟใน monitoring.html
    """
    if combined.empty:
        return pd.DataFrame()

    last_date = combined["date"].max()
    recent_cut = last_date - pd.Timedelta(days=recent_days - 1)
    recent = combined[combined["date"] >= recent_cut]

    stats = combined.groupby(["dma_code", "interval_idx"]).agg(
        f_min=("flow", "min"), f_max=("flow", "max"),
        p_min=("pressure", "min"), p_max=("pressure", "max"),
    ).reset_index()

    latest = recent.groupby(["dma_code", "interval_idx"]).agg(
        f_latest=("flow", "mean"), p_latest=("pressure", "mean"),
    ).reset_index()

    merged = stats.merge(latest, on=["dma_code", "interval_idx"], how="left")

    # เติมช่วงที่ไม่มีข้อมูลให้ครบ 96 ช่วง เพื่อให้กราฟไม่ขาดตอน
    frames = []
    for dma, group in merged.groupby("dma_code"):
        full = pd.DataFrame({"interval_idx": range(INTERVALS_PER_DAY)})
        full["dma_code"] = dma
        merged_full = full.merge(
            group.drop(columns=["dma_code"]), on="interval_idx", how="left"
        )
        for col in ["f_min", "f_max", "f_latest", "p_min", "p_max", "p_latest"]:
            merged_full[col] = merged_full[col].interpolate(limit_area="inside")
        frames.append(merged_full)

    out = pd.concat(frames, ignore_index=True)
    out = out[["dma_code", "interval_idx", "f_min", "f_max", "f_latest",
               "p_min", "p_max", "p_latest"]]
    for col in out.columns[2:]:
        out[col] = out[col].round(3)
    return out


def build_current(mnf_df, combined, meter_map):
    """สรุปสถานะล่าสุดต่อ DMA — ค่าที่ใช้เป็นน้ำหนักกระจายเป้า"""
    if mnf_df.empty:
        return pd.DataFrame()

    meters_per_dma = {}
    for rtu, (dma, _) in meter_map.items():
        meters_per_dma[dma] = meters_per_dma.get(dma, 0) + 1

    rows = []
    for dma, group in mnf_df.groupby("dma_code"):
        group = group.sort_values("date")
        values = pd.to_numeric(group["mnf"], errors="coerce").dropna()
        if values.empty:
            continue

        recent = values.tail(RECENT_DAYS)
        last_date = group["date"].iloc[-1]
        last_month = last_date[:7]
        good_days = int(
            group[group["date"].str.startswith(last_month)]["is_good_day"].sum()
        )

        rows.append({
            "dma_code": dma,
            "mnf_current": round(float(recent.mean()), 3),
            "mnf_floor": round(float(np.percentile(values, FLOOR_PERCENTILE)), 3),
            "mnf_baseline": round(float(values.median()), 3),
            "n_days": int(len(values)),
            "last_date": last_date,
            "n_meters": meters_per_dma.get(dma, 0),
            "good_days_last_month": good_days,
            "last_month": last_month,
        })

    return pd.DataFrame(rows).sort_values("dma_code")


# ==================================================================== output


def write_csv(df, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    log("เขียน %s (%d แถว)" % (os.path.basename(path), len(df)))


def copy_to_static(files):
    if not COPY_TO_STATIC:
        return
    if not os.path.isdir(STATIC_DIR):
        try:
            os.makedirs(STATIC_DIR, exist_ok=True)
        except OSError as exc:
            log("สร้างโฟลเดอร์ static ไม่ได้: %s" % exc)
            return
    for path in files:
        if not os.path.exists(path):
            log("ข้าม (ไม่พบไฟล์): %s" % path)
            continue
        target = os.path.join(STATIC_DIR, os.path.basename(path))
        try:
            shutil.copy(path, target)
            log("คัดลอกไป %s" % target)
        except OSError as exc:
            log("คัดลอกไม่สำเร็จ %s: %s" % (target, exc))


def git_push():
    if not GIT_AUTO_PUSH:
        return
    try:
        subprocess.run(["git", "add", "static/data"], cwd=GIT_REPO_DIR, check=True)
        result = subprocess.run(
            ["git", "commit", "-m", GIT_COMMIT_MESSAGE],
            cwd=GIT_REPO_DIR, capture_output=True, text=True,
        )
        if result.returncode != 0 and "nothing to commit" in (result.stdout or ""):
            log("ไม่มีอะไรเปลี่ยน ข้าม push")
            return
        subprocess.run(["git", "push"], cwd=GIT_REPO_DIR, check=True)
        log("git push เรียบร้อย")
    except (subprocess.CalledProcessError, OSError) as exc:
        log("git push ไม่สำเร็จ: %s" % exc)


def main():
    parser = argparse.ArgumentParser(
        description="คำนวณ MNF และกราฟรายวันของ DMA ในสัญญา PBC"
    )
    parser.add_argument("--parquet", default=RTU_PARQUET)
    parser.add_argument("--meter-map", default=METER_MAP_CSV)
    parser.add_argument("--output", default=OUTPUT_DIR)
    parser.add_argument("--no-copy", action="store_true",
                        help="ไม่ต้องคัดลอกไป static")
    args = parser.parse_args()

    meter_map = load_meter_map(args.meter_map)
    log("ผังมาตร: %d มาตร ใน %d DMA"
        % (len(meter_map), len({v[0] for v in meter_map.values()})))

    df = load_rtu_data(args.parquet, meter_map)
    combined = combine_meters(df)
    mnf_df = daily_mnf(combined)
    envelope = build_envelope(combined, RECENT_DAYS)
    current = build_current(mnf_df, combined, meter_map)

    missing = sorted(
        {v[0] for v in meter_map.values()} - set(current["dma_code"])
        if not current.empty else {v[0] for v in meter_map.values()}
    )
    if missing:
        log("เตือน: %d DMA ไม่มีข้อมูล MNF ใช้ได้เลย -> %s"
            % (len(missing), ", ".join(missing)))
        log("       DMA เหล่านี้จะใช้สัดส่วนปริมาณสูญเสียจาก WB220 "
            "เป็นน้ำหนักกระจายเป้าแทน")

    paths = [
        os.path.join(args.output, "pbc_dma_mnf_daily.csv"),
        os.path.join(args.output, "pbc_dma_current.csv"),
        os.path.join(args.output, "pbc_hourly_envelope.csv"),
    ]
    write_csv(mnf_df, paths[0])
    write_csv(current, paths[1])
    write_csv(envelope, paths[2])

    if not args.no_copy:
        copy_to_static(paths)
        git_push()

    log("เสร็จสิ้น")
    return 0


if __name__ == "__main__":
    sys.exit(main())
