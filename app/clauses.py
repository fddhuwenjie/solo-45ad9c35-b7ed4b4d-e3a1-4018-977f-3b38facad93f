"""条款目录：所有 hold / 警告均引用此处的条款编号。

条款体系：
- T 系列：施工/验收通用（以《压力管道规范 工业管道》GB/T 20801、
  《现场设备、工业管道焊接工程施工规范》GB 50236、《承压设备无损检测》
  NB/T 47013 的通行要求为依据，描述采用规则化口径，不虚构具体条号）。
- WQ 系列：焊工/焊接工艺资格时点匹配。
- HT 系列：炉批（材料可追溯）。
- LT 系列：检验批抽检比例与扩检。
- RP 系列：返修、复检与沿缺陷位置的串接。
- NR 系列：无损检测资源核验（人员证书、设备版本按整个实施时段持续有效）。
"""
from __future__ import annotations

from enum import Enum


class Severity(str, Enum):
    HOLD = "hold"        # 触发即保持 hold，禁止放行
    WARNING = "warning"  # 不阻断放行，但列入审查包提示
    INFO = "info"


class ClauseCategory(str, Enum):
    QUALIFICATION = "qualification"
    MATERIAL = "material"
    NDE = "nde"
    LOT = "lot"
    REPAIR = "repair"
    RECORD = "record"


# 条款目录。键即 finding.code 引用的稳定编号。
CLAUSES: dict[str, dict[str, str]] = {
    # ---- 资格类：一律按"施焊时点"匹配 ----
    "WQ-WPS-EXPIRED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.QUALIFICATION.value,
        "reference": "GB/T 20801 / GB 50236：WPS 应在批准有效期内并经 PQR 支撑",
        "message": "施焊时点 WPS 不在批准有效期内（或无 PQR 支撑）",
    },
    "WQ-WELDER-EXPIRED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.QUALIFICATION.value,
        "reference": "GB 50236 / TSG Z6002：焊工应在合格项目有效期内施焊",
        "message": "焊工资格在施焊时点已失效（超期或未覆盖该焊口）",
    },
    "WQ-GROUP-OUTSIDE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.QUALIFICATION.value,
        "reference": "GB 50236：工艺/焊工认可的母材组别须覆盖实际组别",
        "message": "母材材料组别超出 WPS/PQR 或焊工资格认可范围",
    },
    "WQ-THICKNESS-OUTSIDE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.QUALIFICATION.value,
        "reference": "GB 50236 / NB/T 47014：认可厚度范围须覆盖实际厚度",
        "message": "母材厚度超出 WPS/PQR 或焊工资格认可厚度范围",
    },
    "WQ-DIAMETER-OUTSIDE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.QUALIFICATION.value,
        "reference": "GB 50236：认可管径范围须覆盖实际管径",
        "message": "管径超出焊工资格认可管径范围",
    },
    "WQ-PROCESS-MISMATCH": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.QUALIFICATION.value,
        "reference": "GB 50236：实际焊接方法应与认可项目一致",
        "message": "实际焊接方法/位置与 WPS 或焊工认可项目不一致",
    },
    # ---- 炉批 ----
    "HT-HEAT-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.MATERIAL.value,
        "reference": "GB/T 20801：焊口两侧母材炉批号应可追溯",
        "message": "焊口端缺少母材炉批号（一道焊口两个炉批均须登记）",
    },
    "HT-HEAT-DIAMETER-MISMATCH": {
        "severity": Severity.WARNING.value,
        "category": ClauseCategory.MATERIAL.value,
        "reference": "GB/T 20801：对接两侧几何应匹配（错边量受控）",
        "message": "焊口两侧公称直径不一致（异种管径对接需专项确认）",
    },
    # ---- 无损检测记录 ----
    "NDE-NONE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "GB/T 20801 / NB/T 47013：承压焊口应按规定实施无损检测",
        "message": "该焊口无任何 RT/UT/PT 检测记录",
    },
    "NDE-OPEN-DEFECT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：不合格显示应经返修并复检合格",
        "message": "存在未被返修闭合的不合格显示（缺陷位置无返修链）",
    },
    "NDE-METHOD-LOT-MISMATCH": {
        "severity": Severity.WARNING.value,
        "category": ClauseCategory.NDE.value,
        "reference": "GB/T 20801：检测方法应符合检验批规定",
        "message": "实际检测方法与检验批要求的检测方法不一致（按工艺口径确认）",
    },
    "NDE-UNKNOWN-LOT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.RECORD.value,
        "reference": "质量记录完整性：检测单引用的检验批应在报检范围内",
        "message": "检测记录引用了未登记的检验批编号",
    },
    # ---- 无损检测资源核验：人员证书 / 设备版本，按整个实施时段持续有效 ----
    "NR-CERT-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013 / TSG Z8001：检测报告应注明持证实施与复核人员",
        "message": "检测记录引用的实施/复核人员证书未登记（引用缺失）",
    },
    "NR-CERT-METHOD": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：人员资格项目应与实际检测方法一致",
        "message": "人员证书认可方法与检测实际方法不一致",
    },
    "NR-CERT-LEVEL": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：I 级在 II/III 级指导下操作；II 级及以上方可"
                     "评定结果、复核与签发报告",
        "message": "人员证书级别不足（实施至少 I 级，复核/评片至少 II 级）",
    },
    "NR-CERT-PRODUCT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "TSG Z8001 / NB/T 47013：持证项目应覆盖实际检测产品类别",
        "message": "人员证书认可产品未覆盖该焊口产品（如承压管道）",
    },
    "NR-CERT-TECHNIQUE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：持证检测技术范围应覆盖实际工艺"
                     "（如 TOFD/PA、数字射线、渗透剂类型）",
        "message": "实际检测技术超出人员证书认可的检测技术范围",
    },
    "NR-CERT-EXPIRED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "TSG Z8001：证书应在有效期内实施检测；跨到期点的夜班"
                     "时段不得整体采信",
        "message": "人员证书未在整个检测实施时段内持续有效"
                   "（时段跨过到期点或检测时证书尚未生效）",
    },
    "NR-ROLE-CONFLICT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013 / 质量体系：检测实施与复核/评片应由不同人员承担",
        "message": "实施人员与复核人员为同一证书（自己检测自己复核，角色冲突）",
    },
    "NR-EQUIP-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "质量记录完整性：检测报告应可追溯所用设备的具体校准版本",
        "message": "检测记录引用的设备版本（或其关键附件版本）未登记（引用缺失）",
    },
    "NR-EQUIP-METHOD": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：设备应适用于规定的检测方法",
        "message": "设备版本不适用本次检测方法",
    },
    "NR-EQUIP-CALIBRATION": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：检测设备与探头应在校准/核查有效期内使用；"
                     "跨到期点的检测时段不得整体采信",
        "message": "设备（含关键附件）校准有效期未持续覆盖整个检测实施时段"
                   "（夜班跨校准到期点或使用未校准设备/探头）",
    },
    "NR-EQUIP-PARAMETER": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：实际声程应在探伤仪量程内、实际射线能量"
                     "应在射线源/管电压能力范围内",
        "message": "设备量程或能量范围不匹配实际检测参数"
                   "（声程超量程 / 能量超出源能力）",
    },
    "NR-EQUIP-TECHNIQUE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：设备能力应支持所用检测技术（如 TOFD/PA、数字成像）",
        "message": "设备版本不支持实际检测技术",
    },
    "NR-EQUIP-ACCESSORY": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：探头、胶片/IP 板等关键附件应与方法、技术匹配"
                     "并单独处于有效期内",
        "message": "关键附件不匹配或缺失（RT 未登记胶片/IP、UT 未登记探头，"
                   "或附件方法/技术不一致）",
    },
    "NR-RECORD-EXCLUDED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "质量记录完整性：人员资格或设备状态在检测当时无效的报告"
                     "不得计入抽检、扩检与返修复检",
        "message": "该检测报告因 NDT 资源核验未通过，已从抽检/扩检/返修复检"
                   "计数中剔除（同一张底片不能既覆盖焊缝又覆盖资源资格）",
    },
    # ---- 检验批 ----
    "LT-RULE-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.LOT.value,
        "reference": "GB/T 20801：抽检比例应事先在检验批规则中规定",
        "message": "焊口归属检验批但未提供检验批规则，抽检比例无法核算",
    },
    "LT-SAMPLE-INSUFFICIENT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.LOT.value,
        "reference": "GB/T 20801：检验批实际抽检比例不得低于规定比例",
        "message": "检验批抽检数量不足，实际覆盖率低于规定抽检比例",
    },
    "LT-EXTENSION-INSUFFICIENT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.LOT.value,
        "reference": "GB/T 20801：抽检发现不合格应按规定扩检（加倍/100%）",
        "message": "抽检失败后扩检数量不足：达到扩检规则要求前焊口保持 hold",
    },
    "LT-OPEN-REJECT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.LOT.value,
        "reference": "NB/T 47013 / GB/T 20801：扩检中再次出现不合格且未闭合时整批不得放行",
        "message": "扩检（加倍/全检）过程中仍有不合格焊口未返修闭合，整批保持 hold",
    },
    # ---- 返修链：沿缺陷位置串接 ----
    "RP-SEQ-GAP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "质量记录完整性：返修次序应连续可追溯",
        "message": "返修次序断号（存在第 n 次返修却缺少前序返修记录）",
    },
    "RP-LIMIT-EXCEEDED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "GB 50236：同一位置返修次数一般不得超过 2 次",
        "message": "同一位置返修次数超过允许上限（默认 2 次，超次须技术负责人批准）",
    },
    "RP-NO-APPROVAL": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "GB/T 20801：返修应经批准并按返修工艺实施",
        "message": "返修缺少批准记录（approved 未置真或缺少批准人）",
    },
    "RP-NO-WPS": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "GB 50236：补焊应使用有效的返修 WPS",
        "message": "返修引用的 WPS 未登记或在补焊时点已失效/越界",
    },
    "RP-NO-WELDER": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "TSG Z6002：补焊焊工资格应在补焊时点有效",
        "message": "返修焊工未登记或资格在补焊时点失效/越界",
    },
    "RP-NO-REINSPECTION": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "NB/T 47013：挖补区域返修后必须复检且合格",
        "message": "返修挖补区域缺少返修后的复检记录",
    },
    "RP-REINSPECTION-REJECT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "NB/T 47013：复检不合格应再次返修并复检闭合",
        "message": "返修后复检仍不合格，且未见后续返修/复检闭合",
    },
    "RP-EXCAVATION-UNCOVERED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "NB/T 47013：原拍片未必覆盖挖补区，复检范围必须覆盖挖补位置",
        "message": "复检底片/检测范围未覆盖挖补区域（环向角度区间不覆盖），"
                   "返修前的旧底片不得作为返修合格依据",
    },
    "RP-DEFECT-NOT-EXCAVATED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "GB/T 20801 / NB/T 47013：挖补范围必须完整去除在先检测显示的缺陷",
        "message": "挖补区未完整覆盖在先不合格显示位置：仅相交不等于缺陷已清除，"
                   "残留缺陷不得判闭合（如缺陷 100°~140°，挖补仅 100°~101°）",
    },
    "RP-ORDER-INVALID": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "质量记录可追溯：缺陷发现 → 挖补补焊 → 复检的先后次序必须成立",
        "message": "返修时序倒置或缺少在先不合格依据"
                   "（补焊早于不合格底片、复检早于补焊，或无缺陷显示即返修）",
    },
}


def clause_catalog() -> list[dict[str, str]]:
    """返回条款目录列表（供 /clauses 端点）。"""
    return [
        {"code": code, **meta}
        for code, meta in sorted(CLAUSES.items())
    ]
