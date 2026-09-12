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
    direct_release_payload,
    double_repair_payload,
    extension_payload,
)

SCENARIOS = [
    ("1-direct-release", "直接放行", direct_release_payload),
    ("2-defect-extension", "缺陷扩检（加倍合格）", extension_payload),
    ("2b-extension-short", "缺陷扩检数量不足（hold 对照）",
     lambda: extension_payload(enough_extension=False)),
    ("3-double-repair", "二次返修闭合放行", double_repair_payload),
    ("3b-repair-uncovered", "二次复检未覆盖挖补区（hold 对照）",
     lambda: double_repair_payload(second_covered=False)),
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
