"""按施焊时点匹配 WPS/PQR 与焊工资格的认可范围。"""
from __future__ import annotations

from datetime import datetime

from .schemas import HeatEnd, QualRange, WelderQual, WeldRecord, WpsRecord


def _group_ok(qual: QualRange, groups: list[str]) -> bool:
    return all(g in qual.material_groups for g in groups)


def _thickness_ok(qual: QualRange, thicknesses: list[float]) -> bool:
    # 异厚对接：两侧厚度均须落入认可厚度范围
    return all(qual.thickness_min_mm <= t <= qual.thickness_max_mm for t in thicknesses)


def _diameter_ok(qual: QualRange, diameter: float) -> bool:
    if qual.diameter_min_mm is not None and diameter < qual.diameter_min_mm:
        return False
    if qual.diameter_max_mm is not None and diameter > qual.diameter_max_mm:
        return False
    return True


def _process_ok(qual: QualRange, process: str) -> bool:
    if not qual.processes:
        return True  # 未登记认可方法时不做该项限制（其余条款仍会判定）
    parts = {p.strip().upper() for p in process.replace("+", "/").split("/") if p.strip()}
    allowed = {p.strip().upper() for p in qual.processes}
    return parts <= allowed


def _position_ok(qual: QualRange, position: str) -> bool:
    if not qual.positions:
        return True
    return position.strip().upper() in {p.strip().upper() for p in qual.positions}


def match_wps(
    wps: WpsRecord | None,
    *,
    at: datetime,
    groups: list[str],
    thicknesses: list[float],
    process: str,
) -> list[str]:
    """返回违规条款码；空列表表示 WPS 在该时点有效且覆盖。"""
    codes: list[str] = []
    if wps is None:
        return ["WQ-WPS-EXPIRED"]
    if not (wps.valid_from <= at <= wps.valid_to) or not wps.supported_by_pqr:
        codes.append("WQ-WPS-EXPIRED")
    if not _group_ok(wps, groups):
        codes.append("WQ-GROUP-OUTSIDE")
    if not _thickness_ok(wps, thicknesses):
        codes.append("WQ-THICKNESS-OUTSIDE")
    if not _process_ok(wps, process):
        codes.append("WQ-PROCESS-MISMATCH")
    return codes


def match_welder(
    welder: WelderQual | None,
    *,
    at: datetime,
    groups: list[str],
    thicknesses: list[float],
    diameters: list[float],
    process: str,
    position: str,
) -> list[str]:
    """返回焊工资格违规条款码；空列表表示资格在该时点有效且覆盖。"""
    codes: list[str] = []
    if welder is None:
        return ["WQ-WELDER-EXPIRED"]
    if not (welder.valid_from <= at <= welder.valid_to):
        codes.append("WQ-WELDER-EXPIRED")
    if not _group_ok(welder, groups):
        codes.append("WQ-GROUP-OUTSIDE")
    if not _thickness_ok(welder, thicknesses):
        codes.append("WQ-THICKNESS-OUTSIDE")
    # 焊工资格对管径逐侧检查，任一侧越界即越界
    if not all(_diameter_ok(welder, d) for d in diameters):
        codes.append("WQ-DIAMETER-OUTSIDE")
    if not _process_ok(welder, process):
        codes.append("WQ-PROCESS-MISMATCH")
    if not _position_ok(welder, position):
        codes.append("WQ-PROCESS-MISMATCH")
    return codes


def weld_groups(weld: WeldRecord) -> list[str]:
    return [weld.end_a.material_group, weld.end_b.material_group]


def weld_thicknesses(weld: WeldRecord) -> list[float]:
    return [weld.end_a.thickness_mm, weld.end_b.thickness_mm]


def weld_diameters(weld: WeldRecord) -> list[float]:
    return [weld.end_a.nominal_diameter_mm, weld.end_b.nominal_diameter_mm]
