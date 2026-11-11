"""统一入口：无参数启动 GTK4 图形界面；带子命令走 CLI；--selftest 做无头自检."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        from .gui import run_selftest
        return run_selftest()
    if argv:
        from .cli import main as cli_main
        return cli_main(argv)
    from .gui import run_gui
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
