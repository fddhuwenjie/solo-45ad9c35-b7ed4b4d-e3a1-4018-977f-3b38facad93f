"""周向角度区间的归一化、覆盖与沿缺陷位置串接的几何工具。"""
from __future__ import annotations

from .schemas import AngleBand

FULL = 360.0


def norm(deg: float) -> float:
    """归一化到 [0, 360)。"""
    return deg % FULL


def band_to_arcs(band: AngleBand) -> list[tuple[float, float]]:
    """把区间拆为至多两个不跨 0 的弧段（start, end），均为 [0,360) 内左闭右开。"""
    if band.full_circle:
        return [(0.0, FULL)]
    start = norm(band.start_deg)
    end = norm(band.end_deg)
    if start == end:
        # 等角但未标记全周：长度视为整圈之外的 0 长度无意义，按一整圈处理
        return [(0.0, FULL)]
    if end > start:
        return [(start, end)]
    return [(start, FULL), (0.0, end)]  # 跨 0°


def arc_length(arcs: list[tuple[float, float]]) -> float:
    return sum(e - s for s, e in arcs)


def covered_points(bands: list[AngleBand], step_deg: float = 0.25) -> set[int]:
    """离散化被覆盖的角度点（用整数百分位刻度，避免浮点误差）。"""
    scale = int(FULL / step_deg)
    pts: set[int] = set()
    for band in bands:
        for s, e in band_to_arcs(band):
            i = int(round(s * scale / FULL))
            j = int(round(e * scale / FULL))
            if (s, e) == (0.0, FULL):
                pts.update(range(scale))
            else:
                pts.update(range(i, j))
    return pts


def covers(outer: list[AngleBand], inner: list[AngleBand], step_deg: float = 0.25) -> bool:
    """outer 的并集是否完整覆盖 inner 的并集（用于复检覆盖挖补区）。

    inner 为空视为无需覆盖，返回 True。
    """
    if not inner:
        return True
    return covered_points(inner, step_deg) <= covered_points(outer, step_deg)


def intersects(a: list[AngleBand], b: list[AngleBand], step_deg: float = 0.25) -> bool:
    """两组角度区间是否有重叠（用于沿缺陷位置把返修与旧底片对应）。"""
    if not a or not b:
        return False
    return bool(covered_points(a, step_deg) & covered_points(b, step_deg))


def band_dict(band: AngleBand) -> dict:
    """角度区间的 JSON 表示。

    标记全周或语义上恰好一圈（如 0~360）的区间统一输出 full_circle，
    避免两端分别归一化后坍缩成 (0,0) 而丢失覆盖信息。
    """
    if band.full_circle:
        return {"full_circle": True, "start_deg": 0.0, "end_deg": 360.0}
    start = norm(band.start_deg)
    end = norm(band.end_deg)
    raw_span = (band.end_deg - band.start_deg) % FULL
    if start == end and raw_span == 0.0 and band.end_deg != band.start_deg:
        # 跨度恰为整圈（0~360、360~720 等）
        return {"full_circle": True, "start_deg": 0.0, "end_deg": 360.0}
    return {
        "full_circle": False,
        "start_deg": start,
        "end_deg": end,
    }
