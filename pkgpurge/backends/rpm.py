"""RPM 系共享列表逻辑（dnf / zypper 都基于 rpm 数据库）."""

from __future__ import annotations

from .base import Package, PackageBackend

# name \t version-release \t size(bytes) \t summary
RPM_QF = "%{NAME}\\t%{VERSION}-%{RELEASE}\\t%{SIZE}\\t%{SUMMARY}\\n"


class RpmListMixin:
    """提供 rpm -qa 的包列表解析；子类补各自的 info/removal 命令."""

    def _rpm_list(self) -> list[Package]:
        rc, out, err = self._run(["rpm", "-qa", "--qf", RPM_QF])
        if rc != 0:
            raise RuntimeError(f"rpm -qa failed ({rc}): {err.strip()[:300]}")
        pkgs: list[Package] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 3)
            if len(parts) < 3:
                continue
            name, version, size = parts[0], parts[1], parts[2]
            summary = (parts[3] if len(parts) > 3 else "").replace("\t", " ")
            # rpm summary 可能自带 "(latin1)" 之类乱码，原样保留，不做猜测性修复
            try:
                size_bytes = int(size)
            except ValueError:
                size_bytes = 0
            pkgs.append(Package(name=name, version=version,
                                size_bytes=size_bytes, summary=summary))
        pkgs.sort(key=lambda p: p.name.lower())
        return pkgs
