"""请求与响应 Pydantic 模型。

角度口径：所有周向位置均以度为单位，0° 为管顶（12 点方向），顺时针为正，
允许 0~360 之外的输入，内部统一归一化。区间允许跨 0°（如 [350, 10]）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from .clauses import Severity

NdeMethod = Literal["RT", "UT", "PT"]
NdeResult = Literal["accept", "reject"]
RepairMode = Literal["double", "full"]
Decision = Literal["release", "hold"]


def _as_utc(dt: datetime) -> datetime:
    """无时区输入按 UTC 处理；有时区则换算到 UTC，保证时点比较一致。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class UtcModel(BaseModel):
    """基类：归一化所有 datetime 字段为 UTC aware。"""

    @model_validator(mode="after")
    def _normalize_datetimes(self) -> "UtcModel":
        for name, field in type(self).model_fields.items():
            if field.annotation is datetime or datetime in getattr(field.annotation, "__args__", ()):
                value = getattr(self, name)
                if isinstance(value, datetime):
                    setattr(self, name, _as_utc(value))
        return self


# ---------------------------------------------------------------- 几何与炉批


class AngleBand(UtcModel):
    """周向角度区间 [start_deg, end_deg)，允许跨 0°。

    全周以 full_circle=True 表示（如整条环缝 PT），此时角度忽略。
    """

    start_deg: float = Field(ge=-360, le=720, description="区间起始角，度")
    end_deg: float = Field(ge=-360, le=720, description="区间结束角（不含），度")
    full_circle: bool = False

    @model_validator(mode="after")
    def _check_band(self) -> "AngleBand":
        if not self.full_circle and self.start_deg == self.end_deg:
            raise ValueError("角度区间长度为 0；全周请使用 full_circle=true")
        return self


class HeatEnd(UtcModel):
    """焊口一侧母材：炉批号 + 几何 + 组别。一道焊口两个端（a/b）。"""

    side: Literal["a", "b"]
    heat_no: str = Field(min_length=1, description="母材炉批号")
    material_group: str = Field(min_length=1, description="材料组别，如 Fe-1")
    nominal_diameter_mm: float = Field(gt=0)
    thickness_mm: float = Field(gt=0, description="该侧名义厚度，用于资格厚度匹配")


# ---------------------------------------------------------------- 资格


class QualRange(UtcModel):
    """WPS/PQR 或焊工资格的认可范围（以施焊时点判断有效性）。"""

    valid_from: datetime
    valid_to: datetime
    material_groups: list[str] = Field(min_length=1, description="认可的母材组别")
    thickness_min_mm: float = Field(ge=0)
    thickness_max_mm: float = Field(gt=0)
    diameter_min_mm: Optional[float] = Field(default=None, gt=0, description="焊工资格管径下限")
    diameter_max_mm: Optional[float] = Field(default=None, gt=0)
    processes: list[str] = Field(default_factory=list, description="认可焊接方法，如 GTAW/SMAW")
    positions: list[str] = Field(default_factory=list, description="认可焊接位置，如 6G/5G")
    supported_by_pqr: bool = Field(default=True, description="WPS 是否有 PQR 支撑")

    @model_validator(mode="after")
    def _check_window(self) -> "QualRange":
        if _as_utc(self.valid_to) <= _as_utc(self.valid_from):
            raise ValueError("valid_to 必须晚于 valid_from")
        if self.thickness_max_mm < self.thickness_min_mm:
            raise ValueError("thickness_max_mm 不得小于 thickness_min_mm")
        return self


class WpsRecord(QualRange):
    wps_no: str = Field(min_length=1)
    pqr_no: Optional[str] = None


class WelderQual(QualRange):
    welder_id: str = Field(min_length=1)
    stamp: Optional[str] = None


# ---------------------------------------------------------------- 焊口本体


class WeldRecord(UtcModel):
    weld_no: str = Field(min_length=1, description="焊口编号，包内唯一")
    line_no: str = Field(min_length=1, description="管线号")
    spec: Optional[str] = Field(default=None, description="管道等级/规格书")
    end_a: HeatEnd
    end_b: HeatEnd
    weld_process: str = Field(description="实际焊接方法，如 GTAW+SMAW")
    weld_position: str = Field(description="实际焊接位置，如 6G")
    welded_at: datetime = Field(description="施焊时刻（资格按此时点匹配）")
    welder_id: str
    wps_no: str
    lot_id: Optional[str] = Field(default=None, description="所属检验批；无抽检批时为空")


# ---------------------------------------------------------------- 检测与返修


class NdeRecord(UtcModel):
    nde_id: str = Field(min_length=1)
    weld_no: str
    method: NdeMethod
    result: NdeResult
    examined_at: datetime
    examiner: Optional[str] = None
    lot_id: Optional[str] = Field(default=None, description="该底片计入哪个检验批的抽检")
    # 本次检测覆盖的环向范围（RT/UT 为若干张片；PT 通常为全周）
    coverage: list[AngleBand] = Field(default_factory=list)
    # result=reject 时填写的缺陷显示位置；accept 时应为空
    defect_locations: list[AngleBand] = Field(default_factory=list)
    iteration: int = Field(
        default=0, ge=0,
        description="检测轮次：0=原始检测，1=一次返修后复检，2=二次返修后复检",
    )
    report_no: Optional[str] = None

    @model_validator(mode="after")
    def _check_result(self) -> "NdeRecord":
        if self.result == "reject" and not self.defect_locations:
            raise ValueError("nde 记录为 reject 时必须给出 defect_locations")
        return self


class RepairRecord(UtcModel):
    repair_id: str = Field(min_length=1)
    weld_no: str
    iteration: int = Field(ge=1, le=2, description="第几次返修（同一位置上限 2 次）")
    excavated_band: AngleBand = Field(description="挖补区域（环向角度区间）")
    repaired_at: datetime = Field(description="补焊时刻，返修资格按此时点匹配")
    welder_id: str
    wps_no: str
    approved: bool = False
    approved_by: Optional[str] = None


class LotRule(UtcModel):
    lot_id: str = Field(min_length=1)
    required_method: NdeMethod
    sample_ratio: float = Field(gt=0, le=1, description="规定抽检比例，0.1=10%，1=100%")
    on_reject: RepairMode = Field(description="抽检不合格时：double=加倍，full=全检")
    double_step: float = Field(default=0.2, gt=0, le=1, description="加倍时每次追加比例")
    min_samples: int = Field(default=1, ge=1, description="抽检数量下限（按比例上取整后取大）")


class SubmissionPayload(UtcModel):
    """一次报检/放行申请的完整载荷。"""

    package_ref: Optional[str] = Field(default=None, description="外部审查包编号；缺省由服务端生成")
    line_no: Optional[str] = None
    submitted_by: Optional[str] = None
    wps: list[WpsRecord] = Field(default_factory=list)
    welders: list[WelderQual] = Field(default_factory=list)
    welds: list[WeldRecord] = Field(min_length=1)
    nde: list[NdeRecord] = Field(default_factory=list)
    repairs: list[RepairRecord] = Field(default_factory=list)
    lot_rules: list[LotRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> "SubmissionPayload":
        errors: list[str] = []

        weld_nos = [w.weld_no for w in self.welds]
        if len(set(weld_nos)) != len(weld_nos):
            dupes = sorted({n for n in weld_nos if weld_nos.count(n) > 1})
            errors.append(f"焊口编号重复: {dupes}")
        weld_set = set(weld_nos)

        wps_set = {w.wps_no for w in self.wps}
        welder_set = {w.welder_id for w in self.welders}
        lot_set = {r.lot_id for r in self.lot_rules}

        for w in self.welds:
            if w.wps_no not in wps_set:
                errors.append(f"焊口 {w.weld_no} 引用未登记 WPS: {w.wps_no}")
            if w.welder_id not in welder_set:
                errors.append(f"焊口 {w.weld_no} 引用未登记焊工: {w.welder_id}")
            # 归属未登记检验批不在此拒绝：由引擎按 LT-RULE-MISSING 判 hold

        nde_ids = [r.nde_id for r in self.nde]
        if len(set(nde_ids)) != len(nde_ids):
            dupes = sorted({n for n in nde_ids if nde_ids.count(n) > 1})
            errors.append(f"检测记录编号重复: {dupes}")
        for r in self.nde:
            if r.weld_no not in weld_set:
                errors.append(f"检测 {r.nde_id} 引用不存在的焊口: {r.weld_no}")
            # r.lot_id 指向未登记批时不在此拒绝：由引擎按 NDE-UNKNOWN-LOT 挂条款

        repair_ids = [r.repair_id for r in self.repairs]
        if len(set(repair_ids)) != len(repair_ids):
            dupes = sorted({n for n in repair_ids if repair_ids.count(n) > 1})
            errors.append(f"返修记录编号重复: {dupes}")
        seen_repair: set[tuple[str, int]] = set()
        for r in self.repairs:
            if r.weld_no not in weld_set:
                errors.append(f"返修 {r.repair_id} 引用不存在的焊口: {r.weld_no}")
            if r.wps_no not in wps_set:
                errors.append(f"返修 {r.repair_id} 引用未登记 WPS: {r.wps_no}")
            if r.welder_id not in welder_set:
                errors.append(f"返修 {r.repair_id} 引用未登记焊工: {r.welder_id}")
            key = (r.weld_no, r.iteration)
            if key in seen_repair:
                errors.append(
                    f"焊口 {r.weld_no} 第 {r.iteration} 次返修存在多条记录"
                )
            seen_repair.add(key)

        if errors:
            raise ValueError("；".join(errors))
        return self


# ---------------------------------------------------------------- 响应


class Finding(BaseModel):
    code: str
    severity: Severity
    category: str
    reference: str
    message: str
    weld_no: Optional[str] = None
    lot_id: Optional[str] = None
    nde_id: Optional[str] = None
    repair_id: Optional[str] = None
    evidence: dict = Field(default_factory=dict)


class RepairLink(BaseModel):
    iteration: int
    repair_id: str
    repaired_at: datetime
    wps_no: str
    welder_id: str
    approved: bool
    excavated_band: dict
    reinspections: list[dict] = Field(default_factory=list)
    closed: bool


class WeldVerdict(BaseModel):
    weld_no: str
    line_no: str
    lot_id: Optional[str] = None
    welded_at: datetime
    material_groups: list[str]
    heat_nos: list[str]
    heats: list[dict]
    thickness_mm: list[float]
    nde_count: int
    nde: list[dict] = Field(default_factory=list)
    repairs: list[RepairLink] = Field(default_factory=list)
    decision: Decision
    findings: list[Finding] = Field(default_factory=list)
    svg_url: Optional[str] = None


class LotSummary(BaseModel):
    lot_id: str
    required_method: str
    required_ratio: float
    total_welds: int
    required_initial: int
    examined_initial: int
    extended: bool
    extension_mode: Optional[str] = None
    required_final: int
    examined_final: int
    final_ratio: float
    decision: Decision
    findings: list[Finding] = Field(default_factory=list)


class ReviewPack(BaseModel):
    package_id: str
    version: int
    parent_version: Optional[int] = None
    status: Optional[str] = None
    package_ref: str
    submitted_by: Optional[str] = None
    decision: Decision
    snapshot_sha256: str
    frozen: Optional[bool] = None
    reviewer: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    issuer: Optional[str] = None
    issued_at: Optional[datetime] = None
    evaluated_at: datetime
    stats: dict
    lot_summaries: list[LotSummary] = Field(default_factory=list)
    welds: list[WeldVerdict] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    clauses_triggered: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- 端点辅助载荷


class SignRequest(BaseModel):
    reviewer: str = Field(min_length=1)
    comment: Optional[str] = None


class IssueRequest(BaseModel):
    issuer: str = Field(min_length=1)
    comment: Optional[str] = None


class PackageSummary(BaseModel):
    package_id: str
    package_ref: str
    current_version: int
    status: str
    created_at: datetime
    updated_at: datetime


class DiffEntry(BaseModel):
    kind: Literal["added", "removed", "changed"]
    section: str
    key: Optional[str] = None
    field: Optional[str] = None
    old: object = None
    new: object = None


class DiffReport(BaseModel):
    package_id: str
    from_version: int
    to_version: int
    changes: list[DiffEntry]
    decision_changed: bool
    old_decision: Optional[str] = None
    new_decision: Optional[str] = None
