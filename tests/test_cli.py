"""CLI 端到端单测：打桩 detect_backend，验证 argparse 路由与输出结构."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from pkgpurge import cli
from pkgpurge.backends.apt import AptBackend

DPQ = ("foo\t1.0-1\t1024\tinstalled\tamd64\tFoo tool\n"
       "bar\t2.0\t2048\tinstalled\tamd64\tBar thing\n")

SIM_OUT = ("Inst keepme (1.0)\n"
           "Remv foo [1.0-1]\n"
           "Remv libdep (2.0)\n")


def fake_run_factory():
    def run(argv, timeout=120, input_text=None):
        argv = list(argv)
        if argv[:2] == ["dpkg-query", "-W"]:
            return 0, DPQ, ""
        if argv[:2] == ["apt-mark", "showauto"]:
            return 0, "", ""
        if argv[:2] == ["dpkg", "--print-architecture"]:
            return 0, "amd64\n", ""
        if argv[:2] == ["apt-get", "-s"]:
            return 0, SIM_OUT, ""
        if argv[:2] == ["apt-cache", "show"]:
            return 0, f"Package: {argv[-1]}\nDescription: fake\n", ""
        if argv[:3] == ["apt-cache", "rdepends", "--installed"]:
            return 0, f"Reverse depends:\n  {argv[-1]}\n", ""
        return 127, "", f"not registered: {argv}"
    return run


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.backend = AptBackend(run=fake_run_factory())
        patcher = patch.object(cli, "detect_backend", return_value=self.backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_list_json(self):
        rc, out, _ = self.run_cli(["list", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual([d["name"] for d in data], ["bar", "foo"])
        self.assertEqual(data[1]["size_bytes"], 1024 * 1024)

    def test_list_limit_and_sort_size(self):
        rc, out, _ = self.run_cli(["list", "--json", "--limit", "1", "--sort", "size"])
        data = json.loads(out)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["name"], "bar")  # 2048K > 1024K

    def test_search_alias(self):
        rc, out, _ = self.run_cli(["search", "foo", "--json"])
        data = json.loads(out)
        self.assertEqual([d["name"] for d in data], ["foo"])

    def test_info(self):
        rc, out, _ = self.run_cli(["info", "foo"])
        self.assertEqual(rc, 0)
        self.assertIn("Package: foo", out)

    def test_remove_simulate(self):
        rc, out, _ = self.run_cli(["remove", "foo", "--simulate"])
        self.assertEqual(rc, 0)
        self.assertIn("will remove 2 package(s)", out)
        self.assertIn("libdep (dependency)", out)

    def test_remove_invalid_name_rejected(self):
        rc, out, err = self.run_cli(["remove", "foo;rm", "--simulate"])
        self.assertEqual(rc, 2)
        self.assertIn("illegal package name", err)

    def test_remove_simulate_warns_on_parse_failure(self):
        # B2：parsed_ok=False 时 CLI 必须显著警示，回退名单不得冒充级联结果
        class StubBackend(AptBackend):
            def simulate_removal(self, names, purge=False):
                return list(names), False

        with patch.object(cli, "detect_backend",
                          return_value=StubBackend(run=fake_run_factory())):
            rc, out, _ = self.run_cli(["remove", "foo", "--simulate"])
        self.assertEqual(rc, 0)
        self.assertIn("WARNING", out)
        self.assertIn("may include more", out)

    def test_remove_dry_run_report_distinguishable(self):
        # N16：dry-run 成功文案必须与真实卸载可区分
        from pkgpurge.backends.base import OpResult
        with patch.object(cli, "elevated_run",
                          return_value=OpResult(ok=True, message="all operations succeeded")):
            rc, out, _ = self.run_cli(["remove", "foo", "--yes", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN OK (nothing was removed)", out)
        self.assertNotIn("OK: all operations succeeded", out)

    def test_doctor(self):
        rc, out, _ = self.run_cli(["doctor"])
        self.assertEqual(rc, 0)
        info = json.loads(out)
        self.assertEqual(info["backend"]["id"], "apt")
        self.assertIn("distro", info)
        self.assertIn("pkexec", info)

    def test_version(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli(["--version"])
        self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
