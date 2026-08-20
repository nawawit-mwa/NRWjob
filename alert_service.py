"""
alert_service.py
บันทึกการแจ้งเตือน MNF ที่สนใจจากหน้า NRW Monitoring ไว้ดูภายหลัง + แปลงเป็นเหตุการณ์ได้

State/logic (สรุปตามที่ตกลงกันไว้ก่อน coding):
- บันทึกได้เฉพาะ user ที่ login แล้ว (บังคับที่ route ด้วย login_required)
- เห็นรายการที่บันทึกไว้ตามขอบเขตพื้นที่เดียวกับ Incidents (BranchID -> BranchGroupID)
- แปลงเป็นเหตุการณ์ได้เฉพาะหัวหน้าส่วนขึ้นไป (สิทธิ์เดียวกับแปลง Incident -> Job) + Admin
- แปลงซ้ำไม่ได้ถ้ามี LinkedIncidentID อยู่แล้ว
"""

import datetime

import sheets_client as sc
from auth_service import role_level
from constants import (
    ROLE_ADMIN, ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR,
    ROLE_LEVELS, ROLE_SECTION_CHIEF,
)


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def save_alert(user: dict, rtu_id: str, rtu_name: str, case_classification: str,
               mnf_value: str, cusum: str, trend_result: str, note: str = "",
               image_data_url: str = "") -> str:
    """บันทึกการแจ้งเตือน MNF ที่สนใจ — resolve ZoneID/BranchID จาก RTUID อัตโนมัติ
    (แบบเดียวกับ incident_service.create_incident ตอนสร้างจาก RTU)"""
    rtu = sc.find_one("RTUs", "RTUID", rtu_id)
    zone_id = rtu.get("ZoneID", "") if rtu else ""
    branch_id = rtu.get("BranchID", "") if rtu else ""

    chart_image_url = ""
    if image_data_url:
        try:
            import drive_client
            chart_image_url = drive_client.upload_chart_image(
                image_data_url, f"alert_{rtu_id}_{_now()}.jpg"
            )
        except Exception as e:
            # อัปโหลดรูปไม่สำเร็จ (เช่น โควตา Drive/เน็ตหลุด/ยังไม่เปิด Drive API ใน Google Cloud Project)
            # ไม่ควรทำให้บันทึกทั้งรายการล้มเหลวไปด้วย — บันทึกข้อมูลอื่นต่อไปตามปกติ แค่ไม่มีรูปแนบ
            # แต่ต้อง log ไว้ให้เห็น ไม่งั้นไล่บั๊กไม่ได้เลยว่าทำไมไม่มีรูป (เจอปัญหานี้มาแล้วจริงจากการใช้งานจริง)
            print(f"[alert_service] อัปโหลดรูปกราฟไม่สำเร็จสำหรับ RTU={rtu_id}: {type(e).__name__}: {e}")
            chart_image_url = ""

    alert_id = sc.next_id("SavedAlerts", "AlertID", "ALT-")
    sc.append_row("SavedAlerts", {
        "AlertID": alert_id,
        "RTUID": rtu_id,
        "RTUName": rtu_name,
        "ZoneID": zone_id,
        "BranchID": branch_id,
        "CaseClassification": case_classification,
        "MNFValue": mnf_value,
        "CUSUM": cusum,
        "TrendResult": trend_result,
        "ChartImageURL": chart_image_url,
        "Note": note,
        "SavedBy": user["UserID"],
        "SavedAt": _now(),
        "LinkedIncidentID": "",
        "LinkedIncidentAt": "",
    })
    return alert_id


def get_alerts_for_user(user: dict) -> list:
    """คืนรายการแจ้งเตือนที่บันทึกไว้ ในขอบเขตพื้นที่ของ user (ตรรกะเดียวกับ dashboard_service
    ใช้กับ Incidents — Admin เห็นหมด, รองผู้ว่าการ/ผู้ช่วยผู้ว่าการ เห็นตามกลุ่มสาขา, ที่เหลือเห็นตามสาขาตน)"""
    all_alerts = sc.get_all_records("SavedAlerts")
    role = user.get("Role")

    if role == ROLE_ADMIN:
        return all_alerts

    if role in (ROLE_DEPUTY_GOVERNOR, ROLE_ASSISTANT_GOVERNOR):
        branch_group_id = user.get("BranchGroupID")
        branches = sc.find_many("Branches", "BranchGroupID", branch_group_id)
        branch_ids = {b["BranchID"] for b in branches}
        return [a for a in all_alerts if a.get("BranchID") in branch_ids]

    branch_id = user.get("BranchID")
    if branch_id:
        return [a for a in all_alerts if a.get("BranchID") == branch_id]

    return []


def get_alert_permissions(alert: dict, user: dict) -> dict:
    """เช็คว่า user คนนี้แปลงแจ้งเตือนนี้เป็นเหตุการณ์ได้ไหม
    สิทธิ์: หัวหน้าส่วนขึ้นไป + Admin — แปลงซ้ำไม่ได้ถ้ามี LinkedIncidentID อยู่แล้ว"""
    role = user.get("Role")
    is_admin = role == ROLE_ADMIN
    already_linked = bool(alert.get("LinkedIncidentID"))
    can_convert = (not already_linked) and (is_admin or role_level(role) <= ROLE_LEVELS[ROLE_SECTION_CHIEF])
    return {"can_convert": can_convert, "already_linked": already_linked}


def convert_alert_to_incident(alert_id: str, user: dict) -> str:
    """แปลงแจ้งเตือนที่บันทึกไว้เป็นเหตุการณ์จริง แล้วเขียน LinkedIncidentID กลับเข้า SavedAlerts"""
    import incident_service  # import ในฟังก์ชัน กัน circular import (incident_service ไม่ต้องรู้จัก alert_service)

    alert = sc.find_one("SavedAlerts", "AlertID", alert_id)
    if not alert:
        raise ValueError("ไม่พบรายการแจ้งเตือนนี้")

    perms = get_alert_permissions(alert, user)
    if not perms["can_convert"]:
        if perms["already_linked"]:
            raise ValueError(f"แจ้งเตือนนี้ถูกแปลงเป็นเหตุการณ์ {alert['LinkedIncidentID']} ไปแล้ว")
        raise PermissionError("ไม่มีสิทธิ์แปลงแจ้งเตือนนี้เป็นเหตุการณ์ (ต้องเป็นหัวหน้าส่วนขึ้นไป)")

    description_parts = [
        f"บันทึกจากระบบ NRW Monitoring — RTU: {alert.get('RTUName', alert.get('RTUID', ''))}",
        f"MNF: {alert.get('MNFValue', '—')} · CUSUM: {alert.get('CUSUM', '—')} · เทรนด์: {alert.get('TrendResult', '—')}",
    ]
    if alert.get("Note"):
        description_parts.append(f"หมายเหตุ: {alert['Note']}")
    if alert.get("ChartImageURL"):
        description_parts.append(f"กราฟ ณ ตอนบันทึก: {alert['ChartImageURL']}")

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


def get_alert_status_for_rtu(rtu_id: str) -> dict:
    """เช็คว่า RTU นี้เคยถูกบันทึก+แปลงเป็นเหตุการณ์แล้วหรือยัง (ใช้แสดงใน popup ของ Monitoring)
    คืนรายการล่าสุดที่มี LinkedIncidentID เท่านั้น (ถ้ามีหลายรายการ เอาที่บันทึกล่าสุดก่อน) หรือ None ถ้าไม่มี"""
    alerts = sc.find_many("SavedAlerts", "RTUID", rtu_id)
    linked = [a for a in alerts if a.get("LinkedIncidentID")]
    if not linked:
        return None
    linked.sort(key=lambda a: a.get("SavedAt", ""), reverse=True)
    return linked[0]
