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
# 无损检测人员资格级别（NB/T 47013：I 级可在 II/III 级指导下操作，
# II 级可评定检测结果、签发报告，III 级为最高）
NdeLevel = Literal["I", "II", "III"]
# 设备版本类别：决定按量程(UT)还是能量范围(RT)核对参数
EquipmentKind = Literal[
    "xray_source",      # X 射线机
    "gamma_source",     # γ 射线源
    "ut_flaw_detector", # UT 探伤仪主机
    "ut_probe",         # UT 探头（本身须有校准有效期）
    "pt_materials",     # 渗透检测材料包
    "film",             # 胶片/IP 板等
    "other",
]


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
    consumable_classes: list[str] = Field(
        default_factory=list,
        description="WPS 规定的焊材分类号清单，如 ['E5015']（AWS A5.1/GB/T 5117 "
                    "焊条分类号）；焊材链启用时实际消耗批次分类号必须落在此清单内",
    )
    process_windows: list[WpsProcessWindow] = Field(
        default_factory=list,
        description="按焊接方法冻结的道次参数窗口（极性/电流/电压/焊速/热输入/"
                    "预热/层间温度）；道次链启用时实际道次方法必须在此登记",
    )


class WelderQual(QualRange):
    welder_id: str = Field(min_length=1)
    stamp: Optional[str] = None


# ---------------------------------------------------------------- 无损检测资源
#
# 资源按"版本"登记，检测记录引用版本身份：
# - 人员证书以 cert_no 为稳定身份（续证后换新证号/同号新窗均应作为新登记记录，
#   旧记录保留在已冻结快照中，续证不回写旧版）；
# - 设备以 serial_no + version 标识同一台设备的一次校准配置版本，
#   换探头、重新校准即派生新版本；关键附件以 use 列表引用的其它设备版本体现。


class NdePersonnelCert(UtcModel):
    """无损检测人员资格证书（RT/UT/PT 方法与级别、产品、技术范围、有效期）。

    同一证号在包内仅可登记一条；续证/换证请使用新证号或新审查包版本。
    """

    cert_no: str = Field(min_length=1, description="证书编号，包内唯一")
    name: str = Field(min_length=1, description="持证人员姓名")
    method: NdeMethod
    level: NdeLevel
    products: list[str] = Field(
        min_length=1,
        description="认可产品，如 pressure_pipe(承压管道)/pressure_vessel/boiler",
    )
    techniques: list[str] = Field(
        default_factory=list,
        description="认可检测技术范围，如 RT:[film/digital]、UT:[pulse_echo/tofd/pa]、"
                    "PT:[solvent_removable/water_washable]",
    )
    valid_from: datetime
    valid_to: datetime
    employer: Optional[str] = Field(default=None, description="执业单位（备查）")

    @model_validator(mode="after")
    def _check_window(self) -> "NdePersonnelCert":
        if _as_utc(self.valid_to) <= _as_utc(self.valid_from):
            raise ValueError("valid_to 必须晚于 valid_from")
        return self


class NdeEquipmentUse(UtcModel):
    """检测实施中对一个设备版本的引用（主设备或关键附件）。"""

    equipment_id: str = Field(min_length=1)
    version: str = Field(min_length=1, description="设备配置版本号，如 v1/2026A")
    role: str = Field(
        default="main",
        description="用途：main=主设备；probe/film/consumable 等为关键附件",
    )


class NdeEquipmentVersion(UtcModel):
    """一台无损检测设备的一个校准配置版本。

    序列号不变、换探头/换源/重新校准则产生新版本（version 不同）；
    量程范围（UT 探伤仪/探头）或能量范围（射线源）按检测实际参数核对；
    关键附件（探头、胶片等）以 uses 引用，引用缺失/未校准视为该版本不完整。
    """

    equipment_id: str = Field(min_length=1, description="设备编号（同机跨版本稳定）")
    version: str = Field(min_length=1, description="配置/校准版本号，同机唯一")
    name: str = Field(min_length=1, description="设备名称")
    kind: EquipmentKind
    method: NdeMethod = Field(description="适用检测方法")
    serial_no: str = Field(min_length=1, description="出厂序列号")
    calibrated_from: datetime = Field(description="校准有效期起点")
    calibrated_to: datetime = Field(description="校准有效期止点（含）")
    techniques: list[str] = Field(
        default_factory=list, description="支持的检测技术，与人员证书技术范围同口径"
    )
    # UT 设备：量程范围（声程 mm）；射线源：能量范围
    range_min_mm: Optional[float] = Field(default=None, ge=0, description="UT 量程下限(mm)")
    range_max_mm: Optional[float] = Field(default=None, gt=0, description="UT 量程上限(mm)")
    energy_min_kev: Optional[float] = Field(
        default=None, ge=0, description="射线能量下限(keV；X 机为管电压，γ源为光子能量)"
    )
    energy_max_kev: Optional[float] = Field(
        default=None, gt=0, description="射线能量上限(keV)"
    )
    source_isotope: Optional[str] = Field(
        default=None, description="γ 源核素，如 Ir-192 / Co-60；X 机留空"
    )
    uses: list[NdeEquipmentUse] = Field(
        default_factory=list, description="关键附件（探头/胶片/耗材）引用的设备版本"
    )

    @model_validator(mode="after")
    def _check_self(self) -> "NdeEquipmentVersion":
        cf, ct = _as_utc(self.calibrated_from), _as_utc(self.calibrated_to)
        if ct <= cf:
            raise ValueError("calibrated_to 必须晚于 calibrated_from")
        if self.range_max_mm is not None and self.range_min_mm is not None \
                and self.range_max_mm < self.range_min_mm:
            raise ValueError("range_max_mm 不得小于 range_min_mm")
        if self.energy_max_kev is not None and self.energy_min_kev is not None \
                and self.energy_max_kev < self.energy_min_kev:
            raise ValueError("energy_max_kev 不得小于 energy_min_kev")
        if self.kind == "gamma_source" and not self.source_isotope:
            raise ValueError("kind=gamma_source 时必须填写 source_isotope")
        return self


# ---------------------------------------------------------------- 焊材批次与烘干领用链
#
# 低氢焊条（GB/T 5117 E5015 类）事件链：
#   批次(含质保书/入库验收/适用 WPS)
#     → 烘干周期 BakeCycle（烘箱某校准版本内：装入→温度时序→取出）
#       → 保温暂存 QuiverStay（保温筒某校准版本内：装入→温度时序→取出）
#         → 领用段 IssueSegment（领出数量/时刻）
#           → 施焊/返修消耗 ConsumableUse（每道焊口、每次返修逐根挂账）
#           → 退回(再烘干)/报废 SegmentEvent（闭合领用数量）
# 设备以 container_id + version 复合身份登记，重新校准/换测温探头派生新版本。

ReceivedStatus = Literal["accepted", "quarantine", "rejected"]
ContainerKind = Literal["oven", "quiver"]
SegmentEventType = Literal["return", "scrap"]


class ConsumableBatch(UtcModel):
    """焊材批次：分类号（牌号类别）、制造批号、质保书、入库状态与适用 WPS。"""

    batch_id: str = Field(min_length=1, description="焊材批次登记号，包内唯一")
    classification: str = Field(
        min_length=1,
        description="焊材分类号，如 E5015（低氢碱性焊条），与 WPS 分类号清单核对",
    )
    designation: Optional[str] = Field(
        default=None, description="商品牌号（备查），如 J507"
    )
    manufacturer: Optional[str] = Field(default=None, description="制造厂（备查）")
    manufacturer_lot_no: str = Field(min_length=1, description="制造厂批号（炉批号）")
    cert_no: Optional[str] = Field(default=None, description="质量证明书（质保书）编号")
    cert_lot_no: Optional[str] = Field(
        default=None, description="质保书记载批号；须与制造批号一致"
    )
    received_status: ReceivedStatus = Field(
        default="accepted", description="入库状态：accepted=验收合格/quarantine=待检/rejected=拒收"
    )
    received_qty_kg: float = Field(gt=0, description="入库数量(kg)")
    applicable_wps: list[str] = Field(
        default_factory=list, description="该批次适用的 WPS 编号清单"
    )


class ConsumableRule(UtcModel):
    """按焊材分类号规定的烘干/保温/暴露制度。"""

    classification: str = Field(min_length=1, description="焊材分类号，包内唯一")
    bake_temp_min_c: float = Field(description="烘干恒温温度下限(℃)，如 350")
    bake_temp_max_c: float = Field(description="烘干恒温温度上限(℃)，如 400")
    min_soak_minutes: float = Field(gt=0, description="烘干窗口内最短恒温时长(分钟)")
    holding_temp_min_c: float = Field(description="保温筒温度下限(℃)，如 100")
    holding_temp_max_c: float = Field(description="保温筒温度上限(℃)，如 150")
    max_exposure_minutes: float = Field(
        gt=0, description="领出保温筒后单次最大暴露时长(分钟)，默认 240（4 小时）"
    )
    max_bake_cycles: int = Field(
        default=2, ge=1,
        description="允许烘干周期序号上限（默认 2：首次烘干 + 至多 1 次返烘）",
    )
    max_transfer_minutes: float = Field(
        default=15.0, gt=0,
        description="烘箱取出到装入保温筒的最大允许间隔(分钟)，超期即保温断档",
    )
    max_log_gap_minutes: float = Field(
        default=180.0, gt=0,
        description="烘箱/保温筒温度时序相邻读数最大间隔(分钟)，超出视为记录断档",
    )

    @model_validator(mode="after")
    def _check_rule(self) -> "ConsumableRule":
        if self.bake_temp_max_c < self.bake_temp_min_c:
            raise ValueError("bake_temp_max_c 不得小于 bake_temp_min_c")
        if self.holding_temp_max_c < self.holding_temp_min_c:
            raise ValueError("holding_temp_max_c 不得小于 holding_temp_min_c")
        return self


class ConsumableContainerVersion(UtcModel):
    """烘箱(oven)/保温筒(quiver) 的一个校准配置版本（测温/控温装置校准）。"""

    container_id: str = Field(min_length=1, description="设备编号（同机跨版本稳定）")
    version: str = Field(min_length=1, description="校准版本号，同机唯一")
    name: str = Field(min_length=1)
    kind: ContainerKind
    serial_no: str = Field(min_length=1)
    calibrated_from: datetime
    calibrated_to: datetime

    @model_validator(mode="after")
    def _check_window(self) -> "ConsumableContainerVersion":
        if _as_utc(self.calibrated_to) <= _as_utc(self.calibrated_from):
            raise ValueError("calibrated_to 必须晚于 calibrated_from")
        return self


class TemperatureReading(UtcModel):
    """烘箱/保温筒温度时序上的一个读数点。"""

    at: datetime
    temp_c: float = Field(ge=0)


class BakeCycle(UtcModel):
    """一次烘干周期：同一批次在烘箱内 装入→恒温→取出，cycle_no 为第几烘。"""

    bake_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    cycle_no: int = Field(ge=1, description="烘干序号：1=首次烘干，2=返烘一次")
    oven_id: str
    oven_version: str
    loaded_at: datetime = Field(description="装入烘箱时刻")
    unloaded_at: datetime = Field(description="取出烘箱时刻")
    loaded_qty_kg: float = Field(gt=0, description="本周期装入数量(kg)")
    readings: list[TemperatureReading] = Field(
        default_factory=list, description="烘箱温度时序（须持续覆盖烘干时段）"
    )

    @model_validator(mode="after")
    def _check_order(self) -> "BakeCycle":
        if _as_utc(self.unloaded_at) <= _as_utc(self.loaded_at):
            raise ValueError("unloaded_at 必须晚于 loaded_at")
        return self


class QuiverStay(UtcModel):
    """烘干后在保温筒内的一段连续暂存；领用段必须挂在暂存区间内。"""

    stay_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    bake_id: str = Field(description="来源烘干周期（烘箱→保温筒交接）")
    quiver_id: str
    quiver_version: str
    loaded_at: datetime = Field(description="装入保温筒时刻（须紧随烘箱取出）")
    unloaded_at: Optional[datetime] = Field(
        default=None, description="整段暂存结束时刻；仍在筒内可留空"
    )
    loaded_qty_kg: float = Field(gt=0, description="装入保温筒数量(kg)")
    readings: list[TemperatureReading] = Field(
        default_factory=list, description="保温筒温度时序（须持续覆盖暂存时段）"
    )

    @model_validator(mode="after")
    def _check_order(self) -> "QuiverStay":
        if self.unloaded_at is not None and \
                _as_utc(self.unloaded_at) <= _as_utc(self.loaded_at):
            raise ValueError("unloaded_at 必须晚于 loaded_at")
        return self


class ConsumableIssueSegment(UtcModel):
    """一次领用段：自保温暂存领出的数量与时刻，消耗/退回/报废均挂在其下。"""

    segment_id: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    stay_id: str = Field(description="领出自哪段保温暂存")
    issued_at: datetime
    issued_qty_kg: float = Field(gt=0)
    issued_to: Optional[str] = Field(default=None, description="领用人（备查）")


class ConsumableSegmentEvent(UtcModel):
    """领用段上的退回(return)/报废(scrap)事件，参与领用数量闭合。"""

    event_id: str = Field(min_length=1)
    segment_id: str
    event_type: SegmentEventType
    at: datetime
    qty_kg: float = Field(gt=0)


class ConsumableUse(UtcModel):
    """一次实际消耗：焊口施焊或返修补焊实际使用的批次与领用段及数量。

    used_at 缺省时由引擎按施焊时刻(welded_at)/补焊时刻(repaired_at) 代入。
    """

    use_id: str = Field(min_length=1)
    batch_id: str
    segment_id: str
    qty_kg: float = Field(gt=0)
    used_at: Optional[datetime] = None


# ---------------------------------------------------------------- 焊接道次执行链
#
# WPS 版本除整体认可范围（有效期/组别/厚度/管径/方法）外，还按焊接方法冻结
# 极性、电流、电压、热输入、预热与层间温度窗口（WpsProcessWindow）。
# 逐焊口与逐返修提交道次记录 PassRecord：道次编号、起止时刻、焊工、方法、
# 实测电流/电压、焊缝长度（燃弧时间可显式给出，缺省取起止时长），以及预热/
# 层间温度测温记录 TemperatureMeasurement 与所用仪表（电流表/电压表/测温仪/
# 计时器）的校准版本 WeldGaugeVersion。服务按时间与层序重建道次链并换算热输入
# E = k·U·I·60/v（kJ/mm，v=焊缝长度/燃弧分钟数），返修混用参数同样定位到道次。

Polarity = Literal["DCEN", "DCEP", "AC", "DC"]
WeldGaugeKind = Literal[
    "ammeter",       # 电流表
    "voltmeter",     # 电压表
    "thermometer",   # 测温仪（红外/接触式热电偶等）
    "timer",         # 焊速计时
    "weld_monitor",  # 焊接参数监测仪（电流/电压/焊速一体）
    "other",
]
PassRole = Literal["root", "fill", "cap"]  # 打底(根焊)/填充/盖面
PassScope = Literal["production", "repair"]


class WpsProcessWindow(UtcModel):
    """WPS 对单一焊接方法冻结的道次参数窗口（版本即随 WPS 记录冻结）。"""

    process: str = Field(min_length=1, description="焊接方法代号，如 GTAW/SMAW")
    polarity: list[Polarity] = Field(
        min_length=1, description="允许极性清单，如 ['DCEP']；DC 表示不区分正反接"
    )
    current_min_a: float = Field(ge=0, description="电流下限(A)")
    current_max_a: float = Field(gt=0, description="电流上限(A)")
    voltage_min_v: float = Field(ge=0, description="电压下限(V)")
    voltage_max_v: float = Field(gt=0, description="电压上限(V)")
    travel_min_mm_min: Optional[float] = Field(
        default=None, ge=0, description="焊速下限(mm/min)；不限制可留空"
    )
    travel_max_mm_min: Optional[float] = Field(
        default=None, gt=0, description="焊速上限(mm/min)"
    )
    heat_input_min_kj_mm: Optional[float] = Field(
        default=None, ge=0, description="热输入下限(kJ/mm)"
    )
    heat_input_max_kj_mm: Optional[float] = Field(
        default=None, gt=0, description="热输入上限(kJ/mm)"
    )
    thermal_efficiency: float = Field(
        default=0.8, gt=0, le=1.0,
        description="热效率系数 k（GTAW≈0.75、SMAW≈0.8），用于热输入换算",
    )
    preheat_min_c: float = Field(
        default=0.0, ge=0, description="首道起弧前预热温度下限(℃)"
    )
    preheat_max_c: Optional[float] = Field(
        default=None, gt=0,
        description="首道起弧前预热温度上限(℃)；不限制可留空。注意：下限为 0 "
                    "仅表示不强制预热，首道仍须有可追溯的起弧前温度测点",
    )
    preheat_lead_minutes: float = Field(
        default=60.0, gt=0,
        description="预热测温须在首道起弧前多长时间内完成（默认 60 分钟）",
    )
    interpass_min_c: Optional[float] = Field(
        default=None, ge=0, description="层间温度下限(℃)；不限制留空"
    )
    interpass_max_c: float = Field(
        gt=0, description="层间温度上限(℃)，如 200/250"
    )

    @model_validator(mode="after")
    def _check_window(self) -> "WpsProcessWindow":
        if self.current_max_a < self.current_min_a:
            raise ValueError("current_max_a 不得小于 current_min_a")
        if self.voltage_max_v < self.voltage_min_v:
            raise ValueError("voltage_max_v 不得小于 voltage_min_v")
        if (self.travel_min_mm_min is not None
                and self.travel_max_mm_min is not None
                and self.travel_max_mm_min < self.travel_min_mm_min):
            raise ValueError("travel_max_mm_min 不得小于 travel_min_mm_min")
        if (self.heat_input_min_kj_mm is not None
                and self.heat_input_max_kj_mm is not None
                and self.heat_input_max_kj_mm < self.heat_input_min_kj_mm):
            raise ValueError("heat_input_max_kj_mm 不得小于 heat_input_min_kj_mm")
        if self.interpass_min_c is not None \
                and self.interpass_min_c > self.interpass_max_c:
            raise ValueError("interpass_min_c 不得大于 interpass_max_c")
        if self.preheat_max_c is not None \
                and self.preheat_max_c < self.preheat_min_c:
            raise ValueError("preheat_max_c 不得小于 preheat_min_c")
        return self


class WeldGaugeVersion(UtcModel):
    """一只焊接测量仪表（电流表/电压表/测温仪/计时器/监测仪）的校准版本。

    仪表编号不变、重新检定/校准即派生新版本（version 不同）；
    道次与测温记录以 (gauge_id, version) 引用，校准失效按使用时点判定。
    """

    gauge_id: str = Field(min_length=1, description="仪表编号（同表跨版本稳定）")
    version: str = Field(min_length=1, description="校准版本号，同表唯一")
    name: str = Field(min_length=1)
    kind: WeldGaugeKind
    serial_no: str = Field(min_length=1)
    calibrated_from: datetime
    calibrated_to: datetime

    @model_validator(mode="after")
    def _check_window(self) -> "WeldGaugeVersion":
        if _as_utc(self.calibrated_to) <= _as_utc(self.calibrated_from):
            raise ValueError("calibrated_to 必须晚于 calibrated_from")
        return self


class GaugeUse(UtcModel):
    """道次实施中对一个仪表校准版本的引用（实测参数来源可追溯）。"""

    gauge_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    role: str = Field(
        default="main",
        description="用途：main=主监测；ammeter/voltmeter/thermometer/timer 等",
    )


class TemperatureMeasurement(UtcModel):
    """预热/层间温度的一个测温测点记录。

    归属：焊口施焊或某次返修；pass_no 给出该测温所对应的**道次**
    （该道起弧前测得的层间温度；首道对应预热温度）。pass_no 缺省时由引擎
    按时点（上一道结束 ~ 本道起弧之间）重建归属。
    """

    measure_id: str = Field(min_length=1, description="测温记录编号，包内唯一")
    weld_no: str = Field(min_length=1)
    scope: PassScope = Field(description="production=施焊前测温，repair=返修补焊测温")
    repair_id: Optional[str] = Field(
        default=None, description="scope=repair 时所属返修记录编号"
    )
    iteration: Optional[int] = Field(
        default=None, ge=1, description="返修测温时的返修次序"
    )
    pass_no: Optional[int] = Field(
        default=None, ge=1,
        description="对应道次编号（该道起弧前的层间/预热温度）；缺省按时点重建",
    )
    measured_at: datetime = Field(description="测温时刻")
    temp_c: float = Field(ge=0)
    gauge_id: str = Field(min_length=1, description="测温仪表编号")
    gauge_version: str = Field(min_length=1, description="测温仪表校准版本")
    kind: Literal["preheat", "interpass"] = Field(
        default="interpass",
        description="preheat=首道起弧前预热温度；interpass=层间温度",
    )


class PassRecord(UtcModel):
    """一道焊道的实际执行记录：打底/填充/盖面中的一道（多层多道）。"""

    pass_id: str = Field(min_length=1, description="道次记录编号，包内唯一")
    weld_no: str = Field(min_length=1)
    scope: PassScope
    repair_id: Optional[str] = Field(default=None, description="scope=repair 时必填")
    iteration: Optional[int] = Field(
        default=None, ge=1, le=2, description="返修道次的返修次序（1/2）"
    )
    pass_no: int = Field(ge=1, description="道次编号：1=首道(打底)，同范围连续")
    layer_no: int = Field(
        ge=1, description="层号：1=打底层，其后填充层，最大层为盖面层"
    )
    started_at: datetime = Field(description="起弧时刻")
    finished_at: datetime = Field(description="收弧时刻（须严格晚于起弧）")
    welder_id: str = Field(description="本道实际施焊焊工（须与焊口/返修登记一致）")
    process: str = Field(min_length=1, description="本道实际焊接方法，如 GTAW")
    polarity: Polarity = Field(description="本道实际极性")
    current_a: float = Field(gt=0, description="本道实测电流(A)")
    voltage_v: float = Field(gt=0, description="本道实测电弧电压(V)")
    weld_length_mm: float = Field(gt=0, description="本道焊缝长度(mm)")
    arc_minutes: Optional[float] = Field(
        default=None, gt=0,
        description="燃弧时间(分钟)；缺省按 finished_at-started_at 换算焊速",
    )
    gauges: list[GaugeUse] = Field(
        default_factory=list,
        description="本道实测所用仪表版本（电流/电压/焊速监测仪等）",
    )

    @model_validator(mode="after")
    def _check_order(self) -> "PassRecord":
        if _as_utc(self.finished_at) <= _as_utc(self.started_at):
            raise ValueError("finished_at 必须严格晚于 started_at")
        if self.scope == "repair" and not self.repair_id:
            raise ValueError("scope=repair 的道次必须给出 repair_id")
        if self.scope == "repair" and self.iteration is None:
            raise ValueError("scope=repair 的道次必须给出 iteration")
        return self


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
    product: str = Field(
        default="pressure_pipe",
        description="产品类别，与 NDT 人员证书 products 认可范围核对（默认承压管道）",
    )
    consumables: list[ConsumableUse] = Field(
        default_factory=list,
        description="施焊实际消耗的焊材批次与领用段（焊材链启用时逐道焊口必填）",
    )


# ---------------------------------------------------------------- 检测与返修


class NdeRecord(UtcModel):
    nde_id: str = Field(min_length=1)
    weld_no: str
    method: NdeMethod
    result: NdeResult
    examined_at: datetime = Field(description="检测/报告时刻（排序与抽检时序用）")
    started_at: Optional[datetime] = Field(
        default=None, description="实施开始时刻；缺省与 examined_at 同时刻"
    )
    finished_at: Optional[datetime] = Field(
        default=None, description="实施结束时刻；缺省与 examined_at 同时刻"
    )
    examiner: Optional[str] = Field(default=None, description="实施人姓名（展示用）")
    examiner_cert_no: Optional[str] = Field(
        default=None, description="实施人员证书编号（引用 nde_personnel）"
    )
    reviewer_cert_no: Optional[str] = Field(
        default=None, description="复核/评片人员证书编号（引用 nde_personnel）"
    )
    equipment_uses: list[NdeEquipmentUse] = Field(
        default_factory=list, description="本次检测使用的设备版本（主设备+关键附件）"
    )
    technique: Optional[str] = Field(
        default=None,
        description="实际检测技术，如 film/digital、pulse_echo/tofd/pa、"
                    "solvent_removable",
    )
    applied_thickness_mm: Optional[float] = Field(
        default=None, gt=0,
        description="实际检测声程/壁厚(mm)：与 UT 设备量程范围核对",
    )
    exposure_energy_kev: Optional[float] = Field(
        default=None, gt=0,
        description="实际射线能量(keV)：与射线源能量范围核对",
    )
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
        start = self.started_at or self.examined_at
        finish = self.finished_at or self.examined_at
        if not (start <= self.examined_at <= finish):
            raise ValueError(
                "检测时段必须满足 started_at <= examined_at <= finished_at"
            )
        return self

    @property
    def period_start(self) -> datetime:
        """实施时段起点（UTC）。"""
        return _as_utc(self.started_at or self.examined_at)

    @property
    def period_end(self) -> datetime:
        """实施时段终点（UTC）：资源有效性必须持续覆盖整个时段。"""
        return _as_utc(self.finished_at or self.examined_at)


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
    consumables: list[ConsumableUse] = Field(
        default_factory=list,
        description="补焊实际消耗的焊材批次与领用段；失效焊材不得用于返修闭合",
    )


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
    weld_gauges: list[WeldGaugeVersion] = Field(
        default_factory=list,
        description="焊接测量仪表（电流/电压/测温/计时/监测仪）校准版本登记，"
                    "身份为 (gauge_id, version)，重新校准派生新版本",
    )
    weld_passes: list[PassRecord] = Field(
        default_factory=list,
        description="逐焊口/逐返修的道次执行记录；非空即启用道次执行链",
    )
    temperature_measurements: list[TemperatureMeasurement] = Field(
        default_factory=list,
        description="逐道次起弧前的预热/层间温度测温记录（含测温仪表版本）",
    )
    nde_personnel: list[NdePersonnelCert] = Field(
        default_factory=list, description="无损检测人员证书登记（证号唯一）"
    )
    nde_equipment: list[NdeEquipmentVersion] = Field(
        default_factory=list,
        description="无损检测设备版本登记（equipment_id+version 唯一）",
    )
    consumable_batches: list[ConsumableBatch] = Field(
        default_factory=list,
        description="焊材批次登记（分类号/制造批号/质保书/入库状态/适用 WPS）；"
                    "非空即视为本包启用焊材链，逐道焊口与返修均须挂实际消耗",
    )
    consumable_rules: list[ConsumableRule] = Field(
        default_factory=list,
        description="按分类号规定的烘干/保温/暴露/重复烘干制度",
    )
    consumable_containers: list[ConsumableContainerVersion] = Field(
        default_factory=list,
        description="烘箱/保温筒校准版本登记（container_id+version 唯一）",
    )
    bake_cycles: list[BakeCycle] = Field(default_factory=list)
    quiver_stays: list[QuiverStay] = Field(default_factory=list)
    consumable_segments: list[ConsumableIssueSegment] = Field(default_factory=list)
    consumable_events: list[ConsumableSegmentEvent] = Field(default_factory=list)
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

        # NDT 资源身份唯一：证号唯一；设备 (编号,版本) 唯一。
        # 检测记录引用缺失不在此拒绝（沿用未登记 WPS 的引擎挂条款口径）。
        cert_nos = [c.cert_no for c in self.nde_personnel]
        if len(set(cert_nos)) != len(cert_nos):
            dupes = sorted({n for n in cert_nos if cert_nos.count(n) > 1})
            errors.append(f"NDT 人员证书编号重复: {dupes}")
        equip_keys = [(e.equipment_id, e.version) for e in self.nde_equipment]
        if len(set(equip_keys)) != len(equip_keys):
            dupes = sorted({k for k in equip_keys if equip_keys.count(k) > 1})
            errors.append(f"NDT 设备版本标识重复 (equipment_id,version): {dupes}")

        # ---- 焊接道次执行链身份唯一 ----
        gauge_keys = [(g.gauge_id, g.version) for g in self.weld_gauges]
        if len(set(gauge_keys)) != len(gauge_keys):
            dupes = sorted({k for k in gauge_keys if gauge_keys.count(k) > 1})
            errors.append(f"焊接仪表版本标识重复 (gauge_id,version): {dupes}")
        pass_ids = [p.pass_id for p in self.weld_passes]
        if len(set(pass_ids)) != len(pass_ids):
            dupes = sorted({n for n in pass_ids if pass_ids.count(n) > 1})
            errors.append(f"焊接道次记录编号重复: {dupes}")
        measure_ids = [m.measure_id for m in self.temperature_measurements]
        if len(set(measure_ids)) != len(measure_ids):
            dupes = sorted({n for n in measure_ids if measure_ids.count(n) > 1})
            errors.append(f"测温记录编号重复: {dupes}")

        # ---- 焊材链身份唯一（引用缺失不在此拒绝，沿用引擎挂条款口径）----
        def _dupe_ids(items, label: str, attr: str = None):
            ids = [getattr(i, attr) if attr else i for i in items]
            if len(set(ids)) != len(ids):
                dupes = sorted({n for n in ids if ids.count(n) > 1})
                errors.append(f"{label}重复: {dupes}")

        _dupe_ids(self.consumable_batches, "焊材批次编号", "batch_id")
        _dupe_ids(self.consumable_rules, "焊材烘干制度分类号", "classification")
        cc_keys = [(c.container_id, c.version) for c in self.consumable_containers]
        if len(set(cc_keys)) != len(cc_keys):
            dupes = sorted({k for k in cc_keys if cc_keys.count(k) > 1})
            errors.append(f"烘箱/保温筒校准版本标识重复 (container_id,version): {dupes}")
        _dupe_ids(self.bake_cycles, "烘干周期编号", "bake_id")
        _dupe_ids(self.quiver_stays, "保温暂存编号", "stay_id")
        _dupe_ids(self.consumable_segments, "焊材领用段编号", "segment_id")
        _dupe_ids(self.consumable_events, "领用段事件编号", "event_id")
        # 同一批次的烘干序号不得重复
        bake_keys = [(b.batch_id, b.cycle_no) for b in self.bake_cycles]
        if len(set(bake_keys)) != len(bake_keys):
            dupes = sorted({k for k in bake_keys if bake_keys.count(k) > 1})
            errors.append(f"同批次烘干周期序号重复 (batch_id,cycle_no): {dupes}")
        # 消耗编号在焊口+返修范围内全局唯一
        use_ids = [u.use_id for w in self.welds for u in w.consumables]
        use_ids += [u.use_id for r in self.repairs for u in r.consumables]
        _dupe_ids(use_ids, "焊材消耗记录编号")

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

        # ---- 焊接道次执行链引用完整性 ----
        # 同一焊口/返修范围内的道次重号、层序/时段问题不在此拒绝（由引擎按
        # WP-PASS-DUP / WP-LAYER-GAP / WP-PASS-OVERLAP 挂条款并定位原始道次），
        # 这里只拒绝跨实体的悬空引用与范围归属错误。
        repair_index = {(r.weld_no, r.iteration): r for r in self.repairs}
        for p in self.weld_passes:
            if p.weld_no not in weld_set:
                errors.append(f"道次 {p.pass_id} 引用不存在的焊口: {p.weld_no}")
            if p.scope == "repair":
                rep = repair_index.get((p.weld_no, p.iteration or -1))
                if rep is None:
                    errors.append(
                        f"道次 {p.pass_id} 引用不存在的返修: "
                        f"焊口 {p.weld_no} 第 {p.iteration} 次"
                    )
                elif p.repair_id != rep.repair_id:
                    errors.append(
                        f"道次 {p.pass_id} 的 repair_id {p.repair_id} 与焊口 "
                        f"{p.weld_no} 第 {p.iteration} 次返修编号 "
                        f"{rep.repair_id} 不一致"
                    )
        for m in self.temperature_measurements:
            if m.weld_no not in weld_set:
                errors.append(f"测温记录 {m.measure_id} 引用不存在的焊口: {m.weld_no}")
            if m.scope == "repair":
                rep = repair_index.get((m.weld_no, m.iteration or -1))
                if rep is None:
                    errors.append(
                        f"测温记录 {m.measure_id} 引用不存在的返修: "
                        f"焊口 {m.weld_no} 第 {m.iteration} 次"
                    )
                elif m.repair_id != rep.repair_id:
                    errors.append(
                        f"测温记录 {m.measure_id} 的 repair_id 与返修编号不一致"
                    )

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
    report_no: Optional[str] = None
    batch_id: Optional[str] = None
    segment_id: Optional[str] = None
    bake_id: Optional[str] = None
    stay_id: Optional[str] = None
    use_id: Optional[str] = None
    pass_id: Optional[str] = None
    measure_id: Optional[str] = None
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
    consumable_uses: list[dict] = Field(default_factory=list)
    weld_passes: list[dict] = Field(
        default_factory=list,
        description="本次返修补焊的道次链重建结果（道次/热输入/测温/仪表）",
    )
    passes_valid: bool = Field(
        default=True, description="返修道次链是否核验通过（False 时该次返修不得闭合）"
    )
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
    consumable_uses: list[dict] = Field(
        default_factory=list,
        description="施焊/返修实际消耗的焊材批次、领用段与事件链判定摘要",
    )
    weld_passes: list[dict] = Field(
        default_factory=list,
        description="本焊口施焊缝的道次链重建结果（按时间与层序排序）",
    )
    passes_valid: Optional[bool] = Field(
        default=None,
        description="施焊缝道次链核验是否通过；道次链未启用时为 null",
    )
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
    excluded_reports: list[dict] = Field(
        default_factory=list,
        description="因 NDT 资源核验未通过而从抽检/扩检剔除的报告清单",
    )
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
    nde_resources: dict = Field(
        default_factory=dict,
        description="人员证书/设备版本登记目录与被剔除报告清单（资源版本+失效原因）",
    )
    consumables: dict = Field(
        default_factory=dict,
        description="焊材批次/制度/烘箱保温筒版本/烘干/保温/领用段事件链目录"
                    "与失效消耗清单（规则摘要）",
    )
    weld_execution: dict = Field(
        default_factory=dict,
        description="WPS 方法参数窗口、焊接仪表版本登记目录，及逐焊口/返修的"
                    "道次链核验状态与失效道次清单",
    )
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


class NdeReportState(BaseModel):
    """单份检测报告在一个版本下的资源核验状态（供版本差异呈现）。"""

    nde_id: str
    report_no: Optional[str] = None
    weld_no: str
    method: str
    iteration: int = 0
    resource_valid: bool
    period: Optional[dict] = None
    reasons: list[str] = Field(default_factory=list)
    examiner_cert_no: Optional[str] = None
    reviewer_cert_no: Optional[str] = None
    equipment: list[dict] = Field(default_factory=list)


class NdeEvaluation(BaseModel):
    """两版审查结论中 NDT 资源核验状态的结构化对比。"""

    old_decision: Optional[str] = None
    new_decision: Optional[str] = None
    old_codes: list[str] = Field(default_factory=list)
    new_codes: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(
        default_factory=list,
        description="新版不再触发的条款（含续期消除的 NR-CERT-EXPIRED）",
    )
    introduced: list[str] = Field(
        default_factory=list,
        description="新版新触发的条款",
    )
    old_invalid_reports: list[NdeReportState] = Field(default_factory=list)
    new_invalid_reports: list[NdeReportState] = Field(default_factory=list)
    changed_reports: list[dict] = Field(
        default_factory=list,
        description="资源核验状态发生变化的报告（含失效详情与所用资源版本）",
    )


class DiffReport(BaseModel):
    package_id: str
    from_version: int
    to_version: int
    changes: list[DiffEntry]
    decision_changed: bool
    old_decision: Optional[str] = None
    new_decision: Optional[str] = None
    evaluation: Optional[NdeEvaluation] = None
    consumable_evaluation: Optional["WmConsumableEvaluation"] = None
    weld_pass_evaluation: Optional["WpPassEvaluation"] = None


class WmUseState(BaseModel):
    """单次焊材消耗在一个版本下的事件链核验状态（供版本差异呈现）。"""

    use_id: str
    weld_no: str
    scope: str = Field(description="production=施焊消耗，repair=返修消耗")
    repair_id: Optional[str] = None
    iteration: Optional[int] = None
    batch_id: Optional[str] = None
    segment_id: Optional[str] = None
    qty_kg: Optional[float] = None
    consumable_valid: bool
    reasons: list[str] = Field(default_factory=list)
    chain: dict = Field(default_factory=dict)


class WmConsumableEvaluation(BaseModel):
    """两版审查结论中焊材批次与烘干领用链核验状态的结构化对比。"""

    old_decision: Optional[str] = None
    new_decision: Optional[str] = None
    old_codes: list[str] = Field(default_factory=list)
    new_codes: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(
        default_factory=list,
        description="新版不再触发的 WM 条款（补录温度记录/重新校准后消除）",
    )
    introduced: list[str] = Field(
        default_factory=list,
        description="新版新触发的 WM 条款",
    )
    old_invalid_uses: list[WmUseState] = Field(default_factory=list)
    new_invalid_uses: list[WmUseState] = Field(default_factory=list)
    changed_uses: list[dict] = Field(
        default_factory=list,
        description="事件链核验状态发生变化的消耗（含批次、领用段、失效定位）",
    )


# ============================================================ 焊接道次执行链差异


class WpPassState(BaseModel):
    """一个焊口施焊范围（或某次返修）在一个版本下的道次链核验状态。"""

    scope_key: str = Field(description="production:<weld_no> 或 repair:<repair_id>")
    weld_no: str
    scope: str
    repair_id: Optional[str] = None
    iteration: Optional[int] = None
    wps_no: Optional[str] = None
    pass_total: int = 0
    pass_valid: bool
    reasons: list[str] = Field(default_factory=list)
    passes: list[dict] = Field(default_factory=list)


class WpPassEvaluation(BaseModel):
    """两版审查结论中焊接道次执行链核验状态的结构化对比。"""

    old_decision: Optional[str] = None
    new_decision: Optional[str] = None
    old_codes: list[str] = Field(default_factory=list)
    new_codes: list[str] = Field(default_factory=list)
    resolved: list[str] = Field(
        default_factory=list,
        description="新版不再触发的 WP 条款（补录测温/重新校准/修正参数后消除）",
    )
    introduced: list[str] = Field(default_factory=list,
                                  description="新版新触发的 WP 条款")
    old_invalid_scopes: list[WpPassState] = Field(default_factory=list)
    new_invalid_scopes: list[WpPassState] = Field(default_factory=list)
    changed_scopes: list[dict] = Field(
        default_factory=list,
        description="道次链核验状态发生变化的焊口/返修（含失效道次与测点定位）",
    )
