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

# path ไฟล์ service account — บน Render จะตั้ง env var นี้ให้ชี้ไปที่ Secret File
# ซึ่ง Render จะ mount ไว้ที่ /etc/secrets/service_account.json โดยอัตโนมัติ
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json"
)

# session หมดอายุกี่ชั่วโมง (ใช้กับ auth_service)
SESSION_EXPIRY_HOURS = 8
