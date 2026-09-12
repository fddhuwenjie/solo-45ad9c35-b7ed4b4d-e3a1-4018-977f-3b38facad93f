"""焊接道次执行链回归测试（WP 系列）。

覆盖：
- 正向：合规道次链（GTAW 打底 + SMAW 填充/盖面，起弧前测温、仪表全年有效）
  无 WP 条款，热输入按 E=k·U·I·60/v 换算；
- 道次重号/时段重叠/层序断档/时间序倒置；
- 极性/电流/电压/焊速/热输入越限；道次焊工与登记不一致/未登记；
- 预热/层间温度采样缺失与越限、测温时点倒置、测温仪表未登记/校准失效；
- 未提交道次（仅有 WPS 编号与完工时刻）-> 结构缺口，审查包不得冻结；
  参数越限等规则性 hold 记录齐全时可冻结，纠错从旧版派生修订；
- 返修混用超窗口参数：即使复检 RT 合格，该次返修也不得闭合
  （WP-REPAIR-PASS-INVALID 不被最终合格报告掩盖）；
- 版本差异：weld_pass_evaluation 呈现条款消长与焊口 invalid_to_valid 翻转。
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.engine import evaluate
from app.schemas import SubmissionPayload
from app.weld_execution import heat_input_kj_mm
from examples.sample_data import (
    extension_payload,
    weld_pass_chain,
    weld_pass_failure_payload,
    wps_process_window,
)

UTC = timezone.utc


# ---------------------------------------------------------------- 夹具


@pytest.fixture()
def baseline():
    """缺陷扩检样例 + 合规道次链（随 WPS 冻结 GTAW/SMAW 方法窗口）。"""
    p = extension_payload().model_copy(deep=True)
    for w in p.wps:
        w.process_windows = [wps_process_window("GTAW"),
                             wps_process_window("SMAW")]
    gauges, passes, measures = weld_pass_chain(p.welds, p.repairs)
    p.weld_gauges, p.weld_passes, p.temperature_measurements = (
        gauges, passes, measures)
    return p


def _revalidate(payload) -> SubmissionPayload:
    return SubmissionPayload.model_validate(payload.model_dump(mode="json"))


def _weld(r, no):
    return next(w for w in r["welds"] if w["weld_no"] == no)


def _codes(r, no=None):
    if no is not None:
        return {f["code"] for f in _weld(r, no)["findings"]}
    return {f["code"] for f in r["findings"]}


def _override_pass(payload, weld_no, pass_number, **kw):
    for p in payload.weld_passes:
        if p.weld_no == weld_no and p.scope == "production" \
                and p.pass_no == pass_number:
            for k, v in kw.items():
                setattr(p, k, v)




# ---------------------------------------------------------------- 正向


def test_heat_input_formula():
    assert heat_input_kj_mm(k=0.8, voltage=22, current=120,
                            speed_mm_min=79.8) == pytest.approx(1.588, abs=1e-3)


def test_valid_pass_chain_releases(baseline):
    r = evaluate(baseline)
    assert r["decision"] == "release"
    assert r["stats"]["weld_pass_chain_enabled"] is True
    assert r["stats"]["weld_pass_scopes_invalid"] == 0
    wp = {c for c in r["clauses_triggered"] if c.startswith("WP")}
    assert wp == set()
    w = _weld(r, "W-001")
    assert w["passes_valid"] is True
    assert [p["pass_no"] for p in w["weld_passes"]] == [1, 2, 3]
    assert [p["layer_no"] for p in w["weld_passes"]] == [1, 2, 3]
    assert [p["role"] for p in w["weld_passes"]] == ["root", "fill", "cap"]
    # 热输入换算与窗口冻结摘要
    p1 = w["weld_passes"][0]
    assert p1["travel_speed_mm_min"] == pytest.approx(89.75, abs=0.01)
    assert p1["heat_input_kj_mm"] == pytest.approx(0.548, abs=1e-3)
    assert p1["window"]["wps_no"] == "WPS-101"
    assert p1["window"]["current_a"] == [80.0, 130.0]
    assert p1["governing_temperature"]["kind"] == "preheat"
    # 返修道次视图挂在返修链节上且链闭合
    link = w["repairs"][0]
    assert link["passes_valid"] is True
    assert len(link["weld_passes"]) == 3
    assert link["closed"] is True


def test_disabled_when_chain_absent():
    """旧载荷（无任何道次/仪表/测温）保持道次链未启用，行为不变。"""
    r = evaluate(extension_payload())
    assert r["stats"]["weld_pass_chain_enabled"] is False
    assert r["weld_execution"]["enabled"] is False
    w = _weld(r, "W-001")
    assert w["passes_valid"] is None and w["weld_passes"] == []


# ---------------------------------------------------------------- 序列问题


def test_duplicate_pass_number(baseline):
    _override_pass(baseline, "W-002", 3, **{"pass_no": 2})
    r = evaluate(_revalidate(baseline))
    assert "WP-PASS-DUP" in _codes(r, "W-002")
    assert _weld(r, "W-002")["decision"] == "hold"


def test_layer_gap(baseline):
    # 道次编号连续但层号跳层（1 -> 3）
    _override_pass(baseline, "W-002", 2, layer_no=3)
    _override_pass(baseline, "W-002", 3, layer_no=4)
    r = evaluate(_revalidate(baseline))
    assert "WP-LAYER-GAP" in _codes(r, "W-002")


def test_pass_overlap_within_scope(baseline):
    # 让 W-002 第 2 道起弧早于第 1 道收弧
    p1 = next(p for p in baseline.weld_passes
              if p.weld_no == "W-002" and p.scope == "production"
              and p.pass_no == 1)
    _override_pass(baseline, "W-002", 2, started_at=p1.finished_at
                   - timedelta(minutes=1))
    r = evaluate(_revalidate(baseline))
    assert "WP-PASS-OVERLAP" in _codes(r, "W-002")


def test_cross_scope_welder_overlap(baseline):
    """同一焊工在两道（不同焊口）重叠时段施焊：两个范围都挂条款。"""
    p_a = next(p for p in baseline.weld_passes
               if p.weld_no == "W-002" and p.pass_no == 1)
    # 把 W-003 三道全部挪到 W-002 首道时段内
    for p in [p for p in baseline.weld_passes
              if p.weld_no == "W-003"]:
        p.started_at = p_a.started_at + timedelta(seconds=10)
        p.finished_at = p_a.finished_at - timedelta(seconds=10)
    r = evaluate(_revalidate(baseline))
    codes = _codes(r)
    assert "WP-PASS-OVERLAP" in codes
    assert "WP-PASS-OVERLAP" in _codes(r, "W-002")
    assert "WP-PASS-OVERLAP" in _codes(r, "W-003")


def test_pass_after_completed_at(baseline):
    """道次收弧晚于焊口登记完工时刻：时间序倒置。"""
    _override_pass(baseline, "W-002", 3,
                   finished_at=datetime(2026, 3, 2, 9, 30, tzinfo=UTC))
    r = evaluate(_revalidate(baseline))
    assert "WP-PASS-ORDER" in _codes(r, "W-002")


# ---------------------------------------------------------------- 参数越限


@pytest.mark.parametrize("pass_no,field,value,code", [
    (1, "polarity", "DCEP", "WP-POLARITY"),
    (2, "current_a", 200.0, "WP-CURRENT-OUTSIDE"),
    (2, "voltage_v", 30.0, "WP-VOLTAGE-OUTSIDE"),
])
def test_parameter_window_violations(baseline, pass_no, field, value, code):
    _override_pass(baseline, "W-002", pass_no, **{field: value})
    r = evaluate(_revalidate(baseline))
    assert code in _codes(r, "W-002")


def test_travel_speed_outside(baseline):
    # 燃弧 2 分钟焊 359mm => 179.5 mm/min，超 SMAW 上限 150
    _override_pass(baseline, "W-002", 2, arc_minutes=2.0)
    r = evaluate(_revalidate(baseline))
    assert "WP-TRAVEL-OUTSIDE" in _codes(r, "W-002")
    view = next(p for p in _weld(r, "W-002")["weld_passes"]
                if p["pass_no"] == 2)
    assert view["travel_speed_mm_min"] == pytest.approx(179.5, abs=0.01)


def test_heat_input_outside(baseline):
    # 电流 150A、电压 25V、慢速 => 热输入超 2.5 kJ/mm 上限（SMAW 窗口内）
    _override_pass(baseline, "W-002", 2,
                   current_a=150.0, voltage_v=25.0, arc_minutes=20.0)
    r = evaluate(_revalidate(baseline))
    assert "WP-HEATINPUT-OUTSIDE" in _codes(r, "W-002")


def test_wps_window_missing():
    """链启用但 WPS 未冻结该方法窗口：无据可核，且禁止冻结。"""
    p = extension_payload().model_copy(deep=True)  # wps 无 process_windows
    gauges, passes, measures = weld_pass_chain(p.welds, p.repairs)
    p.weld_gauges, p.weld_passes, p.temperature_measurements = (
        gauges, passes, measures)
    r = evaluate(p)
    assert "WP-PASS-WINDOW-MISSING" in _codes(r, "W-001")
    assert r["weld_execution"]["freeze_blocked"] is True
    kinds = {g["kind"] for g in r["weld_execution"]["completeness"]["gaps"]}
    assert "wps_window_missing" in kinds


def test_welder_mismatch_with_registered(baseline):
    _override_pass(baseline, "W-002", 2, welder_id="W-999")
    r = evaluate(_revalidate(baseline))
    assert "WP-WELDER-MISMATCH" in _codes(r, "W-002")


# ---------------------------------------------------------------- 测温


def test_interpass_high(baseline):
    m = next(m for m in baseline.temperature_measurements
             if m.weld_no == "W-002" and m.pass_no == 2)
    m.temp_c = 240.0
    r = evaluate(baseline)
    assert "WP-INTERPASS-HIGH" in _codes(r, "W-002")


def test_preheat_low(baseline):
    m = next(m for m in baseline.temperature_measurements
             if m.weld_no == "W-002" and m.kind == "preheat")
    m.temp_c = 40.0  # 低于下限 100
    r = evaluate(baseline)
    assert "WP-PREHEAT-LOW" in _codes(r, "W-002")


def _baseline_with_windows(**window_kw):
    """用自定义 GTAW/SMAW 窗口参数重建合规道次链基线。"""
    p = extension_payload().model_copy(deep=True)
    p.wps[0].process_windows = [
        wps_process_window("GTAW", **window_kw),
        wps_process_window("SMAW", **window_kw)]
    gauges, passes, measures = weld_pass_chain(p.welds, p.repairs)
    p.weld_gauges, p.weld_passes, p.temperature_measurements = (
        gauges, passes, measures)
    return p


def test_preheat_sample_required_even_when_min_zero():
    """预热下限为 0（不强制预热）时，首道仍必须有可追溯预热测点。"""
    p = _baseline_with_windows(preheat_min_c=0.0)
    # 基线含首道预热测点：放行
    r = evaluate(p)
    assert _weld(r, "W-002")["decision"] == "release"
    # 删除全部首道预热测点：每口首道均判 WP-PREHEAT-MISSING 并 hold
    p.temperature_measurements = [
        m for m in p.temperature_measurements if m.kind != "preheat"]
    r = evaluate(p)
    assert _weld(r, "W-002")["decision"] == "hold"
    codes = _codes(r, "W-002")
    assert "WP-PREHEAT-MISSING" in codes
    f = next(f for f in _weld(r, "W-002")["findings"]
             if f["code"] == "WP-PREHEAT-MISSING")
    # 定位到缺失项：首道道次编号；测点缺样时 measure_id 为空
    assert f["pass_id"]
    assert f["evidence"]["preheat_min_c"] == 0.0


def test_preheat_high(baseline):
    """预热温度超 WPS 上限：WP-PREHEAT-HIGH，定位测点与道次。"""
    # 基线窗口未设预热上限，先冻结 preheat_max_c=150
    for win in baseline.wps[0].process_windows:
        win.preheat_max_c = 150.0
    m = next(m for m in baseline.temperature_measurements
             if m.weld_no == "W-002" and m.kind == "preheat")
    m.temp_c = 180.0
    r = evaluate(_revalidate(baseline))
    codes = _codes(r, "W-002")
    assert "WP-PREHEAT-HIGH" in codes
    f = next(f for f in _weld(r, "W-002")["findings"]
             if f["code"] == "WP-PREHEAT-HIGH")
    assert f["measure_id"] == m.measure_id and f["pass_id"]
    assert f["evidence"] == {"pass_no": 1, "actual_c": 180.0, "max_c": 150.0}


def test_preheat_within_max_keeps_release():
    p = _baseline_with_windows(preheat_max_c=150.0)  # 默认预热 120
    r = evaluate(p)
    assert r["decision"] == "release"
    assert "WP-PREHEAT-HIGH" not in _codes(r)


def test_preheat_max_below_min_rejected():
    from app.schemas import WpsProcessWindow
    with pytest.raises(Exception):
        WpsProcessWindow(
            process="SMAW", polarity=["DCEP"],
            current_min_a=100, current_max_a=160,
            voltage_min_v=20, voltage_max_v=26,
            interpass_max_c=200, preheat_min_c=120, preheat_max_c=80)


def test_interpass_measurement_missing(baseline):
    baseline.temperature_measurements = [
        m for m in baseline.temperature_measurements
        if not (m.weld_no == "W-002" and m.pass_no == 2)]
    r = evaluate(baseline)
    assert "WP-INTERPASS-MISSING" in _codes(r, "W-002")
    f = next(f for f in _weld(r, "W-002")["findings"]
             if f["code"] == "WP-INTERPASS-MISSING")
    assert f["pass_id"]  # 定位到原始道次


def test_measurement_after_pass_start(baseline):
    """测温时点落在所对应道次起弧之后（事后补测）：WP-MEASURE-LAG。"""
    p2 = next(p for p in baseline.weld_passes
              if p.weld_no == "W-002" and p.pass_no == 2)
    m = next(m for m in baseline.temperature_measurements
             if m.weld_no == "W-002" and m.pass_no == 2)
    m.measured_at = p2.started_at + timedelta(minutes=1)
    r = evaluate(_revalidate(baseline))
    assert "WP-MEASURE-LAG" in _codes(r, "W-002")
    assert "WP-INTERPASS-MISSING" in _codes(r, "W-002")  # 该道仍判采样缺失


def test_temperature_gauge_calibration_expired(baseline):
    tm = next(g for g in baseline.weld_gauges if g.gauge_id == "TM-1")
    tm.calibrated_to = datetime(2026, 3, 4, 0, 0, tzinfo=UTC)
    r = evaluate(baseline)
    # 3/5 之后的焊口/返修测温落在失效区间
    assert "WP-GAUGE-CALIBRATION" in _codes(r)


# ---------------------------------------------------------------- 仪表


def test_pass_gauge_unregistered_blocks_freeze(baseline):
    """道次引用未登记仪表版本：hold 且结构缺口禁止冻结。"""
    for p in baseline.weld_passes:
        if p.weld_no == "W-002":
            for u in p.gauges:
                u.version = "2099X"
    r = evaluate(_revalidate(baseline))
    assert "WP-GAUGE-MISSING" in _codes(r, "W-002")
    assert r["weld_execution"]["freeze_blocked"] is True


def test_monitor_gauge_calibration_expired(baseline):
    wm = next(g for g in baseline.weld_gauges if g.gauge_id == "WM-1")
    wm.calibrated_to = datetime(2026, 3, 4, 0, 0, tzinfo=UTC)
    r = evaluate(baseline)
    # 3/5 返修（及 3/5 之后焊口）的道次跨校准到期点
    assert "WP-GAUGE-CALIBRATION" in _codes(r, "W-001")
    # 类别仍匹配（weld_monitor），不重复发 WP-GAUGE-KIND
    assert "WP-GAUGE-KIND" not in _codes(r, "W-001")


def _point_passes_at_gauge(payload, weld_no, gauge_id, version):
    from app.schemas import GaugeUse
    for p in payload.weld_passes:
        if p.weld_no == weld_no and p.scope == "production":
            p.gauges = [GaugeUse(gauge_id=gauge_id, version=version,
                                 role="main")]


def test_thermometer_cannot_prove_electrical_params(baseline):
    """道次只挂测温仪 TM-1：类别不匹配，电流/电压/焊速无证明 -> hold。"""
    _point_passes_at_gauge(baseline, "W-002", "TM-1", "2026A")
    r = evaluate(_revalidate(baseline))
    codes = _codes(r, "W-002")
    assert "WP-GAUGE-KIND" in codes
    assert _weld(r, "W-002")["decision"] == "hold"
    # 每道都缺三类参数证明，且定位到具体道次
    kind_findings = [f for f in _weld(r, "W-002")["findings"]
                     if f["code"] == "WP-GAUGE-KIND"]
    assert {f["pass_id"] for f in kind_findings} == {
        p.pass_id for p in baseline.weld_passes
        if p.weld_no == "W-002" and p.scope == "production"}
    ev = kind_findings[0]["evidence"]
    assert set(ev["missing_purposes"]) == {"current", "voltage", "travel"}
    assert ev["referenced_gauges"][0]["kind"] == "thermometer"
    # 规则性 hold（记录齐全）：可冻结，不属于结构缺口
    assert r["weld_execution"]["freeze_blocked"] is False


def test_split_param_gauges_all_valid_release(baseline):
    """电流表/电压表/计时器分列且各自校准有效：类别匹配，正常放行。"""
    from app.schemas import GaugeUse, WeldGaugeVersion
    gauges = list(baseline.weld_gauges) + [
        WeldGaugeVersion(gauge_id="AM-1", version="2026A", name="电流表",
                         kind="ammeter", serial_no="SN-AM-1",
                         calibrated_from="2026-01-01T00:00:00Z",
                         calibrated_to="2026-12-31T23:59:59Z"),
        WeldGaugeVersion(gauge_id="VM-1", version="2026A", name="电压表",
                         kind="voltmeter", serial_no="SN-VM-1",
                         calibrated_from="2026-01-01T00:00:00Z",
                         calibrated_to="2026-12-31T23:59:59Z"),
        WeldGaugeVersion(gauge_id="TMER-1", version="2026A", name="计时器",
                         kind="timer", serial_no="SN-TMER-1",
                         calibrated_from="2026-01-01T00:00:00Z",
                         calibrated_to="2026-12-31T23:59:59Z"),
    ]
    baseline.weld_gauges = gauges
    for p in baseline.weld_passes:
        if p.weld_no == "W-002" and p.scope == "production":
            p.gauges = [
                GaugeUse(gauge_id="AM-1", version="2026A", role="ammeter"),
                GaugeUse(gauge_id="VM-1", version="2026A", role="voltmeter"),
                GaugeUse(gauge_id="TMER-1", version="2026A", role="timer"),
            ]
    r = evaluate(_revalidate(baseline))
    assert "WP-GAUGE-KIND" not in _codes(r, "W-002")
    assert _weld(r, "W-002")["decision"] == "release"


def test_split_param_gauges_one_kind_missing(baseline):
    """只挂电流表+电压表、无计时器：焊速无类别匹配 -> WP-GAUGE-KIND。"""
    from app.schemas import GaugeUse, WeldGaugeVersion
    baseline.weld_gauges += [
        WeldGaugeVersion(gauge_id="AM-1", version="2026A", name="电流表",
                         kind="ammeter", serial_no="SN-AM-1",
                         calibrated_from="2026-01-01T00:00:00Z",
                         calibrated_to="2026-12-31T23:59:59Z"),
        WeldGaugeVersion(gauge_id="VM-1", version="2026A", name="电压表",
                         kind="voltmeter", serial_no="SN-VM-1",
                         calibrated_from="2026-01-01T00:00:00Z",
                         calibrated_to="2026-12-31T23:59:59Z"),
    ]
    for p in baseline.weld_passes:
        if p.weld_no == "W-002" and p.scope == "production":
            p.gauges = [
                GaugeUse(gauge_id="AM-1", version="2026A", role="ammeter"),
                GaugeUse(gauge_id="VM-1", version="2026A", role="voltmeter"),
            ]
    r = evaluate(_revalidate(baseline))
    findings = [f for f in _weld(r, "W-002")["findings"]
                if f["code"] == "WP-GAUGE-KIND"]
    assert findings
    assert all(f["evidence"]["missing_purposes"] == ["travel"]
               for f in findings)


def test_temperature_measurement_with_non_thermometer_gauge(baseline):
    """测温记录挂焊接监测仪（非 thermometer）：用途类别不匹配 -> hold。"""
    for m in baseline.temperature_measurements:
        if m.weld_no == "W-002":
            m.gauge_id, m.gauge_version = "WM-1", "2026A"
    r = evaluate(_revalidate(baseline))
    codes = _codes(r, "W-002")
    assert "WP-GAUGE-KIND" in codes
    f = next(f for f in _weld(r, "W-002")["findings"]
             if f["code"] == "WP-GAUGE-KIND" and f.get("measure_id"))
    assert f["evidence"]["required_kind"] == "thermometer"


def test_no_passes_submitted_blocks_freeze(baseline):
    """链已启用（仪表/测温存在）但焊口无道次：WP-PASS-UNTRACED + 禁冻。"""
    baseline.weld_passes = []
    baseline.temperature_measurements = []
    r = evaluate(baseline)
    assert "WP-PASS-UNTRACED" in _codes(r, "W-001")
    assert r["weld_execution"]["freeze_blocked"] is True
    kinds = {g["kind"] for g in r["weld_execution"]["completeness"]["gaps"]}
    assert "passes_missing" in kinds


# ---------------------------------------------------------------- 返修闭合


def test_repair_mixed_params_not_masked_by_accepted_report():
    """返修道次电流超窗口：复检合格也不得闭合返修链（场景六核心）。"""
    r = evaluate(weld_pass_failure_payload())
    w = _weld(r, "W-001")
    link = w["repairs"][0]
    assert link["passes_valid"] is False
    assert link["closed"] is False
    assert "WP-REPAIR-PASS-INVALID" in _codes(r, "W-001")
    # 返修链节里能定位到超窗口道次
    bad = [p for p in link["weld_passes"]
           if "WP-CURRENT-OUTSIDE" in p["reasons"]]
    assert bad and bad[0]["pass_no"] == 3
    # 复检 RT 合格的事实仍在，但结论保持 hold
    assert link["reinspections"] and w["decision"] == "hold"


def test_scenario6_target_codes_and_scope_inventory():
    r = evaluate(weld_pass_failure_payload())
    wp = {c for c in r["clauses_triggered"] if c.startswith("WP")}
    assert {
        "WP-CURRENT-OUTSIDE", "WP-GAUGE-CALIBRATION",
        "WP-INTERPASS-HIGH", "WP-INTERPASS-MISSING",
        "WP-REPAIR-PASS-INVALID",
    } <= wp
    invalid = {s["scope_key"]: s for s in r["weld_execution"]["invalid_scopes"]}
    # 失效范围清单逐道列出违规道次与测点
    w7 = invalid["production:W-007"]
    assert [p["pass_no"] for p in w7["invalid_passes"]] == [3]
    repair_scope = invalid["repair:R-001"]
    assert "WP-GAUGE-CALIBRATION" in repair_scope["reasons"]
    # 结构完整（记录齐全的规则性 hold）：允许冻结供修订纠错
    assert r["weld_execution"]["freeze_blocked"] is False


# ---------------------------------------------------------------- API：冻结与差异


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "weld_pass_test.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))
    with TestClient(main.app) as c:
        yield c


def _json(payload):
    return payload.model_dump(mode="json")


def test_structural_gap_review_returns_409(client, baseline):
    baseline.weld_passes = []
    baseline.temperature_measurements = []
    pid = client.post("/packages", json=_json(baseline)).json()["package_id"]
    resp = client.post(f"/packages/{pid}/review", json={"reviewer": "李"})
    assert resp.status_code == 409
    assert "道次" in resp.json()["error"]


def test_rule_hold_freezable_but_not_issuable(client):
    """参数越限等规则性 hold 记录齐全：可冻结，不可签发。"""
    body = _json(weld_pass_failure_payload())
    pid = client.post("/packages", json=body).json()["package_id"]
    r = client.post(f"/packages/{pid}/review",
                    json={"reviewer": "责任工程师-李"})
    assert r.status_code == 200
    assert r.json()["status"] == "frozen"
    assert client.post(f"/packages/{pid}/issue",
                       json={"issuer": "赵"}).status_code == 409


def test_diff_weld_pass_evaluation_state_flip(client, baseline):
    """v1 盖面道电流超窗 -> 冻结 -> v2 修正参数；差异呈现翻转与条款消长。"""
    bad = baseline.model_copy(deep=True)
    _override_pass(bad, "W-007", 3, current_a=200.0)
    bad = _revalidate(bad)
    pid = client.post("/packages", json=_json(bad)).json()["package_id"]
    client.post(f"/packages/{pid}/review", json={"reviewer": "李"})

    # 修订：合规基线
    r2 = client.post(f"/packages/{pid}/revisions", json=_json(baseline))
    assert r2.status_code == 200
    diff = client.get(f"/packages/{pid}/diff?from_version=1&to_version=2").json()
    ev = diff["weld_pass_evaluation"]
    assert ev is not None
    assert "WP-CURRENT-OUTSIDE" in ev["resolved"]
    keys = {c["scope_key"]: c["change"] for c in ev["changed_scopes"]}
    assert keys.get("production:W-007") == "invalid_to_valid"
    assert diff["decision_changed"] is True
    assert diff["old_decision"] == "hold"
    assert diff["new_decision"] == "release"
    # 快照差异也能看到被修订的道次记录
    assert any(ch["section"] == "weld_passes" for ch in diff["changes"])


def test_preview_shape_contains_weld_execution(client, baseline):
    body = client.post("/preview", json=_json(baseline)).json()
    we = body["weld_execution"]
    assert we["enabled"] is True
    assert {g["gauge_id"] for g in we["gauges"]} == {"WM-1", "TM-1"}
    assert we["freeze_blocked"] is False
    wps_windows = {w["wps_no"]: [x["process"] for x in w["process_windows"]]
                   for w in we["wps_windows"]}
    assert wps_windows["WPS-101"] == ["GTAW", "SMAW"]
    # /clauses 目录含 WP 系列
    codes = {c["code"] for c in client.get("/clauses").json()["clauses"]}
    assert {
        "WP-PASS-DUP", "WP-PASS-OVERLAP", "WP-LAYER-GAP",
        "WP-CURRENT-OUTSIDE", "WP-HEATINPUT-OUTSIDE",
        "WP-PREHEAT-MISSING", "WP-PREHEAT-HIGH",
        "WP-INTERPASS-HIGH", "WP-GAUGE-CALIBRATION", "WP-GAUGE-KIND",
        "WP-REPAIR-PASS-INVALID",
    } <= codes


def test_gauge_kind_rule_hold_full_lifecycle(client, baseline):
    """thermometer-only 属记录齐全的规则性 hold：可冻结/修订/签发。"""
    bad = baseline.model_copy(deep=True)
    _point_passes_at_gauge(bad, "W-002", "TM-1", "2026A")
    bad = _revalidate(bad)
    pid = client.post("/packages", json=_json(bad)).json()["package_id"]
    # 可冻结（非结构缺口），但不可签发
    rr = client.post(f"/packages/{pid}/review",
                     json={"reviewer": "责任工程师-李"})
    assert rr.status_code == 200 and rr.json()["status"] == "frozen"
    assert client.post(f"/packages/{pid}/issue",
                       json={"issuer": "赵"}).status_code == 409
    # 从冻结旧版派生修订：换回类别匹配的监测仪 -> release，可签发
    rv = client.post(f"/packages/{pid}/revisions", json=_json(baseline))
    assert rv.json()["version"] == 2 and rv.json()["decision"] == "release"
    client.post(f"/packages/{pid}/review", json={"reviewer": "李"})
    assert client.post(f"/packages/{pid}/issue",
                       json={"issuer": "赵"}).status_code == 200
    diff = client.get(f"/packages/{pid}/diff?from_version=1&to_version=2").json()
    ev = diff["weld_pass_evaluation"]
    assert "WP-GAUGE-KIND" in ev["resolved"]
    keys = {c["scope_key"]: c["change"] for c in ev["changed_scopes"]}
    assert keys.get("production:W-002") == "invalid_to_valid"
    # 旧版仍冻结保留 WP-GAUGE-KIND
    v1 = client.get(f"/packages/{pid}?version=1").json()
    assert "WP-GAUGE-KIND" in v1["clauses_triggered"]


def test_preheat_max_frozen_and_revised(client):
    """preheat_max_c 随窗口冻结；超上限 hold，修订降温后放行，diff 呈现翻转。"""
    hot = _baseline_with_windows(preheat_max_c=150.0)
    m = next(m for m in hot.temperature_measurements
             if m.weld_no == "W-002" and m.kind == "preheat")
    m.temp_c = 180.0
    pid = client.post("/packages", json=_json(hot)).json()["package_id"]
    client.post(f"/packages/{pid}/review", json={"reviewer": "李"})
    v1 = client.get(f"/packages/{pid}?version=1").json()
    assert "WP-PREHEAT-HIGH" in v1["clauses_triggered"]
    win = v1["weld_execution"]["wps_windows"][0]["process_windows"][0]
    assert win["preheat_max_c"] == 150.0

    fixed = _baseline_with_windows(preheat_max_c=150.0)  # 默认预热 120
    rv = client.post(f"/packages/{pid}/revisions",
                     json=_json(fixed)).json()
    assert rv["decision"] == "release"
    diff = client.get(f"/packages/{pid}/diff?from_version=1&to_version=2").json()
    ev = diff["weld_pass_evaluation"]
    assert "WP-PREHEAT-HIGH" in ev["resolved"]
    assert any(ch["section"] == "temperature_measurements"
               for ch in diff["changes"])

