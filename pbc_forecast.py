# -*- coding: utf-8 -*-
"""
pbc_forecast.py — คำนวณเป้าหมายย่อยราย DMA และพยากรณ์อัตราน้ำสูญเสีย

โมดูลนี้เป็นคณิตศาสตร์ล้วน ไม่ยุ่งกับ Google Sheet หรือ Flask
เพื่อให้ทดสอบแยกได้และไม่ผูกกับแหล่งข้อมูล

หลักการสำคัญ 2 ข้อ
------------------
1) แปลงเป้า "อัตรา %" เป็นเป้า "ปริมาณ ลบ.ม." ต้องแก้สมการ ไม่ใช่คูณตรงๆ
   เพราะเมื่อซ่อมรั่วสำเร็จ ปริมาณน้ำเข้าจะลดลงตามไปด้วย ส่วนน้ำจำหน่ายเท่าเดิม

       อัตรา R = (น้ำเข้า - น้ำจำหน่าย) / น้ำเข้า
       ถ้าน้ำจำหน่าย S คงที่:  น้ำเข้า = S / (1 - R)
       ดังนั้น  ปริมาณสูญเสียเป้าหมาย = S * R / (1 - R)

   ถ้าใช้ "ปริมาณสูญเสียเป้าหมาย = น้ำเข้าเดิม * R" จะได้เป้าที่หลวมเกินจริง

2) กระจายเป้าลง DMA ตาม "ศักยภาพที่ลดได้จริง" ไม่ใช่เฉลี่ยเท่ากัน
   ศักยภาพ = (MNF ปัจจุบัน - พื้นค่า MNF ที่ DMA นั้นเคยทำได้) x ชั่วโมง x วัน
   DMA ที่ไม่มีข้อมูล MNF ใช้สัดส่วนปริมาณสูญเสียจาก WB220 แทน
"""

from math import sqrt

# น้ำหนักขั้นต่ำ กัน DMA ที่ศักยภาพเป็น 0 ทั้งหมดแล้วหารด้วยศูนย์
_EPS = 1e-9


# ------------------------------------------------------------------ เป้าหมายรวม

def area_target_loss(sales_m3, target_rate):
    """
    ปริมาณน้ำสูญเสียเป้าหมายของทั้งพื้นที่ (ลบ.ม.) ที่อัตราเป้าหมาย target_rate

    sales_m3    : ปริมาณน้ำจำหน่าย (ออกบิล + น้ำอื่นๆ) สมมติว่าคงที่
    target_rate : อัตราน้ำสูญเสียเป้าหมาย หน่วยเป็นสัดส่วน (0.375 = 37.5%)
    """
    if target_rate is None or target_rate <= 0:
        return 0.0
    if target_rate >= 1:
        return float("inf")
    return sales_m3 * target_rate / (1.0 - target_rate)


def loss_rate(inflow_m3, sales_m3):
    """อัตราน้ำสูญเสียเป็นสัดส่วน (0-1) คืน None ถ้าไม่มีน้ำเข้า"""
    if not inflow_m3:
        return None
    return (inflow_m3 - sales_m3) / inflow_m3


# ------------------------------------------------------------------ ศักยภาพราย DMA

def dma_potential(mnf_current, mnf_floor, hours_per_day, days):
    """
    ศักยภาพที่ DMA หนึ่งลดได้ (ลบ.ม. ต่อช่วง days วัน)

    คืน None ถ้าไม่มีข้อมูล MNF พอ — ผู้เรียกต้องใช้น้ำหนักสำรองแทน
    """
    if mnf_current is None or mnf_floor is None:
        return None
    gap = mnf_current - mnf_floor
    if gap <= 0:
        return 0.0
    return gap * hours_per_day * days


def build_weights(dmas, hours_per_day, days):
    """
    สร้างน้ำหนักการกระจายเป้าให้แต่ละ DMA

    dmas : list ของ dict อย่างน้อยมีคีย์
           dma_code, loss_m3, mnf_current, mnf_floor

    คืน list ของ dict เดิม + คีย์ potential_m3, weight, weight_source
      weight_source = "mnf"  ใช้ศักยภาพจาก MNF
                    = "loss" ไม่มีข้อมูล MNF ถอยไปใช้สัดส่วนปริมาณสูญเสีย
    """
    out = []
    for d in dmas:
        potential = dma_potential(
            d.get("mnf_current"), d.get("mnf_floor"), hours_per_day, days
        )
        if potential is None:
            weight = max(d.get("loss_m3") or 0.0, 0.0)
            source = "loss"
        else:
            weight = potential
            source = "mnf"
        row = dict(d)
        row["potential_m3"] = potential
        row["weight"] = weight
        row["weight_source"] = source
        out.append(row)

    # ถ้าทุกตัวน้ำหนักเป็นศูนย์ (เช่น MNF อยู่ที่พื้นค่าหมดแล้ว)
    # ถอยไปใช้สัดส่วนปริมาณสูญเสียทั้งชุด เพื่อไม่ให้หารด้วยศูนย์
    if sum(r["weight"] for r in out) <= _EPS:
        for r in out:
            r["weight"] = max(r.get("loss_m3") or 0.0, 0.0)
            r["weight_source"] = "loss"
    return out


# ------------------------------------------------------------------ กระจายเป้า

def allocate_reduction(dmas, total_reduction, manual=None, max_passes=12):
    """
    กระจายปริมาณที่ต้องลดทั้งพื้นที่ (total_reduction, ลบ.ม.) ลงแต่ละ DMA

    dmas   : ผลลัพธ์จาก build_weights() ต้องมี loss_m3 และ weight
    manual : dict {dma_code: target_loss_m3} เป้าที่ผู้ใช้กำหนดเอง (ล็อกไว้)
             ส่วนที่เหลือจะถูกเกลี่ยให้ DMA ที่ไม่ได้ล็อก

    คืน list ของ dict + คีย์ reduction_m3, target_loss_m3, is_manual

    การกระจายทำแบบวนซ้ำ เพราะบาง DMA อาจถูกจำกัดไม่ให้ลดเกินปริมาณสูญเสีย
    ที่มีอยู่จริง ส่วนที่เกินต้องถูกโยนไปให้ตัวอื่นรับแทน
    """
    manual = manual or {}
    rows = []
    for d in dmas:
        row = dict(d)
        code = row["dma_code"]
        loss = max(row.get("loss_m3") or 0.0, 0.0)
        row["loss_m3"] = loss
        if code in manual and manual[code] is not None:
            target = max(min(float(manual[code]), loss), 0.0)
            row["is_manual"] = True
            row["target_loss_m3"] = target
            row["reduction_m3"] = loss - target
        else:
            row["is_manual"] = False
            row["target_loss_m3"] = None
            row["reduction_m3"] = 0.0
        rows.append(row)

    manual_reduction = sum(r["reduction_m3"] for r in rows if r["is_manual"])
    remaining = max(total_reduction - manual_reduction, 0.0)

    free = [r for r in rows if not r["is_manual"]]
    for r in free:
        r["reduction_m3"] = 0.0

    for _ in range(max_passes):
        if remaining <= _EPS:
            break
        # เลือกเฉพาะตัวที่ยังรับเพิ่มได้
        pool = [r for r in free if r["reduction_m3"] < r["loss_m3"] - _EPS]
        total_weight = sum(max(r["weight"], 0.0) for r in pool)
        if not pool or total_weight <= _EPS:
            break
        spill = 0.0
        for r in pool:
            share = remaining * max(r["weight"], 0.0) / total_weight
            room = r["loss_m3"] - r["reduction_m3"]
            take = min(share, room)
            r["reduction_m3"] += take
            spill += share - take
        remaining = spill

    for r in free:
        r["target_loss_m3"] = max(r["loss_m3"] - r["reduction_m3"], 0.0)

    return rows


def target_mnf_for(row, hours_per_day, days):
    """
    แปลงปริมาณที่ต้องลดของ DMA หนึ่ง กลับเป็นเป้า MNF (ลบ.ม./ชม.)
    คืน None ถ้า DMA นั้นไม่มีข้อมูล MNF
    """
    mnf = row.get("mnf_current")
    if mnf is None:
        return None
    denom = hours_per_day * days
    if denom <= 0:
        return None
    target = mnf - (row.get("reduction_m3") or 0.0) / denom
    floor = row.get("mnf_floor")
    if floor is not None:
        target = max(target, floor)
    return max(target, 0.0)


def build_dma_targets(dmas, sales_m3, target_rate, hours_per_day,
                      days, manual=None):
    """
    ฟังก์ชันรวม: จากสถานะปัจจุบันราย DMA + อัตราเป้าหมาย
    คืนเป้าปริมาณและเป้า MNF ของแต่ละ DMA พร้อมสรุปภาพรวม

    dmas ต้องมีคีย์: dma_code, inflow_m3, sales_m3, loss_m3,
                     mnf_current, mnf_floor
    """
    total_loss = sum(max(d.get("loss_m3") or 0.0, 0.0) for d in dmas)
    total_target_loss = area_target_loss(sales_m3, target_rate)
    total_reduction = max(total_loss - total_target_loss, 0.0)

    weighted = build_weights(dmas, hours_per_day, days)
    allocated = allocate_reduction(weighted, total_reduction, manual=manual)

    for row in allocated:
        row["target_mnf"] = target_mnf_for(row, hours_per_day, days)

    allocated_reduction = sum(r["reduction_m3"] for r in allocated)
    # ผลรวมเป้าจริงของทุก DMA อาจไม่เท่ากับเป้าตามสูตร
    # เมื่อผู้ใช้ล็อกเป้าบางตัวไว้ต่ำกว่าที่จำเป็น (ตั้งเป้าเกินความจำเป็น)
    # จึงต้องรายงานทั้งสองค่า ไม่ใช่ค่าเดียว
    sum_target_loss = sum(r["target_loss_m3"] for r in allocated)
    total_inflow = sales_m3 + sum_target_loss
    summary = {
        "total_loss_m3": total_loss,
        "total_target_loss_m3": total_target_loss,
        "sum_target_loss_m3": sum_target_loss,
        "resulting_rate": (
            sum_target_loss / total_inflow * 100.0 if total_inflow else None
        ),
        "total_reduction_m3": total_reduction,
        "allocated_reduction_m3": allocated_reduction,
        "unallocated_m3": max(total_reduction - allocated_reduction, 0.0),
        "target_rate": target_rate,
        "sales_m3": sales_m3,
    }
    return allocated, summary


# ------------------------------------------------------------------ พยากรณ์

def _weighted_linreg(points):
    """
    ถดถอยเชิงเส้นถ่วงน้ำหนักให้ข้อมูลใหม่มีน้ำหนักมากกว่า
    points : list ของ (x, y) เรียงตาม x
    คืน (slope, intercept, residual_sd)
    """
    n = len(points)
    weights = [1.0 + i for i in range(n)]  # จุดล่าสุดมีน้ำหนักสูงสุด
    sw = sum(weights)
    mx = sum(w * x for w, (x, _) in zip(weights, points)) / sw
    my = sum(w * y for w, (_, y) in zip(weights, points)) / sw
    sxx = sum(w * (x - mx) ** 2 for w, (x, _) in zip(weights, points))
    sxy = sum(w * (x - mx) * (y - my) for w, (x, y) in zip(weights, points))
    slope = sxy / sxx if sxx > _EPS else 0.0
    intercept = my - slope * mx
    if n > 2:
        resid = [y - (slope * x + intercept) for x, y in points]
        sd = sqrt(sum(r ** 2 for r in resid) / (n - 2))
    else:
        sd = 0.0
    return slope, intercept, sd


def forecast_rate(series, horizon_months, confidence_k=1.96,
                  floor=0.0, ceiling=100.0, band_width_limit=10.0):
    """
    พยากรณ์อัตราน้ำสูญเสียล่วงหน้า

    series          : list ของ (month_no, rate_percent) เรียงตามเดือน
    horizon_months  : list ของเลขเดือนที่ต้องการค่าพยากรณ์
    floor / ceiling : ขอบเขตที่เป็นไปได้จริงของอัตราน้ำสูญเสีย (0-100%)
    band_width_limit: ถ้าช่วงความเชื่อมั่นกว้างเกินนี้ ถือว่าสรุปอะไรไม่ได้

    คืน dict {
        "level": "none" | "low" | "normal",
        "message": ข้อความอธิบายระดับความเชื่อมั่น,
        "points": [{month_no, rate, lo, hi, conclusive}, ...],
        "slope_per_month": อัตราการเปลี่ยนแปลงต่อเดือน (จุดเปอร์เซ็นต์),
        "max_month_no": เดือนไกลสุดที่ยอมพยากรณ์,
    }

    ข้อจำกัด 3 ชั้นที่จำเป็น เพราะการลากเส้นตรงจากข้อมูลไม่กี่จุดไปไกลๆ
    ให้ค่าที่เป็นไปไม่ได้ทางกายภาพ (เคยได้ -25% ตอนมีข้อมูล 3 จุด)
      1) ไม่พยากรณ์ไกลเกิน 2 เท่าของจำนวนรอบข้อมูลที่มี
      2) บีบค่าให้อยู่ในช่วง 0-100% ทั้งเส้นกลางและขอบช่วง
      3) ถ้าช่วงความเชื่อมั่นกว้างเกินเกณฑ์ ติดธงว่ายังสรุปไม่ได้
    """
    clean = [(int(m), float(v)) for m, v in series if v is not None]
    clean.sort(key=lambda p: p[0])
    n = len(clean)

    if n < 3:
        return {
            "level": "none",
            "message": "ข้อมูลยังไม่พอสำหรับพยากรณ์ (มี %d รอบ ต้องการอย่างน้อย 3)" % n,
            "points": [],
            "slope_per_month": None,
            "max_month_no": None,
        }

    slope, intercept, sd = _weighted_linreg(clean)
    if n < 6:
        level = "low"
        message = "ความเชื่อมั่นต่ำ — มีข้อมูลเพียง %d รอบ ค่าพยากรณ์อาจเปลี่ยนได้มาก" % n
        widen = 2.0
    else:
        level = "normal"
        message = "พยากรณ์จากข้อมูล %d รอบวัดผล" % n
        widen = 1.0

    last_x = clean[-1][0]
    # ยิ่งมีข้อมูลน้อย ยิ่งพยากรณ์ไปได้ไม่ไกล
    max_ahead = max(3, n * 2)
    max_month = last_x + max_ahead

    def clamp(value):
        return max(floor, min(ceiling, value))

    points = []
    truncated = False
    for m in sorted(horizon_months):
        if m <= last_x:
            continue
        if m > max_month:
            truncated = True
            continue
        value = slope * m + intercept
        # ช่วงกว้างขึ้นตามระยะที่พยากรณ์ออกไป
        step = max(m - last_x, 1)
        margin = confidence_k * widen * max(sd, 0.15) * sqrt(step)
        lo, hi = clamp(value - margin), clamp(value + margin)
        points.append({
            "month_no": m,
            "rate": round(clamp(value), 2),
            "lo": round(lo, 2),
            "hi": round(hi, 2),
            "conclusive": (hi - lo) <= band_width_limit,
        })

    if truncated:
        message += " · พยากรณ์ได้ถึงเดือนที่ %d เท่านั้น " \
                   "(ไกลกว่านี้ต้องมีข้อมูลเพิ่ม)" % max_month

    return {
        "level": level,
        "message": message,
        "points": points,
        "slope_per_month": round(slope, 3),
        "max_month_no": max_month,
    }


def gap_to_target(forecast, target_rate_pct, month_no):
    """
    เทียบค่าพยากรณ์ ณ เดือนวัดผลกับเป้า คืน dict สรุปให้แสดงบนหน้าจอ
    ค่าบวกใน gap = ยังสูงกว่าเป้า (ยังไม่ผ่าน)
    """
    for p in forecast.get("points", []):
        if p["month_no"] == month_no:
            return {
                "month_no": month_no,
                "forecast_rate": p["rate"],
                "lo": p.get("lo"),
                "hi": p.get("hi"),
                "target_rate": target_rate_pct,
                "gap": round(p["rate"] - target_rate_pct, 2),
                "on_track": p["rate"] <= target_rate_pct,
                # ถ้าช่วงความเชื่อมั่นกว้างจนคร่อมเส้นเป้า จะบอกว่าผ่านหรือไม่ผ่านไม่ได้
                "conclusive": p.get("conclusive", True)
                and not (p.get("lo") <= target_rate_pct <= p.get("hi")),
                "level": forecast.get("level"),
            }
    return None


def interim_target_line(start_rate, milestones, last_month_no):
    """
    สร้างเส้นเป้าหมายรายเดือนระหว่างทาง (ไม่ผูกพันสัญญา)
    ลากเชิงเส้นระหว่างจุดวัดผลตามสัญญา

    start_rate  : อัตราฐาน X (เปอร์เซ็นต์)
    milestones  : list ของ (month_no, target_rate) เรียงตามเดือน
    คืน list ของ (month_no, rate) ทุกเดือนตั้งแต่ 1 ถึง last_month_no
    """
    anchors = [(1, float(start_rate))] + [
        (int(m), float(r)) for m, r in sorted(milestones)
    ]
    line = []
    for month in range(1, int(last_month_no) + 1):
        prev = anchors[0]
        nxt = None
        for a in anchors:
            if a[0] <= month:
                prev = a
            else:
                nxt = a
                break
        if nxt is None:
            line.append((month, prev[1]))
        else:
            span = nxt[0] - prev[0]
            frac = (month - prev[0]) / span if span else 0.0
            line.append((month, prev[1] + (nxt[1] - prev[1]) * frac))
    return [(m, round(v, 3)) for m, v in line]
