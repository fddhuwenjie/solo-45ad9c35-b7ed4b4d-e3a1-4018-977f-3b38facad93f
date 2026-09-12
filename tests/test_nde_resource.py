"""无损检测资源核验测试（NR 系列）。

覆盖：
- 夜班时段跨过人员证书/设备校准到期点 -> 报告作废；
- 级别（实施 I、复核 II）、方法、产品、技术范围；
- 实施/复核同一证书的角色冲突；
- 设备量程/能量不匹配、关键附件（探头、胶片）缺失/不匹配；
- 引用缺失；
- 失效报告不得计入抽检、扩检与返修复检；
- 复核签字冻结资源摘要；续证不改旧版，纠错从旧版派生并呈现版本差异。
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.engine import evaluate
from app.schemas import (
    NdeEquipmentUse,
    NdeEquipmentVersion,
    NdePersonnelCert,
    SubmissionPayload,
)

from examples.sample_data import (
    direct_release_payload,
    double_repair_payload,
    extension_payload,
)

UTC = timezone.utc


def _weld_codes(p: SubmissionPayload, weld_no: str) -> set[str]:
    r = evaluate(p)
    v = next(w for w in r["welds"] if w["weld_no"] == weld_no)
    return {f["code"] for f in v["findings"]}


def _verdict(p: SubmissionPayload, weld_no: str) -> dict:
    r = evaluate(p)
    return next(w for w in r["welds"] if w["weld_no"] == weld_no)


# ---------------------------------------------------------------- 正向基线


def test_default_payload_resources_valid_and_frozen_summaries():
    r = evaluate(direct_release_payload())
    assert r["decision"] == "release"
    assert r["stats"]["nde_reports_excluded"] == 0
    # 资源登记目录
    certs = {c["cert_no"] for c in r["nde_resources"]["personnel"]}
    assert {"NRT-II-ZHANG", "NRT-II-LI"} <= certs
    eqs = {(e["equipment_id"], e["version"]) for e in r["nde_resources"]["equipment"]}
    assert ("XR-250", "2026A") in eqs and ("FILM-T2", "2026A") in eqs
    # 检测单内冻结所用资源摘要
    w001 = _verdict(direct_release_payload(), "W-001")
    nde = w001["nde"][0]
    assert nde["resource_valid"] is True
    assert nde["resource"]["examiner"]["cert_no"] == "NRT-II-ZHANG"
    assert nde["resource"]["examiner"]["cert"]["level"] == "II"
    assert nde["resource"]["reviewer"]["cert"]["method"] == "RT"
    assert {e["equipment_id"] for e in nde["resource"]["equipment"]} == {
        "XR-250", "FILM-T2"
    }
    # 实施时段落库
    assert nde["started_at"] < nde["finished_at"]


# ---------------------------------------------------------------- 证书到期


def test_night_shift_crossing_cert_expiry_excludes_report():
    """20:00~次日02:00 的夜班，证书 3/2 00:00 到期 => 跨失效点，报告作废。"""
    p = direct_release_payload().model_copy(deep=True)
    cert = p.nde_personnel[0]  # 实施人 NRT-II-ZHANG
    cert.valid_to = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
    rec = p.nde[0]  # W-001, examined 3/2 14:00? 默认 day=2
    # 构造跨到期点的夜班：3/1 20:00 ~ 3/2 02:00，报告时刻 3/2 02:00
    rec.examined_at = datetime(2026, 3, 2, 2, 0, tzinfo=UTC)
    rec.started_at = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)
    rec.finished_at = datetime(2026, 3, 2, 2, 0, tzinfo=UTC)
    r = evaluate(p)
    codes = {f["code"] for f in r["findings"]}
    assert "NR-CERT-EXPIRED" in codes
    assert "NR-RECORD-EXCLUDED" in codes
    assert r["stats"]["nde_reports_excluded"] >= 1
    # 失效原因带时段与失效点
    f = next(f for f in r["findings"] if f["code"] == "NR-CERT-EXPIRED")
    assert f["evidence"]["period_end"] == "2026-03-02T02:00:00+00:00"
    assert f["evidence"]["valid_to"].startswith("2026-03-02T00:00:00")
    # 受影响焊口与报告号
    assert f["weld_no"] == "W-001"
    assert f["report_no"] == "RT-2026-N-001"


def test_shift_fully_before_expiry_is_valid():
    """对照：时段完全在到期前（结束时刻恰为到期时刻，含端点）有效。"""
    p = direct_release_payload().model_copy(deep=True)
    # 只保留一张抽检片（20% 批 10 口要求 2 张，故把批改为全检并只评资源条款）
    p.nde = [p.nde[0]]
    p.lot_rules[0].sample_ratio = 1.0
    p.lot_rules[0].on_reject = "full"
    p.nde_personnel[0].valid_to = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    rec = p.nde[0]
    rec.started_at = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
    rec.examined_at = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    rec.finished_at = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    r = evaluate(p)
    assert not {f["code"] for f in r["findings"]} & {
        "NR-CERT-EXPIRED", "NR-RECORD-EXCLUDED"}


def test_cert_not_yet_effective_at_period_start():
    """证书在实施开始后才生效（续证未覆盖时段起点）=> 失效。"""
    p = direct_release_payload().model_copy(deep=True)
    cert = p.nde_personnel[0]
    cert.valid_from = datetime(2026, 3, 2, 13, 0, tzinfo=UTC)
    cert.valid_to = datetime(2027, 3, 2, 0, 0, tzinfo=UTC)
    codes = _weld_codes(p, "W-001")
    assert "NR-CERT-EXPIRED" in codes


# ---------------------------------------------------------------- 方法/级别/产品/技术


def test_cert_method_mismatch():
    p = direct_release_payload().model_copy(deep=True)
    p.nde[0].method = "UT"  # 但仍是 RT 证书与射线机
    r = evaluate(p)
    codes = {f["code"] for f in r["findings"] if f["nde_id"] == "N-001"}
    assert "NR-CERT-METHOD" in codes
    assert "NR-EQUIP-METHOD" in codes


def test_reviewer_level_i_insufficient():
    """复核人仅 I 级 => NR-CERT-LEVEL（I 级不得复核/签发报告）。"""
    p = direct_release_payload().model_copy(deep=True)
    p.nde_personnel[1].level = "I"  # 复核人降为 I 级
    codes = _weld_codes(p, "W-001")
    assert "NR-CERT-LEVEL" in codes


def test_examiner_level_i_is_allowed_under_level_ii_reviewer():
    """实施 I 级、复核 II 级：实施合规（I 级允许操作）。"""
    p = direct_release_payload().model_copy(deep=True)
    p.nde_personnel[0].level = "I"
    r = evaluate(p)
    level_f = [f for f in r["findings"]
               if f["code"] == "NR-CERT-LEVEL"
               and f["evidence"].get("role") == "examiner"]
    assert level_f == []


def test_cert_product_not_covered():
    p = direct_release_payload().model_copy(deep=True)
    p.nde_personnel[0].products = ["pressure_vessel"]
    codes = _weld_codes(p, "W-001")
    assert "NR-CERT-PRODUCT" in codes
    # 证据含实际产品
    f = next(f for f in evaluate(p)["findings"]
             if f["code"] == "NR-CERT-PRODUCT")
    assert f["evidence"]["product"] == "pressure_pipe"


def test_cert_technique_outside_scope():
    p = direct_release_payload().model_copy(deep=True)
    p.nde[0].technique = "digital"
    p.nde_personnel[0].techniques = ["film"]  # 实施人只认可胶片
    codes = _weld_codes(p, "W-001")
    assert "NR-CERT-TECHNIQUE" in codes


# ---------------------------------------------------------------- 角色冲突


def test_examiner_reviewer_same_cert_conflict():
    p = direct_release_payload().model_copy(deep=True)
    p.nde[0].reviewer_cert_no = "NRT-II-ZHANG"  # 与实施人同一张证
    codes = _weld_codes(p, "W-001")
    assert "NR-ROLE-CONFLICT" in codes


# ---------------------------------------------------------------- 设备校准/参数/附件


def test_equipment_calibration_crosses_expiry_night_shift():
    """设备校准在夜班中途到期：NR-EQUIP-CALIBRATION，报告作废。"""
    p = direct_release_payload().model_copy(deep=True)
    xray = next(e for e in p.nde_equipment if e.equipment_id == "XR-250")
    xray.calibrated_to = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
    rec = p.nde[0]
    rec.started_at = datetime(2026, 3, 1, 20, 0, tzinfo=UTC)
    rec.examined_at = datetime(2026, 3, 2, 2, 0, tzinfo=UTC)
    rec.finished_at = datetime(2026, 3, 2, 2, 0, tzinfo=UTC)
    codes = _weld_codes(p, "W-001")
    assert "NR-EQUIP-CALIBRATION" in codes
    assert "NR-RECORD-EXCLUDED" in codes


def test_uncalibrated_probe_excludes_report():
    """换用未校准探头（附件版本校准失效）=> 报告作废。"""
    p = double_repair_payload().model_copy(deep=True)
    # 样例是 RT；把胶片附件校准期改到检测之后
    film = next(e for e in p.nde_equipment if e.equipment_id == "FILM-T2")
    film.calibrated_from = datetime(2026, 4, 1, tzinfo=UTC)
    film.calibrated_to = datetime(2027, 4, 1, tzinfo=UTC)
    r = evaluate(p)
    assert r["decision"] == "hold"
    assert "NR-EQUIP-CALIBRATION" in {f["code"] for f in r["findings"]}


def test_energy_parameter_outside_source_range():
    """实际曝光能量超出射线源能力 => NR-EQUIP-PARAMETER。"""
    p = direct_release_payload().model_copy(deep=True)
    p.nde[0].exposure_energy_kev = 300.0  # XR-250 上限 250
    codes = _weld_codes(p, "W-001")
    assert "NR-EQUIP-PARAMETER" in codes


def test_ut_thickness_outside_probe_range():
    """UT 实际声程超出探伤仪量程 => NR-EQUIP-PARAMETER。"""
    p = extension_payload().model_copy(deep=True)
    # 整体切换为 UT 资源
    utd = NdeEquipmentVersion(
        equipment_id="UTD-100", version="2026A", name="超声探伤仪",
        kind="ut_flaw_detector", method="UT", serial_no="SN-U",
        calibrated_from="2026-01-01T00:00:00Z",
        calibrated_to="2026-12-31T23:59:59Z",
        techniques=["pulse_echo"], range_min_mm=0, range_max_mm=100)
    probe = NdeEquipmentVersion(
        equipment_id="PRB-5P", version="2026A", name="探头", kind="ut_probe",
        method="UT", serial_no="SN-P",
        calibrated_from="2026-01-01T00:00:00Z",
        calibrated_to="2026-12-31T23:59:59Z",
        techniques=["pulse_echo"], range_min_mm=0, range_max_mm=80)
    p.nde_equipment = [utd, probe]
    p.nde_personnel = [
        NdePersonnelCert(cert_no="NU-Z", name="UT-II-张", method="UT",
                         level="II", products=["pressure_pipe"],
                         techniques=["pulse_echo"],
                         valid_from="2026-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z"),
        NdePersonnelCert(cert_no="NU-L", name="UT-II-李", method="UT",
                         level="II", products=["pressure_pipe"],
                         techniques=["pulse_echo"],
                         valid_from="2026-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z"),
    ]
    for rec in p.nde:
        rec.method = "UT"
        rec.technique = "pulse_echo"
        rec.applied_thickness_mm = 120.0  # 超量程
        rec.exposure_energy_kev = None
        rec.examiner_cert_no = "NU-Z"
        rec.reviewer_cert_no = "NU-L"
        rec.equipment_uses = [
            NdeEquipmentUse(equipment_id="UTD-100", version="2026A", role="main"),
            NdeEquipmentUse(equipment_id="PRB-5P", version="2026A", role="probe"),
        ]
    r = evaluate(p)
    assert "NR-EQUIP-PARAMETER" in {f["code"] for f in r["findings"]}


def test_missing_film_accessory():
    """RT 主设备未登记胶片/IP 附件 => NR-EQUIP-ACCESSORY。"""
    p = direct_release_payload().model_copy(deep=True)
    p.nde_equipment = [e for e in p.nde_equipment if e.equipment_id != "FILM-T2"]
    xray = next(e for e in p.nde_equipment if e.equipment_id == "XR-250")
    xray.uses = []
    codes = _weld_codes(p, "W-001")
    assert "NR-EQUIP-ACCESSORY" in codes


def test_missing_ut_probe_accessory():
    """UT 探伤仪无探头附件 => NR-EQUIP-ACCESSORY。"""
    p = direct_release_payload().model_copy(deep=True)
    utd = NdeEquipmentVersion(
        equipment_id="UTD-100", version="2026A", name="超声探伤仪",
        kind="ut_flaw_detector", method="UT", serial_no="SN-U",
        calibrated_from="2026-01-01T00:00:00Z",
        calibrated_to="2026-12-31T23:59:59Z",
        techniques=["pulse_echo"], range_min_mm=0, range_max_mm=500)
    p.nde_equipment = [utd]
    p.nde_personnel = [
        NdePersonnelCert(cert_no="NU-Z", name="UT-II-张", method="UT",
                         level="II", products=["pressure_pipe"],
                         techniques=["pulse_echo"],
                         valid_from="2026-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z"),
        NdePersonnelCert(cert_no="NU-L", name="UT-II-李", method="UT",
                         level="II", products=["pressure_pipe"],
                         techniques=["pulse_echo"],
                         valid_from="2026-01-01T00:00:00Z",
                         valid_to="2026-12-31T23:59:59Z"),
    ]
    rec = p.nde[0]
    rec.method = "UT"
    rec.technique = "pulse_echo"
    rec.applied_thickness_mm = 12.0
    rec.exposure_energy_kev = None
    rec.examiner_cert_no = "NU-Z"
    rec.reviewer_cert_no = "NU-L"
    rec.equipment_uses = [
        NdeEquipmentUse(equipment_id="UTD-100", version="2026A", role="main")]
    codes = _weld_codes(p, "W-001")
    assert "NR-EQUIP-ACCESSORY" in codes


def test_equipment_reference_missing():
    """检测引用了未登记的设备版本 => NR-EQUIP-MISSING。"""
    p = direct_release_payload().model_copy(deep=True)
    p.nde[0].equipment_uses = [
        NdeEquipmentUse(equipment_id="XR-999", version="vX", role="main")]
    codes = _weld_codes(p, "W-001")
    assert "NR-EQUIP-MISSING" in codes


def test_no_equipment_reference_at_all():
    p = direct_release_payload().model_copy(deep=True)
    p.nde[0].equipment_uses = []
    codes = _weld_codes(p, "W-001")
    assert "NR-EQUIP-MISSING" in codes


def test_cert_reference_missing():
    """检测未引用复核证书 => NR-CERT-MISSING。"""
    p = direct_release_payload().model_copy(deep=True)
    p.nde[0].reviewer_cert_no = None
    codes = _weld_codes(p, "W-001")
    assert "NR-CERT-MISSING" in codes


# ---------------------------------------------------------------- 剔除生效：抽检/扩检/返修复检


def test_excluded_report_not_counted_in_sampling():
    """资源失效的合格片不计入抽检 => 抽检数量不足，整批 hold。"""
    p = direct_release_payload().model_copy(deep=True)
    # 两张片的实施人证书都过期
    for c in p.nde_personnel:
        c.valid_to = datetime(2026, 2, 1, tzinfo=UTC)
    r = evaluate(p)
    lot = r["lot_summaries"][0]
    assert lot["examined_final"] == 0
    assert "LT-SAMPLE-INSUFFICIENT" in {f["code"] for f in lot["findings"]}
    # 批次摘要列出被剔除报告
    excluded = {x["nde_id"] for x in lot["excluded_reports"]}
    assert {"N-001", "N-002"} <= excluded
    # 资源目录的 invalid_reports 列出受影响焊口与报告
    invalid = {x["nde_id"]: x for x in r["nde_resources"]["invalid_reports"]}
    assert invalid["N-001"]["weld_no"] == "W-001"
    assert invalid["N-001"]["report_no"] == "RT-2026-N-001"
    assert "NR-CERT-EXPIRED" in invalid["N-001"]["reasons"]


def test_excluded_reject_film_does_not_trigger_extension():
    """资源失效的不合格片不能作为扩检触发依据：不进入 reject 序列。

    无效片被剔除后，批内不存在合格的扩检起点，W-001 的缺陷也无有效检测依据，
    返修链同时断裂，整口/整批保持 hold。
    """
    p = extension_payload().model_copy(deep=True)
    n001 = next(r for r in p.nde if r.nde_id == "N-001")
    n001.reviewer_cert_no = None
    r = evaluate(p)
    lot = r["lot_summaries"][0]
    # 未记到 reject => 不进入扩检分支（extended=False）
    assert lot["extended"] is False
    # 无效不合格片不充当缺陷依据，返修无在先有效不合格显示
    w001 = next(w for w in r["welds"] if w["weld_no"] == "W-001")
    codes = {f["code"] for f in w001["findings"]}
    assert "NR-RECORD-EXCLUDED" in codes
    assert r["decision"] == "hold"


def test_excluded_reinspection_cannot_close_repair():
    """资源失效的复检片不能闭合返修：返修保持开口。"""
    p = extension_payload().model_copy(deep=True)
    # 唯一复检片 N-051 的设备换成未登记版本
    n051 = next(r for r in p.nde if r.nde_id == "N-051")
    n051.equipment_uses = [
        NdeEquipmentUse(equipment_id="XR-GONE", version="v1", role="main")]
    v = _verdict(p, "W-001")
    codes = {f["code"] for f in v["findings"]}
    assert "NR-EQUIP-MISSING" in codes
    assert "RP-NO-REINSPECTION" in codes  # 无效复检片不充当复检依据
    assert v["repairs"][0]["closed"] is False


def test_excluded_report_still_listed_but_flagged_in_weld_nde():
    p = direct_release_payload().model_copy(deep=True)
    p.nde_personnel[0].valid_to = datetime(2026, 2, 1, tzinfo=UTC)
    v = _verdict(p, "W-001")
    assert v["nde_excluded_count"] == 1
    nde = v["nde"][0]
    assert nde["resource_valid"] is False
    assert nde["resource"]["failures"]
    assert any(x["code"] == "NR-CERT-EXPIRED"
               for x in nde["resource"]["failures"])


# ---------------------------------------------------------------- 冻结/续证/差异


def test_renewal_does_not_rewrite_frozen_version(tmp_path, monkeypatch):
    """v1 冻结时证书 3 月底到期；v2 续证到年底。旧版仍记录到期旧摘要。"""
    import importlib
    from fastapi.testclient import TestClient

    db = tmp_path / "freeze.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))

    # v1：证书 3/31 到期，检测 3/2 在窗内 -> 合规放行
    payload = direct_release_payload().model_dump(mode="json")
    for c in payload["nde_personnel"]:
        c["valid_to"] = "2026-03-31T23:59:59Z"
    with TestClient(main.app) as client:
        r = client.post("/packages", json=payload)
        assert r.status_code == 200, r.text
        pid = r.json()["package_id"]
        assert client.post(f"/packages/{pid}/review",
                           json={"reviewer": "李"}).status_code == 200

        v1 = client.get(f"/packages/{pid}?version=1").json()
        cert_v1 = next(c for c in v1["nde_resources"]["personnel"]
                       if c["cert_no"] == "NRT-II-ZHANG")
        assert cert_v1["valid_to"].startswith("2026-03-31")

        # v2 纠错：续证（同证号新窗口），从冻结旧版派生
        payload_v2 = payload
        for c in payload_v2["nde_personnel"]:
            c["valid_to"] = "2026-12-31T23:59:59Z"
        r2 = client.post(f"/packages/{pid}/revisions", json=payload_v2)
        assert r2.status_code == 200, r2.text
        assert r2.json()["parent_version"] == 1

        # 父版保持冻结且旧证书摘要不变
        v1_again = client.get(f"/packages/{pid}?version=1").json()
        cert_old = next(c for c in v1_again["nde_resources"]["personnel"]
                        if c["cert_no"] == "NRT-II-ZHANG")
        assert cert_old["valid_to"].startswith("2026-03-31")
        assert v1_again["status"] == "frozen"

        v2 = client.get(f"/packages/{pid}?version=2").json()
        cert_new = next(c for c in v2["nde_resources"]["personnel"]
                        if c["cert_no"] == "NRT-II-ZHANG")
        assert cert_new["valid_to"].startswith("2026-12-31")

        diff = client.get(
            f"/packages/{pid}/diff?from_version=1&to_version=2").json()
        cert_changes = [d for d in diff["changes"]
                        if d["section"] == "nde_personnel"
                        and d["key"] == "NRT-II-ZHANG"
                        and d["field"] == "valid_to"]
        assert cert_changes and cert_changes[0]["old"].startswith("2026-03-31")


def test_equipment_version_diff_uses_composite_key(tmp_path, monkeypatch):
    """同机换校准版本：diff 以 equipment_id/version 复合键呈现。"""
    import importlib
    from fastapi.testclient import TestClient

    db = tmp_path / "eq.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))

    v1 = direct_release_payload().model_dump(mode="json")
    with TestClient(main.app) as client:
        pid = client.post("/packages", json=v1).json()["package_id"]
        client.post(f"/packages/{pid}/review", json={"reviewer": "李"})

        v2 = direct_release_payload().model_dump(mode="json")
        # XR-250 重新校准派生 2026B 版本（序列号不变）
        for e in v2["nde_equipment"]:
            if e["equipment_id"] == "XR-250":
                e["version"] = "2026B"
                e["calibrated_from"] = "2026-04-01T00:00:00Z"
                e["calibrated_to"] = "2027-03-31T23:59:59Z"
        for u in v2["nde"]:
            for use in u["equipment_uses"]:
                if use["equipment_id"] == "XR-250":
                    use["version"] = "2026B"
        r = client.post(f"/packages/{pid}/revisions", json=v2)
        assert r.status_code == 200, r.text
        diff = client.get(
            f"/packages/{pid}/diff?from_version=1&to_version=2").json()
        keys = {(d["section"], d["kind"], d["key"]) for d in diff["changes"]}
        assert ("nde_equipment", "removed", "XR-250/2026A") in keys
        assert ("nde_equipment", "added", "XR-250/2026B") in keys
