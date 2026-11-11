"""pkexec helper 白名单校验单测：这是提权面最关键的防线，逐类攻击向量覆盖."""

from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout

from pkgpurge import helper
from pkgpurge.backends import ApkBackend, AptBackend, DnfBackend, PacmanBackend, ZypperBackend
from pkgpurge.backends.pacman import ORPHAN_CLEAN_SCRIPT as PACMAN_SCRIPT


class TestValidateOpsAccept(unittest.TestCase):
    """各后端真实生成的命令必须全部通过校验（自洽性）."""

    def test_backend_generated_ops_all_pass(self):
        cases = [
            AptBackend(run=None).removal_ops(["foo", "bar:amd64"]),
            AptBackend(run=None).removal_ops(["foo"], purge=True),
            AptBackend(run=None).autoremove_ops(),
            DnfBackend(run=None).removal_ops(["foo"]),
            DnfBackend(run=None).autoremove_ops(),
            ZypperBackend(run=None).removal_ops(["foo"]),
            ZypperBackend(run=None).autoremove_ops(),
            PacmanBackend(run=None).removal_ops(["foo"], purge=True),
            PacmanBackend(run=None).autoremove_ops(),
            ApkBackend(run=None).removal_ops(["musl"]),
        ]
        for ops in cases:
            self.assertIsNone(helper.validate_ops(ops), f"rejected own ops: {ops}")

    def test_orphan_script_same_source(self):
        # pacman 后端与 helper 白名单必须是同一字符串，否则提权时会被拒
        self.assertEqual(PACMAN_SCRIPT, helper.ORPHAN_CLEAN_SCRIPT)
        self.assertIn(PACMAN_SCRIPT, helper.BASH_SCRIPT_WHITELIST)


class TestValidateOpsReject(unittest.TestCase):
    def assertRejected(self, ops):
        self.assertIsNotNone(helper.validate_ops(ops), f"should reject: {ops}")

    def test_unknown_binary(self):
        self.assertRejected([["rm", "-rf", "/"]])
        self.assertRejected([["/bin/sh", "-c", "id"]])
        self.assertRejected([["curl", "http://evil"]])

    def test_bash_arbitrary_script(self):
        self.assertRejected([["/bin/bash", "-c", "rm -rf /"]])
        self.assertRejected([["/bin/bash", "-c", "pacman --noconfirm -Rs foo"]])
        self.assertRejected([["/bin/bash", "-c", PACMAN_SCRIPT + "; id"]])

    def test_apt_install_verb_blocked(self):
        # "install" 能匹配包名正则，但动词白名单必须挡住 apt-get install
        self.assertRejected([["apt-get", "-y", "install", "nginx"]])
        self.assertRejected([["apt-get", "update"]])

    def test_flag_injection_blocked(self):
        self.assertRejected([["apt-get", "-y", "-oDPkg::Options::=--force-all", "remove", "foo"]])
        self.assertRejected([["apt-get", "--download-only", "remove", "foo"]])
        self.assertRejected([["dnf", "-y", "--downloadonly", "remove", "foo"]])
        self.assertRejected([["pacman", "--noconfirm", "-Syu"]])  # -Syu 不在旗标白名单

    def test_shell_metachar_names_blocked(self):
        self.assertRejected([["apt-get", "-y", "remove", "foo;reboot"]])
        self.assertRejected([["apt-get", "-y", "remove", "$(id)"]])
        self.assertRejected([["apt-get", "-y", "remove", "foo bar"]])
        self.assertRejected([["apk", "del", "`id`"]])

    def test_structural_rejects(self):
        self.assertRejected([])
        self.assertRejected("not a list")
        self.assertRejected([[]])
        self.assertRejected([["apt-get", 123]])
        self.assertRejected([["apt-get", "-y", "remove", None]])
        self.assertRejected([["apt-get", "-y", "remove", "x" * 300]])


class TestHelperMain(unittest.TestCase):
    def _call(self, payload: dict, extra_args: list[str] | None = None) -> tuple[int, dict]:
        argv = ["pkexec_helper.py"] + (extra_args or []) + [json.dumps(payload)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = helper.main(argv)
        out = buf.getvalue()
        last = [l for l in out.splitlines() if l.strip()][-1]
        return rc, json.loads(last)

    def test_dry_run_ok(self):
        rc, summary = self._call(
            {"version": "x", "dry_run": True,
             "ops": [["apt-get", "-y", "remove", "--", "foo"]]},
            extra_args=["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["results"][0]["dry_run"])

    def test_bad_json(self):
        argv = ["pkexec_helper.py", "{not json"]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = helper.main(argv)
        self.assertEqual(rc, 2)

    def test_rejected_payload_not_root(self):
        rc, summary = self._call(
            {"version": "x", "dry_run": False, "ops": [["rm", "-rf", "/"]]})
        self.assertEqual(rc, 2)
        self.assertFalse(summary["ok"])
        self.assertIn("rejected", summary["error"])

    @unittest.skipIf(os.geteuid() == 0, "test requires non-root")
    def test_valid_payload_but_not_root(self):
        rc, summary = self._call(
            {"version": "x", "dry_run": False,
             "ops": [["apt-get", "-y", "remove", "--", "foo"]]})
        self.assertEqual(rc, 2)
        self.assertIn("root", summary["error"])

    def test_usage_without_args(self):
        rc = helper.main(["pkexec_helper.py"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
