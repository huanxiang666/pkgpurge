"""后端探测：按二进制存在性选择包管理器，os-release 只用于展示.

二进制探测优先于 os-release 的 ID/ID_LIKE：容器、WSL、混合系统里
os-release 可能与实际可用的包管理器不一致，二进制在不在才是硬事实。
"""

from __future__ import annotations

import os
import shutil

from .apk import ApkBackend
from .apt import AptBackend
from .base import OpResult, Package, PackageBackend, human_size, validate_name
from .dnf import DnfBackend
from .pacman import PacmanBackend
from .zypper import ZypperBackend

__all__ = ["ApkBackend", "AptBackend", "DnfBackend", "Package", "PackageBackend",
           "PacmanBackend", "ZypperBackend", "OpResult", "detect_backend",
           "distro_pretty", "human_size", "validate_name"]

# 探测顺序即优先级：同一台机器同时有 apt 和 rpm 时（罕见），按此顺序取第一个。
_PROBES: list[tuple[str, type[PackageBackend]]] = [
    ("apt-get", AptBackend),
    ("dnf", DnfBackend),
    ("zypper", ZypperBackend),
    ("pacman", PacmanBackend),
    ("apk", ApkBackend),
]


def detect_backend(which: callable = shutil.which) -> PackageBackend | None:
    """返回本机第一个可用的后端实例；找不到返回 None."""
    for binary, cls in _PROBES:
        if which(binary):
            return cls()
    return None


def distro_pretty(os_release_path: str = "/etc/os-release") -> str:
    """读取发行版展示名，如 'Ubuntu 26.04.1 LTS'. 读不到返回 'Linux'."""
    fields: dict[str, str] = {}
    try:
        with open(os_release_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    fields[k.strip()] = v.strip().strip('"')
    except OSError:
        return "Linux"
    name = fields.get("PRETTY_NAME") or fields.get("NAME") or "Linux"
    return name
