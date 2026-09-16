"""ipalloc.csvio 검증.

명부는 엑셀에서 온다. BOM 과 컬럼명 흔들림을 견디지 못하면 운영에서 바로 막힌다.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ipalloc.allocator import allocate
from ipalloc.csvio import (
    ASSIGNMENT_COLUMNS,
    CsvError,
    read_assignments,
    read_endpoints,
    read_users,
    write_assignments,
    write_groups,
    write_users,
)
from ipalloc.models import User
from ipalloc.pool import TunnelPlan, endpoints_from_cidr


class CsvTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        p = self.dir / name
        p.write_text(text, encoding=encoding)
        return p


class ReadUsersTest(CsvTestCase):
    def test_plain(self) -> None:
        p = self.write("u.csv", "user_id,label\nu001,갑\nu002,을\n")
        users = read_users(p)
        self.assertEqual([u.id for u in users], ["u001", "u002"])
        self.assertEqual(users[0].label, "갑")

    def test_bom_and_column_aliases(self) -> None:
        # 엑셀이 저장한 UTF-8 CSV 는 BOM 이 붙고, 컬럼명이 id/name 인 경우가 흔하다.
        p = self.write("u.csv", "id,name\nu001,갑\n", encoding="utf-8-sig")
        users = read_users(p)
        self.assertEqual(users[0].id, "u001")
        self.assertEqual(users[0].label, "갑")

    def test_skips_blank_rows(self) -> None:
        p = self.write("u.csv", "user_id\nu001\n\n\nu002\n")
        self.assertEqual(len(read_users(p)), 2)

    def test_reports_the_offending_line(self) -> None:
        p = self.write("u.csv", "user_id\nu001\nbad id\n")
        with self.assertRaises(CsvError) as ctx:
            read_users(p)
        self.assertIn("3번째 줄", str(ctx.exception))

    def test_missing_file_and_empty_roster(self) -> None:
        with self.assertRaises(CsvError):
            read_users(self.dir / "nope.csv")
        with self.assertRaises(CsvError):
            read_users(self.write("e.csv", "other\nx\n"))


class ReadEndpointsTest(CsvTestCase):
    def test_full_columns(self) -> None:
        p = self.write(
            "e.csv",
            "ip,region,provider,listen_host,listen_port,note\n" "203.0.113.1,sg,acme,vpn1.example.com,51999,첫 번째\n",
        )
        ep = read_endpoints(p)[0]
        self.assertEqual(ep.ip, "203.0.113.1")
        self.assertEqual(ep.region, "sg")
        self.assertEqual(ep.listen_host, "vpn1.example.com")
        self.assertEqual(ep.listen_port, 51999)

    def test_ip_column_alias_and_default_port(self) -> None:
        ep = read_endpoints(self.write("e.csv", "egress_ip\n203.0.113.9\n"))[0]
        self.assertEqual(ep.listen_port, 51820)
        self.assertEqual(ep.listen_host, "203.0.113.9")

    def test_bad_port_and_bad_ip_name_the_line(self) -> None:
        with self.assertRaises(CsvError) as ctx:
            read_endpoints(self.write("e.csv", "ip,listen_port\n203.0.113.1,abc\n"))
        self.assertIn("2번째 줄", str(ctx.exception))
        with self.assertRaises(CsvError) as ctx:
            read_endpoints(self.write("e2.csv", "ip\n10.0.0.1\n"))
        self.assertIn("2번째 줄", str(ctx.exception))


class AssignmentRoundTripTest(CsvTestCase):
    def test_write_then_read_is_lossless(self) -> None:
        alloc = allocate([User(id=f"u{i:03d}") for i in range(1, 46)], endpoints_from_cidr("203.0.113.0/28"), group_size=20)
        path = self.dir / "out" / "assignments.csv"
        written = write_assignments(path, alloc.assignments)
        self.assertEqual(written, 45)

        header = path.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(header.split(","), ASSIGNMENT_COLUMNS)

        back = read_assignments(path)
        self.assertEqual([a.as_row() for a in alloc.assignments], [a.as_row() for a in back])

    def test_missing_required_column_is_rejected(self) -> None:
        p = self.write("a.csv", "user_id,group_id,egress_ip,tunnel_ip\nu001,g001,,10.77.0.2\n")
        with self.assertRaises(CsvError) as ctx:
            read_assignments(p)
        self.assertIn("egress_ip", str(ctx.exception))

    def test_empty_table_reads_as_empty_list(self) -> None:
        # 첫 배정 직전에는 기존 배정표가 헤더만 있을 수 있다.
        self.assertEqual(read_assignments(self.write("a.csv", ",".join(ASSIGNMENT_COLUMNS) + "\n")), [])


class WriteGroupsTest(CsvTestCase):
    def test_summary_has_a_row_per_group(self) -> None:
        alloc = allocate([User(id=f"u{i:03d}") for i in range(1, 46)], endpoints_from_cidr("203.0.113.0/28"), group_size=20)
        path = self.dir / "groups.csv"
        self.assertEqual(write_groups(path, alloc.used_groups, 20, TunnelPlan()), 3)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertIn("g001,203.0.113.1,10.77.0.0/24,10.77.0.1", lines[1])
        self.assertTrue(lines[3].endswith(",5,15,,"))  # 마지막 묶음은 5명 / 빈자리 15


class WriteUsersTest(CsvTestCase):
    def test_round_trip(self) -> None:
        path = self.dir / "u.csv"
        write_users(path, [User(id="u001", label="갑"), User(id="u002")])
        self.assertEqual([u.id for u in read_users(path)], ["u001", "u002"])


if __name__ == "__main__":
    unittest.main()
