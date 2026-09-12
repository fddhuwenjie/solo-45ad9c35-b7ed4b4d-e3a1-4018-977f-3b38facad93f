"""引擎条款测试：资质时点、越界、抽检/扩检、返修链。"""
from __future__ import annotations

import pytest

from app.engine import evaluate
from app.schemas import SubmissionPayload, WelderQual, WpsRecord

from examples.sample_data import (
    direct_release_payload,
    double_repair_payload,
    extension_payload,
    repair,
    rt,
    weld,
    welder,
    wps,
)


def revalidate(payload: SubmissionPayload) -> SubmissionPayload:
    """深拷贝并按模型重校：字段级字符串赋值后需经模型归一化（时间等）。"""
    return SubmissionPayload.model_validate(payload.model_dump(mode="json"))


def codes(payload: SubmissionPayload) -> set[str]:
    return set(evaluate(payload)["clauses_triggered"])


def weld_codes(payload: SubmissionPayload, weld_no: str) -> set[str]:
    r = evaluate(payload)
    verdict = next(w for w in r["welds"] if w["weld_no"] == weld_no)
    return {f["code"] for f in verdict["findings"]}


# ---------------------------------------------------------------- 直接放行


def test_direct_release():
    r = evaluate(direct_release_payload())
    assert r["decision"] == "release"
    assert r["clauses_triggered"] == []
    assert r["stats"]["weld_hold"] == 0


# ---------------------------------------------------------------- 资质时点


def test_welder_expired_at_welding_time():
    """焊工证 2 月底到期，焊口 3 月施焊 => 按施焊时点判失效。"""
    data = direct_release_payload().model_dump(mode="json")
    data["welders"][0]["valid_to"] = "2026-02-28T23:59:59Z"
    p2 = SubmissionPayload.model_validate(data)
    c = weld_codes(p2, "W-001")
    assert "WQ-WELDER-EXPIRED" in c
    assert evaluate(p2)["decision"] == "hold"


def test_welder_future_qualification_not_valid_at_weld_time():
    """证在施焊后才生效：不能用后补资格放行。"""
    data = direct_release_payload().model_dump(mode="json")
    data["welders"][0]["valid_from"] = "2026-04-01T00:00:00Z"
    data["welders"][0]["valid_to"] = "2027-04-01T00:00:00Z"
    p = SubmissionPayload.model_validate(data)
    assert "WQ-WELDER-EXPIRED" in weld_codes(p, "W-001")


def test_material_group_outside_wps_and_welder():
    p = direct_release_payload().model_copy(deep=True)
    # 把 W-001 两侧母材改为 Fe-4，WPS/焊工只认 Fe-1
    p.welds[0].end_a.material_group = "Fe-4"
    p.welds[0].end_b.material_group = "Fe-4"
    c = weld_codes(p, "W-001")
    assert "WQ-GROUP-OUTSIDE" in c


def test_thickness_outside_qualification():
    p = direct_release_payload().model_copy(deep=True)
    p.welds[0].end_a.thickness_mm = 42.0
    p.welds[0].end_b.thickness_mm = 42.0
    c = weld_codes(p, "W-001")
    assert "WQ-THICKNESS-OUTSIDE" in c


def test_diameter_outside_welder_qual():
    p = direct_release_payload().model_copy(deep=True)
    p.welders[0].diameter_max_mm = 60.0
    assert "WQ-DIAMETER-OUTSIDE" in weld_codes(p, "W-001")


def test_wps_expired_and_no_pqr_support():
    p = direct_release_payload().model_copy(deep=True)
    p.wps[0].supported_by_pqr = False
    assert "WQ-WPS-EXPIRED" in weld_codes(p, "W-001")


def test_process_mismatch():
    p = direct_release_payload().model_copy(deep=True)
    p.wps[0].processes = ["GTAW"]
    assert "WQ-PROCESS-MISMATCH" in weld_codes(p, "W-001")


# ---------------------------------------------------------------- 炉批


def test_heat_missing():
    p = direct_release_payload().model_copy(deep=True)
    p.welds[0].end_a.heat_no = ""
    # 模型允许空串? min_length=1 会先 422；改为直接构造越层字典测引擎不现实，
    # 因此通过 Pydantic 校验拒绝来确认。
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        SubmissionPayload.model_validate(p.model_dump(mode="json"))


def test_hetero_diameter_warning_only():
    p = direct_release_payload().model_copy(deep=True)
    p.welds[0].end_b.nominal_diameter_mm = 88.9
    c = weld_codes(p, "W-001")
    assert "HT-HEAT-DIAMETER-MISMATCH" in c
    assert evaluate(p)["decision"] == "release"  # warning 不阻断


# ---------------------------------------------------------------- 检验批


def test_initial_sampling_insufficient():
    """10 口批 20% 要求 2 张，只交 1 张合格 => LT-SAMPLE-INSUFFICIENT。"""
    p = direct_release_payload().model_copy(deep=True)
    p.nde = [rt("N-001", "W-001", "accept", day=2)]
    r = evaluate(p)
    lot = r["lot_summaries"][0]
    assert lot["decision"] == "hold"
    assert {f["code"] for f in lot["findings"]} == {"LT-SAMPLE-INSUFFICIENT"}
    # 批内所有焊口连带 hold
    assert all(w["decision"] == "hold" for w in r["welds"])


def test_extension_full_mode_requires_all_welds():
    p = extension_payload().model_copy(deep=True)
    p.lot_rules[0].on_reject = "full"
    r = evaluate(p)
    lot = r["lot_summaries"][0]
    # 只检了 4/10，全检扩检未完成
    assert lot["required_final"] == 10
    assert "LT-EXTENSION-INSUFFICIENT" in {f["code"] for f in lot["findings"]}


def test_extension_double_then_second_reject_escalates_to_full():
    """加倍批内再出不合格 => 升级 100%，未补齐即 hold。"""
    p = extension_payload().model_copy(deep=True)
    # 加倍的两口之一不合格但未返修
    for rec in p.nde:
        if rec.nde_id == "N-102":
            rec.result = "reject"
            rec.defect_locations = []  # 需要合法：手动补
    from app.schemas import AngleBand
    rec = next(r for r in p.nde if r.nde_id == "N-102")
    rec.defect_locations = [AngleBand(start_deg=10, end_deg=40)]
    r = evaluate(p)
    lot = r["lot_summaries"][0]
    assert lot["extension_mode"] == "double"
    assert lot["required_final"] == 10
    assert "LT-EXTENSION-INSUFFICIENT" in {f["code"] for f in lot["findings"]}
    # 不合格口未返修闭合
    w007 = next(w for w in r["welds"] if w["weld_no"] == "W-007")
    assert "NDE-OPEN-DEFECT" in {f["code"] for f in w007["findings"]}
    assert "LT-OPEN-REJECT" in {f["code"] for f in lot["findings"]}


def test_lot_rule_missing():
    p = direct_release_payload().model_copy(deep=True)
    p.lot_rules = []  # 焊口仍带 lot_id
    r = evaluate(p)
    assert "LT-RULE-MISSING" in codes(p)
    assert r["decision"] == "hold"


def test_method_mismatch_is_warning():
    p = direct_release_payload().model_copy(deep=True)
    # 检验批规则要求 RT；两张抽检片都改为 UT（资源齐全），隔离出仅批方法不一致警告
    p.nde[0].method = "UT"
    p.nde[1].method = "UT"
    p.lot_rules[0].required_method = "RT"
    # 方法改为 UT 后同步换用 UT 人员证书与设备版本
    from app.schemas import NdePersonnelCert, NdeEquipmentVersion
    p.nde_personnel = [
        NdePersonnelCert(cert_no="NUT-II-ZHANG", name="UT-II-张", method="UT",
                         level="II", products=["pressure_pipe"],
                         techniques=["pulse_echo"],
                         valid_from="2026-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z"),
        NdePersonnelCert(cert_no="NUT-II-LI", name="UT-II-李", method="UT",
                         level="II", products=["pressure_pipe"],
                         techniques=["pulse_echo"],
                         valid_from="2026-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z"),
    ]
    p.nde_equipment = [
        NdeEquipmentVersion(equipment_id="UTD-100", version="2026A",
                            name="数字超声探伤仪", kind="ut_flaw_detector",
                            method="UT", serial_no="SN-UTD-100",
                            calibrated_from="2026-01-01T00:00:00Z",
                            calibrated_to="2026-12-31T23:59:59Z",
                            techniques=["pulse_echo"],
                            range_min_mm=0.0, range_max_mm=500.0),
        NdeEquipmentVersion(equipment_id="PRB-5P", version="2026A",
                            name="5MHz 斜探头", kind="ut_probe", method="UT",
                            serial_no="SN-PRB-5P",
                            calibrated_from="2026-01-01T00:00:00Z",
                            calibrated_to="2026-12-31T23:59:59Z",
                            techniques=["pulse_echo"], range_min_mm=0.0,
                            range_max_mm=100.0),
    ]
    from app.schemas import NdeEquipmentUse
    for rec in p.nde:
        rec.examiner_cert_no = "NUT-II-ZHANG"
        rec.reviewer_cert_no = "NUT-II-LI"
        rec.equipment_uses = [
            NdeEquipmentUse(equipment_id="UTD-100", version="2026A", role="main"),
            NdeEquipmentUse(equipment_id="PRB-5P", version="2026A", role="probe"),
        ]
        rec.technique = "pulse_echo"
        rec.applied_thickness_mm = 12.0
        rec.exposure_energy_kev = None
    assert "NDE-METHOD-LOT-MISMATCH" in weld_codes(p, "W-001")
    assert evaluate(p)["decision"] == "release"


# ---------------------------------------------------------------- 返修链


def test_double_repair_releases():
    r = evaluate(double_repair_payload())
    assert r["decision"] == "release"
    v = r["welds"][0]
    assert [link["iteration"] for link in v["repairs"]] == [1, 2]
    # 一次复检仍不合格 => 一次返修不闭合；二次复检合格 => 二次返修闭合
    assert v["repairs"][0]["closed"] is False
    assert v["repairs"][1]["closed"] is True


def test_unapproved_repair_holds():
    p = double_repair_payload().model_copy(deep=True)
    p.repairs[0].approved = False
    p.repairs[0].approved_by = None
    assert "RP-NO-APPROVAL" in weld_codes(p, "W-901")


def test_no_reinspection_after_repair():
    p = extension_payload().model_copy(deep=True)
    p.nde = [r for r in p.nde if r.nde_id != "N-051"]  # 删掉复检片
    assert "RP-NO-REINSPECTION" in weld_codes(p, "W-001")


def test_old_film_cannot_cover_excavation():
    """返修前底片即使覆盖挖补区也不算复检；无新片 => NO-REINSPECTION。"""
    p = extension_payload().model_copy(deep=True)
    # 原片覆盖 0~360（含挖补区），删掉返修后新片
    p.nde = [r for r in p.nde if r.iteration != 1]
    c = weld_codes(p, "W-001")
    assert "RP-NO-REINSPECTION" in c


def test_reinspection_must_cover_excavated_band():
    p = double_repair_payload(second_covered=False)
    c = weld_codes(p, "W-901")
    assert "RP-EXCAVATION-UNCOVERED" in c


def test_reinspection_wrong_method_does_not_close():
    p = extension_payload().model_copy(deep=True)
    for rec in p.nde:
        if rec.nde_id == "N-051":
            rec.method = "PT"
    c = weld_codes(p, "W-001")
    assert "RP-NO-REINSPECTION" in c


def test_open_defect_without_repair():
    p = extension_payload().model_copy(deep=True)
    p.repairs = []
    p.nde = [r for r in p.nde if r.iteration == 0]
    c = weld_codes(p, "W-001")
    assert "NDE-OPEN-DEFECT" in c


def test_repair_sequence_gap():
    """有二次返修却缺一次返修记录 => RP-SEQ-GAP。"""
    p = double_repair_payload().model_copy(deep=True)
    p.repairs = [r for r in p.repairs if r.iteration != 1]
    p.nde = [r for r in p.nde if r.iteration != 1]
    c = weld_codes(p, "W-901")
    assert "RP-SEQ-GAP" in c


def test_third_repair_rejected_by_schema():
    """同一位置返修上限 2 次：第三次返修在载荷校验层即被拒绝。"""
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        repair("R-903", "W-901", 3, (205, 222), day=12)


def test_repair_qualification_checked_at_repair_time():
    """补焊时点 WPS/焊工资格已失效（生产施焊时有效）=> 按补焊时点判失效。"""
    data = double_repair_payload().model_dump(mode="json")
    data["wps"][0]["valid_to"] = "2026-03-05T23:59:59Z"
    data["welders"][0]["valid_to"] = "2026-03-05T23:59:59Z"
    p = SubmissionPayload.model_validate(data)
    expired_repairs = [
        f for f in evaluate(p)["findings"]
        if f.get("repair_id") == "R-902"
        and f["evidence"].get("scope", "").startswith("repair_")
    ]
    rcodes = {f["code"] for f in expired_repairs}
    assert "WQ-WPS-EXPIRED" in rcodes
    assert "WQ-WELDER-EXPIRED" in rcodes
    # 一次补焊在 3/4 仍在窗内，不应挂资格条款
    r901 = [f for f in evaluate(p)["findings"]
            if f.get("repair_id") == "R-901"]
    assert not any(f["code"].startswith("WQ-") for f in r901)
    # 生产焊缝在 3/1 施焊，其时窗口有效，不应挂生产资格条款
    production = [f for f in expired_repairs
                  if f["evidence"].get("scope") == "production_wps"]
    assert production == []


def test_ndes_none_in_full_ratio_lot():
    """100% 批焊口无任何检测 => NDE-NONE。"""
    p = double_repair_payload().model_copy(deep=True)
    p.nde = []
    p.repairs = []
    p.lot_rules[0].sample_ratio = 1.0
    c = weld_codes(p, "W-901")
    assert "NDE-NONE" in c


# ---------------------------------------------------------------- 缺陷覆盖与时序回归


def _single_repair_payload_dict():
    """以二次返修样例为底，裁成 原始不合格->一次返修->一次复检 的 dict。"""
    data = double_repair_payload().model_dump(mode="json")
    data["repairs"] = [r for r in data["repairs"] if r["iteration"] == 1]
    data["nde"] = [r for r in data["nde"] if r["iteration"] in (0, 1)]
    n0, n1 = data["nde"]
    assert n0["iteration"] == 0 and n1["iteration"] == 1
    return data, n0, n1, data["repairs"][0]


def test_excavation_must_fully_cover_defect_partial_overlap():
    """缺陷 100~140，挖补仅 100~101（仅相交）：不得闭合、整口 hold。"""
    data, n0, n1, rep = _single_repair_payload_dict()
    n0["defect_locations"] = [
        {"start_deg": 100, "end_deg": 140, "full_circle": False}]
    rep["excavated_band"] = {"start_deg": 100, "end_deg": 101,
                             "full_circle": False}
    n1["result"] = "accept"
    n1["defect_locations"] = []
    n1["coverage"] = [{"start_deg": 100, "end_deg": 101,
                       "full_circle": False}]
    p = SubmissionPayload.model_validate(data)
    r = evaluate(p)
    v = next(w for w in r["welds"] if w["weld_no"] == "W-901")
    c = {f["code"] for f in v["findings"]}
    assert "RP-DEFECT-NOT-EXCAVATED" in c
    assert v["repairs"][0]["closed"] is False
    assert v["decision"] == "hold"
    assert r["decision"] == "hold"


def test_excavation_covering_defect_and_reinspection_releases():
    """正向对照：挖补包住缺陷、复检包住挖补 => 闭合放行。"""
    data, n0, n1, rep = _single_repair_payload_dict()
    n0["defect_locations"] = [
        {"start_deg": 100, "end_deg": 140, "full_circle": False}]
    rep["excavated_band"] = {"start_deg": 95, "end_deg": 145,
                             "full_circle": False}
    n1["result"] = "accept"
    n1["defect_locations"] = []
    n1["coverage"] = [{"start_deg": 90, "end_deg": 150,
                       "full_circle": False}]
    p = SubmissionPayload.model_validate(data)
    v = next(w for w in evaluate(p)["welds"] if w["weld_no"] == "W-901")
    assert {f["code"] for f in v["findings"]} == set()
    assert v["repairs"][0]["closed"] is True
    assert v["decision"] == "release"


def test_repair_earlier_than_reject_film_is_order_invalid():
    """补焊时刻早于在先不合格底片：RP-ORDER-INVALID，链不闭合。"""
    data, n0, n1, rep = _single_repair_payload_dict()
    n0["examined_at"] = "2026-03-06T14:00:00Z"
    n0["started_at"] = "2026-03-06T12:00:00Z"
    n0["finished_at"] = "2026-03-06T14:00:00Z"
    rep["repaired_at"] = "2026-03-04T10:00:00Z"
    n1["examined_at"] = "2026-03-08T14:00:00Z"
    n1["started_at"] = "2026-03-08T12:00:00Z"
    n1["finished_at"] = "2026-03-08T14:00:00Z"
    n1["result"] = "accept"
    n1["defect_locations"] = []
    n1["coverage"] = [b for b in [
        {"start_deg": 180, "end_deg": 250, "full_circle": False}]]
    p = SubmissionPayload.model_validate(data)
    r = evaluate(p)
    v = next(w for w in r["welds"] if w["weld_no"] == "W-901")
    c = {f["code"] for f in v["findings"]}
    assert "RP-ORDER-INVALID" in c
    assert v["repairs"][0]["closed"] is False
    assert v["decision"] == "hold"


def test_reinspection_earlier_than_repair_is_order_invalid():
    """复检底片早于补焊：判次序倒置且该底片不得充当复检 => hold。"""
    data, n0, n1, rep = _single_repair_payload_dict()
    n0["examined_at"] = "2026-03-02T14:00:00Z"
    n0["started_at"] = "2026-03-02T12:00:00Z"
    n0["finished_at"] = "2026-03-02T14:00:00Z"
    rep["repaired_at"] = "2026-03-06T10:00:00Z"
    n1["examined_at"] = "2026-03-04T14:00:00Z"  # 早于补焊
    n1["started_at"] = "2026-03-04T12:00:00Z"
    n1["finished_at"] = "2026-03-04T14:00:00Z"
    n1["result"] = "accept"
    n1["defect_locations"] = []
    n1["coverage"] = [{"start_deg": 180, "end_deg": 250,
                       "full_circle": False}]
    p = SubmissionPayload.model_validate(data)
    v = next(w for w in evaluate(p)["welds"] if w["weld_no"] == "W-901")
    c = {f["code"] for f in v["findings"]}
    assert "RP-ORDER-INVALID" in c
    assert "RP-NO-REINSPECTION" in c  # 倒置底片被排除后无有效复检
    assert v["repairs"][0]["closed"] is False
    assert v["decision"] == "hold"


def test_equal_timestamp_is_not_valid_order():
    """补焊与不合格底片同一时刻也不满足严格先后（必须先发现后补焊）。"""
    data, n0, n1, rep = _single_repair_payload_dict()
    n0["examined_at"] = "2026-03-04T10:00:00Z"
    n0["started_at"] = "2026-03-04T08:00:00Z"
    n0["finished_at"] = "2026-03-04T10:00:00Z"
    rep["repaired_at"] = "2026-03-04T10:00:00Z"
    n1["examined_at"] = "2026-03-06T14:00:00Z"
    n1["started_at"] = "2026-03-06T12:00:00Z"
    n1["finished_at"] = "2026-03-06T14:00:00Z"
    n1["result"] = "accept"
    n1["defect_locations"] = []
    n1["coverage"] = [{"start_deg": 180, "end_deg": 250,
                       "full_circle": False}]
    p = SubmissionPayload.model_validate(data)
    v = next(w for w in evaluate(p)["welds"] if w["weld_no"] == "W-901")
    assert "RP-ORDER-INVALID" in {f["code"] for f in v["findings"]}
    assert v["repairs"][0]["closed"] is False


def test_mixed_early_and_late_reinspection_blocks_closure():
    """同一返修轮次同时有早于补焊的 RT 和晚于补焊、覆盖充分的合格 RT：

    虽然存在一张几何覆盖充分且结论合格的复检片，但同轮另一张底片
    时序倒置（RP-ORDER-INVALID）属于阻断性条款，该轮不得闭合，
    链节 closed、整口/整包 decision 必须一致为 hold/false。
    """
    data, n0, n1, rep = _single_repair_payload_dict()
    rep["repaired_at"] = "2026-03-04T10:00:00Z"
    rep["excavated_band"] = {"start_deg": 195, "end_deg": 235,
                             "full_circle": False}
    n1["nde_id"] = "N-EARLY"
    n1["examined_at"] = "2026-03-03T14:00:00Z"   # 早于补焊
    n1["started_at"] = "2026-03-03T12:00:00Z"
    n1["finished_at"] = "2026-03-03T14:00:00Z"
    n1["result"] = "accept"
    n1["defect_locations"] = []
    n1["coverage"] = [{"start_deg": 180, "end_deg": 250,
                       "full_circle": False}]
    # 追加一张晚于补焊、覆盖挖补区的合格片
    data["nde"].append({
        "nde_id": "N-LATE", "weld_no": "W-901", "method": "RT",
        "result": "accept", "examined_at": "2026-03-06T14:00:00Z",
        "started_at": "2026-03-06T12:00:00Z",
        "finished_at": "2026-03-06T14:00:00Z",
        "examiner": "x", "examiner_cert_no": "NRT-II-ZHANG",
        "reviewer_cert_no": "NRT-II-LI",
        "equipment_uses": [{"equipment_id": "XR-250", "version": "2026A",
                            "role": "main"}],
        "technique": "film", "applied_thickness_mm": None,
        "exposure_energy_kev": 160.0, "lot_id": None,
        "coverage": [{"start_deg": 180, "end_deg": 250,
                      "full_circle": False}],
        "defect_locations": [], "iteration": 1, "report_no": None,
    })
    p = SubmissionPayload.model_validate(data)
    r = evaluate(p)
    v = next(w for w in r["welds"] if w["weld_no"] == "W-901")
    codes = {f["code"] for f in v["findings"]}
    assert "RP-ORDER-INVALID" in codes
    nde_ids = {f.get("nde_id") for f in v["findings"]
               if f["code"] == "RP-ORDER-INVALID"}
    assert "N-EARLY" in nde_ids and "N-LATE" not in nde_ids
    # 链节与结论一致
    assert v["repairs"][0]["closed"] is False
    assert v["decision"] == "hold"
    assert r["decision"] == "hold"

    # 序列化（模拟 JSON 审查包往返）后闭合状态仍为 false、条款仍在
    import json
    ser = json.loads(json.dumps({
        "decision": v["decision"],
        "clauses": sorted(codes),
        "repairs": [{"iteration": l["iteration"], "closed": l["closed"]}
                    for l in v["repairs"]],
    }, default=str))
    assert ser["decision"] == "hold"
    assert "RP-ORDER-INVALID" in ser["clauses"]
    assert all(not l["closed"] for l in ser["repairs"])
