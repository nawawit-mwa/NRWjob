import subprocess
import shutil
import os
import sys
import glob
import json
import time
from datetime import datetime

# สมมติโฟลเดอร์ที่เป็น Git Repository ของคุณ
git_folder = r"C:\NRWjob"

# ==========================================
# ส่วนดึงข้อมูลจาก Oracle ย้ายไปอยู่บน server ใหม่แล้ว (Windows Server 2022, อินทราเน็ต) —
# server รัน server_export_rtu.py ตาม Task Scheduler ของตัวเอง แล้ววางไฟล์ลง shared folder
# เครื่องนี้แค่ "หยิบ" ไฟล์ชุดล่าสุดจาก share มาใช้ (ไม่ต่อ Oracle เองอีก) แล้วประมวลผล + git push ต่อเหมือนเดิม
#
# SERVER_SHARE_DIR: path UNC ของ shared folder บน server (ตั้งทับได้ด้วย env NRW_SHARE_DIR โดยไม่ต้องแก้โค้ด
#   เช่น  setx NRW_SHARE_DIR \\172.19.32.165\apps\NRW_Share ) — ไฟล์อยู่ในโฟลเดอร์ย่อย rtu\ ของ share นี้
# FALLBACK_LOCAL_FETCH: ถ้าต่อ share ไม่ได้/ยังไม่มีข้อมูล ให้กลับไปรัน WLMAmeterExport.py ดึง Oracle จาก
#   เครื่องนี้แบบเดิม (ใช้ช่วงเปลี่ยนผ่าน — ถ้าเครื่องนี้ต่อ Oracle ไม่ได้แล้ว ให้ตั้งเป็น False)
# STALE_WARN_HOURS: ชุดข้อมูลบน server เก่ากว่านี้ (นับจากเวลาดึง) จะขึ้นคำเตือน — แต่ยังใช้ต่อได้
# ==========================================
# server ดึงข้อมูล = 172.19.32.165 (ชื่อเครื่อง WLMA-USER) — ใส่ได้หลาย path คั่นด้วย ";" สคริปต์จะใช้ path แรก
# ที่เจอ rtu\latest.json (path UNC ใช้ได้แม้ Task Scheduler รันแบบไม่ล็อกอิน ส่วน Y: ใช้ได้เฉพาะตอนล็อกอินอยู่)
DEFAULT_SHARE_DIRS = r"\\172.19.32.165\apps\NRW_Share;Y:\NRW_Share"
SERVER_SHARE_DIR = os.environ.get("NRW_SHARE_DIR", DEFAULT_SHARE_DIRS)
FALLBACK_LOCAL_FETCH = True
STALE_WARN_HOURS = 26

# Python ที่ใช้เรียกสคริปต์ย่อย — ใช้ตัวเดียวกับที่รันไฟล์นี้อยู่ (sys.executable) แทนคำว่า "python" เฉยๆ
# เพราะตอน Task Scheduler รัน PATH อาจไม่มี python ทำให้สคริปต์ย่อยไม่ถูกเรียกเลยแต่ task ยังขึ้น 0x0
# (ถ้ารันจาก GetNewRTUlog.exe ที่ build ด้วย PyInstaller, sys.executable คือตัว exe เอง -> ใช้ "python" ตามเดิม)
PYTHON_EXE = "python" if getattr(sys, "frozen", False) else (sys.executable or "python")

# log ทุกรอบ (ทั้งข้อความของไฟล์นี้และของสคริปต์ย่อย) — อยู่นอก C:\NRWjob เพื่อไม่ให้ `git add .` เอาขึ้น Render
LOG_DIR = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\logs"

# จดว่ารอบล่าสุดหยิบ export_id ไหนมาแล้ว (กันคัดลอกไฟล์ 500MB ซ้ำทุกรอบเมื่อ server ยังไม่มีชุดใหม่)
PULL_STATE_FILE = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\server_pull_state.json"


def _copy_verified(src, dest_tmp, expected_bytes=None, retries=3):
    """คัดลอก src -> dest_tmp แล้วเช็คขนาดไฟล์ตรงกับ manifest — ใช้ copyfile (ไม่ใช่ copy2) ให้ mtime ของ
    ไฟล์ปลายทางเป็น "ตอนนี้" เสมอ เพราะ evaluate_export_rtu_data.py ใช้ mtime ของ rtu_raw_export.csv
    ตัดสินว่ามีข้อมูลดิบใหม่หรือไม่"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            shutil.copyfile(src, dest_tmp)
            size = os.path.getsize(dest_tmp)
            if expected_bytes is not None and size != int(expected_bytes):
                raise IOError(f"ขนาดไฟล์ไม่ตรง ({size:,} != {int(expected_bytes):,} bytes)")
            return
        except Exception as e:
            last_err = e
            print(f"⚠️ คัดลอก {os.path.basename(src)} รอบที่ {attempt} ไม่สำเร็จ: {e}")
            time.sleep(5 * attempt)
    raise IOError(f"คัดลอก {src} ไม่สำเร็จหลังลอง {retries} ครั้ง: {last_err}")


def resolve_share_dir(share_dirs, probe=os.path.join("rtu", "latest.json")):
    """share_dirs = path เดียว หรือหลาย path คั่นด้วย ";" — คืน path แรกที่มีไฟล์ probe อยู่จริง
    (ไม่เจอเลย คืน path แรก เพื่อให้ข้อความ error ชี้ path ที่ตั้งใจไว้)"""
    candidates = [d.strip() for d in str(share_dirs).split(";") if d.strip()]
    for d in candidates:
        try:
            if os.path.exists(os.path.join(d, probe)):
                return d
        except OSError:
            pass
    return candidates[0] if candidates else ""


def pull_from_server(share_dir, dest_csv, dest_parquet, state_path=PULL_STATE_FILE):
    """หยิบชุดข้อมูลล่าสุดที่ server_export_rtu.py วางไว้ใน <share_dir>\\rtu\\ (อ่านจาก latest.json ซึ่ง
    server เขียนเป็นอย่างสุดท้ายหลังไฟล์ข้อมูลเขียนเสร็จแล้วเท่านั้น)

    คืนค่า:
      "pulled"      — ได้ชุดใหม่ คัดลอกทับ rtu_raw_export.csv + rtu_hist_cache.parquet แล้ว
      "unchanged"   — server ยังไม่มีชุดใหม่กว่ารอบก่อน ใช้ไฟล์เดิมในเครื่องต่อ
      "unavailable" — ต่อ share ไม่ได้ / ไม่มี latest.json / ไฟล์ไม่ครบ / คัดลอกไม่สำเร็จ
    """
    rtu_dir = os.path.join(share_dir, "rtu")
    manifest_path = os.path.join(rtu_dir, "latest.json")
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError:
        print(f"⚠️ ไม่พบ {manifest_path} (ต่อ share ไม่ได้ หรือ server ยังไม่เคยรัน)")
        return "unavailable"
    except Exception as e:
        print(f"⚠️ อ่าน {manifest_path} ไม่ได้: {e}")
        return "unavailable"

    export_id = manifest.get("export_id")
    src_csv = os.path.join(rtu_dir, manifest.get("csv", ""))
    src_pq = os.path.join(rtu_dir, manifest.get("parquet", ""))
    print(f"🔎 ชุดล่าสุดบน server: {export_id} (ดึงเมื่อ {manifest.get('fetched_at')}, "
          f"{manifest.get('rows', 0):,} แถว, ข้อมูลถึง {manifest.get('log_dt_max')})")

    try:
        fetched_at = datetime.strptime(manifest["fetched_at"], "%Y-%m-%d %H:%M:%S")
        age_h = (datetime.now() - fetched_at).total_seconds() / 3600
        if age_h > STALE_WARN_HOURS:
            print(f"⚠️ ชุดข้อมูลบน server เก่าแล้ว {age_h:.0f} ชม. — ตรวจ Task Scheduler / log บน server")
    except (KeyError, ValueError):
        pass

    last_id = None
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                last_id = json.load(f).get("export_id")
        except Exception:
            last_id = None
    if last_id == export_id and os.path.exists(dest_csv) and os.path.exists(dest_parquet):
        print(f"ℹ️ เคยหยิบชุด {export_id} มาแล้ว — ใช้ไฟล์เดิมในเครื่องต่อ ไม่คัดลอกซ้ำ")
        return "unchanged"

    if not (os.path.exists(src_csv) and os.path.exists(src_pq)):
        print(f"⚠️ latest.json ชี้ไฟล์ที่ไม่มีอยู่จริง ({src_csv} / {src_pq})")
        return "unavailable"

    tmp_csv, tmp_pq = dest_csv + ".tmp", dest_parquet + ".tmp"
    try:
        t0 = time.time()
        _copy_verified(src_pq, tmp_pq, manifest.get("parquet_bytes"))
        _copy_verified(src_csv, tmp_csv, manifest.get("csv_bytes"))
        # คัดลอกครบทั้งสองไฟล์แล้วค่อยสลับเข้าที่จริงพร้อมกัน — ไม่มีทางได้ CSV ใหม่คู่กับ parquet เก่า
        os.replace(tmp_pq, dest_parquet)
        os.replace(tmp_csv, dest_csv)
        print(f"✅ คัดลอกจาก server เรียบร้อย ({time.time() - t0:.0f} วินาที) -> "
              f"{os.path.basename(dest_csv)}, {os.path.basename(dest_parquet)}")
    except Exception as e:
        print(f"❌ คัดลอกไฟล์จาก server ไม่สำเร็จ: {e}")
        for t in (tmp_csv, tmp_pq):
            if os.path.exists(t):
                try:
                    os.remove(t)
                except OSError:
                    pass
        return "unavailable"

    try:
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"export_id": export_id, "pulled_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "source": manifest_path}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ บันทึก {state_path} ไม่ได้ (รอบหน้าจะคัดลอกซ้ำ ไม่กระทบผลลัพธ์): {e}")
    return "pulled"


class _Tee:
    """เขียนทุกอย่างที่ print ลงทั้งหน้าจอและไฟล์ log"""
    def __init__(self, stream, fh):
        self.stream, self.fh = stream, fh
    def write(self, data):
        for target in (self.stream, self.fh):
            if target is None:
                continue
            try:
                target.write(data)
            except Exception:
                try:
                    target.write(data.encode("ascii", "replace").decode("ascii"))
                except Exception:
                    pass
        self.flush()
    def flush(self):
        for target in (self.stream, self.fh):
            try:
                if target is not None:
                    target.flush()
            except Exception:
                pass


def setup_run_log():
    """เปิดไฟล์ log รายเดือน แล้วส่ง stdout/stderr ไปทั้งหน้าจอและไฟล์ — ล้มเหลวก็รันต่อได้ปกติ"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"GetNewRTUlog_{datetime.now():%Y%m}.log")
        fh = open(path, "a", encoding="utf-8")
        fh.write(f"\n===== เริ่มรอบใหม่ {datetime.now():%Y-%m-%d %H:%M:%S} "
                 f"(python={PYTHON_EXE}, cwd={os.getcwd()}) =====\n")
        sys.stdout = _Tee(sys.__stdout__, fh)
        sys.stderr = _Tee(sys.__stderr__, fh)
        return path
    except Exception as e:
        print(f"⚠️ เปิดไฟล์ log ไม่ได้: {e}")
        return None


def run_step(args, cwd):
    """รันสคริปต์ย่อยแล้วส่งข้อความของมันผ่าน print (จึงลงไฟล์ log ด้วย) — exit code ไม่ใช่ 0 จะ raise
    CalledProcessError เหมือน subprocess.run(check=True) เดิม"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=env)
    for line in proc.stdout:
        print(line, end="")
    rc = proc.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, args)


def git_push_auto(repo_dir, commit_message=None):
    """ฟังก์ชันสั่ง git add, commit, และ push อัตโนมัติ"""
    if commit_message is None:
        commit_message = (
            f"auto update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )

    try:
        print(f"--- 📌 เริ่มกระบวนการ Git Push ใน {repo_dir} ---")

        # 1. git add .
        run_step(["git", "add", "."], cwd=repo_dir)
        print("✅ Git add เรียบร้อย")

       # 2. git commit -m "..."
        # ใช้ capture_output=True เพื่อเช็คว่ามีไฟล์ให้ commit หรือไม่
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",    # 🟢 เพิ่มบรรทัดนี้: บังคับให้อ่านเป็น UTF-8
            errors="replace"     # 🟢 เพิ่มบรรทัดนี้: ถ้าเจออักขระที่อ่านไม่ออกให้แทนที่ ไม่ต้อง Error
        )

        if "nothing to commit" in commit_result.stdout:
            print("ℹ️ ไม่มีไฟล์เปลี่ยนแปลง ข้ามขั้นตอน commit และ push")
            return

        print(f"✅ Git commit เรียบร้อย: '{commit_message}'")

        # 3. git push
        run_step(["git", "push"], cwd=repo_dir)
        print("🚀 Git push ขึ้นเซิร์ฟเวอร์เรียบร้อยแล้ว!\n")

    except subprocess.CalledProcessError as e:
        print(f"❌ เกิดข้อผิดพลาดในการรันคำสั่ง Git: {e}")
    except Exception as e:
        print(f"❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")

def get_latest_csv(folder_path, exclude_files=None, pattern="*.csv"):
    """ฟังก์ชันหาไฟล์ .csv ที่ถูกสร้างหรือแก้ไขล่าสุดในโฟลเดอร์
    exclude_files: path ของไฟล์ที่ไม่ต้องการนับ (เช่นไฟล์ปลายทางชื่อคงที่ที่ถูก copy ทับทุกรอบ)
    ป้องกันไม่ให้ไฟล์ปลายทางนั้นถูกเข้าใจผิดว่าเป็น "ไฟล์ดิบล่าสุด" ในรอบที่ไม่มีการดึงข้อมูลใหม่
    pattern: glob pattern ของชื่อไฟล์ที่นับเป็น "ไฟล์ดิบ" ได้ (default "*.csv" = ทุกไฟล์ .csv ในโฟลเดอร์)
    เดิมใช้ "*.csv" แบบกว้างสุด ซึ่งเป็นปัญหาเมื่อโฟลเดอร์เดียวกัน (NRW_Monitoring) มีทั้งไฟล์ raw จาก
    WLMAmeterExport.py (ชื่อ VIEW_METER_HIST_RTU_*.csv) และไฟล์ output ของ pipeline เอง
    (dma_status_summary.csv, dma_hourly_envelope.csv, rtu_quality_report.csv ฯลฯ) ปนกันอยู่ — รอบไหนที่
    WLMAmeterExport.py ข้ามการดึง Oracle (cache ยังไม่เก่า) จะไม่มีไฟล์ raw ใหม่เกิดขึ้นเลย แต่ไฟล์ output
    จากรอบก่อนหน้าที่เพิ่งถูกเขียนทับท้ายสุดจะ "ใหม่กว่า" เสมอ ทำให้ถูกเข้าใจผิดเป็น "ไฟล์ดิบล่าสุด" และถูก
    copy ไปทับ rtu_raw_export.csv จนพัง (บั๊กที่เจอจริงและแก้ไปแล้ว) — เรียกจาก run_batch_tasks() ด้วย
    pattern="VIEW_METER_HIST_RTU_*.csv" ให้ตรงกับชื่อไฟล์ที่ WLMAmeterExport.py สร้างจริงเท่านั้น กันไม่ให้
    ไปหยิบไฟล์ output ของ pipeline เองมาใช้ผิดอีก
    """
    # ค้นหาไฟล์ที่ตรง pattern ในโฟลเดอร์ (ไม่ใช่ทุกไฟล์ .csv แบบเดิม — ดู docstring ด้านบน)
    search_pattern = os.path.join(folder_path, pattern)
    csv_files = glob.glob(search_pattern)

    if exclude_files:
        exclude_abs = {os.path.normcase(os.path.abspath(f)) for f in exclude_files}
        csv_files = [
            f for f in csv_files
            if os.path.normcase(os.path.abspath(f)) not in exclude_abs
        ]

    if not csv_files:
        return None

    # เรียงลำดับไฟล์ตามเวลาแก้ไขล่าสุด (ไฟล์ล่าสุดจะอยู่ท้ายสุด)
    latest_file = max(csv_files, key=os.path.getmtime)
    return latest_file

def fetch_locally_legacy(dir_script1, script1_path, dir_raw, destination_file1):
    """ทางสำรอง (FALLBACK_LOCAL_FETCH) — ขั้นตอนที่ 1-2 แบบเดิมก่อนย้ายไป server: รัน WLMAmeterExport.py
    ดึง Oracle จากเครื่องนี้ แล้วคัดลอก VIEW_METER_HIST_RTU_*.csv ล่าสุดเป็น rtu_raw_export.csv
    คืน True ถ้าไปต่อได้, False ถ้าต้องหยุด

    ต่างจากเดิมจุดเดียว: คัดลอกเฉพาะเมื่อไฟล์ VIEW_METER_HIST_RTU_*.csv ล่าสุด "ใหม่กว่า" rtu_raw_export.csv
    ที่มีอยู่ — เพราะตอนนี้ rtu_raw_export.csv ปกติมาจาก server แล้ว ถ้า WLMAmeterExport.py ข้ามการดึง (cache
    ยังไม่เก่า) ไฟล์ VIEW_METER_HIST_RTU_*.csv ที่เหลือในเครื่องอาจเป็นชุดเก่าก่อนย้าย ห้ามเอาไปทับข้อมูลใหม่
    """
    print(f"--- 1b. กำลังรัน WLMAmeterExport.py ใน {dir_script1} ---")
    run_step([PYTHON_EXE, script1_path], cwd=dir_script1)
    print("✅ รัน WLMAmeterExport.py เสร็จสิ้น\n")

    print("--- 2b. กำลังค้นหาไฟล์ CSV ดิบล่าสุด ---")
    latest_csv = get_latest_csv(
        dir_raw, exclude_files=[destination_file1], pattern="VIEW_METER_HIST_RTU_*.csv"
    )
    dest_exists = os.path.exists(destination_file1)
    if latest_csv and (not dest_exists or os.path.getmtime(latest_csv) > os.path.getmtime(destination_file1)):
        print(f"🔎 พบไฟล์ CSV ล่าสุด: {os.path.basename(latest_csv)}")
        os.makedirs(os.path.dirname(destination_file1), exist_ok=True)
        shutil.copy(latest_csv, destination_file1)
        print(f"✅ คัดลอก '{latest_csv}' ไปยัง '{destination_file1}' เรียบร้อย\n")
        return True
    if dest_exists:
        print(f"ℹ️ ไม่มีไฟล์ CSV ดิบที่ใหม่กว่าเดิม — ใช้ไฟล์ปัจจุบันต่อ: '{destination_file1}'\n")
        return True
    print(f"❌ ไม่พบไฟล์ .csv ในโฟลเดอร์ '{dir_raw}' และไม่มีไฟล์ปลายทางเดิม การทำงานหยุดลง")
    return False

def run_batch_tasks():
    # ==========================================
    # กำหนดตัวแปรที่อยู่ไฟล์ (Path) ต่างๆ
    # แนะนำให้ใส่ r (Raw string) หน้าข้อความ Path ใน Windows เพื่อป้องกันปัญหาเครื่องหมาย \
    # ==========================================
    dir_script1 = r"C:\Users\00100156\Desktop\BI\WLMAexport"
    script1_path = os.path.join(dir_script1, "WLMAmeterExport.py")

    destination_file1 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\rtu_raw_export.csv"

    dir_script2 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring"
    script2_path = os.path.join(dir_script2, "evaluate_export_rtu_data.py")


    dir_script3 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring"
    script3_path = os.path.join(dir_script2, "prepare_dma_csv.py")

    
    source_file2_1 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\dma_daily_series.csv"
    destination_file2_1 = r"C:\NRWjob\static\data\dma_daily_series.csv"

    source_file2_2 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\dma_status_summary.csv"
    destination_file2_2 = r"C:\NRWjob\static\data\dma_status_summary.csv"

    source_file2_3 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\flow_log.csv"
    destination_file2_3 = r"C:\NRWjob\static\data\flow_log.csv"

    # dma_hourly_envelope.csv -- ไฟล์ที่กราฟรายวัน 15 นาทีใน dashboard ใช้ (ENVELOPE_CSV_URL ใน monitoring.html)
    # เดิมไม่มี copy step นี้เลย ทำให้ prepare_dma_csv.py สร้างไฟล์ใหม่ถูกต้องทุกรอบในเครื่อง แต่ static folder
    # ที่ dashboard โหลดจริงค้างเป็นเวอร์ชันเก่าตลอด (กราฟไม่ตรงกับตัวเลขใน dma_status_summary.csv ที่ถูกคัดลอก
    # อัตโนมัติอยู่แล้ว) -- เพิ่มให้คัดลอกเหมือน 3 ไฟล์ด้านบนทุกประการ
    source_file2_4 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\dma_hourly_envelope.csv"
    destination_file2_4 = r"C:\NRWjob\static\data\dma_hourly_envelope.csv"

    # rtu_hist_cache.parquet: compute_weekly_trend.py (NRW_MediumTerm) อ่านไฟล์นี้ — ตอนนี้ได้มาจาก server
    # พร้อมกับ CSV ในชุดเดียวกัน (เดิม WLMAmeterExport.py ในเครื่องนี้เป็นคนเขียน)
    destination_parquet1 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\rtu_hist_cache.parquet"

    try:
        # ==========================================
        # ขั้นตอนที่ 1: หยิบข้อมูลดิบชุดล่าสุดจาก server (แทนการรัน WLMAmeterExport.py ดึง Oracle เองแบบเดิม)
        # ==========================================
        share_dir = resolve_share_dir(SERVER_SHARE_DIR)
        print(f"--- 1. กำลังหยิบข้อมูลดิบล่าสุดจาก server: {share_dir} ---")
        status = pull_from_server(share_dir, destination_file1, destination_parquet1)

        if status in ("pulled", "unchanged"):
            print("")
        elif FALLBACK_LOCAL_FETCH:
            print("🟡 ใช้ข้อมูลจาก server ไม่ได้รอบนี้ — fallback: ดึง Oracle จากเครื่องนี้แบบเดิม\n")
            if not fetch_locally_legacy(dir_script1, script1_path, dir_script2, destination_file1):
                return
        elif os.path.exists(destination_file1):
            print(f"⚠️ ใช้ข้อมูลจาก server ไม่ได้ และปิด fallback ไว้ — ประมวลผลต่อด้วยไฟล์เดิม "
                  f"'{destination_file1}'\n")
        else:
            print("❌ ใช้ข้อมูลจาก server ไม่ได้ และไม่มีไฟล์ข้อมูลดิบเดิมในเครื่อง การทำงานหยุดลง")
            return

        # ==========================================
        # ขั้นตอนที่ 3: รัน script2.py ใน folder_b
        # ==========================================
        print(f"--- 3. กำลังรัน evaluate_export_rtu_data.py ใน {dir_script2} ---")
        run_step([PYTHON_EXE, script2_path], cwd=dir_script2)
        print("✅ รัน evaluate_export_rtu_data.py เสร็จสิ้น\n")

        # ==========================================
        # ขั้นตอนที่ 4: รัน script3.py ใน folder_b
        # ==========================================
        print(f"--- 4. กำลังรัน prepare_dma_csv.py ใน {dir_script2} ---")
        run_step([PYTHON_EXE, script3_path], cwd=dir_script3)
        print("✅ รัน prepare_dma_csv.py เสร็จสิ้น\n")

        # ==========================================
        # ขั้นตอนที่ 5: คัดลอกและเปลี่ยนชื่อไฟล์ข้ามโฟลเดอร์
        # ==========================================
        print("--- 5. กำลังคัดลอกและเปลี่ยนชื่อไฟล์ข้ามโฟลเดอร์ ---")
        if os.path.exists(source_file2_1):
            
            # ดึงเฉพาะชื่อโฟลเดอร์ปลายทางออกมา (E:\backup_folder)
            dest_dir = os.path.dirname(destination_file2_1)
            
            # ถ้าโฟลเดอร์ปลายทางยังไม่มีให้สร้างใหม่ก่อน (เพื่อป้องกัน Error)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
                print(f"📁 สร้างโฟลเดอร์ปลายทาง: {dest_dir}")

            shutil.copy(source_file2_1, destination_file2_1)
            print(f"✅ คัดลอกไฟล์ไปยัง '{destination_file2_1}' เรียบร้อย\n")
        else:
            print(f"❌ ไม่พบไฟล์ '{source_file2_1}' การทำงานหยุดลง")
            return 

        if os.path.exists(source_file2_2):
                    
            # ดึงเฉพาะชื่อโฟลเดอร์ปลายทางออกมา (E:\backup_folder)
            dest_dir = os.path.dirname(destination_file2_2)
            
            # ถ้าโฟลเดอร์ปลายทางยังไม่มีให้สร้างใหม่ก่อน (เพื่อป้องกัน Error)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
                print(f"📁 สร้างโฟลเดอร์ปลายทาง: {dest_dir}")

            shutil.copy(source_file2_2, destination_file2_2)
            print(f"✅ คัดลอกไฟล์ไปยัง '{destination_file2_2}' เรียบร้อย\n")
        else:
            print(f"❌ ไม่พบไฟล์ '{source_file2_2}' การทำงานหยุดลง")
            return 

        if os.path.exists(source_file2_3):
                            
            # ดึงเฉพาะชื่อโฟลเดอร์ปลายทางออกมา (E:\backup_folder)
            dest_dir = os.path.dirname(destination_file2_3)
            
            # ถ้าโฟลเดอร์ปลายทางยังไม่มีให้สร้างใหม่ก่อน (เพื่อป้องกัน Error)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
                print(f"📁 สร้างโฟลเดอร์ปลายทาง: {dest_dir}")

            shutil.copy(source_file2_3, destination_file2_3)
            print(f"✅ คัดลอกไฟล์ไปยัง '{destination_file2_3}' เรียบร้อย\n")
        else:
            print(f"❌ ไม่พบไฟล์ '{source_file2_3}' การทำงานหยุดลง")
            return

        if os.path.exists(source_file2_4):

            # ดึงเฉพาะชื่อโฟลเดอร์ปลายทางออกมา (E:\backup_folder)
            dest_dir = os.path.dirname(destination_file2_4)

            # ถ้าโฟลเดอร์ปลายทางยังไม่มีให้สร้างใหม่ก่อน (เพื่อป้องกัน Error)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
                print(f"📁 สร้างโฟลเดอร์ปลายทาง: {dest_dir}")

            shutil.copy(source_file2_4, destination_file2_4)
            print(f"✅ คัดลอกไฟล์ไปยัง '{destination_file2_4}' เรียบร้อย\n")
        else:
            print(f"❌ ไม่พบไฟล์ '{source_file2_4}' การทำงานหยุดลง")
            return

        print("🎉 การทำงานทั้งหมดเสร็จสมบูรณ์!")

    except subprocess.CalledProcessError as e:
        print(f"\n❌ เกิดข้อผิดพลาด! สคริปต์รันไม่สำเร็จ (Error Code: {e.returncode})")
    except Exception as e:
        print(f"\n❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")

    
    # 2. เรียกใช้ฟังก์ชัน Git push เป็นขั้นตอนสุดท้าย
    git_push_auto(repo_dir=git_folder)

if __name__ == "__main__":
    _log_path = setup_run_log()
    if _log_path:
        print(f"📝 log: {_log_path}")
    run_batch_tasks()
    print(f"===== จบรอบ {datetime.now():%Y-%m-%d %H:%M:%S} =====")