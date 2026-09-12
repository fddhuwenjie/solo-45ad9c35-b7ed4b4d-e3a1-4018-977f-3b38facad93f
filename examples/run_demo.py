"""本地样例演示：直接放行 / 缺陷扩检 / 二次返修。

用法（仓库根目录）：
    python examples/run_demo.py                 # 仅打印 JSON 审查包摘要
    python examples/run_demo.py --out out/demo  # 同时导出每口焊口的标色 SVG

不经过 HTTP，直接调用引擎；HTTP 流程见 README 的 curl 示例。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.engine import evaluate
from app.svg import render_weld_svg
from examples.sample_data import (
    consumable_failure_payload,
    direct_release_payload,
    double_repair_payload,
    extension_payload,
    ndt_resource_failure_payload,
    weld_pass_failure_payload,
)

SCENARIOS = [
    ("1-direct-release", "直接放行", direct_release_payload),
    ("2-defect-extension", "缺陷扩检（加倍合格）", extension_payload),
    ("2b-extension-short", "缺陷扩检数量不足（hold 对照）",
     lambda: extension_payload(enough_extension=False)),
    ("3-double-repair", "二次返修闭合放行", double_repair_payload),
    ("3b-repair-uncovered", "二次复检未覆盖挖补区（hold 对照）",
     lambda: double_repair_payload(second_covered=False)),
    ("4-ndt-resource", "夜班跨证书到期点/未校准设备（报告剔除 hold）",
     ndt_resource_failure_payload),
    ("5-weld-material", "焊材保温校准失效/超时/数量重复分配（材料链 hold）",
     consumable_failure_payload),
    ("6-weld-pass", "道次参数越限/层间高温/采样缺失/返修混用（道次链 hold）",
     weld_pass_failure_payload),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="焊口合规放行本地样例")
    parser.add_argument("--out", default=None, help="SVG 输出目录")
    args = parser.parse_args()
    out = Path(args.out) if args.out else None

    for key, title, factory in SCENARIOS:
        payload = factory()
        result = evaluate(payload)
        print("=" * 72)
        print(f"{key}  {title}")
        print(f"  整包结论: {result['decision']}  "
              f"放行 {result['stats']['weld_release']}/"
              f"hold {result['stats']['weld_hold']} "
              f"（共 {result['stats']['weld_total']} 口）")
        for lot in result["lot_summaries"]:
            print(f"  检验批 {lot['lot_id']}: 初始 "
                  f"{lot['examined_initial']}/{lot['required_initial']}"
                  f" -> 最终 {lot['examined_final']}/{lot['required_final']}"
                  f"  扩检={lot['extended']}"
                  f"({lot['extension_mode'] or '-'})  "
                  f"{lot['decision']}")
        codes = result["clauses_triggered"]
        print("  触发条款:", "、".join(codes) if codes else "无")
        invalid = result["nde_resources"]["invalid_reports"]
        if invalid:
            print("  剔除报告（不得计入抽检/扩检/复检）:")
            for item in invalid:
                print(f"    {item['nde_id']} ({item['report_no']}) "
                      f"焊口 {item['weld_no']}  时段 {item['period']['started_at']} "
                      f"~ {item['period']['finished_at']}  原因: "
                      f"{'、'.join(item['reasons'])}")
        cons = result.get("consumables") or {}
        if cons.get("enabled"):
            print(f"  焊材链: 批次 {len(cons['batches'])} 烘干 "
                  f"{len(cons['bake_cycles'])} 保温 {len(cons['quiver_stays'])} "
                  f"领用段 {len(cons['segments'])} 消耗 "
                  f"{result['stats']['consumable_uses_total']} "
                  f"失效 {result['stats']['consumable_uses_invalid']}"
                  f"{('  [冻结阻断:' + ','.join(sorted({g['kind'] for g in cons['completeness']['gaps']})) + ']') if cons.get('freeze_blocked') else ''}")
            for item in cons["invalid_uses"]:
                print(f"    失效消耗 {item['use_id']} 焊口 {item['weld_no']}"
                      f"{('/返修' + item['repair_id']) if item['repair_id'] else ''} "
                      f"批次 {item['batch_id']} 段 {item['segment_id']} "
                      f"暴露 {item['exposure_minutes']}min 原因: "
                      f"{'、'.join(item['reasons'])}")
        we = result.get("weld_execution") or {}
        if we.get("enabled"):
            print(f"  道次链: 仪表 {len(we['gauges'])} 道次 "
                  f"{result['stats']['weld_passes_total']} 测温 "
                  f"{result['stats']['temperature_measurements_total']} "
                  f"失效范围 {result['stats']['weld_pass_scopes_invalid']}"
                  f"{('  [冻结阻断]' if we.get('freeze_blocked') else '')}")
            for item in we["invalid_scopes"]:
                loc = (f"返修{item['repair_id']}" if item["scope"] == "repair"
                       else item["weld_no"])
                bad = ",".join(sorted({
                    q["pass_id"] for q in item["invalid_passes"]}))
                print(f"    失效范围 {loc}（{item['weld_no']}）道次 {bad or '-'} "
                      f"原因: {'、'.join(item['reasons'])}")
        for w in result["welds"]:
            if w["decision"] == "hold":
                detail = sorted({f["code"] for f in w["findings"]})
                print(f"    [HOLD] {w['weld_no']}: {' '.join(detail)}")
            for link in w["repairs"]:
                print(f"    {w['weld_no']} 第{link['iteration']}次返修 "
                      f"{link['repair_id']} closed={link['closed']} "
                      f"复检{len(link['reinspections'])}张")

        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            pack_path = out / f"{key}.json"
            pack_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8")
            for w in result["welds"]:
                svg_path = out / f"{key}--{w['weld_no']}.svg"
                svg_path.write_text(render_weld_svg(w), encoding="utf-8")
            print(f"  已写出: {pack_path} 及 {len(result['welds'])} 张 SVG")

    print("=" * 72)
    print("完成。")


if __name__ == "__main__":
    main()
