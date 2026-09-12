"""焊材批次与烘干/保温/领用链核验（WM 系列）。

低氢焊条事件链：
    批次（质保书/入库验收/适用 WPS）
      → 烘干周期（烘箱校准版本 + 温度时序 + 恒温时长 + 重复次数）
        → 保温暂存（保温筒校准版本 + 温度时序连续性）
          → 领用段（领出数量/时刻）
            → 施焊/返修实际消耗（数量）
            → 退回(再烘干)/报废事件（领用数量闭合）

核验口径（任一不满足，相关焊口保持 hold；失效焊材不得用于返修闭合）：
1. 批次：已验收入库、质保书批号与制造批号一致、分类号在 WPS 焊材清单内、
   批次登记适用该 WPS、分类号有烘干制度；
2. 烘箱/保温筒：引用版本存在、用途匹配（oven/quiver）、校准有效期持续覆盖使用时段；
3. 烘干：温度时序连续覆盖烘干时段（读数间隔不超过制度缺口）、温度在烘干窗口内、
   窗口内恒温时长达标、同批次烘干序号连续且不超过允许重复次数；
4. 保温：烘箱取出→保温筒装入间隔不超时限、领用必须落在暂存区间内、
   保温温度时序连续且在保温窗口内；
5. 领用：领出→施焊→退回/报废时序正确、暴露时长不超制度上限；
6. 数量守恒：焊口/返修消耗 + 退回 + 报废 ≤ 领出（同一数量不得重复分配），
   未处置余量必须办理退回/报废；保温装入不超烘干出箱、累计领用不超保温装入、
   累计烘干不超入库数量；报废不超可处置数量。

返回结构化 failures（条款码 + 批次/周期/暂存/领用段/消耗定位 + 区间证据），
由引擎统一发 finding 并挂到相关焊口；同时给出每次消耗的冻结用事件链摘要。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from .clauses import CLAUSES
from .schemas import (
    BakeCycle,
    ConsumableBatch,
    ConsumableContainerVersion,
    ConsumableIssueSegment,
    ConsumableRule,
    ConsumableSegmentEvent,
    ConsumableUse,
    QuiverStay,
    RepairRecord,
    SubmissionPayload,
    WeldRecord,
)


def _f(code: str, *, evidence=None, scope=None, weld_no=None, repair_id=None,
       use_id=None, batch_id=None, segment_id=None, bake_id=None,
       stay_id=None) -> dict:
    meta = CLAUSES[code]
    return {
        "code": code,
        "severity": meta["severity"],
        "category": meta["category"],
        "reference": meta["reference"],
        "message": meta["message"],
        "scope": scope,
        "weld_no": weld_no,
        "repair_id": repair_id,
        "ids": {
            "batch_id": batch_id,
            "segment_id": segment_id,
            "bake_id": bake_id,
            "stay_id": stay_id,
            "use_id": use_id,
        },
        "evidence": evidence or {},
    }


# ============================================================ 温度时序核验


def _reading_coverage(readings, start: datetime, end: datetime,
                      max_gap: timedelta, *, temp_lo: float, temp_hi: float,
                      temp_code: str, context: dict):
    """核验温度时序对 [start,end] 的连续覆盖与越限读数。

    返回 (gap_failures, out_of_range_failures, in_range_minutes_or_None)。
    读数必须首尾覆盖时段；相邻读数间隔超过 max_gap 视为记录断档。
    """
    gap_failures: list[dict] = []
    range_failures: list[dict] = []
    pts = sorted(readings, key=lambda r: r.at)

    if not pts:
        gap_failures.append(_f(
            "WM-HOLDING-GAP" if context["kind"] == "quiver"
            else "WM-CHAIN-GAP",
            evidence={**context, "reason": "无任何温度读数记录，温度时序完全缺失",
                      "window": {"from": start.isoformat(), "to": end.isoformat()}},
            **{k: v for k, v in context.get("ref", {}).items()},
        ))
        return gap_failures, range_failures, None

    # 首尾覆盖
    if pts[0].at > start:
        gap_failures.append(_f(
            "WM-HOLDING-GAP" if context["kind"] == "quiver"
            else "WM-CHAIN-GAP",
            evidence={**context,
                      "reason": "温度时序未覆盖时段起点",
                      "gap": {"from": start.isoformat(),
                              "to": pts[0].at.isoformat(),
                              "gap_minutes": round(
                                  (pts[0].at - start).total_seconds() / 60, 1)}},
            **context.get("ref", {}),
        ))
    if pts[-1].at < end:
        gap_failures.append(_f(
            "WM-HOLDING-GAP" if context["kind"] == "quiver"
            else "WM-CHAIN-GAP",
            evidence={**context,
                      "reason": "温度时序未覆盖时段止点",
                      "gap": {"from": pts[-1].at.isoformat(),
                              "to": end.isoformat(),
                              "gap_minutes": round(
                                  (end - pts[-1].at).total_seconds() / 60, 1)}},
            **context.get("ref", {}),
        ))

    # 相邻读数间隔
    for a, b in zip(pts, pts[1:]):
        delta = b.at - a.at
        if delta > max_gap:
            gap_failures.append(_f(
                "WM-HOLDING-GAP" if context["kind"] == "quiver"
                else "WM-CHAIN-GAP",
                evidence={**context,
                          "reason": "温度读数间隔超过制度允许缺口（记录断档）",
                          "gap": {"from": a.at.isoformat(), "to": b.at.isoformat(),
                                  "gap_minutes": round(delta.total_seconds() / 60, 1),
                                  "max_minutes": max_gap.total_seconds() / 60}},
                **context.get("ref", {}),
            ))

    # 越限读数（落在时段内的读数才纳入）
    for r in pts:
        if start <= r.at <= end and not (temp_lo <= r.temp_c <= temp_hi):
            range_failures.append(_f(
                temp_code,
                evidence={**context,
                          "at": r.at.isoformat(), "temp_c": r.temp_c,
                          "allowed": {"min": temp_lo, "max": temp_hi}},
                **context.get("ref", {}),
            ))
    return gap_failures, range_failures, pts


def _in_range_minutes(pts, start: datetime, end: datetime,
                      lo: float, hi: float) -> float:
    """窗口内温度持续在 [lo,hi] 的累计分钟数（相邻两读数均在窗口内才计入）。"""
    total = 0.0
    win = [r for r in pts if start <= r.at <= end]
    win.sort(key=lambda r: r.at)
    for a, b in zip(win, win[1:]):
        if lo <= a.temp_c <= hi and lo <= b.temp_c <= hi:
            total += (b.at - a.at).total_seconds() / 60.0
    return total


# ============================================================ 核验入口


def verify_consumables(payload: SubmissionPayload) -> dict:
    """核验整包焊材链。未登记任何焊材批次时返回 enabled=False（链不启用）。"""
    batches = {b.batch_id: b for b in payload.consumable_batches}
    if not batches:
        return {"enabled": False, "use_states": {}, "failures": []}

    rules = {r.classification: r for r in payload.consumable_rules}
    containers = {(c.container_id, c.version): c
                  for c in payload.consumable_containers}
    bakes = {b.bake_id: b for b in payload.bake_cycles}
    stays = {s.stay_id: s for s in payload.quiver_stays}
    segments = {s.segment_id: s for s in payload.consumable_segments}
    events_by_segment: dict[str, list[ConsumableSegmentEvent]] = defaultdict(list)
    for ev in payload.consumable_events:
        events_by_segment[ev.segment_id].append(ev)
    for evs in events_by_segment.values():
        evs.sort(key=lambda e: e.at)

    failures: list[dict] = []

    # ---- 全部消耗（施焊 + 返修），统一编号定位 ----
    # use_ref: ConsumableUse + 归属焊口/返修
    use_refs: list[dict] = []
    weld_by_no = {w.weld_no: w for w in payload.welds}
    repair_by_id = {r.repair_id: r for r in payload.repairs}
    for w in payload.welds:
        for u in w.consumables:
            use_refs.append({"use": u, "scope": "production",
                             "weld_no": w.weld_no, "repair_id": None})
    for r in payload.repairs:
        for u in r.consumables:
            use_refs.append({"use": u, "scope": "repair",
                             "weld_no": r.weld_no, "repair_id": r.repair_id})

    def emit(f: dict) -> None:
        failures.append(f)

    # ------------------------------------------------ 1) 批次静态核验
    for b in payload.consumable_batches:
        ref = {"batch_id": b.batch_id}
        if b.received_status != "accepted":
            emit(_f("WM-BATCH-NOT-RECEIVED", batch_id=b.batch_id,
                    evidence={"batch_id": b.batch_id,
                              "received_status": b.received_status}))
        if not b.cert_no:
            emit(_f("WM-CERT-MISSING", batch_id=b.batch_id,
                    evidence={"batch_id": b.batch_id,
                              "reason": "批次未登记质量证明书编号"}))
        elif not b.cert_lot_no or b.cert_lot_no != b.manufacturer_lot_no:
            emit(_f("WM-CERT-MISSING", batch_id=b.batch_id,
                    evidence={"batch_id": b.batch_id, "cert_no": b.cert_no,
                              "manufacturer_lot_no": b.manufacturer_lot_no,
                              "cert_lot_no": b.cert_lot_no,
                              "reason": "质保书记载批号与制造批号不一致或缺失"}))
        if b.classification not in rules:
            emit(_f("WM-RULE-MISSING", batch_id=b.batch_id,
                    evidence={"batch_id": b.batch_id,
                              "classification": b.classification}))

    # 烘干总量不超入库数量
    baked_per_batch: dict[str, float] = defaultdict(float)
    for cyc in payload.bake_cycles:
        if cyc.batch_id in batches:
            baked_per_batch[cyc.batch_id] += cyc.loaded_qty_kg
    for bid, qty in baked_per_batch.items():
        if qty > batches[bid].received_qty_kg + 1e-9:
            emit(_f("WM-QTY-LIMIT", batch_id=bid,
                    evidence={"batch_id": bid,
                              "reason": "累计烘干装入数量超过批次入库数量",
                              "baked_qty_kg": round(qty, 3),
                              "received_qty_kg": batches[bid].received_qty_kg}))

    # ------------------------------------------------ 2) 烘箱/保温筒版本
    def check_container(*, cid: str, version: str, kind: str,
                        start: datetime, end: datetime | None,
                        ref: dict) -> ConsumableContainerVersion | None:
        c = containers.get((cid, version))
        if c is None:
            emit(_f("WM-CONTAINER-MISSING",
                    evidence={"container_id": cid, "version": version,
                              "expected_kind": kind}, **ref))
            return None
        if c.kind != kind:
            emit(_f("WM-CONTAINER-KIND",
                    evidence={"container_id": cid, "version": version,
                              "registered_kind": c.kind, "expected_kind": kind},
                    **ref))
        check_end = end if end is not None else start
        if not (c.calibrated_from <= start and check_end <= c.calibrated_to):
            emit(_f("WM-CONTAINER-CALIBRATION",
                    evidence={"container_id": cid, "version": version,
                              "calibrated_from": c.calibrated_from.isoformat(),
                              "calibrated_to": c.calibrated_to.isoformat(),
                              "used_from": start.isoformat(),
                              "used_to": check_end.isoformat()}, **ref))
        return c

    # ------------------------------------------------ 3) 烘干周期
    bakes_by_batch: dict[str, list[BakeCycle]] = defaultdict(list)
    for cyc in payload.bake_cycles:
        bakes_by_batch[cyc.batch_id].append(cyc)

    for bid, cycs in bakes_by_batch.items():
        cycs.sort(key=lambda c: c.loaded_at)
        rule = rules.get(batches[bid].classification) if bid in batches else None
        seq = sorted(c.cycle_no for c in cycs)
        if seq != list(range(1, len(seq) + 1)):
            emit(_f("WM-CHAIN-GAP", batch_id=bid,
                    evidence={"batch_id": bid,
                              "reason": "同批次烘干周期序号不连续（缺第 n 烘记录）",
                              "cycle_nos": seq}))
        if rule is not None and seq and max(seq) > rule.max_bake_cycles:
            emit(_f("WM-REBAKE-EXCEEDED", batch_id=bid,
                    evidence={"batch_id": bid, "cycle_nos": seq,
                              "max_bake_cycles": rule.max_bake_cycles,
                              "reason": "重复烘干次数超过烘干制度上限"}))

    for cyc in payload.bake_cycles:
        ref = {"batch_id": cyc.batch_id, "bake_id": cyc.bake_id}
        batch = batches.get(cyc.batch_id)
        rule = rules.get(batch.classification) if batch else None
        check_container(cid=cyc.oven_id, version=cyc.oven_version, kind="oven",
                        start=cyc.loaded_at, end=cyc.unloaded_at, ref=ref)
        ctx = {"kind": "oven", "bake_id": cyc.bake_id,
               "batch_id": cyc.batch_id, "cycle_no": cyc.cycle_no,
               "ref": ref}
        if rule is not None:
            gaps, rng, pts = _reading_coverage(
                cyc.readings, cyc.loaded_at, cyc.unloaded_at,
                timedelta(minutes=rule.max_log_gap_minutes),
                temp_lo=rule.bake_temp_min_c, temp_hi=rule.bake_temp_max_c,
                temp_code="WM-BAKE-TEMP", context=ctx)
            failures.extend(gaps)
            failures.extend(rng)
            if pts is not None:
                soak = _in_range_minutes(
                    pts, cyc.loaded_at, cyc.unloaded_at,
                    rule.bake_temp_min_c, rule.bake_temp_max_c)
                if soak + 1e-9 < rule.min_soak_minutes:
                    emit(_f("WM-BAKE-DURATION",
                            evidence={"bake_id": cyc.bake_id,
                                      "batch_id": cyc.batch_id,
                                      "cycle_no": cyc.cycle_no,
                                      "soak_minutes": round(soak, 1),
                                      "required_minutes": rule.min_soak_minutes,
                                      "reason": "烘干温度窗口内恒温时长不足"},
                            **ref))

    # 同批次多次烘干的时序：第 n 烘不得早于第 n-1 烘
    for bid, cycs in bakes_by_batch.items():
        ordered = sorted(cycs, key=lambda c: c.cycle_no)
        for prev, cur in zip(ordered, ordered[1:]):
            if cur.loaded_at < prev.unloaded_at:
                emit(_f("WM-CHAIN-GAP", batch_id=bid, bake_id=cur.bake_id,
                        evidence={"batch_id": bid,
                                  "reason": "后续烘干周期早于前次烘干结束，时序倒置",
                                  "prev_bake_id": prev.bake_id,
                                  "bake_id": cur.bake_id}))

    # ------------------------------------------------ 4) 保温暂存
    stays_by_bake: dict[str, list[QuiverStay]] = defaultdict(list)
    out_qty_per_bake: dict[str, float] = defaultdict(float)
    for st in payload.quiver_stays:
        stays_by_bake[st.bake_id].append(st)
        out_qty_per_bake[st.bake_id] += st.loaded_qty_kg
    for bake_id, qty in out_qty_per_bake.items():
        bake = bakes.get(bake_id)
        if bake is not None and qty > bake.loaded_qty_kg + 1e-9:
            emit(_f("WM-QTY-LIMIT", batch_id=bake.batch_id, bake_id=bake_id,
                    evidence={"bake_id": bake_id, "batch_id": bake.batch_id,
                              "reason": "保温装入累计数量超过烘干出箱数量",
                              "stay_loaded_qty_kg": round(qty, 3),
                              "baked_qty_kg": bake.loaded_qty_kg}))

    for st in payload.quiver_stays:
        ref = {"batch_id": st.batch_id, "bake_id": st.bake_id,
               "stay_id": st.stay_id}
        bake = bakes.get(st.bake_id)
        batch = batches.get(st.batch_id)
        rule = rules.get(batch.classification) if batch else None
        if bake is None:
            emit(_f("WM-CHAIN-GAP",
                    evidence={"stay_id": st.stay_id, "batch_id": st.batch_id,
                              "reason": "保温暂存引用的烘干周期未登记"}, **ref))
        else:
            if bake.batch_id != st.batch_id:
                emit(_f("WM-CHAIN-GAP",
                        evidence={"stay_id": st.stay_id,
                                  "reason": "保温暂存批次与其来源烘干周期批次不一致",
                                  "bake_batch_id": bake.batch_id,
                                  "batch_id": st.batch_id}, **ref))
            if st.loaded_at < bake.unloaded_at:
                emit(_f("WM-HOLDING-GAP",
                        evidence={"stay_id": st.stay_id,
                                  "reason": "装入保温筒时刻早于烘箱取出时刻，时序倒置",
                                  "bake_unloaded_at": bake.unloaded_at.isoformat(),
                                  "stay_loaded_at": st.loaded_at.isoformat()},
                        **ref))
            elif rule is not None:
                transfer = (st.loaded_at - bake.unloaded_at).total_seconds() / 60.0
                if transfer > rule.max_transfer_minutes + 1e-9:
                    emit(_f("WM-HOLDING-GAP",
                            evidence={"stay_id": st.stay_id,
                                      "reason": "烘箱取出到装入保温筒超过最大交接时限",
                                      "transfer_minutes": round(transfer, 1),
                                      "max_transfer_minutes":
                                          rule.max_transfer_minutes,
                                      "bake_unloaded_at":
                                          bake.unloaded_at.isoformat(),
                                      "stay_loaded_at":
                                          st.loaded_at.isoformat()}, **ref))
        check_container(cid=st.quiver_id, version=st.quiver_version, kind="quiver",
                        start=st.loaded_at,
                        end=st.unloaded_at or st.loaded_at, ref=ref)
        if rule is not None:
            ctx = {"kind": "quiver", "stay_id": st.stay_id,
                   "batch_id": st.batch_id, "bake_id": st.bake_id, "ref": ref}
            # 未结束暂存只核验到末个读数；已结束须整段覆盖
            cover_end = st.unloaded_at
            open_ended = st.unloaded_at is None and st.readings
            if open_ended:
                cover_end = max(r.at for r in st.readings)
            if cover_end is not None:
                gaps, rng, _ = _reading_coverage(
                    st.readings, st.loaded_at, cover_end,
                    timedelta(minutes=rule.max_log_gap_minutes),
                    temp_lo=rule.holding_temp_min_c,
                    temp_hi=rule.holding_temp_max_c,
                    temp_code="WM-HOLDING-TEMP", context=ctx)
                failures.extend(gaps)
                failures.extend(rng)

    # ------------------------------------------------ 5) 领用段与退回/报废
    issued_per_stay: dict[str, float] = defaultdict(float)
    for seg in payload.consumable_segments:
        issued_per_stay[seg.stay_id] += seg.issued_qty_kg
    for stay_id, qty in issued_per_stay.items():
        st = stays.get(stay_id)
        if st is not None and qty > st.loaded_qty_kg + 1e-9:
            emit(_f("WM-QTY-LIMIT", batch_id=st.batch_id, stay_id=stay_id,
                    evidence={"stay_id": stay_id, "batch_id": st.batch_id,
                              "reason": "领用段领出累计数量超过保温装入数量"
                                        "（同一保温数量被重复发放）",
                              "issued_qty_kg": round(qty, 3),
                              "stay_loaded_qty_kg": st.loaded_qty_kg}))

    for seg in payload.consumable_segments:
        ref = {"batch_id": seg.batch_id, "stay_id": seg.stay_id,
               "segment_id": seg.segment_id}
        st = stays.get(seg.stay_id)
        if st is None:
            emit(_f("WM-SEGMENT-MISSING",
                    evidence={"segment_id": seg.segment_id,
                              "reason": "领用段引用的保温暂存未登记"}, **ref))
        else:
            if st.batch_id != seg.batch_id:
                emit(_f("WM-CHAIN-GAP",
                        evidence={"segment_id": seg.segment_id,
                                  "reason": "领用段批次与其保温暂存批次不一致",
                                  "stay_batch_id": st.batch_id,
                                  "batch_id": seg.batch_id}, **ref))
            if not (st.loaded_at <= seg.issued_at):
                emit(_f("WM-SEGMENT-ORDER",
                        evidence={"segment_id": seg.segment_id,
                                  "reason": "领用时刻早于保温筒装入时刻",
                                  "issued_at": seg.issued_at.isoformat(),
                                  "stay_loaded_at": st.loaded_at.isoformat()},
                        **ref))
            if st.unloaded_at is not None and seg.issued_at > st.unloaded_at:
                emit(_f("WM-HOLDING-GAP",
                        evidence={"segment_id": seg.segment_id,
                                  "reason": "领用时刻晚于保温暂存结束（焊材已离筒）",
                                  "issued_at": seg.issued_at.isoformat(),
                                  "stay_unloaded_at":
                                      st.unloaded_at.isoformat()}, **ref))

        evs = events_by_segment.get(seg.segment_id, [])
        unknown_stay = st is None
        if not unknown_stay and any(ev.at < seg.issued_at for ev in evs):
            emit(_f("WM-SEGMENT-ORDER",
                    evidence={"segment_id": seg.segment_id,
                              "reason": "领用段存在早于领出时刻的退回/报废事件"},
                    **ref))
        returned = sum(e.qty_kg for e in evs if e.event_type == "return")
        scrapped = sum(e.qty_kg for e in evs if e.event_type == "scrap")
        # 报废不超可处置数量（领出 - 已退回）
        for ev in evs:
            if ev.event_type != "scrap":
                continue
            prior_return = sum(e.qty_kg for e in evs
                               if e.event_type == "return" and e.at <= ev.at)
            disposable = seg.issued_qty_kg - prior_return
            if ev.qty_kg > disposable + 1e-9:
                emit(_f("WM-SCRAP-EXCESS",
                        evidence={"segment_id": seg.segment_id,
                                  "event_id": ev.event_id,
                                  "reason": "报废数量超过该领用段可处置数量",
                                  "scrap_qty_kg": ev.qty_kg,
                                  "disposable_qty_kg": round(disposable, 3)},
                        **ref))

    # ------------------------------------------------ 6) 实际消耗（焊口/返修）
    # 先对每次消耗解析引用链，供逐耗判定与冻结摘要
    resolved: dict[str, dict] = {}
    for ref_use in use_refs:
        u: ConsumableUse = ref_use["use"]
        weld = weld_by_no.get(ref_use["weld_no"])
        repair = repair_by_id.get(ref_use["repair_id"]) if ref_use["repair_id"] \
            else None
        at = u.used_at or (repair.repaired_at if repair else weld.welded_at)
        resolved[u.use_id] = {
            "ref": ref_use, "used_at": at, "weld": weld, "repair": repair,
            "batch": batches.get(u.batch_id),
            "segment": segments.get(u.segment_id),
        }

    # 无消耗挂账：链启用后逐道焊口、每次返修都必须引用实际批次/领用段
    for w in payload.welds:
        if not w.consumables:
            emit(_f("WM-CONSUMABLE-UNTRACED", scope="production",
                    weld_no=w.weld_no,
                    evidence={"weld_no": w.weld_no,
                              "reason": "施焊记录未引用实际消耗焊材批次与领用段"}))
    for r in payload.repairs:
        if not r.consumables:
            emit(_f("WM-CONSUMABLE-UNTRACED", scope="repair",
                    weld_no=r.weld_no, repair_id=r.repair_id,
                    evidence={"weld_no": r.weld_no, "repair_id": r.repair_id,
                              "iteration": r.iteration,
                              "reason": "返修补焊未引用实际消耗焊材批次与领用段"}))

    # 领用段数量闭合（消耗按实际 used_at 归属，引用缺失的消耗不计入以免放大）
    used_qty_by_segment: dict[str, float] = defaultdict(float)
    for use_id, info in resolved.items():
        if info["segment"] is not None:
            used_qty_by_segment[info["segment"].segment_id] += \
                info["ref"]["use"].qty_kg
    for seg in payload.consumable_segments:
        # 领用段级数量条款只按 segment 定位，避免经 stay_id 跨段传播
        seg_ref = {"batch_id": seg.batch_id, "segment_id": seg.segment_id}
        evs = events_by_segment.get(seg.segment_id, [])
        used = used_qty_by_segment.get(seg.segment_id, 0.0)
        returned = sum(e.qty_kg for e in evs if e.event_type == "return")
        scrapped = sum(e.qty_kg for e in evs if e.event_type == "scrap")
        allocated = used + returned + scrapped
        if allocated > seg.issued_qty_kg + 1e-9:
            emit(_f("WM-QTY-CONSERVATION",
                    evidence={"segment_id": seg.segment_id,
                              "reason": "消耗+退回+报废数量超过领出数量"
                                        "（同一数量被重复分配）",
                              "issued_qty_kg": seg.issued_qty_kg,
                              "used_qty_kg": round(used, 3),
                              "returned_qty_kg": round(returned, 3),
                              "scrapped_qty_kg": round(scrapped, 3)}, **ref))
        elif not evs and abs(allocated - seg.issued_qty_kg) > 1e-9:
            emit(_f("WM-QTY-OPEN",
                    evidence={"segment_id": seg.segment_id,
                              "reason": "领用段无退回/报废事件且数量未闭合",
                              "issued_qty_kg": seg.issued_qty_kg,
                              "allocated_qty_kg": round(allocated, 3),
                              "open_qty_kg":
                                  round(seg.issued_qty_kg - allocated, 3)},
                    **ref))
        elif evs and abs(allocated - seg.issued_qty_kg) > 1e-9 \
                and allocated <= seg.issued_qty_kg + 1e-9:
            emit(_f("WM-QTY-OPEN",
                    evidence={"segment_id": seg.segment_id,
                              "reason": "领用段数量未闭合（处置量与领出量不符）",
                              "issued_qty_kg": seg.issued_qty_kg,
                              "allocated_qty_kg": round(allocated, 3),
                              "open_qty_kg":
                                  round(seg.issued_qty_kg - allocated, 3)},
                    **ref))

    # 逐耗核验
    per_use_failures: dict[str, list[dict]] = defaultdict(list)
    for use_id, info in resolved.items():
        u: ConsumableUse = info["ref"]["use"]
        scope = info["ref"]["scope"]
        ref = {"batch_id": u.batch_id, "segment_id": u.segment_id,
               "use_id": use_id,
               "weld_no": info["ref"]["weld_no"],
               "repair_id": info["ref"]["repair_id"]}
        local: list[dict] = []
        batch = info["batch"]
        seg = info["segment"]
        weld, repair = info["weld"], info["repair"]
        if batch is None:
            local.append(_f("WM-SEGMENT-MISSING" if seg is None
                            else "WM-CHAIN-GAP", scope=scope,
                            evidence={"use_id": use_id,
                                      "batch_id": u.batch_id,
                                      "reason": "消耗引用的焊材批次未登记"}, **ref))
        if seg is None:
            local.append(_f("WM-SEGMENT-MISSING", scope=scope,
                            evidence={"use_id": use_id,
                                      "segment_id": u.segment_id,
                                      "reason": "消耗引用的领用段未登记"}, **ref))
        if batch is not None and seg is not None \
                and seg.batch_id != batch.batch_id:
            local.append(_f("WM-CHAIN-GAP", scope=scope,
                            evidence={"use_id": use_id,
                                      "reason": "消耗批次与其领用段批次不一致",
                                      "segment_batch_id": seg.batch_id}, **ref))
        at = info["used_at"]
        st = stays.get(seg.stay_id) if seg else None
        bake = bakes.get(st.bake_id) if st else None
        rule = rules.get(batch.classification) if batch else None
        wps_no = repair.wps_no if repair else weld.wps_no
        wps = next((w for w in payload.wps if w.wps_no == wps_no), None)

        # 分类号必须在 WPS 焊材清单内（牌号拿错）
        if batch is not None:
            if not wps.consumable_classes:
                local.append(_f("WM-WPS-CLASS-MISMATCH", scope=scope,
                                evidence={"use_id": use_id, "wps_no": wps_no,
                                          "actual_classification":
                                              batch.classification,
                                          "wps_classes": [],
                                          "reason": "WPS 未规定焊材分类号，"
                                                    "无法证明焊材合规"}, **ref))
            elif batch.classification not in wps.consumable_classes:
                local.append(_f("WM-WPS-CLASS-MISMATCH", scope=scope,
                                evidence={"use_id": use_id, "wps_no": wps_no,
                                          "actual_classification":
                                              batch.classification,
                                          "wps_classes":
                                              list(wps.consumable_classes)}, **ref))
            # 批次登记的适用 WPS 范围
            if wps_no not in batch.applicable_wps:
                local.append(_f("WM-BATCH-WPS-NOT-APPLICABLE", scope=scope,
                                evidence={"use_id": use_id, "wps_no": wps_no,
                                          "batch_id": batch.batch_id,
                                          "applicable_wps":
                                              list(batch.applicable_wps)}, **ref))

        # 时序与暴露
        if seg is not None:
            if at < seg.issued_at:
                local.append(_f("WM-SEGMENT-ORDER", scope=scope,
                                evidence={"use_id": use_id,
                                          "reason": "施焊/补焊时刻早于领用时刻",
                                          "used_at": at.isoformat(),
                                          "issued_at":
                                              seg.issued_at.isoformat()}, **ref))
            else:
                exposure = (at - seg.issued_at).total_seconds() / 60.0
                if rule is not None and \
                        exposure > rule.max_exposure_minutes + 1e-9:
                    local.append(_f("WM-EXPOSURE-EXCEEDED", scope=scope,
                                    evidence={"use_id": use_id,
                                              "issued_at":
                                                  seg.issued_at.isoformat(),
                                              "used_at": at.isoformat(),
                                              "exposure_minutes":
                                                  round(exposure, 1),
                                              "max_exposure_minutes":
                                                  rule.max_exposure_minutes},
                                    **ref))
            # 退回后仍在施焊
            for ev in events_by_segment.get(seg.segment_id, []):
                if at > ev.at:
                    local.append(_f("WM-SEGMENT-ORDER", scope=scope,
                                    evidence={"use_id": use_id,
                                              "event_id": ev.event_id,
                                              "event_type": ev.event_type,
                                              "reason": f"消耗发生在{ev.event_type}"
                                                        "事件之后",
                                              "used_at": at.isoformat(),
                                              "event_at": ev.at.isoformat()},
                                    **ref))
                    break
        if st is not None:
            if st.unloaded_at is not None and at > st.unloaded_at \
                    and (seg is None or at > seg.issued_at):
                # 已领出后的暴露由暴露条款处理；领用前离筒才是保温断档
                if seg is None or at <= seg.issued_at:
                    local.append(_f("WM-HOLDING-GAP", scope=scope,
                                    evidence={"use_id": use_id,
                                              "reason": "消耗时刻晚于保温暂存结束",
                                              "used_at": at.isoformat(),
                                              "stay_unloaded_at":
                                                  st.unloaded_at.isoformat()},
                                    **ref))

        failures.extend(local)
        per_use_failures[use_id] = local

    # ------------------------------------------------ 7) 每次消耗汇总结论
    # 实体级失败（批次/烘干/暂存/领用段）传播到挂在其下的所有消耗
    descendants: dict[str, set[str]] = defaultdict(set)
    for use_id, info in resolved.items():
        u = info["ref"]["use"]
        seg = info["segment"]
        st = stays.get(seg.stay_id) if seg else None
        bake = bakes.get(st.bake_id) if st else None
        descendants[("batch", u.batch_id)].add(use_id)
        descendants[("segment", u.segment_id)].add(use_id)
        if st:
            descendants[("stay", st.stay_id)].add(use_id)
        if bake:
            descendants[("bake", bake.bake_id)].add(use_id)

    def entity_failures_for(use_id: str) -> list[dict]:
        info = resolved[use_id]
        u = info["ref"]["use"]
        seg = info["segment"]
        st = stays.get(seg.stay_id) if seg else None
        bake = bakes.get(st.bake_id) if st else None
        keys = {("batch", u.batch_id), ("segment", u.segment_id)}
        if st:
            keys.add(("stay", st.stay_id))
        if bake:
            keys.add(("bake", bake.bake_id))
        out = []
        at = info["used_at"]
        for f in failures:
            if f.get("scope"):
                continue  # 消耗级失败已在 per_use_failures
            ids = f["ids"]
            # 只按失败所属实体本体匹配：批次级问题传播到该批次全部消耗，
            # 领用段级问题（如数量重复分配）只影响该段，不跨段扩散
            own_match = (
                (ids.get("batch_id") and ("batch", u.batch_id) in keys
                 and not (ids.get("segment_id") or ids.get("stay_id")
                          or ids.get("bake_id")))
                or (ids.get("segment_id") and ("segment", u.segment_id) in keys)
                or (ids.get("stay_id") and ("stay", ids.get("stay_id"))
                    in keys and not ids.get("segment_id"))
                or (ids.get("bake_id") and ("bake", ids.get("bake_id"))
                    in keys and not (ids.get("stay_id")
                                     or ids.get("segment_id")))
            )
            if not own_match:
                continue
            matched = "batch" if ids.get("batch_id") and not (
                ids.get("segment_id") or ids.get("stay_id")
                or ids.get("bake_id")) else (
                "segment" if ids.get("segment_id")
                else ("stay" if ids.get("stay_id") else "bake"))
            # 烘箱校准失效按整个烘干周期传播（该烘焊材全部失效）；
            # 保温筒校准失效只影响到期点之后的消耗（到期前领用仍有效），
            # 与 NDE 设备跨到期点口径区分：焊材保温筒按取用时点核对。
            if f["code"] == "WM-CONTAINER-CALIBRATION" and matched == "stay" \
                    and st is not None:
                c = containers.get((st.quiver_id, st.quiver_version))
                if c is not None and at is not None and \
                        st.loaded_at <= at <= c.calibrated_to:
                    continue
            out.append(f)
        return out

    use_states: dict[str, dict] = {}
    for use_id, info in resolved.items():
        u: ConsumableUse = info["ref"]["use"]
        eff = per_use_failures.get(use_id, []) + entity_failures_for(use_id)
        codes = sorted({f["code"] for f in eff})
        seg = info["segment"]
        st = stays.get(seg.stay_id) if seg else None
        bake = bakes.get(st.bake_id) if st else None
        batch = info["batch"]
        rule = rules.get(batch.classification) if batch else None
        at = info["used_at"]
        exposure = round((at - seg.issued_at).total_seconds() / 60.0, 1) \
            if seg and at >= seg.issued_at else None
        use_states[use_id] = {
            "use_id": use_id,
            "scope": info["ref"]["scope"],
            "weld_no": info["ref"]["weld_no"],
            "repair_id": info["ref"]["repair_id"],
            "iteration": info["repair"].iteration if info["repair"] else None,
            "batch_id": u.batch_id,
            "segment_id": u.segment_id,
            "qty_kg": u.qty_kg,
            "used_at": at.isoformat() if at else None,
            "consumable_valid": not codes,
            "reasons": codes,
            "exposure_minutes": exposure,
            "exposure_limit_minutes": rule.max_exposure_minutes if rule else None,
            "chain": _chain_summary(
                u=u, batch=batch, rule=rule, bake=bake, stay=st, seg=seg,
                events=events_by_segment.get(u.segment_id, []), at=at),
        }

    return {
        "enabled": True,
        "use_states": use_states,
        "failures": failures,
        "chain_failures": [f for f in failures if not f.get("scope")],
        "resolved": resolved,
        "indexes": {
            "batches": batches, "rules": rules, "containers": containers,
            "bakes": bakes, "stays": stays, "segments": segments,
            "events_by_segment": dict(events_by_segment),
        },
    }


def _chain_summary(*, u: ConsumableUse, batch: ConsumableBatch | None,
                   rule: ConsumableRule | None, bake: BakeCycle | None,
                   stay: QuiverStay | None, seg: ConsumableIssueSegment | None,
                   events: list[ConsumableSegmentEvent], at: datetime | None):
    """单次消耗的冻结用事件链摘要（随快照冻结，续录不回写旧版）。"""
    return {
        "use_id": u.use_id,
        "qty_kg": u.qty_kg,
        "used_at": at.isoformat() if at else None,
        "batch": None if batch is None else {
            "batch_id": batch.batch_id,
            "classification": batch.classification,
            "designation": batch.designation,
            "manufacturer_lot_no": batch.manufacturer_lot_no,
            "cert_no": batch.cert_no,
            "cert_lot_no": batch.cert_lot_no,
            "received_status": batch.received_status,
            "applicable_wps": list(batch.applicable_wps),
        },
        "rule": None if rule is None else rule.model_dump(mode="json"),
        "bake": None if bake is None else {
            "bake_id": bake.bake_id, "cycle_no": bake.cycle_no,
            "oven_id": bake.oven_id, "oven_version": bake.oven_version,
            "loaded_at": bake.loaded_at.isoformat(),
            "unloaded_at": bake.unloaded_at.isoformat(),
            "loaded_qty_kg": bake.loaded_qty_kg,
        },
        "stay": None if stay is None else {
            "stay_id": stay.stay_id,
            "quiver_id": stay.quiver_id, "quiver_version": stay.quiver_version,
            "loaded_at": stay.loaded_at.isoformat(),
            "unloaded_at": stay.unloaded_at.isoformat()
            if stay.unloaded_at else None,
            "loaded_qty_kg": stay.loaded_qty_kg,
        },
        "segment": None if seg is None else {
            "segment_id": seg.segment_id,
            "issued_at": seg.issued_at.isoformat(),
            "issued_qty_kg": seg.issued_qty_kg,
            "issued_to": seg.issued_to,
        },
        "events": [e.model_dump(mode="json") for e in events],
    }
