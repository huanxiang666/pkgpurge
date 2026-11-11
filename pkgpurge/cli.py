"""命令行入口：list / search / info / remove / autoremove / doctor.

既是无 GUI 场景的可用工具，也是 headless 验证入口（--json 输出稳定结构）。
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .backends import detect_backend, distro_pretty
from .backends.base import OpResult, human_size
from .priv import elevated_run, find_helper, pkexec_available


def _get_backend_or_die():
    backend = detect_backend()
    if backend is None:
        print("error: no supported package manager found "
              "(need one of apt-get/dnf/zypper/pacman/apk)", file=sys.stderr)
        sys.exit(2)
    return backend


def _pkg_dict(p) -> dict:
    return {"name": p.name, "version": p.version, "size_bytes": p.size_bytes,
            "summary": p.summary, "auto_installed": p.auto_installed,
            "arch": p.arch}


def cmd_list(args) -> int:
    backend = _get_backend_or_die()
    pkgs = backend.list_installed()
    if args.auto_only:
        pkgs = [p for p in pkgs if p.auto_installed]
    if args.search:
        term = args.search.lower()
        pkgs = [p for p in pkgs
                if term in p.name.lower() or term in p.summary.lower()]
    if args.sort == "size":
        pkgs.sort(key=lambda p: p.size_bytes, reverse=True)
    if args.limit > 0:
        pkgs = pkgs[:args.limit]
    if args.json:
        print(json.dumps([_pkg_dict(p) for p in pkgs], ensure_ascii=False, indent=1))
    else:
        for p in pkgs:
            auto = " [auto]" if p.auto_installed else ""
            print(f"{p.name:<44} {human_size(p.size_bytes):>9}  {p.version}{auto}")
            if p.summary and not args.quiet:
                print(f"    {p.summary[:110]}")
    return 0


def cmd_info(args) -> int:
    backend = _get_backend_or_die()
    print(backend.info(args.name))
    return 0


def _confirm(text: str, assume_yes: bool) -> bool:
    if assume_yes or not sys.stdin.isatty():
        return assume_yes
    try:
        ans = input(text).strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def cmd_remove(args) -> int:
    backend = _get_backend_or_die()
    purge = args.purge and backend.supports_purge
    if args.purge and not backend.supports_purge:
        print(f"note: backend {backend.id} has no purge; falling back to remove",
              file=sys.stderr)

    targets = list(args.names)

    if backend.supports_simulate:
        will_remove, parsed_ok = backend.simulate_removal(targets, purge=purge)
        if not parsed_ok:
            print("WARNING: dependency-cascade parsing failed; the list below is only"
                  " your requested packages — the actual removal may include more.")
        if not will_remove:
            print("nothing to remove (package not installed, or only config residue left;"
                  " use --purge to clean rc-state residue on apt)")
            return 0
        extra = [n for n in will_remove if n not in targets]
        print(f"will remove {len(will_remove)} package(s):")
        for n in will_remove[:80]:
            mark = " (dependency)" if n in extra else ""
            print(f"  - {n}{mark}")
        if len(will_remove) > 80:
            print(f"  ... and {len(will_remove) - 80} more")
    else:
        will_remove = targets
        print(f"backend {backend.id} cannot simulate; requested: {', '.join(targets)}")

    if args.simulate:
        return 0

    if not _confirm("proceed? [y/N] ", args.yes):
        print("aborted by user")
        return 1

    ops = backend.removal_ops(targets, purge=purge)
    result = elevated_run(ops, dry_run=args.dry_run,
                          line_cb=lambda l: print(l))
    return _report(result, dry_run=args.dry_run)


def cmd_autoremove(args) -> int:
    backend = _get_backend_or_die()
    if not backend.supports_autoremove:
        print(f"backend {backend.id} does not support autoremove", file=sys.stderr)
        return 2
    preview = backend.autoremove_preview()
    if preview:
        print(f"will clean {len(preview)} orphan package(s): {', '.join(preview[:50])}")
    else:
        print("no orphans previewed (backend gave no list); the tool will still run its native autoremove")
    if args.simulate:
        return 0
    if not _confirm("proceed? [y/N] ", args.yes):
        print("aborted by user")
        return 1
    result = elevated_run(backend.autoremove_ops(), dry_run=args.dry_run,
                          line_cb=lambda l: print(l))
    return _report(result, dry_run=args.dry_run)


def _report(result: OpResult, dry_run: bool = False) -> int:
    if result.ok:
        # N16：dry-run 与真实卸载必须在文案上可区分，避免脚本误判
        print("DRY-RUN OK (nothing was removed)" if dry_run
              else "OK: " + result.message)
        return 0
    if result.cancelled:
        print("CANCELLED: " + result.message)
        return 1
    print("FAILED: " + result.message, file=sys.stderr)
    if isinstance(result.data, str) and result.data:
        print(result.data[-2000:], file=sys.stderr)
    return 1


def cmd_doctor(args) -> int:
    backend = detect_backend()
    info = {
        "pkgpurge_version": __version__,
        "distro": distro_pretty(),
        "backend": {"id": backend.id, "label": backend.label,
                    "purge": backend.supports_purge,
                    "autoremove": backend.supports_autoremove,
                    "simulate": backend.supports_simulate} if backend else None,
        "pkexec": pkexec_available(),
        "helper_path": str(find_helper() or ""),
    }
    print(json.dumps(info, ensure_ascii=False, indent=1))
    return 0 if backend else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pkgpurge",
        description="PkgPurge — cross-distro Linux package uninstaller (CLI)")
    ap.add_argument("--version", action="version", version=f"pkgpurge {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="list installed packages")
    p.add_argument("--search", default="", help="filter by name/summary substring")
    p.add_argument("--auto-only", action="store_true", help="only dependency-installed packages")
    p.add_argument("--sort", choices=("name", "size"), default="name")
    p.add_argument("--limit", type=int, default=0, help="max rows (0 = all)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="hide summary lines")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("search", help="search installed packages (alias of list --search)")
    p.add_argument("term")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: cmd_list(
        argparse.Namespace(search=a.term, auto_only=False, sort="name",
                           limit=0, json=a.json, quiet=False)))

    p = sub.add_parser("info", help="show package details")
    p.add_argument("name")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("remove", help="uninstall packages (via pkexec)")
    p.add_argument("names", nargs="+")
    p.add_argument("--purge", action="store_true", help="also remove config files (apt/pacman)")
    p.add_argument("--simulate", action="store_true", help="preview only, do not remove")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--dry-run", action="store_true",
                   help="authenticate but only echo the root commands")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("autoremove", help="remove orphaned dependencies (via pkexec)")
    p.add_argument("--simulate", action="store_true")
    p.add_argument("--yes", "-y", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_autoremove)

    p = sub.add_parser("doctor", help="print environment/backend diagnostics as JSON")
    p.set_defaults(func=cmd_doctor)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ValueError as e:
        # 包名校验失败等输入错误：给明确报错而不是 traceback
        print(f"error: {e}", file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
