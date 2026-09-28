"""고객 · 안내 발송 기록 SQLite 저장소.

안내 기록의 키는 (customer_id, expires_on, days_before) 다. 만료일이 바뀌면(연장)
새 주기로 보고 다시 안내하고, 같은 주기의 같은 안내는 두 번 나가지 않는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

SENT = "sent"
FAILED = "failed"
#: 타임아웃·5xx — 상대가 받았는지 모름. 자동 재시도하지 않는다.
UNKNOWN = "unknown"
#: 더 가까운 안내가 대신 나가서 건너뜀 (예: 만료 하루 전에 등록 → D-7 은 건너뜀)
SKIPPED = "skipped"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL UNIQUE,
    plan       TEXT NOT NULL DEFAULT '',
    expires_on TEXT NOT NULL,
    memo       TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    sms_opt_out INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    expires_on  TEXT NOT NULL,
    days_before INTEGER NOT NULL,
    status      TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    message_id  TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (customer_id, expires_on, days_before)
);
CREATE INDEX IF NOT EXISTS idx_customers_expires ON customers(expires_on);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Customer:
    id: int
    name: str
    phone: str
    plan: str
    expires_on: date
    memo: str
    active: bool
    sms_opt_out: bool

    @property
    def masked_phone(self) -> str:
        return f"{self.phone[:3]}-****-{self.phone[-4:]}"


@dataclass(frozen=True, slots=True)
class ReminderRecord:
    customer_id: int
    expires_on: str
    days_before: int
    status: str
    attempts: int
    message_id: str
    error: str
    updated_at: str
    name: str = ""


class DuplicatePhone(ValueError):
    pass


_CUSTOMER_COLS = "id, name, phone, plan, expires_on, memo, active, sms_opt_out"


def _customer(row: tuple) -> Customer:
    return Customer(row[0], row[1], row[2], row[3], date.fromisoformat(row[4]), row[5], bool(row[6]), bool(row[7]))


class Store:
    def __init__(self, path: str) -> None:
        # 웹 서버는 요청마다 새 연결을 연다(스레드 간 공유 금지).
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- 고객 ----------

    def customers(self, *, query: str = "", include_inactive: bool = True) -> list[Customer]:
        sql = f"SELECT {_CUSTOMER_COLS} FROM customers"
        where, params = [], []
        if query:
            where.append("(name LIKE ? OR phone LIKE ? OR plan LIKE ? OR memo LIKE ?)")
            like = f"%{query}%"
            params += [like, like, like, like]
        if not include_inactive:
            where.append("active = 1")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY active DESC, expires_on, name"
        return [_customer(r) for r in self.conn.execute(sql, params).fetchall()]

    def customer(self, customer_id: int) -> Customer | None:
        row = self.conn.execute(f"SELECT {_CUSTOMER_COLS} FROM customers WHERE id = ?", (customer_id,)).fetchone()
        return _customer(row) if row else None

    def customer_by_phone(self, phone: str) -> Customer | None:
        row = self.conn.execute(f"SELECT {_CUSTOMER_COLS} FROM customers WHERE phone = ?", (phone,)).fetchone()
        return _customer(row) if row else None

    def add_customer(self, name: str, phone: str, expires_on: date, *, plan: str = "", memo: str = "", active: bool = True, sms_opt_out: bool = False) -> int:
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO customers (name, phone, plan, expires_on, memo, active, sms_opt_out, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (name, phone, plan, expires_on.isoformat(), memo, int(active), int(sms_opt_out), _now(), _now()),
                )
        except sqlite3.IntegrityError:
            raise DuplicatePhone(f"이미 등록된 번호입니다: {phone[:3]}-****-{phone[-4:]}") from None
        return int(cur.lastrowid)

    def update_customer(self, customer_id: int, *, name: str, phone: str, expires_on: date, plan: str, memo: str, active: bool, sms_opt_out: bool) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE customers SET name=?, phone=?, plan=?, expires_on=?, memo=?, active=?, sms_opt_out=?, updated_at=? WHERE id=?",
                    (name, phone, plan, expires_on.isoformat(), memo, int(active), int(sms_opt_out), _now(), customer_id),
                )
        except sqlite3.IntegrityError:
            raise DuplicatePhone(f"다른 고객이 쓰는 번호입니다: {phone[:3]}-****-{phone[-4:]}") from None

    def set_expiry(self, customer_id: int, expires_on: date) -> None:
        with self.conn:
            self.conn.execute("UPDATE customers SET expires_on=?, updated_at=? WHERE id=?", (expires_on.isoformat(), _now(), customer_id))

    def delete_customer(self, customer_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))

    # ---------- 안내 기록 ----------

    def reminder(self, customer_id: int, expires_on: date, days_before: int) -> ReminderRecord | None:
        row = self.conn.execute(
            "SELECT customer_id, expires_on, days_before, status, attempts, message_id, error, updated_at"
            " FROM reminders WHERE customer_id=? AND expires_on=? AND days_before=?",
            (customer_id, expires_on.isoformat(), days_before),
        ).fetchone()
        return ReminderRecord(*row) if row else None

    def record(self, customer_id: int, expires_on: date, days_before: int, status: str, *, message_id: str = "", error: str = "", count_attempt: bool = True) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO reminders (customer_id, expires_on, days_before, status, attempts, message_id, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (customer_id, expires_on, days_before) DO UPDATE SET
                    status = excluded.status, attempts = reminders.attempts + excluded.attempts,
                    message_id = excluded.message_id, error = excluded.error, updated_at = excluded.updated_at
                """,
                (customer_id, expires_on.isoformat(), days_before, status, int(count_attempt), message_id, error[:500], _now()),
            )

    def clear_reminder(self, customer_id: int, expires_on: date, days_before: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM reminders WHERE customer_id=? AND expires_on=? AND days_before=?",
                (customer_id, expires_on.isoformat(), days_before),
            )

    def recent_reminders(self, limit: int = 100) -> list[ReminderRecord]:
        rows = self.conn.execute(
            "SELECT r.customer_id, r.expires_on, r.days_before, r.status, r.attempts, r.message_id, r.error, r.updated_at,"
            " COALESCE(c.name, '(삭제됨)') FROM reminders r LEFT JOIN customers c ON c.id = r.customer_id"
            " WHERE r.status != ? ORDER BY r.updated_at DESC LIMIT ?",
            (SKIPPED, limit),
        ).fetchall()
        return [ReminderRecord(*row) for row in rows]
