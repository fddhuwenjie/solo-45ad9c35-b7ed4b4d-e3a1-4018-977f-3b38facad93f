"""标色 SVG 焊口图：把焊口展开为周向圆环，沿角度位置叠加

- 外圈：原始 RT/UT/PT 底片覆盖（绿=合格，红=不合格）；
- 红条：不合格显示的缺陷位置；
- 橙/紫条：一次/二次挖补返修区间；
- 蓝/红条：返修后复检覆盖（蓝=合格，红=仍不合格）；
- 内环粗描边：焊口放行结论（绿=release，红=hold）。

0° 为管顶（12 点位），顺时针为正。
"""
from __future__ import annotations

import math
from xml.sax.saxutils import escape

from . import geometry as geo

CX, CY = 380.0, 390.0

C_RELEASE = "#2e7d32"
C_HOLD = "#c62828"
C_WARN = "#f9a825"
C_DEFECT = "#b71c1c"
C_REPAIR1 = "#ef6c00"
C_REPAIR2 = "#6a1b9a"
C_REINSPECT_OK = "#1565c0"
C_REINSPECT_NG = "#c62828"
C_INIT_OK = "#66bb6a"
C_INIT_NG = "#ef5350"
C_GRID = "#90a4ae"


def _xy(r: float, deg: float) -> tuple[float, float]:
    a = math.radians(geo.norm(deg))
    return CX + r * math.sin(a), CY - r * math.cos(a)


def _sector(r_in: float, r_out: float, a1: float, a2: float) -> str:
    """顺时针环扇形 path（a1、a2 为归一化角度，a2>a1 且跨度<=360）。"""
    sweep = a2 - a1
    if sweep >= 360.0 - 1e-9:
        # 整环：两个半圆 path
        half = _sector(r_in, r_out, 0.0, 180.0) + _sector(r_in, r_out, 180.0, 360.0)
        return half
    large = 1 if sweep > 180 else 0
    x1, y1 = _xy(r_out, a1)
    x2, y2 = _xy(r_out, a2)
    x3, y3 = _xy(r_in, a2)
    x4, y4 = _xy(r_in, a1)
    return (
        f"M{x1:.2f},{y1:.2f} "
        f"A{r_out},{r_out} 0 {large} 1 {x2:.2f},{y2:.2f} "
        f"L{x3:.2f},{y3:.2f} "
        f"A{r_in},{r_in} 0 {large} 0 {x4:.2f},{y4:.2f} Z"
    )


def _band_path(r_in: float, r_out: float, band: dict) -> list[str]:
    if band.get("full_circle"):
        return [_sector(r_in, r_out, 0.0, 360.0)]
    start = geo.norm(band["start_deg"])
    end = geo.norm(band["end_deg"])
    if start == end:
        # 归一化后零长度（如 0~360 的全周片经序列化）按整圈绘制
        return [_sector(r_in, r_out, 0.0, 360.0)]
    obj = geo.AngleBand(
        start_deg=start, end_deg=end, full_circle=False
    )
    return [_sector(r_in, r_out, s, e) for s, e in geo.band_to_arcs(obj)]


def _wrap(text: str, width: int) -> list[str]:
    """按显示宽度粗略折行（CJK 字符计 1，ASCII 计 0.55）。"""
    lines: list[str] = []
    cur, w = "", 0.0
    for ch in text:
        cw = 0.55 if ord(ch) < 128 else 1.0
        if w + cw > width and cur:
            lines.append(cur)
            cur, w = ch, cw
        else:
            cur += ch
            w += cw
    if cur:
        lines.append(cur)
    return lines


def render_weld_svg(verdict: dict) -> str:
    """依据单焊口审查结论生成独立 SVG 文档字符串。"""
    parts: list[str] = []
    decision = verdict["decision"]
    ring = C_RELEASE if decision == "release" else C_HOLD

    parts.append(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 760" '
        'font-family="sans-serif">'
    )
    parts.append(
        '<rect width="1000" height="760" fill="#fafafa" stroke="#cfd8dc"/>'
    )

    # ---- 标题 ----
    title = f"焊口 {verdict['weld_no']} · 管线 {verdict['line_no']}"
    parts.append(
        f'<text x="32" y="44" font-size="26" font-weight="bold" fill="#263238">'
        f"{escape(title)}</text>"
    )
    badge = "放行 RELEASE" if decision == "release" else "保持 HOLD"
    parts.append(
        f'<rect x="720" y="22" width="248" height="38" rx="6" '
        f'fill="{ring}"/><text x="844" y="47" font-size="20" font-weight="bold" '
        f'fill="#ffffff" text-anchor="middle">{escape(badge)}</text>'
    )

    # ---- 底环 / 刻度 ----
    for r in (300, 172):
        parts.append(
            f'<circle cx="{CX}" cy="{CY}" r="{r}" fill="none" '
            f'stroke="#eceff1" stroke-width="1"/>'
        )
    for deg, label in ((0, "0°/12点"), (90, "90°"), (180, "180°"), (270, "270°")):
        x1, y1 = _xy(306, deg)
        x2, y2 = _xy(314, deg)
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{C_GRID}" stroke-width="1.5"/>'
        )
        lx, ly = _xy(330, deg)
        parts.append(
            f'<text x="{lx:.1f}" y="{ly + 4:.1f}" font-size="12" '
            f'fill="#546e7a" text-anchor="middle">{escape(label)}</text>'
        )

    # ---- 原始检测（iteration 0）覆盖，车道 300→266 ----
    drawn: set[str] = set()
    for rec in verdict.get("nde", []):
        if rec["iteration"] != 0:
            continue
        color = C_INIT_OK if rec["result"] == "accept" else C_INIT_NG
        for b in rec["coverage"]:
            for d in _band_path(266, 300, b):
                parts.append(
                    f'<path d="{d}" fill="{color}" fill-opacity="0.55" '
                    f'stroke="#37474f" stroke-width="0.6"/>'
                )
        for b in rec["defect_locations"]:
            for d in _band_path(258, 266, b):
                parts.append(
                    f'<path d="{d}" fill="{C_DEFECT}"/>'
                )
        drawn.add(rec["nde_id"])

    if not verdict.get("nde"):
        parts.append(
            f'<text x="{CX}" y="{CY - 4}" font-size="16" fill="{C_HOLD}" '
            f'text-anchor="middle">无任何 RT/UT/PT 记录</text>'
        )

    # ---- 返修与复检车道 ----
    lane_outer = {1: (248, 232), 2: (208, 192)}
    lane_re = {1: (228, 212), 2: (188, 172)}
    for link in verdict.get("repairs", []):
        it = link["iteration"]
        ro, ri = lane_outer.get(it, (248, 232))
        rc = C_REPAIR1 if it == 1 else C_REPAIR2
        for d in _band_path(ri, ro, link["excavated_band"]):
            parts.append(
                f'<path d="{d}" fill="{rc}" fill-opacity="0.85" '
                f'stroke="#263238" stroke-width="0.6"/>'
            )
        eo, ei = lane_re.get(it, (228, 212))
        for rn in link["reinspections"]:
            color = C_REINSPECT_OK if rn["result"] == "accept" else C_REINSPECT_NG
            for b in rn["coverage"]:
                for d in _band_path(ei, eo, b):
                    parts.append(
                        f'<path d="{d}" fill="{color}" fill-opacity="0.6" '
                        f'stroke="#0d47a1" stroke-width="0.5"/>'
                    )

    # ---- 结论内环 ----
    parts.append(
        f'<circle cx="{CX}" cy="{CY}" r="160" fill="#ffffff" '
        f'stroke="{ring}" stroke-width="10"/>'
    )
    heats = " / ".join(verdict.get("heat_nos", []))
    groups = " / ".join(verdict.get("material_groups", []))
    thick = " / ".join(f"{t:g}" for t in verdict.get("thickness_mm", []))
    inner_lines = [
        f"炉批: {heats}",
        f"组别: {groups}",
        f"厚度mm: {thick}",
        f"检验批: {verdict.get('lot_id') or '—'}",
        f"施焊: {verdict['welded_at'].strftime('%Y-%m-%d %H:%M') if hasattr(verdict['welded_at'], 'strftime') else str(verdict['welded_at'])[:16]}",
        f"返修: {len(verdict.get('repairs', []))} 次",
    ]
    for i, line in enumerate(inner_lines):
        parts.append(
            f'<text x="{CX}" y="{CY - 52 + i * 24}" font-size="15" '
            f'fill="#37474f" text-anchor="middle">{escape(line)}</text>'
        )

    # ---- 右侧条款面板 ----
    parts.append(
        '<rect x="712" y="80" width="264" height="540" rx="8" '
        'fill="#ffffff" stroke="#cfd8dc"/>'
    )
    parts.append(
        '<text x="728" y="108" font-size="16" font-weight="bold" fill="#263238">'
        "触发条款</text>"
    )
    findings = verdict.get("findings", [])
    y = 132
    if not findings:
        parts.append(
            f'<text x="728" y="{y}" font-size="14" fill="{C_RELEASE}">'
            "无：资格/覆盖/返修链均成立</text>"
        )
    for f in findings[:22]:
        color = C_HOLD if f["severity"] == "hold" else C_WARN
        head = f"[{f['code']}]"
        parts.append(
            f'<circle cx="733" cy="{y - 5}" r="3.5" fill="{color}"/>'
            f'<text x="744" y="{y}" font-size="12.5" font-weight="bold" '
            f'fill="{color}">{escape(head)}</text>'
        )
        y += 16
        for line in _wrap(f["message"], 33)[:3]:
            parts.append(
                f'<text x="744" y="{y}" font-size="11.5" fill="#455a64">'
                f"{escape(line)}</text>"
            )
            y += 15
        y += 5
        if y > 600:
            parts.append(
                f'<text x="728" y="612" font-size="11" fill="#78909c">'
                f"余 {len(findings) - 22} 条见 JSON 审查包</text>"
            )
            break

    # ---- 图例 ----
    legend = [
        (C_INIT_OK, "原始检测合格覆盖"),
        (C_INIT_NG, "原始检测（不合格底片）"),
        (C_DEFECT, "缺陷显示位置"),
        (C_REPAIR1, "一次挖补返修区"),
        (C_REPAIR2, "二次挖补返修区"),
        (C_REINSPECT_OK, "复检覆盖（合格）"),
        (C_REINSPECT_NG, "复检覆盖（仍不合格）"),
    ]
    lx, ly = 32, 690
    for i, (color, label) in enumerate(legend):
        col = i % 4
        row = i // 4
        x = lx + col * 240
        y = ly + row * 24
        parts.append(
            f'<rect x="{x}" y="{y - 12}" width="16" height="12" '
            f'fill="{color}" fill-opacity="0.8"/>'
            f'<text x="{x + 22}" y="{y - 2}" font-size="13" '
            f'fill="#37474f">{escape(label)}</text>'
        )
    parts.append(
        f'<text x="32" y="752" font-size="12" fill="#78909c">'
        "角度自管顶 0° 顺时针；旧底片仅画在外圈，不充当返修复检依据</text>"
    )
    parts.append("</svg>")
    return "".join(parts)
