"""DNF 后端（Fedora / RHEL / CentOS Stream / Rocky / AlmaLinux …）."""

from __future__ import annotations

from .base import NAME_RE, Package, validate_name, validate_names
from .rpm import RpmListMixin
from .base import PackageBackend


class DnfBackend(RpmListMixin, PackageBackend):
    id = "dnf"
    label = "DNF"
    supports_purge = False          # rpm 系 remove 即含配置清理选项有限，统一按 remove
    supports_autoremove = True
    supports_simulate = True

    def list_installed(self) -> list[Package]:
        pkgs = self._rpm_list()
        rc, out, _ = self._run(["dnf", "repoquery", "--userinstalled", "-q"])
        if rc == 0:
            user_pkgs = {l.rsplit("-", 2)[0] for l in out.split() if l.strip()}
            for p in pkgs:
                p.auto_installed = bool(user_pkgs) and p.name not in user_pkgs
        return pkgs

    def info(self, name: str) -> str:
        validate_name(name)
        rc, qi, _ = self._run(["rpm", "-qi", name])
        rc2, req, _ = self._run(["rpm", "-q", "--whatrequires", name])
        text = qi.strip() if rc == 0 else f"(rpm -qi 失败, rc={rc})"
        if rc2 == 0 and req.strip():
            text += "\n\nRequired by:\n" + req.strip()
        return text

    def simulate_removal(self, names: list[str], purge: bool = False) -> list[str]:
        names = validate_names(names)
        rc, out, err = self._run(["dnf", "-y", "--assumeno", "remove"] + names)
        if rc != 0 and not out:
            raise RuntimeError(f"dnf simulate failed ({rc}): {err.strip()[:300]}")
        return self._parse_dnf_removal(out) or list(names)

    @staticmethod
    def _parse_dnf_removal(out: str) -> list[str]:
        """解析 dnf 事务表中 'Removing:' / 'Removing unused dependencies:' 段。
        best-effort：格式随版本略有差异；表格分隔线（=…）与非包名行被正则排除."""
        removed: list[str] = []
        in_section = False
        for line in out.splitlines():
            s = line.strip()
            if not s:
                in_section = False
                continue
            if s.endswith(":"):
                in_section = "Removing" in s or "removing" in s
                continue
            if in_section:
                fields = s.split()
                if fields and NAME_RE.match(fields[0]):
                    removed.append(fields[0])
        return removed

    def removal_ops(self, names: list[str], purge: bool = False) -> list[list[str]]:
        names = validate_names(names)
        return [["dnf", "-y", "remove"] + names]

    def autoremove_ops(self) -> list[list[str]]:
        return [["dnf", "-y", "autoremove"]]

    def autoremove_preview(self) -> list[str]:
        rc, out, _ = self._run(["dnf", "-y", "--assumeno", "autoremove"])
        return self._parse_dnf_removal(out) if rc == 0 or out else []
