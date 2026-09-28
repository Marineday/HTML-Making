import hashlib
import hmac
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from familyinvite import channels, config, recipients, runner, telegram_link
from familyinvite.channels import ChannelError
from familyinvite.config import ConfigError
from familyinvite.recipients import Recipient, RecipientError, normalize_phone
from familyinvite.store import Store

BASE = {"message": "[{month}월] {name}님 {link}", "link": "https://example.com/x"}


def make_cfg(**overrides):
    return config.from_dict({**BASE, **overrides})


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        response = self.responses.pop(0) if self.responses else {"ok": True, "result": {}}
        if isinstance(response, Exception):
            raise response
        return response


class PhoneTest(unittest.TestCase):
    def test_normalizes_common_formats(self):
        for raw in ("010-1234-5678", "01012345678", "+82 10 1234 5678", "821012345678", "+82-010-1234-5678"):
            self.assertEqual(normalize_phone(raw), "01012345678", raw)

    def test_excel_dropped_leading_zero(self):
        self.assertEqual(normalize_phone("1012345678"), "01012345678")

    def test_old_ten_digit_mobile(self):
        self.assertEqual(normalize_phone("011-123-4567"), "0111234567")

    def test_rejects_landline_and_garbage(self):
        for raw in ("02-123-4567", "", "abc", "010-12"):
            with self.assertRaises(RecipientError):
                normalize_phone(raw)


class RecipientsTest(unittest.TestCase):
    def write(self, text, encoding="utf-8"):
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding=encoding)
        handle.write(text)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_loads_with_excel_bom_and_defaults(self):
        path = self.write("name,phone,channel,link,active\n홍길동,010-1234-5678,,,\n김철수,01023456789,telegram,,y\n", "utf-8-sig")
        loaded = recipients.load(path, default_channel="kakao")
        self.assertEqual([r.channel for r in loaded], ["kakao", "telegram"])
        self.assertEqual(loaded[0].phone, "01012345678")

    def test_skips_inactive_blank_and_comment_rows(self):
        path = self.write("name,phone,active\n홍길동,01012345678,n\n,,\n# 메모,01099999999,\n김철수,01023456789,\n")
        self.assertEqual([r.name for r in recipients.load(path)], ["김철수"])

    def test_duplicate_phone_is_an_error_with_line_number(self):
        path = self.write("name,phone\n홍길동,010-1234-5678\n동명이인,+821012345678\n")
        with self.assertRaisesRegex(RecipientError, r":3: 2번 줄"):
            recipients.load(path)

    def test_bad_channel_and_link(self):
        with self.assertRaisesRegex(RecipientError, "channel"):
            recipients.load(self.write("name,phone,channel\n홍,01012345678,email\n"))
        with self.assertRaisesRegex(RecipientError, "link"):
            recipients.load(self.write("name,phone,link\n홍,01012345678,example.com\n"))

    def test_missing_header(self):
        with self.assertRaisesRegex(RecipientError, "phone"):
            recipients.load(self.write("name,tel\n홍,01012345678\n"))

    def test_masked_phone(self):
        self.assertEqual(Recipient("a", "01012345678", "sms").masked_phone, "010-****-5678")


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = make_cfg()
        self.assertEqual((cfg.send_day, cfg.send_hour, cfg.default_channel), (1, 10, "sms"))
        self.assertTrue(cfg.approval_required)

    def test_rejects_day_that_some_months_lack(self):
        with self.assertRaisesRegex(ConfigError, "send_day"):
            make_cfg(send_day=31)

    def test_rejects_unknown_placeholder_and_bad_braces(self):
        with self.assertRaisesRegex(ConfigError, "phone"):
            make_cfg(message="{phone}")
        with self.assertRaisesRegex(ConfigError, "중괄호"):
            make_cfg(message="{name")

    def test_rejects_unknown_key_and_channel(self):
        with self.assertRaisesRegex(ConfigError, "sendday"):
            make_cfg(sendday=1)
        with self.assertRaisesRegex(ConfigError, "channels"):
            make_cfg(channels={"email": {}})

    def test_owner_chat_id(self):
        self.assertEqual(make_cfg(owner={"telegram_chat_id": "42"}).owner_telegram_chat_id, "42")

    def test_env_reference_must_exist(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write('// 주석\n{"message": "{name}", "channels": {"sms": {"api_key": "${NO_SUCH_ENV_FOR_TEST}"}}}')
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaisesRegex(ConfigError, "NO_SUCH_ENV_FOR_TEST"):
            config.load(handle.name)


class SolapiTest(unittest.TestCase):
    OPTIONS = {"api_key": "KEY", "api_secret": "SECRET", "from": "02-123-4567"}
    R = Recipient("홍길동", "01012345678", "sms")

    def test_auth_header_signature(self):
        header = channels.solapi_auth_header("KEY", "SECRET", date="2026-10-01T01:00:00Z", salt="abc")
        expected = hmac.new(b"SECRET", b"2026-10-01T01:00:00Zabc", hashlib.sha256).hexdigest()
        self.assertEqual(header, f"HMAC-SHA256 apiKey=KEY, date=2026-10-01T01:00:00Z, salt=abc, signature={expected}")

    def test_sms_request_and_message_id(self):
        fake = FakeTransport({"messageList": [{"messageId": "M1"}], "failedMessageList": []})
        message_id = channels.SolapiSmsChannel(self.OPTIONS, fake).send(self.R, "안녕", {})
        url, body, headers = fake.calls[0]
        self.assertEqual(url, channels.SOLAPI_SEND_URL)
        self.assertEqual(body, {"messages": [{"to": "01012345678", "from": "021234567", "text": "안녕", "autoTypeDetect": True}],
                                "showMessageList": True})
        self.assertTrue(headers["Authorization"].startswith("HMAC-SHA256 apiKey=KEY,"))
        self.assertNotIn("SECRET", headers["Authorization"])
        self.assertEqual(message_id, "M1")

    def test_failed_message_list_raises(self):
        fake = FakeTransport({"failedMessageList": [{"statusCode": "1062", "statusMessage": "발신번호 미등록"}]})
        with self.assertRaisesRegex(ChannelError, "1062.*발신번호 미등록"):
            channels.SolapiSmsChannel(self.OPTIONS, fake).send(self.R, "x", {})

    def test_http_error_masks_secret(self):
        fake = FakeTransport(ChannelError("HTTP 401: bad SECRET"))
        with self.assertRaises(ChannelError) as ctx:
            channels.SolapiSmsChannel(self.OPTIONS, fake).send(self.R, "x", {})
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_requires_sender_number(self):
        with self.assertRaisesRegex(ChannelError, "from"):
            channels.SolapiSmsChannel({"api_key": "k", "api_secret": "s"}, FakeTransport())

    def test_alimtalk_fills_template_variables(self):
        options = {**self.OPTIONS, "pf_id": "PF", "template_id": "TP", "sms_fallback": False,
                   "variables": {"#{이름}": "{name}", "#{링크}": "{link}"}}
        fake = FakeTransport({"groupInfo": {"groupId": "G1"}})
        message_id = channels.SolapiAlimtalkChannel(options, fake).send(self.R, "무시됨", {"name": "홍길동", "link": "https://l"})
        message = fake.calls[0][1]["messages"][0]
        self.assertNotIn("text", message)
        self.assertEqual(message["kakaoOptions"], {"pfId": "PF", "templateId": "TP",
                                                   "variables": {"#{이름}": "홍길동", "#{링크}": "https://l"}, "disableSms": True})
        self.assertEqual(message_id, "G1")

    def test_alimtalk_rejects_bad_variable_keys(self):
        for key in ("이름", "#{a.b}"):
            options = {**self.OPTIONS, "pf_id": "PF", "template_id": "TP", "variables": {key: "{name}"}}
            with self.assertRaisesRegex(ChannelError, "variables"):
                channels.SolapiAlimtalkChannel(options, FakeTransport())

    def test_ambiguous_flag_survives_secret_masking(self):
        fake = FakeTransport(ChannelError("HTTP 502 SECRET", ambiguous=True))
        with self.assertRaises(ChannelError) as ctx:
            channels.SolapiSmsChannel(self.OPTIONS, fake).send(self.R, "x", {})
        self.assertTrue(ctx.exception.ambiguous)

    def test_inspector_balance_and_template(self):
        calls = []

        def fake_get(url, headers):
            calls.append(url)
            return {"balance": 1234.5, "point": 10} if url.endswith("balance") else {"status": "APPROVED"}

        inspector = channels.SolapiInspector(self.OPTIONS, fake_get)
        self.assertEqual(inspector.balance(), (1234.5, 10.0))
        self.assertEqual(inspector.template("KA01TP 1")["status"], "APPROVED")
        self.assertEqual(calls, [f"{channels.SOLAPI_BASE}/cash/v1/balance", f"{channels.SOLAPI_BASE}/kakao/v2/templates/KA01TP%201"])

    def test_check_alimtalk_template(self):
        variables = {"#{이름}": "{name}", "#{링크}": "{link}"}
        ok = {"status": "APPROVED", "channelId": "PF", "variables": [{"name": "이름"}, {"name": "#{링크}"}]}
        self.assertEqual(channels.check_alimtalk_template(ok, variables, "PF"), [])
        bad = {"status": "INSPECTING", "channelId": "OTHER", "variables": [{"name": "이름"}, {"name": "월"}]}
        problems = " / ".join(channels.check_alimtalk_template(bad, variables, "PF"))
        for fragment in ("INSPECTING", "#{월}", "#{링크}", "OTHER"):
            self.assertIn(fragment, problems)

    def test_build_rejects_unknown_provider(self):
        with self.assertRaisesRegex(ChannelError, "provider"):
            channels.build("sms", {"provider": "twilio"}, lookup_chat_id=lambda _: None)


class HttpClassificationTest(unittest.TestCase):
    """실제 로컬 소켓으로 '확실히 안 나감' 과 '모름' 을 가르는지 확인한다."""

    def serve(self, status, body):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/x"

    def test_4xx_is_definite_and_parses_solapi_error(self):
        url = self.serve(400, {"errorCode": "ValidationError", "errorMessage": "발신번호 미등록"})
        with self.assertRaises(ChannelError) as ctx:
            channels.http_post_json(url, {}, {})
        self.assertFalse(ctx.exception.ambiguous)
        self.assertIn("ValidationError: 발신번호 미등록", str(ctx.exception))

    def test_5xx_is_ambiguous(self):
        with self.assertRaises(ChannelError) as ctx:
            channels.http_post_json(self.serve(502, {}), {}, {})
        self.assertTrue(ctx.exception.ambiguous)

    def test_connection_refused_is_definite(self):
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with self.assertRaises(ChannelError) as ctx:
            channels.http_post_json(f"http://127.0.0.1:{port}/x", {}, {}, timeout=2)
        self.assertFalse(ctx.exception.ambiguous)

    def test_success_returns_json(self):
        self.assertEqual(channels.http_post_json(self.serve(200, {"ok": True}), {}, {}), {"ok": True})


class TelegramChannelTest(unittest.TestCase):
    def test_unlinked_recipient_fails_without_calling_api(self):
        fake = FakeTransport()
        channel = channels.TelegramChannel({"bot_token": "T"}, lambda _: None, fake)
        with self.assertRaisesRegex(ChannelError, "미연결"):
            channel.send(Recipient("a", "01012345678", "telegram"), "x", {})
        self.assertEqual(fake.calls, [])

    def test_sends_to_linked_chat_and_masks_token_on_error(self):
        fake = FakeTransport({"ok": True, "result": {"message_id": 7}}, ChannelError("HTTP 404 /botTOKEN123/sendMessage"))
        channel = channels.TelegramChannel({"bot_token": "TOKEN123"}, lambda _: "555", fake)
        self.assertEqual(channel.send(Recipient("a", "01012345678", "telegram"), "hi", {}), "7")
        self.assertEqual(fake.calls[0][1], {"chat_id": "555", "text": "hi"})
        with self.assertRaises(ChannelError) as ctx:
            channel.send(Recipient("a", "01012345678", "telegram"), "hi", {})
        self.assertNotIn("TOKEN123", str(ctx.exception))


class StoreMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class RecordingChannel:
    name = "rec"

    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    def send(self, recipient, text, variables):
        if recipient.phone in self.fail_for:
            raise ChannelError("boom")
        self.sent.append((recipient.phone, text))
        return f"id-{recipient.phone}"


class RunnerTest(StoreMixin, unittest.TestCase):
    PEOPLE = [Recipient("홍길동", "01011111111", "sms"), Recipient("김철수", "01022222222", "sms", link="https://own")]

    def run_once(self, cfg, channel, dry_run=False):
        planned = runner.plan(cfg, self.PEOPLE, self.store, "2026-10")
        sleeps = []
        report = runner.run(planned, {"sms": channel}, self.store, "2026-10", gap_seconds=1.5, dry_run=dry_run, sleep=sleeps.append)
        return report, sleeps

    def test_is_due(self):
        cfg = make_cfg(send_day=5, send_hour=10)
        self.assertFalse(runner.is_due(cfg, datetime(2026, 10, 4, 23)))
        self.assertFalse(runner.is_due(cfg, datetime(2026, 10, 5, 9)))
        self.assertTrue(runner.is_due(cfg, datetime(2026, 10, 5, 10)))
        self.assertTrue(runner.is_due(cfg, datetime(2026, 10, 20, 0)))

    def test_render_uses_personal_link_and_month_without_zero(self):
        text, _ = runner.render(make_cfg(), self.PEOPLE[1], "2026-03")
        self.assertEqual(text, "[3월] 김철수님 https://own")

    def test_sends_once_per_month_with_gap(self):
        cfg = make_cfg()
        channel = RecordingChannel()
        report, sleeps = self.run_once(cfg, channel)
        self.assertEqual(report.sent, ["홍길동", "김철수"])
        self.assertEqual(sleeps, [1.5])
        report, _ = self.run_once(cfg, channel)
        self.assertEqual((report.sent, report.already), ([], 2))
        self.assertEqual(len(channel.sent), 2)
        self.assertEqual(self.store.get("2026-10", "01011111111").message_id, "id-01011111111")

    def test_failure_is_retried_until_max_attempts(self):
        cfg = make_cfg(max_attempts=2)
        channel = RecordingChannel(fail_for={"01022222222"})
        report, _ = self.run_once(cfg, channel)
        self.assertEqual(report.failed, [("김철수", "boom")])
        report, _ = self.run_once(cfg, channel)
        self.assertEqual(len(report.failed), 1)
        report, _ = self.run_once(cfg, channel)
        self.assertEqual((report.failed, report.gave_up), ([], ["김철수"]))
        self.assertIn("재시도 한도 초과", report.summary())

    def test_ambiguous_failure_is_not_retried(self):
        class Timeout(RecordingChannel):
            def send(self, recipient, text, variables):
                raise ChannelError("timed out", ambiguous=True)

        report, _ = self.run_once(make_cfg(max_attempts=5), Timeout())
        self.assertEqual((report.unknown, report.failed), (["홍길동", "김철수"], []))
        channel = RecordingChannel()
        report, _ = self.run_once(make_cfg(max_attempts=5), channel)
        self.assertEqual((channel.sent, report.unknown), ([], ["홍길동", "김철수"]))
        self.assertIn("전송 여부 불명", report.summary())
        self.store.reset("2026-10", "01011111111")
        self.run_once(make_cfg(), channel)
        self.assertEqual([p for p, _ in channel.sent], ["01011111111"])

    def test_unexpected_exception_counts_as_unknown(self):
        class Broken(RecordingChannel):
            def send(self, recipient, text, variables):
                raise KeyError("x")

        import logging

        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        report, _ = self.run_once(make_cfg(), Broken())
        self.assertEqual(report.unknown, ["홍길동", "김철수"])

    def test_link_noscheme_placeholder(self):
        _, variables = runner.render(make_cfg(), self.PEOPLE[1], "2026-10")
        self.assertEqual(variables["link_noscheme"], "own")
        self.assertEqual(runner.render(make_cfg(message="{link_noscheme}"), self.PEOPLE[0], "2026-10")[0], "example.com/x")

    def test_dry_run_records_nothing(self):
        channel = RecordingChannel()
        self.run_once(make_cfg(), channel, dry_run=True)
        self.assertEqual(channel.sent, [])
        self.assertIsNone(self.store.get("2026-10", "01011111111"))

    def test_missing_channel_is_recorded_as_failure(self):
        planned = runner.plan(make_cfg(), self.PEOPLE[:1], self.store, "2026-10")
        report = runner.run(planned, {}, self.store, "2026-10", sleep=lambda _: None)
        self.assertIn("channels.sms", report.failed[0][1])

    def test_reset_allows_resend(self):
        channel = RecordingChannel()
        self.run_once(make_cfg(), channel)
        self.assertEqual(self.store.reset("2026-10", "01011111111"), 1)
        report, _ = self.run_once(make_cfg(), channel)
        self.assertEqual(report.sent, ["홍길동"])

    def test_validate_links(self):
        cfg = make_cfg(link="")
        self.assertEqual(runner.validate_links(cfg, self.PEOPLE), ["홍길동"])
        self.assertEqual(runner.validate_links(make_cfg(link="", message="{name}"), self.PEOPLE), [])

    def test_message_kind(self):
        self.assertEqual(runner.message_kind("가" * 45), "SMS 90B")
        self.assertEqual(runner.message_kind("가" * 46), "LMS 92B")
        self.assertIn("초과", runner.message_kind("가" * 1001))

    def test_approval_flags(self):
        self.assertFalse(self.store.is_approved("2026-10"))
        self.store.mark_notified("2026-10")
        self.assertFalse(self.store.is_approved("2026-10"))
        self.store.approve("2026-10")
        self.assertTrue(self.store.is_approved("2026-10") and self.store.was_notified("2026-10"))
        self.assertFalse(self.store.is_approved("2026-11"))


def update(update_id, *, text=None, contact=None, sender=100, chat_type="private"):
    message = {"chat": {"id": 900, "type": chat_type}, "from": {"id": sender}}
    if text is not None:
        message["text"] = text
    if contact is not None:
        message["contact"] = contact
    return {"update_id": update_id, "message": message}


class TelegramLinkTest(StoreMixin, unittest.TestCase):
    PEOPLE = {"01012345678": Recipient("홍길동", "01012345678", "telegram")}

    def test_start_offers_share_button(self):
        chat_id, text, markup = telegram_link.handle_update(update(1, text="/start"), self.PEOPLE, self.store)
        self.assertEqual(chat_id, "900")
        self.assertTrue(markup["keyboard"][0][0]["request_contact"])

    def test_links_own_contact_on_list(self):
        reply = telegram_link.handle_update(update(1, contact={"phone_number": "821012345678", "user_id": 100}), self.PEOPLE, self.store)
        self.assertIn("홍길동", reply[1])
        self.assertEqual(self.store.telegram_chat_id("01012345678"), "900")

    def test_rejects_someone_elses_contact(self):
        reply = telegram_link.handle_update(update(1, contact={"phone_number": "01012345678", "user_id": 999}), self.PEOPLE, self.store)
        self.assertIn("본인", reply[1])
        self.assertIsNone(self.store.telegram_chat_id("01012345678"))

    def test_rejects_number_not_on_list(self):
        reply = telegram_link.handle_update(update(1, contact={"phone_number": "+821099999999", "user_id": 100}), self.PEOPLE, self.store)
        self.assertIn("명단에 없는", reply[1])

    def test_ignores_group_chats_and_chatter(self):
        self.assertIsNone(telegram_link.handle_update(update(1, text="/start", chat_type="group"), self.PEOPLE, self.store))
        self.assertIsNone(telegram_link.handle_update(update(1, text="안녕"), self.PEOPLE, self.store))

    def test_poll_once_replies_and_advances_offset(self):
        fake = FakeTransport(
            {"ok": True, "result": [update(41, text="/start"), update(42, contact={"phone_number": "01012345678", "user_id": 100})]},
            {"ok": True, "result": {}},
            {"ok": True, "result": {}},
        )
        handled = telegram_link.poll_once("T", list(self.PEOPLE.values()), self.store, transport=fake)
        self.assertEqual(handled, 2)
        self.assertEqual(self.store.get_value(telegram_link.OFFSET_KEY), "43")
        self.assertEqual(fake.calls[0][1]["offset"], 0)
        self.assertEqual([c[0].rsplit("/", 1)[1] for c in fake.calls], ["getUpdates", "sendMessage", "sendMessage"])


class CliTest(unittest.TestCase):
    """console 경로로 승인 게이트 → 발송 → 중복 방지를 실제 CLI 로 확인한다."""

    def setUp(self):
        import json

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "r.csv").write_text("name,phone\n홍길동,010-1234-5678\n", encoding="utf-8")
        self.cfg = root / "invite.json"
        self.cfg.write_text(json.dumps({
            **BASE, "recipients_path": str(root / "r.csv"), "db_path": str(root / "i.db"),
            "send_gap_seconds": 0, "channels": {"sms": {"provider": "console"}},
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def invite(self, *args):
        import contextlib
        import io

        import invite

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = invite.main(["-c", str(self.cfg), "--log-level", "ERROR", *args])
        return code, out.getvalue()

    def test_send_waits_for_approval_then_sends_once(self):
        code, out = self.invite("send", "--period", "2026-10")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.invite("approve", "--period", "2026-10")[0], 0)
        code, out = self.invite("send", "--period", "2026-10")
        self.assertEqual(code, 0)
        self.assertIn("[10월] 홍길동님 https://example.com/x", out)
        self.assertIn("성공 1명", out)
        code, out = self.invite("send", "--period", "2026-10")
        self.assertNotIn("홍길동님", out)

    def test_concurrent_send_is_refused(self):
        import fcntl

        with open(Path(self.tmp.name) / "i.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            import contextlib
            import io

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, _ = self.invite("send", "--period", "2026-10")
        self.assertEqual(code, 2)
        self.assertIn("다른 send", err.getvalue())

    def test_solapi_check_without_solapi_routes(self):
        code, out = self.invite("solapi-check")
        self.assertEqual(code, 0)
        self.assertIn("솔라피를 쓰는 경로", out)

    def test_rejects_bad_period(self):
        self.assertEqual(self.invite("status", "--period", "2026-13")[0], 2)


if __name__ == "__main__":
    unittest.main()
