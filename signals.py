"""Safety signal data layer: signals, evidence, decisions and actions storage."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app import iso, utcnow


class SignalStore:
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_no TEXT NOT NULL UNIQUE,
            product TEXT NOT NULL,
            event_term TEXT NOT NULL,
            product_key TEXT NOT NULL,
            event_key TEXT NOT NULL,
            case_count INTEGER NOT NULL DEFAULT 0,
            serious_count INTEGER NOT NULL DEFAULT 0,
            fatal_count INTEGER NOT NULL DEFAULT 0,
            regions_json TEXT NOT NULL DEFAULT '[]',
            rules_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            rationale TEXT,
            decided_by TEXT,
            decided_at TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(product_key, event_key)
        );
        CREATE TABLE IF NOT EXISTS signal_evidence (
            signal_id INTEGER NOT NULL REFERENCES signals(id),
            case_id INTEGER NOT NULL REFERENCES cases(id),
            region TEXT NOT NULL,
            serious INTEGER NOT NULL,
            fatal INTEGER NOT NULL,
            added_at TEXT NOT NULL,
            PRIMARY KEY(signal_id, case_id)
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
            status TEXT NOT NULL DEFAULT 'registered',
            completed_at TEXT,
            completed_by TEXT,
            created_by TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def init_schema(self) -> None:
        self.conn.executescript(self.SCHEMA)

    @staticmethod
    def serialize(signal: sqlite3.Row, now: str | None = None) -> dict[str, Any]:
        data = dict(signal)
        data["regions"] = json.loads(data.pop("regions_json") or "[]")
        data["rules"] = json.loads(data.pop("rules_json") or "{}")
        return data

    def get(self, conn: sqlite3.Connection, signal_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        if not row:
            from app import ApiError
            raise ApiError(404, "signal_not_found", "信号不存在")
        return row

    def reconcile_evidence(
        self,
        conn: sqlite3.Connection,
        signal_id: int,
        cases: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """按当前有效案例集合同步证据，并据此重算例数/严重/死亡/区域统计。"""
        valid_ids = {int(c["id"]) for c in cases}
        kept = {cid for (cid,) in conn.execute(
            "SELECT case_id FROM signal_evidence WHERE signal_id=?", (signal_id,)
        ) if cid in valid_ids}
        removed = {cid for (cid,) in conn.execute(
            "SELECT case_id FROM signal_evidence WHERE signal_id=?", (signal_id,)
        ) if cid not in valid_ids}
        if removed:
            conn.executemany(
                "DELETE FROM signal_evidence WHERE signal_id=? AND case_id=?",
                [(signal_id, cid) for cid in removed],
            )
        now = iso()
        for case in cases:
            if int(case["id"]) not in kept:
                conn.execute(
                    """INSERT INTO signal_evidence(signal_id,case_id,region,serious,fatal,added_at)
                       VALUES(?,?,?,?,?,?)""",
                    (signal_id, case["id"], case["region"], int(bool(case["serious"])),
                     int(bool(case["fatal"])), now),
                )
        regions = sorted({c["region"] for c in cases if c.get("region")})
        stats = {
            "case_count": len(cases),
            "serious_count": sum(1 for c in cases if c["serious"]),
            "fatal_count": sum(1 for c in cases if c["fatal"]),
            "regions": regions,
        }
        conn.execute(
            """UPDATE signals SET case_count=?,serious_count=?,fatal_count=?,regions_json=?,updated_at=?
               WHERE id=?""",
            (stats["case_count"], stats["serious_count"], stats["fatal_count"],
             json.dumps(regions, ensure_ascii=False), now, signal_id),
        )
        return stats

    def next_signal_no(self, conn: sqlite3.Connection, year: int) -> str:
        count = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0] + 1
        return f"SIG-{year}-{count:06d}"

    def list_signals(self, conn: sqlite3.Connection, region: str | None) -> list[dict[str, Any]]:
        """区域负责人只能看到本区域涉及的信号；region 为空表示跨区角色。"""
        if region:
            rows = conn.execute(
                "SELECT * FROM signals WHERE EXISTS ("
                "SELECT 1 FROM signal_evidence e WHERE e.signal_id=signals.id AND e.region=?) "
                "ORDER BY id DESC",
                (region,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM signals ORDER BY id DESC").fetchall()
        return [self.serialize(row) for row in rows]

    def evidence(self, conn: sqlite3.Connection, signal_id: int, region: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT e.case_id,e.region,e.serious,e.fatal,e.added_at,"
            "c.case_no,c.product,c.event_term,c.received_at,c.serious AS case_serious,c.fatal AS case_fatal,c.status "
            "FROM signal_evidence e JOIN cases c ON c.id=e.case_id WHERE e.signal_id=?"
        )
        args: list[Any] = [signal_id]
        if region:
            sql += " AND e.region=?"
            args.append(region)
        sql += " ORDER BY e.case_id"
        return [dict(r) for r in conn.execute(sql, args)]

    def decisions(self, conn: sqlite3.Connection, signal_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(
            "SELECT id,decision,rationale,reviewer,created_at FROM signal_decisions WHERE signal_id=? ORDER BY id",
            (signal_id,),
        )]

    def actions(self, conn: sqlite3.Connection, signal_id: int, now: str | None = None) -> list[dict[str, Any]]:
        current = now or iso()
        result = []
        for row in conn.execute("SELECT * FROM signal_actions WHERE signal_id=? ORDER BY id", (signal_id,)):
            item = dict(row)
            item["overdue"] = int(item["status"] != "completed" and item["due_at"] < current)
            result.append(item)
        return result

    def action(self, conn: sqlite3.Connection, action_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM signal_actions WHERE id=?", (action_id,)).fetchone()
        if not row:
            from app import ApiError
            raise ApiError(404, "action_not_found", "措施不存在")
        return row

    @staticmethod
    def has_overdue(signal_actions: list[dict[str, Any]]) -> bool:
        return any(a["status"] != "completed" and a["due_at"] < iso(utcnow()) for a in signal_actions)
