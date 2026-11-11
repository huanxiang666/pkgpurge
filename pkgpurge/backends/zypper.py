"""Zypper 后端（openSUSE / SLES）."""

from __future__ import annotations

from .base import NAME_RE, Package, PackageBackend, validate_name, validate_names
from .rpm import RpmListMixin


class ZypperBackend(RpmListMixin, PackageBackend):
    id = "zypper"
    label = "Zypper"
    supports_purge = False
    supports_autoremove = True      # zypper rm -u（不再被需要的包）
    supports_simulate = True

    def list_installed(self) -> list[Package]:
        # zypper 无 apt-mark 等价物；全部按手动安装展示。LIMIT: 无 auto 标记
        return self._rpm_list()

    def info(self, name: str) -> str:
        validate_name(name)
        rc, zi, _ = self._run(["zypper", "--non-interactive", "info", name])
        rc2, req, _ = self._run(["rpm", "-q", "--whatrequires", name])
        text = zi.strip() if rc == 0 else f"(zypper info 失败, rc={rc})"
        if rc2 == 0 and req.strip():
            text += "\n\nRequired by:\n" + req.strip()
        return text

    def simulate_removal(self, names: list[str], purge: bool = False) -> tuple[list[str], bool]:
        names = validate_names(names)
        rc, out, err = self._run(
            ["zypper", "--non-interactive", "rm", "--dry-run"] + names)
        if rc != 0 and not out:
            raise RuntimeError(f"zypper dry-run failed ({rc}): {err.strip()[:300]}")
        removed = self._parse_zypper_removal(out)
        # B2：解析失败回退请求名单时必须带 parsed_ok=False，由调用方明示
        return (removed, True) if removed else (list(names), False)

    @staticmethod
    def _parse_zypper_removal(out: str) -> list[str]:
        """解析 dry-run 输出中被标记为移除的包。
        LIMIT: zypper 表格格式随版本变化，解析不出时返回空列表，
        由 simulate_removal 统一决定回退语义."""
        removed: list[str] = []
        for line in out.splitlines():
            # 表格行形如: "r | foo | 1.0-1 | x86_64 | ..."
            if line.startswith(("r |", "r|")):
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2 and parts[1] and NAME_RE.match(parts[1]):
                    removed.append(parts[1])
        return removed

    def removal_ops(self, names: list[str], purge: bool = False) -> list[list[str]]:
        names = validate_names(names)
        return [["zypper", "--non-interactive", "remove"] + names]

    def autoremove_ops(self) -> list[list[str]]:
        return [["zypper", "--non-interactive", "remove", "-u"]]

    def autoremove_preview(self) -> list[str]:
        rc, out, _ = self._run(
            ["zypper", "--non-interactive", "rm", "--dry-run", "-u"])
        return self._parse_zypper_removal(out) if (rc == 0 or out) else []
