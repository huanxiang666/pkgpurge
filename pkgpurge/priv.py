"""客户端提权通道：定位 helper、组装 payload、经 pkexec 执行、流式回传输出.

pkexec 退出码约定（man pkexec）：
  126 = 用户未授权/关闭认证框；127 = 认证失败或程序不可执行。
两者都映射为 OpResult(cancelled=True)，与「命令执行失败」区分开。
helper 的 rc=2 = 白名单校验拒绝，单独映射（N2：不与执行失败混淆）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
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
RC_REJECTED = 2


def find_helper() -> Path | None:
    """按优先级定位 helper：环境变量 > 系统安装路径 > 开发树.

    LIMIT(N12)：开发树回退会让 pkexec 走通用 action（polkit exec.path 注解
    指向安装路径），仍要认证、非提权洞，但弹窗文案与专用 policy 不一致。
    """
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
    N1：timeout 用看门狗 Timer 实现——流式读 stdout 会阻塞，wait(timeout)
    轮不到；子进程超时或读流异常都会被 kill，不泄漏 root 进程。
    """
    if not ops:
        return OpResult(ok=False, message="no operations given")
    helper = find_helper()
    if helper is None:
        return OpResult(ok=False, message="pkexec helper not found")
    if not pkexec_available():
        return OpResult(ok=False,
                        message="pkexec not installed (need policykit-1/pkexec package)")

    payload = json.dumps({"version": __version__, "dry_run": dry_run, "ops": ops},
                         ensure_ascii=False)
    cmd = ["pkexec", str(helper), "--stdin"]

    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            errors="replace")  # 非 UTF-8 输出不炸读流循环
    except OSError as e:
        return OpResult(ok=False, message=f"failed to start pkexec: {e}")

    assert proc.stdin is not None and proc.stdout is not None
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass  # 认证被拒时 stdin 可能已关闭；退出码阶段统一判语义

    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    tail: list[str] = []
    try:
        for line in proc.stdout:
            if line_cb:
                line_cb(line.rstrip("\n"))
            tail.append(line)
            if len(tail) > 40:
                tail.pop(0)
        rc = proc.wait()
    except Exception as e:  # noqa: BLE001 — 读流异常也必须收尸，不漏 root 子进程
        proc.kill()
        proc.wait()
        return OpResult(ok=False, message=f"error reading helper output: {e}")
    finally:
        killer.cancel()

    if rc in RC_CANCELLED:
        return OpResult(ok=False, cancelled=True,
                        message="authentication dismissed or failed (pkexec rc=%d)" % rc)
    if rc == RC_REJECTED:
        return OpResult(ok=False,
                        message="helper rejected the command payload (whitelist validation)",
                        data="".join(tail[-20:]))
    if rc == 0:
        return OpResult(ok=True, message="all operations succeeded", data=tail)
    return OpResult(ok=False, message=f"operations failed (rc={rc})",
                    data="".join(tail[-20:]))
