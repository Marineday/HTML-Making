"""메신저봇R 이 폴링해 가는 메시지 큐 서버 (표준 라이브러리만 사용).

왜 큐인가
    안드로이드 기기는 대개 공인 IP 가 없고 배터리 최적화 때문에 인바운드
    연결을 유지하지 못한다. 그래서 서버가 밀어넣는 대신 기기가 당겨 간다.

왜 lease/ack 인가
    단순히 "pull 하면 삭제" 로 하면, 응답이 도중에 끊겼을 때 메시지가 조용히
    사라진다(모바일 회선에서 드물지 않다). 여기서는 pull 시 임대(lease)만 걸고
    실제 카톡 전송에 성공한 뒤 ack 를 받아야 삭제한다. 임대가 만료되면 다시
    대기 상태로 돌아간다 — 최소 1회 전달(at-least-once)이다.

엔드포인트 (나가는 메시지)
    POST /enqueue   {"room": "...", "text": "..."}         → {"ok":true,"id":N}
    GET  /pull?room=...&max=5                              → {"ok":true,"messages":[{"id":N,"text":"..."}]}
    POST /ack       {"ids": [1,2,3]}                       → {"ok":true,"acked":N}

엔드포인트 (들어오는 딜)
    POST /ingest    {"source":"toss", "deals":[{...}]}     → {"ok":true,"accepted":N}
    GET  /inbox?limit=50                                   → {"ok":true,"deals":[{...}]}

    안드로이드 기기에서 수집한 딜을 밀어넣는 통로다. UI 덤프 스크립트든
    알림 캡처(Tasker/MacroDroid)든 같은 엔드포인트를 쓴다.

    inbox 는 소비되지 않는 롤링 버퍼다. 읽어도 지워지지 않는다 —
    파이프라인이 이미 지문 기반 중복제거를 하므로 같은 딜을 여러 번 읽어도
    안전하고, 대신 파이프라인이 죽어도 딜이 유실되지 않는다.

    GET  /health                                           → {"ok":true,...}

인증
    모든 엔드포인트(/health 제외)는 X-Bot-Token 헤더를 요구한다.
    토큰 비교는 hmac.compare_digest 로 타이밍 공격을 피한다.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("bridge")

MAX_BODY_BYTES = 64 * 1024
DEFAULT_LEASE_SECONDS = 120
MAX_QUEUE_DEPTH = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    room         TEXT NOT NULL,
    text         TEXT NOT NULL,
    created_at   REAL NOT NULL,
    leased_until REAL NOT NULL DEFAULT 0,
    delivered_at REAL
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(room, delivered_at, leased_until);

CREATE TABLE IF NOT EXISTS inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key   TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    received_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbox_received ON inbox(received_at);
"""

MAX_INBOX_ROWS = 2000
MAX_INGEST_DEALS = 200


class Queue:
    """스레드 안전한 SQLite 큐."""

    def __init__(self, path: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # http.server 가 스레드마다 핸들러를 돌리므로 락으로 직렬화한다.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()
        self.lease_seconds = lease_seconds

    def enqueue(self, room: str, text: str) -> int:
        with self._lock:
            depth = self._conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE room = ? AND delivered_at IS NULL", (room,)
            ).fetchone()[0]
            if depth >= MAX_QUEUE_DEPTH:
                # 안드로이드 기기가 죽어 있는데 계속 쌓으면, 살아난 순간
                # 수백 통이 한꺼번에 나가서 확실히 밴을 맞는다. 여기서 막는다.
                raise QueueFull(f"'{room}' 대기열이 가득 찼습니다({depth}). 안드로이드 기기가 폴링 중인지 확인하세요.")
            cursor = self._conn.execute(
                "INSERT INTO outbox (room, text, created_at) VALUES (?, ?, ?)", (room, text, time.time())
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def pull(self, room: str, limit: int) -> list[dict[str, object]]:
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text FROM outbox"
                " WHERE room = ? AND delivered_at IS NULL AND leased_until < ?"
                " ORDER BY id LIMIT ?",
                (room, now, limit),
            ).fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" * len(ids))
                self._conn.execute(
                    f"UPDATE outbox SET leased_until = ? WHERE id IN ({placeholders})",
                    [now + self.lease_seconds, *ids],
                )
                self._conn.commit()
            return [{"id": row["id"], "text": row["text"]} for row in rows]

    def ack(self, ids: list[int]) -> int:
        if not ids:
            return 0
        with self._lock:
            placeholders = ",".join("?" * len(ids))
            cursor = self._conn.execute(
                f"UPDATE outbox SET delivered_at = ? WHERE id IN ({placeholders}) AND delivered_at IS NULL",
                [time.time(), *ids],
            )
            self._conn.commit()
            return cursor.rowcount

    # ---------- 들어오는 딜 ----------

    def ingest(self, source: str, deals: list[dict]) -> int:
        """딜을 인박스에 넣는다. 이미 있는 것은 건너뛰고 새로 들어간 수를 반환."""
        now = time.time()
        accepted = 0
        with self._lock:
            for deal in deals:
                key = _inbox_key(source, deal)
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO inbox (dedup_key, source, payload, received_at) VALUES (?, ?, ?, ?)",
                    (key, source, json.dumps(deal, ensure_ascii=False), now),
                )
                accepted += cursor.rowcount
            # 버퍼가 무한히 자라지 않도록 오래된 것부터 잘라낸다.
            self._conn.execute(
                "DELETE FROM inbox WHERE id NOT IN (SELECT id FROM inbox ORDER BY id DESC LIMIT ?)",
                (MAX_INBOX_ROWS,),
            )
            self._conn.commit()
        return accepted

    def inbox(self, limit: int) -> list[dict]:
        """최근 딜 목록. 읽어도 지워지지 않는다(롤링 버퍼)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, payload FROM inbox ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        deals = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            payload.setdefault("source", row["source"])
            deals.append(payload)
        return deals

    def stats(self) -> dict[str, int]:
        with self._lock:
            pending = self._conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0]
            delivered = self._conn.execute("SELECT COUNT(*) FROM outbox WHERE delivered_at IS NOT NULL").fetchone()[0]
            inbox = self._conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0]
        return {"pending": int(pending), "delivered": int(delivered), "inbox": int(inbox)}

    def prune(self, older_than_seconds: float = 86400) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM outbox WHERE delivered_at IS NOT NULL AND delivered_at < ?",
                (time.time() - older_than_seconds,),
            )
            self._conn.commit()
            return cursor.rowcount


class QueueFull(RuntimeError):
    pass


def _inbox_key(source: str, deal: dict) -> str:
    """같은 딜을 여러 번 밀어넣어도 한 번만 쌓이게 하는 키.

    UI 덤프는 화면을 볼 때마다 같은 카드를 다시 읽고, 알림 캡처도 재알림이
    올 수 있다. url 이 있으면 url, 없으면 제목으로 판정한다.
    """
    basis = str(deal.get("url") or "").strip() or str(deal.get("title") or "").strip()
    return hashlib.sha1(f"{source}|{basis}".encode("utf-8")).hexdigest()


def make_handler(queue: Queue, token: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "hotdeal-bridge/0.1"
        protocol_version = "HTTP/1.1"

        # ---------- 유틸 ----------

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            supplied = self.headers.get("X-Bot-Token", "")
            return hmac.compare_digest(supplied, token)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValueError("빈 요청 본문")
            if length > MAX_BODY_BYTES:
                raise ValueError(f"요청 본문이 너무 큽니다 ({length} bytes)")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON 객체가 아닙니다")
            return payload

        def log_message(self, fmt: str, *args: object) -> None:
            log.info("%s - %s", self.address_string(), fmt % args)

        # ---------- 라우팅 ----------

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 규약
            parts = urlsplit(self.path)
            if parts.path == "/health":
                self._json(HTTPStatus.OK, {"ok": True, **queue.stats()})
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "토큰이 올바르지 않습니다"})
                return
            if parts.path == "/pull":
                params = parse_qs(parts.query)
                room = (params.get("room") or [""])[0]
                if not room:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "room 파라미터가 필요합니다"})
                    return
                try:
                    limit = max(1, min(20, int((params.get("max") or ["5"])[0])))
                except ValueError:
                    limit = 5
                self._json(HTTPStatus.OK, {"ok": True, "messages": queue.pull(room, limit)})
                return
            if parts.path == "/inbox":
                params = parse_qs(parts.query)
                try:
                    limit = max(1, min(500, int((params.get("limit") or ["100"])[0])))
                except ValueError:
                    limit = 100
                self._json(HTTPStatus.OK, {"ok": True, "deals": queue.inbox(limit)})
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "없는 경로"})

        def do_POST(self) -> None:  # noqa: N802
            parts = urlsplit(self.path)
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "토큰이 올바르지 않습니다"})
                return
            try:
                payload = self._read_json()
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if parts.path == "/enqueue":
                room = str(payload.get("room") or "").strip()
                text = str(payload.get("text") or "").strip()
                if not room or not text:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "room 과 text 가 필요합니다"})
                    return
                try:
                    message_id = queue.enqueue(room, text)
                except QueueFull as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "id": message_id})
                return

            if parts.path == "/ingest":
                source = str(payload.get("source") or "").strip()
                deals = payload.get("deals")
                if not source:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "source 가 필요합니다"})
                    return
                if not isinstance(deals, list):
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "deals 는 배열이어야 합니다"})
                    return
                if len(deals) > MAX_INGEST_DEALS:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": f"한 번에 {MAX_INGEST_DEALS}건까지만 보낼 수 있습니다"},
                    )
                    return

                clean = []
                for item in deals:
                    if not isinstance(item, dict):
                        continue
                    title = str(item.get("title") or "").strip()
                    if not title:
                        continue
                    clean.append(
                        {
                            "title": title[:300],
                            "url": str(item.get("url") or "").strip()[:1000],
                            "price_krw": item.get("price_krw"),
                            "source": source,
                        }
                    )
                if not clean:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "유효한 딜이 없습니다 (title 필수)"})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "accepted": queue.ingest(source, clean), "received": len(clean)})
                return

            if parts.path == "/ack":
                raw_ids = payload.get("ids")
                if not isinstance(raw_ids, list):
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "ids 는 배열이어야 합니다"})
                    return
                try:
                    ids = [int(value) for value in raw_ids]
                except (TypeError, ValueError):
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "ids 는 정수 배열이어야 합니다"})
                    return
                self._json(HTTPStatus.OK, {"ok": True, "acked": queue.ack(ids)})
                return

            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "없는 경로"})

    return Handler


def serve(host: str, port: int, db_path: str, token: str, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
    queue = Queue(db_path, lease_seconds)
    queue.prune()
    server = ThreadingHTTPServer((host, port), make_handler(queue, token))
    log.info("브리지 서버 시작: http://%s:%d (db=%s)", host, port, db_path)
    log.info("메신저봇R 은 GET /pull?room=<방이름> 을 폴링하고 전송 성공 후 POST /ack 하면 됩니다")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("종료합니다")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="카카오 오픈채팅 브리지 큐 서버")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="bridge.db")
    parser.add_argument("--lease-seconds", type=int, default=DEFAULT_LEASE_SECONDS)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    token = os.environ.get("BRIDGE_TOKEN", "")
    if not token:
        parser.error("환경변수 BRIDGE_TOKEN 을 설정하세요. 예: export BRIDGE_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')")
    if len(token) < 16:
        parser.error("BRIDGE_TOKEN 이 너무 짧습니다. 최소 16자 이상을 쓰세요.")

    serve(args.host, args.port, args.db, token, args.lease_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
