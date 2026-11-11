"""APT 后端（Debian / Ubuntu / Mint / Deepin / Kali …）."""

from __future__ import annotations

from .base import Package, PackageBackend, validate_names


class AptBackend(PackageBackend):
    id = "apt"
    label = "APT"
    supports_purge = True
    supports_autoremove = True
    supports_simulate = True

    # dpkg-query 字段顺序：name version installed-size(KiB) status arch summary
    _FMT = ("${Package}\\t${Version}\\t${Installed-Size}\\t"
            "${db:Status-Status}\\t${Architecture}\\t${binary:Summary}\\n")

    def __init__(self, run=None):
        super().__init__(run)
        self._native_arch: str | None = None

    @property
    def native_arch(self) -> str:
        if self._native_arch is None:
            rc, out, _ = self._run(["dpkg", "--print-architecture"])
            self._native_arch = out.strip() if rc == 0 and out.strip() else "amd64"
        return self._native_arch

    def list_installed(self) -> list[Package]:
        rc, out, err = self._run(
            ["dpkg-query", "-W", "-f", self._FMT])
        if rc != 0:
            raise RuntimeError(f"dpkg-query failed ({rc}): {err.strip()[:300]}")
        auto = self._auto_set()
        pkgs: list[Package] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 5)
            if len(parts) < 5:
                continue  # LIMIT: 畸形行直接跳过；dpkg-query 输出稳定，出现即说明格式变了
            name, version, size_kib, status, arch = parts[:5]
            summary = (parts[5] if len(parts) > 5 else "").replace("\t", " ")
            if status != "installed":
                continue  # 跳过 config-files / half-installed 等状态
            try:
                size_bytes = int(size_kib) * 1024
            except ValueError:
                size_bytes = 0
            pkgs.append(Package(name=name, version=version, size_bytes=size_bytes,
                                summary=summary, auto_installed=name in auto,
                                arch=arch))
        pkgs.sort(key=lambda p: p.name.lower())
        return pkgs

    def _auto_set(self) -> set[str]:
        rc, out, _ = self._run(["apt-mark", "showauto"])
        return set(out.split()) if rc == 0 else set()

    def info(self, name: str) -> str:
        from .base import validate_name
        validate_name(name)
        rc, show, _ = self._run(["apt-cache", "show", "--no-all-versions", name])
        rc2, rdep, _ = self._run(["apt-cache", "rdepends", "--installed", name])
        text = show.strip() if rc == 0 and show.strip() else f"(apt-cache show 无输出, rc={rc})"
        if rc2 == 0 and rdep.strip():
            text += "\n\n" + rdep.strip()
        return text

    def simulate_removal(self, names: list[str], purge: bool = False) -> tuple[list[str], bool]:
        names = validate_names(names)
        cmd = ["apt-get", "-s", "-y"]
        cmd += ["--purge"] if purge else []
        cmd += ["remove", "--"] + names
        rc, out, err = self._run(cmd)
        if rc != 0:
            raise RuntimeError(f"apt-get -s failed ({rc}): {(err or out).strip()[:300]}")
        removed = []
        for line in out.splitlines():
            if line.startswith(("Remv ", "Purg ")):
                fields = line.split()
                if len(fields) >= 2:
                    removed.append(fields[1])
        # apt 的 Remv/Purg 行解析是确定性的：rc=0 且无移除行 = 无事可做
        # （例如包处于 rc 残留态或根本没装），不回退成输入名单，避免误导。
        return removed, True

    def removal_target(self, pkg) -> str:
        # multiarch：非本机架构的包必须用 name:arch 精确卸载，避免连带另一架构。
        if pkg.arch and pkg.arch not in ("all", self.native_arch):
            return f"{pkg.name}:{pkg.arch}"
        return pkg.name

    def removal_ops(self, names: list[str], purge: bool = False) -> list[list[str]]:
        names = validate_names(names)
        cmd = ["apt-get", "-y"]
        if purge:
            cmd.append("--purge")
        cmd.append("remove")
        cmd += ["--"] + names
        return [cmd]

    def autoremove_ops(self) -> list[list[str]]:
        return [["apt-get", "-y", "autoremove", "--purge"]]

    def autoremove_preview(self) -> list[str]:
        rc, out, _ = self._run(["apt-get", "-s", "-y", "autoremove", "--purge"])
        if rc != 0:
            return []
        return [l.split()[1] for l in out.splitlines()
                if l.startswith(("Remv ", "Purg ")) and len(l.split()) >= 2]
