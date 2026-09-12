"""合规审查引擎。

输入 SubmissionPayload，输出结构化审查结果：
1. 按施焊/补焊时点匹配 WPS/PQR 与焊工资格；
2. 一道焊口两个母材炉批，逐端核对材料组别与厚度；
3. 检验批按时间序模拟抽检 -> 扩检（加倍/全检），核算覆盖率；
4. 沿缺陷周向位置把 不合格底片 -> 挖补返修 -> 复检底片 串成链，
   复检范围必须覆盖挖补区，返修前旧底片不得充数。
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from . import geometry as geo
from .clauses import CLAUSES, Severity
from .consumables import verify_consumables
from .nde_resource import verify_nde_resource
from .weld_execution import verify_weld_passes
from .qualification import (
    match_welder,
    match_wps,
    weld_diameters,
    weld_groups,
    weld_thicknesses,
)
from .schemas import (
    NdeRecord,
    RepairRecord,
    SubmissionPayload,
    WeldRecord,
)

MAX_REPAIRS = 2


def _finding(code: str, *, weld_no=None, lot_id=None, nde_id=None,
             repair_id=None, report_no=None, evidence=None,
             batch_id=None, segment_id=None, bake_id=None, stay_id=None,
             use_id=None, pass_id=None, measure_id=None) -> dict:
    meta = CLAUSES[code]
    return {
        "code": code,
        "severity": meta["severity"],
        "category": meta["category"],
        "reference": meta["reference"],
        "message": meta["message"],
        "weld_no": weld_no,
        "lot_id": lot_id,
        "nde_id": nde_id,
        "repair_id": repair_id,
        "report_no": report_no,
        "batch_id": batch_id,
        "segment_id": segment_id,
        "bake_id": bake_id,
        "stay_id": stay_id,
        "use_id": use_id,
        "pass_id": pass_id,
        "measure_id": measure_id,
        "evidence": evidence or {},
    }


def _ceil_ratio(ratio: float, total: int) -> int:
    return max(1, math.ceil(ratio * total - 1e-9))


def json_dumps_evidence(evidence) -> str:
    return json.dumps(evidence or {}, ensure_ascii=False,
                      sort_keys=True, default=str)


def _wm_finding(fail: dict) -> dict:
    """把焊材核验模块的消耗级 failure 转成引擎 finding（定位到焊口/返修）。"""
    ids = fail.get("ids") or {}
    return {
        "code": fail["code"],
        "severity": fail["severity"],
        "category": fail["category"],
        "reference": fail["reference"],
        "message": fail["message"],
        "weld_no": fail.get("weld_no"),
        "lot_id": None,
        "nde_id": None,
        "repair_id": fail.get("repair_id"),
        "report_no": None,
        "batch_id": ids.get("batch_id"),
        "segment_id": ids.get("segment_id"),
        "bake_id": ids.get("bake_id"),
        "stay_id": ids.get("stay_id"),
        "use_id": ids.get("use_id"),
        "evidence": fail.get("evidence") or {},
    }


# ================================================================ 返修/复检链


def _evaluate_repair_chain(weld: WeldRecord, ndes: list[NdeRecord],
                           repairs: list[RepairRecord], wps_index, welder_index,
                           nde_valid: dict[str, bool] | None = None,
                           repair_consumable_invalid: dict[str, bool] | None = None,
                           repair_consumable_codes: dict[str, list[str]] | None = None,
                           repair_consumable_links: dict[str, list[dict]] | None = None,
                           repair_pass_invalid: dict[str, bool] | None = None,
                           repair_pass_codes: dict[str, list[str]] | None = None,
                           repair_pass_links: dict[str, list[dict]] | None = None):
    """沿缺陷位置串接 原始不合格 -> 挖补 -> 复检，返回 (findings, links, closed)。

    资源核验未通过的检测记录（nde_valid[nde_id]=False）一律不参与串接：
    既不能作为不合格缺陷依据，也不能作为返修复检合格片。
    repair_consumable_invalid[repair_id]=True 时该次返修焊材链不合规，
    失效焊材不得用于返修闭合（RP-WM-INVALID）。
    repair_pass_invalid[repair_id]=True 时该次返修补焊道次链不合规，
    返修混用参数不得被最终合格报告掩盖（WP-REPAIR-PASS-INVALID）。
    """
    nde_valid = nde_valid or {}
    repair_consumable_invalid = repair_consumable_invalid or {}
    repair_consumable_codes = repair_consumable_codes or {}
    repair_consumable_links = repair_consumable_links or {}
    repair_pass_invalid = repair_pass_invalid or {}
    repair_pass_codes = repair_pass_codes or {}
    repair_pass_links = repair_pass_links or {}
    findings: list[dict] = []
    links: list[dict] = []

    ndes_by_iter: dict[int, list[NdeRecord]] = defaultdict(list)
    for r in ndes:
        if nde_valid.get(r.nde_id, True):
            ndes_by_iter[r.iteration].append(r)
    for lst in ndes_by_iter.values():
        lst.sort(key=lambda r: r.examined_at)

    repairs_by_iter: dict[int, RepairRecord] = {}
    for rep in repairs:
        repairs_by_iter[rep.iteration] = rep  # 模型层已保证同焊口同次唯一

    groups = weld_groups(weld)
    thicknesses = weld_thicknesses(weld)
    diameters = weld_diameters(weld)

    last_iteration = max(
        [0] + list(ndes_by_iter.keys()) + list(repairs_by_iter.keys())
    )

    # 初始（iteration 0）不合格显示的位置汇总；逐次返修时据此核对挖补完整性
    initial_rejects = [r for r in ndes_by_iter.get(0, []) if r.result == "reject"]

    # 有复检底片却没有对应返修记录：序列断裂
    for it in range(1, last_iteration + 1):
        if ndes_by_iter.get(it) and it not in repairs_by_iter:
            findings.append(_finding(
                "RP-SEQ-GAP",
                weld_no=weld.weld_no,
                evidence={"iteration": it,
                          "reason": f"存在第 {it} 轮复检底片但无第 {it} 次返修记录"},
            ))

    # --- 逐次返修 ---
    prev_reject_method: str | None = None
    if initial_rejects:
        prev_reject_method = initial_rejects[0].method

    for it in range(1, last_iteration + 1):
        rep = repairs_by_iter.get(it)
        if rep is None:
            if it <= MAX_REPAIRS and any(r.iteration >= it for r in repairs):
                findings.append(_finding(
                    "RP-SEQ-GAP", weld_no=weld.weld_no,
                    evidence={"iteration": it, "reason": f"缺少第 {it} 次返修记录"},
                ))
            continue

        # 本iteration产生的阻塞性条款：任一存在则该次返修不得闭合
        iter_blocking: list[dict] = []

        # 次序与次数
        if it > 1 and (it - 1) not in repairs_by_iter:
            iter_blocking.append(_finding(
                "RP-SEQ-GAP", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"iteration": it},
            ))
        if it > MAX_REPAIRS:
            iter_blocking.append(_finding(
                "RP-LIMIT-EXCEEDED", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"iteration": it, "limit": MAX_REPAIRS},
            ))

        # --- 在先缺陷依据与时间次序 ---
        # 第 1 次返修的在先是初始不合格片；第 n 次返修的在先是第 n-1 轮复检不合格片
        predecessor_rejects = (
            initial_rejects if it == 1
            else [r for r in ndes_by_iter.get(it - 1, []) if r.result == "reject"]
        )
        if not predecessor_rejects:
            iter_blocking.append(_finding(
                "RP-ORDER-INVALID", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"iteration": it,
                          "reason": "无在先不合格显示即实施返修，返修无缺陷依据"},
            ))
        else:
            # 补焊必须晚于全部在先不合格片（以最晚一张为准）
            predecessor_deadline = max(r.examined_at for r in predecessor_rejects)
            if rep.repaired_at <= predecessor_deadline:
                iter_blocking.append(_finding(
                    "RP-ORDER-INVALID", weld_no=weld.weld_no,
                    repair_id=rep.repair_id, nde_id=predecessor_rejects[-1].nde_id,
                    evidence={"iteration": it,
                              "repaired_at": rep.repaired_at.isoformat(),
                              "prior_reject_at": predecessor_deadline.isoformat(),
                              "reason": "补焊时刻不晚于在先不合格底片，"
                                        "缺陷发现→补焊次序倒置"},
                ))

            # 挖补区必须完整覆盖（而非仅相交）在先全部缺陷显示位置
            predecessor_defects = [
                loc for rec in predecessor_rejects
                for loc in rec.defect_locations
            ]
            if not geo.covers([rep.excavated_band], predecessor_defects):
                iter_blocking.append(_finding(
                    "RP-DEFECT-NOT-EXCAVATED",
                    weld_no=weld.weld_no, repair_id=rep.repair_id,
                    evidence={
                        "iteration": it,
                        "excavated_band": geo.band_dict(rep.excavated_band),
                        "prior_defect_locations": [
                            geo.band_dict(loc) for loc in predecessor_defects
                        ],
                        "reason": "挖补区未完整覆盖在先缺陷位置，"
                                  "仅相交不构成缺陷已清除",
                    },
                ))

        # 批准
        if not rep.approved or not rep.approved_by:
            iter_blocking.append(_finding(
                "RP-NO-APPROVAL", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"approved": rep.approved, "approved_by": rep.approved_by},
            ))

        # 补焊时点资格（WPS、焊工各自判定）
        wps = wps_index.get(rep.wps_no)
        for code in match_wps(
            wps, at=rep.repaired_at, groups=groups,
            thicknesses=thicknesses, process=weld.weld_process,
        ):
            iter_blocking.append(_finding(
                code, weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"at": rep.repaired_at.isoformat(), "wps_no": rep.wps_no,
                          "scope": "repair_wps", "iteration": it},
            ))
        if wps is None:
            # 未登记 WPS 用 RP-NO-WPS 再显式挂一条返修条款
            iter_blocking.append(_finding(
                "RP-NO-WPS", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"wps_no": rep.wps_no},
            ))

        welder = welder_index.get(rep.welder_id)
        welder_codes = match_welder(
            welder, at=rep.repaired_at, groups=groups, thicknesses=thicknesses,
            diameters=diameters, process=weld.weld_process,
            position=weld.weld_position,
        )
        for code in welder_codes:
            iter_blocking.append(_finding(
                code, weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"at": rep.repaired_at.isoformat(),
                          "welder_id": rep.welder_id,
                          "scope": "repair_welder", "iteration": it},
            ))
        if welder is None:
            iter_blocking.append(_finding(
                "RP-NO-WELDER", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"welder_id": rep.welder_id},
            ))

        # 焊材链：返修补焊消耗的焊材必须合规；失效焊材不得用于返修闭合
        if repair_consumable_invalid.get(rep.repair_id):
            iter_blocking.append(_finding(
                "RP-WM-INVALID", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"iteration": it,
                          "wm_codes": repair_consumable_codes.get(
                              rep.repair_id, [])},
            ))
        # 道次链：返修补焊道次必须按返修 WPS 窗口施焊；混用/越限参数
        # 不得被最终合格报告掩盖
        if repair_pass_invalid.get(rep.repair_id):
            iter_blocking.append(_finding(
                "WP-REPAIR-PASS-INVALID", weld_no=weld.weld_no,
                repair_id=rep.repair_id,
                evidence={"iteration": it,
                          "wp_codes": repair_pass_codes.get(rep.repair_id, [])},
            ))
        findings.extend(iter_blocking)

        # --- 复检时序：同iteration、同方法的底片若早于/等于补焊，判次序倒置 ---
        # 该阻断条款必须同时进入 iter_blocking：即使同轮另有一张晚于补焊、
        # 覆盖充分的合格片，本iteration也不得判闭合（链节与整链保持 false）。
        same_iter = ndes_by_iter.get(it, [])
        for rec in same_iter:
            same_method = (prev_reject_method is None
                           or rec.method == prev_reject_method)
            if same_method and rec.examined_at <= rep.repaired_at:
                order_finding = _finding(
                    "RP-ORDER-INVALID", weld_no=weld.weld_no,
                    repair_id=rep.repair_id, nde_id=rec.nde_id,
                    evidence={"iteration": it,
                              "repaired_at": rep.repaired_at.isoformat(),
                              "reinspected_at": rec.examined_at.isoformat(),
                              "reason": "复检底片不晚于补焊时刻，"
                                        "补焊→复检次序倒置，该底片无效"},
                )
                findings.append(order_finding)
                iter_blocking.append(order_finding)

        # --- 复检：必须发生在补焊之后、同方法、覆盖挖补区、结论闭合 ---
        candidates = [
            r for r in same_iter
            if r.examined_at > rep.repaired_at
        ]
        method_matched = [r for r in candidates
                          if prev_reject_method is None or r.method == prev_reject_method]
        method_other = [r for r in candidates if r not in method_matched]

        reinspection_dicts: list[dict] = []
        closed = False

        if not method_matched:
            reason = "返修后无同方法复检记录"
            if method_other:
                reason = "复检方法与发现缺陷的方法不一致，不能替代"
            findings.append(_finding(
                "RP-NO-REINSPECTION", weld_no=weld.weld_no, repair_id=rep.repair_id,
                evidence={"iteration": it, "expected_method": prev_reject_method,
                          "reason": reason,
                          "other_method_nde": [r.nde_id for r in method_other]},
            ))
        else:
            cover_bands: list = []
            reject_records: list[NdeRecord] = []
            old_films: list[dict] = []
            # 返修前拍过的底片即使覆盖挖补区也不得作为复检依据
            for old in ndes_by_iter.get(it - 1, []):
                if any(
                    geo.intersects([rep.excavated_band], [c]) for c in old.coverage
                ):
                    old_films.append({"nde_id": old.nde_id,
                                      "examined_at": old.examined_at.isoformat(),
                                      "iteration": old.iteration})
            for rec in method_matched:
                cover_bands.extend(rec.coverage)
                reinspection_dicts.append({
                    "nde_id": rec.nde_id,
                    "method": rec.method,
                    "result": rec.result,
                    "examined_at": rec.examined_at.isoformat(),
                    "coverage": [geo.band_dict(b) for b in rec.coverage],
                    "report_no": rec.report_no,
                })
                if rec.result == "reject":
                    reject_records.append(rec)

            # 覆盖挖补区
            excavation_covered = geo.covers(
                cover_bands, [rep.excavated_band]
            )
            if not excavation_covered:
                findings.append(_finding(
                    "RP-EXCAVATION-UNCOVERED",
                    weld_no=weld.weld_no, repair_id=rep.repair_id,
                    evidence={
                        "iteration": it,
                        "excavated_band": geo.band_dict(rep.excavated_band),
                        "reinspection_coverage": [
                            geo.band_dict(b) for b in cover_bands
                        ],
                        "old_films_excluded": old_films,
                        "reason": "复检覆盖范围未包含挖补区；旧底片不得复用",
                    },
                ))

            # 本轮仍有不合格显示：须由下一次返修沿位置闭合
            next_rep = repairs_by_iter.get(it + 1)
            for rec in reject_records:
                prev_reject_method = rec.method
                for loc in rec.defect_locations:
                    if next_rep is None:
                        findings.append(_finding(
                            "RP-REINSPECTION-REJECT",
                            weld_no=weld.weld_no, nde_id=rec.nde_id,
                            repair_id=rep.repair_id,
                            evidence={"iteration": it,
                                      "defect": geo.band_dict(loc),
                                      "reason": "复检仍不合格且无后续返修闭合"},
                        ))

            # 闭合条件（全部满足）：
            # 1) 本轮复检合格；2) 复检覆盖挖补区；3) 挖补完整覆盖在先缺陷；
            # 4) 时序有效；5) 批准/资格/次数等本iteration阻塞条款均不存在
            iteration_blocked = any(
                f["severity"] == Severity.HOLD.value for f in iter_blocking
            )
            if (not reject_records and excavation_covered
                    and not iteration_blocked):
                closed = True

        links.append({
            "iteration": it,
            "repair_id": rep.repair_id,
            "repaired_at": rep.repaired_at,
            "wps_no": rep.wps_no,
            "welder_id": rep.welder_id,
            "approved": rep.approved and bool(rep.approved_by),
            "excavated_band": geo.band_dict(rep.excavated_band),
            "reinspections": reinspection_dicts,
            "consumable_uses": repair_consumable_links.get(rep.repair_id, []),
            "weld_passes": repair_pass_links.get(rep.repair_id, []),
            "passes_valid": not repair_pass_invalid.get(rep.repair_id, False),
            "closed": closed,
        })

    # 初始有不合格显示但整口无任何返修：缺陷保持开口
    if initial_rejects and not repairs_by_iter:
        for rec in initial_rejects:
            for loc in rec.defect_locations:
                findings.append(_finding(
                    "NDE-OPEN-DEFECT",
                    weld_no=weld.weld_no, nde_id=rec.nde_id,
                    evidence={"defect": geo.band_dict(loc),
                              "nde_id": rec.nde_id,
                              "reason": "缺陷位置无返修挖补记录"},
                ))

    # 整口闭合：存在初始不合格时，每一次返修链节都必须闭合，
    # 且最后一次返修的挖补必须完整覆盖其在先缺陷（防止早期挖补偏小被后续链节掩盖）。
    chain_closed = True
    if initial_rejects:
        chain_closed = bool(links)
        if chain_closed:
            last_rep = repairs_by_iter[max(repairs_by_iter)]
            last_pred = (
                initial_rejects if last_rep.iteration == 1
                else [r for r in ndes_by_iter.get(last_rep.iteration - 1, [])
                      if r.result == "reject"]
            )
            last_defects = [loc for rec in last_pred for loc in rec.defect_locations]
            if not geo.covers([last_rep.excavated_band], last_defects):
                chain_closed = False
            else:
                chain_closed = all(link["closed"] for link in links)
    return findings, links, chain_closed


# ================================================================ 检验批


def _evaluate_lots(payload: SubmissionPayload, weld_index, hold_welds,
                   nde_valid: dict[str, bool]):
    """按时间序模拟抽检与扩检。返回 (lot_findings_per_weld, lot_summaries)。

    资源核验未通过的检测报告（nde_valid=False）不计入抽检/扩检口数，
    也不构成扩检触发事件（不能凭无效底片认定扩检序列）。
    """
    rules = {rule.lot_id: rule for rule in payload.lot_rules}

    # 焊口按所属检验批分组；焊口带了 lot_id 但未登记规则也要建组（LT-RULE-MISSING）
    welds_by_lot: dict[str, list[WeldRecord]] = defaultdict(list)
    for w in payload.welds:
        if w.lot_id:
            welds_by_lot[w.lot_id].append(w)

    # 检测单归属检验批（nde.lot_id 缺省随焊口）
    ndes_by_lot: dict[str, list[NdeRecord]] = defaultdict(list)
    orphan_findings: list[dict] = []
    method_warnings: list[dict] = []
    excluded_by_lot: dict[str, list[dict]] = defaultdict(list)
    for rec in payload.nde:
        weld = weld_index.get(rec.weld_no)
        lot_id = rec.lot_id or (weld.lot_id if weld else None)
        if not nde_valid.get(rec.nde_id, True):
            if lot_id is not None:
                excluded_by_lot[lot_id].append({
                    "nde_id": rec.nde_id,
                    "report_no": rec.report_no,
                    "weld_no": rec.weld_no,
                    "iteration": rec.iteration,
                })
            # 资源失效报告不计入后续任何批核算
            continue
        if lot_id is None:
            continue
        if lot_id not in rules and lot_id not in welds_by_lot:
            orphan_findings.append(_finding(
                "NDE-UNKNOWN-LOT",
                weld_no=rec.weld_no, lot_id=lot_id, nde_id=rec.nde_id,
                report_no=rec.report_no,
            ))
            continue
        ndes_by_lot[lot_id].append(rec)
        rule = rules.get(lot_id)
        if rule and rec.method != rule.required_method:
            method_warnings.append(_finding(
                "NDE-METHOD-LOT-MISMATCH",
                weld_no=rec.weld_no, lot_id=lot_id, nde_id=rec.nde_id,
                report_no=rec.report_no,
                evidence={"required": rule.required_method, "actual": rec.method},
            ))

    lot_findings: dict[str, list[dict]] = defaultdict(list)
    summaries: list[dict] = []

    for lot_id, lot_welds in sorted(welds_by_lot.items()):
        rule = rules.get(lot_id)
        weld_nos = {w.weld_no for w in lot_welds}
        n_total = len(lot_welds)
        findings: list[dict] = []

        if rule is None:
            findings.append(_finding(
                "LT-RULE-MISSING", lot_id=lot_id,
                evidence={"weld_count": n_total},
            ))
            summaries.append(_lot_summary(
                lot_id, None, 0.0, n_total, 0, 0, False, None, 0, 0, 0.0,
                "hold", findings, excluded_by_lot.get(lot_id, [])))
            lot_findings[lot_id] = findings
            continue

        # 只有原始检测（iteration 0）计入抽检/扩检数量；返修复检不算新抽检口
        orig = [r for r in ndes_by_lot.get(lot_id, []) if r.iteration == 0
                and r.weld_no in weld_nos]
        orig.sort(key=lambda r: (r.examined_at, r.nde_id))

        first_seen: dict[str, datetime] = {}
        reject_events: list[NdeRecord] = []
        for rec in orig:
            first_seen.setdefault(rec.weld_no, rec.examined_at)
            if rec.result == "reject":
                reject_events.append(rec)

        n0 = max(rule.min_samples, _ceil_ratio(rule.sample_ratio, n_total))
        examined_final = len(first_seen)
        rejects = bool(reject_events)
        extended = rejects
        required_final = n0
        extension_mode = None

        if rejects:
            first_reject_at = min(r.examined_at for r in reject_events)
            extension_mode = rule.on_reject
            # 首批 = 首拒时点（含）前完成首检的口；首拒当口计入初始抽检
            initial_welds = {wno for wno, t in first_seen.items()
                             if t <= first_reject_at}
            # 扩检序列 = 首拒之后新增首检的口
            ext_ordered = [wno for wno, t in sorted(first_seen.items(),
                           key=lambda kv: (kv[1], kv[0]))
                           if t > first_reject_at]
            # 扩检序列中的不合格片（用于判定扩检批是否再次失败）
            ext_reject_welds = {r.weld_no for r in orig
                                if r.result == "reject"
                                and r.examined_at > first_reject_at}

            if rule.on_reject == "full":
                required_final = n_total
            else:
                # double：抽检比例翻倍（按批总量计）；扩检批再失败 => 全检
                double_required = max(
                    rule.min_samples,
                    _ceil_ratio(rule.sample_ratio * 2.0, n_total),
                )
                # 扩检批容量 = 翻倍总量 - 首批已检
                ext_capacity = max(0, double_required - len(initial_welds))
                batch1 = set(ext_ordered[:ext_capacity])
                if batch1 & ext_reject_welds:
                    required_final = n_total
                else:
                    required_final = double_required

        # 结论判定
        if not rejects and examined_final < n0:
            findings.append(_finding(
                "LT-SAMPLE-INSUFFICIENT", lot_id=lot_id,
                evidence={"required_initial": n0,
                          "examined": examined_final,
                          "ratio_required": rule.sample_ratio,
                          "ratio_actual": round(examined_final / n_total, 4)},
            ))
        elif rejects and examined_final < required_final:
            findings.append(_finding(
                "LT-EXTENSION-INSUFFICIENT", lot_id=lot_id,
                evidence={"mode": extension_mode,
                          "required_final": required_final,
                          "examined_final": examined_final,
                          "first_reject_at": min(
                              r.examined_at for r in reject_events
                          ).isoformat()},
            ))

        # 扩检中仍有未闭合的不合格口 => 整批 hold
        open_reject_welds = sorted({
            r.weld_no for r in reject_events if r.weld_no in hold_welds
        })
        if open_reject_welds:
            findings.append(_finding(
                "LT-OPEN-REJECT", lot_id=lot_id,
                evidence={"open_reject_welds": open_reject_welds},
            ))

        decision = "hold" if any(
            f["severity"] == Severity.HOLD.value for f in findings
        ) else "release"

        summaries.append(_lot_summary(
            lot_id, rule.required_method, rule.sample_ratio, n_total,
            n0,
            len([w for w, t in first_seen.items()
                 if not rejects or t <= min(
                     r.examined_at for r in reject_events)]),
            extended, extension_mode, required_final, examined_final,
            round(examined_final / n_total, 4), decision, findings,
            excluded_by_lot.get(lot_id, []),
        ))
        lot_findings[lot_id] = findings

    # 批次挂口：批次 hold 时，批内所有焊口连带 hold
    per_weld: dict[str, list[dict]] = defaultdict(list)
    for lot_id, findings in lot_findings.items():
        holds = [f for f in findings if f["severity"] == Severity.HOLD.value]
        for f in holds:
            for w in welds_by_lot[lot_id]:
                per_weld[w.weld_no].append({**f, "weld_no": w.weld_no})

    # 警告/孤儿记录直接挂到对应焊口
    for f in orphan_findings + method_warnings:
        per_weld[f["weld_no"]].append(f)

    return per_weld, summaries


def _lot_summary(lot_id, method, ratio, total, req_init, exam_init,
                 extended, mode, req_final, exam_final, final_ratio,
                 decision, findings, excluded_reports=None) -> dict:
    return {
        "lot_id": lot_id,
        "required_method": method,
        "required_ratio": ratio,
        "total_welds": total,
        "required_initial": req_init,
        "examined_initial": exam_init,
        "extended": extended,
        "extension_mode": mode,
        "required_final": req_final,
        "examined_final": exam_final,
        "final_ratio": final_ratio,
        "excluded_reports": excluded_reports or [],
        "decision": decision,
        "findings": findings,
    }


# ================================================================ 单焊口


def _evaluate_weld(weld: WeldRecord, payload: SubmissionPayload,
                   wps_index, welder_index,
                   nde_valid: dict[str, bool] | None = None,
                   repair_consumable_invalid: dict[str, bool] | None = None,
                   repair_consumable_codes: dict[str, list[str]] | None = None,
                   repair_consumable_links: dict[str, list[dict]] | None = None,
                   repair_pass_invalid: dict[str, bool] | None = None,
                   repair_pass_codes: dict[str, list[str]] | None = None,
                   repair_pass_links: dict[str, list[dict]] | None = None):
    findings: list[dict] = []

    # 炉批：两个母材端都必须有炉批号；异径提示
    if not weld.end_a.heat_no or not weld.end_b.heat_no:
        findings.append(_finding("HT-HEAT-MISSING", weld_no=weld.weld_no))
    if weld.end_a.nominal_diameter_mm != weld.end_b.nominal_diameter_mm:
        findings.append(_finding(
            "HT-HEAT-DIAMETER-MISMATCH", weld_no=weld.weld_no,
            evidence={"diameter_a": weld.end_a.nominal_diameter_mm,
                      "diameter_b": weld.end_b.nominal_diameter_mm},
        ))

    # 施焊时点资格
    groups = weld_groups(weld)
    thicknesses = weld_thicknesses(weld)
    diameters = weld_diameters(weld)
    wps = wps_index.get(weld.wps_no)
    for code in match_wps(
        wps, at=weld.welded_at, groups=groups,
        thicknesses=thicknesses, process=weld.weld_process,
    ):
        findings.append(_finding(
            code, weld_no=weld.weld_no,
            evidence={"at": weld.welded_at.isoformat(), "wps_no": weld.wps_no,
                      "scope": "production_wps"},
        ))
    welder = welder_index.get(weld.welder_id)
    for code in match_welder(
        welder, at=weld.welded_at, groups=groups, thicknesses=thicknesses,
        diameters=diameters, process=weld.weld_process,
        position=weld.weld_position,
    ):
        findings.append(_finding(
            code, weld_no=weld.weld_no,
            evidence={"at": weld.welded_at.isoformat(),
                      "welder_id": weld.welder_id, "scope": "production_welder"},
        ))

    ndes = [r for r in payload.nde if r.weld_no == weld.weld_no]
    repairs = [r for r in payload.repairs if r.weld_no == weld.weld_no]

    # 注意：NDE-NONE 由 evaluate() 在检验批核算后按批规则补发，
    # 抽样批中未抽中的焊口不应被判为"无检测"。

    chain_findings, links, _chain_closed = _evaluate_repair_chain(
        weld, ndes, repairs, wps_index, welder_index, nde_valid,
        repair_consumable_invalid, repair_consumable_codes,
        repair_consumable_links,
        repair_pass_invalid, repair_pass_codes, repair_pass_links,
    )
    findings.extend(chain_findings)

    return findings, links, ndes


# ================================================================ 引擎入口


def evaluate(payload: SubmissionPayload) -> dict:
    wps_index = {w.wps_no: w for w in payload.wps}
    welder_index = {w.welder_id: w for w in payload.welders}
    weld_index = {w.weld_no: w for w in payload.welds}
    cert_index = {c.cert_no: c for c in payload.nde_personnel}
    equip_index = {(e.equipment_id, e.version): e for e in payload.nde_equipment}

    # ---- 0) 无损检测资源核验：按整个实施时段检查人员证书与设备版本 ----
    nde_resources: dict[str, dict] = {}
    nde_valid: dict[str, bool] = {}
    resource_findings: dict[str, list[dict]] = defaultdict(list)
    for rec in payload.nde:
        weld = weld_index.get(rec.weld_no)
        product = weld.product if weld else "pressure_pipe"
        verdict = verify_nde_resource(
            rec, product=product,
            cert_index=cert_index, equip_index=equip_index,
        )
        nde_resources[rec.nde_id] = verdict
        nde_valid[rec.nde_id] = verdict["resource_valid"]
        if not verdict["resource_valid"]:
            for fail in verdict["failures"]:
                resource_findings[rec.weld_no].append(_finding(
                    fail["code"], weld_no=rec.weld_no, nde_id=rec.nde_id,
                    report_no=rec.report_no,
                    evidence={
                        "role": fail.get("role"),
                        "target": fail.get("target"),
                        "reason": fail.get("reason"),
                        "period": verdict["period"],
                        **{k: v for k, v in fail.items()
                           if k not in ("code", "role", "target", "reason")},
                    },
                ))
            # 显式剔除条款：该报告不得计入抽检、扩检和返修复检
            resource_findings[rec.weld_no].append(_finding(
                "NR-RECORD-EXCLUDED", weld_no=rec.weld_no, nde_id=rec.nde_id,
                report_no=rec.report_no,
                evidence={
                    "report_no": rec.report_no,
                    "period": verdict["period"],
                    "reasons": sorted({f["code"] for f in verdict["failures"]}),
                    "examiner_cert_no": rec.examiner_cert_no,
                    "reviewer_cert_no": rec.reviewer_cert_no,
                    "equipment": [
                        {"equipment_id": e["equipment_id"], "version": e["version"],
                         "role": e["role"]} for e in verdict["equipment"]
                    ],
                },
            ))

    weld_results: dict[str, dict] = {}

    # ---- 0b) 焊材批次与烘干/保温/领用链核验（链启用时整包逐耗判定）----
    wm = verify_consumables(payload)
    wm_findings_by_weld: dict[str, list[dict]] = defaultdict(list)
    wm_chain_findings: list[dict] = []
    repair_consumable_invalid: dict[str, bool] = {}
    repair_consumable_codes: dict[str, list[str]] = defaultdict(list)
    repair_consumable_links: dict[str, list[dict]] = defaultdict(list)
    production_use_views: dict[str, list[dict]] = defaultdict(list)
    if wm["enabled"]:
        def _use_view(state: dict) -> dict:
            """逐耗扁平视图：生产消耗、返修消耗与版本差异共用同一数据结构。"""
            return {
                "use_id": state["use_id"],
                "scope": state["scope"],
                "weld_no": state["weld_no"],
                "repair_id": state["repair_id"],
                "iteration": state["iteration"],
                "batch_id": state["batch_id"],
                "segment_id": state["segment_id"],
                "qty_kg": state["qty_kg"],
                "used_at": state["used_at"],
                "consumable_valid": state["consumable_valid"],
                "reasons": list(state["reasons"]),
                "exposure_minutes": state.get("exposure_minutes"),
                "exposure_limit_minutes": state.get("exposure_limit_minutes"),
                "chain": state["chain"],
            }

        use_views = {uid: _use_view(s)
                     for uid, s in wm["use_states"].items()}
        for uid, view in use_views.items():
            if view["scope"] == "production":
                production_use_views[view["weld_no"]].append(view)

        # 消耗级失败：直接挂到归属焊口（返修消耗同时标 repair_id）
        for fail in wm["failures"]:
            finding = _wm_finding(fail)
            if fail.get("scope"):
                wm_findings_by_weld[fail["weld_no"]].append(finding)
            else:
                wm_chain_findings.append(finding)

        # 实体级（批次/烘干/暂存/领用段）失败已在 verify_consumables 内
        # 按实际归属传播到每耗 reasons（段级数量问题只影响该 segment_id 下
        # 的消耗，不会错指其它段或连带无关焊口）。
        for state in wm["use_states"].values():
            weld_no = state["weld_no"]
            for code in state["reasons"]:
                finding = _finding(
                    code, weld_no=weld_no,
                    repair_id=state["repair_id"],
                    batch_id=state["batch_id"],
                    segment_id=state["segment_id"],
                    use_id=state["use_id"],
                    evidence={"scope": state["scope"],
                              "use_id": state["use_id"],
                              "batch_id": state["batch_id"],
                              "segment_id": state["segment_id"],
                              "exposure_minutes": state.get("exposure_minutes"),
                              "exposure_limit_minutes":
                                  state.get("exposure_limit_minutes")},
                )
                wm_findings_by_weld[weld_no].append(finding)
            if state["scope"] == "repair":
                repair_consumable_links[state["repair_id"]].append(
                    use_views[state["use_id"]])
                if not state["consumable_valid"]:
                    repair_consumable_invalid[state["repair_id"]] = True
                    repair_consumable_codes[state["repair_id"]].extend(
                        state["reasons"])

        # 返修未挂任何实际消耗：失效焊材不得用于返修闭合
        for rep in payload.repairs:
            if not rep.consumables:
                repair_consumable_invalid[rep.repair_id] = True
                repair_consumable_codes[rep.repair_id].append(
                    "WM-CONSUMABLE-UNTRACED")

    # ---- 0c) 焊接道次执行链核验（WPS 方法窗口 + 逐道次参数/测温/仪表）----
    wp = verify_weld_passes(payload)
    wp_findings_by_weld: dict[str, list[dict]] = defaultdict(list)
    repair_pass_invalid: dict[str, bool] = {}
    repair_pass_codes: dict[str, list[str]] = defaultdict(list)
    repair_pass_links: dict[str, list[dict]] = defaultdict(list)
    production_pass_views: dict[str, list[dict]] = {}
    production_pass_valid: dict[str, bool] = {}
    if wp["enabled"]:
        for state in wp["scope_states"].values():
            if state["scope"] == "production":
                production_pass_views[state["weld_no"]] = state["passes"]
                production_pass_valid[state["weld_no"]] = state["passes_valid"]
                owner = state["weld_no"]
            else:
                repair_pass_links[state["repair_id"]] = state["passes"]
                owner = state["weld_no"]
                if not state["passes_valid"]:
                    repair_pass_invalid[state["repair_id"]] = True
                    repair_pass_codes[state["repair_id"]].extend(
                        state["reasons"])
            # 范围级条款（含 WP-PASS-UNTRACED）挂到归属焊口；返修范围的
            # 阻断性 WP-REPAIR-PASS-INVALID 由返修链统一补发
            for f in state["findings"]:
                wp_findings_by_weld[owner].append({**f, "weld_no": owner})

    def _dedup_findings(items: list[dict]) -> list[dict]:
        seen: set[tuple] = set()
        out: list[dict] = []
        for f in items:
            key = (f["code"], f.get("nde_id"), f.get("repair_id"),
                   f.get("use_id"), f.get("pass_id"), f.get("measure_id"),
                   f.get("batch_id"), f.get("segment_id"),
                   json_dumps_evidence(f.get("evidence")))
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out

    for weld in payload.welds:
        findings, links, ndes = _evaluate_weld(
            weld, payload, wps_index, welder_index, nde_valid,
            repair_consumable_invalid,
            {k: sorted(set(v)) for k, v in repair_consumable_codes.items()},
            dict(repair_consumable_links),
            repair_pass_invalid,
            {k: sorted(set(v)) for k, v in repair_pass_codes.items()},
            dict(repair_pass_links),
        )
        findings = resource_findings.get(weld.weld_no, []) + findings
        if wm["enabled"]:
            findings = wm_findings_by_weld.get(weld.weld_no, []) + findings
        if wp["enabled"]:
            findings = wp_findings_by_weld.get(weld.weld_no, []) + findings
        findings = _dedup_findings(findings)
        hold = any(f["severity"] == Severity.HOLD.value for f in findings)
        weld_results[weld.weld_no] = {
            "findings": findings,
            "links": links,
            "ndes": ndes,
            "hold": hold,
        }

    # 先按批规则补发 NDE-NONE：抽样批未抽中的焊口不要求逐口检测
    rules = {rule.lot_id: rule for rule in payload.lot_rules}
    for weld in payload.welds:
        r = weld_results[weld.weld_no]
        if r["ndes"]:
            continue
        if weld.lot_id is not None:
            rule = rules.get(weld.lot_id)
            if rule is not None and rule.sample_ratio < 1.0:
                continue  # 抽样批：未抽中属正常状态
        r["findings"].append(_finding("NDE-NONE", weld_no=weld.weld_no))
        r["hold"] = True

    # 再核算检验批覆盖率与扩检（open reject 判定依赖最新 hold 集；
    # 资源失效报告不计入口数，故必须传入 nde_valid）
    hold_welds = {no for no, r in weld_results.items() if r["hold"]}
    lot_per_weld, lot_summaries = _evaluate_lots(
        payload, weld_index, hold_welds, nde_valid
    )

    # 批次 hold 会连带焊口 hold，汇总最终焊口结论
    weld_verdicts: list[dict] = []
    all_findings: list[dict] = []

    def _production_use_links(weld_no: str) -> list[dict]:
        if not wm["enabled"]:
            return []
        return list(production_use_views.get(weld_no, []))

    def _production_pass_views(weld_no: str) -> list[dict]:
        if not wp["enabled"]:
            return []
        return list(production_pass_views.get(weld_no, []))

    for weld in payload.welds:
        base = weld_results[weld.weld_no]
        findings = base["findings"] + lot_per_weld.get(weld.weld_no, [])
        findings.sort(key=lambda f: (f["code"], f.get("nde_id") or "",
                                     f.get("repair_id") or ""))
        hold = any(f["severity"] == Severity.HOLD.value for f in findings)
        all_findings.extend(findings)
        weld_verdicts.append({
            "weld_no": weld.weld_no,
            "line_no": weld.line_no,
            "lot_id": weld.lot_id,
            "product": weld.product,
            "welded_at": weld.welded_at,
            "material_groups": [weld.end_a.material_group,
                                weld.end_b.material_group],
            "heat_nos": [weld.end_a.heat_no, weld.end_b.heat_no],
            "heats": [
                {"side": "a", **weld.end_a.model_dump()},
                {"side": "b", **weld.end_b.model_dump()},
            ],
            "thickness_mm": weld_thicknesses(weld),
            "nde_count": len(base["ndes"]),
            "nde_valid_count": sum(
                1 for r in base["ndes"] if nde_valid.get(r.nde_id, True)
            ),
            "nde_excluded_count": sum(
                1 for r in base["ndes"] if not nde_valid.get(r.nde_id, True)
            ),
            "nde": [{
                "nde_id": r.nde_id,
                "method": r.method,
                "result": r.result,
                "iteration": r.iteration,
                "examined_at": r.examined_at,
                "started_at": r.period_start,
                "finished_at": r.period_end,
                "examiner": r.examiner,
                "lot_id": r.lot_id,
                "report_no": r.report_no,
                "technique": r.technique,
                "applied_thickness_mm": r.applied_thickness_mm,
                "exposure_energy_kev": r.exposure_energy_kev,
                "resource_valid": nde_valid.get(r.nde_id, True),
                "resource": nde_resources.get(r.nde_id),
                "coverage": [geo.band_dict(b) for b in r.coverage],
                "defect_locations": [geo.band_dict(b) for b in r.defect_locations],
            } for r in sorted(base["ndes"],
                              key=lambda r: (r.iteration, r.examined_at))],
            "repairs": base["links"],
            "consumable_uses": _production_use_links(weld.weld_no),
            "weld_passes": _production_pass_views(weld.weld_no),
            "passes_valid": (production_pass_valid.get(weld.weld_no)
                             if wp["enabled"] else None),
            "decision": "hold" if hold else "release",
            "findings": findings,
        })

    if wm["enabled"]:
        all_findings.extend(wm_chain_findings)
    all_findings.sort(key=lambda f: (
        f.get("weld_no") or "~", f.get("lot_id") or "~",
        f["code"], f.get("nde_id") or "", f.get("repair_id") or "",
        f.get("use_id") or "", f.get("pass_id") or "",
        f.get("measure_id") or "", f.get("batch_id") or "",
        f.get("segment_id") or "",
    ))

    total = len(payload.welds)
    held = sum(1 for v in weld_verdicts if v["decision"] == "hold")
    codes = sorted({f["code"] for f in all_findings})
    return {
        "decision": "hold" if held else "release",
        "stats": {
            "weld_total": total,
            "weld_release": total - held,
            "weld_hold": held,
            "lot_total": len(lot_summaries),
            "lot_hold": sum(1 for s in lot_summaries
                            if s["decision"] == "hold"),
            "findings_total": len(all_findings),
            "findings_hold": sum(
                1 for f in all_findings
                if f["severity"] == Severity.HOLD.value
            ),
            "findings_warning": sum(
                1 for f in all_findings
                if f["severity"] == Severity.WARNING.value
            ),
            "repairs_total": len(payload.repairs),
            "nde_reports_total": len(payload.nde),
            "nde_reports_excluded": sum(
                1 for no, ok in nde_valid.items() if not ok
            ),
            "nde_personnel_total": len(payload.nde_personnel),
            "nde_equipment_versions_total": len(payload.nde_equipment),
            "consumable_chain_enabled": wm["enabled"],
            "consumable_batches_total": len(payload.consumable_batches),
            "consumable_uses_total": len(wm["use_states"]) if wm["enabled"] else 0,
            "consumable_uses_invalid": sum(
                1 for s in wm["use_states"].values()
                if not s["consumable_valid"]) if wm["enabled"] else 0,
            "weld_pass_chain_enabled": wp["enabled"],
            "weld_gauges_total": len(payload.weld_gauges),
            "weld_passes_total": len(payload.weld_passes),
            "temperature_measurements_total": len(payload.temperature_measurements),
            "weld_pass_scopes_invalid": sum(
                1 for st in wp["scope_states"].values()
                if not st["passes_valid"]) if wp["enabled"] else 0,
        },
        "nde_resources": {
            "personnel": [
                {
                    "cert_no": c.cert_no, "name": c.name, "method": c.method,
                    "level": c.level, "products": list(c.products),
                    "techniques": list(c.techniques),
                    "valid_from": c.valid_from.isoformat(),
                    "valid_to": c.valid_to.isoformat(),
                } for c in payload.nde_personnel
            ],
            "equipment": [
                {
                    "equipment_id": e.equipment_id, "version": e.version,
                    "name": e.name, "kind": e.kind, "method": e.method,
                    "serial_no": e.serial_no,
                    "calibrated_from": e.calibrated_from.isoformat(),
                    "calibrated_to": e.calibrated_to.isoformat(),
                    "techniques": list(e.techniques),
                    "range_min_mm": e.range_min_mm,
                    "range_max_mm": e.range_max_mm,
                    "energy_min_kev": e.energy_min_kev,
                    "energy_max_kev": e.energy_max_kev,
                    "source_isotope": e.source_isotope,
                    "uses": [u.model_dump(mode="json") for u in e.uses],
                } for e in payload.nde_equipment
            ],
            "invalid_reports": [
                {
                    "nde_id": rec.nde_id,
                    "report_no": rec.report_no,
                    "weld_no": rec.weld_no,
                    "method": rec.method,
                    "iteration": rec.iteration,
                    "period": nde_resources[rec.nde_id]["period"],
                    "reasons": sorted({
                        f["code"]
                        for f in nde_resources[rec.nde_id]["failures"]
                    }),
                }
                for rec in payload.nde if not nde_valid.get(rec.nde_id, True)
            ],
        },
        "lot_summaries": lot_summaries,
        "welds": weld_verdicts,
        "consumables": _consumables_block(payload, wm),
        "weld_execution": _weld_execution_block(payload, wp),
        "findings": all_findings,
        "clauses_triggered": codes,
        "evaluated_at": datetime.now(timezone.utc),
    }


def _consumables_block(payload: SubmissionPayload, wm: dict) -> dict:
    """焊材事件链目录与规则摘要（随审查包冻结）。"""
    if not wm.get("enabled"):
        return {"enabled": False}
    idx = wm["indexes"]
    invalid_uses = [
        {
            "use_id": s["use_id"],
            "scope": s["scope"],
            "weld_no": s["weld_no"],
            "repair_id": s["repair_id"],
            "iteration": s["iteration"],
            "batch_id": s["batch_id"],
            "segment_id": s["segment_id"],
            "used_at": s["used_at"],
            "qty_kg": s["qty_kg"],
            "exposure_minutes": s.get("exposure_minutes"),
            "exposure_limit_minutes": s.get("exposure_limit_minutes"),
            "reasons": s["reasons"],
        }
        for s in sorted(wm["use_states"].values(), key=lambda s: s["use_id"])
        if not s["consumable_valid"]
    ]
    return {
        "enabled": True,
        "completeness": wm.get("completeness", {"gaps": [],
                                               "affected_welds": []}),
        "freeze_blocked": wm.get("freeze_blocked", False),
        "batches": [b.model_dump(mode="json") for b in payload.consumable_batches],
        "rules": [r.model_dump(mode="json") for r in payload.consumable_rules],
        "containers": [c.model_dump(mode="json")
                       for c in payload.consumable_containers],
        "bake_cycles": [b.model_dump(mode="json") for b in payload.bake_cycles],
        "quiver_stays": [s.model_dump(mode="json") for s in payload.quiver_stays],
        "segments": [s.model_dump(mode="json")
                     for s in payload.consumable_segments],
        "events": [e.model_dump(mode="json")
                   for e in payload.consumable_events],
        "invalid_uses": invalid_uses,
        "chain_findings": [
            {
                "code": f["code"],
                "severity": f["severity"],
                "category": f["category"],
                "reference": f["reference"],
                "message": f["message"],
                "batch_id": f["ids"].get("batch_id"),
                "segment_id": f["ids"].get("segment_id"),
                "bake_id": f["ids"].get("bake_id"),
                "stay_id": f["ids"].get("stay_id"),
                "evidence": f.get("evidence") or {},
            }
            for f in wm.get("chain_failures", [])
        ],
    }

def _weld_execution_block(payload: SubmissionPayload, wp: dict) -> dict:
    """焊接道次执行链目录与逐范围核验状态（随审查包冻结）。"""
    if not wp.get("enabled"):
        return {"enabled": False}
    invalid_scopes = [
        {
            "scope_key": st["scope_key"],
            "weld_no": st["weld_no"],
            "scope": st["scope"],
            "repair_id": st["repair_id"],
            "iteration": st["iteration"],
            "wps_no": st["wps_no"],
            "pass_total": st["pass_total"],
            "reasons": st["reasons"],
            "invalid_passes": [
                {
                    "pass_id": pv["pass_id"],
                    "pass_no": pv["pass_no"],
                    "layer_no": pv["layer_no"],
                    "role": pv["role"],
                    "process": pv["process"],
                    "welder_id": pv["welder_id"],
                    "started_at": pv["started_at"],
                    "finished_at": pv["finished_at"],
                    "current_a": pv["current_a"],
                    "voltage_v": pv["voltage_v"],
                    "travel_speed_mm_min": pv["travel_speed_mm_min"],
                    "heat_input_kj_mm": pv["heat_input_kj_mm"],
                    "governing_temperature": pv["governing_temperature"],
                    "reasons": pv["reasons"],
                }
                for pv in st["passes"] if not pv["pass_valid"]
            ],
            "invalid_measurements": [
                m for m in st.get("measurements", [])
                if not m.get("gauge_valid", True)
            ],
        }
        for st in sorted(wp["scope_states"].values(),
                         key=lambda x: (x["weld_no"], x["scope"],
                                        x["repair_id"] or ""))
        if not st["passes_valid"]
    ]
    return {
        "enabled": True,
        "completeness": {"gaps": wp.get("gaps", []),
                         "affected_welds": wp.get("affected_welds", [])},
        "freeze_blocked": wp.get("freeze_blocked", False),
        "wps_windows": [
            {
                "wps_no": w.wps_no,
                "process_windows": [
                    win.model_dump(mode="json") for win in w.process_windows
                ],
            }
            for w in payload.wps if w.process_windows
        ],
        "gauges": [g.model_dump(mode="json") for g in payload.weld_gauges],
        "passes": [p.model_dump(mode="json") for p in payload.weld_passes],
        "measurements": [m.model_dump(mode="json")
                         for m in payload.temperature_measurements],
        "invalid_scopes": invalid_scopes,
    }
