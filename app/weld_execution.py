"""焊接道次执行链核验（WP 系列）。

焊口只登记 WPS 编号与完工时刻，无法证明打底/填充/盖面各道实际遵守电流、
电压、焊速与层间温度；返修混用参数也会被最终合格报告掩盖。道次链核验：

    WPS 版本冻结方法窗口（极性/电流/电压/焊速/热输入/预热/层间温度）
      → 逐焊口与返修提交道次（编号/起止时刻/焊工/方法/实测参数/焊缝长度/
        燃弧时间/仪表版本）与起弧前测温记录（测温仪表版本）
        → 按时间与层序重建道次
          → 换算焊速 v=焊缝长度/燃弧时间、热输入 E=k·U·I·60/v
            → 核对参数窗口、逐道焊工资格、道次起弧前层间温度与仪表校准

任一不满足，相关焊口保持 hold，并定位到原始道次（pass_id）与测点
（measure_id）。返修范围道次链不合规时该次返修不得闭合
（WP-REPAIR-PASS-INVALID，具体 WP 码随附）。

与焊材链一致，区分两类问题：
- 结构缺口（未提交道次、WPS 未冻结方法窗口、仪表版本未登记/未引用）：
  审查包不得冻结，只能补录后重新提交（freeze_blocked）；
- 规则性 hold（参数越限、测温采样缺失、层序/时段问题、校准失效）：
  记录齐全时可冻结，纠错从旧版派生修订分支。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .clauses import CLAUSES
from .qualification import (
    weld_diameters,
    weld_groups,
    weld_thicknesses,
    match_welder,
)
from .schemas import (
    PassRecord,
    RepairRecord,
    SubmissionPayload,
    TemperatureMeasurement,
    WeldGaugeVersion,
    WeldRecord,
    WpsProcessWindow,
    WpsRecord,
)


def _f(code: str, *, evidence=None, weld_no=None, repair_id=None,
       pass_id=None, measure_id=None, scope=None) -> dict:
    meta = CLAUSES[code]
    return {
        "code": code,
        "severity": meta["severity"],
        "category": meta["category"],
        "reference": meta["reference"],
        "message": meta["message"],
        "weld_no": weld_no,
        "repair_id": repair_id,
        "pass_id": pass_id,
        "measure_id": measure_id,
        "scope": scope,
        "evidence": evidence or {},
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ============================================================ 热输入换算


def travel_speed_mm_min(p: PassRecord) -> tuple[float | None, float]:
    """焊速 v = 焊缝长度(mm) / 燃弧时间(min)。

    燃弧时间缺省时取道次起止时段；返回 (v, arc_minutes)。
    """
    span = (p.finished_at - p.started_at).total_seconds() / 60.0
    arc = p.arc_minutes if p.arc_minutes is not None else span
    if arc <= 0:
        return None, arc
    return p.weld_length_mm / arc, arc


def heat_input_kj_mm(*, k: float, voltage: float, current: float,
                     speed_mm_min: float) -> float:
    """热输入 E = k·U·I·60/v（J/mm）/1000 → kJ/mm。"""
    return k * voltage * current * 60.0 / speed_mm_min / 1000.0


# ============================================================ 单道次核对


def _role_of(layer_no: int, max_layer: int) -> str:
    if layer_no == 1:
        return "root" if max_layer > 1 else "root"  # 单道焊也算打底道
    if layer_no == max_layer:
        return "cap"
    return "fill"


def _pass_checks(
    p: PassRecord,
    *,
    wps: WpsRecord | None,
    window: WpsProcessWindow | None,
    welder_index: dict,
    weld: WeldRecord,
    scope_label: str,
    repair: RepairRecord | None,
) -> tuple[list[dict], dict, list[str]]:
    """单道次的参数/资格/极性核对。返回 (findings, calc, reasons)。"""
    findings: list[dict] = []
    reasons: list[str] = []
    ref = dict(weld_no=weld.weld_no, repair_id=p.repair_id, pass_id=p.pass_id,
               scope=p.scope)

    def emit(code: str, evidence: dict) -> None:
        findings.append(_f(code, evidence=evidence, **ref))
        reasons.append(code)

    speed, arc = travel_speed_mm_min(p)
    calc = {
        "arc_minutes": round(arc, 3),
        "travel_speed_mm_min": round(speed, 2) if speed is not None else None,
        "heat_input_kj_mm": None,
    }

    # 燃弧时间不得长于道次墙钟时段（记录可信性）
    span = (p.finished_at - p.started_at).total_seconds() / 60.0
    if p.arc_minutes is not None and p.arc_minutes > span + 1e-9:
        emit("WP-PARAM-MISSING", {
            "pass_no": p.pass_no, "reason": "登记燃弧时间长于道次起止时段，"
            "焊速换算依据不可信",
            "arc_minutes": p.arc_minutes,
            "span_minutes": round(span, 3)})

    # 焊工：必须与焊口/返修登记焊工一致且已登记，逐道按起弧时点核对资格
    registered_welder = (repair.welder_id if repair else weld.welder_id)
    if p.welder_id != registered_welder:
        emit("WP-WELDER-MISMATCH", {
            "pass_no": p.pass_no, "pass_welder": p.welder_id,
            "registered_welder": registered_welder,
            "reason": "道次实际施焊焊工与焊口/返修登记焊工不一致"})
    welder = welder_index.get(p.welder_id)
    if welder is None:
        emit("WP-WELDER-MISMATCH", {
            "pass_no": p.pass_no, "pass_welder": p.welder_id,
            "reason": "道次施焊焊工未登记"})
    else:
        for code in match_welder(
            welder, at=p.started_at,
            groups=weld_groups(weld), thicknesses=weld_thicknesses(weld),
            diameters=weld_diameters(weld), process=p.process,
            position=weld.weld_position,
        ):
            emit(code, {
                "pass_no": p.pass_no, "at": _iso(p.started_at),
                "welder_id": p.welder_id, "process": p.process,
                "scope": scope_label})

    # WPS 方法窗口：未冻结该方法窗口即无据可核
    if wps is None or window is None:
        emit("WP-PASS-WINDOW-MISSING", {
            "pass_no": p.pass_no, "process": p.process,
            "wps_no": None if wps is None else wps.wps_no,
            "reason": "WPS 未冻结该焊接方法的极性/电流/电压/热输入/"
                      "预热/层间温度窗口"})
        return findings, calc, reasons

    # 极性（窗口内含通用 DC 时，DCEN/DCEP/DC 均认可）
    allowed = [x.upper() for x in window.polarity]
    if p.polarity.upper() not in allowed and not (
        "DC" in allowed and p.polarity.upper() in ("DCEN", "DCEP", "DC")):
        emit("WP-POLARITY", {
            "pass_no": p.pass_no, "actual": p.polarity,
            "allowed": list(window.polarity)})

    # 电流 / 电压
    if not (window.current_min_a <= p.current_a <= window.current_max_a):
        emit("WP-CURRENT-OUTSIDE", {
            "pass_no": p.pass_no, "actual_a": p.current_a,
            "window_a": [window.current_min_a, window.current_max_a]})
    if not (window.voltage_min_v <= p.voltage_v <= window.voltage_max_v):
        emit("WP-VOLTAGE-OUTSIDE", {
            "pass_no": p.pass_no, "actual_v": p.voltage_v,
            "window_v": [window.voltage_min_v, window.voltage_max_v]})

    # 焊速
    if speed is not None:
        lo, hi = window.travel_min_mm_min, window.travel_max_mm_min
        if (lo is not None and speed < lo) or (hi is not None and speed > hi):
            emit("WP-TRAVEL-OUTSIDE", {
                "pass_no": p.pass_no, "weld_length_mm": p.weld_length_mm,
                "arc_minutes": round(arc, 3),
                "actual_mm_min": round(speed, 2),
                "window_mm_min": [lo, hi]})

        # 热输入（只要焊速可算即换算；越限不因电流/电压越限而跳过，
        # 便于审查包同时呈现超窗口的实际热输入证据）
        hi_e = heat_input_kj_mm(
            k=window.thermal_efficiency, voltage=p.voltage_v,
            current=p.current_a, speed_mm_min=speed)
        calc["heat_input_kj_mm"] = round(hi_e, 4)
        elo, ehi = (window.heat_input_min_kj_mm,
                    window.heat_input_max_kj_mm)
        if (elo is not None and hi_e < elo - 1e-9) or \
                (ehi is not None and hi_e > ehi + 1e-9):
            emit("WP-HEATINPUT-OUTSIDE", {
                "pass_no": p.pass_no,
                "thermal_efficiency": window.thermal_efficiency,
                "actual_kj_mm": round(hi_e, 4),
                "window_kj_mm": [elo, ehi]})

    return findings, calc, reasons


# ============================================================ 测温链


def _assign_measurement(
    m: TemperatureMeasurement,
    ordered: list[PassRecord],
    preheat_lead: timedelta,
) -> PassRecord | None:
    """把测点重建归属到道次：上一道收弧后 ~ 本道起弧前（含边界）。"""
    first = ordered[0]
    if m.measured_at < first.started_at - preheat_lead:
        return None  # 早于预热测温窗口
    for p in ordered:
        if m.measured_at <= p.started_at:
            return p
    return None  # 晚于全部道次起弧（事后补测）


def _temperature_checks(
    ordered: list[PassRecord],
    measurements: list[TemperatureMeasurement],
    windows_by_process: dict[str, WpsProcessWindow],
    *,
    weld_no: str, repair_id: str | None, scope: str,
    gauge_index: dict[tuple[str, str], WeldGaugeVersion],
    structural_gaps: list[dict],
) -> tuple[list[dict], dict[int, dict], list[dict]]:
    """测温记录的仪表校准、时点归属与预热/层间温度核对。

    返回 (findings, governing_by_pass_no, measure_views)。
    """
    findings: list[dict] = []
    lead_minutes = 60.0
    if ordered:
        w0 = windows_by_process.get(ordered[0].process.strip().upper())
        if w0 is not None:
            lead_minutes = w0.preheat_lead_minutes
    lead = timedelta(minutes=lead_minutes)

    # 仪表版本：登记 + 类别匹配（测温须用 thermometer）+ 校准时点覆盖；
    # 归属道次确定后调用，使条款同时定位到原始测点与道次
    def gauge_ok(m: TemperatureMeasurement, target_pass_no: int | None) -> bool:
        pid = next((p.pass_id for p in ordered
                    if p.pass_no == target_pass_no), None)
        g = gauge_index.get((m.gauge_id, m.gauge_version))
        if g is None:
            findings.append(_f(
                "WP-GAUGE-MISSING", weld_no=weld_no, repair_id=repair_id,
                pass_id=pid,
                measure_id=m.measure_id, scope=scope,
                evidence={"target": f"{m.gauge_id}@{m.gauge_version}",
                          "reason": "测温记录引用的仪表版本未登记",
                          "pass_no": target_pass_no,
                          "measured_at": _iso(m.measured_at)}))
            structural_gaps.append({
                "kind": "temperature_gauge_missing",
                "detail": f"测温 {m.measure_id} 引用未登记仪表 "
                          f"{m.gauge_id}@{m.gauge_version}",
                "weld_no": weld_no, "repair_id": repair_id,
                "measure_id": m.measure_id})
            return False
        ok = True
        if g.kind != "thermometer":
            findings.append(_f(
                "WP-GAUGE-KIND", weld_no=weld_no, repair_id=repair_id,
                pass_id=pid, measure_id=m.measure_id, scope=scope,
                evidence={"target": f"{m.gauge_id}@{m.gauge_version}",
                          "pass_no": target_pass_no,
                          "gauge_kind": g.kind,
                          "required_kind": "thermometer",
                          "reason": "测温记录引用的仪表类别不是测温仪"
                                    "（用途与类别不匹配）"}))
            ok = False
        if not (g.calibrated_from <= m.measured_at <= g.calibrated_to):
            findings.append(_f(
                "WP-GAUGE-CALIBRATION", weld_no=weld_no, repair_id=repair_id,
                pass_id=pid, measure_id=m.measure_id, scope=scope,
                evidence={"target": f"{m.gauge_id}@{m.gauge_version}",
                          "pass_no": target_pass_no,
                          "calibrated_from": _iso(g.calibrated_from),
                          "calibrated_to": _iso(g.calibrated_to),
                          "used_at": _iso(m.measured_at)}))
            ok = False
        return ok

    assigned: dict[int, list[TemperatureMeasurement]] = defaultdict(list)
    views: list[dict] = []
    for m in measurements:
        target_no = m.pass_no
        if target_no is not None:
            if not any(p.pass_no == target_no for p in ordered):
                findings.append(_f(
                    "WP-MEASURE-LAG", weld_no=weld_no, repair_id=repair_id,
                    measure_id=m.measure_id, scope=scope,
                    evidence={"reason": "测温记录标注的道次编号不存在",
                              "pass_no": target_no}))
                views.append({
                    "measure_id": m.measure_id, "kind": m.kind,
                    "measured_at": _iso(m.measured_at), "temp_c": m.temp_c,
                    "gauge_id": m.gauge_id, "gauge_version": m.gauge_version,
                    "gauge_valid": False, "pass_no": m.pass_no,
                    "assigned_pass_no": None})
                continue
            target = next(p for p in ordered if p.pass_no == target_no)
            prev_finish = None
            idx = ordered.index(target)
            if idx > 0:
                prev_finish = ordered[idx - 1].finished_at
            too_early = (prev_finish is None
                         and m.measured_at < target.started_at - lead)
            after_start = m.measured_at > target.started_at
            before_prev = prev_finish is not None and m.measured_at < prev_finish
            timing_invalid = too_early or after_start or before_prev
        else:
            target = _assign_measurement(m, ordered, lead)
            timing_invalid = target is None
            if target is not None:
                target_no = target.pass_no
            prev_finish = None

        view = {
            "measure_id": m.measure_id,
            "kind": m.kind,
            "measured_at": _iso(m.measured_at),
            "temp_c": m.temp_c,
            "gauge_id": m.gauge_id,
            "gauge_version": m.gauge_version,
            "gauge_valid": gauge_ok(m, target_no),
            "pass_no": m.pass_no,
            "assigned_pass_no": (target.pass_no if m.pass_no is None
                                 and target is not None else None),
        }
        if timing_invalid:
            if m.pass_no is None:
                evidence = {"measured_at": _iso(m.measured_at),
                            "reason": "测温时点无法落入任何道次的起弧前窗口"
                                      "（过早或事后补测）"}
            else:
                evidence = {"pass_no": target_no,
                            "measured_at": _iso(m.measured_at),
                            "pass_started_at": _iso(target.started_at),
                            "prev_finished_at": _iso(prev_finish),
                            "reason": "测温时点不在该道起弧前窗口内"}
            findings.append(_f(
                "WP-MEASURE-LAG", weld_no=weld_no, repair_id=repair_id,
                pass_id=(target.pass_id if target is not None else None),
                measure_id=m.measure_id, scope=scope, evidence=evidence))
            views.append(view)
            continue

        # 测点类型应与道次角色对应（1=预热，其余=层间）
        expect_kind = "preheat" if target.pass_no == 1 else "interpass"
        if m.kind != expect_kind:
            findings.append(_f(
                "WP-MEASURE-LAG", weld_no=weld_no, repair_id=repair_id,
                pass_id=target.pass_id, measure_id=m.measure_id, scope=scope,
                evidence={"pass_no": target.pass_no, "recorded_kind": m.kind,
                          "expected_kind": expect_kind,
                          "reason": "测温类型与道次不匹配（首道须为预热，"
                                    "其余须为层间）"}))
        else:
            assigned[target.pass_no].append(m)
        views.append(view)

    # 逐道：采样缺失 / 越限（以起弧前最后一个有效测点为准）
    governing: dict[int, dict] = {}
    for p in ordered:
        expect_kind = "preheat" if p.pass_no == 1 else "interpass"
        pts = sorted(assigned.get(p.pass_no, []), key=lambda x: x.measured_at)
        if not pts:
            w_here = windows_by_process.get(p.process.strip().upper())
            if p.pass_no == 1:
                # 首道始终要求可追溯的预热测点：即使 WPS 预热下限为 0
                # （不强制预热），也不得省略温度记录
                findings.append(_f(
                    "WP-PREHEAT-MISSING", weld_no=weld_no,
                    repair_id=repair_id, pass_id=p.pass_id, scope=scope,
                    evidence={
                        "pass_no": 1,
                        "preheat_min_c": (w_here.preheat_min_c
                                          if w_here is not None else None),
                        "reason": "首道起弧前无预热测温记录（预热下限为 0 "
                                  "也须有可追溯测点）"}))
            else:
                findings.append(_f(
                    "WP-INTERPASS-MISSING", weld_no=weld_no,
                    repair_id=repair_id, pass_id=p.pass_id, scope=scope,
                    evidence={"pass_no": p.pass_no,
                              "reason": "该道起弧前无层间温度测温记录"}))
            continue
        gov = pts[-1]
        governing[p.pass_no] = {
            "measure_id": gov.measure_id,
            "measured_at": _iso(gov.measured_at),
            "temp_c": gov.temp_c,
            "kind": gov.kind,
        }
        window = windows_by_process.get(p.process.strip().upper())
        if window is None:
            continue
        if p.pass_no == 1:
            if gov.temp_c < window.preheat_min_c:
                findings.append(_f(
                    "WP-PREHEAT-LOW", weld_no=weld_no, repair_id=repair_id,
                    pass_id=p.pass_id, measure_id=gov.measure_id, scope=scope,
                    evidence={"pass_no": 1, "actual_c": gov.temp_c,
                              "min_c": window.preheat_min_c}))
            if window.preheat_max_c is not None \
                    and gov.temp_c > window.preheat_max_c:
                findings.append(_f(
                    "WP-PREHEAT-HIGH", weld_no=weld_no, repair_id=repair_id,
                    pass_id=p.pass_id, measure_id=gov.measure_id, scope=scope,
                    evidence={"pass_no": 1, "actual_c": gov.temp_c,
                              "max_c": window.preheat_max_c}))
        else:
            lo, hi = window.interpass_min_c, window.interpass_max_c
            if (lo is not None and gov.temp_c < lo) or gov.temp_c > hi:
                findings.append(_f(
                    "WP-INTERPASS-HIGH", weld_no=weld_no,
                    repair_id=repair_id, pass_id=p.pass_id,
                    measure_id=gov.measure_id, scope=scope,
                    evidence={"pass_no": p.pass_no, "actual_c": gov.temp_c,
                              "window_c": [lo, hi]}))
    return findings, governing, views


# ============================================================ 范围（焊口/返修）核验


# 仪表类别 -> 可证明的测量用途。weld_monitor 为电流/电压/焊速一体监测仪；
# thermometer 只证明温度，不能单独证明任何电气参数或焊速。
_GAUGE_KIND_PURPOSES: dict[str, set[str]] = {
    "ammeter": {"current"},
    "voltmeter": {"voltage"},
    "timer": {"travel"},
    "weld_monitor": {"current", "voltage", "travel"},
    "thermometer": {"temperature"},
    "other": set(),
}
# 道次参数核对所需用途（电流/电压/焊速）：每一项都必须有类别匹配且
# 校准有效期覆盖整个道次时段的仪表证明。
_PASS_PARAM_PURPOSES = ("current", "voltage", "travel")


def _gauge_purposes(g: WeldGaugeVersion) -> set[str]:
    return set(_GAUGE_KIND_PURPOSES.get(g.kind, set()))


def _gauge_checks_passes(
    passes: list[PassRecord],
    *,
    weld_no: str, repair_id: str | None, scope: str,
    gauge_index: dict[tuple[str, str], WeldGaugeVersion],
    structural_gaps: list[dict],
) -> list[dict]:
    """道次仪表版本：引用存在、类别与测量用途匹配、校准覆盖整个道次时段。

    thermometer 只能证明温度；电流/电压/焊速分别须由 ammeter/voltmeter/timer
    （或 weld_monitor 一体仪）证明。缺少类别匹配且校准有效的参数仪表时
    判 WP-GAUGE-KIND（规则性 hold，记录齐全可冻结纠错）。
    """
    findings: list[dict] = []
    for p in passes:
        ref = dict(weld_no=weld_no, repair_id=repair_id,
                   pass_id=p.pass_id, scope=scope)
        if not p.gauges:
            findings.append(_f("WP-GAUGE-MISSING", **ref, evidence={
                "pass_no": p.pass_no,
                "reason": "道次未引用任何电流/电压/焊速测量仪表版本"}))
            structural_gaps.append({
                "kind": "pass_gauges_missing",
                "detail": f"道次 {p.pass_id} 未引用测量仪表版本",
                "weld_no": weld_no, "repair_id": repair_id,
                "pass_id": p.pass_id})
            # 无任何仪表引用时三类参数均无证明
            findings.append(_f("WP-GAUGE-KIND", **ref, evidence={
                "pass_no": p.pass_no,
                "missing_purposes": list(_PASS_PARAM_PURPOSES),
                "reason": "道次未引用可证明电流/电压/焊速的类别匹配仪表"}))
            continue

        resolved: dict[tuple[str, str], WeldGaugeVersion] = {}
        seen = set()
        for use in p.gauges:
            key = (use.gauge_id, use.version)
            if key in seen:
                continue
            seen.add(key)
            target = f"{use.gauge_id}@{use.version}"
            g = gauge_index.get(key)
            if g is None:
                findings.append(_f("WP-GAUGE-MISSING", **ref, evidence={
                    "pass_no": p.pass_no, "target": target,
                    "reason": "道次引用的仪表版本未登记"}))
                structural_gaps.append({
                    "kind": "gauge_missing",
                    "detail": f"道次 {p.pass_id} 引用未登记仪表 {target}",
                    "weld_no": weld_no, "repair_id": repair_id,
                    "pass_id": p.pass_id})
                continue
            resolved[key] = g
            # 测温仪表挂在道次上属于用途错配（测温应由 temperature_measurements
            # 链登记）；此处只记录类别，参数用途覆盖在下面统一判定
            if not (g.calibrated_from <= p.started_at
                    and p.finished_at <= g.calibrated_to):
                findings.append(_f("WP-GAUGE-CALIBRATION", **ref, evidence={
                    "pass_no": p.pass_no, "target": target,
                    "gauge_kind": g.kind,
                    "calibrated_from": _iso(g.calibrated_from),
                    "calibrated_to": _iso(g.calibrated_to),
                    "used_from": _iso(p.started_at),
                    "used_to": _iso(p.finished_at)}))

        # 用途-类别绑定：逐参数核对证明仪表。
        # - 完全没有对应用途类别的仪表 -> WP-GAUGE-KIND（如只用 thermometer）；
        # - 类别正确但全部校准失效 -> 已由 WP-GAUGE-CALIBRATION 挂条款，不重复。
        missing_purposes: list[str] = []
        for purpose in _PASS_PARAM_PURPOSES:
            matching = [g for g in resolved.values()
                        if purpose in _gauge_purposes(g)]
            if not matching:
                missing_purposes.append(purpose)
        if missing_purposes:
            referenced = [
                {"target": f"{g.gauge_id}@{g.version}", "kind": g.kind}
                for g in resolved.values()
            ]
            findings.append(_f("WP-GAUGE-KIND", **ref, evidence={
                "pass_no": p.pass_no,
                "missing_purposes": missing_purposes,
                "referenced_gauges": referenced,
                "reason": "缺少与电流/电压/焊速用途类别匹配的仪表"
                          "（thermometer 不能单独证明电气参数或焊速）"}))
    return findings


def _sequence_checks(
    passes: list[PassRecord],
    *,
    weld_no: str, repair_id: str | None, scope: str,
    completed_at: datetime,
) -> list[dict]:
    """重号、层序断档、时间序与层序一致性、时段重叠与完工时刻核对。"""
    findings: list[dict] = []
    ref = dict(weld_no=weld_no, repair_id=repair_id, scope=scope)

    counts: dict[int, list[str]] = defaultdict(list)
    for p in passes:
        counts[p.pass_no].append(p.pass_id)
    dup_nos = sorted(no for no, ids in counts.items() if len(ids) > 1)
    for no in dup_nos:
        findings.append(_f("WP-PASS-DUP", pass_id=counts[no][0], **ref,
                           evidence={"pass_no": no, "pass_ids": counts[no]}))

    by_no: dict[int, PassRecord] = {}
    for p in passes:
        by_no.setdefault(p.pass_no, p)
    nos = sorted(by_no)
    if nos and nos != list(range(1, len(nos) + 1)):
        missing = sorted(set(range(1, max(nos) + 1)) - set(nos))
        findings.append(_f("WP-LAYER-GAP", **ref, evidence={
            "reason": "道次编号不自 1 连续", "pass_nos": nos,
            "missing": missing}))

    ordered = [by_no[n] for n in nos]
    if ordered:
        layers = [p.layer_no for p in ordered]
        if layers[0] != 1 or any(
                b < a or b - a > 1 for a, b in zip(layers, layers[1:])):
            findings.append(_f("WP-LAYER-GAP",
                               pass_id=ordered[0].pass_id, **ref,
                               evidence={"reason": "层号不自 1 起、回退或跳层",
                                         "pass_nos": nos, "layers": layers}))

        for prev, cur in zip(ordered, ordered[1:]):
            pair_ref = {**ref, "pass_id": cur.pass_id}
            if cur.started_at < prev.started_at:
                findings.append(_f("WP-PASS-ORDER", **pair_ref, evidence={
                    "reason": "后一道起弧早于前一道（时间序倒置）",
                    "prev_pass_no": prev.pass_no,
                    "pass_no": cur.pass_no,
                    "prev_started_at": _iso(prev.started_at),
                    "started_at": _iso(cur.started_at)}))
            if cur.started_at < prev.finished_at:
                findings.append(_f("WP-PASS-OVERLAP", **pair_ref, evidence={
                    "reason": "相邻道次施焊时段重叠",
                    "prev_pass_no": prev.pass_no,
                    "pass_no": cur.pass_no,
                    "prev_pass_id": prev.pass_id,
                    "pass_id": cur.pass_id,
                    "prev_finished_at": _iso(prev.finished_at),
                    "started_at": _iso(cur.started_at)}))

    for p in ordered:
        if p.started_at > completed_at or p.finished_at > completed_at:
            findings.append(_f("WP-PASS-ORDER", pass_id=p.pass_id, **ref,
                               evidence={
                                   "reason": "道次施焊时段晚于焊口/返修登记的"
                                             "完工时刻",
                                   "pass_no": p.pass_no,
                                   "started_at": _iso(p.started_at),
                                   "finished_at": _iso(p.finished_at),
                                   "completed_at": _iso(completed_at)}))
    return findings


def verify_scope(
    *,
    label: str,
    scope: str,
    weld: WeldRecord,
    repair: RepairRecord | None,
    passes: list[PassRecord],
    measurements: list[TemperatureMeasurement],
    wps_index: dict[str, WpsRecord],
    welder_index: dict,
    gauge_index: dict[tuple[str, str], WeldGaugeVersion],
    structural_gaps: list[dict],
) -> dict:
    """核验一个施焊范围（一道焊口的施焊缝，或一次返修的补焊道次）。"""
    weld_no = weld.weld_no
    repair_id = repair.repair_id if repair else None
    wps_no = repair.wps_no if repair else weld.wps_no
    completed_at = repair.repaired_at if repair else weld.welded_at
    ref = dict(weld_no=weld_no, repair_id=repair_id, scope=scope)

    findings: list[dict] = []
    wps = wps_index.get(wps_no)
    # 该范围各道方法可能不同（GTAW 打底 + SMAW 填充盖面），窗口按方法取用
    windows: dict[str, WpsProcessWindow] = {}
    if wps is not None:
        windows = {win.process.strip().upper(): win
                   for win in wps.process_windows}

    findings.extend(_sequence_checks(
        passes, weld_no=weld_no, repair_id=repair_id, scope=scope,
        completed_at=completed_at))
    findings.extend(_gauge_checks_passes(
        passes, weld_no=weld_no, repair_id=repair_id, scope=scope,
        gauge_index=gauge_index, structural_gaps=structural_gaps))

    # 按编号去重后的稳定排序（重号时仍保留全部记录以便定位）
    by_no_first: dict[int, PassRecord] = {}
    for p in sorted(passes, key=lambda x: (x.pass_no, x.started_at)):
        by_no_first.setdefault(p.pass_no, p)
    ordered = [by_no_first[n] for n in sorted(by_no_first)]
    max_layer = max((p.layer_no for p in ordered), default=1)

    pass_calcs: dict[str, dict] = {}
    for p in passes:
        window = windows.get(p.process.strip().upper())
        if window is None:
            structural_gaps.append({
                "kind": "wps_window_missing",
                "detail": f"WPS {wps_no} 未冻结方法 {p.process} 的参数窗口",
                "weld_no": weld_no, "repair_id": repair_id,
                "pass_id": p.pass_id})
        pf, calc, _reasons = _pass_checks(
            p, wps=wps, window=window, welder_index=welder_index,
            weld=weld, scope_label=label, repair=repair)
        findings.extend(pf)
        pass_calcs[p.pass_id] = calc

    # 测温：窗口以该范围各道所属方法窗口取并集判定——预热/层间限值按
    # 每道自身方法的窗口核对（不同方法窗口限值不一致时以该道窗口为准）。
    # 为保持测点归属唯一，测温统一按"范围主窗口"（首道方法窗口）核对。
    temp_findings, governing, measure_views = _temperature_checks(
        ordered, measurements, windows,
        weld_no=weld_no, repair_id=repair_id, scope=scope,
        gauge_index=gauge_index, structural_gaps=structural_gaps)
    findings.extend(temp_findings)

    # ---- 冻结用道次视图 ----
    pass_views: list[dict] = []
    # 测点 -> 道次归属（显式 pass_no 或按时点重建）
    measure_pass = {m["measure_id"]: (m.get("pass_no")
                    or m.get("assigned_pass_no"))
                    for m in measure_views}
    for p in sorted(passes, key=lambda x: (x.pass_no, x.started_at, x.pass_id)):
        calc = pass_calcs.get(p.pass_id, {})
        window = windows.get(p.process.strip().upper())
        codes = sorted({f["code"] for f in findings
                        if f.get("pass_id") == p.pass_id
                        or measure_pass.get(f.get("measure_id")) == p.pass_no})
        gov = governing.get(p.pass_no)
        pass_views.append({
            "pass_id": p.pass_id,
            "pass_no": p.pass_no,
            "layer_no": p.layer_no,
            "role": _role_of(p.layer_no, max_layer),
            "started_at": _iso(p.started_at),
            "finished_at": _iso(p.finished_at),
            "welder_id": p.welder_id,
            "process": p.process,
            "polarity": p.polarity,
            "current_a": p.current_a,
            "voltage_v": p.voltage_v,
            "weld_length_mm": p.weld_length_mm,
            "arc_minutes": calc.get("arc_minutes"),
            "travel_speed_mm_min": calc.get("travel_speed_mm_min"),
            "heat_input_kj_mm": calc.get("heat_input_kj_mm"),
            "window": None if window is None else {
                "wps_no": wps_no, "process": window.process,
                "polarity": list(window.polarity),
                "current_a": [window.current_min_a, window.current_max_a],
                "voltage_v": [window.voltage_min_v, window.voltage_max_v],
                "travel_mm_min": [window.travel_min_mm_min,
                                  window.travel_max_mm_min],
                "heat_input_kj_mm": [window.heat_input_min_kj_mm,
                                     window.heat_input_max_kj_mm],
                "preheat_min_c": window.preheat_min_c,
                "preheat_max_c": window.preheat_max_c,
                "interpass_c": [window.interpass_min_c,
                                window.interpass_max_c],
                "thermal_efficiency": window.thermal_efficiency,
            },
            "gauges": [u.model_dump(mode="json") for u in p.gauges],
            "governing_temperature": gov,
            "reasons": codes,
            "pass_valid": not codes,
        })

    reasons = sorted({f["code"] for f in findings})
    scope_key = f"{scope}:{repair_id or weld_no}"
    return {
        "scope_key": scope_key,
        "label": label,
        "weld_no": weld_no,
        "scope": scope,
        "repair_id": repair_id,
        "iteration": repair.iteration if repair else None,
        "wps_no": wps_no,
        "pass_total": len(passes),
        "passes_valid": not reasons,
        "reasons": reasons,
        "findings": findings,
        "passes": pass_views,
        "measurements": measure_views,
    }


# ============================================================ 整包核验入口


def verify_weld_passes(payload: SubmissionPayload) -> dict:
    """核验整包焊接道次执行链。

    启用判定与焊材链同口径：只要仪表版本/道次/测温记录任一非空即启用链。
    全部缺省（旧载荷）返回 enabled=False，不影响既有核算。
    """
    chain_present = bool(
        payload.weld_gauges or payload.weld_passes
        or payload.temperature_measurements
    )
    if not chain_present:
        return {"enabled": False, "scope_states": {}, "findings": [],
                "gaps": [], "affected_welds": []}

    wps_index = {w.wps_no: w for w in payload.wps}
    welder_index = {w.welder_id: w for w in payload.welders}
    gauge_index = {(g.gauge_id, g.version): g for g in payload.weld_gauges}
    weld_index = {w.weld_no: w for w in payload.welds}

    passes_by_scope: dict[str, list[PassRecord]] = defaultdict(list)
    for p in payload.weld_passes:
        key = f"{p.scope}:{p.repair_id or p.weld_no}"
        passes_by_scope[key].append(p)
    measures_by_scope: dict[str, list[TemperatureMeasurement]] = defaultdict(list)
    for m in payload.temperature_measurements:
        key = f"{m.scope}:{m.repair_id or m.weld_no}"
        measures_by_scope[key].append(m)

    structural_gaps: list[dict] = []
    scope_states: dict[str, dict] = {}

    def _scope_key(scope: str, weld_no: str, repair_id: str | None) -> str:
        return f"{scope}:{repair_id or weld_no}"

    # 施焊缝：逐焊口；补焊道次：逐返修
    targets: list[tuple[str, WeldRecord, RepairRecord | None]] = []
    for w in payload.welds:
        targets.append(("production", w, None))
    repair_by_id = {r.repair_id: r for r in payload.repairs}
    for r in payload.repairs:
        targets.append(("repair", weld_index[r.weld_no], r))

    for scope, weld, repair in targets:
        key = _scope_key(scope, weld.weld_no,
                         repair.repair_id if repair else None)
        passes = passes_by_scope.get(key, [])
        measures = measures_by_scope.get(key, [])
        label = ("返修" + f"#{repair.iteration}({repair.repair_id})"
                 if repair else "施焊")
        if not passes:
            # 链启用后逐焊口/逐返修必须提交道次，不得仅有 WPS 编号与完工时刻
            structural_gaps.append({
                "kind": "passes_missing",
                "detail": f"{label}范围 {key} 未提交任何道次记录",
                "weld_no": weld.weld_no,
                "repair_id": repair.repair_id if repair else None})
            state = {
                "scope_key": key, "label": label,
                "weld_no": weld.weld_no, "scope": scope,
                "repair_id": repair.repair_id if repair else None,
                "iteration": repair.iteration if repair else None,
                "wps_no": repair.wps_no if repair else weld.wps_no,
                "pass_total": 0, "passes_valid": False,
                "reasons": ["WP-PASS-UNTRACED"], "findings": [
                    _f("WP-PASS-UNTRACED",
                       weld_no=weld.weld_no,
                       repair_id=repair.repair_id if repair else None,
                       scope=scope,
                       evidence={"scope": label})],
                "passes": [], "measurements": [],
            }
            scope_states[key] = state
            continue

        state = verify_scope(
            label=label, scope=scope, weld=weld, repair=repair,
            passes=passes, measurements=measures,
            wps_index=wps_index, welder_index=welder_index,
            gauge_index=gauge_index, structural_gaps=structural_gaps)
        scope_states[key] = state

    # ---- 跨范围：同一焊工不得在重叠时段施焊（不同焊口/施焊与返修之间）----
    all_pass_refs: list[tuple[PassRecord, str]] = [
        (p, key) for key, state in scope_states.items()
        for p in passes_by_scope.get(key, [])
    ]
    for i in range(len(all_pass_refs)):
        pa, ka = all_pass_refs[i]
        for j in range(i + 1, len(all_pass_refs)):
            pb, kb = all_pass_refs[j]
            if pa.welder_id != pb.welder_id:
                continue
            if pa.started_at < pb.finished_at and pb.started_at < pa.finished_at:
                sa, sb = scope_states[ka], scope_states[kb]
                finding = _f(
                    "WP-PASS-OVERLAP",
                    weld_no=pa.weld_no, repair_id=pa.repair_id,
                    pass_id=pa.pass_id, scope=pa.scope,
                    evidence={
                        "reason": "同一焊工的道次时段跨焊口/返修重叠",
                        "welder_id": pa.welder_id,
                        "pass_a": {"scope_key": ka, "pass_id": pa.pass_id,
                                   "pass_no": pa.pass_no,
                                   "period": [_iso(pa.started_at),
                                              _iso(pa.finished_at)]},
                        "pass_b": {"scope_key": kb, "pass_id": pb.pass_id,
                                   "pass_no": pb.pass_no,
                                   "period": [_iso(pb.started_at),
                                              _iso(pb.finished_at)]}})
                sa["findings"].append(finding)
                sa["reasons"] = sorted(set(sa["reasons"] + ["WP-PASS-OVERLAP"]))
                sa["passes_valid"] = False
                other = dict(finding)
                other.update({"weld_no": pb.weld_no,
                              "repair_id": pb.repair_id,
                              "pass_id": pb.pass_id, "scope": pb.scope})
                sb["findings"].append(other)
                sb["reasons"] = sorted(set(sb["reasons"] + ["WP-PASS-OVERLAP"]))
                sb["passes_valid"] = False

    # 跨范围重叠已追加条款，重新汇总视图 reasons
    for state in scope_states.values():
        codes_by_pass: dict[str, set[str]] = defaultdict(set)
        for f in state["findings"]:
            if f.get("pass_id"):
                codes_by_pass[f["pass_id"]].add(f["code"])
        for view in state["passes"]:
            extra = codes_by_pass.get(view["pass_id"], set())
            merged = sorted(set(view["reasons"]) | extra)
            view["reasons"] = merged
            view["pass_valid"] = not merged
        state["reasons"] = sorted({f["code"] for f in state["findings"]})
        state["passes_valid"] = not state["reasons"]

    findings: list[dict] = []
    for state in scope_states.values():
        findings.extend(state["findings"])

    affected_welds = sorted({
        g["weld_no"] for g in structural_gaps if g.get("weld_no")
    } | {state["weld_no"] for state in scope_states.values()
         if not state["passes_valid"]})

    return {
        "enabled": True,
        "scope_states": scope_states,
        "findings": findings,
        "gauge_index": gauge_index,
        "gaps": structural_gaps,
        "affected_welds": affected_welds,
        "freeze_blocked": bool(structural_gaps),
    }
