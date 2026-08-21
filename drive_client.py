"""
drive_client.py
อัปโหลดรูปกราฟ (base64 data URL จาก canvas.toDataURL) ขึ้น Google Drive
ใช้ credentials ตัวเดียวกับ sheets_client.py (service account เดิม — ไม่ต้องขอสิทธิ์เพิ่ม
เพราะ SCOPES ใน sheets_client.py มี "https://www.googleapis.com/auth/drive" อยู่แล้วตั้งแต่ต้น)

ไม่ใช้ google-api-python-client (ไม่อยากเพิ่ม dependency ใหญ่ๆ) — เรียก Drive REST API v3
ตรงๆ ผ่าน AuthorizedSession ของ google-auth ซึ่งมีอยู่แล้วในโปรเจกต์
"""

import base64
import json
import re

from google.auth.transport.requests import AuthorizedSession

import config
import sheets_client as sc

DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id"
DRIVE_PERMISSIONS_URL_TMPL = "https://www.googleapis.com/drive/v3/files/{file_id}/permissions"

_session = None


def _get_session():
    global _session
    if _session is None:
        _session = AuthorizedSession(sc.get_credentials())
    return _session


def upload_chart_image(data_url: str, filename: str) -> str:
    """อัปโหลดรูปจาก data URL (เช่น 'data:image/jpeg;base64,...') ขึ้น Google Drive
    ตั้งสิทธิ์ให้ดูได้ผ่านลิงก์ (anyone/reader) แล้วคืนลิงก์เปิดดูรูป
    raise ValueError ถ้ารูปแบบข้อมูลผิด/ยังไม่ตั้งค่าโฟลเดอร์, raise ตาม HTTP error ถ้าอัปโหลดไม่สำเร็จ"""
    if not config.GOOGLE_DRIVE_FOLDER_ID:
        raise ValueError(
            "ยังไม่ได้ตั้งค่า GOOGLE_DRIVE_FOLDER_ID — ต้องสร้างโฟลเดอร์ Drive จริง (ของบัญชี Google จริง "
            "ไม่ใช่ของ Service Account เอง) แล้วแชร์สิทธิ์ Editor ให้ Service Account ก่อน"
        )

    match = re.match(r"^data:(image/\w+);base64,(.+)$", data_url or "")
    if not match:
        raise ValueError("รูปแบบข้อมูลรูปภาพไม่ถูกต้อง (ต้องเป็น data URL แบบ image/*)")
    mime_type, b64data = match.groups()
    file_bytes = base64.b64decode(b64data)

    session = _get_session()
    # ต้องระบุ parents เป็นโฟลเดอร์ของบัญชี Google จริงเสมอ — ถ้าไม่ระบุ ไฟล์จะพยายามสร้างในพื้นที่ของ
    # Service Account เอง ซึ่งมีพื้นที่เก็บข้อมูล 0 ไบต์ ทำให้เจอ 403 Forbidden ทันที (ปัญหาที่เจอมาแล้วจริง)
    metadata = {"name": filename, "mimeType": mime_type, "parents": [config.GOOGLE_DRIVE_FOLDER_ID]}
    files = {
        "data": ("metadata", json.dumps(metadata), "application/json; charset=UTF-8"),
        "file": (filename, file_bytes, mime_type),
    }
    resp = session.post(DRIVE_UPLOAD_URL, files=files)
    if not resp.ok:
        # resp.raise_for_status() เดิมให้แค่ "403 Forbidden" เฉยๆ ไม่พอไล่บั๊ก — ต้องดึง response body
        # จริงจาก Google มาด้วย เพราะเหตุผลจริง (เช่น "Drive API ยังไม่เปิดใช้งาน", "storageQuotaExceeded",
        # "insufficientFilePermissions" ฯลฯ) อยู่ใน JSON body ไม่ใช่แค่ status line
        raise RuntimeError(
            f"อัปโหลด Drive ไม่สำเร็จ HTTP {resp.status_code}: {resp.text[:500]}"
        )
    file_id = resp.json()["id"]

    # เปิดสิทธิ์ให้ดูได้แบบมีลิงก์ (ไม่ต้อง login Google) เพื่อให้เปิดดูรูปจาก popup ได้ทุกคน
    session.post(
        DRIVE_PERMISSIONS_URL_TMPL.format(file_id=file_id),
        json={"role": "reader", "type": "anyone"},
    )

    return f"https://drive.google.com/uc?id={file_id}"
