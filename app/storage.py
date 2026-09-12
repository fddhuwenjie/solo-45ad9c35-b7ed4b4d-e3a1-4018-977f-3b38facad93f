"""SQLite 持久化：审查包、版本快照、签字冻结与旧版分支修订。

版本口径：
- create   -> v1 draft，落库即保存输入快照与评估结果；
- review   -> 当前 draft 版本经复核签字后冻结（frozen），输入不可再变；
- issue    -> 仅 frozen 且整包 decision=release 可签发；
- revision -> 以当前冻结版本为父版另开新版（纠错从旧版分支），
              父版行保持 frozen/issued 不被覆盖。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    package_id      TEXT PRIMARY KEY,
    package_ref     TEXT UNIQUE NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    status          TEXT NOT NULL DEFAULT 'draft',
    created_by      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    package_id       TEXT NOT NULL,
    version          INTEGER NOT NULL,
    parent_version   INTEGER,
    status           TEXT NOT NULL,            -- draft/frozen/issued/superseded
    snapshot_json    TEXT NOT NULL,            -- 规范化输入快照
    snapshot_sha256  TEXT NOT NULL,
    result_json      TEXT NOT NULL,            -- 评估结果
    decision         TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    reviewed_at      TEXT,
    reviewer         TEXT,
    review_comment   TEXT,
    issued_at        TEXT,
    issuer           TEXT,
    issue_comment    TEXT,
    PRIMARY KEY (package_id, version),
    FOREIGN KEY (package_id) REFERENCES packages(package_id)
);
"""

KEY_FIELDS = {
    "wps": "wps_no",
    "welders": "welder_id",
    "nde_personnel": "cert_no",
    # 设备版本身份是 (equipment_id, version)：同序列号换探头/重新校准派生新版本
    "nde_equipment": ("equipment_id", "version"),
    # 焊材链：烘箱/保温筒同样以 (container_id, version) 复合身份进入版本差异
    "consumable_batches": "batch_id",
    "consumable_rules": "classification",
    "consumable_containers": ("container_id", "version"),
    "bake_cycles": "bake_id",
    "quiver_stays": "stay_id",
    "consumable_segments": "segment_id",
    "consumable_events": "event_id",
    "welds": "weld_no",
    "nde": "nde_id",
    "repairs": "repair_id",
    "lot_rules": "lot_id",
}
SCALAR_SECTIONS = ("package_ref", "line_no", "submitted_by")


def _item_key(item: dict, key_field) -> str:
    """节内记录的稳定键：单字段直接取值；复合字段拼为 a/version。"""
    if isinstance(key_field, tuple):
        return "/".join(str(item[k]) for k in key_field)
    return str(item[key_field])


class StoreError(Exception):
    pass


class ConflictError(StoreError):
    """状态冲突（如对 hold 包签发、对冻结版改写）。"""


class NotFoundError(StoreError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_snapshot(payload: dict) -> bytes:
    """载荷 dict（Pydantic mode=json 结果）的规范化字节串。"""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ------------------------------------------------------------ 写入

    def create_package(self, payload_json: dict, result: dict,
                       submitted_by: Optional[str]) -> dict:
        snap = canonical_snapshot(payload_json)
        digest = sha256_hex(snap)
        ref = payload_json.get("package_ref") or f"PKG-{uuid.uuid4().hex[:10].upper()}"
        pid = "pk_" + uuid.uuid4().hex[:16]
        ts = now_iso()
        with self._lock, self._conn() as c:
            try:
                c.execute(
                    """INSERT INTO packages(package_id, package_ref, current_version,
                       status, created_by, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (pid, ref, 1, "draft", submitted_by, ts, ts),
                )
                c.execute(
                    """INSERT INTO versions(package_id, version, parent_version,
                       status, snapshot_json, snapshot_sha256, result_json,
                       decision, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (pid, 1, None, "draft", snap.decode("utf-8"), digest,
                     json.dumps(result, ensure_ascii=False, default=str),
                     result["decision"], ts),
                )
            except sqlite3.IntegrityError as e:
                raise ConflictError(f"package_ref 已存在: {ref}") from e
        return self.get_version(pid, 1)

    def review(self, package_id: str, reviewer: str, comment: Optional[str]) -> dict:
        with self._lock, self._conn() as c:
            row = self._current_row(c, package_id)
            if row["status"] != "draft":
                raise ConflictError(
                    f"当前版本 v{row['version']} 状态为 {row['status']}，"
                    "只有 draft 可复核签字"
                )
            # 焊材链结构缺口（批次/制度/设备/事件/逐口消耗为空或引用缺失）：
            # 材料链根本无法证明，禁止冻结，只能补录后重新提交；
            # 规则性 hold（超时/温度/数量）可冻结供从旧版开修订分支纠错。
            result = json.loads(row["result_json"])
            cons = result.get("consumables") or {}
            if cons.get("enabled") and cons.get("freeze_blocked"):
                gaps = (cons.get("completeness") or {}).get("gaps", [])
                raise ConflictError(
                    "焊材批次/制度/设备/事件或逐口消耗存在空项或引用缺失，"
                    "审查包不得冻结；请补录完整材料链后重新提交。缺口: "
                    + "；".join(
                        f"{g['kind']}({g.get('detail', '')})" for g in gaps[:10]
                    )
                )
            # 冻结前校验快照哈希，防库内被篡改
            digest = sha256_hex(row["snapshot_json"].encode("utf-8"))
            if digest != row["snapshot_sha256"]:
                raise ConflictError("快照哈希校验失败：输入疑似被改动，拒绝冻结")
            ts = now_iso()
            c.execute(
                """UPDATE versions SET status='frozen', reviewed_at=?, reviewer=?,
                   review_comment=? WHERE package_id=? AND version=?""",
                (ts, reviewer, comment, package_id, row["version"]),
            )
            c.execute(
                "UPDATE packages SET status='frozen', updated_at=? WHERE package_id=?",
                (ts, package_id),
            )
        return self.get_version(package_id)

    def issue(self, package_id: str, issuer: str, comment: Optional[str]) -> dict:
        with self._lock, self._conn() as c:
            row = self._current_row(c, package_id)
            if row["status"] != "frozen":
                raise ConflictError(
                    f"当前版本 v{row['version']} 为 {row['status']}，"
                    "须先复核冻结(frozen)才能签发"
                )
            if row["decision"] != "release":
                raise ConflictError(
                    "整包存在 hold 焊口，不得签发；请整改后从当前版本开修订分支"
                )
            ts = now_iso()
            c.execute(
                """UPDATE versions SET status='issued', issued_at=?, issuer=?,
                   issue_comment=? WHERE package_id=? AND version=?""",
                (ts, issuer, comment, package_id, row["version"]),
            )
            c.execute(
                "UPDATE packages SET status='issued', updated_at=? WHERE package_id=?",
                (ts, package_id),
            )
        return self.get_version(package_id)

    def revise(self, package_id: str, payload_json: dict, result: dict) -> dict:
        """从当前（冻结/签发）版本拉出新版本；父版永不被覆盖。"""
        with self._lock, self._conn() as c:
            parent = self._current_row(c, package_id)
            if parent["status"] not in ("frozen", "issued"):
                raise ConflictError(
                    "仅已复核冻结/已签发的版本允许开修订分支；"
                    f"当前为 {parent['status']}"
                )
            new_version = parent["version"] + 1
            snap = canonical_snapshot(payload_json)
            digest = sha256_hex(snap)
            ts = now_iso()
            # 父版行保持冻结状态作为历史；packages.current_version 前移
            c.execute(
                """INSERT INTO versions(package_id, version, parent_version,
                   status, snapshot_json, snapshot_sha256, result_json,
                   decision, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (package_id, new_version, parent["version"], "draft",
                 snap.decode("utf-8"), digest,
                 json.dumps(result, ensure_ascii=False, default=str),
                 result["decision"], ts),
            )
            c.execute(
                """UPDATE packages SET current_version=?, status='draft',
                   updated_at=? WHERE package_id=?""",
                (new_version, ts, package_id),
            )
        return self.get_version(package_id, new_version)

    # ------------------------------------------------------------ 读取

    def _row(self, c, package_id: str, version: Optional[int]) -> sqlite3.Row:
        if version is None:
            row = c.execute(
                "SELECT * FROM packages WHERE package_id=?", (package_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"审查包不存在: {package_id}")
            version = row["current_version"]
        vrow = c.execute(
            "SELECT * FROM versions WHERE package_id=? AND version=?",
            (package_id, version),
        ).fetchone()
        if vrow is None:
            raise NotFoundError(f"版本不存在: {package_id} v{version}")
        return vrow

    def _current_row(self, c, package_id: str) -> sqlite3.Row:
        prow = c.execute(
            "SELECT * FROM packages WHERE package_id=?", (package_id,)
        ).fetchone()
        if prow is None:
            raise NotFoundError(f"审查包不存在: {package_id}")
        return self._row(c, package_id, prow["current_version"])

    def get_version(self, package_id: str, version: Optional[int] = None) -> dict:
        with self._conn() as c:
            vrow = self._row(c, package_id, version)
            prow = c.execute(
                "SELECT * FROM packages WHERE package_id=?", (package_id,)
            ).fetchone()
        return self._hydrate(vrow, prow)

    def get_by_ref(self, ref: str) -> Optional[dict]:
        with self._conn() as c:
            prow = c.execute(
                "SELECT * FROM packages WHERE package_ref=?", (ref,)
            ).fetchone()
            if prow is None:
                return None
            vrow = self._row(c, prow["package_id"], None)
        return self._hydrate(vrow, prow)

    def list_packages(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT p.*, v.decision FROM packages p
                   JOIN versions v ON v.package_id=p.package_id
                      AND v.version=p.current_version
                   ORDER BY p.created_at"""
            ).fetchall()
        return [{
            "package_id": r["package_id"],
            "package_ref": r["package_ref"],
            "current_version": r["current_version"],
            "status": r["status"],
            "decision": r["decision"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        } for r in rows]

    def list_versions(self, package_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT version, parent_version, status, decision, created_at,
                          reviewed_at, reviewer, issued_at, issuer, snapshot_sha256
                   FROM versions WHERE package_id=? ORDER BY version""",
                (package_id,),
            ).fetchall()
        if not rows:
            raise NotFoundError(f"审查包不存在: {package_id}")
        return [dict(r) for r in rows]

    def _hydrate(self, vrow: sqlite3.Row, prow: sqlite3.Row) -> dict:
        return {
            "package_id": vrow["package_id"],
            "package_ref": prow["package_ref"],
            "version": vrow["version"],
            "parent_version": vrow["parent_version"],
            "status": vrow["status"],
            "decision": vrow["decision"],
            "snapshot": json.loads(vrow["snapshot_json"]),
            "snapshot_sha256": vrow["snapshot_sha256"],
            "result": json.loads(vrow["result_json"]),
            "created_at": vrow["created_at"],
            "reviewed_at": vrow["reviewed_at"],
            "reviewer": vrow["reviewer"],
            "review_comment": vrow["review_comment"],
            "issued_at": vrow["issued_at"],
            "issuer": vrow["issuer"],
            "issue_comment": vrow["issue_comment"],
            "current_version": prow["current_version"],
            "package_status": prow["status"],
        }


# ================================================================ 版本差异


def _flat_values(obj: Any, prefix: str = "") -> dict[str, Any]:
    """把嵌套 dict 摊平为 点分路径 -> 值；list 整体作为叶子。"""
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                out.update(_flat_values(v, path))
            else:
                out[path] = v
    else:
        out[prefix] = obj
    return out


def diff_snapshots(old: dict, new: dict) -> list[dict]:
    """结构化对比两个快照，返回 DiffEntry 形态的列表。"""
    changes: list[dict] = []

    for sec in SCALAR_SECTIONS:
        if old.get(sec) != new.get(sec):
            changes.append({"kind": "changed", "section": sec,
                            "old": old.get(sec), "new": new.get(sec)})

    for sec, key_field in KEY_FIELDS.items():
        old_items = {_item_key(item, key_field): item
                     for item in old.get(sec, [])}
        new_items = {_item_key(item, key_field): item
                     for item in new.get(sec, [])}
        for key in sorted(set(new_items) - set(old_items)):
            changes.append({"kind": "added", "section": sec, "key": key,
                            "new": new_items[key]})
        for key in sorted(set(old_items) - set(new_items)):
            changes.append({"kind": "removed", "section": sec, "key": key,
                            "old": old_items[key]})
        for key in sorted(set(old_items) & set(new_items)):
            of = _flat_values(old_items[key])
            nf = _flat_values(new_items[key])
            for path in sorted(set(of) | set(nf)):
                if of.get(path) != nf.get(path):
                    changes.append({
                        "kind": "changed", "section": sec, "key": key,
                        "field": path, "old": of.get(path), "new": nf.get(path),
                    })
    return changes


def _nde_report_states(pkg: dict) -> dict[str, dict]:
    """从冻结评估结果抽取每份报告的资源核验状态（含失效详情与资源版本）。"""
    result = pkg.get("result", {})
    states: dict[str, dict] = {}
    for w in result.get("welds", []):
        for n in w.get("nde", []):
            res = n.get("resource") or {}
            states[n["nde_id"]] = {
                "nde_id": n["nde_id"],
                "report_no": n.get("report_no"),
                "weld_no": n.get("weld_no", w["weld_no"]),
                "method": n.get("method"),
                "iteration": n.get("iteration", 0),
                "resource_valid": bool(n.get("resource_valid", True)),
                "period": res.get("period"),
                "reasons": sorted({f.get("code") for f in res.get("failures", [])
                                   if f.get("code")}),
                "examiner_cert_no": (res.get("examiner") or {}).get("cert_no"),
                "reviewer_cert_no": (res.get("reviewer") or {}).get("cert_no"),
                "equipment": [
                    {"equipment_id": e.get("equipment_id"),
                     "version": e.get("version"), "role": e.get("role"),
                     "serial_no": e.get("serial_no"),
                     "calibrated_from": e.get("calibrated_from"),
                     "calibrated_to": e.get("calibrated_to")}
                    for e in res.get("equipment", [])
                ],
            }
    return states


def _nde_evaluation(old_pkg: dict, new_pkg: dict) -> dict:
    """对比两版 NDT 资源核验结论：保留旧版失效条款/报告并呈现状态变化。"""
    old_result, new_result = old_pkg["result"], new_pkg["result"]
    old_codes = sorted(old_result.get("clauses_triggered", []))
    new_codes = sorted(new_result.get("clauses_triggered", []))
    old_nr = {c for c in old_codes if c.startswith("NR-")}
    new_nr = {c for c in new_codes if c.startswith("NR-")}

    old_states = _nde_report_states(old_pkg)
    new_states = _nde_report_states(new_pkg)

    def _invalid(states: dict[str, dict]) -> list[dict]:
        return [s for s in states.values() if not s["resource_valid"]]

    changed_reports: list[dict] = []
    for nde_id in sorted(set(old_states) | set(new_states)):
        o, n = old_states.get(nde_id), new_states.get(nde_id)
        ov = o["resource_valid"] if o else None
        nv = n["resource_valid"] if n else None
        if ov == nv:
            continue
        changed_reports.append({
            "nde_id": nde_id,
            "change": "valid_to_invalid" if ov and not nv
            else ("invalid_to_valid" if not ov and nv else "presence_changed"),
            "old": o,
            "new": n,
        })

    return {
        "old_decision": old_result.get("decision"),
        "new_decision": new_result.get("decision"),
        "old_codes": old_codes,
        "new_codes": new_codes,
        # 已消除/新引入的 NR 条款：旧版 NR-CERT-EXPIRED 仍保留在 old_codes
        "resolved": sorted(old_nr - new_nr),
        "introduced": sorted(new_nr - old_nr),
        "old_invalid_reports": sorted(
            _invalid(old_states), key=lambda s: s["nde_id"]),
        "new_invalid_reports": sorted(
            _invalid(new_states), key=lambda s: s["nde_id"]),
        "changed_reports": changed_reports,
    }


def _wm_use_states(pkg: dict) -> dict[str, dict]:
    """从冻结评估结果抽取每次焊材消耗的事件链核验状态（供版本差异呈现）。

    生产消耗与返修消耗在引擎中已是同一扁平结构（chain 为内嵌事件链摘要），
    此处统一读取，旧版/未启用焊材链时返回空映射。
    """
    result = pkg.get("result", {})
    states: dict[str, dict] = {}
    if not result.get("consumables", {}).get("enabled"):
        return states

    def _add(weld_no: str, u: dict, *, scope: str, repair_id=None,
             iteration=None) -> None:
        chain = u.get("chain") or {}
        states[u["use_id"]] = {
            "use_id": u["use_id"],
            "weld_no": weld_no,
            "scope": scope,
            "repair_id": repair_id,
            "iteration": iteration,
            "batch_id": u.get("batch_id")
            or (chain.get("batch") or {}).get("batch_id"),
            "segment_id": u.get("segment_id")
            or (chain.get("segment") or {}).get("segment_id"),
            "qty_kg": u.get("qty_kg", chain.get("qty_kg")),
            "consumable_valid": bool(u.get("consumable_valid", True)),
            "reasons": list(u.get("reasons", [])),
            "chain": chain,
        }

    for w in result.get("welds", []):
        for u in w.get("consumable_uses", []):
            _add(w["weld_no"], u, scope="production")
        for link in w.get("repairs", []):
            for u in link.get("consumable_uses", []):
                _add(w["weld_no"], u, scope="repair",
                     repair_id=link["repair_id"],
                     iteration=link["iteration"])
    return states


def _wm_evaluation(old_pkg: dict, new_pkg: dict) -> Optional[dict]:
    """对比两版焊材链核验结论：保留旧版失效消耗并呈现批次/领用段状态变化。

    预演、版本差异与 JSON 审查包共用同一判定，故两侧均未启用焊材链时返回 None。
    """
    old_cons = old_pkg.get("result", {}).get("consumables", {})
    new_cons = new_pkg.get("result", {}).get("consumables", {})
    if not old_cons.get("enabled") and not new_cons.get("enabled"):
        return None
    old_result, new_result = old_pkg["result"], new_pkg["result"]
    old_codes = [c for c in old_result.get("clauses_triggered", [])
                 if c.startswith("WM-") or c == "RP-WM-INVALID"]
    new_codes = [c for c in new_result.get("clauses_triggered", [])
                 if c.startswith("WM-") or c == "RP-WM-INVALID"]
    old_states = _wm_use_states(old_pkg)
    new_states = _wm_use_states(new_pkg)

    changed: list[dict] = []
    for use_id in sorted(set(old_states) | set(new_states)):
        o, n = old_states.get(use_id), new_states.get(use_id)
        ov = o["consumable_valid"] if o else None
        nv = n["consumable_valid"] if n else None
        if ov == nv:
            continue
        changed.append({
            "use_id": use_id,
            "change": "valid_to_invalid" if ov and not nv
            else ("invalid_to_valid" if not ov and nv else "presence_changed"),
            "old": o, "new": n,
        })

    def _invalid(states: dict[str, dict]) -> list[dict]:
        # 保留 chain 事件链摘要（批次/烘干/保温/领用段定位），与 NDE 差异
        # 保留资源摘要同口径，便于失效定位与外部审查。
        return [dict(s) for s in sorted(states.values(),
                                        key=lambda s: s["use_id"])
                if not s["consumable_valid"]]

    return {
        "old_decision": old_result.get("decision"),
        "new_decision": new_result.get("decision"),
        "old_codes": sorted(old_codes),
        "new_codes": sorted(new_codes),
        "resolved": sorted(set(old_codes) - set(new_codes)),
        "introduced": sorted(set(new_codes) - set(old_codes)),
        "old_invalid_uses": _invalid(old_states),
        "new_invalid_uses": _invalid(new_states),
        "changed_uses": changed,
    }


def diff_versions(old_pkg: dict, new_pkg: dict) -> dict:
    changes = diff_snapshots(old_pkg["snapshot"], new_pkg["snapshot"])
    return {
        "package_id": old_pkg["package_id"],
        "from_version": old_pkg["version"],
        "to_version": new_pkg["version"],
        "changes": changes,
        "decision_changed": old_pkg["decision"] != new_pkg["decision"],
        "old_decision": old_pkg["decision"],
        "new_decision": new_pkg["decision"],
        "evaluation": _nde_evaluation(old_pkg, new_pkg),
        "consumable_evaluation": _wm_evaluation(old_pkg, new_pkg),
    }
