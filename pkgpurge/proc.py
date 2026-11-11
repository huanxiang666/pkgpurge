"""子进程执行器：后端命令统一走这里，便于测试注入与超时控制."""

from __future__ import annotations

import os
import subprocess
from typing import Callable, Sequence

Runner = Callable[..., tuple[int, str, str]]


def run_cmd(argv: Sequence[str], timeout: int = 120,
            input_text: str | None = None) -> tuple[int, str, str]:
    """执行命令，返回 (returncode, stdout, stderr)。

    - LC_ALL=C 固定输出为英文，保证解析稳定（各后端都依赖这一点）。
    - 命令不存在 / 超时都转成退出码而不是抛异常，调用方按 rc 分支处理。
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True,
                           timeout=timeout, env=env, input=input_text)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(argv)}"
