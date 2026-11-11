#!/usr/bin/python3
"""pkexec 提权助手 — 以 root 执行「已白名单化」的包管理命令.

威胁模型：本机任何进程都能直接 `pkexec 本脚本 '<任意JSON>'`，polkit 只会
弹密码框，不会展示参数内容。因此本脚本自己必须 fail-closed：
  1. 命令首词只允许 {apt-get, dnf, zypper, pacman, apk, /bin/bash}；
  2. bash 只允许与 ORPHAN_CLEAN_SCRIPT 完全一致的字符串（防任意 root shell）；
  3. 其余参数要么命中该命令的动词/旗标白名单，要么匹配包名正则；
  4. 任何校验失败整体拒绝，不执行任何一条命令。

协议：stdin 或 argv[1] 传入 JSON
  {"version": "1.0.0", "dry_run": false, "ops": [["apt-get","-y","remove","--","foo"], ...]}
输出：逐行透传各命令 stdout/stderr（带 [op N] 前缀），最后一行 JSON 摘要：
  {"ok": true, "results": [{"op": [...], "rc": 0}]}
退出码：0 全部成功 / 1 有命令失败 / 2 校验失败或环境问题
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

# 与 backends.base 相同的包名正则——helper 以 root 独立运行，故意不 import
# 应用包（安装路径可能不在 root 的 sys.path），重复定义换取自包含与稳定。
# 同源一致性由 tests/test_helper.py::test_name_regex_same_source 断言。
# "@" 是 Arch makepkg 合法 pkgname 字符（如 python@2 风格命名）。
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:@\-]*$")
MAX_NAME_LEN = 200
MAX_OPS = 32
MAX_ARGS_PER_OP = 512

# B1 加固：helper 以 root 运行，绝不继承/信任调用方 PATH。
# 解析只用固定 PATH，且 realpath 必须落在系统目录（挡 /tmp/evil/apt-get 与
# 符号链接指回用户目录两类污染）。/usr/local 故意排除：它常对用户可写。
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
ALLOWED_BIN_DIRS = {"/usr/bin", "/bin", "/usr/sbin", "/sbin"}

# 与 backends/pacman.py 的 ORPHAN_CLEAN_SCRIPT 保持一致（打包测试会断言同源）
ORPHAN_CLEAN_SCRIPT = (
    'orphans=$(pacman -Qdtq); '
    'if [ -n "$orphans" ]; then pacman --noconfirm -Rs $orphans; '
    'else echo "no orphans found"; fi'
)

ALLOWED = {
    "apt-get": {"verbs": {"remove", "purge", "autoremove"},
                "flags": {"-y", "--purge", "-s", "--"}},
    "dnf":     {"verbs": {"remove", "autoremove"},
                "flags": {"-y", "--"}},
    "zypper":  {"verbs": {"remove", "rm"},
                "flags": {"--non-interactive", "-u"}},
    "pacman":  {"verbs": set(),
                "flags": {"-Rs", "-Rsn", "-R", "--noconfirm", "--"}},
    "apk":     {"verbs": {"del"},
                "flags": set()},
}
BASH_SCRIPT_WHITELIST = {ORPHAN_CLEAN_SCRIPT}


def validate_ops(ops) -> str | None:
    """返回 None 表示通过；返回字符串即拒绝原因.

    规则（对每条命令）：
    - 首词必须是白名单二进制（或白名单 bash 脚本）；
    - 以 "-" 开头的参数必须在该二进制的旗标白名单内（防 -o 类注入）；
    - 第一个「位置参数」必须是该二进制的动词白名单成员（verbs 非空时）；
      verbs 为空（pacman）时位置参数一律按包名校验；
    - 其余位置参数必须匹配包名正则。
    """
    if not isinstance(ops, list) or not (1 <= len(ops) <= MAX_OPS):
        return f"ops must be a list of 1..{MAX_OPS} commands"
    for i, op in enumerate(ops):
        if not isinstance(op, list) or not (1 <= len(op) <= MAX_ARGS_PER_OP):
            return f"op[{i}]: must be a list of 1..{MAX_ARGS_PER_OP} strings"
        if not all(isinstance(a, str) for a in op):
            return f"op[{i}]: non-string argument"
        head = op[0]
        if head == "/bin/bash":
            if len(op) != 3 or op[1] != "-c" or op[2] not in BASH_SCRIPT_WHITELIST:
                return f"op[{i}]: bash is only allowed for the exact orphan-clean script"
            continue
        if "/" in head:
            # 带路径的 head 一律拒绝：不让 "/tmp/evil/apt-get" 借 basename 洗白
            return f"op[{i}]: paths are not allowed as command: {head!r}"
        if head not in ALLOWED:
            return f"op[{i}]: binary not allowed: {head!r}"
        spec = ALLOWED[head]
        seen_positional = False
        for arg in op[1:]:
            if arg.startswith("-"):
                if arg not in spec["flags"]:
                    return f"op[{i}]: flag not allowed: {arg!r}"
                continue
            if not seen_positional:
                seen_positional = True
                if spec["verbs"]:
                    if arg not in spec["verbs"]:
                        return f"op[{i}]: verb not allowed: {arg!r}"
                    continue
            if not NAME_RE.match(arg) or len(arg) > MAX_NAME_LEN:
                return f"op[{i}]: illegal argument {arg!r}"
    return None


def resolve_binary(name: str) -> str | None:
    """固定 PATH 解析 + realpath 必须落在系统二进制目录，否则拒绝."""
    path = shutil.which(name, path=SAFE_PATH)
    if not path:
        return None
    real = os.path.realpath(path)
    if os.path.dirname(real) not in ALLOWED_BIN_DIRS:
        return None
    return real


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:]]
    dry_run = "--dry-run" in args
    args = [a for a in args if a != "--dry-run"]

    if args and args[0] == "--stdin":
        raw = sys.stdin.read()
    elif args:
        raw = args[0]
    else:
        print("usage: pkexec_helper.py [--dry-run] (--stdin | '<json>')", file=sys.stderr)
        return 2

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"bad json: {e}"}))
        return 2

    if not isinstance(payload, dict):
        print(json.dumps({"ok": False, "error": "payload must be a JSON object"}))
        return 2

    ops = payload.get("ops")
    reason = validate_ops(ops)
    if reason:
        print(json.dumps({"ok": False, "error": f"rejected: {reason}"}))
        return 2
    if payload.get("dry_run", False):
        dry_run = True

    if not dry_run and os.geteuid() != 0:
        print(json.dumps({"ok": False, "error": "must run as root (use pkexec)"}))
        return 2

    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env["PATH"] = SAFE_PATH  # B1 加固：root 子进程也不继承调用方 PATH

    results = []
    all_ok = True
    for i, op in enumerate(ops):
        head = op[0]
        if head == "/bin/bash":  # validate 已确保 op[1]=="-c" 且 op[2] 在白名单内
            cmd = ["/bin/bash", "-c", op[2]]
        else:
            path = resolve_binary(head)
            if not path:
                print(f"[op {i}] binary not found: {head}", file=sys.stderr)
                results.append({"op": op, "rc": 127})
                all_ok = False
                continue
            cmd = [path] + op[1:]
        if dry_run:
            print(f"[op {i}] DRY-RUN would execute: {json.dumps(cmd, ensure_ascii=False)}")
            results.append({"op": op, "rc": 0, "dry_run": True})
            continue
        print(f"[op {i}] executing: {' '.join(cmd)}", flush=True)
        p = subprocess.run(cmd, env=env)  # 不捕获输出：直接透传给用户看
        results.append({"op": op, "rc": p.returncode})
        if p.returncode != 0:
            all_ok = False
            print(f"[op {i}] FAILED rc={p.returncode}", file=sys.stderr)

    print(json.dumps({"ok": all_ok, "results": results}, ensure_ascii=False))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
