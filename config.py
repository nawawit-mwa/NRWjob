"""
Config สำหรับระบบ NRW Job Management (Prototype)

รองรับการรันทั้งในเครื่อง (local) และบน Render.com (production):
- ในเครื่อง: ใช้ค่า default ด้านล่างตรงๆ (service_account.json อยู่โฟลเดอร์เดียวกับโค้ด)
- บน Render: ตั้งค่า environment variable ทับค่า default ได้ (ดู README หัวข้อ Deploy)
"""

import os

GOOGLE_SHEET_ID = os.environ.get(
    "GOOGLE_SHEET_ID", "1s18pk_eUVLAIdkAX3I3KLOF1VDmAZEin2xcqygbHzRs"
)

# โฟลเดอร์ Google Drive ที่ใช้เก็บรูปกราฟจาก "บันทึกการแจ้งเตือน MNF" — ต้องเป็นโฟลเดอร์ของบัญชี Google
# จริงที่แชร์สิทธิ์ Editor ให้ Service Account แล้วเท่านั้น (ห้ามปล่อยว่าง/อัปโหลดเข้าพื้นที่ของ Service
# Account เอง เพราะ Service Account ไม่มีพื้นที่เก็บข้อมูลเป็นของตัวเอง จะเจอ 403 Forbidden ทันที)
GOOGLE_DRIVE_FOLDER_ID = os.environ.get(
    "GOOGLE_DRIVE_FOLDER_ID", "1ejwnP_1c1QKgJU6j7pQbkUj6N7t1jA5q"
)

# path ไฟล์ service account — บน Render จะตั้ง env var นี้ให้ชี้ไปที่ Secret File
# ซึ่ง Render จะ mount ไว้ที่ /etc/secrets/service_account.json โดยอัตโนมัติ
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"
)

# session หมดอายุกี่ชั่วโมง (ใช้กับ auth_service)
SESSION_EXPIRY_HOURS = 8
