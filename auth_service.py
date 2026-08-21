"""
auth_service.py
ระบบ Login และ Session (Prototype)

หมายเหตุสำคัญสำหรับ prototype นี้:
- Session เก็บใน memory (dict) ของ process เดียว เหมาะสำหรับทดสอบ/เดโมเท่านั้น
  งานจริงควรย้ายไปเก็บใน sheet "Sessions" หรือฐานข้อมูล/Redis ที่ persist ข้ามการรีสตาร์ท
- Password hash ใช้ hashlib.pbkdf2 (ไม่ต้องพึ่ง library เสริม) เพียงพอสำหรับต้นแบบ
  งานจริงแนะนำให้พิจารณา bcrypt/argon2
"""

import hashlib
import os
import secrets
import time

import sheets_client as sc
from constants import ROLE_LEVELS
from config import SESSION_EXPIRY_HOURS

_sessions = {}  # token -> {"user": {...}, "expires_at": epoch_seconds}


def _hash_password(password: str, salt: bytes = None) -> str:
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex() + "$" + dk.hex()


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt_hex, _ = stored_hash.split("$")
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    return _hash_password(password, salt) == stored_hash


def create_user(user_id, name, username, password, role, skip_if_exists=False, **org_fields):
    """สร้างผู้ใช้ใหม่ - org_fields รับ BranchGroupID/BranchID/DivisionID/SectionID/
    ContractorCompany/Phone ตามที่ role นั้นต้องใช้
    skip_if_exists=True: ถ้ามี username นี้อยู่แล้ว ให้คืนข้อมูลเดิมเฉยๆ แทนการ raise error
    (มีประโยชน์เวลารัน demo/seed script ซ้ำหลายครั้ง)"""
    existing = sc.find_one("Users", "Username", username)
    if existing:
        if skip_if_exists:
            return existing
        raise ValueError(f"Username '{username}' มีอยู่แล้ว")
    if role not in ROLE_LEVELS:
        raise ValueError(f"ไม่รู้จัก Role: {role}")

    row = {
        "UserID": user_id,
        "Name": name,
        "Username": username,
        "PasswordHash": _hash_password(password),
        "Role": role,
        "Status": "Active",
    }
    row.update(org_fields)
    sc.append_row("Users", row)
    return row


def login(username: str, password: str) -> str:
    """คืน session token ถ้า login สำเร็จ, raise ValueError ถ้าไม่สำเร็จ"""
    user = sc.find_one("Users", "Username", username)
    if not user:
        # หาไม่เจอในรอบแรก อาจเป็นเพราะ cache ของ Sheet Users ยังว่าง/ผิดพลาดตอน warm_up ตอนเริ่ม
        # โปรแกรม (เช่น โดน rate limit ชั่วคราว) — ลองบังคับโหลดใหม่อีกครั้งก่อนสรุปว่าไม่มีจริง
        # กันปัญหา "login ไม่ผ่านทุกครั้งจนกว่าจะ restart service" ที่เจอมาแล้วจริงจากการใช้งานจริง
        sc.get_all_records("Users", force_refresh=True)
        user = sc.find_one("Users", "Username", username)
    if not user:
        raise ValueError("ไม่พบผู้ใช้งาน")
    if user.get("Status") != "Active":
        raise ValueError("บัญชีนี้ถูกระงับการใช้งาน")
    if not _verify_password(password, user.get("PasswordHash", "")):
        raise ValueError("รหัสผ่านไม่ถูกต้อง")

    token = secrets.token_hex(24)
    _sessions[token] = {
        "user": user,
        "expires_at": time.time() + SESSION_EXPIRY_HOURS * 3600,
    }
    return token


def get_current_user(token: str) -> dict:
    """คืนข้อมูล user จาก token ถ้ายังไม่หมดอายุ, คืน None ถ้าไม่ valid/หมดอายุ"""
    session = _sessions.get(token)
    if not session:
        return None
    if time.time() > session["expires_at"]:
        del _sessions[token]
        return None
    return session["user"]


def logout(token: str):
    _sessions.pop(token, None)


def role_level(role_name: str) -> int:
    return ROLE_LEVELS.get(role_name, 999)
