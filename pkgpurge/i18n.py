"""轻量 i18n：内置 zh/en 词典，按 locale 选择，缺省中文.

优先级（N9）：LC_ALL > LANG——LC_ALL 是 POSIX 约定的最高优先覆盖项。
"""

from __future__ import annotations

import os

_EN = {
    "软件包卸载器": "Package Uninstaller",
    "搜索软件包…": "Search packages…",
    "刷新": "Refresh",
    "关于": "About",
    "卸载所选": "Uninstall Selected",
    "清除配置并卸载": "Purge (remove + config)",
    "清理孤立依赖": "Remove Orphan Deps",
    "模拟预览…": "Simulating…",
    "正在加载软件包列表…": "Loading package list…",
    "加载中…": "Loading…",
    "共 {n} 个软件包 · 已选 {m} 个 · {backend}": "{n} packages · {m} selected · {backend}",
    "将卸载 {n} 个软件包（含依赖级联）：": "{n} package(s) will be removed (with dependencies):",
    "取消": "Cancel",
    "卸载": "Uninstall",
    "正在执行卸载…": "Uninstalling…",
    "卸载完成": "Removal Finished",
    "卸载被取消（未通过身份验证）": "Cancelled (authentication dismissed)",
    "卸载失败": "Removal Failed",
    "详细信息": "Details",
    "选中左侧软件包查看详情": "Select a package to view details",
    "需要 root 权限，请在弹出的对话框中输入密码。": "Root required — enter your password in the dialog.",
    "本机未检测到受支持的包管理器": "No supported package manager found",
    "加载软件包列表失败": "Failed to load package list",
    "请先勾选要卸载的软件包": "Select packages to uninstall first",
    "没有需要卸载的软件包": "Nothing to uninstall",
    "所选软件包未安装，或仅剩配置残留（apt 可用「清除配置并卸载」清理）。":
        "The selected packages are not installed, or only config residue remains"
        " (use Purge on apt to clean it).",
    "无法构造卸载命令": "Failed to build removal command",
    "清理不再被需要的依赖软件包？": "Remove dependencies that are no longer needed?",
    "清理": "Clean",
    "(后端未提供预览列表，将执行其原生 autoremove)":
        "(backend provided no preview list; its native autoremove will run)",
    "依赖级联解析失败：以下仅为你的勾选清单，实际移除可能包含更多依赖":
        "Dependency-cascade parsing FAILED: the list below is only your selection;"
        " the actual removal may include more dependencies",
    "跨发行版 Linux 软件包卸载器": "Cross-distro Linux package uninstaller",
    "后端": "Backend",
    "系统": "System",
    "版本": "Version",
    "大小": "Size",
    "软件包": "Package",
    "自动安装": "auto",
}

# LC_ALL 覆盖 LANG（POSIX 约定）
_lang = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
_USE_EN = _lang.startswith("en")


def _(text: str) -> str:
    """中文为 key；英文环境返回译文，其他环境原样返回."""
    if _USE_EN:
        return _EN.get(text, text)
    return text
