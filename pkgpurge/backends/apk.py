"""APK 后端（Alpine）：解析 /lib/apk/db/installed 数据库文件."""

from __future__ import annotations

import os

from .base import Package, PackageBackend, validate_name, validate_names


class ApkBackend(PackageBackend):
    id = "apk"
    label = "APK"
    supports_purge = False
    supports_autoremove = False     # apk 无官方 autoremove；不猜测替代命令
    supports_simulate = False

    def __init__(self, run=None, db_file: str = "/lib/apk/db/installed"):
        super().__init__(run)
        self.db_file = db_file

    def list_installed(self) -> list[Package]:
        if not os.path.isfile(self.db_file):
            raise RuntimeError(f"apk db not found: {self.db_file}")
        pkgs: list[Package] = []
        with open(self.db_file, encoding="utf-8", errors="replace") as f:
            cur: dict[str, str] = {}
            for raw in f.read().split("\n\n"):
                block = raw.strip()
                if not block:
                    continue
                fields: dict[str, str] = {}
                key = None
                for line in block.splitlines():
                    if len(line) > 2 and line[1] == ":":
                        key = line[0]
                        fields[key] = line[2:].strip()
                    elif key and line.startswith("  "):
                        fields[key] = (fields.get(key, "") + " " + line.strip()).strip()
                name = fields.get("P", "")
                if not name:
                    continue
                try:
                    size = int(fields.get("I", "0") or 0)
                except ValueError:
                    size = 0
                pkgs.append(Package(name=name, version=fields.get("V", ""),
                                    size_bytes=size, summary=fields.get("T", "")))
        pkgs.sort(key=lambda p: p.name.lower())
        return pkgs

    def info(self, name: str) -> str:
        validate_name(name)
        rc, out, err = self._run(["apk", "info", name])
        rc2, out2, _ = self._run(["apk", "info", "-R", name])  # 依赖
        text = out.strip() if rc == 0 else f"(apk info 失败, rc={rc}) {err.strip()[:200]}"
        if rc2 == 0 and out2.strip():
            text += "\n\nDepends:\n" + out2.strip()
        return text

    def removal_ops(self, names: list[str], purge: bool = False) -> list[list[str]]:
        names = validate_names(names)
        # apk del 自带依赖回收（删除不再被需要的依赖），无需单独 autoremove
        return [["apk", "del"] + names]
