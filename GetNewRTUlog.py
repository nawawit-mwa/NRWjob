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
        # ใช้ capture_output=True เพื่อเช็คว่ามีไฟล์ให้ commit หรือไม่ (ป้องกันโปรแกรมหลุดถ้าไม่มีอะไรเปลี่ยน)
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=repo_dir,
            capture_output=True,
            text=True,
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

def get_latest_csv(folder_path):
    """ฟังก์ชันหาไฟล์ .csv ที่ถูกสร้างหรือแก้ไขล่าสุดในโฟลเดอร์"""
    # ค้นหาไฟล์ .csv ทั้งหมดในโฟลเดอร์
    search_pattern = os.path.join(folder_path, "*.csv")
    csv_files = glob.glob(search_pattern)
    
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

    dir_script4 = r"C:\NRWjob"
    
    source_file2_1 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\dma_daily_series.csv"
    destination_file2_1 = r"C:\NRWjob\static\data\dma_daily_series.csv"

    source_file2_2 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\dma_status_summary.csv"
    destination_file2_2 = r"C:\NRWjob\static\data\dma_status_summary.csv"

    source_file2_3 = r"C:\Users\00100156\Desktop\BI\NRW_Monitoring\flow_log.csv"
    destination_file2_3 = r"C:\NRWjob\static\data\flow_log.csv"

    try:
        # ==========================================
        # ขั้นตอนที่ 1: รัน script1.py ใน folder_a
        # ==========================================
        print(f"--- 1. กำลังรัน script1.py ใน {dir_script1} ---")
        # cwd=dir_script1 จะจำลองการ cd (change directory) เข้าไปใน folder_a ก่อนรันสคริปต์
        subprocess.run(["python", script1_path], cwd=dir_script1, check=True)
        print("✅ รัน script1.py เสร็จสิ้น\n")

        # ==========================================
        # ขั้นตอนที่ 2: ค้นหาไฟล์ CSV ล่าสุดที่เพิ่งสร้างขึ้นมา
        # ==========================================
        print("--- 2. กำลังค้นหาไฟล์ CSV ล่าสุด ---")
        latest_csv = get_latest_csv(dir_script2)

        if latest_csv:
            print(f"🔎 พบไฟล์ CSV ล่าสุด: {os.path.basename(latest_csv)}")
            
            # ตรวจสอบและสร้างโฟลเดอร์ปลายทางถ้ายังไม่มี
            dest_dir = os.path.dirname(destination_file1)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)

            # คัดลอกและเปลี่ยนชื่อไฟล์ไปยังปลายทาง
            shutil.copy(latest_csv, destination_file1)
            print(f"✅ คัดลอก '{latest_csv}' ไปยัง '{destination_file1}' เรียบร้อย\n")
        else:
            print(f"❌ ไม่พบไฟล์ .csv ในโฟลเดอร์ '{dir_script1}' การทำงานหยุดลง")
            return

        # ==========================================
        # ขั้นตอนที่ 3: คัดลอกและเปลี่ยนชื่อทับ
        # ==========================================
        print("--- 3. กำลังคัดลอกและเปลี่ยนชื่อไฟล์ข้ามโฟลเดอร์ ---")
        if os.path.exists(latest_csv):
            
            # ดึงเฉพาะชื่อโฟลเดอร์ปลายทางออกมา (E:\backup_folder)
            dest_dir = os.path.dirname(destination_file1)
            
            # ถ้าโฟลเดอร์ปลายทางยังไม่มีให้สร้างใหม่ก่อน (เพื่อป้องกัน Error)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
                print(f"📁 สร้างโฟลเดอร์ปลายทาง: {dest_dir}")

            shutil.copy(latest_csv, destination_file1)
            print(f"✅ คัดลอกไฟล์ไปยัง '{destination_file1}' เรียบร้อย\n")
        else:
            print(f"❌ ไม่พบไฟล์ '{latest_csv}' การทำงานหยุดลง")
            return 
       
        # ==========================================
        # ขั้นตอนที่ 4: รัน script3.py ใน folder_b
        # ==========================================
        print(f"--- 4. กำลังรัน script3.py ใน {dir_script2} ---")
        subprocess.run(["python", script2_path], cwd=dir_script2, check=True)
        print("✅ รัน script3.py เสร็จสิ้น\n")

         # ==========================================
        # ขั้นตอนที่ 5: คัดลอกและเปลี่ยนชื่อไฟล์ข้ามโฟลเดอร์
        # ==========================================
        print("--- 2. กำลังคัดลอกและเปลี่ยนชื่อไฟล์ข้ามโฟลเดอร์ ---")
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
        
        print("🎉 การทำงานทั้งหมดเสร็จสมบูรณ์!")

    except subprocess.CalledProcessError as e:
        print(f"\n❌ เกิดข้อผิดพลาด! สคริปต์รันไม่สำเร็จ (Error Code: {e.returncode})")
    except Exception as e:
        print(f"\n❌ เกิดข้อผิดพลาดที่ไม่คาดคิด: {e}")

    
    # 2. เรียกใช้ฟังก์ชัน Git push เป็นขั้นตอนสุดท้าย
    git_push_auto(repo_dir=git_folder)

if __name__ == "__main__":
    run_batch_tasks()