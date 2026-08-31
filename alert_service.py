"""
alert_service.py
บันทึกการแจ้งเตือน MNF ที่สนใจจากหน้า NRW Monitoring ไว้ดูภายหลัง + แปลงเป็นเหตุการณ์ได้

State/logic:
- บันทึกได้เฉพาะ user ที่ login แล้ว (บังคับที่ route ด้วย login_required)
- เห็นรายการที่บันทึกไว้ตามขอบเขตพื้นที่เดียวกับ Incidents (BranchID -> BranchGroupID)
- RTU 1 ตัว บันทึกซ้ำไม่ได้ถ้ามีแถว 'active' (Status ไม่ใช่ 'ยกเลิกแล้ว') อยู่แล้ว
- แปลงเป็นเหตุการณ์ / ยกเลิก: เฉพาะหัวหน้าส่วนขึ้นไป + Admin
- แปลงซ้ำไม่ได้ถ้ามี LinkedIncidentID อยู่แล้ว
- ยกเลิกไม่ได้ถ้าแปลงเป็นเหตุการณ์ไปแล้ว (LinkedIncidentID มีค่าแล้ว)
- กด "ยกเลิก" = เปลี่ยน Status เป็น 'ยกเลิกแล้ว' (soft — เก็บแถวไว้เป็นประวัติ ไม่ลบทิ้ง) ทำให้ RTU
  นั้นกลับมาบันทึกใหม่ได้ทันที เพราะไม่นับเป็น 'active' อีกต่อไป
"""

import datetime

import sheets_client as sc
from auth_service import role_level
from constants import (
    ROLE_ADMIN, ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR,
    ROLE_LEVELS, ROLE_SECTION_CHIEF,
)

ALERT_STATUS_ACTIVE = "บันทึกไว้"
ALERT_STATUS_CANCELLED = "ยกเลิกแล้ว"

MAX_CHART_IMAGE_CHARS = 45000  # เผื่อ margin จากลิมิต 50,000 ตัวอักษร/เซลล์ของ Google Sheets


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def save_alert(user: dict, rtu_id: str, rtu_name: str, case_classification: str,
               current_mnf: str, baseline_mean: str, night_flow_window: str,
               mnf_value: str, cusum: str, trend_result: str, chart_date: str = "",
               note: str = "", image_data_url: str = "") -> str:
    """บันทึกการแจ้งเตือน MNF ที่สนใจ — resolve ZoneID/BranchID จาก RTUID อัตโนมัติ
    (แบบเดียวกับ incident_service.create_incident ตอนสร้างจาก RTU)

    รูปกราฟ: ฝัง base64 data URL ลงเซลล์ ChartImageURL ตรงๆ (ไม่ผ่าน Google Drive เพราะ Service
    Account ไม่มีพื้นที่เก็บข้อมูลใน Drive เป็นของตัวเอง) ถ้ารูปใหญ่เกินลิมิตเซลล์ของ Sheets จะไม่บันทึก
    รูป (แต่ข้อมูลอื่นบันทึกสำเร็จตามปกติ ไม่ทำให้ทั้งรายการล้มเหลว)

    raise ValueError ถ้า RTU นี้มีแถวที่ยัง active (ยังไม่ถูกยกเลิก) อยู่แล้ว — กันบันทึกซ้ำ"""
    existing = get_active_alert_for_rtu(rtu_id)
    if existing:
        raise ValueError(
            f"RTU นี้มีการบันทึกไว้แล้ว ({existing['AlertID']}) — กด 'ยกเลิก' ก่อนถ้าต้องการบันทึกใหม่"
        )

    rtu = sc.find_one("RTUs", "RTUID", rtu_id)
    zone_id = rtu.get("ZoneID", "") if rtu else ""
    branch_id = rtu.get("BranchID", "") if rtu else ""

    chart_image_url = ""
    if image_data_url:
        if len(image_data_url) <= MAX_CHART_IMAGE_CHARS:
            chart_image_url = image_data_url
        else:
            print(
                f"[alert_service] รูปกราฟใหญ่เกิน {MAX_CHART_IMAGE_CHARS} ตัวอักษร "
                f"({len(image_data_url)}) สำหรับ RTU={rtu_id} — ไม่บันทึกรูปแนบ (ข้อมูลอื่นบันทึกต่อตามปกติ)"
            )

    alert_id = sc.next_id("SavedAlerts", "AlertID", "ALT-")
    sc.append_row("SavedAlerts", {
        "AlertID": alert_id,
        "RTUID": rtu_id,
        "RTUName": rtu_name,
        "ZoneID": zone_id,
        "BranchID": branch_id,
        "CaseClassification": case_classification,
        "CurrentMNF": current_mnf,
        "BaselineMean": baseline_mean,
        "NightFlowWindow": night_flow_window,
        "MNFValue": mnf_value,
        "CUSUM": cusum,
        "TrendResult": trend_result,
        "ChartDate": chart_date,
        "ChartImageURL": chart_image_url,
        "Note": note,
        "Status": ALERT_STATUS_ACTIVE,
        "SavedBy": user["UserID"],
        "SavedAt": _now(),
        "LinkedIncidentID": "",
        "LinkedIncidentAt": "",
    })
    return alert_id


def get_alerts_for_user(user: dict) -> list:
    """คืนรายการแจ้งเตือนที่บันทึกไว้ ในขอบเขตพื้นที่ของ user (ตรรกะเดียวกับ dashboard_service
    ใช้กับ Incidents — Admin เห็นหมด, รองผู้ว่าการ/ผู้ช่วยผู้ว่าการ เห็นตามกลุ่มสาขา, ที่เหลือเห็นตามสาขาตน)
    ไม่แสดงรายการที่ถูกยกเลิกแล้ว (Status == ยกเลิกแล้ว) เลย — ยังเก็บแถวไว้ใน Sheet เป็นประวัติ
    (soft-cancel) แค่ไม่โผล่ในรายการที่แสดงผลบนหน้าเว็บอีกต่อไป
    เรียงรายการล่าสุด (SavedAt) ขึ้นก่อนเสมอ — ปกติ Sheet เก็บแถวใหม่ต่อท้ายล่างสุด (เก่าสุดขึ้นก่อน)
    ถ้าไม่เรียงเองจะดูย้อนลำดับกับที่ผู้ใช้คาดหวัง"""
    all_alerts = [
        a for a in sc.get_all_records("SavedAlerts") if a.get("Status") != ALERT_STATUS_CANCELLED
    ]
    role = user.get("Role")

    if role == ROLE_ADMIN:
        scoped_alerts = all_alerts
    elif role in (ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR):
        branch_group_id = user.get("BranchGroupID")
        branches = sc.find_many("Branches", "BranchGroupID", branch_group_id)
        branch_ids = {b["BranchID"] for b in branches}
        scoped_alerts = [a for a in all_alerts if a.get("BranchID") in branch_ids]
    else:
        branch_id = user.get("BranchID")
        scoped_alerts = [a for a in all_alerts if a.get("BranchID") == branch_id] if branch_id else []

    return sorted(scoped_alerts, key=lambda a: a.get("SavedAt", ""), reverse=True)


def get_active_alert_for_rtu(rtu_id: str) -> dict:
    """คืนแถว SavedAlerts ล่าสุดของ RTU นี้ที่ยัง 'active' (Status ไม่ใช่ 'ยกเลิกแล้ว') หรือ None ถ้าไม่มี
    ใช้กันบันทึกซ้ำ + ใช้แสดงสถานะ 'บันทึกข้อมูลแล้ว' ใน popup ของ Monitoring
    หมายเหตุ: แถวเก่าก่อนมีคอลัมน์ Status จะอ่านได้ค่าว่าง ("") ซึ่งนับเป็น active ด้วย (ไม่ใช่ 'ยกเลิกแล้ว')
    จึงย้อนความเข้ากันได้กับข้อมูลเดิมโดยอัตโนมัติ ไม่ต้อง migrate ข้อมูล"""
    alerts = sc.find_many("SavedAlerts", "RTUID", rtu_id)
    active = [a for a in alerts if a.get("Status") != ALERT_STATUS_CANCELLED]
    if not active:
        return None
    active.sort(key=lambda a: a.get("SavedAt", ""), reverse=True)
    return active[0]


def get_alert_permissions(alert: dict, user: dict) -> dict:
    """เช็คว่า user คนนี้แปลง/ยกเลิกแจ้งเตือนนี้ได้ไหม
    สิทธิ์: หัวหน้าส่วนขึ้นไป + Admin ทั้งสองการกระทำ
    - แปลงซ้ำไม่ได้ถ้ามี LinkedIncidentID อยู่แล้ว
    - ยกเลิกไม่ได้ถ้าแปลงเป็นเหตุการณ์ไปแล้ว หรือถูกยกเลิกไปแล้ว"""
    role = user.get("Role")
    is_admin = role == ROLE_ADMIN
    is_authorized = is_admin or role_level(role) <= ROLE_LEVELS[ROLE_SECTION_CHIEF]

    already_linked = bool(alert.get("LinkedIncidentID"))
    already_cancelled = alert.get("Status") == ALERT_STATUS_CANCELLED

    can_convert = is_authorized and not already_linked and not already_cancelled
    can_cancel = is_authorized and not already_linked and not already_cancelled

    # เช็คสถานะจริงของเหตุการณ์ที่เชื่อมโยงไว้ (ปิดแล้วหรือยัง) — เพื่อแสดงสถานะให้ตรงในหน้ารายการ
    incident_closed = False
    if already_linked:
        linked_incident = sc.find_one("Incidents", "IncidentID", alert["LinkedIncidentID"])
        if linked_incident and linked_incident.get("Status") == "ปิดแล้ว":
            incident_closed = True

    return {
        "can_convert": can_convert,
        "can_cancel": can_cancel,
        "already_linked": already_linked,
        "already_cancelled": already_cancelled,
        "incident_closed": incident_closed,
    }


def convert_alert_to_incident(alert_id: str, user: dict) -> str:
    """แปลงแจ้งเตือนที่บันทึกไว้เป็นเหตุการณ์จริง แล้วเขียน LinkedIncidentID กลับเข้า SavedAlerts"""
    import incident_service  # import ในฟังก์ชัน กัน circular import (incident_service ไม่ต้องรู้จัก alert_service)

    alert = sc.find_one("SavedAlerts", "AlertID", alert_id)
    if not alert:
        raise ValueError("ไม่พบรายการแจ้งเตือนนี้")

    perms = get_alert_permissions(alert, user)
    if not perms["can_convert"]:
        if perms["already_linked"]:
            raise ValueError(f"แจ้งเตือนนี้ถูกสร้างเป็นเหตุการณ์ {alert['LinkedIncidentID']} ไปแล้ว")
        if perms["already_cancelled"]:
            raise ValueError("แจ้งเตือนนี้ถูกยกเลิกไปแล้ว")
        raise PermissionError("ไม่มีสิทธิ์สร้างเหตุการณ์จากแจ้งเตือนนี้ (ต้องเป็นหัวหน้าส่วนขึ้นไป)")

    description_parts = [
        f"บันทึกจากระบบ NRW Monitoring — RTU: {alert.get('RTUName', alert.get('RTUID', ''))}",
        f"MNF: {alert.get('MNFValue', '—')} · CUSUM: {alert.get('CUSUM', '—')} · เทรนด์: {alert.get('TrendResult', '—')}",
    ]
    if alert.get("Note"):
        description_parts.append(f"หมายเหตุ: {alert['Note']}")

    incident_id = incident_service.create_incident(
        incident_type=f"MNF ผิดปกติ ({alert.get('CaseClassification', '')})",
        description="\n".join(description_parts),
        severity="สูง",
        reported_by=user["UserID"],
        source="MNF Alert",
        zone_id=alert.get("ZoneID", ""),
        branch_id=alert.get("BranchID", ""),
    )

    sc.update_row("SavedAlerts", "AlertID", alert_id, {
        "LinkedIncidentID": incident_id,
        "LinkedIncidentAt": _now(),
    })
    return incident_id


def cancel_alert(alert_id: str, user: dict):
    """ยกเลิกแจ้งเตือนที่บันทึกไว้ (soft — เปลี่ยน Status เป็น 'ยกเลิกแล้ว' เก็บแถวไว้เป็นประวัติ)
    ทำให้ RTU นั้นกลับมาบันทึกใหม่ได้ทันที"""
    alert = sc.find_one("SavedAlerts", "AlertID", alert_id)
    if not alert:
        raise ValueError("ไม่พบรายการแจ้งเตือนนี้")

    perms = get_alert_permissions(alert, user)
    if not perms["can_cancel"]:
        if perms["already_linked"]:
            raise ValueError("ยกเลิกไม่ได้ เพราะแจ้งเตือนนี้ถูกสร้างเป็นเหตุการณ์ไปแล้ว")
        if perms["already_cancelled"]:
            raise ValueError("แจ้งเตือนนี้ถูกยกเลิกไปแล้ว")
        raise PermissionError("ไม่มีสิทธิ์ยกเลิกแจ้งเตือนนี้ (ต้องเป็นหัวหน้าส่วนขึ้นไป)")

    sc.update_row("SavedAlerts", "AlertID", alert_id, {"Status": ALERT_STATUS_CANCELLED})


def get_all_linked_rtu_ids() -> dict:
    """คืน {RTUID: LinkedIncidentID} ของทุกแถวที่เชื่อมโยงเหตุการณ์แล้ว (ทั้งระบบ ไม่จำกัดขอบเขตพื้นที่
    เพราะใช้เป็นตัวกรอง overlay บนหน้า Monitoring ซึ่งไม่ได้ scope ตามสาขาอยู่แล้วตั้งแต่ต้น)
    ใช้ตัวเดียวดึงข้อมูล RTU ทั้งหมดพร้อมกัน แทนยิง /alerts/status ทีละตัวเป็นร้อยๆ ครั้ง"""
    all_alerts = sc.get_all_records("SavedAlerts")
    return {
        a["RTUID"]: a["LinkedIncidentID"]
        for a in all_alerts
        if a.get("LinkedIncidentID") and a.get("RTUID")
    }


def get_alert_status_for_rtu(rtu_id: str) -> dict:
    """เช็คสถานะแจ้งเตือนของ RTU นี้ (ใช้แสดงใน popup ของ Monitoring) คืน dict เสมอ:
    - saved: มีแถว active อยู่ไหม (บันทึกไว้แล้ว ไม่ว่าจะแปลงเป็นเหตุการณ์หรือยัง)
    - alert_id / saved_at: ข้อมูลของแถว active ล่าสุด (ถ้า saved=True)
    - linked / incident_id: แถวนั้นถูกแปลงเป็นเหตุการณ์แล้วหรือยัง"""
    active = get_active_alert_for_rtu(rtu_id)
    if not active:
        return {"saved": False, "linked": False}

    result = {
        "saved": True,
        "alert_id": active["AlertID"],
        "saved_at": active.get("SavedAt", ""),
        "linked": bool(active.get("LinkedIncidentID")),
    }
    if result["linked"]:
        result["incident_id"] = active["LinkedIncidentID"]
    return result
