"""安全信号处置域。

三层分开维护：
- 信号数据：``signals`` / ``signal_decisions`` / ``signal_actions`` 表（见 SIGNAL_SCHEMA）；
- 判定规则：:class:`SignalRules`，阈值集中维护，不与数据、页面耦合；
- 页面操作：由 ``app.py`` 路由和 ``static/signals.html`` 调用本模块函数。

归并口径：按 product + event_term 归一化后分组，只统计有效案例
（``status != 'merged'``）。已合并来源案例通过 ``merged_into`` 跟随目标案例，
其 intakes 在合并时已迁移到目标案例，因此来源案例不再单独计数，避免重复计数。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
import __main__
from typing import Any

# python app.py 启动时 app 以 __main__ 加载；必须复用同一模块实例，
# 否则异常类身份不同，路由层的 except ApiError 无法捕获。
if hasattr(__main__, "ApiError") and getattr(__main__, "__name__", "") == "__main__" and getattr(
        __main__, "__file__", "").endswith("app.py"):
    _app = __main__
else:
    import app as _app

ApiError = _app.ApiError
iso = _app.iso
parse_time = _app.parse_time
utcnow = _app.utcnow

VIEW_ROLES = {"regional_lead", "medical_reviewer", "global_admin"}
SIGNAL_STATUSES = {"open", "confirmed", "rejected"}

SIGNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_no TEXT NOT NULL UNIQUE,
    product TEXT NOT NULL,
    event_term TEXT NOT NULL,
    product_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    case_count INTEGER NOT NULL,
    serious_count INTEGER NOT NULL DEFAULT 0,
    fatal_count INTEGER NOT NULL DEFAULT 0,
    regions_json TEXT NOT NULL DEFAULT '[]',
    trigger_reasons_json TEXT NOT NULL DEFAULT '[]',
    first_detected_at TEXT NOT NULL,
    last_detected_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(product_key, event_key)
);
CREATE TABLE IF NOT EXISTS signal_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    decision TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS signal_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER NOT NULL REFERENCES signals(id),
    measure TEXT NOT NULL,
    owner TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    registered_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    completed_by TEXT
);
"""


def init_signal_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SIGNAL_SCHEMA)


@dataclass(frozen=True)
class SignalRules:
    """信号判定规则，唯一可调阈值的地方：达到例数或出现死亡即建立信号。"""

    min_cases: int = 3
    fatal_trigger: bool = True

    def evaluate(self, *, case_count: int, fatal_count: int) -> list[str]:
        reasons: list[str] = []
        if case_count >= self.min_cases:
            reasons.append("case_threshold")
        if self.fatal_trigger and fatal_count >= 1:
            reasons.append("fatal")
        return reasons


DEFAULT_RULES = SignalRules()

GROUP_SQL = """
    SELECT lower(trim(product)) AS product_key,
           lower(trim(event_term)) AS event_key,
           min(trim(product)) AS product,
           min(trim(event_term)) AS event_term,
           count(*) AS case_count,
           sum(serious) AS serious_count,
           sum(fatal) AS fatal_count,
           group_concat(DISTINCT region) AS regions
    FROM cases
    WHERE status != 'merged'
    GROUP BY 1, 2
"""


def _signal(conn: sqlite3.Connection, signal_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
    if not row:
        raise ApiError(404, "signal_not_found", "信号不存在")
    return row


def _regions(signal: sqlite3.Row | dict[str, Any]) -> list[str]:
    return json.loads(signal["regions_json"])


def _require_view(signal: sqlite3.Row, role: str, region: str) -> None:
    if role not in VIEW_ROLES:
        raise ApiError(403, "signal_forbidden", "当前角色无权查看安全信号")
    if role == "regional_lead" and region not in _regions(signal):
        raise ApiError(403, "region_forbidden", "只能查看本区域涉及的信号")


def _action_dict(row: sqlite3.Row, now: datetime) -> dict[str, Any]:
    result = dict(row)
    result["overdue"] = bool(not result["completed_at"] and parse_time(result["due_at"]) < now)
    return result


def _decisions(conn: sqlite3.Connection, signal_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT id,decision,rationale,reviewer,created_at FROM signal_decisions WHERE signal_id=? ORDER BY id",
        (signal_id,),
    )]


def _actions(conn: sqlite3.Connection, signal_id: int) -> list[dict[str, Any]]:
    now = utcnow()
    rows = conn.execute(
        "SELECT * FROM signal_actions WHERE signal_id=? ORDER BY id", (signal_id,)
    ).fetchall()
    return [_action_dict(r, now) for r in rows]


def _signal_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    decisions = _decisions(conn, result["id"])
    result["latest_decision"] = decisions[-1] if decisions else None
    result["actions"] = _actions(conn, result["id"])
    result["has_overdue"] = any(a["overdue"] for a in result["actions"])
    return result


def scan(repo: Any, actor: str, role: str, rules: SignalRules = DEFAULT_RULES) -> dict[str, Any]:
    """按现行规则扫描全部产品+事件词组合；重复扫描不产生第二份信号，只刷新统计。"""
    if role not in {"medical_reviewer", "global_admin"}:
        raise ApiError(403, "scan_forbidden", "只有医学审核员或全局管理员可以执行信号检查")
    created: list[dict[str, Any]] = []
    refreshed: list[int] = []
    with repo.tx() as conn:
        groups = conn.execute(GROUP_SQL).fetchall()
        now = iso()
        for group in groups:
            stats = {
                "case_count": group["case_count"],
                "fatal_count": group["fatal_count"],
            }
            reasons = rules.evaluate(**stats)
            if not reasons:
                continue
            regions = sorted({r for r in (group["regions"] or "").split(",") if r})
            existing = conn.execute(
                "SELECT id FROM signals WHERE product_key=? AND event_key=?",
                (group["product_key"], group["event_key"]),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE signals SET case_count=?,serious_count=?,fatal_count=?,
                       regions_json=?,trigger_reasons_json=?,last_detected_at=? WHERE id=?""",
                    (group["case_count"], group["serious_count"], group["fatal_count"],
                     json.dumps(regions, ensure_ascii=False), json.dumps(reasons), now, existing["id"]),
                )
                refreshed.append(existing["id"])
                continue
            seq = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] + 1
            signal_no = f"SIG-{utcnow().year}-{seq:06d}"
            cursor = conn.execute(
                """INSERT INTO signals(signal_no,product,event_term,product_key,event_key,status,
                   case_count,serious_count,fatal_count,regions_json,trigger_reasons_json,
                   first_detected_at,last_detected_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal_no, group["product"], group["event_term"], group["product_key"],
                 group["event_key"], "open", group["case_count"], group["serious_count"],
                 group["fatal_count"], json.dumps(regions, ensure_ascii=False),
                 json.dumps(reasons), now, now),
            )
            Repository = repo.__class__
            Repository.audit(conn, None, actor, role, "signal_detected",
                             {"signal_id": cursor.lastrowid, "signal_no": signal_no, "reasons": reasons})
            created.append(dict(_signal(conn, cursor.lastrowid)))
        Repository = repo.__class__
        Repository.audit(conn, None, actor, role, "signals_scanned",
                         {"created": [s["id"] for s in created], "refreshed": refreshed})
    return {"rules": asdict(rules), "checked_groups": len(groups),
            "created": created, "refreshed": refreshed}


def list_signals(repo: Any, role: str, region: str) -> list[dict[str, Any]]:
    if role not in VIEW_ROLES:
        raise ApiError(403, "signal_forbidden", "当前角色无权查看安全信号")
    conn = repo.conn
    rows = conn.execute(
        "SELECT * FROM signals ORDER BY fatal_count DESC, last_detected_at DESC, id DESC"
    ).fetchall()
    result = []
    for row in rows:
        if role == "regional_lead" and region not in _regions(row):
            continue
        result.append(_signal_dict(conn, row))
    return result


def get_signal(repo: Any, signal_id: int, role: str, region: str) -> dict[str, Any]:
    conn = repo.conn
    signal = _signal(conn, signal_id)
    _require_view(signal, role, region)
    sql = """SELECT id,case_no,patient_ref,region,product,event_term,serious,fatal,
                    causality,received_at,status,revision
             FROM cases WHERE status != 'merged'
             AND lower(trim(product))=? AND lower(trim(event_term))=?"""
    args: list[Any] = [signal["product_key"], signal["event_key"]]
    if role == "regional_lead":
        sql += " AND region=?"
        args.append(region)
    sql += " ORDER BY received_at, id"
    evidence = []
    for case_row in conn.execute(sql, args):
        alias_sql = "SELECT id,case_no,region,received_at FROM cases WHERE status='merged' AND merged_into=?"
        alias_args: list[Any] = [case_row["id"]]
        if role == "regional_lead":
            alias_sql += " AND region=?"
            alias_args.append(region)
        aliases = [dict(r) for r in conn.execute(alias_sql, alias_args)]
        sources = [r[0] for r in conn.execute(
            "SELECT DISTINCT source FROM intakes WHERE case_id=? ORDER BY source", (case_row["id"],))]
        evidence.append({**dict(case_row), "merged_sources": aliases, "sources": sources})
    return {
        "signal": _signal_dict(conn, signal),
        "evidence": evidence,
        "decisions": _decisions(conn, signal_id),
        "actions": _actions(conn, signal_id),
        "server_time": iso(),
    }


def decide(repo: Any, signal_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
    """医学审核员确认/驳回，必须写依据；同一信号只能判定一次。"""
    if role != "medical_reviewer":
        raise ApiError(403, "decision_forbidden", "只有医学审核员可以确认或驳回信号")
    decision = str(body.get("decision", "")).strip().lower()
    rationale = str(body.get("rationale", "")).strip()
    if decision not in {"confirmed", "rejected"}:
        raise ApiError(400, "invalid_decision", "decision 必须是 confirmed 或 rejected")
    if not rationale:
        raise ApiError(400, "rationale_required", "确认或驳回必须填写判定依据")
    with repo.tx() as conn:
        signal = _signal(conn, signal_id)
        if signal["status"] != "open":
            raise ApiError(409, "signal_decided", f"信号已{signal['status']}，不能重复判定")
        now = iso()
        cursor = conn.execute(
            "INSERT INTO signal_decisions(signal_id,decision,rationale,reviewer,created_at) VALUES(?,?,?,?,?)",
            (signal_id, decision, rationale, actor, now),
        )
        conn.execute("UPDATE signals SET status=?,decided_at=? WHERE id=?", (decision, now, signal_id))
        repo.__class__.audit(conn, None, actor, role, "signal_decided",
                             {"signal_id": signal_id, "decision": decision})
        return {"signal": dict(_signal(conn, signal_id)),
                "decision": dict(conn.execute(
                    "SELECT * FROM signal_decisions WHERE id=?", (cursor.lastrowid,)).fetchone())}


def _parse_due(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ApiError(400, "due_required", "措施期限必填（ISO 日期或时间）")
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return parse_time(text)
    return datetime.combine(day, time(23, 59, 59), tzinfo=timezone.utc)


def register_action(repo: Any, signal_id: int, actor: str, role: str, region: str,
                    body: dict[str, Any]) -> dict[str, Any]:
    """信号确认后登记措施、负责人和期限。"""
    if role not in {"global_admin", "regional_lead"}:
        raise ApiError(403, "action_forbidden", "只有区域负责人或全局管理员可以登记处置措施")
    measure = str(body.get("measure", "")).strip()
    owner = str(body.get("owner", "")).strip()
    if not measure or not owner:
        raise ApiError(400, "missing_fields", "measure 和 owner 必填")
    due = _parse_due(body.get("due_at"))
    with repo.tx() as conn:
        signal = _signal(conn, signal_id)
        _require_view(signal, role, region)
        if signal["status"] != "confirmed":
            raise ApiError(409, "signal_not_confirmed", "只有确认后的信号才能登记处置措施")
        now = iso()
        cursor = conn.execute(
            """INSERT INTO signal_actions(signal_id,measure,owner,due_at,status,
               registered_by,created_at) VALUES(?,?,?,?,'open',?,?)""",
            (signal_id, measure, owner, iso(due), actor, now),
        )
        repo.__class__.audit(conn, None, actor, role, "signal_action_registered",
                             {"signal_id": signal_id, "action_id": cursor.lastrowid,
                              "owner": owner, "due_at": iso(due)})
        return _action_dict(conn.execute("SELECT * FROM signal_actions WHERE id=?",
                                         (cursor.lastrowid,)).fetchone(), utcnow())


def complete_action(repo: Any, action_id: int, actor: str, role: str, region: str) -> dict[str, Any]:
    if role not in {"global_admin", "regional_lead"}:
        raise ApiError(403, "action_forbidden", "当前角色不能更新处置措施")
    with repo.tx() as conn:
        row = conn.execute("SELECT * FROM signal_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            raise ApiError(404, "action_not_found", "处置措施不存在")
        signal = _signal(conn, row["signal_id"])
        _require_view(signal, role, region)
        if row["completed_at"]:
            return {"action": _action_dict(row, utcnow()), "idempotent": True}
        now = iso()
        conn.execute("UPDATE signal_actions SET status='done',completed_at=?,completed_by=? WHERE id=?",
                     (now, actor, action_id))
        repo.__class__.audit(conn, None, actor, role, "signal_action_completed",
                             {"signal_id": signal["id"], "action_id": action_id})
        return {"action": _action_dict(conn.execute(
            "SELECT * FROM signal_actions WHERE id=?", (action_id,)).fetchone(), utcnow()),
            "idempotent": False}
