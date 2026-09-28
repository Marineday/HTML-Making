"""운영자용 관리 사이트 (표준 라이브러리 http.server).

고객 목록·만료 D-day·연장·CSV 일괄 등록·오늘 안내 미리보기·발송 기록을 본다.
고객이 보는 사이트가 아니다. 인터넷에 열 때는 반드시 HTTPS 리버스 프록시
(caddy 등) 뒤에 둘 것 — 비밀번호와 세션 쿠키가 평문으로 흐른다.

보안 장치
  * 비밀번호 로그인 (12자 이상), 같은 IP 5회 실패 시 15분 잠금
  * 서명된 세션 쿠키 (HttpOnly, SameSite=Strict, 12시간 만료)
  * 모든 POST 에 CSRF 토큰
  * 모든 출력 HTML 이스케이프, CSP·X-Frame-Options 헤더
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import logging
import secrets
import threading
import time
from datetime import date, datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from familyinvite.channels import Channel
from familyinvite.recipients import RecipientError, normalize_phone

from . import reminders
from .config import RenewConfig
from .dates import add_months, dday_label, parse_date
from .store import SENT, UNKNOWN, Customer, DuplicatePhone, Store

log = logging.getLogger(__name__)

SESSION_SECONDS = 12 * 3600
MAX_BODY = 1_000_000
LOGIN_MAX_FAILS = 5
LOGIN_LOCK_SECONDS = 15 * 60


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# 세션 · CSRF · 로그인 제한
# ---------------------------------------------------------------------------


class Auth:
    def __init__(self, password: str, secret_key: str = "", clock: Callable[[], float] = time.time) -> None:
        self.password = password
        self.secret = (secret_key or secrets.token_hex(32)).encode()
        self.clock = clock
        self._fails: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def _sign(self, payload: str) -> str:
        return hmac.new(self.secret, payload.encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        payload = f"{int(self.clock()) + SESSION_SECONDS}.{secrets.token_hex(16)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str) -> str | None:
        """유효하면 세션 nonce 를, 아니면 None."""
        try:
            expires, nonce, signature = token.split(".")
        except ValueError:
            return None
        if not hmac.compare_digest(signature, self._sign(f"{expires}.{nonce}")):
            return None
        if not expires.isdigit() or int(expires) < self.clock():
            return None
        return nonce

    def csrf(self, nonce: str) -> str:
        return self._sign(f"csrf:{nonce}")[:32]

    def locked(self, ip: str) -> bool:
        with self._lock:
            fails, until = self._fails.get(ip, (0, 0.0))
            return fails >= LOGIN_MAX_FAILS and until > self.clock()

    def check_password(self, ip: str, attempt: str) -> bool:
        ok = hmac.compare_digest(attempt.encode(), self.password.encode())
        with self._lock:
            if ok:
                self._fails.pop(ip, None)
            else:
                fails, _ = self._fails.get(ip, (0, 0.0))
                if fails >= LOGIN_MAX_FAILS:
                    fails = 0  # 잠금이 풀린 뒤 다시 센다
                self._fails[ip] = (fails + 1, self.clock() + LOGIN_LOCK_SECONDS)
        return ok


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1c2025;--muted:#6a7280;--line:#e3e6ea;--accent:#2f6fed;
--ok:#17803d;--warn:#b45309;--bad:#c62828;--chip:#eef1f5}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1e24;--text:#e7eaee;--muted:#9aa3ae;
--line:#2a3038;--accent:#6c9bff;--ok:#4cc27a;--warn:#f0a44b;--bad:#ff6b6b;--chip:#232932}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Noto Sans KR",sans-serif}
a{color:var(--accent);text-decoration:none}header{background:var(--card);border-bottom:1px solid var(--line)}
.wrap{max-width:1080px;margin:0 auto;padding:0 16px}nav{display:flex;gap:16px;align-items:center;height:52px;overflow-x:auto}
nav b{margin-right:auto;white-space:nowrap}nav a{white-space:nowrap;color:var(--muted)}nav a.on{color:var(--text);font-weight:600}
main{padding:20px 0 48px}h1{font-size:20px;margin:0 0 16px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.stat,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stat small{color:var(--muted);display:block}.stat strong{font-size:24px;font-variant-numeric:tabular-nums}
.card{margin-bottom:16px}.tablewrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;background:var(--card)}th,td{padding:10px 8px;border-bottom:1px solid var(--line);
text-align:left;vertical-align:middle;white-space:nowrap}th{font-size:13px;color:var(--muted);font-weight:600}
td.wrap{white-space:pre-wrap;min-width:220px}
.chip{display:inline-block;padding:2px 8px;border-radius:999px;background:var(--chip);font-size:12px;font-weight:600}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
form.inline{display:inline}input,textarea,select{font:inherit;color:var(--text);background:var(--bg);
border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:100%}textarea{min-height:180px;font-family:ui-monospace,monospace}
label{display:block;margin:0 0 12px}label span{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0 16px}
.check{display:flex;gap:8px;align-items:center}.check input{width:auto}
button,.btn{font:inherit;font-weight:600;border:1px solid var(--line);background:var(--card);color:var(--text);
border-radius:8px;padding:7px 12px;cursor:pointer;display:inline-block}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}button.danger{color:var(--bad)}
.toolbar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px}.toolbar input{max-width:280px}
.flash{padding:10px 14px;border-radius:8px;margin-bottom:16px;background:var(--chip);white-space:pre-wrap}
.login{max-width:360px;margin:12vh auto}
"""


def page(title: str, body: str, *, active: str = "", flash: str = "", logged_in: bool = True) -> str:
    nav = ""
    if logged_in:
        links = [("/", "고객", "customers"), ("/reminders", "문자 안내", "reminders"), ("/import", "일괄 등록", "import")]
        items = "".join(f'<a href="{href}" class="{"on" if key == active else ""}">{label}</a>' for href, label, key in links)
        nav = f'<header><div class="wrap"><nav><b>만료 안내</b>{items}<a href="/logout">로그아웃</a></nav></div></header>'
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    return (
        "<!doctype html><html lang=ko><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{nav}"
        f'<main class="wrap">{flash_html}{body}</main></body></html>'
    )


def dday_chip(customer: Customer, today: date) -> str:
    diff = (customer.expires_on - today).days
    tone = "bad" if diff < 0 else "warn" if diff <= 7 else "ok"
    return f'<span class="chip {tone}">{esc(dday_label(customer.expires_on, today))}</span>'


# ---------------------------------------------------------------------------
# 요청 처리
# ---------------------------------------------------------------------------


class App:
    """라우팅과 화면. HTTP 세부사항은 Handler 가 맡는다."""

    def __init__(
        self,
        cfg: RenewConfig,
        channel_factory: Callable[[], Channel],
        *,
        now: Callable[[], datetime] | None = None,
        auth: Auth | None = None,
    ) -> None:
        self.cfg = cfg
        self.channel_factory = channel_factory
        self.now = now or (lambda: datetime.now(cfg.tz))
        self.auth = auth or Auth(cfg.admin_password, cfg.secret_key)
        # 발송 버튼 연타로 같은 사람에게 두 통 가는 것을 막는다.
        self.send_lock = threading.Lock()

    def store(self) -> Store:
        return Store(self.cfg.db_path)

    def today(self) -> date:
        return self.now().date()

    # ---------- 고객 목록 ----------

    def customers_page(self, query: dict[str, str], csrf: str, flash: str) -> str:
        today = self.today()
        q = query.get("q", "").strip()
        with self.store() as store:
            customers = store.customers(query=q)
            due_count = len(reminders.due_reminders(self.cfg, store, today))
        active = [c for c in customers if c.active]
        soon = sum(1 for c in active if 0 <= (c.expires_on - today).days <= 7)
        expired = sum(1 for c in active if c.expires_on < today)
        stats = (
            f'<div class="stats"><div class="stat"><small>이용 중</small><strong>{len(active)}</strong></div>'
            f'<div class="stat"><small>7일 내 만료</small><strong class="warn">{soon}</strong></div>'
            f'<div class="stat"><small>만료됨</small><strong class="bad">{expired}</strong></div>'
            f'<div class="stat"><small>오늘 문자 대상</small><strong>{due_count}</strong></div></div>'
        )
        rows = []
        for c in customers:
            status = "" if c.active else '<span class="chip muted">중지</span> '
            if c.sms_opt_out:
                status += '<span class="chip muted">문자 거부</span>'
            rows.append(
                f"<tr><td>{esc(c.name)}</td><td>{esc(c.masked_phone)}</td><td>{esc(c.plan)}</td>"
                f"<td>{c.expires_on.isoformat()}</td><td>{dday_chip(c, today)}</td><td>{status}</td>"
                f'<td><form class="inline" method="post" action="/customers/{c.id}/extend">'
                f'<input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="months" value="1">'
                f'<button title="만료일 +1개월">+1개월</button></form> '
                f'<a class="btn" href="/customers/{c.id}/edit">수정</a></td></tr>'
            )
        table = (
            '<div class="tablewrap"><table><thead><tr><th>이름</th><th>번호</th><th>상품</th><th>만료일</th>'
            f'<th>D-day</th><th>상태</th><th></th></tr></thead><tbody>{"".join(rows) or "<tr><td colspan=7 class=muted>고객이 없습니다</td></tr>"}'
            "</tbody></table></div>"
        )
        toolbar = (
            '<div class="toolbar"><form method="get" action="/" style="flex:1;display:flex;gap:8px">'
            f'<input name="q" value="{esc(q)}" placeholder="이름·번호·상품·메모 검색"><button>검색</button></form>'
            '<a class="btn" href="/customers/new">+ 고객 추가</a></div>'
        )
        return page("고객", f"<h1>고객</h1>{stats}{toolbar}{table}", active="customers", flash=flash)

    # ---------- 고객 추가·수정 ----------

    def customer_form(self, csrf: str, customer: Customer | None = None, *, values: dict[str, str] | None = None, error: str = "") -> str:
        v = values or {}
        if customer and not values:
            v = {"name": customer.name, "phone": customer.phone, "plan": customer.plan,
                 "expires_on": customer.expires_on.isoformat(), "memo": customer.memo,
                 "active": "1" if customer.active else "", "sms_opt_out": "1" if customer.sms_opt_out else ""}
        elif not values:
            v = {"active": "1", "expires_on": add_months(self.today(), 1).isoformat()}
        action = f"/customers/{customer.id}" if customer else "/customers"
        title = "고객 수정" if customer else "고객 추가"

        def field(name: str, label: str, kind: str = "text", extra: str = "") -> str:
            return f'<label><span>{label}</span><input type="{kind}" name="{name}" value="{esc(v.get(name, ""))}" {extra}></label>'

        def check(name: str, label: str) -> str:
            checked = "checked" if v.get(name) else ""
            return f'<label class="check"><input type="checkbox" name="{name}" value="1" {checked}>{label}</label>'

        form = (
            f'<form method="post" action="{action}" class="card"><input type="hidden" name="csrf" value="{csrf}">'
            '<div class="row">'
            + field("name", "이름", extra="required maxlength=50")
            + field("phone", "휴대폰 번호", "tel", "required placeholder=010-1234-5678")
            + field("plan", "상품/메모 (선택)", extra="maxlength=50")
            + field("expires_on", "만료일", "date", "required")
            + "</div>"
            + f'<label><span>메모 (선택, 고객에게 안 보임)</span><input name="memo" value="{esc(v.get("memo", ""))}" maxlength=200></label>'
            + check("active", "이용 중 (끄면 안내 문자 안 보냄)")
            + check("sms_opt_out", "문자 수신 거부")
            + '<div class="toolbar"><button class="primary">저장</button><a class="btn" href="/">취소</a></div></form>'
        )
        delete = ""
        if customer:
            delete = (
                f'<form method="post" action="/customers/{customer.id}/delete" onsubmit="return confirm(\'삭제할까요? 발송 기록도 함께 지워집니다.\')">'
                f'<input type="hidden" name="csrf" value="{csrf}"><button class="danger">고객 삭제</button></form>'
            )
        return page(title, f"<h1>{title}</h1>{form}{delete}", active="customers", flash=error)

    def parse_customer(self, form: dict[str, str]) -> dict:
        name = form.get("name", "").strip()
        if not name:
            raise ValueError("이름을 입력하세요")
        try:
            phone = normalize_phone(form.get("phone", ""))
        except RecipientError as exc:
            raise ValueError(str(exc)) from None
        return {
            "name": name[:50],
            "phone": phone,
            "plan": form.get("plan", "").strip()[:50],
            "expires_on": parse_date(form.get("expires_on", "")),
            "memo": form.get("memo", "").strip()[:200],
            "active": bool(form.get("active")),
            "sms_opt_out": bool(form.get("sms_opt_out")),
        }

    # ---------- 안내 ----------

    def reminders_page(self, csrf: str, flash: str) -> str:
        today = self.today()
        with self.store() as store:
            dues = reminders.due_reminders(self.cfg, store, today)
            recent = store.recent_reminders(100)
        mode = "자동 발송 (매일 크론)" if self.cfg.auto_send else "수동 (아래 버튼으로만 발송)"
        days = ", ".join("D-DAY" if d == 0 else f"D-{d}" for d in self.cfg.reminder_days)
        head = (
            f'<div class="card">안내 시점 <b>{days}</b> · {self.cfg.send_hour}시 이후 · {esc(mode)} · '
            f"한 번 최대 {self.cfg.max_per_run}건</div>"
        )
        rows = "".join(
            f"<tr><td>{esc(d.customer.name)}</td><td>{esc(d.customer.masked_phone)}</td><td>D-{d.days_before}</td>"
            f'<td class="wrap">{esc(d.text)}</td><td class="muted">{"재시도 " + str(d.attempts) + "회째" if d.attempts else ""}</td></tr>'
            for d in dues
        )
        over = len(dues) > self.cfg.max_per_run
        force = '<label class="check"><input type="checkbox" name="force" value="1">한도 초과를 확인했고 그래도 보냅니다</label>' if over else ""
        send_form = (
            f'<form method="post" action="/reminders/send" onsubmit="return confirm(\'{len(dues)}명에게 지금 문자를 보낼까요?\')">'
            f'<input type="hidden" name="csrf" value="{csrf}">{force}<button class="primary">지금 {len(dues)}명에게 발송</button></form>'
            if dues else '<p class="muted">오늘 보낼 안내가 없습니다.</p>'
        )
        due_table = (
            f'<h1>오늘 보낼 안내 ({len(dues)}명)</h1>'
            + (f'<div class="tablewrap card"><table><thead><tr><th>이름</th><th>번호</th><th>시점</th><th>내용</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>' if dues else "")
            + send_form
        )
        labels = {SENT: '<span class="chip ok">발송</span>', UNKNOWN: '<span class="chip warn">불명</span>', "failed": '<span class="chip bad">실패</span>'}
        log_rows = []
        for r in recent:
            action = ""
            if r.status == UNKNOWN:
                action = (
                    f'<form class="inline" method="post" action="/reminders/clear"><input type="hidden" name="csrf" value="{csrf}">'
                    f'<input type="hidden" name="customer_id" value="{r.customer_id}"><input type="hidden" name="expires_on" value="{esc(r.expires_on)}">'
                    f'<input type="hidden" name="days_before" value="{r.days_before}"><button title="솔라피 콘솔에서 안 나간 것을 확인했을 때">재발송 허용</button></form>'
                )
            log_rows.append(
                f"<tr><td>{esc(self.local_time(r.updated_at))}</td><td>{esc(r.name)}</td><td>{esc(r.expires_on)}</td>"
                f"<td>D-{r.days_before}</td><td>{labels.get(r.status, esc(r.status))}</td><td>{r.attempts}</td>"
                f'<td class="muted">{esc(r.error[:80])}</td><td>{action}</td></tr>'
            )
        log_table = (
            '<h1 style="margin-top:32px">발송 기록</h1><div class="tablewrap"><table><thead><tr><th>시각</th><th>이름</th>'
            '<th>만료일</th><th>시점</th><th>상태</th><th>시도</th><th>오류</th><th></th></tr></thead><tbody>'
            + ("".join(log_rows) or '<tr><td colspan=8 class="muted">기록이 없습니다</td></tr>') + "</tbody></table></div>"
        )
        return page("문자 안내", head + due_table + log_table, active="reminders", flash=flash)

    def local_time(self, stamp: str) -> str:
        try:
            return datetime.fromisoformat(stamp).astimezone(self.cfg.tz).strftime("%m-%d %H:%M")
        except ValueError:
            return stamp

    def send_now(self, force: bool) -> str:
        if not self.send_lock.acquire(blocking=False):
            return "다른 발송이 진행 중입니다. 잠시 후 다시 시도하세요."
        try:
            with self.store() as store:
                dues = reminders.due_reminders(self.cfg, store, self.today())
                if not dues:
                    return "보낼 안내가 없습니다."
                report = reminders.send(dues, self.channel_factory(), store, max_per_run=self.cfg.max_per_run,
                                        gap_seconds=self.cfg.send_gap_seconds, force=force)
            return report.summary()
        finally:
            self.send_lock.release()

    # ---------- 일괄 등록 ----------

    def import_page(self, csrf: str, flash: str = "", text: str = "") -> str:
        example = "이름,전화번호,만료일,상품,메모\n홍길동,010-1234-5678,2026-10-15,프리미엄,\n김철수,01023456789,2026.11.01,,지인 소개"
        body = (
            "<h1>일괄 등록</h1><div class=card><p>엑셀에서 표를 복사해 붙여넣거나 CSV 를 붙여넣으세요. "
            "첫 줄은 머리글 <code>이름,전화번호,만료일,상품,메모</code> (상품·메모는 선택).<br>"
            "<b>이미 있는 번호는 만료일·상품·메모를 덮어씁니다.</b></p>"
            f'<form method="post" action="/import"><input type="hidden" name="csrf" value="{csrf}">'
            f'<textarea name="csv" placeholder="{esc(example)}">{esc(text)}</textarea>'
            '<div class="toolbar" style="margin-top:12px"><button class="primary">등록</button></div></form></div>'
        )
        return page("일괄 등록", body, active="import", flash=flash)

    def import_rows(self, text: str) -> str:
        # 엑셀에서 복사하면 탭으로 구분된다.
        dialect = "excel-tab" if "\t" in text.splitlines()[0] else "excel"
        reader = csv.reader(io.StringIO(text.strip()), dialect=dialect)
        rows = [r for r in reader if any(cell.strip() for cell in r)]
        if not rows:
            raise ValueError("붙여넣은 내용이 없습니다")
        header = [h.strip() for h in rows[0]]
        aliases = {"이름": "name", "name": "name", "전화번호": "phone", "번호": "phone", "phone": "phone",
                   "만료일": "expires_on", "expires_on": "expires_on", "상품": "plan", "plan": "plan", "메모": "memo", "memo": "memo"}
        keys = [aliases.get(h.lower() if h.isascii() else h) for h in header]
        if not {"name", "phone", "expires_on"} <= set(keys):
            raise ValueError("첫 줄 머리글에 이름, 전화번호, 만료일이 있어야 합니다")

        parsed, errors = [], []
        for line_no, row in enumerate(rows[1:], start=2):
            record = {k: (row[i].strip() if i < len(row) else "") for i, k in enumerate(keys) if k}
            try:
                parsed.append(self.parse_customer({**record, "active": "1"}))
            except ValueError as exc:
                errors.append(f"{line_no}번 줄: {exc}")
        if errors:
            # 일부만 들어가면 무엇이 들어갔는지 헷갈린다. 전부 고친 뒤 한 번에 넣는다.
            raise ValueError("아무것도 등록하지 않았습니다. 아래를 고쳐 다시 붙여넣으세요.\n" + "\n".join(errors[:20]))

        added = updated = 0
        with self.store() as store:
            for data in parsed:
                existing = store.customer_by_phone(data["phone"])
                if existing:
                    store.update_customer(existing.id, name=data["name"], phone=data["phone"], expires_on=data["expires_on"],
                                          plan=data["plan"] or existing.plan, memo=data["memo"] or existing.memo,
                                          active=existing.active, sms_opt_out=existing.sms_opt_out)
                    updated += 1
                else:
                    store.add_customer(data["name"], data["phone"], data["expires_on"], plan=data["plan"], memo=data["memo"])
                    added += 1
        return f"새로 등록 {added}명 · 갱신 {updated}명"


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "renewal"
        sys_version = ""

        # ---------- 공통 ----------

        def log_message(self, fmt: str, *args: object) -> None:
            log.debug("%s %s", self.client_address[0], fmt % args)

        def client_ip(self) -> str:
            return self.client_address[0]

        def session(self) -> str | None:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get("sid")
            return app.auth.verify(morsel.value) if morsel else None

        def respond(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; script-src 'unsafe-inline'")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
            self.respond(HTTPStatus.SEE_OTHER, "", {"Location": location, **(headers or {})})

        def cookie_header(self, value: str, max_age: int) -> str:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return f"sid={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

        def form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("요청이 너무 큽니다")
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        # ---------- GET ----------

        def do_GET(self) -> None:  # noqa: N802
            url = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            if url.path == "/login":
                self.respond(HTTPStatus.OK, login_page())
                return
            nonce = self.session()
            if nonce is None:
                self.redirect("/login")
                return
            csrf = app.auth.csrf(nonce)
            flash = query.get("msg", "")
            path = url.path
            try:
                if path == "/":
                    self.respond(HTTPStatus.OK, app.customers_page(query, csrf, flash))
                elif path == "/customers/new":
                    self.respond(HTTPStatus.OK, app.customer_form(csrf))
                elif path.startswith("/customers/") and path.endswith("/edit"):
                    customer = self.load_customer(path.split("/")[2])
                    if customer:
                        self.respond(HTTPStatus.OK, app.customer_form(csrf, customer))
                elif path == "/reminders":
                    self.respond(HTTPStatus.OK, app.reminders_page(csrf, flash))
                elif path == "/import":
                    self.respond(HTTPStatus.OK, app.import_page(csrf, flash))
                elif path == "/logout":
                    self.redirect("/login", {"Set-Cookie": self.cookie_header("", 0)})
                else:
                    self.respond(HTTPStatus.NOT_FOUND, page("없음", "<h1>페이지가 없습니다</h1>"))
            except Exception:  # noqa: BLE001
                log.exception("GET %s", path)
                self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, page("오류", "<h1>서버 오류</h1><p>로그를 확인하세요.</p>"))

        def load_customer(self, raw_id: str) -> Customer | None:
            customer = None
            if raw_id.isdigit():
                with app.store() as store:
                    customer = store.customer(int(raw_id))
            if customer is None:
                self.respond(HTTPStatus.NOT_FOUND, page("없음", "<h1>고객이 없습니다</h1>"))
            return customer

        # ---------- POST ----------

        def do_POST(self) -> None:  # noqa: N802
            path = urlsplit(self.path).path
            try:
                form = self.form()
            except ValueError as exc:
                self.respond(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, page("오류", f"<h1>{esc(exc)}</h1>"))
                return

            if path == "/login":
                self.handle_login(form)
                return
            nonce = self.session()
            if nonce is None:
                self.redirect("/login")
                return
            csrf = app.auth.csrf(nonce)
            if not hmac.compare_digest(form.get("csrf", ""), csrf):
                self.respond(HTTPStatus.FORBIDDEN, page("거부", "<h1>요청이 만료됐습니다. 페이지를 새로고침하세요.</h1>"))
                return
            try:
                self.route_post(path, form, csrf)
            except Exception:  # noqa: BLE001
                log.exception("POST %s", path)
                self.respond(HTTPStatus.INTERNAL_SERVER_ERROR, page("오류", "<h1>서버 오류</h1><p>로그를 확인하세요.</p>"))

        def handle_login(self, form: dict[str, str]) -> None:
            ip = self.client_ip()
            if app.auth.locked(ip):
                self.respond(HTTPStatus.TOO_MANY_REQUESTS, login_page("로그인 시도가 너무 많습니다. 15분 뒤 다시 시도하세요."))
                return
            if not app.auth.check_password(ip, form.get("password", "")):
                log.warning("로그인 실패 %s", ip)
                self.respond(HTTPStatus.UNAUTHORIZED, login_page("비밀번호가 틀렸습니다."))
                return
            self.redirect("/", {"Set-Cookie": self.cookie_header(app.auth.issue(), SESSION_SECONDS)})

        def route_post(self, path: str, form: dict[str, str], csrf: str) -> None:
            parts = path.strip("/").split("/")
            if path == "/customers":
                try:
                    data = app.parse_customer(form)
                    with app.store() as store:
                        store.add_customer(data["name"], data["phone"], data["expires_on"], plan=data["plan"], memo=data["memo"],
                                           active=data["active"], sms_opt_out=data["sms_opt_out"])
                except (ValueError, DuplicatePhone) as exc:
                    self.respond(HTTPStatus.BAD_REQUEST, app.customer_form(csrf, values=form, error=str(exc)))
                    return
                self.redirect("/?" + urlencode({"msg": f"{data['name']} 추가됨"}))
            elif len(parts) == 2 and parts[0] == "customers" and parts[1].isdigit():
                customer = self.load_customer(parts[1])
                if not customer:
                    return
                try:
                    data = app.parse_customer(form)
                    with app.store() as store:
                        store.update_customer(customer.id, **data)
                except (ValueError, DuplicatePhone) as exc:
                    self.respond(HTTPStatus.BAD_REQUEST, app.customer_form(csrf, customer, values=form, error=str(exc)))
                    return
                self.redirect("/?" + urlencode({"msg": f"{data['name']} 저장됨"}))
            elif len(parts) == 3 and parts[0] == "customers" and parts[2] == "extend":
                customer = self.load_customer(parts[1])
                if not customer:
                    return
                months = max(1, min(24, int(form.get("months", "1") or 1)))
                # 이미 만료됐으면 오늘부터, 아니면 기존 만료일부터 연장한다.
                base = max(customer.expires_on, app.today())
                new_expiry = add_months(base, months)
                with app.store() as store:
                    store.set_expiry(customer.id, new_expiry)
                self.redirect("/?" + urlencode({"msg": f"{customer.name} 만료일 {customer.expires_on} → {new_expiry}"}))
            elif len(parts) == 3 and parts[0] == "customers" and parts[2] == "delete":
                customer = self.load_customer(parts[1])
                if not customer:
                    return
                with app.store() as store:
                    store.delete_customer(customer.id)
                self.redirect("/?" + urlencode({"msg": f"{customer.name} 삭제됨"}))
            elif path == "/reminders/send":
                message = app.send_now(force=bool(form.get("force")))
                self.redirect("/reminders?" + urlencode({"msg": message}))
            elif path == "/reminders/clear":
                with app.store() as store:
                    store.clear_reminder(int(form["customer_id"]), date.fromisoformat(form["expires_on"]), int(form["days_before"]))
                self.redirect("/reminders?" + urlencode({"msg": "재발송을 허용했습니다. 다음 발송 때 다시 나갑니다."}))
            elif path == "/import":
                text = form.get("csv", "")
                try:
                    message = app.import_rows(text)
                except (ValueError, DuplicatePhone) as exc:
                    self.respond(HTTPStatus.BAD_REQUEST, app.import_page(csrf, str(exc), text))
                    return
                self.redirect("/?" + urlencode({"msg": message}))
            else:
                self.respond(HTTPStatus.NOT_FOUND, page("없음", "<h1>페이지가 없습니다</h1>"))

    return Handler


def login_page(error: str = "") -> str:
    body = (
        '<div class="login card"><h1>만료 안내 관리</h1>'
        '<form method="post" action="/login"><label><span>비밀번호</span>'
        '<input type="password" name="password" autofocus required autocomplete="current-password"></label>'
        '<button class="primary" style="width:100%">로그인</button></form></div>'
    )
    return page("로그인", body, flash=error, logged_in=False)


def serve(app: App, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(app))
    log.info("관리 사이트: http://%s:%d", host, port)
    return server

