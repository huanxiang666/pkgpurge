"""后端抽象层：所有包管理器后端实现同一接口，GUI/CLI 不感知具体发行版."""

from __future__ import annotations

import abc
import dataclasses
import re

from ..proc import Runner, run_cmd

# 包名白名单正则：字母数字开头，允许 . _ + - : @
# （apt 的 name:arch 需要冒号；Arch makepkg pkgname 合法字符集含 @）。
# helper 与后端在构造任何 root 命令前都用它校验，防注入。
# 两处正则的同源一致性由 tests/test_helper.py::test_name_regex_same_source 断言。
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:@\-]*$")
MAX_NAME_LEN = 200


def validate_name(name: str) -> str:
    """校验单个包名，非法即抛 ValueError（fail-closed）."""
    if not isinstance(name, str) or not NAME_RE.match(name) or len(name) > MAX_NAME_LEN:
        raise ValueError(f"illegal package name: {name!r}")
    return name


def validate_names(names) -> list[str]:
    out = [validate_name(n) for n in names]
    if not out:
        raise ValueError("empty package list")
    return out


def human_size(num_bytes: float) -> str:
    if num_bytes <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(num_bytes)} B"
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


@dataclasses.dataclass
class Package:
    name: str
    version: str = ""
    size_bytes: int = 0
    summary: str = ""
    auto_installed: bool = False
    arch: str = ""

    @property
    def display_name(self) -> str:
        return self.name


@dataclasses.dataclass
class OpResult:
    """统一三态结果：成功 / 用户取消 / 失败（含原因）."""
    ok: bool
    cancelled: bool = False
    message: str = ""
    data: object = None


class PackageBackend(abc.ABC):
    """包管理器后端接口."""

    id: str = "unknown"
    label: str = "Unknown"
    supports_purge: bool = False
    supports_autoremove: bool = False
    supports_simulate: bool = False

    def __init__(self, run: Runner | None = None):
        self._run = run or run_cmd

    @abc.abstractmethod
    def list_installed(self) -> list[Package]:
        """列出全部已安装软件包."""

    @abc.abstractmethod
    def info(self, name: str) -> str:
        """单个包的详细信息（人类可读文本）."""

    def simulate_removal(self, names: list[str], purge: bool = False) -> tuple[list[str], bool]:
        """预览将被移除的包（含依赖级联）。

        返回 (包列表, parsed_ok)。parsed_ok=False 表示级联解析失败，
        列表已回退为请求名单——调用方**必须**向用户明示这一点，
        实际移除可能包含更多依赖（B2：禁止静默回退冒充级联结果）。
        """
        return list(names), False

    @abc.abstractmethod
    def removal_ops(self, names: list[str], purge: bool = False) -> list[list[str]]:
        """生成卸载命令序列（交给 pkexec helper 校验后执行）."""

    def removal_target(self, pkg: "Package") -> str:
        """GUI/CLI 把一个 Package 转成 removal_ops 接受的标识。
        默认裸包名；apt 覆写以处理 multiarch."""
        return pkg.name

    def autoremove_ops(self) -> list[list[str]]:
        """生成孤立依赖清理命令。不支持的后端抛 NotImplementedError."""
        raise NotImplementedError(f"{self.id} backend has no autoremove")

    def autoremove_preview(self) -> list[str]:
        """孤立依赖清理的预览列表。默认空."""
        return []
