"""ipalloc.proxy 검증 — 모델, sticky 세션, 비밀번호 참조."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from ipalloc.proxy import (
    DEFAULT_STICKY_TEMPLATE,
    ProxyEndpoint,
    ProxyError,
    dedupe_proxies,
    expand_env,
    session_id,
    sticky_username,
)


class EnvTest(unittest.TestCase):
    def test_expands_and_fails_loudly(self) -> None:
        with mock.patch.dict(os.environ, {"IPALLOC_TEST_PW": "s3cret"}, clear=False):
            self.assertEqual(expand_env("${IPALLOC_TEST_PW}", field="x"), "s3cret")
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProxyError) as ctx:
                expand_env("${NOPE_NOT_SET}", field="password")
            self.assertIn("NOPE_NOT_SET", str(ctx.exception))

    def test_leaves_plain_text_alone(self) -> None:
        self.assertEqual(expand_env("plain", field="x"), "plain")


class ProxyEndpointTest(unittest.TestCase):
    def test_rejects_bad_input(self) -> None:
        for kwargs in (
            {"host": "", "port": 8000},
            {"host": "has space", "port": 8000},
            {"host": "p.example.com", "port": 0},
            {"host": "p.example.com", "port": 70000},
            {"host": "p.example.com", "port": 8000, "protocol": "ftp"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProxyError):
                ProxyEndpoint(**kwargs)

    def test_sticky_template_must_have_a_session_slot(self) -> None:
        # {session} 이 없으면 모든 묶음이 같은 사용자명을 써서 출구가 갈린다.
        with self.assertRaises(ProxyError):
            ProxyEndpoint(host="p.example.com", port=8000, sticky_template="{username}-fixed")

    def test_blank_template_falls_back_to_default(self) -> None:
        self.assertEqual(ProxyEndpoint(host="p.example.com", port=8000, sticky_template="  ").sticky_template, DEFAULT_STICKY_TEMPLATE)

    def test_password_env_var_detection(self) -> None:
        ref = ProxyEndpoint(host="p.example.com", port=8000, username="u", password="${MY_PW}")
        self.assertEqual(ref.password_env_var, "MY_PW")
        plain = ProxyEndpoint(host="p.example.com", port=8000, username="u", password="literal")
        self.assertIsNone(plain.password_env_var)

    def test_resolve_password_reads_env_only_when_asked(self) -> None:
        ep = ProxyEndpoint(host="p.example.com", port=8000, username="u", password="${MY_PW}")
        # 모델 자체는 참조를 그대로 들고 있어야 한다.
        self.assertEqual(ep.password, "${MY_PW}")
        with mock.patch.dict(os.environ, {"MY_PW": "hunter2"}, clear=False):
            self.assertEqual(ep.resolve_password(), "hunter2")

    def test_redacted_never_leaks_the_password(self) -> None:
        ep = ProxyEndpoint(host="p.example.com", port=8000, username="user", password="literal-secret")
        self.assertNotIn("literal-secret", ep.redacted())
        self.assertEqual(ep.redacted(), "socks5://user@p.example.com:8000")

    def test_needs_auth(self) -> None:
        self.assertFalse(ProxyEndpoint(host="p.example.com", port=8000).needs_auth)
        self.assertTrue(ProxyEndpoint(host="p.example.com", port=8000, username="u").needs_auth)


class StickyTest(unittest.TestCase):
    def test_session_id_is_deterministic(self) -> None:
        # 재시작마다 세션이 바뀌면 "묶음은 같은 IP" 전제가 매번 깨진다.
        self.assertEqual(session_id("g001"), session_id("g001"))
        self.assertNotEqual(session_id("g001"), session_id("g002"))

    def test_salt_rotates_every_group_at_once(self) -> None:
        self.assertNotEqual(session_id("g001"), session_id("g001", salt="v2"))

    def test_username_is_rendered_into_the_template(self) -> None:
        ep = ProxyEndpoint(host="p.example.com", port=8000, username="customer1")
        name = sticky_username(ep, "g007")
        self.assertTrue(name.startswith("customer1-session-"))
        self.assertEqual(name, f"customer1-session-{session_id('g007')}")

    def test_custom_template(self) -> None:
        ep = ProxyEndpoint(host="p.example.com", port=8000, username="u", sticky_template="{username}_sess{session}_kr")
        self.assertEqual(sticky_username(ep, "g001"), f"u_sess{session_id('g001')}_kr")

    def test_no_username_means_no_auth(self) -> None:
        self.assertEqual(sticky_username(ProxyEndpoint(host="p.example.com", port=8000), "g001"), "")


class DedupeTest(unittest.TestCase):
    def test_same_host_port_user_collapses(self) -> None:
        a = ProxyEndpoint(host="p.example.com", port=8000, username="u")
        b = ProxyEndpoint(host="p.example.com", port=8000, username="u", region="kr")
        c = ProxyEndpoint(host="p.example.com", port=8001, username="u")
        self.assertEqual(len(dedupe_proxies([a, b, c])), 2)


if __name__ == "__main__":
    unittest.main()
