"""발송 기록 · 월별 승인 · 텔레그램 연결을 담는 SQLite 저장소.

발송 기록의 키는 (period, phone) 이다. 크론이 하루에 여러 번 돌거나 서버가
재시작돼도 같은 달에 같은 번호로 두 번 나가지 않게 하는 게 이 테이블의 역할이다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

SENT = "sent"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sends (
    period     TEXT NOT NULL,
    phone      TEXT NOT NULL,
    name       TEXT NOT NULL,
    channel    TEXT NOT NULL,
    status     TEXT NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    message_id TEXT NOT NULL DEFAULT '',
    error      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (period, phone)
);
CREATE TABLE IF NOT EXISTS approvals (
    period      TEXT PRIMARY KEY,
    approved_at TEXT NOT NULL DEFAULT '',
    notified_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS telegram_links (
    phone     TEXT PRIMARY KEY,
    chat_id   TEXT NOT NULL,
    linked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class SendRecord:
    period: str
    phone: str
    name: str
    channel: str
    status: str
    attempts: int
    message_id: str
    error: str
    updated_at: str


class Store:
    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- 발송 기록 ----------

    def get(self, period: str, phone: str) -> SendRecord | None:
        row = self.conn.execute(
            "SELECT period, phone, name, channel, status, attempts, message_id, error, updated_at"
            " FROM sends WHERE period = ? AND phone = ?",
            (period, phone),
        ).fetchone()
        return SendRecord(*row) if row else None

    def records(self, period: str) -> list[SendRecord]:
        rows = self.conn.execute(
            "SELECT period, phone, name, channel, status, attempts, message_id, error, updated_at"
            " FROM sends WHERE period = ? ORDER BY name",
            (period,),
        ).fetchall()
        return [SendRecord(*row) for row in rows]

    def record(self, period: str, phone: str, name: str, channel: str, *, ok: bool, message_id: str = "", error: str = "") -> None:
        """시도 1회를 기록한다. 성공이든 실패든 attempts 가 1 오른다."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO sends (period, phone, name, channel, status, attempts, message_id, error, updated_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT (period, phone) DO UPDATE SET
                    name = excluded.name, channel = excluded.channel, status = excluded.status,
                    attempts = sends.attempts + 1, message_id = excluded.message_id,
                    error = excluded.error, updated_at = excluded.updated_at
                """,
                (period, phone, name, channel, SENT if ok else FAILED, message_id, error[:500], _now()),
            )

    def reset(self, period: str, phone: str | None = None) -> int:
        """재발송하고 싶을 때 기록을 지운다. 지운 행 수를 돌려준다."""
        with self.conn:
            if phone is None:
                cur = self.conn.execute("DELETE FROM sends WHERE period = ?", (period,))
            else:
                cur = self.conn.execute("DELETE FROM sends WHERE period = ? AND phone = ?", (period, phone))
        return cur.rowcount

    # ---------- 월별 승인 ----------

    def is_approved(self, period: str) -> bool:
        row = self.conn.execute("SELECT approved_at FROM approvals WHERE period = ?", (period,)).fetchone()
        return bool(row and row[0])

    def approve(self, period: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO approvals (period, approved_at) VALUES (?, ?)"
                " ON CONFLICT (period) DO UPDATE SET approved_at = excluded.approved_at",
                (period, _now()),
            )

    def was_notified(self, period: str) -> bool:
        row = self.conn.execute("SELECT notified_at FROM approvals WHERE period = ?", (period,)).fetchone()
        return bool(row and row[0])

    def mark_notified(self, period: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO approvals (period, notified_at) VALUES (?, ?)"
                " ON CONFLICT (period) DO UPDATE SET notified_at = excluded.notified_at",
                (period, _now()),
            )

    # ---------- 텔레그램 ----------

    def link_telegram(self, phone: str, chat_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO telegram_links (phone, chat_id, linked_at) VALUES (?, ?, ?)"
                " ON CONFLICT (phone) DO UPDATE SET chat_id = excluded.chat_id, linked_at = excluded.linked_at",
                (phone, chat_id, _now()),
            )

    def telegram_chat_id(self, phone: str) -> str | None:
        row = self.conn.execute("SELECT chat_id FROM telegram_links WHERE phone = ?", (phone,)).fetchone()
        return row[0] if row else None

    def get_value(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_value(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
