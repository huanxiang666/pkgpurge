"""轻量 i18n：内置 zh/en 词典，按 LANG 选择，缺省中文."""

from __future__ import annotations

import os

_ZH = {}  # zh 是源语言，key 即中文，无需翻译表

_EN = {
    "软件包卸载器": "Package Uninstaller",
    "搜索软件包…": "Search packages…",
    "刷新": "Refresh",
    "关于": "About",
    "卸载所选": "Uninstall Selected",
    "清除配置并卸载": "Purge (remove + config)",
    "清理孤立依赖": "Remove Orphan Deps",
    "模拟预览": "Simulate",
    "正在加载软件包列表…": "Loading package list…",
    "加载中…": "Loading…",
    "共 {n} 个软件包 · 已选 {m} 个": "{n} packages · {m} selected",
    "共 {n} 个软件包 · 已选 {m} 个 · {backend}": "{n} packages · {m} selected · {backend}",
    "将卸载 {n} 个软件包（含依赖级联）：": "{n} packages will be removed (with dependencies):",
    "确认卸载": "Confirm Removal",
    "取消": "Cancel",
    "卸载": "Uninstall",
    "正在执行卸载…": "Uninstalling…",
    "卸载完成": "Removal Finished",
    "卸载被取消（未通过身份验证）": "Cancelled (authentication dismissed)",
    "卸载失败": "Removal Failed",
    "详细信息": "Details",
    "选中左侧软件包查看详情": "Select a package to view details",
    "自动安装": "auto",
    "需要 root 权限，请在弹出的对话框中输入密码。": "Root required — enter your password in the dialog.",
    "本机未检测到受支持的包管理器": "No supported package manager found",
    "版本": "Version",
    "大小": "Size",
    "后端": "Backend",
    "系统": "System",
    "软件包": "Package",
}

_USE_EN = (os.environ.get("LANG", "") or os.environ.get("LC_ALL", "")).lower().startswith("en")


def _(text: str) -> str:
    """中文为 key；英文环境返回译文，其他环境原样返回."""
    if _USE_EN:
        return _EN.get(text, text)
    return text
