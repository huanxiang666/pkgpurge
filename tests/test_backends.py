"""后端解析器单测：全部用假 runner / 临时数据库目录，不碰真实系统."""

from __future__ import annotations

import os
import tempfile
import unittest

from pkgpurge.backends import detect_backend, distro_pretty
from pkgpurge.backends.apk import ApkBackend
from pkgpurge.backends.apt import AptBackend
from pkgpurge.backends.base import NAME_RE, human_size, validate_name
from pkgpurge.backends.dnf import DnfBackend
from pkgpurge.backends.pacman import ORPHAN_CLEAN_SCRIPT, PacmanBackend
from pkgpurge.backends.zypper import ZypperBackend


class FakeRun:
    """按 argv 前缀匹配返回 (rc, out, err)；未注册命令返回 127."""

    def __init__(self, table: dict[tuple[str, ...], tuple[int, str, str]]):
        self.table = table
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout=120, input_text=None):
        argv = list(argv)
        self.calls.append(argv)
        for prefix, resp in self.table.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return resp
        return 127, "", f"not registered: {argv}"


class TestAptBackend(unittest.TestCase):
    def setUp(self):
        dpq = ("foo\t1.0-1\t1024\tinstalled\tamd64\tFoo tool\n"
               "bar\t2.0\t0\tinstalled\tall\tBar thing\n"
               "baz\t1.0\t10\tconfig-files\tamd64\tgone already\n"
               "multi\t1.0\t5\tinstalled\ti386\tMulti arch lib\n"
               "broken-line-without-tabs\n")
        self.run = FakeRun({
            ("dpkg-query", "-W"): (0, dpq, ""),
            ("apt-mark", "showauto"): (0, "foo\nlibx\n", ""),
            ("dpkg", "--print-architecture"): (0, "amd64\n", ""),
        })
        self.b = AptBackend(run=self.run)

    def test_list_parsing(self):
        pkgs = self.b.list_installed()
        by = {p.name: p for p in pkgs}
        # config-files 状态与畸形行被过滤；只剩 3 个 installed
        self.assertEqual(sorted(by), ["bar", "foo", "multi"])
        self.assertEqual(by["foo"].size_bytes, 1024 * 1024)   # KiB → bytes
        self.assertTrue(by["foo"].auto_installed)
        self.assertFalse(by["bar"].auto_installed)
        self.assertEqual(by["foo"].summary, "Foo tool")
        self.assertEqual(by["bar"].arch, "all")
        # 排序稳定：bar < foo < multi
        self.assertEqual([p.name for p in pkgs], ["bar", "foo", "multi"])

    def test_list_failure_raises(self):
        b = AptBackend(run=FakeRun({("dpkg-query", "-W"): (2, "", "boom")}))
        with self.assertRaises(RuntimeError):
            b.list_installed()

    def test_multiarch_target(self):
        pkgs = {p.name: p for p in self.b.list_installed()}
        self.assertEqual(self.b.removal_target(pkgs["multi"]), "multi:i386")
        self.assertEqual(self.b.removal_target(pkgs["foo"]), "foo")
        self.assertEqual(self.b.removal_target(pkgs["bar"]), "bar")

    def test_removal_ops(self):
        ops = self.b.removal_ops(["foo", "bar:amd64"])
        self.assertEqual(ops, [["apt-get", "-y", "remove", "--", "foo", "bar:amd64"]])
        ops = self.b.removal_ops(["foo"], purge=True)
        self.assertEqual(ops, [["apt-get", "-y", "--purge", "remove", "--", "foo"]])

    def test_removal_ops_rejects_injection(self):
        with self.assertRaises(ValueError):
            self.b.removal_ops(["foo; rm -rf /"])
        with self.assertRaises(ValueError):
            self.b.removal_ops(["$(reboot)"])
        with self.assertRaises(ValueError):
            self.b.removal_ops([])

    def test_simulate_parsing(self):
        run = FakeRun({
            **self.run.table,
            ("apt-get", "-s"): (0, (
                "Inst keepme (1.0 now)\n"
                "Remv foo [1.0-1] (reason)\n"
                "Purg libdep (2.0)\n"
                "Remv bar [2.0]\n"), ""),
        })
        b = AptBackend(run=run)
        got, parsed_ok = b.simulate_removal(["foo"])
        self.assertEqual(got, ["foo", "libdep", "bar"])
        self.assertTrue(parsed_ok)

    def test_simulate_empty_means_nothing_to_remove(self):
        # rc=0 但无 Remv 行（包未安装/仅 rc 残留）：必须返回空而不是回退输入
        run = FakeRun({
            **self.run.table,
            ("apt-get", "-s"): (0, "NOTE: This is only a simulation!\n"
                                   "Package 'foo' is not installed, so not removed\n", ""),
        })
        b = AptBackend(run=run)
        got, parsed_ok = b.simulate_removal(["foo"])
        self.assertEqual(got, [])
        self.assertTrue(parsed_ok)

    def test_autoremove_ops(self):
        self.assertEqual(self.b.autoremove_ops(),
                         [["apt-get", "-y", "autoremove", "--purge"]])


class TestDnfBackend(unittest.TestCase):
    def setUp(self):
        rpm_out = ("foo\t1.0-1.fc40\t1048576\tFoo tool\n"
                   "bar\t2.0-1.fc40\t2048\tBar 工具（UTF-8）\n")
        self.run = FakeRun({
            ("rpm", "-qa"): (0, rpm_out, ""),
            ("dnf", "repoquery", "--userinstalled"): (0, "foo-1.0-1.fc40.x86_64\n", ""),
        })
        self.b = DnfBackend(run=self.run)

    def test_list_parsing(self):
        pkgs = self.b.list_installed()
        by = {p.name: p for p in pkgs}
        self.assertEqual(by["foo"].size_bytes, 1048576)
        self.assertFalse(by["foo"].auto_installed)   # userinstalled → 手动
        self.assertTrue(by["bar"].auto_installed)    # 不在 userinstalled → auto
        self.assertIn("UTF-8", by["bar"].summary)

    def test_parse_removal_sections(self):
        out = """Dependencies resolved.
===============================================================================
 Package        Architecture   Version        Repository     Size
===============================================================================
Removing:
 foo            x86_64         1.0-1.fc40     @System        1 M
Removing unused dependencies:
 bar            noarch         2.0-1          @System        2 M

Transaction Summary
===============================================================================
Remove  2 Packages

Operation aborted.
"""
        got = DnfBackend._parse_dnf_removal(out)
        self.assertEqual(got, ["foo", "bar"])

    def test_parse_removal_skips_separators(self):
        out = ("Removing:\n"
               "===========\n"
               " foo   x86_64  1.0  @System  1 M\n")
        self.assertEqual(DnfBackend._parse_dnf_removal(out), ["foo"])

    def test_simulate_fallback_flags_parsed_ok_false(self):
        # B2：解析不出任何行时回退请求名单，但必须带 parsed_ok=False
        run = FakeRun({
            ("dnf", "-y", "--assumeno"): (1, "weird output without tables\n", ""),
        })
        b = DnfBackend(run=run)
        got, parsed_ok = b.simulate_removal(["foo"])
        self.assertEqual(got, ["foo"])
        self.assertFalse(parsed_ok)

    def test_removal_ops(self):
        self.assertEqual(self.b.removal_ops(["foo"]),
                         [["dnf", "-y", "remove", "foo"]])
        self.assertEqual(self.b.autoremove_ops(), [["dnf", "-y", "autoremove"]])
        self.assertFalse(self.b.supports_purge)


class TestZypperBackend(unittest.TestCase):
    def test_parse_dry_run_table(self):
        out = ("Loading repository data...\n"
               "S | Name  | Version | Arch   | Repository\n"
               "--+-------+---------+--------+-----------\n"
               "r | foo   | 1.0-1   | x86_64 | main\n"
               "r | bar   | 2.0-1   | noarch | main\n"
               "  | keep  | 1.0-1   | x86_64 | main\n")
        got = ZypperBackend._parse_zypper_removal(out)
        self.assertEqual(got, ["foo", "bar"])

    def test_parse_junk_returns_empty(self):
        # B2：解析器本身不再内置回退；回退语义由 simulate_removal 统一负责
        self.assertEqual(ZypperBackend._parse_zypper_removal("junk"), [])

    def test_simulate_fallback_flags_parsed_ok_false(self):
        run = FakeRun({
            ("zypper", "--non-interactive", "rm"): (0, "unparsable garbage\n", ""),
        })
        b = ZypperBackend(run=run)
        got, parsed_ok = b.simulate_removal(["foo"])
        self.assertEqual(got, ["foo"])
        self.assertFalse(parsed_ok)

    def test_removal_ops(self):
        b = ZypperBackend(run=FakeRun({}))
        self.assertEqual(b.removal_ops(["foo"]),
                         [["zypper", "--non-interactive", "remove", "foo"]])
        self.assertEqual(b.autoremove_ops(),
                         [["zypper", "--non-interactive", "remove", "-u"]])


class TestPacmanBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pkgpurge-test-pacman-")
        db = self.tmp.name
        for entry, name, ver, desc, isize in [
            ("foo-1.0-1", "foo", "1.0-1", "Foo tool", "2048"),
            ("bar-2.0-1", "bar", "2.0-1", "Bar dep", "4096"),
            ("libfoo-bar-3.0-2", "libfoo-bar", "3.0-2", "Dashed name", "100"),
        ]:
            d = os.path.join(db, entry)
            os.makedirs(d)
            with open(os.path.join(d, "desc"), "w", encoding="utf-8") as f:
                f.write(f"%NAME%\n{name}\n\n%VERSION%\n{ver}\n\n%DESC%\n{desc}\n\n%ISIZE%\n{isize}\n\n")
        self.run = FakeRun({("pacman", "-Qe"): (0, "foo 1.0-1\n", "")})
        self.b = PacmanBackend(run=self.run, db_path=db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_from_desc_files(self):
        pkgs = self.b.list_installed()
        by = {p.name: p for p in pkgs}
        self.assertEqual(sorted(by), ["bar", "foo", "libfoo-bar"])
        self.assertEqual(by["foo"].size_bytes, 2048)
        self.assertEqual(by["foo"].summary, "Foo tool")
        self.assertFalse(by["foo"].auto_installed)   # 在 -Qe 显式列表
        self.assertTrue(by["bar"].auto_installed)    # 不在 → 依赖安装
        self.assertTrue(by["libfoo-bar"].auto_installed)

    def test_db_missing_raises(self):
        b = PacmanBackend(run=self.run, db_path="/nonexistent/path")
        with self.assertRaises(RuntimeError):
            b.list_installed()

    def test_strip_version(self):
        self.assertEqual(PacmanBackend._strip_version("foo-1.0-1"), "foo")
        self.assertEqual(PacmanBackend._strip_version("libfoo-bar-3.0-2"), "libfoo-bar")
        self.assertEqual(PacmanBackend._strip_version("noversion"), "noversion")

    def test_simulate_targets(self):
        run = FakeRun({
            **self.run.table,
            ("pacman", "-Rs", "--print"): (
                0, "Targets (2): foo-1.0-1  libfoo-bar-3.0-2\n\nTotal Removed Size: 0.01 MiB\n", ""),
        })
        b = PacmanBackend(run=run, db_path=self.b.db_path)
        got, parsed_ok = b.simulate_removal(["foo"])
        self.assertEqual(got, ["foo", "libfoo-bar"])
        self.assertTrue(parsed_ok)

    def test_simulate_fallback_flags_parsed_ok_false(self):
        run = FakeRun({
            **self.run.table,
            ("pacman", "-Rs", "--print"): (0, "no targets section here\n", ""),
        })
        b = PacmanBackend(run=run, db_path=self.b.db_path)
        got, parsed_ok = b.simulate_removal(["foo"])
        self.assertEqual(got, ["foo"])
        self.assertFalse(parsed_ok)

    def test_malformed_isize_does_not_crash(self):
        # N8：畸形 ISIZE 只影响该包大小，不拖垮整个列表
        d = os.path.join(self.b.db_path, "bad-1.0-1")
        os.makedirs(d)
        with open(os.path.join(d, "desc"), "w", encoding="utf-8") as f:
            f.write("%NAME%\nbad\n\n%VERSION%\n1.0-1\n\n%ISIZE%\nnot-a-number\n\n")
        by = {p.name: p for p in self.b.list_installed()}
        self.assertIn("bad", by)
        self.assertEqual(by["bad"].size_bytes, 0)

    def test_removal_ops(self):
        self.assertEqual(self.b.removal_ops(["foo"]),
                         [["pacman", "--noconfirm", "-Rs", "--", "foo"]])
        self.assertEqual(self.b.removal_ops(["foo"], purge=True),
                         [["pacman", "--noconfirm", "-Rsn", "--", "foo"]])
        self.assertEqual(self.b.autoremove_ops(),
                         [["/bin/bash", "-c", ORPHAN_CLEAN_SCRIPT]])


class TestApkBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            "w", suffix="-installed", delete=False, prefix="pkgpurge-test-apk-",
            encoding="utf-8")
        self.tmp.write(
            "P:musl\nV:1.2.5-r0\nI:409600\nT:the musl c library\n\n"
            "P:busybox\nV:1.36.1-r0\nI:1024000\nT:Size optimized toolbox\n\n")
        self.tmp.close()
        self.b = ApkBackend(run=FakeRun({}), db_file=self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_list_parsing(self):
        pkgs = self.b.list_installed()
        by = {p.name: p for p in pkgs}
        self.assertEqual(sorted(by), ["busybox", "musl"])
        self.assertEqual(by["musl"].size_bytes, 409600)
        self.assertEqual(by["busybox"].summary, "Size optimized toolbox")

    def test_db_missing_raises(self):
        b = ApkBackend(run=FakeRun({}), db_file="/nonexistent")
        with self.assertRaises(RuntimeError):
            b.list_installed()

    def test_removal_ops(self):
        self.assertEqual(self.b.removal_ops(["musl"]), [["apk", "del", "musl"]])
        with self.assertRaises(NotImplementedError):
            self.b.autoremove_ops()


class TestDetect(unittest.TestCase):
    def test_detect_by_binary_priority(self):
        b = detect_backend(which=lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
        self.assertIsInstance(b, AptBackend)
        b = detect_backend(which=lambda name: "/usr/bin/dnf" if name == "dnf" else None)
        self.assertIsInstance(b, DnfBackend)
        b = detect_backend(which=lambda name: None)
        self.assertIsNone(b)

    def test_apt_wins_over_dnf(self):
        b = detect_backend(which=lambda name: f"/usr/bin/{name}")
        self.assertIsInstance(b, AptBackend)

    def test_distro_pretty(self):
        with tempfile.NamedTemporaryFile("w", suffix="-os-release", delete=False,
                                         encoding="utf-8") as f:
            f.write('NAME="Ubuntu"\nVERSION_ID="26.04"\nPRETTY_NAME="Ubuntu 26.04.1 LTS"\n')
            path = f.name
        try:
            self.assertEqual(distro_pretty(path), "Ubuntu 26.04.1 LTS")
        finally:
            os.unlink(path)
        self.assertEqual(distro_pretty("/nonexistent-os-release"), "Linux")


class TestBaseUtils(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), "—")
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(2048), "2.0 KB")
        self.assertEqual(human_size(5 * 1024 * 1024), "5.0 MB")

    def test_name_regex(self):
        for good in ("foo", "libfoo-bar", "g++", "pkg.name", "foo:i386", "a_b-c+d",
                     "foo@bar"):  # Arch makepkg pkgname 合法字符集含 @
            self.assertTrue(NAME_RE.match(good), good)
        for bad in ("-foo", ";rm", "foo bar", "foo;bar", "$(x)", "", ".hidden"):
            self.assertFalse(NAME_RE.match(bad), bad)

    def test_validate_name_raises(self):
        validate_name("foo")
        with self.assertRaises(ValueError):
            validate_name("foo && rm -rf /")


if __name__ == "__main__":
    unittest.main()
