"""FastAPI 端到端测试：预演、复核冻结、签发、修订分支、差异、审查包、SVG。"""
from __future__ import annotations

import importlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "weld_test.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)  # 让模块级 DB_PATH 与 store 指向临时库
    main.store = main.Store(str(db))
    with TestClient(main.app) as c:
        yield c


def _payload(factory_name: str = "direct_release_payload", **kw):
    from examples import sample_data
    factory = getattr(sample_data, factory_name)
    return factory(**kw).model_dump(mode="json")


# ---------------------------------------------------------------- 健康与条款


def test_health_and_clauses(client):
    assert client.get("/health").json()["status"] == "ok"
    codes = {c["code"] for c in client.get("/clauses").json()["clauses"]}
    assert {"WQ-WELDER-EXPIRED", "LT-EXTENSION-INSUFFICIENT",
            "RP-EXCAVATION-UNCOVERED", "RP-DEFECT-NOT-EXCAVATED",
            "RP-ORDER-INVALID"} <= codes


# ---------------------------------------------------------------- 预演


def test_preview_does_not_persist(client):
    r = client.post("/preview", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "release"
    assert body["version"] == 0
    assert client.get("/packages").json() == []


def test_preview_hold_lists_clauses_and_weld_numbers(client):
    r = client.post("/preview",
                    json=_payload("extension_payload", enough_extension=False))
    body = r.json()
    assert body["decision"] == "hold"
    triggered = set(body["clauses_triggered"])
    assert "LT-EXTENSION-INSUFFICIENT" in triggered
    held = {w["weld_no"] for w in body["welds"] if w["decision"] == "hold"}
    assert held == {f"W-{i:03d}" for i in range(1, 11)}


def test_preview_three_defect_scenarios(client):
    """三个已修复缺陷在 API 层同样必须 hold。"""
    from app.schemas import SubmissionPayload
    from examples.sample_data import double_repair_payload

    data = double_repair_payload().model_dump(mode="json")
    data["repairs"] = [r for r in data["repairs"] if r["iteration"] == 1]
    data["nde"] = [r for r in data["nde"] if r["iteration"] in (0, 1)]
    n0, n1 = data["nde"]
    rep = data["repairs"][0]

    # A：挖补仅与缺陷相交
    a = _deep(data)
    a["nde"][0]["defect_locations"] = [
        {"start_deg": 100, "end_deg": 140, "full_circle": False}]
    a["repairs"][0]["excavated_band"] = {
        "start_deg": 100, "end_deg": 101, "full_circle": False}
    a["nde"][1].update({
        "result": "accept", "defect_locations": [],
        "coverage": [{"start_deg": 100, "end_deg": 101,
                      "full_circle": False}]})
    _with_nde_resources(a["nde"][0])
    _with_nde_resources(a["nde"][1])
    body = client.post("/preview", json=a).json()
    assert body["decision"] == "hold"
    assert "RP-DEFECT-NOT-EXCAVATED" in body["clauses_triggered"]

    # B：补焊早于不合格底片
    b = _deep(data)
    b["nde"][0]["examined_at"] = "2026-03-06T14:00:00Z"
    b["repairs"][0]["repaired_at"] = "2026-03-04T10:00:00Z"
    b["nde"][1].update({
        "examined_at": "2026-03-08T14:00:00Z", "result": "accept",
        "defect_locations": [],
        "coverage": [{"start_deg": 180, "end_deg": 250,
                      "full_circle": False}]})
    _with_nde_resources(b["nde"][0])
    _with_nde_resources(b["nde"][1])
    body = client.post("/preview", json=b).json()
    assert body["decision"] == "hold"
    assert "RP-ORDER-INVALID" in body["clauses_triggered"]

    # C：复检早于补焊
    c = _deep(data)
    c["nde"][0]["examined_at"] = "2026-03-02T14:00:00Z"
    c["repairs"][0]["repaired_at"] = "2026-03-06T10:00:00Z"
    c["nde"][1].update({
        "examined_at": "2026-03-04T14:00:00Z", "result": "accept",
        "defect_locations": [],
        "coverage": [{"start_deg": 180, "end_deg": 250,
                      "full_circle": False}]})
    _with_nde_resources(c["nde"][0])
    _with_nde_resources(c["nde"][1])
    body = client.post("/preview", json=c).json()
    assert body["decision"] == "hold"
    assert "RP-ORDER-INVALID" in body["clauses_triggered"]


def _deep(obj):
    import copy
    return copy.deepcopy(obj)


def _with_nde_resources(rec: dict) -> dict:
    """给手工构造的检测单补齐默认有效的人员证书与设备版本引用。

    examined_at 可能已被改写，时段始终随之重设为 examined 前 2 小时起。
    """
    rec["started_at"] = rec["examined_at"].replace("14:00", "12:00")
    rec["finished_at"] = rec["examined_at"]
    rec.setdefault("examiner_cert_no", "NRT-II-ZHANG")
    rec.setdefault("reviewer_cert_no", "NRT-II-LI")
    rec.setdefault("equipment_uses",
                   [{"equipment_id": "XR-250", "version": "2026A",
                     "role": "main"}])
    rec.setdefault("technique", "film")
    rec.setdefault("applied_thickness_mm", None)
    rec.setdefault("exposure_energy_kev", 160.0)
    return rec


def test_preview_mixed_early_and_late_rt_keeps_link_unclosed(client):
    """/preview：同轮早于补焊 RT + 晚于补焊且覆盖充分 RT 的组合，
    条款、hold 与序列化 closed 必须一致。"""
    from examples.sample_data import double_repair_payload
    data = double_repair_payload().model_dump(mode="json")
    data["repairs"] = [r for r in data["repairs"] if r["iteration"] == 1]
    data["nde"] = [r for r in data["nde"] if r["iteration"] in (0, 1)]
    rep = data["repairs"][0]
    rep["repaired_at"] = "2026-03-04T10:00:00Z"
    rep["excavated_band"] = {"start_deg": 195, "end_deg": 235,
                             "full_circle": False}
    early = data["nde"][1]
    early.update({
        "nde_id": "N-EARLY",
        "examined_at": "2026-03-03T14:00:00Z",  # 早于补焊
        "started_at": "2026-03-03T12:00:00Z",
        "finished_at": "2026-03-03T14:00:00Z",
        "result": "accept", "defect_locations": [],
        "coverage": [{"start_deg": 180, "end_deg": 250,
                      "full_circle": False}],
    })
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

    body = client.post("/preview", json=data).json()
    assert body["decision"] == "hold"
    assert "RP-ORDER-INVALID" in body["clauses_triggered"]

    weld = next(w for w in body["welds"] if w["weld_no"] == "W-901")
    assert weld["decision"] == "hold"
    order_nde = {f.get("nde_id") for f in weld["findings"]
                 if f["code"] == "RP-ORDER-INVALID"}
    assert order_nde == {"N-EARLY"}  # 仅倒置片挂条款，晚片不挂
    # 序列化结果中链节仍为 false，与 hold 一致
    assert all(link["closed"] is False for link in weld["repairs"])
    assert weld["repairs"][0]["closed"] is False


# ---------------------------------------------------------------- JSON 审查包


def test_json_review_package_shape(client):
    r = client.post("/packages", json=_payload())
    assert r.status_code == 200
    pack = r.json()
    assert pack["version"] == 1
    assert pack["status"] == "draft"
    assert len(pack["snapshot_sha256"]) == 64
    assert pack["decision"] == "release"
    assert pack["stats"]["weld_total"] == 10
    weld = next(w for w in pack["welds"] if w["weld_no"] == "W-001")
    assert weld["heat_nos"] == ["H-A101", "H-B201"]
    assert weld["svg_url"].endswith("/svg?version=1")


def test_review_freezes_snapshot_and_issue(client):
    pid = client.post("/packages", json=_payload()).json()["package_id"]

    r = client.post(f"/packages/{pid}/review",
                    json={"reviewer": "责任工程师-李", "comment": "记录齐全"})
    assert r.status_code == 200
    assert r.json()["status"] == "frozen"
    assert r.json()["reviewer"] == "责任工程师-李"

    # 冻结后再开修订才允许；draft 不可直接二次签字
    r2 = client.post(f"/packages/{pid}/review",
                     json={"reviewer": "x"})
    assert r2.status_code == 409

    r3 = client.post(f"/packages/{pid}/issue",
                     json={"issuer": "质保师-赵"})
    assert r3.status_code == 200
    assert r3.json()["status"] == "issued"


def test_issue_blocked_when_hold(client):
    pid = client.post(
        "/packages",
        json=_payload("extension_payload", enough_extension=False)
    ).json()["package_id"]
    client.post(f"/packages/{pid}/review", json={"reviewer": "李"})
    r = client.post(f"/packages/{pid}/issue", json={"issuer": "赵"})
    assert r.status_code == 409
    assert "hold" in r.json()["error"]


# ---------------------------------------------------------------- 修订分支与差异


def test_revision_branches_from_frozen_and_diff(client):
    pid = client.post("/packages", json=_payload()).json()["package_id"]
    client.post(f"/packages/{pid}/review", json={"reviewer": "李"})

    # v2：在冻结旧版基础上纠错——换焊工有效期并新增一口焊口的检测
    v2_payload = _payload()
    # 制造一次差异：把 W-002 纳入抽检
    v2_payload["nde"].append({
        "nde_id": "N-003", "weld_no": "W-002", "method": "RT",
        "result": "accept", "examined_at": "2026-03-09T14:00:00Z",
        "started_at": "2026-03-09T12:00:00Z",
        "finished_at": "2026-03-09T14:00:00Z",
        "examiner": "NDE-II-张", "examiner_cert_no": "NRT-II-ZHANG",
        "reviewer_cert_no": "NRT-II-LI",
        "equipment_uses": [{"equipment_id": "XR-250", "version": "2026A",
                            "role": "main"}],
        "technique": "film", "applied_thickness_mm": None,
        "exposure_energy_kev": 160.0, "lot_id": None,
        "coverage": [{"start_deg": 0, "end_deg": 360,
                      "full_circle": False}],
        "defect_locations": [], "iteration": 0,
        "report_no": "RT-2026-N-003",
    })
    r = client.post(f"/packages/{pid}/revisions", json=v2_payload)
    assert r.status_code == 200
    assert r.json()["version"] == 2
    assert r.json()["parent_version"] == 1
    assert r.json()["status"] == "draft"

    # 父版仍冻结可取
    v1 = client.get(f"/packages/{pid}?version=1").json()
    assert v1["status"] == "frozen"
    versions = client.get(f"/packages/{pid}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["status"] == "frozen"

    diff = client.get(f"/packages/{pid}/diff?from_version=1&to_version=2").json()
    assert diff["decision_changed"] is False
    kinds = {(d["section"], d["kind"], d["key"]) for d in diff["changes"]}
    assert ("nde", "added", "N-003") in kinds


def test_revision_requires_frozen_parent(client):
    pid = client.post("/packages", json=_payload()).json()["package_id"]
    r = client.post(f"/packages/{pid}/revisions", json=_payload())
    assert r.status_code == 409


# ---------------------------------------------------------------- SVG


def test_svg_endpoint_and_content(client):
    pid = client.post(
        "/packages", json=_payload("double_repair_payload")
    ).json()["package_id"]
    r = client.get(f"/packages/{pid}/welds/W-901/svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    svg = r.text
    assert svg.lstrip().startswith("<?xml") or svg.lstrip().startswith("<svg")
    assert "W-901" in svg
    # 二次返修紫色车道与闭合结论
    assert "#6a1b9a" in svg
    assert "放行 RELEASE" in svg


def test_svg_hold_badge_and_clause(client):
    pid = client.post(
        "/packages",
        json=_payload("extension_payload", enough_extension=False)
    ).json()["package_id"]
    r = client.get(f"/packages/{pid}/welds/W-001/svg")
    svg = r.text
    assert "保持 HOLD" in svg
    assert "LT-EXTENSION-INSUFFICIENT" in svg


def test_preview_svg(client):
    r = client.post("/preview/welds/W-001/svg", json=_payload())
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"


# ---------------------------------------------------------------- NDT 资源核验


def test_preview_ndt_resource_failure_lists_weld_report_and_version(client):
    """夜班跨证书到期点 + 未登记设备版本：报告剔除、列焊口/报告/资源版本。"""
    from examples.sample_data import ndt_resource_failure_payload
    body = client.post(
        "/preview", json=ndt_resource_failure_payload().model_dump(mode="json")
    ).json()
    assert body["decision"] == "hold"
    assert {
        "NR-CERT-EXPIRED", "NR-EQUIP-MISSING", "NR-RECORD-EXCLUDED",
    } <= set(body["clauses_triggered"])

    invalid = {x["nde_id"]: x for x in body["nde_resources"]["invalid_reports"]}
    assert set(invalid) == {"N-001", "N-002"}
    assert invalid["N-001"]["weld_no"] == "W-001"
    assert invalid["N-001"]["report_no"] == "RT-2026-N-001"
    assert invalid["N-001"]["period"]["started_at"].startswith("2026-03-05T22:00")
    assert invalid["N-001"]["period"]["finished_at"].startswith("2026-03-06T02:00")
    assert invalid["N-002"]["reasons"] == ["NR-EQUIP-MISSING"]

    w001 = next(w for w in body["welds"] if w["weld_no"] == "W-001")
    nde = w001["nde"][0]
    assert nde["resource_valid"] is False
    # 冻结摘要含证书版本与设备版本
    assert nde["resource"]["examiner"]["cert"]["valid_to"].startswith(
        "2026-03-06T00:00")
    assert nde["resource"]["equipment"]
    # 批次摘要列出被剔除报告
    lot = body["lot_summaries"][0]
    assert {x["nde_id"] for x in lot["excluded_reports"]} == {"N-001", "N-002"}
    assert lot["examined_final"] == 0


def test_clauses_catalog_contains_nr_series(client):
    codes = {c["code"] for c in client.get("/clauses").json()["clauses"]}
    assert {
        "NR-CERT-EXPIRED", "NR-CERT-LEVEL", "NR-ROLE-CONFLICT",
        "NR-EQUIP-CALIBRATION", "NR-EQUIP-PARAMETER", "NR-EQUIP-ACCESSORY",
        "NR-RECORD-EXCLUDED",
    } <= codes


def test_svg_marks_resource_excluded_report(client):
    from examples.sample_data import ndt_resource_failure_payload
    pid = client.post(
        "/packages",
        json=ndt_resource_failure_payload().model_dump(mode="json"),
    ).json()["package_id"]
    svg = client.get(f"/packages/{pid}/welds/W-001/svg").text
    assert "ndexcluded" in svg
    assert "NR-CERT-EXPIRED" in svg
