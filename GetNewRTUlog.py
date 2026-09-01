import subprocess
import shutil
import os
import glob
from datetime import datetime

# สมมติโฟลเดอร์ที่เป็น Git Repository ของคุณ
git_folder = r"C:\NRWjob"

def git_push_auto(repo_dir, commit_message=None):
    """ฟังก์ชันสั่ง git add, commit, และ push อัตโนมัติ"""
    if commit_message is None:
        commit_message = (
            f"auto update: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        )

    try:
        print(f"--- 📌 เริ่มกระบวนการ Git Push ใน {repo_dir} ---")

        # 1. git add .
        subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
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
        subprocess.run(["git", "push"], cwd=repo_dir, check=True)
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

    try:
        # ==========================================
        # ขั้นตอนที่ 1: รัน script1.py ใน folder_a
        # ==========================================
        print(f"--- 1. กำลังรัน script1.py ใน {dir_script1} ---")
        # cwd=dir_script1 จะจำลองการ cd (change directory) เข้าไปใน folder_a ก่อนรันสคริปต์
        subprocess.run(["python", script1_path], cwd=dir_script1, check=True)
        print("✅ รัน script1.py เสร็จสิ้น\n")

        # ==========================================
        # ขั้นตอนที่ 2: ค้นหาไฟล์ CSV ดิบล่าสุดที่ WLMAmeterExport.py เพิ่งสร้างขึ้นมา (ยกเว้นไฟล์ปลายทางเอง)
        # จำกัด pattern เหลือแค่ VIEW_METER_HIST_RTU_*.csv (ชื่อไฟล์ raw จริงจาก WLMAmeterExport.py บรรทัด
        # 113) ไม่ใช่ "*.csv" กว้างๆ เหมือนเดิม — กันไม่ให้ไปหยิบไฟล์ output ของ pipeline เอง
        # (dma_status_summary.csv, dma_hourly_envelope.csv ฯลฯ ที่อยู่โฟลเดอร์เดียวกัน) มาเข้าใจผิดว่าเป็น
        # ไฟล์ raw ใหม่ ตอนที่ WLMAmeterExport.py ข้ามการดึง Oracle เพราะ cache ยังไม่เก่า (ดู docstring
        # ของ get_latest_csv ด้านบนสำหรับรายละเอียดบั๊กเดิม)
        # ==========================================
        print("--- 2. กำลังค้นหาไฟล์ CSV ดิบล่าสุด ---")
        latest_csv = get_latest_csv(
            dir_script2, exclude_files=[destination_file1], pattern="VIEW_METER_HIST_RTU_*.csv"
        )

        if latest_csv:
            print(f"🔎 พบไฟล์ CSV ล่าสุด: {os.path.basename(latest_csv)}")

            # ตรวจสอบและสร้างโฟลเดอร์ปลายทางถ้ายังไม่มี
            dest_dir = os.path.dirname(destination_file1)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)

            # คัดลอกและเปลี่ยนชื่อไฟล์ไปยังปลายทาง
            shutil.copy(latest_csv, destination_file1)
            print(f"✅ คัดลอก '{latest_csv}' ไปยัง '{destination_file1}' เรียบร้อย\n")
        elif os.path.exists(destination_file1):
            # ไม่มีไฟล์ CSV ใหม่ (เช่น script1.py ข้ามการดึงจาก ORACLE เพราะ cache ยังไม่เก่า)
            # แต่ไฟล์ปลายทางจากรอบก่อนยังอยู่ ใช้ต่อได้เลยโดยไม่ต้อง copy ทับ
            print(
                f"ℹ️ ไม่พบไฟล์ CSV ใหม่ในโฟลเดอร์ '{dir_script2}' (อาจข้ามการดึงจาก ORACLE รอบนี้) "
                f"— ใช้ไฟล์ปัจจุบันต่อ: '{destination_file1}'\n"
            )
        else:
            print(f"❌ ไม่พบไฟล์ .csv ในโฟลเดอร์ '{dir_script2}' และไม่มีไฟล์ปลายทางเดิม การทำงานหยุดลง")
            return

        # ==========================================
        # ขั้นตอนที่ 3: รัน script2.py ใน folder_b
        # ==========================================
        print(f"--- 3. กำลังรัน evaluate_export_rtu_data.py ใน {dir_script2} ---")
        subprocess.run(["python", script2_path], cwd=dir_script2, check=True)
        print("✅ รัน evaluate_export_rtu_data.py เสร็จสิ้น\n")

        # ==========================================
        # ขั้นตอนที่ 4: รัน script3.py ใน folder_b
        # ==========================================
        print(f"--- 4. กำลังรัน prepare_dma_csv.py ใน {dir_script2} ---")
        subprocess.run(["python", script3_path], cwd=dir_script3, check=True)
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
    run_batch_tasks()