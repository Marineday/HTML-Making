import http.cookiejar
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

from familyinvite.channels import ChannelError
from hotdeal.config import ConfigError
from renewal import config, reminders, web
from renewal.dates import add_months, dday_label, parse_date
from renewal.store import SENT, SKIPPED, UNKNOWN, DuplicatePhone, Store

TODAY = date(2026, 10, 1)
BASE = {"message": "{name}님 {expires}({dday}) 만료", "reminder_days": [7, 1], "admin": {"password": "correct-horse-battery"}}


def make_cfg(db_path, **overrides):
    return config.from_dict({**BASE, "db_path": str(db_path), **overrides})


class Recording:
    name = "rec"

    def __init__(self, error=None):
        self.sent = []
        self.error = error

    def send(self, recipient, text, variables):
        if self.error:
            raise self.error
        self.sent.append((recipient.phone, text))
        return "M1"


class DatesTest(unittest.TestCase):
    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(add_months(date(2026, 12, 15), 1), date(2027, 1, 15))
        self.assertEqual(add_months(date(2026, 11, 30), 3), date(2027, 2, 28))

    def test_parse_date_formats(self):
        for text in ("2026-10-05", "2026.10.05", "2026/10/5", " 2026-10-5 "):
            self.assertEqual(parse_date(text), date(2026, 10, 5))
        with self.assertRaises(ValueError):
            parse_date("10/05")

    def test_dday_label(self):
        self.assertEqual(dday_label(date(2026, 10, 8), TODAY), "D-7")
        self.assertEqual(dday_label(TODAY, TODAY), "D-DAY")
        self.assertEqual(dday_label(date(2026, 9, 29), TODAY), "만료 2일 지남")


class ConfigTest(unittest.TestCase):
    def test_defaults_and_sorted_days(self):
        cfg = config.from_dict({**BASE, "reminder_days": [1, 7, 7]})
        self.assertEqual(cfg.reminder_days, [7, 1])
        self.assertTrue(cfg.auto_send)

    def test_rejects_bad_values(self):
        with self.assertRaisesRegex(ConfigError, "reminder_days"):
            config.from_dict({**BASE, "reminder_days": [-1]})
        with self.assertRaisesRegex(ConfigError, "phone"):
            config.from_dict({**BASE, "message": "{phone}"})
        with self.assertRaisesRegex(ConfigError, "알 수 없는"):
            config.from_dict({**BASE, "sendhour": 1})

    def test_admin_password_length(self):
        with self.assertRaisesRegex(ConfigError, "12자"):
            config.require_admin_password(config.from_dict({**BASE, "admin": {"password": "short"}}))


class ReminderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name) / "r.db")
        self.store = Store(self.cfg.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, name, phone, days_left, **kw):
        return self.store.add_customer(name, phone, date.fromordinal(TODAY.toordinal() + days_left), **kw)

    def due(self, today=TODAY):
        return reminders.due_reminders(self.cfg, self.store, today)

    def send(self, channel=None, today=TODAY, **kw):
        channel = channel or Recording()
        report = reminders.send(self.due(today), channel, self.store, max_per_run=kw.pop("max_per_run", 50), sleep=lambda _: None, **kw)
        return report, channel

    def test_windows(self):
        self.add("멀다", "01000000008", 8)
        self.add("일주일", "01000000007", 7)
        self.add("사흘", "01000000003", 3)
        self.add("당일", "01000000000", 0)
        self.add("지남", "01000000009", -1)
        got = {d.customer.name: d.days_before for d in self.due()}
        # 당일(0)은 reminder_days 에 없어도 D-1 창이 열려 있으므로 D-1 안내 대상이다
        self.assertEqual(got, {"일주일": 7, "사흘": 7, "당일": 1})

    def test_text_rendering(self):
        self.add("홍길동", "01011112222", 7)
        self.assertEqual(self.due()[0].text, "홍길동님 10월 8일(D-7) 만료")

    def test_sends_once_then_next_window(self):
        cid = self.add("홍길동", "01011112222", 7)
        report, channel = self.send()
        self.assertEqual(report.sent, ["홍길동"])
        self.assertEqual(self.send()[1].sent, [])  # 같은 날 재실행
        self.assertEqual(self.due(date(2026, 10, 4)), [])  # 중간 날짜
        later = self.due(date(2026, 10, 7))
        self.assertEqual([d.days_before for d in later], [1])
        self.assertEqual(self.store.reminder(cid, date(2026, 10, 8), 7).status, SENT)

    def test_late_registration_skips_farther_window(self):
        cid = self.add("막차", "01011112222", 1)
        due = self.due()
        self.assertEqual((due[0].days_before, due[0].superseded), (1, [7]))
        self.send()
        self.assertEqual(self.store.reminder(cid, date(2026, 10, 2), 7).status, SKIPPED)
        self.assertNotIn(SKIPPED, [r.status for r in self.store.recent_reminders()])

    def test_extension_starts_new_cycle(self):
        cid = self.add("연장", "01011112222", 7)
        self.send()
        self.store.set_expiry(cid, add_months(date(2026, 10, 8), 1))
        self.assertEqual(self.due(), [])
        self.assertEqual([d.days_before for d in self.due(date(2026, 11, 1))], [7])

    def test_inactive_and_opt_out_excluded(self):
        self.add("중지", "01011112222", 7, active=False)
        self.add("거부", "01033334444", 7, sms_opt_out=True)
        self.assertEqual(self.due(), [])

    def test_failure_retries_until_max_attempts(self):
        self.add("실패", "01011112222", 7)
        for _ in range(3):
            report, _ = self.send(Recording(ChannelError("400 bad")))
            self.assertEqual(len(report.failed), 1)
        self.assertEqual(self.due(), [])

    def test_ambiguous_is_not_retried_until_cleared(self):
        cid = self.add("불명", "01011112222", 7)
        report, _ = self.send(Recording(ChannelError("timeout", ambiguous=True)))
        self.assertEqual(report.unknown, ["불명"])
        self.assertEqual(self.store.reminder(cid, date(2026, 10, 8), 7).status, UNKNOWN)
        self.assertEqual(self.due(), [])
        self.store.clear_reminder(cid, date(2026, 10, 8), 7)
        self.assertEqual(len(self.due()), 1)

    def test_cap_blocks_everything_unless_forced(self):
        for i in range(3):
            self.add(f"고객{i}", f"0101111000{i}", 7)
        report, channel = self.send(max_per_run=2)
        self.assertEqual((report.blocked_by_cap, channel.sent), (3, []))
        self.assertIn("아무것도 보내지 않았습니다", report.summary())
        report, channel = self.send(max_per_run=2, force=True)
        self.assertEqual(len(channel.sent), 3)

    def test_duplicate_phone(self):
        self.add("a", "01011112222", 7)
        with self.assertRaises(DuplicatePhone):
            self.add("b", "01011112222", 3)

    def test_send_time(self):
        self.assertFalse(reminders.is_send_time(self.cfg, datetime(2026, 10, 1, 9, 59)))
        self.assertTrue(reminders.is_send_time(self.cfg, datetime(2026, 10, 1, 10, 0)))


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.now = [1_000_000.0]
        self.auth = web.Auth("correct-horse-battery", "k", clock=lambda: self.now[0])

    def test_session_roundtrip_expiry_and_tamper(self):
        token = self.auth.issue()
        nonce = self.auth.verify(token)
        self.assertIsNotNone(nonce)
        self.assertIsNone(self.auth.verify(token[:-1] + ("0" if token[-1] != "0" else "1")))
        self.assertIsNone(web.Auth("correct-horse-battery", "other-key").verify(token))
        self.now[0] += web.SESSION_SECONDS + 1
        self.assertIsNone(self.auth.verify(token))

    def test_lockout_after_failures(self):
        for _ in range(web.LOGIN_MAX_FAILS):
            self.assertFalse(self.auth.check_password("1.2.3.4", "nope"))
        self.assertTrue(self.auth.locked("1.2.3.4"))
        self.assertFalse(self.auth.locked("5.6.7.8"))
        self.now[0] += web.LOGIN_LOCK_SECONDS + 1
        self.assertFalse(self.auth.locked("1.2.3.4"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class WebTest(unittest.TestCase):
    """실제 소켓에 사이트를 띄워 로그인·CSRF·등록·연장·발송을 확인한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_cfg(Path(self.tmp.name) / "w.db")
        self.channel = Recording()
        self.app = web.App(self.cfg, lambda: self.channel, now=lambda: datetime(2026, 10, 1, 11, 0))
        self.server = web.serve(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, path, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        try:
            with self.opener.open(self.base + path, data=body, timeout=5) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read().decode()

    def login(self):
        status, headers, _ = self.request("/login", {"password": "correct-horse-battery"})
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        _, _, html = self.request("/customers/new")
        return html.split('name="csrf" value="')[1].split('"')[0]

    def test_requires_login_and_csrf(self):
        status, headers, _ = self.request("/")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertEqual(self.request("/login", {"password": "wrong"})[0], 401)
        csrf = self.login()
        self.assertEqual(self.request("/customers", {"name": "x"})[0], 403)
        self.assertEqual(self.request("/customers", {"csrf": csrf, "name": "a", "phone": "01011112222", "expires_on": "2026-10-08"})[0], 303)

    def test_escapes_and_shows_security_headers(self):
        csrf = self.login()
        self.request("/customers", {"csrf": csrf, "name": "<script>x</script>", "phone": "01011112222", "expires_on": "2026-10-08", "active": "1"})
        status, headers, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertNotIn("<script>x", html)
        self.assertIn("&lt;script&gt;x", html)
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_form_errors_keep_input(self):
        csrf = self.login()
        status, _, html = self.request("/customers", {"csrf": csrf, "name": "홍길동", "phone": "12", "expires_on": "2026-10-08"})
        self.assertEqual(status, 400)
        self.assertIn("휴대폰 번호 형식", html)
        self.assertIn('value="홍길동"', html)

    def test_import_all_or_nothing_and_upsert(self):
        csrf = self.login()
        bad = "이름,전화번호,만료일\n가,01011112222,2026-10-08\n나,999,2026-10-08\n"
        status, _, html = self.request("/import", {"csrf": csrf, "csv": bad})
        self.assertEqual(status, 400)
        self.assertIn("3번 줄", html)
        with Store(self.cfg.db_path) as store:
            self.assertEqual(store.customers(), [])
        tsv = "이름\t전화번호\t만료일\t상품\n가\t1011112222\t2026.10.08\t베이직\n"
        self.assertEqual(self.request("/import", {"csrf": csrf, "csv": tsv})[0], 303)
        self.request("/import", {"csrf": csrf, "csv": "이름,전화번호,만료일\n가,010-1111-2222,2026-11-08\n"})
        with Store(self.cfg.db_path) as store:
            [customer] = store.customers()
        self.assertEqual((customer.phone, customer.expires_on, customer.plan), ("01011112222", date(2026, 11, 8), "베이직"))

    def test_extend_from_today_when_expired(self):
        csrf = self.login()
        with Store(self.cfg.db_path) as store:
            live = store.add_customer("유효", "01011112222", date(2026, 10, 20))
            dead = store.add_customer("만료", "01033334444", date(2026, 9, 1))
        self.request(f"/customers/{live}/extend", {"csrf": csrf, "months": "1"})
        self.request(f"/customers/{dead}/extend", {"csrf": csrf, "months": "1"})
        with Store(self.cfg.db_path) as store:
            self.assertEqual(store.customer(live).expires_on, date(2026, 11, 20))
            self.assertEqual(store.customer(dead).expires_on, date(2026, 11, 1))

    def test_send_button(self):
        csrf = self.login()
        with Store(self.cfg.db_path) as store:
            store.add_customer("홍길동", "01011112222", date(2026, 10, 8))
        _, _, html = self.request("/reminders")
        self.assertIn("오늘 보낼 안내 (1명)", html)
        status, headers, _ = self.request("/reminders/send", {"csrf": csrf})
        self.assertEqual(status, 303)
        self.assertIn("%EC%84%B1%EA%B3%B5+1", headers["Location"])  # "성공 1"
        self.assertEqual(self.channel.sent, [("01011112222", "홍길동님 10월 8일(D-7) 만료")])
        self.request("/reminders/send", {"csrf": csrf})
        self.assertEqual(len(self.channel.sent), 1)


if __name__ == "__main__":
    unittest.main()
