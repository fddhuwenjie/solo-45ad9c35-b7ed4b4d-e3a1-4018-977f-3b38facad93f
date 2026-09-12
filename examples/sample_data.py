"""样例数据构造工厂：直接放行 / 缺陷扩检 / 二次返修 三套本地样例。

时间轴约定（UTC）：
- 资质窗口覆盖 2026 全年；
- 施焊 2026-03 上旬；
- 原始 RT 紧随其后；返修与复检按周递进。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.schemas import (
    AngleBand,
    BakeCycle,
    GaugeUse,
    PassRecord,
    TemperatureMeasurement,
    WeldGaugeVersion,
    WpsProcessWindow,
    ConsumableBatch,
    ConsumableContainerVersion,
    ConsumableIssueSegment,
    ConsumableRule,
    ConsumableSegmentEvent,
    ConsumableUse,
    HeatEnd,
    LotRule,
    NdeEquipmentUse,
    NdeEquipmentVersion,
    NdePersonnelCert,
    NdeRecord,
    QuiverStay,
    RepairRecord,
    SubmissionPayload,
    TemperatureReading,
    WelderQual,
    WeldRecord,
    WpsRecord,
)

GROUP = "Fe-1"


# ============================================================ NDT 资源（默认有效）
#
# 默认样例：RT-II 张工 实施、RT-II 李工 复核；X 射线机 XR-250 配胶片包
# FILM-T2，校准与证书均覆盖 2026 全年。夜班跨到期点等失效场景在测试中构造。


def ndt_cert(no: str, name: str, method: str = "RT", level: str = "II",
             *, products=("pressure_pipe",),
             techniques=("film", "digital"),
             valid_from="2026-01-01T00:00:00Z",
             valid_to="2026-12-31T23:59:59Z"):
    return NdePersonnelCert(
        cert_no=no, name=name, method=method, level=level,
        products=list(products), techniques=list(techniques),
        valid_from=valid_from, valid_to=valid_to,
    )


def ndt_equipment(eid: str, version: str, name: str, kind: str,
                  method: str = "RT", *, serial: str | None = None,
                  calibrated_from="2026-01-01T00:00:00Z",
                  calibrated_to="2026-12-31T23:59:59Z",
                  techniques=("film", "digital"),
                  range_min=None, range_max=None,
                  energy_min=20.0, energy_max=250.0,
                  isotope=None, uses=()):
    return NdeEquipmentVersion(
        equipment_id=eid, version=version, name=name, kind=kind,
        method=method, serial_no=serial or f"SN-{eid}",
        calibrated_from=calibrated_from, calibrated_to=calibrated_to,
        techniques=list(techniques),
        range_min_mm=range_min, range_max_mm=range_max,
        energy_min_kev=energy_min, energy_max_kev=energy_max,
        source_isotope=isotope,
        uses=[NdeEquipmentUse(**u) for u in uses],
    )


def default_personnel():
    """两张不同证号的 RT-II 证书（实施/复核不可同人）。"""
    return [
        ndt_cert("NRT-II-ZHANG", "NDE-II-张"),
        ndt_cert("NRT-II-LI", "NDE-II-李"),
    ]


def default_equipment():
    """X 射线机（主设备）+ T2 胶片包（关键附件）。"""
    film = ndt_equipment("FILM-T2", "2026A", "工业射线胶片 T2", "film",
                         serial="SN-FILM-T2",
                         energy_min=None, energy_max=None)
    xray = ndt_equipment(
        "XR-250", "2026A", "250kV 便携式 X 射线机", "xray_source",
        serial="SN-XR-250-07", energy_min=20.0, energy_max=250.0,
        uses=[{"equipment_id": "FILM-T2", "version": "2026A", "role": "film"}],
    )
    return [xray, film]


def default_rt_uses():
    return [NdeEquipmentUse(equipment_id="XR-250", version="2026A", role="main")]


def wps_process_window(process: str = "GTAW", **kw):
    """单方法道次参数窗口工厂（极性/电流/电压/焊速/热输入/预热/层间温度）。"""
    defaults = {
        "GTAW": dict(polarity=["DCEN"], current=(80, 130), voltage=(9, 14),
                     travel=(50, 160), heat_max=2.5, k=0.75),
        "SMAW": dict(polarity=["DCEP"], current=(100, 160), voltage=(20, 26),
                     travel=(50, 150), heat_max=2.5, k=0.8),
    }[process]
    cur = kw.pop("current", defaults["current"])
    vol = kw.pop("voltage", defaults["voltage"])
    travel = kw.pop("travel", defaults["travel"])
    heat = kw.pop("heat", (None, defaults["heat_max"]))
    base = dict(
        process=process,
        polarity=list(defaults["polarity"]),
        current_min_a=cur[0], current_max_a=cur[1],
        voltage_min_v=vol[0], voltage_max_v=vol[1],
        travel_min_mm_min=travel[0], travel_max_mm_min=travel[1],
        heat_input_min_kj_mm=heat[0], heat_input_max_kj_mm=heat[1],
        thermal_efficiency=defaults["k"],
        preheat_min_c=100.0, preheat_lead_minutes=60.0,
        interpass_min_c=80.0, interpass_max_c=200.0,
    )
    base.update(kw)
    return WpsProcessWindow(**base)


def wps(no: str = "WPS-101", groups=("Fe-1",), tmin=3.0, tmax=20.0,
        dmin=25.0, dmax=600.0, processes=("GTAW", "SMAW"),
        positions=("5G", "6G"), supported=True,
        consumable_classes=("E5015",), with_windows=False):
    return WpsRecord(
        wps_no=no,
        pqr_no="PQR-101",
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-12-31T23:59:59Z",
        material_groups=list(groups),
        thickness_min_mm=tmin,
        thickness_max_mm=tmax,
        diameter_min_mm=dmin,
        diameter_max_mm=dmax,
        processes=list(processes),
        positions=list(positions),
        supported_by_pqr=supported,
        consumable_classes=list(consumable_classes),
        process_windows=([wps_process_window("GTAW"),
                          wps_process_window("SMAW")]
                         if with_windows else []),
    )


def welder(wid: str = "W-001", groups=("Fe-1",), tmin=3.0, tmax=20.0,
           dmin=25.0, dmax=600.0, processes=("GTAW", "SMAW"),
           positions=("5G", "6G"), **kw):
    return WelderQual(
        welder_id=wid,
        stamp=f"S-{wid[-3:]}",
        valid_from=kw.get("valid_from", "2026-01-01T00:00:00Z"),
        valid_to=kw.get("valid_to", "2026-12-31T23:59:59Z"),
        material_groups=list(groups),
        thickness_min_mm=tmin,
        thickness_max_mm=tmax,
        diameter_min_mm=dmin,
        diameter_max_mm=dmax,
        processes=list(processes),
        positions=list(positions),
    )


def end(side: str, heat: str, *, diameter=114.3, thickness=8.18, group=GROUP):
    return HeatEnd(
        side=side,
        heat_no=heat,
        material_group=group,
        nominal_diameter_mm=diameter,
        thickness_mm=thickness,
    )


def weld(no: str, heat_a: str, heat_b: str, *, day: int = 1,
         welder_id="W-001", wps_no="WPS-101", lot_id="LOT-A",
         line="PL-100", diameter=114.3, thickness=8.18, process="GTAW+SMAW",
         position="6G"):
    return WeldRecord(
        weld_no=no,
        line_no=line,
        end_a=end("a", heat_a, diameter=diameter, thickness=thickness),
        end_b=end("b", heat_b, diameter=diameter, thickness=thickness),
        weld_process=process,
        weld_position=position,
        welded_at=f"2026-03-{day:02d}T09:00:00Z",
        welder_id=welder_id,
        wps_no=wps_no,
        lot_id=lot_id,
    )


def rt(no: str, weld_no: str, result: str, *, day: int, hour=14,
       bands=((0, 360),), defects=(), iteration=0, lot_id=None,
       method="RT", full=False, examiner="NDE-II-张",
       examiner_cert="NRT-II-ZHANG", reviewer_cert="NRT-II-LI",
       equipment_uses=None, technique="film",
       applied_thickness_mm=None, exposure_energy_kev=160.0,
       duration_hours=2, start_hour=None, start_day=None):
    """RT 检测单工厂；默认资源（人员证书/设备版本）全年有效。

    duration_hours>0 时生成 [examined-duration, examined] 的实施时段，
    start_hour/start_day 可显式指定跨零点夜班的起点。
    """
    examined = f"2026-03-{day:02d}T{hour:02d}:00:00Z"
    if start_hour is not None:
        sday = start_day if start_day is not None else day
        started = f"2026-03-{sday:02d}T{start_hour:02d}:00:00Z"
    elif duration_hours:
        from datetime import datetime, timedelta, timezone
        t0 = datetime.strptime(examined, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc) - timedelta(hours=duration_hours)
        started = t0.strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        started = examined
    uses = equipment_uses if equipment_uses is not None else (
        default_rt_uses() if method == "RT" else [])
    return NdeRecord(
        nde_id=no,
        weld_no=weld_no,
        method=method,
        result=result,
        examined_at=examined,
        started_at=started,
        finished_at=examined,
        examiner=examiner,
        examiner_cert_no=examiner_cert,
        reviewer_cert_no=reviewer_cert,
        equipment_uses=uses,
        technique=technique,
        applied_thickness_mm=applied_thickness_mm,
        exposure_energy_kev=exposure_energy_kev if method == "RT" else None,
        lot_id=lot_id,
        coverage=[
            AngleBand(start_deg=a, end_deg=b, full_circle=full)
            for a, b in bands
        ],
        defect_locations=[AngleBand(start_deg=a, end_deg=b) for a, b in defects],
        iteration=iteration,
        report_no=f"RT-2026-{no}",
    )


def repair(no: str, weld_no: str, iteration: int, band, *, day: int,
           welder_id="W-001", wps_no="WPS-101", approved=True,
           approver="焊接责任工程师-李"):
    return RepairRecord(
        repair_id=no,
        weld_no=weld_no,
        iteration=iteration,
        excavated_band=AngleBand(start_deg=band[0], end_deg=band[1]),
        repaired_at=f"2026-03-{day:02d}T10:00:00Z",
        welder_id=welder_id,
        wps_no=wps_no,
        approved=approved,
        approved_by=approver if approved else None,
    )


def lot(rule_id="LOT-A", ratio=0.2, on_reject="double", double_step=0.2,
        method="RT", min_samples=1):
    return LotRule(
        lot_id=rule_id,
        required_method=method,
        sample_ratio=ratio,
        on_reject=on_reject,
        double_step=double_step,
        min_samples=min_samples,
    )


# ============================================================ 焊材批次与烘干领用链
#
# 低氢碱性焊条 E5015（J507）：2/28 早 5:00 入烘箱 350~400℃ 恒温 1h，
# 5 分钟内转入 100~150℃ 保温筒；每个施工日 08:00 开一段领用，
# 焊口/返修挂实际消耗，18:00 退回余量闭合数量。暴露上限 4h
# （焊口 09:00、补焊 10:00，均在限内）。温度读数每 2h 一条（缺口限 3h）。


def consumable_rule(classification="E5015", **kw):
    defaults = dict(
        classification=classification,
        bake_temp_min_c=350.0, bake_temp_max_c=400.0,
        min_soak_minutes=60.0,
        holding_temp_min_c=100.0, holding_temp_max_c=150.0,
        max_exposure_minutes=240.0,
        max_bake_cycles=2, max_transfer_minutes=15.0,
        max_log_gap_minutes=180.0,
    )
    defaults.update(kw)
    return ConsumableRule(**defaults)


def consumable_batch(batch_id="WM-E5015-2602", classification="E5015",
                     lot_no="LOT-J507-260201", received_qty=50.0,
                     applicable=("WPS-101",), **kw):
    return ConsumableBatch(
        batch_id=batch_id,
        classification=classification,
        designation=kw.get("designation", "J507"),
        manufacturer=kw.get("manufacturer", "某焊材厂"),
        manufacturer_lot_no=lot_no,
        cert_no=kw.get("cert_no", f"MTC-{lot_no}"),
        cert_lot_no=kw.get("cert_lot_no", lot_no),
        received_status=kw.get("received_status", "accepted"),
        received_qty_kg=received_qty,
        applicable_wps=list(applicable),
    )


def consumable_containers():
    """烘箱 OV-1 与保温筒 QV-1，校准覆盖 2026 全年。"""
    return [
        ConsumableContainerVersion(
            container_id="OV-1", version="2026A", name="电焊条烘干箱",
            kind="oven", serial_no="SN-OV-1",
            calibrated_from="2026-01-01T00:00:00Z",
            calibrated_to="2026-12-31T23:59:59Z"),
        ConsumableContainerVersion(
            container_id="QV-1", version="2026A", name="焊工保温筒",
            kind="quiver", serial_no="SN-QV-1",
            calibrated_from="2026-01-01T00:00:00Z",
            calibrated_to="2026-12-31T23:59:59Z"),
    ]


def _readings(start, end, *, hours_step, temp):
    from datetime import datetime as _dt, timedelta as _td
    out = []
    t = start
    while t <= end:
        out.append(TemperatureReading(at=t, temp_c=temp))
        t = t + _td(hours=hours_step)
    if out[-1].at < end:
        out.append(TemperatureReading(at=end, temp_c=temp))
    return out


def valid_consumable_chain(welds, repairs=(), *, batch_id="WM-E5015-2602",
                           rule=None, batch=None, containers=None,
                           use_qty_weld=0.2, use_qty_repair=0.1,
                           issue_weld_uses: dict | None = None,
                           reserve_kg: float = 0.5):
    """按焊口/返修的日期自动构造一条合规焊材链。

    每段领用领出量 = 当日实际消耗 + reserve_kg，余量 18:00 退回，保证
    数量恰好闭合（裁剪返修/焊口后，段仍按裁剪后的消耗量构造，不出现 WM-QTY-OPEN）。
    返回 dict(batches, rules, containers, bake_cycles, quiver_stays,
              segments, events) 及已挂到 welds/repairs 上的消耗。
    issue_weld_uses: 可选 {weld_no: qty} 覆盖逐口消耗量。
    """
    from datetime import datetime as _dt, timedelta as _td

    rule = rule or consumable_rule()
    batch = batch or consumable_batch(batch_id=batch_id,
                                      classification=rule.classification)
    containers = containers if containers is not None else consumable_containers()

    def _day(w: WeldRecord) -> int:
        return w.welded_at.day

    # 每个有施工活动（焊口施焊或补焊）的日期开一段领用
    days: dict[int, dict] = {}
    for w in welds:
        days.setdefault(_day(w), {"welds": [], "repairs": []})["welds"].append(w)
    for r in repairs:
        d = r.repaired_at.day
        days.setdefault(d, {"welds": [], "repairs": []})["repairs"].append(r)

    first_day = min(days)
    last_day = max(days)

    # 烘干：首个施工日 04:00 装炉（首日前一天无 2/30 时落在当日凌晨），
    # 入炉即按 380℃ 恒温 1h（04:00/04:30/05:00 读数），取出后 5 分钟内转保温
    if first_day > 1:
        bake_loaded = _dt(2026, 3, first_day - 1, 5, 0, tzinfo=timezone.utc)
    else:
        bake_loaded = _dt(2026, 3, 1, 4, 0, tzinfo=timezone.utc)
    bake_unloaded = bake_loaded + _td(hours=1)
    bake_readings = [
        TemperatureReading(at=bake_loaded, temp_c=380.0),
        TemperatureReading(at=bake_loaded + _td(minutes=30), temp_c=380.0),
        TemperatureReading(at=bake_unloaded, temp_c=380.0),
    ]
    bake = BakeCycle(
        bake_id="BK-1", batch_id=batch_id, cycle_no=1,
        oven_id="OV-1", oven_version="2026A",
        loaded_at=bake_loaded, unloaded_at=bake_unloaded,
        loaded_qty_kg=batch.received_qty_kg,
        readings=bake_readings,
    )

    # 保温：5 分钟内转入保温筒，覆盖到最后一个施工日 19:00（退回事件之后）
    stay_loaded = bake_unloaded + _td(minutes=5)
    stay_end = _dt(2026, 3, last_day, 19, 0, tzinfo=timezone.utc)
    stay_readings = _readings(stay_loaded, stay_end, hours_step=2, temp=120.0)
    stay = QuiverStay(
        stay_id="ST-1", batch_id=batch_id, bake_id="BK-1",
        quiver_id="QV-1", quiver_version="2026A",
        loaded_at=stay_loaded, unloaded_at=stay_end,
        loaded_qty_kg=batch.received_qty_kg,
        readings=stay_readings,
    )

    segments, events = [], []
    use_seq = 0
    for day in sorted(days):
        seg_id = f"SG-{day:02d}"
        issued_at = _dt(2026, 3, day, 8, 0, tzinfo=timezone.utc)
        day_welds = days[day]["welds"]
        day_repairs = days[day]["repairs"]
        used = 0.0
        for w in day_welds:
            q = (issue_weld_uses or {}).get(w.weld_no, use_qty_weld)
            use_seq += 1
            w.consumables.append(ConsumableUse(
                use_id=f"CU-{use_seq:03d}", batch_id=batch_id,
                segment_id=seg_id, qty_kg=q,
                used_at=w.welded_at))
            used += q
        for r in sorted(day_repairs, key=lambda x: x.iteration):
            use_seq += 1
            r.consumables.append(ConsumableUse(
                use_id=f"CU-{use_seq:03d}", batch_id=batch_id,
                segment_id=seg_id, qty_kg=use_qty_repair,
                used_at=r.repaired_at))
            used += use_qty_repair
        # 当日领出 = 实际消耗 + 预留余量，未消耗余量 18:00 退回，数量恰好闭合
        issued_qty = round(used + reserve_kg, 3)
        returned = reserve_kg
        segments.append(ConsumableIssueSegment(
            segment_id=seg_id, batch_id=batch_id, stay_id="ST-1",
            issued_at=issued_at, issued_qty_kg=issued_qty,
            issued_to="焊工班组"))
        events.append(ConsumableSegmentEvent(
            event_id=f"EV-R{day:02d}", segment_id=seg_id,
            event_type="return",
            at=_dt(2026, 3, day, 18, 0, tzinfo=timezone.utc),
            qty_kg=returned))

    return {
        "consumable_batches": [batch],
        "consumable_rules": [rule],
        "consumable_containers": containers,
        "bake_cycles": [bake],
        "quiver_stays": [stay],
        "consumable_segments": segments,
        "consumable_events": events,
    }


# ============================================================ 焊接道次执行链
#
# 每道焊口默认三道：GTAW 打底（根焊）+ SMAW 填充 + SMAW 盖面；
# 道次时段排在同日 09:00 完工时刻之前（同日多口按 2 小时槽位错开，
# 同一焊工不跨口时段重叠）。每道起弧前有预热/层间测温，仪表校准全年有效。
# 焊速 v=焊缝长度/燃弧时间，热输入 E=k·U·I·60/v，默认参数全部落在 WPS 窗口。


def weld_gauges(*, calibrated_to="2026-12-31T23:59:59Z"):
    """默认：焊接监测仪 WM-1（电流/电压/焊速）+ 测温仪 TM-1，校准覆盖全年。"""
    return [
        WeldGaugeVersion(
            gauge_id="WM-1", version="2026A", name="焊接参数监测仪",
            kind="weld_monitor", serial_no="SN-WM-1",
            calibrated_from="2026-01-01T00:00:00Z",
            calibrated_to=calibrated_to),
        WeldGaugeVersion(
            gauge_id="TM-1", version="2026A", name="表面测温仪",
            kind="thermometer", serial_no="SN-TM-1",
            calibrated_from="2026-01-01T00:00:00Z",
            calibrated_to=calibrated_to),
    ]


def _dt(day, hh, mm):
    return datetime(2026, 3, day, hh, mm, tzinfo=timezone.utc)


# 默认三道：(层号, 方法, 极性, 电流A, 电压V, 长度mm, 燃弧min,
#           起弧距完工分钟, 时长min)
_DEFAULT_PASSES = [
    (1, "GTAW", "DCEN", 95.0, 11.5, 359.0, 4.0, 30, 10),
    (2, "SMAW", "DCEP", 120.0, 22.0, 359.0, 4.5, 19, 7),
    (3, "SMAW", "DCEP", 125.0, 22.0, 359.0, 3.5, 9, 5),
]
# 测温：(对应道次, 类型, 测温距完工分钟, 温度℃)
# 测温时点须落在 上一道收弧后、本道起弧前：
# 道1 [completed-40, completed-30]；道2 [completed-26, completed-19]；
# 道3 [completed-14, completed-9]。预热在道1起弧前，层间分别取区间中值。
_DEFAULT_MEASURES = [
    (1, "preheat", 42, 120.0),
    (2, "interpass", 28, 150.0),
    (3, "interpass", 16, 160.0),
]


def weld_pass_chain(welds, repairs=(), *, gauges=None,
                    weld_overrides=None, repair_overrides=None,
                    measure_skip=None, monitor_gauge=("WM-1", "2026A"),
                    temp_gauge=("TM-1", "2026A")):
    """为给定焊口/返修构造合规的道次与测温记录。

    同日多口按 2 小时槽位向完工时刻 09:00（返修 10:00）之前排程，同一焊工
    不跨口时段重叠。
    weld_overrides: {weld_no: {pass_no: PassRecord 字段覆盖}}；
    repair_overrides: {repair_id: {pass_no: ...}}；
    measure_skip: {"weld": {weld_no: {pass_no,...}},
                   "repair": {repair_id: {pass_no,...}}} 跳过测温（采样缺失）。
    返回 (gauges, passes, measurements)（gauges 缺省为全年有效默认版本）。
    """
    from datetime import timedelta as _td

    gauges = gauges if gauges is not None else weld_gauges()
    weld_overrides = weld_overrides or {}
    repair_overrides = repair_overrides or {}
    measure_skip = measure_skip or {"weld": {}, "repair": {}}
    passes: list = []
    measures: list = []
    seq = 0

    def emit_scope(owner_no, completed, overrides, *, scope,
                   repair_id=None, iteration=None, welder_id="W-001",
                   skip_set=None):
        nonlocal seq
        skip_set = skip_set or set()
        for no, spec_row in enumerate(_DEFAULT_PASSES, start=1):
            layer, proc, pol, cur, vol, length, arc, soff, dur = spec_row
            seq += 1
            t1 = completed - _td(minutes=soff)
            t0 = t1 - _td(minutes=dur)
            data = dict(
                pass_id=f"WP-{seq:03d}", weld_no=owner_no, scope=scope,
                repair_id=repair_id, iteration=iteration,
                pass_no=no, layer_no=layer, started_at=t0, finished_at=t1,
                welder_id=welder_id, process=proc, polarity=pol,
                current_a=cur, voltage_v=vol, weld_length_mm=length,
                arc_minutes=arc,
                gauges=[GaugeUse(gauge_id=monitor_gauge[0],
                                 version=monitor_gauge[1], role="main")])
            data.update(overrides.get(no, {}))
            data["pass_no"] = no
            passes.append(PassRecord(**data))
        for no, kind, moff, temp in _DEFAULT_MEASURES:
            if no in skip_set:
                continue
            seq += 1
            measures.append(TemperatureMeasurement(
                measure_id=f"TM-{seq:03d}", weld_no=owner_no, scope=scope,
                repair_id=repair_id, iteration=iteration, pass_no=no,
                measured_at=completed - _td(minutes=moff), temp_c=temp,
                gauge_id=temp_gauge[0], gauge_version=temp_gauge[1],
                kind=kind))

    days: dict[int, int] = {}
    for w in sorted(welds, key=lambda x: x.weld_no):
        day = w.welded_at.day
        slot = days.get(day, 0)
        days[day] = slot + 1
        completed = _dt(day, 9, 0) - _td(hours=2 * slot)
        emit_scope(w.weld_no, completed,
                   weld_overrides.get(w.weld_no, {}), scope="production",
                   welder_id=w.welder_id,
                   skip_set=measure_skip["weld"].get(w.weld_no))
    rdays: dict[tuple[int, str], int] = {}
    for r in sorted(repairs, key=lambda x: (x.weld_no, x.iteration)):
        day = r.repaired_at.day
        slot = rdays.get((day, r.weld_no), 0)
        rdays[(day, r.weld_no)] = slot + 1
        completed = _dt(day, 10, 0) - _td(hours=2 * slot)
        emit_scope(r.weld_no, completed,
                   repair_overrides.get(r.repair_id, {}), scope="repair",
                   repair_id=r.repair_id, iteration=r.iteration,
                   welder_id=r.welder_id,
                   skip_set=measure_skip["repair"].get(r.repair_id))
    return gauges, passes, measures


def weld_pass_failure_payload() -> SubmissionPayload:
    """场景六：道次执行链失效（参数越限/层间高温/仪表校准失效/采样缺失）。

    以缺陷扩检样例为底（一次返修链已闭合、焊材链合规），叠加：
    - WPS-101 冻结 GTAW/SMAW 道次参数窗口；
    - W-007 盖面道电流 200A，超 SMAW 窗口（WP-CURRENT-OUTSIDE）；
    - W-008 填充道起弧前层间温度 240℃，超 200℃ 上限
      （WP-INTERPASS-HIGH）；
    - W-009 漏登填充道前测温（WP-INTERPASS-MISSING，采样缺失）；
    - 监测仪表 WM-1 校准在 3/4 到期（未登记重新校准版本），W-001 一次
      返修（3/5 补焊）道次全部落在校准失效区间（WP-GAUGE-CALIBRATION），
      且盖面道电流 180A 超 SMAW 窗口（返修混用参数，WP-CURRENT-OUTSIDE）；
    即使返修后的复检 RT 合格，也触发 WP-REPAIR-PASS-INVALID，该次返修
    不得闭合——最终合格报告不得掩盖道次违规，相关焊口保持 hold。
    """
    base = extension_payload().model_copy(deep=True)
    for wpsrec in base.wps:
        wpsrec.process_windows = [wps_process_window("GTAW"),
                                  wps_process_window("SMAW")]

    # 监测仪 WM-1 校准 3/4 到期（测温仪 TM-1 保持全年有效）
    gauges = weld_gauges()
    gauges[0].calibrated_to = datetime(2026, 3, 4, 0, 0, tzinfo=timezone.utc)

    measure_skip = {"weld": {"W-009": {2}}, "repair": {}}
    gauges, passes, measures = weld_pass_chain(
        base.welds, base.repairs, gauges=gauges,
        weld_overrides={
            "W-007": {3: {"current_a": 200.0}},
            "W-008": {},
        },
        repair_overrides={
            "R-001": {3: {"current_a": 180.0}},
        },
        measure_skip=measure_skip)
    # W-008 填充道层间温度改为 240℃（超 200℃ 上限）
    for m in measures:
        if m.weld_no == "W-008" and m.scope == "production" and m.pass_no == 2:
            m.temp_c = 240.0

    return SubmissionPayload(
        package_ref="DEMO-6-WELD-PASS",
        line_no=base.line_no,
        submitted_by=base.submitted_by,
        wps=base.wps,
        welders=base.welders,
        nde_personnel=base.nde_personnel,
        nde_equipment=base.nde_equipment,
        weld_gauges=gauges,
        weld_passes=passes,
        temperature_measurements=measures,
        welds=base.welds,
        nde=base.nde,
        repairs=base.repairs,
        lot_rules=base.lot_rules,
        **{k: getattr(base, k) for k in (
            "consumable_batches", "consumable_rules",
            "consumable_containers", "bake_cycles", "quiver_stays",
            "consumable_segments", "consumable_events")},
    )


# ============================================================ 场景一：直接放行


def direct_release_payload() -> SubmissionPayload:
    """10 道口、20% 抽检（2 张合格），资格/炉批齐全 => 全部直接放行。"""
    welds = [
        weld(f"W-{i:03d}", f"H-A{100+i}", f"H-B{200+i}", day=1 + i // 4)
        for i in range(1, 11)
    ]
    # 20% = 2 口抽检，均合格全周片
    ndes = [
        rt("N-001", "W-001", "accept", day=2, bands=((0, 360),)),
        rt("N-002", "W-006", "accept", day=8, bands=((0, 360),)),
    ]
    wm_chain = valid_consumable_chain(welds)
    return SubmissionPayload(
        package_ref="DEMO-1-DIRECT",
        line_no="PL-100",
        submitted_by="质检员-王",
        wps=[wps()],
        welders=[welder()],
        nde_personnel=default_personnel(),
        nde_equipment=default_equipment(),
        welds=welds,
        nde=ndes,
        repairs=[],
        lot_rules=[lot(ratio=0.2)],
        **wm_chain,
    )


# ============================================================ 场景二：缺陷扩检


def extension_payload(*, enough_extension: bool = True) -> SubmissionPayload:
    """抽检 2 张中 W-001 不合格 -> 加倍扩检。

    enough_extension=False 时只补 1 口（应需 4 口），触发 LT-EXTENSION-INSUFFICIENT。
    返修与复检沿 300°~340° 缺陷位置闭合。
    """
    welds = [
        weld(f"W-{i:03d}", f"H-A{100+i}", f"H-B{200+i}", day=1 + i // 4)
        for i in range(1, 11)
    ]
    ndes = [
        # 首批 20% = W-001、W-006；W-001 在 300~340 出缺陷
        rt("N-001", "W-001", "reject", day=2,
           bands=((0, 360),), defects=((300, 340),)),
        rt("N-002", "W-006", "accept", day=8),
    ]
    if enough_extension:
        # 加倍 20%：再检 2 口，均合格 => 扩检数量够
        ndes += [
            rt("N-101", "W-002", "accept", day=16),
            rt("N-102", "W-007", "accept", day=17),
        ]
    else:
        ndes.append(rt("N-101", "W-002", "accept", day=16))

    # W-001 挖补 295~345，复检片覆盖 280~355 且合格
    repairs = [repair("R-001", "W-001", 1, (295, 345), day=5)]
    ndes.append(rt("N-051", "W-001", "accept", day=6,
                   bands=((280, 355),), iteration=1))

    wm_chain = valid_consumable_chain(welds, repairs)
    return SubmissionPayload(
        package_ref="DEMO-2-EXTEND" if enough_extension
        else "DEMO-2-EXTEND-SHORT",
        line_no="PL-100",
        submitted_by="质检员-王",
        wps=[wps()],
        welders=[welder()],
        nde_personnel=default_personnel(),
        nde_equipment=default_equipment(),
        welds=welds,
        nde=ndes,
        repairs=repairs,
        lot_rules=[lot(ratio=0.2, on_reject="double")],
        **wm_chain,
    )


# ============================================================ 场景三：二次返修


def double_repair_payload(*, second_covered: bool = True) -> SubmissionPayload:
    """单口 100% RT：原始 200~230 缺陷 -> 一次返修 -> 复检在 210~218 仍不合格
    -> 二次返修 -> 复检覆盖挖补区合格。

    second_covered=False 时二次复检片刻意漏掉挖补区尾部，触发 RP-EXCAVATION-UNCOVERED。
    """
    welds = [weld("W-901", "H-X1", "H-X2", day=1, lot_id="LOT-B")]
    ndes = [
        rt("N-900", "W-901", "reject", day=2,
           bands=((0, 360),), defects=((200, 230),)),
    ]
    repairs = [
        repair("R-901", "W-901", 1, (195, 235), day=4),
    ]
    # 一次复检：210~218 仍显示不合格
    ndes.append(rt("N-901", "W-901", "reject", day=6,
                   bands=((180, 250),), defects=((210, 218),), iteration=1))
    repairs.append(repair("R-902", "W-901", 2, (205, 222), day=8))
    # 二次复检
    if second_covered:
        bands = ((200, 228),)
    else:
        bands = ((200, 215),)  # 挖补 205~222 未被完整覆盖
    ndes.append(rt("N-902", "W-901", "accept", day=10,
                   bands=bands, iteration=2))

    wm_chain = valid_consumable_chain(welds, repairs)
    return SubmissionPayload(
        package_ref="DEMO-3-REPAIR2" if second_covered
        else "DEMO-3-REPAIR2-UNCOVERED",
        line_no="PL-200",
        submitted_by="质检员-赵",
        wps=[wps()],
        welders=[welder()],
        nde_personnel=default_personnel(),
        nde_equipment=default_equipment(),
        welds=welds,
        nde=ndes,
        repairs=repairs,
        lot_rules=[lot(rule_id="LOT-B", ratio=1.0, on_reject="full")],
        **wm_chain,
    )


# ============================================================ 场景四：NDT 资源失效


def ndt_resource_failure_payload() -> SubmissionPayload:
    """夜班跨证书到期点 + 复检换用未重新校准的射线机版本。

    - W-001 原始 RT 实施时段 22:00~次日02:00，跨过实施人证书 3/6 00:00 到期点；
    - W-006 的检测引用了未登记校准版本 XR-250@2026B；
    两份报告均被 NR-RECORD-EXCLUDED 剔除，20% 抽检数量不足，整批 hold。
    """
    welds = [
        weld(f"W-{i:03d}", f"H-A{100+i}", f"H-B{200+i}", day=1 + i // 4)
        for i in range(1, 11)
    ]
    # 实施人旧证 3/6 00:00 到期（续证后的新窗不在此包内——续证不改旧版）
    personnel = default_personnel()
    personnel[0].valid_to = datetime(2026, 3, 6, 0, 0, tzinfo=timezone.utc)
    # 3/6 后续发的新证（新证号）：N-002 用新证，但设备版本仍未登记
    personnel.append(ndt_cert(
        "NRT-II-ZHANG-R2", "NDE-II-张",
        valid_from="2026-03-06T00:00:00Z",
        valid_to="2030-03-05T23:59:59Z",
    ))
    ndes = [
        # W-001：夜班 3/5 22:00 ~ 3/6 02:00，跨过到期点
        rt("N-001", "W-001", "accept", day=6, hour=2, start_hour=22,
           start_day=5, bands=((0, 360),)),
        # W-006：设备版本改为未登记的 2026B（人员用续发新证，均有效）
        rt("N-002", "W-006", "accept", day=8,
           examiner_cert="NRT-II-ZHANG-R2", reviewer_cert="NRT-II-LI",
           equipment_uses=[NdeEquipmentUse(equipment_id="XR-250",
                                           version="2026B", role="main")]),
    ]
    wm_chain = valid_consumable_chain(welds)
    return SubmissionPayload(
        package_ref="DEMO-4-NDT-RESOURCE",
        line_no="PL-100",
        submitted_by="质检员-王",
        wps=[wps()],
        welders=[welder()],
        nde_personnel=personnel,
        nde_equipment=default_equipment(),
        welds=welds,
        nde=ndes,
        repairs=[],
        lot_rules=[lot(ratio=0.2, on_reject="double")],
        **wm_chain,
    )


# ============================================================ 场景五：焊材链失效


def consumable_failure_payload() -> SubmissionPayload:
    """缺陷扩检样例上叠加焊材链失效（用于演示 WM 系列条款定位）。

    - 保温筒 QV-1 校准在 3/4 到期（未登记重新校准版本）：3/4 之后的领用
      全部落在保温筒校准失效区间，3/5 补焊与 3/6 后焊口失效；
    - 3/5 补焊消耗登记 6.0kg，而该段只领出 5kg：消耗+退回 > 领出
      （同一数量重复分配，WM-QTY-CONSERVATION），该段全部消耗失效；
    - 3/8 领用段提前到 03:00 领出，W-008 09:00 施焊时暴露 6h
      超过 4h 上限（WM-EXPOSURE-EXCEEDED）；
    - 失效焊材不得用于返修闭合：W-001 一次返修链节保持 false
      （RP-WM-INVALID，随附具体 WM 条款）。
    """
    from datetime import datetime as _dt

    base = extension_payload().model_copy(deep=True)
    # extension_payload 已挂过一次合规消耗；清空后用本场景的（失效）链重建
    for w in base.welds:
        w.consumables = []
    for r in base.repairs:
        r.consumables = []
    wm_chain = valid_consumable_chain(base.welds, base.repairs)

    # 保温筒校准 3/4 00:00 到期（重新校准应派生新版本，此处故意不登记）
    for c in wm_chain["consumable_containers"]:
        if c.container_id == "QV-1":
            c.calibrated_to = _dt(2026, 3, 4, 0, 0, tzinfo=timezone.utc)

    # 数量重复分配：3/5 段领出 5kg，把补焊消耗改成 6.0kg，
    # 消耗 6.2 + 退回 4.7 > 领出 5
    for rep in base.repairs:
        for u in rep.consumables:
            u.qty_kg = 6.0

    # SG-03 段提前到 03:00 领出 -> W-008 09:00 施焊暴露 6h > 4h 上限
    # （WM-EXPOSURE-EXCEEDED）；3/3 仍在保温筒校准有效期内（3/4 到期），
    # 故该段只命中暴露超时，与 3/5 段的数量/校准问题各自独立定位。
    for seg in wm_chain["consumable_segments"]:
        if seg.segment_id == "SG-03":
            seg.issued_at = _dt(2026, 3, 3, 3, 0, tzinfo=timezone.utc)

    return SubmissionPayload(
        package_ref="DEMO-5-WELD-MATERIAL",
        line_no=base.line_no,
        submitted_by=base.submitted_by,
        wps=base.wps,
        welders=base.welders,
        nde_personnel=base.nde_personnel,
        nde_equipment=base.nde_equipment,
        welds=base.welds,
        nde=base.nde,
        repairs=base.repairs,
        lot_rules=base.lot_rules,
        **wm_chain,
    )
