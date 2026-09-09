"""SQLite 중복제거 저장소.

봇이 재시작해도 같은 딜을 다시 쏘면 안 되므로 상태는 반드시 디스크에 있어야
한다. 인메모리 set 은 여기서 오답이다.

중복 판정은 2단계:
  1) 정규화 URL 해시 — 같은 글의 재수집
  2) 제목 키       — 여러 커뮤니티에 동시에 올라온 같은 딜
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Deal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_deals (
    fingerprint TEXT PRIMARY KEY,
    title_key   TEXT NOT NULL,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    first_seen  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_title_key ON seen_deals(title_key);
CREATE INDEX IF NOT EXISTS idx_seen_first_seen ON seen_deals(first_seen);
"""


class DealStore:
    """이미 보낸 딜을 기억한다. `with` 로 쓰거나 close() 를 호출할 것."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ---------- 조회 ----------

    def is_new(self, deal: Deal, *, cross_source_title_dedup: bool = True) -> bool:
        cursor = self._conn.execute("SELECT 1 FROM seen_deals WHERE fingerprint = ? LIMIT 1", (deal.fingerprint,))
        if cursor.fetchone():
            return False
        if cross_source_title_dedup and deal.title_key:
            cursor = self._conn.execute("SELECT 1 FROM seen_deals WHERE title_key = ? LIMIT 1", (deal.title_key,))
            if cursor.fetchone():
                return False
        return True

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM seen_deals").fetchone()[0])

    # ---------- 기록 ----------

    def mark_seen(self, deal: Deal) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_deals (fingerprint, title_key, source, title, url, first_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                deal.fingerprint,
                deal.title_key,
                deal.source,
                deal.title,
                deal.normalized_url,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()

    def mark_many(self, deals: list[Deal]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.executemany(
            "INSERT OR IGNORE INTO seen_deals (fingerprint, title_key, source, title, url, first_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(d.fingerprint, d.title_key, d.source, d.title, d.normalized_url, now) for d in deals],
        )
        self._conn.commit()

    def prune(self, retention_days: int) -> int:
        """오래된 기록을 지운다. DB 가 무한히 커지는 것을 막는다."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        cursor = self._conn.execute("DELETE FROM seen_deals WHERE first_seen < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    # ---------- 수명 ----------

    def close(self) -> None:
        with closing(self._conn):
            pass

    def __enter__(self) -> "DealStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
