"""ipalloc.cli 검증 — 실제 운영 흐름을 처음부터 끝까지 한 번 돌린다."""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ipalloc.cli import main
from ipalloc.csvio import read_assignments


def run(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue() + err.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class PlanTest(CliTestCase):
    def test_reports_ip_count_and_capacity(self) -> None:
        code, text = run("plan", "--users", "1000", "--per-ip", "20")
        self.assertEqual(code, 0)
        self.assertIn("출구 IP 50개가 필요하다", text)
        self.assertIn("최악 필요 대역", text)
        self.assertIn("여유 있다", text)

    def test_warns_when_the_link_is_too_small(self) -> None:
        code, text = run("plan", "--users", "1000", "--per-ip", "20", "--link-mbps", "50")
        self.assertEqual(code, 0)
        self.assertIn("!", text)
        self.assertIn("Mbps 가 필요", text)

    def test_warns_when_tunnel_range_is_too_small(self) -> None:
        code, text = run("plan", "--users", "10000", "--per-ip", "20", "--tunnel-cidr", "10.77.0.0/20")
        self.assertEqual(code, 0)
        self.assertIn("터널 대역이 부족하다", text)


class CostTest(CliTestCase):
    ARGS = ("cost", "--users", "1000", "--per-ip", "20", "--dc-ip-monthly", "3.6", "--dc-hosts", "2", "--dc-host-monthly", "20")

    def test_residential_loses_at_realistic_per_gb(self) -> None:
        code, text = run(*self.ARGS, "--dc-egress-per-gb", "0.09", "--res-per-gb", "2.0")
        self.assertEqual(code, 0)
        self.assertIn("월 전송량 750 GB", text)
        self.assertIn("초과", text)
        self.assertIn("손익분기 GB 단가: 0.3300", text)
        # 레지덴셜을 써도 진입 호스트는 필요하다는 점이 드러나야 한다.
        self.assertIn("대체가 아니라 추가", text)

    def test_residential_wins_below_breakeven(self) -> None:
        code, text = run(*self.ARGS, "--dc-egress-per-gb", "0.09", "--res-per-gb", "0.1")
        self.assertEqual(code, 0)
        self.assertIn("저렴", text)

    def test_static_isp_per_ip_pricing(self) -> None:
        code, text = run(*self.ARGS, "--res-ip-monthly", "5.0")
        self.assertEqual(code, 0)
        self.assertIn("250.00", text)  # 50 x 5.0

    def test_requires_a_datacenter_price(self) -> None:
        code, text = run("cost", "--users", "1000", "--per-ip", "20")
        self.assertEqual(code, 1)
        self.assertIn("--dc-ip-monthly", text)

    def test_scale_shows_metered_is_flat_and_names_the_crossover(self) -> None:
        # 1인당 100MB / 60분 → 전체 월 100GB
        code, text = run(
            *self.ARGS, "--minutes", "60", "--mbps", "0.2222", "--dc-egress-per-gb", "0.09", "--res-per-gb", "2.0",
            "--scale", "5", "10", "20", "50",
        )
        self.assertEqual(code, 0)
        self.assertIn("월 전송량 100 GB", text)
        # 스케일 표에서 종량 총액이 네 줄 모두 같아야 한다. (위쪽 합계까지 세지 않도록 구간을 자른다)
        table = text.split("[IP 개수를 늘렸을 때]", 1)[1]
        self.assertEqual(table.count("239.98"), 4)
        self.assertIn("교차점: 출구 IP 54개부터 종량제가 싸다", text)
        self.assertIn("200개", text)  # 묶음당 5명

    def test_says_when_fixed_cost_alone_already_loses(self) -> None:
        # 레지덴셜에 IP 월정액까지 붙으면 GB 단가가 0이어도 못 이기는 경우가 나온다.
        code, text = run("cost", "--users", "1000", "--per-ip", "20", "--dc-ip-monthly", "1.0", "--res-ip-monthly", "20.0")
        self.assertEqual(code, 0)
        self.assertIn("GB 단가가 0이어도 이길 수 없는 구조", text)


class FullFlowTest(CliTestCase):
    def test_sample_assign_verify_export(self) -> None:
        users = self.dir / "users.csv"
        out = self.dir / "out"

        code, _ = run("sample-users", "--count", "1000", "--out", str(users))
        self.assertEqual(code, 0)

        code, text = run("assign", "--users", str(users), "--cidr", "203.0.113.0/26", "--out", str(out), "--per-ip", "20")
        self.assertEqual(code, 0)
        self.assertIn("사용자 1000명 · 묶음 50개", text)
        self.assertIn("예제용(RFC5737)", text)  # 문서용 대역 경고

        assignments = out / "assignments.csv"
        rows = read_assignments(assignments)
        self.assertEqual(len(rows), 1000)
        self.assertEqual(len({r.egress_ip for r in rows}), 50)

        code, text = run("verify", "--assignments", str(assignments), "--per-ip", "20")
        self.assertEqual(code, 0)
        self.assertIn("[정상]", text)

        code, text = run("wg", "--assignments", str(assignments), "--out", str(out / "wg"), "--per-ip", "20")
        self.assertEqual(code, 0)
        self.assertIn("서버 설정 50개, 사용자 설정 1000개", text)
        self.assertIn("전체 터널", text)  # AllowedIPs 기본값 경고
        self.assertTrue((out / "wg" / "genkeys.sh").exists())

    def test_incremental_assign_keeps_people_in_place(self) -> None:
        users = self.dir / "users.csv"
        users.write_text("user_id\n" + "".join(f"u{i:04d}\n" for i in range(1, 101)), encoding="utf-8")
        out = self.dir / "out"
        run("assign", "--users", str(users), "--cidr", "203.0.113.0/26", "--out", str(out), "--per-ip", "20")
        before = {a.user_id: a.egress_ip for a in read_assignments(out / "assignments.csv")}

        # 한 명 빠지고 한 명 들어온다.
        users.write_text(
            "user_id\n" + "".join(f"u{i:04d}\n" for i in range(1, 101) if i != 5) + "u9999\n",
            encoding="utf-8",
        )
        out2 = self.dir / "out2"
        code, text = run(
            "assign",
            "--users",
            str(users),
            "--cidr",
            "203.0.113.0/26",
            "--out",
            str(out2),
            "--per-ip",
            "20",
            "--existing",
            str(out / "assignments.csv"),
        )
        self.assertEqual(code, 0)
        self.assertIn("유지 99", text)
        self.assertIn("신규 1", text)
        self.assertIn("명부에서 빠진 사용자 1명", text)

        after = {a.user_id: a.egress_ip for a in read_assignments(out2 / "assignments.csv")}
        self.assertNotIn("u0005", after)
        for uid, ip in before.items():
            if uid == "u0005":
                continue
            self.assertEqual(after[uid], ip, f"{uid} 의 출구 IP 가 바뀌었다")


class ProxyModeTest(CliTestCase):
    """레지덴셜 경로 — 내보내기부터 실제 SOCKS5 확인까지."""

    def _setup_assignments(self, count: int = 100, per_ip: int = 20) -> Path:
        users = self.dir / "users.csv"
        run("sample-users", "--count", str(count), "--out", str(users))
        out = self.dir / "out"
        run("assign", "--users", str(users), "--cidr", "198.18.0.0/26", "--out", str(out), "--per-ip", str(per_ip))
        return out / "assignments.csv"

    def _write_proxies(self, rows: list[str]) -> Path:
        p = self.dir / "proxies.csv"
        p.write_text("host,port,protocol,username,password\n" + "".join(rows), encoding="utf-8")
        return p

    def test_export_produces_a_runnable_bundle(self) -> None:
        assignments = self._setup_assignments()
        proxies = self._write_proxies([f"gw{i}.example.com,8000,socks5,cust,${{PROXY_PW}}\n" for i in range(1, 6)])
        out = self.dir / "px"
        code, text = run(
            "proxy-export", "--assignments", str(assignments), "--proxies", str(proxies), "--entry-host", "vpn.example.com", "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertIn("묶음 5개, 사용자 설정 100개", text)
        self.assertIn("공인 IP 1개면 된다", text)
        self.assertTrue((out / "wg0.conf").exists())
        self.assertEqual(len(list((out / "redsocks").glob("*.conf"))), 5)

    def test_export_warns_about_plaintext_passwords(self) -> None:
        assignments = self._setup_assignments()
        proxies = self._write_proxies([f"gw{i}.example.com,8000,socks5,cust,literalpw\n" for i in range(1, 6)])
        code, text = run(
            "proxy-export", "--assignments", str(assignments), "--proxies", str(proxies), "--entry-host", "vpn.example.com", "--out", str(self.dir / "px")
        )
        self.assertEqual(code, 0)
        self.assertIn("평문 비밀번호가 5건", text)

    def test_export_refuses_when_proxies_are_short(self) -> None:
        assignments = self._setup_assignments()
        proxies = self._write_proxies(["gw1.example.com,8000,socks5,cust,${PROXY_PW}\n"])
        code, text = run(
            "proxy-export", "--assignments", str(assignments), "--proxies", str(proxies), "--entry-host", "vpn.example.com", "--out", str(self.dir / "px")
        )
        self.assertEqual(code, 1)
        self.assertIn("프록시가 부족하다", text)

    def test_check_reports_real_egress_ips(self) -> None:
        from tests.test_ipalloc_proxycheck import FakeSocks5Server

        # 묶음마다 다른 출구를 주는 서버 두 대. 한 대로 두 줄을 쓰면
        # host:port:username 이 같아 dedupe 에 걸린다 — 그게 올바른 동작이다.
        first = FakeSocks5Server(egress_ip="198.51.100.42", require_auth=True, password="hunter2")
        second = FakeSocks5Server(egress_ip="198.51.100.77", require_auth=True, password="hunter2")
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        assignments = self._setup_assignments(count=40)
        proxies = self._write_proxies(
            [f"127.0.0.1,{first.port},socks5,cust,${{IPALLOC_TEST_PW}}\n", f"127.0.0.1,{second.port},socks5,cust,${{IPALLOC_TEST_PW}}\n"]
        )
        results_csv = self.dir / "run1.csv"

        with mock.patch.dict(os.environ, {"IPALLOC_TEST_PW": "hunter2"}, clear=False):
            code, text = run(
                "proxy-check", "--assignments", str(assignments), "--proxies", str(proxies),
                "--echo-url", "http://echo.example.com/", "--timeout", "5", "--out", str(results_csv),
            )
        self.assertEqual(code, 0)
        self.assertIn("198.51.100.42", text)
        self.assertIn("198.51.100.77", text)
        self.assertIn("고유 출구 IP 2개", text)
        self.assertNotIn("같은 출구 IP", text)
        self.assertTrue(results_csv.exists())
        # 묶음마다 다른 sticky 사용자명을 제시해야 한다.
        self.assertNotEqual(first.seen_usernames, second.seen_usernames)

    def test_check_flags_groups_that_share_an_exit(self) -> None:
        from tests.test_ipalloc_proxycheck import FakeSocks5Server

        # 두 프록시가 같은 출구를 주면 sticky 가 묶음별로 갈리지 않은 것이다.
        first = FakeSocks5Server(egress_ip="198.51.100.42")
        second = FakeSocks5Server(egress_ip="198.51.100.42")
        self.addCleanup(first.close)
        self.addCleanup(second.close)

        assignments = self._setup_assignments(count=40)
        proxies = self._write_proxies([f"127.0.0.1,{first.port},socks5,,\n", f"127.0.0.1,{second.port},socks5,,\n"])
        code, text = run(
            "proxy-check", "--assignments", str(assignments), "--proxies", str(proxies), "--echo-url", "http://e.example.com/", "--timeout", "5"
        )
        self.assertEqual(code, 0)
        self.assertIn("같은 출구 IP", text)
        self.assertIn("g001, g002", text)

    def test_check_reports_auth_failure_without_crashing(self) -> None:
        from tests.test_ipalloc_proxycheck import FakeSocks5Server

        server = FakeSocks5Server(require_auth=True, password="right")
        self.addCleanup(server.close)
        assignments = self._setup_assignments(count=20)
        proxies = self._write_proxies([f"127.0.0.1,{server.port},socks5,cust,${{IPALLOC_TEST_PW}}\n"])

        with mock.patch.dict(os.environ, {"IPALLOC_TEST_PW": "wrong"}, clear=False):
            code, text = run("proxy-check", "--assignments", str(assignments), "--proxies", str(proxies), "--echo-url", "http://e.example.com/", "--timeout", "5")
        self.assertEqual(code, 1)
        self.assertIn("인증에 실패", text)

    def test_check_fails_loudly_when_password_env_is_missing(self) -> None:
        assignments = self._setup_assignments(count=20)
        proxies = self._write_proxies(["gw1.example.com,8000,socks5,cust,${NOPE_NOT_SET_ANYWHERE}\n"])
        code, text = run("proxy-check", "--assignments", str(assignments), "--proxies", str(proxies), "--timeout", "2")
        self.assertEqual(code, 1)
        self.assertIn("NOPE_NOT_SET_ANYWHERE", text)

    def test_sticky_compares_two_runs(self) -> None:
        before = self.dir / "b.csv"
        after = self.dir / "a.csv"
        header = "group_id,ok,egress_ip,elapsed_ms,error\n"
        before.write_text(header + "g001,1,198.51.100.1,10,\ng002,1,198.51.100.2,10,\n", encoding="utf-8")
        after.write_text(header + "g001,1,198.51.100.1,10,\ng002,1,198.51.100.99,10,\n", encoding="utf-8")
        code, text = run("proxy-sticky", "--before", str(before), "--after", str(after))
        self.assertEqual(code, 0)
        self.assertIn("유지 1개", text)
        self.assertIn("변경 1개", text)


class FailureTest(CliTestCase):
    def test_not_enough_ips_is_an_error_by_default(self) -> None:
        users = self.dir / "users.csv"
        run("sample-users", "--count", "100", "--out", str(users))
        code, text = run("assign", "--users", str(users), "--cidr", "203.0.113.0/29", "--out", str(self.dir / "o"), "--per-ip", "5")
        self.assertEqual(code, 1)
        self.assertIn("출구 IP 가 부족하다", text)

    def test_allow_partial_reports_instead_of_failing_early(self) -> None:
        users = self.dir / "users.csv"
        run("sample-users", "--count", "100", "--out", str(users))
        code, text = run(
            "assign", "--users", str(users), "--cidr", "203.0.113.0/29", "--out", str(self.dir / "o"), "--per-ip", "5", "--allow-partial"
        )
        self.assertEqual(code, 1)  # verify 가 미배정을 문제로 잡는다
        self.assertIn("미배정 70", text)

    def test_missing_file_is_reported_not_traced(self) -> None:
        code, text = run("assign", "--users", str(self.dir / "nope.csv"), "--cidr", "203.0.113.0/29", "--out", str(self.dir / "o"))
        self.assertEqual(code, 1)
        self.assertIn("오류:", text)
        self.assertIn("파일이 없다", text)

    def test_unwritable_out_path_is_reported_not_traced(self) -> None:
        users = self.dir / "users.csv"
        run("sample-users", "--count", "10", "--out", str(users))
        blocker = self.dir / "blocker"
        blocker.write_text("파일이지 디렉터리가 아니다\n", encoding="utf-8")
        code, text = run("assign", "--users", str(users), "--cidr", "203.0.113.0/29", "--out", str(blocker / "sub"))
        self.assertEqual(code, 1)
        self.assertIn("오류:", text)
        self.assertNotIn("Traceback", text)

    def test_wg_refuses_to_export_a_broken_table(self) -> None:
        bad = self.dir / "bad.csv"
        bad.write_text(
            "user_id,group_id,egress_ip,tunnel_ip,listen_host,listen_port,label\n"
            "u001,g001,203.0.113.1,10.77.0.2,203.0.113.1,51820,\n"
            "u001,g002,203.0.113.2,10.77.1.2,203.0.113.2,51820,\n",
            encoding="utf-8",
        )
        code, text = run("wg", "--assignments", str(bad), "--out", str(self.dir / "wg"))
        self.assertEqual(code, 1)
        self.assertIn("중복 배정", text)
        self.assertFalse((self.dir / "wg").exists())


if __name__ == "__main__":
    unittest.main()
