"""无损检测资源核验：人员证书与设备版本必须在整个检测实施时段持续有效。

核验口径（资源失效即报告作废，不得计入抽检/扩检/返修复检）：
1. 人员证书：方法、级别（实施≥I 级、复核≥II 级）、产品类别、检测技术范围，
   以及 [started_at, finished_at] 整段落在有效期内；实施与复核不得同一证书；
2. 设备版本：方法适用、校准有效期持续覆盖整个实施时段（含夜班跨到期点）、
   量程/能量范围覆盖实际检测参数，主设备登记的关键附件（探头、胶片/IP 板）
   存在、同方法、且各自校准有效；
3. 引用缺失（人员证号、设备/附件版本未登记）同样判资源失效。

返回结构化 failures（条款码 + 角色/目标/原因），由引擎统一发 finding，
同时给出证书/设备摘要——复核签字时随快照冻结，续证不回写旧版。
"""
from __future__ import annotations

from datetime import datetime

from .schemas import NdeEquipmentVersion, NdePersonnelCert, NdeRecord

LEVEL_RANK = {"I": 1, "II": 2, "III": 3}

# 主设备必须带有的关键附件（按检测方法）
_REQUIRED_ACCESSORY = {
    "RT": {"kinds": {"film"}, "roles": {"film", "ip", "film_ip"}},
    "UT": {"kinds": {"ut_probe"}, "roles": {"probe"}},
}


def _window_covers(start_valid: datetime, end_valid: datetime,
                   period_start: datetime, period_end: datetime) -> bool:
    """资源有效期 [start_valid, end_valid]（含端点）是否覆盖整个实施时段。"""
    return start_valid <= period_start and period_end <= end_valid


def cert_summary(cert: NdePersonnelCert | None) -> dict | None:
    if cert is None:
        return None
    return {
        "cert_no": cert.cert_no,
        "name": cert.name,
        "method": cert.method,
        "level": cert.level,
        "products": list(cert.products),
        "techniques": list(cert.techniques),
        "valid_from": cert.valid_from.isoformat(),
        "valid_to": cert.valid_to.isoformat(),
    }


def equipment_summary(eq: NdeEquipmentVersion | None, role: str = "main") -> dict | None:
    if eq is None:
        return None
    return {
        "equipment_id": eq.equipment_id,
        "version": eq.version,
        "role": role,
        "name": eq.name,
        "kind": eq.kind,
        "method": eq.method,
        "serial_no": eq.serial_no,
        "calibrated_from": eq.calibrated_from.isoformat(),
        "calibrated_to": eq.calibrated_to.isoformat(),
        "techniques": list(eq.techniques),
        "range_min_mm": eq.range_min_mm,
        "range_max_mm": eq.range_max_mm,
        "energy_min_kev": eq.energy_min_kev,
        "energy_max_kev": eq.energy_max_kev,
        "source_isotope": eq.source_isotope,
        "uses": [u.model_dump(mode="json") for u in eq.uses],
    }


def _personnel_failures(role: str, cert_no: str | None,
                        cert_index: dict[str, NdePersonnelCert],
                        rec: NdeRecord, product: str) -> list[dict]:
    """实施(role=examiner)/复核(role=reviewer)人员逐项核验。"""
    failures: list[dict] = []
    min_level = "I" if role == "examiner" else "II"
    role_label = "实施人员" if role == "examiner" else "复核人员"

    cert = cert_index.get(cert_no) if cert_no else None
    if not cert_no or cert is None:
        failures.append({
            "code": "NR-CERT-MISSING", "role": role,
            "target": cert_no,
            "reason": f"{role_label}证书引用缺失或未登记",
        })
        return failures  # 后续逐项核对无意义

    if cert.method != rec.method:
        failures.append({
            "code": "NR-CERT-METHOD", "role": role, "target": cert.cert_no,
            "reason": f"{role_label}证书认可方法 {cert.method} ≠ 检测方法 {rec.method}",
            "cert_method": cert.method, "actual_method": rec.method,
        })
    if LEVEL_RANK[cert.level] < LEVEL_RANK[min_level]:
        failures.append({
            "code": "NR-CERT-LEVEL", "role": role, "target": cert.cert_no,
            "reason": f"{role_label}证书级别 {cert.level} 不足（至少需 {min_level} 级）",
            "cert_level": cert.level, "required_level": min_level,
        })
    if product not in cert.products:
        failures.append({
            "code": "NR-CERT-PRODUCT", "role": role, "target": cert.cert_no,
            "reason": f"证书认可产品 {cert.products} 未覆盖焊口产品 {product}",
            "products": list(cert.products), "product": product,
        })
    # 技术范围：证书未登记技术范围（空）不得解释为"全部认可"；
    # 记录未声明实际技术同样无法核对，按技术核验失败剔除。
    if not cert.techniques or not rec.technique \
            or rec.technique not in cert.techniques:
        failures.append({
            "code": "NR-CERT-TECHNIQUE", "role": role, "target": cert.cert_no,
            "reason": (
                f"证书技术范围 {list(cert.techniques)} 不含实际技术 "
                f"{rec.technique!r}（证书技术范围留空或记录未声明技术均不采信）"
            ),
            "techniques": list(cert.techniques),
            "technique": rec.technique,
        })
    if not _window_covers(cert.valid_from, cert.valid_to,
                          rec.period_start, rec.period_end):
        failures.append({
            "code": "NR-CERT-EXPIRED", "role": role, "target": cert.cert_no,
            "reason": (
                f"证书有效期 {cert.valid_from.isoformat()} ~ "
                f"{cert.valid_to.isoformat()} 未持续覆盖实施时段 "
                f"{rec.period_start.isoformat()} ~ {rec.period_end.isoformat()}"
            ),
            "valid_from": cert.valid_from.isoformat(),
            "valid_to": cert.valid_to.isoformat(),
            "period_start": rec.period_start.isoformat(),
            "period_end": rec.period_end.isoformat(),
        })
    return failures


def _accessory_failures(eq: NdeEquipmentVersion, rec: NdeRecord,
                        equip_index, chain: tuple[str, ...],
                        enforce_required: bool = True,
                        extra_uses=()) -> list[dict]:
    """核对主设备版本登记的关键附件：存在、同方法、校准覆盖、技术一致。

    附件有两种登记方式，均予认可：
    - 随主设备版本登记（eq.uses，推荐：版本即冻结了所用附件组合）；
    - 检测记录直接在 equipment_uses 中挂载 role != main 的附件（extra_uses）。
    chain 防止异常数据中附件相互引用形成环；enforce_required 仅对主设备
    顶层生效（胶片/探头自身不要求再挂胶片/探头）。
    """
    failures: list[dict] = []
    required = _REQUIRED_ACCESSORY.get(eq.method)
    accessories = []

    registered = {(u.equipment_id, u.version) for u in eq.uses}
    # 直挂在检测记录上的附件已在 _equipment_failures 逐项校验；
    # 这里只纳入未随主设备版本登记者，避免重复发条款。
    all_uses = list(eq.uses) + [
        u for u in extra_uses
        if (u.equipment_id, u.version) not in registered
    ]
    for use in all_uses:
        target = f"{use.equipment_id}@{use.version}"
        if use.equipment_id in chain:
            continue
        acc = equip_index.get((use.equipment_id, use.version))
        accessories.append((use, acc))
        if acc is None:
            failures.append({
                "code": "NR-EQUIP-MISSING", "role": "accessory",
                "target": target,
                "reason": f"主设备 {eq.equipment_id}@{eq.version} 的关键附件 "
                          f"{target} 未登记",
            })
            continue
        if acc.method != eq.method:
            failures.append({
                "code": "NR-EQUIP-ACCESSORY", "role": "accessory",
                "target": target,
                "reason": f"附件方法 {acc.method} 与主设备方法 {eq.method} 不一致",
            })
        if not _window_covers(acc.calibrated_from, acc.calibrated_to,
                              rec.period_start, rec.period_end):
            failures.append({
                "code": "NR-EQUIP-CALIBRATION", "role": "accessory",
                "target": target,
                "reason": f"附件 {target} 校准有效期未持续覆盖整个实施时段",
                "calibrated_from": acc.calibrated_from.isoformat(),
                "calibrated_to": acc.calibrated_to.isoformat(),
                "period_start": rec.period_start.isoformat(),
                "period_end": rec.period_end.isoformat(),
            })
        if not acc.techniques or not rec.technique \
                or rec.technique not in acc.techniques:
            failures.append({
                "code": "NR-EQUIP-ACCESSORY", "role": "accessory",
                "target": target,
                "reason": f"附件 {target} 不支持实际检测技术 "
                          f"{rec.technique!r}（或附件技术能力未登记）",
                "techniques": list(acc.techniques),
                "technique": rec.technique,
            })
        # 附件自身的下挂附件（罕见）递归核对一层；不再套主设备附件要求
        failures.extend(_accessory_failures(
            acc, rec, equip_index, chain + (acc.equipment_id,),
            enforce_required=False,
        ))

    if enforce_required and required is not None:
        ok_kinds = any(acc is not None and acc.kind in required["kinds"]
                       and acc.method == eq.method
                       for _use, acc in accessories)
        ok_roles = any(use.role in required["roles"] for use, _acc in accessories)
        if not (ok_kinds or ok_roles):
            label = "胶片/IP 板" if eq.method == "RT" else "探头"
            failures.append({
                "code": "NR-EQUIP-ACCESSORY", "role": "accessory",
                "target": f"{eq.equipment_id}@{eq.version}",
                "reason": f"{eq.method} 主设备缺少有效登记的关键附件（{label}）",
                "required_kind": sorted(required["kinds"]),
            })
    return failures


def _equipment_failures(rec: NdeRecord, equip_index) -> list[dict]:
    failures: list[dict] = []
    uses = rec.equipment_uses
    main_uses = [u for u in uses if u.role == "main"]

    if not uses:
        failures.append({
            "code": "NR-EQUIP-MISSING", "role": "main", "target": None,
            "reason": "检测记录未引用任何设备版本",
        })
        return failures
    if not main_uses:
        failures.append({
            "code": "NR-EQUIP-MISSING", "role": "main", "target": None,
            "reason": "检测记录未标记主设备(role=main)",
        })

    for use in uses:
        target = f"{use.equipment_id}@{use.version}"
        eq = equip_index.get((use.equipment_id, use.version))
        if eq is None:
            failures.append({
                "code": "NR-EQUIP-MISSING", "role": use.role, "target": target,
                "reason": f"设备版本 {target} 未登记（引用缺失）",
            })
            continue
        is_main = use.role == "main"

        if eq.method != rec.method:
            failures.append({
                "code": "NR-EQUIP-METHOD" if is_main else "NR-EQUIP-ACCESSORY",
                "role": use.role, "target": target,
                "reason": f"设备方法 {eq.method} ≠ 检测方法 {rec.method}",
                "equipment_method": eq.method, "actual_method": rec.method,
            })
        if not _window_covers(eq.calibrated_from, eq.calibrated_to,
                              rec.period_start, rec.period_end):
            failures.append({
                "code": "NR-EQUIP-CALIBRATION", "role": use.role, "target": target,
                "reason": (
                    f"设备校准有效期 {eq.calibrated_from.isoformat()} ~ "
                    f"{eq.calibrated_to.isoformat()} 未持续覆盖实施时段 "
                    f"{rec.period_start.isoformat()} ~ {rec.period_end.isoformat()}"
                ),
                "calibrated_from": eq.calibrated_from.isoformat(),
                "calibrated_to": eq.calibrated_to.isoformat(),
                "period_start": rec.period_start.isoformat(),
                "period_end": rec.period_end.isoformat(),
            })
        if is_main:
            # 技术能力：设备未登记技术能力（空）不解释为全部支持
            if not eq.techniques or not rec.technique \
                    or rec.technique not in eq.techniques:
                failures.append({
                    "code": "NR-EQUIP-TECHNIQUE", "role": use.role,
                    "target": target,
                    "reason": f"设备技术能力 {list(eq.techniques)} 不支持实际技术 "
                              f"{rec.technique!r}（设备能力留空或记录未声明技术）",
                    "techniques": list(eq.techniques),
                    "technique": rec.technique,
                })
            # 能力规格必填：射线源必须登记能量范围；UT 主机必须登记量程。
            # 规格缺失即无法核对匹配，不得以"记录不填实际参数"绕过。
            if eq.kind in ("xray_source", "gamma_source"):
                if eq.energy_min_kev is None or eq.energy_max_kev is None:
                    failures.append({
                        "code": "NR-EQUIP-SPEC-MISSING", "role": use.role,
                        "target": target,
                        "reason": f"射线源 {target} 未登记能量范围",
                        "energy_min_kev": eq.energy_min_kev,
                        "energy_max_kev": eq.energy_max_kev,
                    })
                if rec.exposure_energy_kev is None:
                    failures.append({
                        "code": "NR-RECORD-PARAMETER-MISSING", "role": use.role,
                        "target": target,
                        "reason": "RT 检测记录未登记实际曝光能量 exposure_energy_kev",
                    })
            if eq.kind == "ut_flaw_detector":
                if eq.range_min_mm is None or eq.range_max_mm is None:
                    failures.append({
                        "code": "NR-EQUIP-SPEC-MISSING", "role": use.role,
                        "target": target,
                        "reason": f"UT 探伤仪 {target} 未登记量程范围",
                        "range_min_mm": eq.range_min_mm,
                        "range_max_mm": eq.range_max_mm,
                    })
                if rec.applied_thickness_mm is None:
                    failures.append({
                        "code": "NR-RECORD-PARAMETER-MISSING", "role": use.role,
                        "target": target,
                        "reason": "UT 检测记录未登记实际声程/壁厚 "
                                  "applied_thickness_mm",
                    })
            # 实际参数落入能力范围（仅在双方均有值时比对，缺失已在上面挂条款）
            if rec.applied_thickness_mm is not None \
                    and eq.range_min_mm is not None and eq.range_max_mm is not None:
                t = rec.applied_thickness_mm
                lo, hi = eq.range_min_mm, eq.range_max_mm
                if not (lo <= t <= hi):
                    failures.append({
                        "code": "NR-EQUIP-PARAMETER", "role": use.role,
                        "target": target,
                        "reason": f"实际声程/壁厚 {t}mm 超出设备量程 [{lo}, {hi}]mm",
                        "applied_thickness_mm": t,
                        "range_min_mm": lo, "range_max_mm": hi,
                    })
            if rec.exposure_energy_kev is not None \
                    and eq.energy_min_kev is not None and eq.energy_max_kev is not None:
                e = rec.exposure_energy_kev
                lo, hi = eq.energy_min_kev, eq.energy_max_kev
                if not (lo <= e <= hi):
                    failures.append({
                        "code": "NR-EQUIP-PARAMETER", "role": use.role,
                        "target": target,
                        "reason": f"实际射线能量 {e}keV 超出设备/源能量范围 "
                                  f"[{lo}, {hi}]keV",
                        "exposure_energy_kev": e,
                        "energy_min_kev": lo, "energy_max_kev": hi,
                    })
            # 关键附件（探头/胶片）：随主设备版本登记，或由检测记录直接挂载
            extra = [u for u in rec.equipment_uses if u.role != "main"]
            failures.extend(_accessory_failures(
                eq, rec, equip_index, (eq.equipment_id,),
                extra_uses=extra))
        else:
            # 检测记录直接挂的附件（不随主设备版本）：技术能力一致性
            if not eq.techniques or not rec.technique \
                    or rec.technique not in eq.techniques:
                failures.append({
                    "code": "NR-EQUIP-ACCESSORY", "role": use.role, "target": target,
                    "reason": f"附件 {target} 不支持实际检测技术 "
                              f"{rec.technique!r}（或附件技术能力未登记）",
                })
    return failures


def verify_nde_resource(rec: NdeRecord, *, product: str,
                        cert_index: dict[str, NdePersonnelCert],
                        equip_index: dict[tuple[str, str], NdeEquipmentVersion]
                        ) -> dict:
    """核验单条检测记录的资源有效性，返回资源结论与冻结用摘要。

    返回：
      {
        "resource_valid": bool,
        "period": {started_at, finished_at},
        "examiner": {cert_no, cert: 摘要|None},
        "reviewer": {cert_no, cert: 摘要|None},
        "equipment": [设备版本摘要...],
        "failures": [{code, role, target, reason, ...}],
      }
    """
    failures: list[dict] = []

    # 实施起止时刻必须显式登记：缺任一项即无法证明整个实施时段持续有效，
    # 不得以 examined_at 倒推替代（时段口径用于夜班跨到期点判定）。
    missing_period = []
    if rec.started_at is None:
        missing_period.append("started_at")
    if rec.finished_at is None:
        missing_period.append("finished_at")
    if missing_period:
        failures.append({
            "code": "NR-RECORD-PERIOD-MISSING", "role": "record",
            "target": rec.nde_id,
            "reason": "检测记录缺少实施时段字段: "
                      + "、".join(missing_period),
            "missing": missing_period,
        })

    examiner_cert = cert_index.get(rec.examiner_cert_no) \
        if rec.examiner_cert_no else None
    reviewer_cert = cert_index.get(rec.reviewer_cert_no) \
        if rec.reviewer_cert_no else None

    failures.extend(_personnel_failures(
        "examiner", rec.examiner_cert_no, cert_index, rec, product))
    failures.extend(_personnel_failures(
        "reviewer", rec.reviewer_cert_no, cert_index, rec, product))

    # 角色冲突：实施与复核同一证书（两者均已登记时才比对，
    # 缺失已由 NR-CERT-MISSING 处理）
    if (rec.examiner_cert_no and rec.reviewer_cert_no
            and rec.examiner_cert_no == rec.reviewer_cert_no):
        failures.append({
            "code": "NR-ROLE-CONFLICT", "role": "both",
            "target": rec.examiner_cert_no,
            "reason": "实施人员与复核人员为同一证书，检测不得自己复核",
            "cert_no": rec.examiner_cert_no,
        })

    failures.extend(_equipment_failures(rec, equip_index))

    equipment = []
    seen_keys: set[tuple[str, str]] = set()

    def _collect(use, is_main: bool) -> None:
        key = (use.equipment_id, use.version)
        eq = equip_index.get(key)
        if eq is None or key in seen_keys:
            return
        seen_keys.add(key)
        equipment.append(equipment_summary(eq, use.role if is_main else "accessory"))
        # 随设备版本登记的关键附件一并纳入冻结摘要（版本即冻结附件组合）
        for sub in eq.uses:
            _collect(sub, False)

    for use in rec.equipment_uses:
        _collect(use, use.role == "main")

    return {
        "resource_valid": not failures,
        "period": {
            "started_at": rec.period_start.isoformat(),
            "finished_at": rec.period_end.isoformat(),
        },
        "examiner": {
            "cert_no": rec.examiner_cert_no,
            "cert": cert_summary(examiner_cert),
        },
        "reviewer": {
            "cert_no": rec.reviewer_cert_no,
            "cert": cert_summary(reviewer_cert),
        },
        "equipment": equipment,
        "failures": failures,
    }
