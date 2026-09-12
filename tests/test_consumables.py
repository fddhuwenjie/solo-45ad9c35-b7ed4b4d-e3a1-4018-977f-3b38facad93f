"""焊材批次与烘干/保温/领用链回归测试（WM 系列）。

覆盖：
- 正向：合规链不产生 WM-QTY-OPEN/RP-WM-INVALID/LT-OPEN-REJECT；
- 空项：批次/制度/设备/事件/逐口消耗为空或引用缺失 -> 相关焊口 hold，
  审查包不得冻结（review 409）或签发；
- 数量守恒按实际 segment_id 归属：SG-01 超配不得错指 SG-03 或连带无关消耗；
- 最大暴露时长、保温筒校准到期、重复烘干、温度越限等规则；
- 失效焊材不得用于返修闭合（RP-WM-INVALID）；
- 版本差异：已签发 v1 派生 v2 返回 consumable_evaluation，
  生产消耗与返修消耗共用同一扁平结构，不抛 KeyError: 'chain'。
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.engine import evaluate
from app.schemas import SubmissionPayload
from examples.sample_data import (
    consumable_failure_payload,
    direct_release_payload,
    double_repair_payload,
    extension_payload,
    valid_consumable_chain,
)

UTC = timezone.utc


def _weld(r, no):
    return next(w for w in r["welds"] if w["weld_no"] == no)


def _codes(r, no):
    return {f["code"] for f in _weld(r, no)["findings"]}


# ---------------------------------------------------------------- 正向基线


def test_valid_chain_releases_without_wm_findings():
    r = evaluate(direct_release_payload())
    assert r["decision"] == "release"
    assert r["stats"]["consumable_chain_enabled"] is True
    assert r["stats"]["consumable_uses_invalid"] == 0
    wm = {c for c in r["clauses_triggered"] if c.startswith("WM-")}
    assert wm == set()
    w = _weld(r, "W-001")
    assert w["consumable_uses"] and w["consumable_uses"][0]["consumable_valid"]
    # 冻结用扁平结构：顶层字段 + 内嵌 chain
    view = w["consumable_uses"][0]
    assert view["scope"] == "production"
    assert view["chain"]["batch"]["classification"] == "E5015"
    assert view["chain"]["segment"]["segment_id"] == "SG-01"


def test_positive_repair_not_blocked_by_wm_quantity_or_lt_open():
    """返修正向用例不得被 WM-QTY-OPEN / RP-WM-INVALID / LT-OPEN-REJECT 误拦截。"""
    r = evaluate(extension_payload())
    assert r["decision"] == "release"
    w = _weld(r, "W-001")
    codes = {f["code"] for f in w["findings"]}
    assert not (codes & {
        "WM-QTY-OPEN", "WM-QTY-CONSERVATION", "RP-WM-INVALID",
        "LT-OPEN-REJECT",
    })
    link = w["repairs"][0]
    assert link["closed"] is True
    # 返修消耗同样是扁平结构
    cu = link["consumable_uses"][0]
    assert cu["scope"] == "repair"
    assert cu["consumable_valid"] is True
    assert cu["chain"]["segment"]["segment_id"] == "SG-05"


def test_double_repair_positive_chain_clean():
    r = evaluate(double_repair_payload())
    assert r["decision"] == "release"
    links = _weld(r, "W-901")["repairs"]
    # 一次复检仍不合格（需二次返修），故 iteration1 链节开口、iteration2 闭合，
    # 整条返修链最终闭合放行——焊材在两次补焊均合规。
    assert links[0]["closed"] is False
    assert links[1]["closed"] is True
    for link in links:
        assert all(u["consumable_valid"] for u in link["consumable_uses"])


# ---------------------------------------------------------------- 空项 -> hold


def test_no_consumable_use_on_weld_keeps_hold_and_blocks_freeze(
        tmp_path, monkeypatch):
    """链启用但某焊口无逐口消耗：该口 hold，且审查包不得冻结。"""
    payload = direct_release_payload().model_copy(deep=True)
    payload.welds[0].consumables = []  # W-001 无消耗
    r = evaluate(payload)
    assert "WM-CONSUMABLE-UNTRACED" in _codes(r, "W-001")
    assert _weld(r, "W-001")["decision"] == "hold"
    # 完整性缺口
    assert r["consumables"]["freeze_blocked"] is True
    kinds = {g["kind"] for g in r["consumables"]["completeness"]["gaps"]}
    assert "use_missing" in kinds

    db = tmp_path / "freeze.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))
    with TestClient(main.app) as client:
        pid = client.post("/packages",
                          json=payload.model_dump(mode="json")).json()["package_id"]
        resp = client.post(f"/packages/{pid}/review",
                           json={"reviewer": "李"})
        assert resp.status_code == 409
        assert "焊材" in resp.json()["error"]
        # 未冻结即不可签发
        assert client.post(f"/packages/{pid}/issue",
                           json={"issuer": "赵"}).status_code == 409


def test_empty_rules_containers_bakes_segments_keep_hold_and_block_freeze():
    payload = direct_release_payload().model_copy(deep=True)
    # 保留批次但把制度/设备/烘干/保温/领用全部清空
    payload.consumable_rules = []
    payload.consumable_containers = []
    payload.bake_cycles = []
    payload.quiver_stays = []
    payload.consumable_segments = []
    payload.consumable_events = []
    r = evaluate(payload)
    assert r["consumables"]["freeze_blocked"] is True
    kinds = {g["kind"] for g in r["consumables"]["completeness"]["gaps"]}
    assert {"rules_empty", "containers_empty", "bakes_empty",
            "stays_empty", "segments_empty"} <= kinds
    # 每道焊口都因逐耗引用无法解析而 hold
    assert all(w["decision"] == "hold" for w in r["welds"])


def test_batches_empty_is_completeness_failure_not_chain_disabled(tmp_path,
                                                                  monkeypatch):
    """仅批次为空：不得被当作"链未启用"放行。

    链仍启用、判为结构完整性失败，十道焊口全部 hold，审查包不得冻结/签发。
    """
    payload = direct_release_payload().model_copy(deep=True)
    payload.consumable_batches = []  # 制度/设备/事件/逐耗仍在
    r = evaluate(payload)
    cons = r["consumables"]
    assert cons["enabled"] is True
    assert cons["freeze_blocked"] is True
    kinds = {g["kind"] for g in cons["completeness"]["gaps"]}
    assert "batches_empty" in kinds
    # 每道焊口都挂账但批次无法解析 -> 全部 hold
    assert [w["weld_no"] for w in r["welds"]
            if w["decision"] == "release"] == []
    assert all(w["decision"] == "hold" for w in r["welds"])
    assert r["decision"] == "hold"

    db = tmp_path / "nobatch.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))
    with TestClient(main.app) as client:
        pid = client.post(
            "/packages", json=payload.model_dump(mode="json")
        ).json()["package_id"]
        rv = client.post(f"/packages/{pid}/review",
                         json={"reviewer": "李"})
        assert rv.status_code == 409
        assert client.post(f"/packages/{pid}/issue",
                           json={"issuer": "赵"}).status_code == 409


def test_events_empty_blocks_freeze_despite_wm_qty_open(tmp_path, monkeypatch):
    """仅退回/报废事件为空：焊口已因 WM-QTY-OPEN hold，且必须同步阻止冻结。"""
    payload = direct_release_payload().model_copy(deep=True)
    payload.consumable_events = []  # 批次/制度/设备/段/逐耗齐全
    r = evaluate(payload)
    cons = r["consumables"]
    assert cons["enabled"] is True
    assert "WM-QTY-OPEN" in r["clauses_triggered"]
    assert cons["freeze_blocked"] is True
    kinds = {g["kind"] for g in cons["completeness"]["gaps"]}
    assert "events_empty" in kinds
    assert "segment_events_missing" in kinds
    # 每个领用段都逐段标记缺事件
    seg_gaps = {g.get("segment_id")
                for g in cons["completeness"]["gaps"]
                if g["kind"] == "segment_events_missing"}
    assert seg_gaps == {s.segment_id for s in payload.consumable_segments}

    db = tmp_path / "noevents.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))
    with TestClient(main.app) as client:
        pid = client.post(
            "/packages", json=payload.model_dump(mode="json")
        ).json()["package_id"]
        rv = client.post(f"/packages/{pid}/review",
                         json={"reviewer": "李"})
        assert rv.status_code == 409
        assert client.post(f"/packages/{pid}/issue",
                           json={"issuer": "赵"}).status_code == 409


def test_chain_fully_absent_stays_disabled_and_releases():
    """旧载荷完全无焊材链（批次与逐耗皆空）：链不启用，按原逻辑放行。"""
    payload = direct_release_payload().model_copy(deep=True)
    payload.consumable_batches = []
    payload.consumable_rules = []
    payload.consumable_containers = []
    payload.bake_cycles = []
    payload.quiver_stays = []
    payload.consumable_segments = []
    payload.consumable_events = []
    for w in payload.welds:
        w.consumables = []
    r = evaluate(payload)
    assert r["consumables"]["enabled"] is False
    assert r["decision"] == "release"


# ---------------------------------------------------------------- 数量守恒归属


def test_quantity_conservation_attributed_only_to_offending_segment():
    """SG-05 超配只能影响 SG-05 下的消耗，不得错指 SG-01..SG-03 或连带无关焊口。"""
    p = consumable_failure_payload()
    r = evaluate(p)
    invalid = {s["use_id"]: s for s in r["consumables"]["invalid_uses"]}
    # SG-03 的暴露超时是独立失效；SG-01/SG-02 完全合规不得出现
    offending_qty = {u for u, s in invalid.items()
                     if "WM-QTY-CONSERVATION" in s["reasons"]}
    assert offending_qty == {"CU-011"}  # 仅 3/5 返修消耗
    seg_of = {s["segment_id"] for s in invalid.values()
              if "WM-QTY-CONSERVATION" in s["reasons"]}
    assert seg_of == {"SG-05"}
    # 无关焊口 W-002（SG-01）不被数量条款牵连
    # （注：W-001 返修未闭合会经 LT-OPEN-REJECT 连带整批，那是检验批规则，
    # 不是焊材数量错指；这里只断言 WM 数量条款不串段）
    assert "WM-QTY-CONSERVATION" not in _codes(r, "W-002")
    assert "WM-QTY-CONSERVATION" not in _codes(r, "W-008")


def test_same_quantity_double_allocated_segment_holds_its_welds():
    """同一领用段数量超配：只影响该段（SG-05）内的消耗，不连带其它段焊口。"""
    payload = extension_payload().model_copy(deep=True)
    # SG-05 段只有返修补焊消耗 0.1、领出 0.6、退回 0.5（恰闭合）。
    # 把返修消耗改成 5.0 => 消耗 5.0 + 退回 0.5 > 领出 0.6（同一数量重复分配）。
    for rep in payload.repairs:
        for u in rep.consumables:
            if u.segment_id == "SG-05":
                u.qty_kg = 5.0
    r = evaluate(payload)
    # 仅 SG-05 上的返修消耗失效
    bad_segments = {s["segment_id"] for s in r["consumables"]["invalid_uses"]}
    assert bad_segments == {"SG-05"}
    assert "WM-QTY-CONSERVATION" in _codes(r, "W-001")
    # 其它段焊口不被 SG-05 数量超配牵连
    assert "WM-QTY-CONSERVATION" not in _codes(r, "W-002")
    assert "WM-QTY-CONSERVATION" not in _codes(r, "W-008")


# ---------------------------------------------------------------- 规则类失效


def test_exposure_exceeded_only_affects_overtime_uses():
    r = evaluate(consumable_failure_payload())
    invalid = r["consumables"]["invalid_uses"]
    exp = {s["use_id"] for s in invalid
           if "WM-EXPOSURE-EXCEEDED" in s["reasons"]}
    # SG-03 当日 03:00 领出、09:00 施焊 = 360 分钟 > 240
    assert exp  # 至少一道超时焊口
    for s in invalid:
        if "WM-EXPOSURE-EXCEEDED" in s["reasons"]:
            assert s["segment_id"] == "SG-03"
            assert s["exposure_minutes"] == 360.0


def test_quiver_calibration_expired_blocks_repair_close():
    """保温筒校准 3/4 到期：3/5 补焊失效，返修不得闭合（RP-WM-INVALID）。"""
    r = evaluate(consumable_failure_payload())
    w = _weld(r, "W-001")
    codes = {f["code"] for f in w["findings"]}
    assert "RP-WM-INVALID" in codes
    rp = next(f for f in w["findings"] if f["code"] == "RP-WM-INVALID")
    assert "WM-CONTAINER-CALIBRATION" in rp["evidence"]["wm_codes"]
    assert w["repairs"][0]["closed"] is False


def test_invalid_material_cannot_close_repair_in_double_repair():
    """二次返修若使用失效焊材，链节不得闭合。"""
    p = double_repair_payload().model_copy(deep=True)
    # 第二次补焊消耗指向不存在的领用段
    for rep in p.repairs:
        if rep.iteration == 2:
            for u in rep.consumables:
                u.segment_id = "SG-GONE"
    r = evaluate(p)
    w = _weld(r, "W-901")
    link2 = next(l for l in w["repairs"] if l["iteration"] == 2)
    assert link2["closed"] is False
    codes = {f["code"] for f in w["findings"]}
    assert "RP-WM-INVALID" in codes
    assert "WM-SEGMENT-MISSING" in codes


def test_bake_temperature_out_of_range_invalidates_chain():
    p = direct_release_payload().model_copy(deep=True)
    bk = p.bake_cycles[0]
    # 整段恒温只到 300℃，低于烘干下限 350℃
    for rd in bk.readings:
        rd.temp_c = 300.0
    r = evaluate(p)
    assert r["decision"] == "hold"
    assert "WM-BAKE-TEMP" in r["clauses_triggered"]
    # 烘干周期级失效传播到该烘产出的全部消耗
    assert r["stats"]["consumable_uses_invalid"] == r["stats"]["consumable_uses_total"]


def test_rebake_cycle_count_exceeded():
    p = direct_release_payload().model_copy(deep=True)
    p.bake_cycles[0].cycle_no = 3  # 制度上限 2
    r = evaluate(p)
    assert "WM-REBAKE-EXCEEDED" in r["clauses_triggered"]


def test_wrong_classification_wps_mismatch():
    """牌号拿错：批次分类号不在 WPS 清单内 -> WM-WPS-CLASS-MISMATCH。"""
    p = direct_release_payload().model_copy(deep=True)
    p.consumable_batches[0].classification = "E4303"  # 钛钙型，非 WPS 的 E5015
    r = evaluate(p)
    assert "WM-WPS-CLASS-MISMATCH" in _codes(r, "W-001")


def test_cert_lot_mismatch_holds():
    p = direct_release_payload().model_copy(deep=True)
    p.consumable_batches[0].cert_lot_no = "OTHER-LOT"
    r = evaluate(p)
    assert "WM-CERT-MISSING" in r["clauses_triggered"]


def test_batch_not_accepted_holds():
    p = direct_release_payload().model_copy(deep=True)
    p.consumable_batches[0].received_status = "quarantine"
    r = evaluate(p)
    assert "WM-BATCH-NOT-RECEIVED" in r["clauses_triggered"]


# ---------------------------------------------------------------- 冻结/签发与版本差异


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db = tmp_path / "wm.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))
    with TestClient(main.app) as c:
        yield main, c


def test_hold_due_to_rule_violation_can_still_freeze_but_not_issue(client):
    """规则性 hold（超时，记录齐全）允许冻结以便从旧版开修订分支；不得签发。"""
    main, c = client
    body = consumable_failure_payload().model_dump(mode="json")
    pid = c.post("/packages", json=body).json()["package_id"]
    assert c.post(f"/packages/{pid}/review",
                  json={"reviewer": "李"}).status_code == 200
    iss = c.post(f"/packages/{pid}/issue", json={"issuer": "赵"})
    assert iss.status_code == 409


def test_diff_consumable_evaluation_after_issued_v1_branches_v2(client):
    """已签发 v1（合规）派生 v2（引入焊材失效）：consumable_evaluation 正常返回，
    生产/返修消耗共用扁平结构，不抛 KeyError: 'chain'。"""
    main, c = client

    v1 = direct_release_payload().model_dump(mode="json")
    pid = c.post("/packages", json=v1).json()["package_id"]
    c.post(f"/packages/{pid}/review", json={"reviewer": "李"})
    c.post(f"/packages/{pid}/issue", json={"issuer": "赵"})

    # v2：把 SG-03 领用时点提前造成超时（3/3 焊口 W-008/009/010）
    v2 = direct_release_payload().model_dump(mode="json")
    for s in v2["consumable_segments"]:
        if s["segment_id"] == "SG-03":
            s["issued_at"] = "2026-03-03T03:00:00Z"
    r2 = c.post(f"/packages/{pid}/revisions", json=v2)
    assert r2.status_code == 200, r2.text

    diff = c.get(f"/packages/{pid}/diff?from_version=1&to_version=2").json()
    ev = diff["consumable_evaluation"]
    assert ev is not None
    assert ev["old_decision"] == "release"
    assert ev["new_decision"] == "hold"
    assert "WM-EXPOSURE-EXCEEDED" in ev["introduced"]
    # v1 无失效消耗，v2 出现 invalid_to_valid 反向记录
    assert ev["old_invalid_uses"] == []
    new_invalid = {u["use_id"]: u for u in ev["new_invalid_uses"]}
    assert new_invalid  # 3/3 焊口失效
    for u in new_invalid.values():
        assert u["segment_id"] == "SG-03"
        assert u["chain"]["segment"]["segment_id"] == "SG-03"
    changed = {ch["use_id"]: ch["change"] for ch in ev["changed_uses"]}
    assert any(v == "valid_to_invalid" for v in changed.values())


def test_diff_consumable_none_when_chain_disabled_on_both(tmp_path, monkeypatch):
    db = tmp_path / "noc.db"
    monkeypatch.setenv("WELD_DB_PATH", str(db))
    import app.main as main
    importlib.reload(main)
    main.store = main.Store(str(db))
    # 构造无焊材链载荷：直接给引擎层包（样例均启用链，故手改）
    payload = direct_release_payload().model_dump(mode="json")
    for key in ("consumable_batches", "consumable_rules",
                "consumable_containers", "bake_cycles", "quiver_stays",
                "consumable_segments", "consumable_events"):
        payload[key] = []
    for w in payload["welds"]:
        w["consumables"] = []
    with TestClient(main.app) as c:
        pid = c.post("/packages", json=payload).json()["package_id"]
        c.post(f"/packages/{pid}/review", json={"reviewer": "李"})
        v2 = dict(payload)
        v2["submitted_by"] = "质检员-王2"
        c.post(f"/packages/{pid}/revisions", json=v2)
        diff = c.get(
            f"/packages/{pid}/diff?from_version=1&to_version=2").json()
    assert diff["consumable_evaluation"] is None


def test_diff_repair_consumable_invalid_to_valid(client):
    """返修焊材规则性失效 v1（保温筒校准到期，记录完整可冻结）-> v2 补录
    重新校准版本：changed_uses 标 invalid_to_valid，可从签发 v1 正常派生。"""
    from datetime import datetime as _dt
    main, c = client

    v1 = extension_payload().model_copy(deep=True)
    # 保温筒校准 3/4 到期 -> 3/5 补焊失效（记录齐全，属规则性 hold，可冻结）
    for qv in v1.consumable_containers:
        if qv.container_id == "QV-1":
            qv.calibrated_to = _dt(2026, 3, 4, tzinfo=UTC)
    pid = c.post("/packages", json=v1.model_dump(mode="json")).json()["package_id"]
    assert c.post(f"/packages/{pid}/review",
                  json={"reviewer": "李"}).status_code == 200

    # v2：保温筒重新校准（同机派生 2026B，覆盖全年），暂存与引用切到新版本
    v2 = extension_payload().model_copy(deep=True)
    for qv in v2.consumable_containers:
        if qv.container_id == "QV-1":
            qv.version = "2026B"
            qv.calibrated_from = _dt(2026, 1, 1, tzinfo=UTC)
            qv.calibrated_to = _dt(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
    for st in v2.quiver_stays:
        st.quiver_version = "2026B"
    rv = c.post(f"/packages/{pid}/revisions", json=v2.model_dump(mode="json"))
    assert rv.status_code == 200, rv.text
    assert rv.json()["decision"] == "release"

    diff = c.get(f"/packages/{pid}/diff?from_version=1&to_version=2").json()
    ev = diff["consumable_evaluation"]
    assert ev is not None
    repair_uses = {u["use_id"]: u for u in ev["old_invalid_uses"]
                   if u["scope"] == "repair"}
    assert repair_uses, "返修失效消耗应出现在 old_invalid_uses"
    changed = {ch["use_id"]: ch["change"] for ch in ev["changed_uses"]}
    assert any(v == "invalid_to_valid" for v in changed.values())
    assert "WM-CONTAINER-CALIBRATION" in ev["resolved"]
    # 快照差异本身也记录了保温筒版本的复合键增删
    keys = {(d["section"], d["kind"], d["key"]) for d in diff["changes"]}
    assert ("consumable_containers", "removed", "QV-1/2026A") in keys
    assert ("consumable_containers", "added", "QV-1/2026B") in keys
