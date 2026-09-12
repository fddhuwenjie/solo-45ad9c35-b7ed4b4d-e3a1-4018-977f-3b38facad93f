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
- WM 系列：焊材批次、烘干/保温/领用链（Welding Material）。
- WP 系列：焊接道次执行链（Welding Pass）：WPS 版本冻结各方法参数窗口，
  逐道次登记起止时刻、焊工、方法、实测电流/电压/焊速、焊缝长度、测温记录与
  仪表版本；按时间与层序重建道次并换算热输入，合格报告不得掩盖道次违规与
  返修混用参数。
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
    CONSUMABLE = "consumable"
    WELD_EXECUTION = "weld_execution"


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
        "message": "实际检测技术超出人员证书认可的检测技术范围"
                   "（证书技术范围留空不能采信）",
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
    "NR-RECORD-PERIOD-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "质量记录完整性：检测报告应如实记录实施起止时刻，"
                     "不得仅以报告时刻倒推替代",
        "message": "检测记录缺少实施开始/结束时刻（started_at/finished_at），"
                   "无法证明整个实施时段持续有效，不得以 examined_at 替代",
    },
    "NR-EQUIP-SPEC-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：探伤仪应登记有效量程、射线源应登记能量范围，"
                     "能力参数缺失的设备版本不得放行",
        "message": "设备版本缺少与方法对应的能力规格"
                   "（射线源未登记能量范围，或 UT 主机未登记量程）",
    },
    "NR-RECORD-PARAMETER-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.NDE.value,
        "reference": "NB/T 47013：检测记录应载明实际工艺参数"
                     "（UT 声程/壁厚、RT 曝光能量），缺失即无法核对设备匹配",
        "message": "检测记录缺少与方法对应的实际参数"
                   "（UT 未登记 applied_thickness_mm 或 RT 未登记 "
                   "exposure_energy_kev），不得以空值绕过设备能力核对",
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
    "RP-WM-INVALID": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "GB 50236 / GB/T 20801：返修补焊所用焊材必须合规；"
                     "超时、断档、校准失效或已报废焊材不得用于返修闭合",
        "message": "返修补焊消耗的焊材未通过批次/烘干/保温/领用链核验，"
                   "失效焊材不得用于返修闭合（具体 WM 条款随附）",
    },
    # ---- 焊材批次与烘干/保温/领用链（WM 系列）----
    # 低氢焊条（GB/T 5117 E5015 类）出厂后经"入库验收 → 烘干 → 保温筒暂存 →
    # 领出 → 施焊/退回/报废"流转；任一环节断档、超时、数量对不上，
    # 合格底片也不能证明施焊材料合规，相关焊口保持 hold。
    "WM-CONSUMABLE-UNTRACED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236 / GB/T 20801：焊条、焊丝应有可追溯的批号、"
                     "烘干与领用记录，做到随用随领、账物相符",
        "message": "施焊/补焊未引用实际消耗的焊材批次与领用段"
                   "（焊材链已启用时不得出现无出处焊材）",
    },
    "WM-BATCH-NOT-RECEIVED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：焊材须经验收合格入库后方可发放使用",
        "message": "焊材批次未经验收合格入库（received_status 非 accepted），不得领用",
    },
    "WM-CERT-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB/T 20801 / 质量体系：焊材质量证明书（质保书）应随批可查",
        "message": "焊材批次缺质保书，或质保书批号与制造批号不一致",
    },
    "WM-WPS-CLASS-MISMATCH": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236 / NB/T 47014：WPS 规定的焊材分类号（牌号类别）"
                     "应与实际使用焊材一致",
        "message": "实际消耗焊材分类号不在该 WPS 规定的焊材分类号清单内（牌号拿错）",
    },
    "WM-BATCH-WPS-NOT-APPLICABLE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量体系：焊材批次登记的适用 WPS 范围应覆盖实际 WPS",
        "message": "焊材批次未登记适用于施焊/返修所用 WPS",
    },
    "WM-RULE-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：焊条烘干温度、保温时间与重复烘干次数应在"
                     "烘干制度中事先规定",
        "message": "焊材分类号未登记烘干制度（烘干温度/时长/暴露时限/重复次数无据可核）",
    },
    "WM-CONTAINER-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量记录完整性：烘干/保温记录应可追溯所用烘箱、保温筒的"
                     "具体校准版本",
        "message": "烘干周期或保温暂存引用的烘箱/保温筒校准版本未登记（引用缺失）",
    },
    "WM-CONTAINER-KIND": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：焊条烘干应在烘干箱内进行，领出后应置于通电保温筒",
        "message": "设备用途不匹配（烘干记录挂在保温筒上，或保温记录挂在烘箱上）",
    },
    "WM-CONTAINER-CALIBRATION": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "计量体系：烘箱、保温筒的测温/控温装置应在校准有效期内使用；"
                     "跨到期点的时段不得整体采信",
        "message": "烘箱/保温筒校准有效期未持续覆盖使用时段（含跨到期点）",
    },
    "WM-BAKE-TEMP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236 / 焊材说明书：低氢焊条烘干升温与恒温温度应符合"
                     "烘干制度（如 350~400℃），温度时序须持续在窗口内",
        "message": "烘干温度时序存在越限读数（低于烘干下限或高于上限），该烘干周期无效",
    },
    "WM-BAKE-DURATION": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：焊条在规定烘干温度下的恒温时间不得短于烘干制度",
        "message": "规定烘干温度窗口内的恒温时长不足（保温时间不够即取出）",
    },
    "WM-REBAKE-EXCEEDED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：低氢焊条重复烘干次数不宜超过规定（默认最多 1 次返烘，"
                     "即烘干周期序号不得大于烘干制度上限）",
        "message": "焊材重复烘干次数超过烘干制度允许上限（反复烘干判废，不得再领出）",
    },
    "WM-HOLDING-GAP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：烘干后焊条应保存在 100~150℃ 保温筒内随用随取；"
                     "烘干→保温→领出的时序与交接时点必须连续",
        "message": "保温连续性断档：烘干取出到保温装入超时限、使用/领用落在保温"
                   "暂存区间之外，或温度时序存在未覆盖缺口",
    },
    "WM-HOLDING-TEMP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：保温筒温度应维持在烘干制度规定的保温温度窗口内",
        "message": "保温筒温度时序存在越限读数（低于保温下限即等同暴露吸潮）",
    },
    "WM-EXPOSURE-EXCEEDED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：低氢焊条在大气中暴露时间不得超过规定"
                     "（默认单次领用暴露上限 4 小时），超时应退回重新烘干或报废",
        "message": "焊材自保温筒领出后的暴露时长超过烘干制度规定的最大暴露时长，"
                   "仍继续用于施焊",
    },
    "WM-SEGMENT-ORDER": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量记录可追溯：领用 → 施焊消耗 → 退回/报废的先后次序必须成立",
        "message": "领用段时序倒置（退回/报废早于领用、施焊发生在退回之后）",
    },
    "WM-SEGMENT-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量记录完整性：每道焊口/返修应能定位到具体的焊材领用段",
        "message": "消耗记录引用的领用段未登记（领用单/保温筒事件缺失，无法定位出处）",
    },
    "WM-CHAIN-GAP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量记录完整性：批号 → 烘干 → 保温 → 领用 → 退回/报废"
                     "事件链应连续闭合",
        "message": "焊材事件链断档：批次未烘干/未入保温即被领用，或领用段未挂接"
                   "在任何保温暂存事件下",
    },
    "WM-QTY-CONSERVATION": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量体系：焊材发放、回收、报废数量应与消耗相符（账物相符），"
                     "同一数量不得重复分配",
        "message": "数量守恒失败：领用段的消耗+退回+报废数量超出领出数量"
                   "（同一数量被重复分配）",
    },
    "WM-QTY-OPEN": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量体系：领用焊材未消耗部分应办理退库或报废，领用单必须闭合",
        "message": "领用段未闭合：领出数量与消耗+退回+报废不符且无退回/报废事件",
    },
    "WM-QTY-LIMIT": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "质量体系：烘干/保温容量与发放数量应一致，装入数量不得"
                     "凭空放大",
        "message": "数量守恒失败：领用累计超出保温装入数量，或保温装入超出烘干"
                   "出箱数量",
    },
    "WM-SCRAP-EXCESS": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.CONSUMABLE.value,
        "reference": "GB 50236：超过暴露时限或返烘次数的焊条应报废，报废数量"
                     "不得超出该领用段可处置数量",
        "message": "报废数量超出领用段内可处置数量（报废事件与领用数量对不上）",
    },
    # ---- 焊接道次执行链（WP 系列）----
    # 焊口只登记 WPS 编号和完工时刻无法证明打底/填充/盖面实际遵守电流、电压、
    # 焊速与层间温度；返修混用参数也会被最终合格报告掩盖。逐焊口与返修提交
    # 道次链（编号/起止时刻/焊工/方法/实测参数/焊缝长度/测温记录/仪表版本），
    # 服务按时间与层序重建道次、换算热输入并逐项核对；任一不满足，相关焊口
    # 保持 hold，并定位到原始道次与测点。
    "WP-PASS-UNTRACED": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236 / GB/T 20801：焊接施工记录应逐道次记载施焊人、"
                     "时间与实际工艺参数，做到道次可追溯",
        "message": "施焊/补焊未提交任何道次记录（道次链启用时不得仅有 WPS 编号"
                   "与完工时刻）",
    },
    "WP-PASS-DUP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "质量记录可追溯：同一焊口/返修范围内道次编号应唯一",
        "message": "同一焊口（或返修）范围内道次编号重号",
    },
    "WP-PASS-OVERLAP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "质量记录可追溯：同一焊工/同一焊口的道次施焊时段不得重叠",
        "message": "道次施焊时段相互重叠（时间序无法重建）",
    },
    "WP-LAYER-GAP": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：焊缝应按打底(根焊)→填充→盖面的层序逐道施焊，"
                     "层序应连续",
        "message": "道次层序断档：层号不自 1 连续或道次时间序与层序不一致",
    },
    "WP-PASS-ORDER": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "质量记录可追溯：各道次起止时刻应严格递增，结束时刻应晚于"
                     "开始时刻",
        "message": "道次时间序倒置或时段无效（止不晚于起，或后道早于前道）",
    },
    "WP-PASS-WINDOW-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "NB/T 47014 / GB 50236：WPS 应按焊接方法冻结极性、电流、"
                     "电压、热输入、预热与层间温度范围",
        "message": "所用 WPS 未冻结该焊接方法的参数窗口（无据可核实际参数）",
    },
    "WP-POLARITY": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：实际极性应与 WPS 规定一致",
        "message": "道次实际极性不在 WPS 该方法允许极性内",
    },
    "WP-CURRENT-OUTSIDE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：焊接电流应落在 WPS 规定范围内",
        "message": "道次实测电流超出 WPS 该方法窗口",
    },
    "WP-VOLTAGE-OUTSIDE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：电弧电压应落在 WPS 规定范围内",
        "message": "道次实测电压超出 WPS 该方法窗口",
    },
    "WP-TRAVEL-OUTSIDE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：焊接速度（由焊缝长度/燃弧时间换算）应落在 WPS "
                     "规定范围内",
        "message": "道次焊速超出 WPS 该方法窗口",
    },
    "WP-HEATINPUT-OUTSIDE": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "NB/T 47014：热输入 E=k·U·I·60/v 应落在 WPS 认可范围内，"
                     "返修不得混用超窗口参数",
        "message": "道次换算热输入超出 WPS 该方法热输入上限（或低于规定下限）",
    },
    "WP-PARAM-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "质量记录完整性：道次记录应载明电流、电压、焊缝长度等实际"
                     "工艺参数，缺失即无法核对窗口",
        "message": "道次实测参数缺失（电流/电压/焊缝长度，或换算焊速所需燃弧"
                   "时间不足），不得以空值绕过参数核对",
    },
    "WP-WELDER-MISMATCH": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "TSG Z6002：逐道次施焊焊工应与焊口登记焊工一致并在其资格"
                     "项目内施焊",
        "message": "道次施焊焊工与焊口/返修登记焊工不一致，或未登记",
    },
    "WP-PREHEAT-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：首道（根焊）起弧前应测温并记录预热温度；"
                     "即使 WPS 预热下限为 0（不强制预热），仍须有可追溯的"
                     "起弧前温度测点证明",
        "message": "首道起弧前缺少预热温度测温记录（采样缺失；预热下限为 0 "
                   "也不得省略）",
    },
    "WP-PREHEAT-LOW": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：预热温度不得低于 WPS 规定下限",
        "message": "首道起弧前预热温度低于 WPS 规定下限",
    },
    "WP-PREHEAT-HIGH": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：预热温度不得高于 WPS 规定上限（过热同样改变"
                     "焊接热循环）",
        "message": "首道起弧前预热温度高于 WPS 规定上限",
    },
    "WP-INTERPASS-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：多层多道焊每道起弧前应测温并记录层间温度",
        "message": "道次起弧前缺少层间温度测温记录（采样缺失，无法证明符合层间"
                   "温度要求）",
    },
    "WP-INTERPASS-HIGH": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "GB 50236：层间温度不得高于 WPS 规定上限（也不得低于规定"
                     "下限）",
        "message": "道次起弧前层间温度超出 WPS 窗口（过高或过低）",
    },
    "WP-GAUGE-MISSING": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "计量体系/质量记录完整性：道次参数与测温应可追溯所用电流表、"
                     "电压表、焊速计时与测温仪表的具体校准版本",
        "message": "道次或测温记录引用的仪表版本未登记（引用缺失）",
    },
    "WP-GAUGE-CALIBRATION": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "计量体系：电流表/电压表/测温仪表应在校准有效期内使用；"
                     "跨到期点的施焊时段不得整体采信",
        "message": "仪表校准有效期未持续覆盖道次施焊/测温时点（校准失效）",
    },
    "WP-GAUGE-KIND": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "计量体系：测量仪表类别应与测量用途匹配——测温仪只能"
                     "证明温度，电流/电压/焊速须由对应类别（电流表/电压表/"
                     "计时器或焊接参数监测仪）且校准有效的仪表证明",
        "message": "道次缺少与测量用途类别匹配且校准有效的参数仪表"
                   "（thermometer 不能单独证明电流、电压或焊速）",
    },
    "WP-MEASURE-LAG": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.WELD_EXECUTION.value,
        "reference": "质量记录可追溯：测温时点应位于上一道结束与本道起弧之间，"
                     "不得事后补测",
        "message": "测温记录时点不在其所对应道次的起弧前窗口内（事后补测或"
                   "时序倒置）",
    },
    "WP-REPAIR-PASS-INVALID": {
        "severity": Severity.HOLD.value,
        "category": ClauseCategory.REPAIR.value,
        "reference": "GB 50236 / GB/T 20801：返修补焊道次必须按返修 WPS 窗口"
                     "施焊；混用打底/填充/盖面参数或超窗口参数不得被最终合格"
                     "报告掩盖",
        "message": "返修道次链核验未通过（参数越限/层序/测温/仪表等具体 WP "
                   "条款随附），该次返修不得闭合",
    },
}


def clause_catalog() -> list[dict[str, str]]:
    """返回条款目录列表（供 /clauses 端点）。"""
    return [
        {"code": code, **meta}
        for code, meta in sorted(CLAUSES.items())
    ]
