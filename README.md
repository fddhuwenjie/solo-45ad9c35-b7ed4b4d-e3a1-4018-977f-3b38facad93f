# 承压管道焊口合规放行 REST API

后端：Python 3.10+ / FastAPI / Pydantic v2 / SQLite（标准库 `sqlite3`，无外部数据库）。

引擎按**施焊/补焊时点**匹配 WPS/PQR 与焊工资格的认可范围（有效期、材料组别、
厚度、管径、焊接方法/位置），按**整个检测实施时段**核验无损检测人员证书与
设备版本，核算检验批的**抽检与扩检覆盖率**，沿缺陷的
**周向位置**把 `不合格显示 → 挖补返修 → 复检底片` 串成链，并沿
**批次 → 烘干 → 保温 → 领用 → 消耗/退回/报废**核验焊材事件链：

- 挖补区必须**完整覆盖**在先缺陷显示（仅角度相交不算缺陷已清除）；
- 复检必须**晚于**补焊、同方法、覆盖挖补区，返修前旧底片不得充数；
- 补焊必须**晚于**在先不合格底片；无在先缺陷依据不得返修；
- 抽检失败后按规则加倍或全检，数量不足或扩检中再有未闭合缺陷则整批 hold；
- NDT **人员证书**（RT/UT/PT 方法、级别、产品、检测技术范围、有效期）与
  **设备版本**（序列号、校准有效期、量程/能量范围、探头/胶片等关键附件）
  必须在 `[started_at, finished_at]` 整段持续有效；夜班跨过到期点、
  实施/复核同人、设备参数或附件不匹配、引用缺失，该检测一律
  **不得计入抽检、扩检与返修复检**，并列出受影响焊口与报告；
- 资质失效、组别/厚度/管径越界、缺炉批、抽检不足、返修区未复检等一律保持 hold，
  响应逐口列出焊口编号与触发条款（编号与依据见 `GET /clauses`）。
- **焊材批次与烘干/领用链（WM 系列）**：低氢焊条领出保温筒后，批号、烘干记录与
  暴露时长常与焊口记录分开；焊材批次登记分类号、制造批号、质保书、入库状态及
  适用 WPS，烘箱/保温筒按校准版本登记并附温度时序，逐道焊口与返修引用实际
  消耗批次与领用段。核算时检查 **WPS 牌号匹配、质保书与批号一致、烘干温度与
  时长、保温连续性、最大暴露时长、重复烘干次数及数量守恒**；记录断档、设备
  校准失效、超时焊材或同一数量被重复分配时相关焊口保持 hold，并定位批次、
  领用段与事件区间/缺口；**失效焊材不得用于返修闭合**（`RP-WM-INVALID`）。
  批次/制度/设备/事件/逐口消耗为空或引用缺失属**结构缺口**，审查包不得冻结
  （`POST /review` 返回 409）或签发，只能补录后重新提交；超时/温度等
  **规则性** hold 记录齐全时可冻结，纠错从旧版派生新版本。
- **焊接道次执行链（WP 系列）**：焊口只登记 WPS 编号与完工时刻无法证明
  打底/填充/盖面各道实际遵守电流、电压、焊速与层间温度，返修混用参数也会
  被最终合格报告掩盖。WPS 版本按焊接方法冻结**极性、电流、电压、焊速、
  热输入、预热与层间温度窗口**（`process_windows`），逐焊口与逐返修提交
  道次记录（编号/层号/起止时刻/焊工/方法/极性/实测电流电压/焊缝长度/
  燃弧时间/仪表版本）与起弧前**预热/层间测温记录**（测温仪表版本）。服务按
  时间与层序重建道次，换算焊速 v=焊缝长度/燃弧时间、热输入
  E=k·U·I·60/v，核对参数窗口、逐道焊工资格与前一道后的层间温度；
  道次重号、时段重叠（含同一焊工跨焊口重叠）、层序断档、时间序倒置、
  仪表校准失效、测温采样缺失/事后补测或任一参数越限时相关焊口保持 hold，
  并定位到原始道次（`pass_id`）与测点（`measure_id`）。**返修道次链不
  合规即 `WP-REPAIR-PASS-INVALID`，该次返修不得闭合——复检 RT 合格也不
  能掩盖**。未提交道次、WPS 未冻结方法窗口、仪表版本未登记/未引用属结构
  缺口，禁止冻结（409）；参数越限等记录齐全的规则性 hold 可冻结，纠错
  派生修订版本，`/diff` 的 `weld_pass_evaluation` 呈现条款新增/撤销与
  焊口、返修范围的 invalid_to_valid 状态翻转。

复核签字后**冻结输入快照**（SHA-256，含当时的人员证书与设备版本摘要），
续证/重新校准不回写旧版；纠错只能从旧版**开修订分支**，
设备以 `equipment_id + version` 复合身份进入版本差异。

## 一、安装与启动（全新环境）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # fastapi / pydantic / uvicorn

# 启动（SQLite 默认落在 ./data/weld_release.db，可用环境变量改路径）
uvicorn app.main:app --reload --port 8000
# 交互文档： http://127.0.0.1:8000/docs
```

仅装运行时依赖即可 `import app.main` 并启动；开发/测试再装：

```bash
pip install -r requirements-dev.txt      # 额外安装 httpx、pytest
pytest -q
```

数据库路径环境变量：`WELD_DB_PATH=/srv/data/weld.db uvicorn app.main:app`。

## 二、端点一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/clauses` | 条款目录（编号、级别 hold/warning、标准依据、释义） |
| POST | `/preview` | 合规预演，**不落库**，返回完整 JSON 审查包 |
| POST | `/preview/welds/{weld_no}/svg` | 预演单焊口标色 SVG |
| POST | `/packages` | 创建审查包 v1（draft），立即冻结输入快照 |
| GET | `/packages` | 审查包列表 |
| GET | `/packages/{id}` | 取审查包（`?version=N` 可取旧版分支） |
| POST | `/packages/{id}/review` | 复核签字：冻结当前 draft |
| POST | `/packages/{id}/issue` | 签发（仅 frozen 且整包 release） |
| POST | `/packages/{id}/revisions` | 从冻结/签发旧版拉新版分支纠错 |
| GET | `/packages/{id}/versions` | 版本树（父版本、状态、签字/签发信息） |
| GET | `/packages/{id}/diff?from_version=1&to_version=2` | 两版输入快照结构化差异 |
| GET | `/packages/{id}/welds/{weld_no}/svg?version=N` | 单焊口标色 SVG 焊口图 |

## 三、快速体验

本地样例（不经过 HTTP，直接跑引擎并导出 JSON/SVG）：

```bash
pip install -r requirements-dev.txt
python examples/run_demo.py --out out/demo
```

三种场景：

1. **直接放行**：10 口批 20% 抽检 2 张合格，资质/炉批齐全 → 全部 release；
2. **缺陷扩检**：首批 W-001 在 300°~340° 出缺陷 → 挖补+复检沿位置闭合，
   加倍扩检 2 张合格 → release；对照样例只补 1 张 →
   `LT-EXTENSION-INSUFFICIENT` 整批 hold；
3. **二次返修**：一次复检仍有显示 → 二次挖补、复检覆盖挖补区合格 → release；
   对照样例二次复检漏掉挖补区尾部 → `RP-EXCAVATION-UNCOVERED` 保持 hold。
4. **NDT 资源失效**：夜班 22:00~次日 02:00 跨过实施人证书 3/6 00:00 到期点、
   另一报告换用未登记校准版本 → 两份报告被剔除，抽检数量不足整批 hold，
   触发 `NR-CERT-EXPIRED`、`NR-EQUIP-MISSING`、`NR-RECORD-EXCLUDED`。
5. **焊材链失效**：保温筒校准 3/4 到期（3/5 补焊落在失效区间）、
   3/3 段提前到 03:00 领出致施焊暴露 6h 超过 4h 上限、3/5 段数量重复分配
   （消耗+退回 > 领出）→ 仅命中段内消耗失效、返修不得闭合，触发
   `WM-CONTAINER-CALIBRATION`、`WM-EXPOSURE-EXCEEDED`、
   `WM-QTY-CONSERVATION`、`RP-WM-INVALID`；无关领用段不被牵连。
6. **道次执行链失效**：WPS 冻结 GTAW/SMAW 道次窗口后，W-007 盖面道
   电流 200A 超窗、W-008 填充道前层间温度 240℃ 超 200℃ 上限、W-009
   漏登填充道前测温、监测仪表 3/4 校准到期致 W-001 返修道次落在失效区间
   且盖面道电流 180A 超窗 → 即使返修复检 RT 合格，返修也不得闭合
   （`WP-REPAIR-PASS-INVALID`），触发 `WP-CURRENT-OUTSIDE`、
   `WP-INTERPASS-HIGH`、`WP-INTERPASS-MISSING`、`WP-GAUGE-CALIBRATION`。

### HTTP 流程示例

```bash
# 1) 预演（用任意一个样例场景的载荷）
python - <<'PY' > /tmp/payload.json
from examples.sample_data import extension_payload
import json; print(json.dumps(extension_payload().model_dump(mode="json")))
PY
curl -s -X POST localhost:8000/preview -H 'Content-Type: application/json' \
  -d @/tmp/payload.json | python -m json.tool | head -40

# 2) 落库 -> 复核 -> 签发
PID=$(curl -s -X POST localhost:8000/packages -H 'Content-Type: application/json' \
  -d @/tmp/payload.json | python -c 'import json,sys;print(json.load(sys.stdin)["package_id"])')
curl -s -X POST localhost:8000/packages/$PID/review \
  -H 'Content-Type: application/json' -d '{"reviewer":"责任工程师-李"}'
curl -s -X POST localhost:8000/packages/$PID/issue \
  -H 'Content-Type: application/json' -d '{"issuer":"质保师-赵"}'

# 3) 从冻结旧版开修订分支，再比对两版差异
curl -s -X POST localhost:8000/packages/$PID/revisions \
  -H 'Content-Type: application/json' -d @/tmp/payload.json
curl -s "localhost:8000/packages/$PID/diff?from_version=1&to_version=2"

# 4) 标色 SVG 焊口图
curl -s "localhost:8000/packages/$PID/welds/W-001/svg?version=1" -o w001.svg
```

## 四、关键判定口径

**时点匹配**：WPS（须有 PQR 支撑）与焊工证均以焊口 `welded_at`、返修以
`repaired_at` 落在认可窗口内判定；证在施焊后才生效或已过期，均不认可。
异厚/异种对接时两侧母材的组别与厚度都必须在认可范围内。

**检验批**：`sample_ratio` 按批总量上取整（且不低于 `min_samples`）。
出现不合格后，`on_reject=double` 时抽检比例翻倍，扩检批内再出不合格则升级
100%；`on_reject=full` 时直接要求全检。返修复检片（`iteration>0`）不计入
新的抽检口数。批次 hold 时批内所有焊口连带 hold。

**返修链（角度均为 0°=管顶、顺时针，支持跨 0° 区间）**：

1. 缺陷位置必须被某次挖补区**完整覆盖**（`covers`，不是相交）；
2. 补焊时刻必须**严格晚于**在先不合格底片（相等也判倒置）；
3. 同方法复检片必须**严格晚于**补焊时刻；早于补焊的底片判
   `RP-ORDER-INVALID` 且不得作为复检依据；
4. 复检覆盖并集必须**完整覆盖挖补区**，否则 `RP-EXCAVATION-UNCOVERED`
   （返修前的旧底片即使几何上覆盖挖补区也被显式排除）；
5. 同一位置返修最多 2 次，且每次均须批准、使用当时有效的返修 WPS 与焊工资格。

**无损检测资源核验（NR 系列，按整个实施时段）**：每份检测记录引用实施人员
证书（`examiner_cert_no`）、复核人员证书（`reviewer_cert_no`）、
设备版本（`equipment_uses`，主设备 `role=main` 与探头/胶片等关键附件）与
起止时刻（`started_at`/`finished_at`，缺省等于 `examined_at`）。

- 人员证书逐角色核对方法、级别（实施≥I、复核≥II）、产品类别
  （焊口 `product`，默认 `pressure_pipe`）、技术范围与有效期；
  实施与复核同一证书判 `NR-ROLE-CONFLICT`；
  **技术范围留空或记录未声明技术均不采信**（`NR-CERT-TECHNIQUE`）；
- 检测记录必须显式登记 `started_at`/`finished_at`，缺任一项即
  `NR-RECORD-PERIOD-MISSING`，不得用 `examined_at` 倒推替代；
- 设备版本核对方法适用、校准有效期**持续覆盖整段时段**（夜班跨过到期点即失效）、
  UT 量程覆盖实际声程、射线源能量范围覆盖实际曝光能量；
  **射线源必须登记能量范围、UT 主机必须登记量程**（缺规格 `NR-EQUIP-SPEC-MISSING`），
  记录必须载明实际参数（缺参数 `NR-RECORD-PARAMETER-MISSING`），不得以空值绕过；
  RT 必须有胶片/IP 附件、UT 必须有探头，附件可随设备版本 `uses` 登记
  或在检测记录上直接挂载，附件自身同样须在校准有效期内；
- 任一资源核验未通过：该报告从抽检/扩检口数、扩检触发与返修链中剔除
  （`NR-RECORD-EXCLUDED`），返修链只采信资源有效的底片，
  `nde_resources.invalid_reports` 与检验批 `excluded_reports`
  逐份列焊口、报告号、时段与失效原因码；
- 复核签字冻结资源摘要：检测记录的 `resource` 字段内嵌当时所用证书
  （证号/级别/有效期）与设备版本（编号/版本/序列号/校准期/量程能量/附件）；
  续证与重新校准产生新证书或新设备版本，旧版快照与 diff 均不被改写；
  `/diff` 的 `evaluation` 块对比两版资源核验状态：旧版 hold 因续期变 release 时，
  `old_codes` 仍保留 `NR-CERT-EXPIRED`，`resolved`/`introduced` 标注条款消长，
  `old_invalid_reports`/`new_invalid_reports` 逐份给出焊口、报告、时段、
  失效原因与所用证书/设备版本，`changed_reports` 标出 invalid_to_valid 等状态翻转。

**焊材批次与烘干领用链（WM 系列）**：载荷在 `consumable_batches`（非空即启用链）、
`consumable_rules`（按分类号的烘干/保温/暴露制度）、`consumable_containers`
（烘箱/保温筒校准版本，复合身份 `container_id+version`）、`bake_cycles`、
`quiver_stays`、`consumable_segments`、`consumable_events` 中登记；每道焊口的
`consumables` 与每次返修的 `consumables` 引用实际批次、领用段与数量。

- **批次**：须 `received_status=accepted`、有质保书且质保书批号与制造批号一致、
  分类号在 WPS 的 `consumable_classes` 清单内（牌号拿错即 `WM-WPS-CLASS-MISMATCH`）、
  批次登记适用该 WPS、分类号有烘干制度；
- **设备**：烘箱/保温筒引用版本须登记、用途匹配、校准有效期持续覆盖使用时段
  （保温筒按焊材取用时点核对，到期点之后领用失效）；
- **烘干**：温度时序须连续覆盖烘干时段（相邻读数间隔不超 `max_log_gap_minutes`）、
  温度在烘干窗口内（越限 `WM-BAKE-TEMP`）、窗口内恒温时长达标
  （`WM-BAKE-DURATION`）、同批次烘干预序号连续且不超 `max_bake_cycles`
  （`WM-REBAKE-EXCEEDED`）；
- **保温连续**：烘箱取出→保温筒装入间隔不超 `max_transfer_minutes`、领用须落在
  暂存区间内、保温温度时序连续且在保温窗口（`WM-HOLDING-GAP`/`WM-HOLDING-TEMP`）；
- **暴露**：自领用至施焊/补焊时长不超 `max_exposure_minutes`
  （默认 240 分钟，超时 `WM-EXPOSURE-EXCEEDED`），退回/报废后不得再施焊；
- **数量守恒**：消耗+退回+报废 ≤ 领出（超出即同一数量重复分配
  `WM-QTY-CONSERVATION`），未处置余量须有退回/报废闭合（`WM-QTY-OPEN`），
  保温装入不超烘干出箱、累计领用不超保温装入、累计烘干不超入库数量。
  **段级数量条款只归属实际 `segment_id`，SG-01 超配不会错指 SG-03 或连带无关焊口**；
- **返修闭合**：返修补焊消耗未通过焊材链核验即 `RP-WM-INVALID`，该次返修不得
  闭合（随附具体 WM 码）；
- **结构缺口**：批次为空，或制度/设备/烘干/保温/领用段为空，或存在领用段却无
  任何退回/报废事件、焊口/返修无逐耗、引用无法解析时，
  `consumables.freeze_blocked=true`，`POST /review` 返回 409，
  审查包不得冻结或签发；补录后重新提交。**注意**：批次为空不再被当作"链未
  启用"放行——只要链中任一组成（含焊口/返修上的逐耗）非空即启用链，批次缺失
  即完整性失败；规则性 hold（记录齐全但超时/温度越限/数量重复分配）可冻结供
  从旧版开修订分支；
- 复核冻结时每耗内嵌事件链摘要（批次/制度/烘干/保温/领用段/退回报废事件），
  `/diff` 的 `consumable_evaluation` 块对比两版：`old_codes`/`new_codes`、
  `resolved`/`introduced`、`old_invalid_uses`/`new_invalid_uses` 逐笔给出
  焊口/返修、批次、领用段、暴露时长与原因，`changed_uses` 标出
  invalid_to_valid / valid_to_invalid；生产消耗与返修消耗共用同一扁平结构。

**焊接道次执行链（WP 系列）**：WPS 在 `process_windows` 中按方法
（GTAW/SMAW…）冻结极性、电流、电压、焊速、热输入、预热与层间温度窗口；
载荷在 `weld_gauges`（电流表/电压表/测温仪/计时器/监测仪校准版本，复合身份
`gauge_id+version`）、`weld_passes`（逐焊口/逐返修的道次）与
`temperature_measurements`（起弧前预热/层间测温）中登记。

- **道次重建**：同一焊口（或返修）范围内 `pass_no` 必须唯一且自 1 连续；
  层号自 1 起逐层递增（1=打底/根焊，末层=盖面，中间=填充）；按起弧时刻重建
  时间序，相邻道次时段不得重叠；同一焊工在不同焊口/施焊与返修之间的时段
  重叠同样判 `WP-PASS-OVERLAP`；道次收弧晚于焊口/返修登记完工时刻判
  `WP-PASS-ORDER`；
- **热输入换算**：焊速 v=焊缝长度/燃弧时间（`arc_minutes` 缺省取道次起止
  时长），热输入 E=k·U·I·60/v（kJ/mm，k 为方法热效率系数）；极性、电流、
  电压、焊速、热输入逐项核对方法窗口，越限即 hold 并给出实测值与窗口；
- **焊工资格**：每道实际焊工必须与焊口/返修登记焊工一致，并按该道起弧时点
  匹配焊工资格（方法/位置/组别/厚度/管径/有效期）；
- **预热/层间温度**：首道起弧前须有 `preheat` 测温且不低于窗口下限；其余
  每道起弧前须有 `interpass` 测温且落在层间窗口；测温时点必须位于上一道
  收弧之后、本道起弧之前（事后补测判 `WP-MEASURE-LAG` 且不计入采样）；
  `pass_no` 缺省时由引擎按测温时点重建归属；
- **仪表版本**：道次监测仪表与测温仪表都必须登记并在校准有效期内，按
  整个道次时段（测温按测温时点）核对；跨到期点即 `WP-GAUGE-CALIBRATION`；
- **返修闭合**：返修补焊道次链任一不通过即 `WP-REPAIR-PASS-INVALID`
  （随附具体 WP 码），该次返修不得闭合——即使返修后复检 RT 合格，
  `RP-*` 返修链仍保持开口、整口 hold，最终合格报告无法掩盖参数混用；
- **结构缺口**：链启用（仪表/道次/测温任一非空）后，焊口或返修未提交任何
  道次（`WP-PASS-UNTRACED`）、WPS 未冻结实际方法窗口、道次/测温引用未登记
  仪表版本时，`weld_execution.freeze_blocked=true`，`POST /review` 返回
  409；记录齐全的规则性 hold（越限/断档/采样缺失/校准失效）可冻结，
  纠错从旧版派生修订；
- 复核冻结时随快照冻结 WPS 方法窗口、仪表版本、每道实测参数/换算结果/
  测温归属；`/diff` 的 `weld_pass_evaluation` 块给出 `old_codes`/`new_codes`、
  `resolved`/`introduced`、`old/new_invalid_scopes` 与 `changed_scopes`
  （invalid_to_valid / valid_to_invalid），施焊缝与返修范围共用同一结构。

## 五、项目结构

```
app/
  clauses.py        条款目录（稳定编号 + 标准依据）
  schemas.py        Pydantic v2 请求/响应模型与引用完整性校验
  geometry.py       周向角度区间：归一化、覆盖(covers)/相交、跨0°
  qualification.py  按时点匹配 WPS/PQR、焊工资格范围
  nde_resource.py   按整个实施时段核验 NDT 人员证书与设备版本
  consumables.py    焊材批次/烘干制度/烘箱保温筒/领用段/逐耗事件链核验（WM 系列）
  weld_execution.py 道次执行链：WPS 方法窗口/道次重建/热输入换算/测温/仪表（WP 系列）
  engine.py         审查引擎：焊接资格 + NDT 资源 + 焊材链 + 道次链 + 检验批 + 返修链
  storage.py        SQLite：快照冻结、版本分支、差异
  svg.py            标色 SVG 焊口图
  main.py           FastAPI 路由
examples/           三套本地样例与演示脚本
tests/              引擎与 API 回归测试
```
