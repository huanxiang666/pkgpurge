"""Pacman 后端（Arch / Manjaro / EndeavourOS / Garuda …）.

包列表直接解析 pacman 本地数据库（/var/lib/pacman/local/*/desc），
一次读盘拿到 name/version/desc/size，避免对每个包跑 pacman -Qi。
"""

from __future__ import annotations

import os
import re

from .base import Package, PackageBackend, validate_names

# 孤立依赖清理脚本：helper 侧以「完整字符串白名单」放行，禁止任何变体。
# pacman 没有 autoremove 子命令，官方惯用法即 -Qdtq + -Rs。
ORPHAN_CLEAN_SCRIPT = (
    'orphans=$(pacman -Qdtq); '
    'if [ -n "$orphans" ]; then pacman --noconfirm -Rs $orphans; '
    'else echo "no orphans found"; fi'
)

_TARGET_RE = re.compile(r"^(.+)-[^-]+-[^-]+$")


class PacmanBackend(PackageBackend):
    id = "pacman"
    label = "Pacman"
    supports_purge = True           # -Rsn：连带删除配置文件
    supports_autoremove = True
    supports_simulate = True

    def __init__(self, run=None, db_path: str = "/var/lib/pacman/local"):
        super().__init__(run)
        self.db_path = db_path

    def list_installed(self) -> list[Package]:
        explicit = self._explicit_set()
        pkgs: list[Package] = []
        try:
            entries = sorted(os.listdir(self.db_path))
        except OSError as e:
            raise RuntimeError(f"cannot read pacman db {self.db_path}: {e}") from e
        for entry in entries:
            desc = os.path.join(self.db_path, entry, "desc")
            if not os.path.isfile(desc):
                continue
            fields = self._parse_desc(desc)
            name = fields.get("NAME", "")
            if not name:
                continue
            try:
                isize = int(fields.get("ISIZE", "0") or 0)
            except ValueError:
                isize = 0  # N8：畸形 ISIZE 不拖垮整个列表，与 apt/rpm/apk 口径一致
            pkgs.append(Package(
                name=name,
                version=fields.get("VERSION", ""),
                size_bytes=isize,
                summary=(fields.get("DESC", "") or "").replace("\n", " "),
                auto_installed=bool(explicit) and name not in explicit,
            ))
        pkgs.sort(key=lambda p: p.name.lower())
        return pkgs

    @staticmethod
    def _parse_desc(path: str) -> dict[str, str]:
        """解析 pacman desc 文件：%KEY% 后跟值行，空行分隔."""
        out: dict[str, str] = {}
        key = None
        buf: list[str] = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if line.startswith("%") and line.endswith("%") and len(line) > 2:
                    if key:
                        out[key] = "\n".join(buf).strip()
                    key = line[1:-1]
                    buf = []
                elif line == "":
                    if key:
                        out[key] = "\n".join(buf).strip()
                        key = None
                        buf = []
                elif key:
                    buf.append(line)
        if key:
            out[key] = "\n".join(buf).strip()
        return out

    def _explicit_set(self) -> set[str]:
        rc, out, _ = self._run(["pacman", "-Qe"])
        return {l.split()[0] for l in out.splitlines() if l.strip()} if rc == 0 else set()

    def info(self, name: str) -> str:
        from .base import validate_name
        validate_name(name)
        rc, out, err = self._run(["pacman", "-Qi", name])
        return out.strip() if rc == 0 else f"(pacman -Qi 失败, rc={rc}) {err.strip()[:200]}"

    def simulate_removal(self, names: list[str], purge: bool = False) -> tuple[list[str], bool]:
        names = validate_names(names)
        flags = "-Rsn" if purge else "-Rs"
        rc, out, err = self._run(
            ["pacman", flags, "--print", "--noconfirm"] + names)
        if rc != 0:
            raise RuntimeError(f"pacman --print failed ({rc}): {err.strip()[:300]}")
        targets: list[str] = []
        grab = False
        for line in out.splitlines():
            if line.startswith("Targets"):
                grab = True
                # "Targets (2): foo-1.0-1  bar-2.3-4" 同行形式
                if ":" in line:
                    targets += line.split(":", 1)[1].split()
                continue
            if grab:
                if not line.strip():
                    grab = False
                    continue
                targets += line.split()
        if not targets:
            # B2：解析失败回退请求名单，parsed_ok=False 由调用方明示
            return list(names), False
        return [self._strip_version(t) for t in targets], True

    @staticmethod
    def _strip_version(target: str) -> str:
        m = _TARGET_RE.match(target)
        return m.group(1) if m else target

    def removal_ops(self, names: list[str], purge: bool = False) -> list[list[str]]:
        names = validate_names(names)
        flags = "-Rsn" if purge else "-Rs"
        return [["pacman", "--noconfirm", flags, "--"] + names]

    def autoremove_ops(self) -> list[list[str]]:
        return [["/bin/bash", "-c", ORPHAN_CLEAN_SCRIPT]]

    def autoremove_preview(self) -> list[str]:
        rc, out, _ = self._run(["pacman", "-Qdtq"])
        return out.split() if rc == 0 else []
