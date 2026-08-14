# NRW Job Management — ต้นแบบ (Prototype)

ต้นแบบระบบจัดการงาน NRW บนฐานข้อมูล Google Sheets ตามที่ออกแบบไว้ในบทสนทนา
ครอบคลุม: Login, ลำดับชั้นผู้ใช้งาน, เหตุการณ์→งาน (1:หลาย), มอบหมาย/โอนงาน,
วงจรสถานะงาน 9 สถานะ, DueDate อัตโนมัติ + แจ้งเตือน, Viewer Scope

**ยังไม่รวม:** API สำหรับระบบ NRW Monitoring ภายนอก (พักไว้ตามที่แจ้ง จนกว่าจะเริ่มพัฒนาต้นแบบส่วนนี้จริง)

## โครงสร้างไฟล์

| ไฟล์ | หน้าที่ |
|---|---|
| `config.py` | ค่าตั้งค่า Sheet ID / service account |
| `constants.py` | Role, RoleLevel, Job Status, Action Type ทั้งหมด |
| `sheets_client.py` | Wrapper กลางคุย Google Sheets (ผ่าน gspread) — โมดูลอื่นเรียกผ่านนี้เท่านั้น |
| `schema_setup.py` | สร้าง Sheet ทั้งหมด + ใส่ header + seed ข้อมูล Roles (รันครั้งแรกครั้งเดียว) |
| `auth_service.py` | Login/Logout, hash รหัสผ่าน, session (เก็บใน memory — ดูหมายเหตุด้านล่าง) |
| `org_service.py` | หา Section ที่ถูกต้องจาก JobType+สาขา, เช็ค "หน่วยงานเดียวกัน" สำหรับโอนงาน |
| `incident_service.py` | สร้างเหตุการณ์ + แปลงเป็นงาน (เลือกได้หลาย JobType พร้อมกัน) |
| `job_service.py` | มอบหมาย/โอนงาน/รับ-ปฏิเสธ/ดำเนินงาน/ตรวจสอบ/ปิด-ยกเลิก/DueDate |
| `viewer_service.py` | กรองข้อมูลที่ Viewer มองเห็นตาม ViewerScopes |
| `dashboard_service.py` | กรองรายการงาน/เหตุการณ์ที่แต่ละ Role เห็นในหน้า Dashboard |
| `app.py` | Flask Web App — หน้า Login + Dashboard |
| `templates/*.html`, `static/style.css` | หน้าตาเว็บ (Jinja2 templates) |
| `demo.py` | สาธิต workflow เต็มรูปแบบด้วยข้อมูลตัวอย่าง (ทาง backend, ไม่ใช้เว็บ) |

## วิธีติดตั้งและรัน

1. ติดตั้ง dependency:
   ```
   pip install -r requirements.txt
   ```

2. สร้าง Service Account บน Google Cloud Console เปิดใช้ Google Sheets API +
   Google Drive API แล้วดาวน์โหลดไฟล์ key เป็น `service_account.json`
   วางไว้ในโฟลเดอร์เดียวกับโค้ดชุดนี้

3. แชร์ Google Sheet (ID: `1s18pk_eUVLAIdkAX3I3KLOF1VDmAZEin2xcqygbHzRs`)
   ให้กับอีเมลของ service account (มีสิทธิ์แก้ไข/Editor)

4. สร้างโครงสร้าง Sheet ทั้งหมด (รันครั้งเดียว):
   ```
   python schema_setup.py
   ```

5. รันสาธิต workflow เต็มรูปแบบ (ทาง backend เท่านั้น ไม่ผ่านเว็บ):
   ```
   python demo.py
   ```

6. รันเว็บแอป (หลังมีข้อมูล user อย่างน้อย 1 คนแล้ว เช่นจากขั้นตอนที่ 5):
   ```
   python app.py
   ```
   เปิดเบราว์เซอร์ไปที่ `http://127.0.0.1:5000` แล้ว login ด้วย user ตัวอย่างจาก `demo.py`
   เช่น username `mgr1` / password `pass123` (ผู้จัดการสาขา) — Dashboard จะแสดงงาน/เหตุการณ์
   เฉพาะขอบเขตของ Role ที่ login เข้ามา ตามที่ออกแบบไว้

> **หมายเหตุ:** สภาพแวดล้อมที่ใช้ออกแบบโค้ดชุดนี้ไม่มีการเชื่อมต่ออินเทอร์เน็ต
> จึงไม่สามารถรันทดสอบกับ Google Sheets จริงให้ดูได้ในขั้นตอนนี้ — โค้ดผ่านการ
> ตรวจไวยากรณ์ (`py_compile`) เรียบร้อยแล้ว แนะนำให้รันตามขั้นตอนข้างต้นในเครื่อง/
> เซิร์ฟเวอร์ของคุณเพื่อทดสอบกับข้อมูลจริงอีกครั้ง

## ข้อจำกัดของต้นแบบนี้ (ที่ควรทราบก่อนนำไปใช้จริง)

- **Session เก็บใน memory** ของ process เดียว — ถ้าจะทำเป็น Web App/API หลาย process
  ต้องย้ายไปเก็บที่ persist ได้ (เช่น Sheet "Sessions" หรือ Redis)
- **การเขียนพร้อมกัน (concurrent write)** — `next_id()` และการหาแถวเป็นแบบอ่าน-แล้ว-เขียน
  ธรรมดา ถ้ามีผู้ใช้พร้อมกันหลายคนเสี่ยง race condition เล็กน้อย เหมาะกับต้นแบบ/ทดสอบ
  งานจริงควรพิจารณา Apps Script (มี lock service ในตัว) หรือคิวการเขียนฝั่ง backend
- **การแจ้งเตือน (DueDate ขยับ)** ตอนนี้แค่บันทึก log + print เท่านั้น ยังไม่ได้ต่อ
  อีเมล/LINE Notify จริง
- **Viewer scope** กรองตามสาขาแล้ว แต่การจำกัด "ความละเอียดของข้อมูล" ตาม `ViewAsRole`
  ยังเป็นโครงไว้ให้ต่อยอด (เช่น ซ่อนบาง field ตามระดับ) ยังไม่ implement เต็มรูปแบบ
- ยังไม่มี Web UI — เป็น backend logic (Python module) ล้วนๆ พร้อมให้ต่อ Flask/FastAPI
  หรือ Apps Script Web App เป็นหน้าจอได้ในขั้นถัดไป

## ขั้นตอนถัดไปที่แนะนำ

1. ทดสอบรัน `schema_setup.py` + `demo.py` กับ Sheet จริง ตรวจสอบผลลัพธ์ตาม log ที่ print ออกมา
2. เลือกว่าจะทำหน้าจอ (Frontend) เป็น Flask/FastAPI + HTML หรือ Apps Script Web App
3. เพิ่ม Viewer scope filtering แบบเต็ม (จำกัด field ตาม ViewAsRole)
4. ค่อยเพิ่ม API รับ Incident จากระบบ NRW Monitoring ภายนอกทีหลัง
