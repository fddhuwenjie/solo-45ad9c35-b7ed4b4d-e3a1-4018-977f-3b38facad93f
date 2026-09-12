# 承压管道焊口合规放行 REST API

后端：Python 3.10+ / FastAPI / Pydantic v2 / SQLite（标准库 `sqlite3`，无外部数据库）。

引擎按**施焊/补焊时点**匹配 WPS/PQR 与焊工资格的认可范围（有效期、材料组别、
厚度、管径、焊接方法/位置），核算检验批的**抽检与扩检覆盖率**，并沿缺陷的
**周向位置**把 `不合格显示 → 挖补返修 → 复检底片` 串成链：

- 挖补区必须**完整覆盖**在先缺陷显示（仅角度相交不算缺陷已清除）；
- 复检必须**晚于**补焊、同方法、覆盖挖补区，返修前旧底片不得充数；
- 补焊必须**晚于**在先不合格底片；无在先缺陷依据不得返修；
- 抽检失败后按规则加倍或全检，数量不足或扩检中再有未闭合缺陷则整批 hold；
- 资质失效、组别/厚度/管径越界、缺炉批、抽检不足、返修区未复检等一律保持 hold，
  响应逐口列出焊口编号与触发条款（编号与依据见 `GET /clauses`）。

复核签字后**冻结输入快照**（SHA-256），纠错只能从旧版**开修订分支**，
父版保持冻结/签发状态不被覆盖。

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

## 五、项目结构

```
app/
  clauses.py        条款目录（稳定编号 + 标准依据）
  schemas.py        Pydantic v2 请求/响应模型与引用完整性校验
  geometry.py       周向角度区间：归一化、覆盖(covers)/相交、跨0°
  qualification.py  按时点匹配 WPS/PQR、焊工资格范围
  engine.py         审查引擎：资质 + 检验批 + 返修/复检链
  storage.py        SQLite：快照冻结、版本分支、差异
  svg.py            标色 SVG 焊口图
  main.py           FastAPI 路由
examples/           三套本地样例与演示脚本
tests/              引擎与 API 回归测试
```
