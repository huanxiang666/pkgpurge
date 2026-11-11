"""客户端提权通道：定位 helper、组装 payload、经 pkexec 执行、流式回传输出.

pkexec 退出码约定（man pkexec）：
  126 = 用户未授权/关闭认证框；127 = 认证失败或程序不可执行。
两者都映射为 OpResult(cancelled=True)，与「命令执行失败」区分开。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .backends.base import OpResult

INSTALLED_HELPER_CANDIDATES = (
    "/usr/lib/pkgpurge/pkexec_helper.py",        # deb 安装
    "/usr/local/lib/pkgpurge/pkexec_helper.py",  # install.sh 源码安装
)

# 退出码 → 语义（集中一处，GUI/CLI 共用）
RC_CANCELLED = {126, 127}


def find_helper() -> Path | None:
    """按优先级定位 helper：环境变量 > 系统安装路径 > 开发树."""
    env = os.environ.get("PKGPURGE_HELPER")
    if env and Path(env).is_file():
        return Path(env)
    for cand in INSTALLED_HELPER_CANDIDATES:
        p = Path(cand)
        if p.is_file():
            return p
    dev = Path(__file__).resolve().parent / "helper.py"
    if dev.is_file():
        return dev
    return None


def pkexec_available() -> bool:
    return shutil.which("pkexec") is not None


def elevated_run(ops: list[list[str]],
                 dry_run: bool = False,
                 line_cb: Callable[[str], None] | None = None,
                 timeout: int = 1800) -> OpResult:
    """经 pkexec 执行 ops，流式把 helper 输出交给 line_cb。

    返回统一三态 OpResult：ok / cancelled（用户关了认证框）/ 失败（带输出尾部）。
    """
    if not ops:
        return OpResult(ok=False, message="no operations given")
    helper = find_helper()
    if helper is None:
        return OpResult(ok=False, message="pkexec helper not found")
    if not pkexec_available():
        return OpResult(ok=False, message="pkexec not installed (need policykit-1/pkexec package)")

    payload = json.dumps({"version": __version__, "dry_run": dry_run, "ops": ops},
                         ensure_ascii=False)
    cmd = ["pkexec", str(helper), "--stdin"]

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        return OpResult(ok=False, message=f"failed to start pkexec: {e}")

    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # 认证被拒时 stdin 可能已关闭；退出码阶段统一判语义

    tail: list[str] = []
    try:
        for line in proc.stdout:
            if line_cb:
                line_cb(line.rstrip("\n"))
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return OpResult(ok=False, message=f"pkexec timed out after {timeout}s")

    if rc in RC_CANCELLED:
        return OpResult(ok=False, cancelled=True,
                        message="authentication dismissed or failed (pkexec rc=%d)" % rc)
    if rc == 0:
        return OpResult(ok=True, message="all operations succeeded", data=tail)
    return OpResult(ok=False, message=f"operations failed (rc={rc})",
                    data="".join(tail[-20:]))
