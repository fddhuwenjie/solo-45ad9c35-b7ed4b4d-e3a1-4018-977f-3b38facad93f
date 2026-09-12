"""样例数据构造工厂：直接放行 / 缺陷扩检 / 二次返修 三套本地样例。

时间轴约定（UTC）：
- 资质窗口覆盖 2026 全年；
- 施焊 2026-03 上旬；
- 原始 RT 紧随其后；返修与复检按周递进。
"""
from __future__ import annotations

from app.schemas import (
    AngleBand,
    HeatEnd,
    LotRule,
    NdeRecord,
    RepairRecord,
    SubmissionPayload,
    WelderQual,
    WeldRecord,
    WpsRecord,
)

GROUP = "Fe-1"


def wps(no: str = "WPS-101", groups=("Fe-1",), tmin=3.0, tmax=20.0,
        dmin=25.0, dmax=600.0, processes=("GTAW", "SMAW"),
        positions=("5G", "6G"), supported=True):
    return WpsRecord(
        wps_no=no,
        pqr_no="PQR-101",
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-12-31T23:59:59Z",
        material_groups=list(groups),
        thickness_min_mm=tmin,
        thickness_max_mm=tmax,
        diameter_min_mm=dmin,
        diameter_max_mm=dmax,
        processes=list(processes),
        positions=list(positions),
        supported_by_pqr=supported,
    )


def welder(wid: str = "W-001", groups=("Fe-1",), tmin=3.0, tmax=20.0,
           dmin=25.0, dmax=600.0, processes=("GTAW", "SMAW"),
           positions=("5G", "6G"), **kw):
    return WelderQual(
        welder_id=wid,
        stamp=f"S-{wid[-3:]}",
        valid_from=kw.get("valid_from", "2026-01-01T00:00:00Z"),
        valid_to=kw.get("valid_to", "2026-12-31T23:59:59Z"),
        material_groups=list(groups),
        thickness_min_mm=tmin,
        thickness_max_mm=tmax,
        diameter_min_mm=dmin,
        diameter_max_mm=dmax,
        processes=list(processes),
        positions=list(positions),
    )


def end(side: str, heat: str, *, diameter=114.3, thickness=8.18, group=GROUP):
    return HeatEnd(
        side=side,
        heat_no=heat,
        material_group=group,
        nominal_diameter_mm=diameter,
        thickness_mm=thickness,
    )


def weld(no: str, heat_a: str, heat_b: str, *, day: int = 1,
         welder_id="W-001", wps_no="WPS-101", lot_id="LOT-A",
         line="PL-100", diameter=114.3, thickness=8.18, process="GTAW+SMAW",
         position="6G"):
    return WeldRecord(
        weld_no=no,
        line_no=line,
        end_a=end("a", heat_a, diameter=diameter, thickness=thickness),
        end_b=end("b", heat_b, diameter=diameter, thickness=thickness),
        weld_process=process,
        weld_position=position,
        welded_at=f"2026-03-{day:02d}T09:00:00Z",
        welder_id=welder_id,
        wps_no=wps_no,
        lot_id=lot_id,
    )


def rt(no: str, weld_no: str, result: str, *, day: int, hour=14,
       bands=((0, 360),), defects=(), iteration=0, lot_id=None,
       method="RT", full=False, examiner="NDE-II-张"):
    return NdeRecord(
        nde_id=no,
        weld_no=weld_no,
        method=method,
        result=result,
        examined_at=f"2026-03-{day:02d}T{hour:02d}:00:00Z",
        examiner=examiner,
        lot_id=lot_id,
        coverage=[
            AngleBand(start_deg=a, end_deg=b, full_circle=full)
            for a, b in bands
        ],
        defect_locations=[AngleBand(start_deg=a, end_deg=b) for a, b in defects],
        iteration=iteration,
        report_no=f"RT-2026-{no}",
    )


def repair(no: str, weld_no: str, iteration: int, band, *, day: int,
           welder_id="W-001", wps_no="WPS-101", approved=True,
           approver="焊接责任工程师-李"):
    return RepairRecord(
        repair_id=no,
        weld_no=weld_no,
        iteration=iteration,
        excavated_band=AngleBand(start_deg=band[0], end_deg=band[1]),
        repaired_at=f"2026-03-{day:02d}T10:00:00Z",
        welder_id=welder_id,
        wps_no=wps_no,
        approved=approved,
        approved_by=approver if approved else None,
    )


def lot(rule_id="LOT-A", ratio=0.2, on_reject="double", double_step=0.2,
        method="RT", min_samples=1):
    return LotRule(
        lot_id=rule_id,
        required_method=method,
        sample_ratio=ratio,
        on_reject=on_reject,
        double_step=double_step,
        min_samples=min_samples,
    )


# ============================================================ 场景一：直接放行


def direct_release_payload() -> SubmissionPayload:
    """10 道口、20% 抽检（2 张合格），资格/炉批齐全 => 全部直接放行。"""
    welds = [
        weld(f"W-{i:03d}", f"H-A{100+i}", f"H-B{200+i}", day=1 + i // 4)
        for i in range(1, 11)
    ]
    # 20% = 2 口抽检，均合格全周片
    ndes = [
        rt("N-001", "W-001", "accept", day=2, bands=((0, 360),)),
        rt("N-002", "W-006", "accept", day=8, bands=((0, 360),)),
    ]
    return SubmissionPayload(
        package_ref="DEMO-1-DIRECT",
        line_no="PL-100",
        submitted_by="质检员-王",
        wps=[wps()],
        welders=[welder()],
        welds=welds,
        nde=ndes,
        repairs=[],
        lot_rules=[lot(ratio=0.2)],
    )


# ============================================================ 场景二：缺陷扩检


def extension_payload(*, enough_extension: bool = True) -> SubmissionPayload:
    """抽检 2 张中 W-001 不合格 -> 加倍扩检。

    enough_extension=False 时只补 1 口（应需 4 口），触发 LT-EXTENSION-INSUFFICIENT。
    返修与复检沿 300°~340° 缺陷位置闭合。
    """
    welds = [
        weld(f"W-{i:03d}", f"H-A{100+i}", f"H-B{200+i}", day=1 + i // 4)
        for i in range(1, 11)
    ]
    ndes = [
        # 首批 20% = W-001、W-006；W-001 在 300~340 出缺陷
        rt("N-001", "W-001", "reject", day=2,
           bands=((0, 360),), defects=((300, 340),)),
        rt("N-002", "W-006", "accept", day=8),
    ]
    if enough_extension:
        # 加倍 20%：再检 2 口，均合格 => 扩检数量够
        ndes += [
            rt("N-101", "W-002", "accept", day=16),
            rt("N-102", "W-007", "accept", day=17),
        ]
    else:
        ndes.append(rt("N-101", "W-002", "accept", day=16))

    # W-001 挖补 295~345，复检片覆盖 280~355 且合格
    repairs = [repair("R-001", "W-001", 1, (295, 345), day=5)]
    ndes.append(rt("N-051", "W-001", "accept", day=6,
                   bands=((280, 355),), iteration=1))

    return SubmissionPayload(
        package_ref="DEMO-2-EXTEND" if enough_extension
        else "DEMO-2-EXTEND-SHORT",
        line_no="PL-100",
        submitted_by="质检员-王",
        wps=[wps()],
        welders=[welder()],
        welds=welds,
        nde=ndes,
        repairs=repairs,
        lot_rules=[lot(ratio=0.2, on_reject="double")],
    )


# ============================================================ 场景三：二次返修


def double_repair_payload(*, second_covered: bool = True) -> SubmissionPayload:
    """单口 100% RT：原始 200~230 缺陷 -> 一次返修 -> 复检在 210~218 仍不合格
    -> 二次返修 -> 复检覆盖挖补区合格。

    second_covered=False 时二次复检片刻意漏掉挖补区尾部，触发 RP-EXCAVATION-UNCOVERED。
    """
    welds = [weld("W-901", "H-X1", "H-X2", day=1, lot_id="LOT-B")]
    ndes = [
        rt("N-900", "W-901", "reject", day=2,
           bands=((0, 360),), defects=((200, 230),)),
    ]
    repairs = [
        repair("R-901", "W-901", 1, (195, 235), day=4),
    ]
    # 一次复检：210~218 仍显示不合格
    ndes.append(rt("N-901", "W-901", "reject", day=6,
                   bands=((180, 250),), defects=((210, 218),), iteration=1))
    repairs.append(repair("R-902", "W-901", 2, (205, 222), day=8))
    # 二次复检
    if second_covered:
        bands = ((200, 228),)
    else:
        bands = ((200, 215),)  # 挖补 205~222 未被完整覆盖
    ndes.append(rt("N-902", "W-901", "accept", day=10,
                   bands=bands, iteration=2))

    return SubmissionPayload(
        package_ref="DEMO-3-REPAIR2" if second_covered
        else "DEMO-3-REPAIR2-UNCOVERED",
        line_no="PL-200",
        submitted_by="质检员-赵",
        wps=[wps()],
        welders=[welder()],
        welds=welds,
        nde=ndes,
        repairs=repairs,
        lot_rules=[lot(rule_id="LOT-B", ratio=1.0, on_reject="full")],
    )
