# PkgPurge · 软件包卸载器

跨发行版的 Linux 软件包卸载管理工具：GTK4 图形界面 + 无头 CLI，
一个 `.deb` 装遍 Debian 系，源码安装适配所有主流发行版。

A cross-distro Linux package uninstaller: GTK4 GUI + headless CLI.
Ships as a `.deb` for Debian-based systems, source install for everything else.

![version](https://img.shields.io/badge/version-1.0.0-blue) ![license](https://img.shields.io/badge/license-GPL--3.0-green) ![python](https://img.shields.io/badge/python-≥3.10-yellow)

---

## 功能 / Features

| 功能 | 说明 |
|---|---|
| 📦 全量包列表 | 名称、版本、安装大小、摘要、`[auto]` 依赖安装标记 |
| 🔍 即时搜索 | 按名称/摘要过滤，几千个包不卡顿（worker 线程加载，UI 不冻结） |
| ✅ 多选卸载 | 复选框多选，状态栏实时计数 |
| 🧪 级联模拟预览 | 卸载前先跑 `apt-get -s` / `dnf --assumeno` / `pacman --print` / `zypper --dry-run`，**明确区分「你勾选的」和「被依赖带走的」**，确认后才执行 |
| 🧹 清除配置（purge） | apt `-y --purge remove`、pacman `-Rsn`（rc 残留一键清） |
| ♻️ 孤立依赖清理 | apt autoremove / dnf autoremove / zypper rm -u / pacman 孤儿脚本 |
| 🔐 最小提权面 | 所有 root 操作经 `pkexec` + polkit，助手脚本只放行**白名单命令**（见下文安全模型） |
| 🖥️ 无头 CLI | `list / search / info / remove / autoremove / doctor`，支持 `--json`、`--simulate`、`--dry-run`，可脚本化 |
| 🌍 多发行版 | 按二进制探测自动选择后端，同机混装也能正确识别 |

## 支持的发行版 / Supported distros

| 后端 | 发行版 | 探测依据 | purge | autoremove | 模拟预览 |
|---|---|---|---|---|---|
| **APT** | Debian, Ubuntu, Linux Mint, Deepin, Kali, Zorin, Pop!_OS … | `apt-get` | ✅ | ✅ | ✅ |
| **DNF** | Fedora, RHEL, CentOS Stream, Rocky, AlmaLinux … | `dnf` | —(rpm 语义) | ✅ | ✅ |
| **Zypper** | openSUSE Leap/Tumbleweed, SLES | `zypper` | — | ✅ (`rm -u`) | ✅ |
| **Pacman** | Arch, Manjaro, EndeavourOS, Garuda … | `pacman` | ✅ (`-Rsn`) | ✅ (孤儿清理) | ✅ |
| **APK** | Alpine, postmarketOS | `apk` | — | —(apk del 自带回收) | — |

## 安装 / Install

### Debian / Ubuntu（.deb）

```bash
# 方式一：从 GitHub Releases 下载
sudo apt install ./pkgpurge_1.0.0_all.deb

# 方式二：源码构建 deb（需要 dpkg-deb + fakeroot）
git clone https://github.com/huanxiang666/pkgpurge.git
cd pkgpurge && make deb
sudo apt install ./dist/pkgpurge_1.0.0_all.deb
```

### 其他发行版（源码安装）

```bash
# 先装依赖：
#   Fedora   : sudo dnf install python3-gobject gtk4 pkexec
#   Arch     : sudo pacman -S python-gobject gtk4 polkit
#   openSUSE : sudo zypper install python3-gobject gtk4 polkit
#   Alpine   : doas apk add py3-gobject gtk4.0 polkit
git clone https://github.com/huanxiang666/pkgpurge.git
cd pkgpurge && sudo ./packaging/install.sh          # 默认 /usr/local
sudo ./packaging/install.sh --uninstall             # 卸载
```

依赖：`python3 ≥ 3.10`、`PyGObject (GTK4)`、`pkexec (polkit)`。CLI 不需要 GTK。

## 使用 / Usage

**GUI**：应用菜单启动「软件包卸载器」，或终端 `pkgpurge`。
勾选 → 「卸载所选」（先看级联预览再确认）→ 输一次密码（`auth_admin_keep` 短时间内免重复认证）。

**CLI**：

```bash
pkgpurge doctor                          # 环境/后端诊断 (JSON)
pkgpurge list --sort size --limit 20     # 最大的 20 个包
pkgpurge search nginx --json             # 脚本化检索
pkgpurge info nginx
pkgpurge remove nginx --simulate         # 只看级联，不动手
pkgpurge remove nginx --purge            # 连配置一起清（apt/pacman）
pkgpurge autoremove                      # 清孤立依赖
pkgpurge remove foo --dry-run            # 走完认证但只回显 root 命令（演练）
```

## 安全模型 / Security model

本机任何进程都可以对 `pkexec_helper.py` 发起 `pkexec` 调用，polkit 弹窗**不展示参数**，
所以提权助手自身必须 fail-closed。它对每条命令做四层校验，任何一条不过即整体拒绝：

1. **二进制白名单**：仅 `apt-get / dnf / zypper / pacman / apk`，以及仅用于 pacman 孤儿清理的 `/bin/bash`；
2. **bash 脚本全文匹配**：只放行与内置孤儿清理脚本**逐字符一致**的字符串，杜绝任意 root shell；
3. **动词白名单**：`apt-get` 只许 `remove/purge/autoremove` —— `install`、`update` 等虽然长得像合法包名也一律拒绝；
4. **旗标白名单 + 包名正则**：`-` 开头参数必须在旗标白名单内（挡掉 `-oDPkg::Options::=…` 之类注入），其余参数必须匹配 `^[A-Za-z0-9][A-Za-z0-9._+:-]*$`。

白名单与包名正则的对抗用例见 `tests/test_helper.py`（覆盖 `rm -rf`、`$(id)`、分号拼接、旗标注入、动词伪装等向量），并做了变异测试验证断言真实有效。

## 开发 / Development

```bash
make test       # 50 个单测（假 runner / 临时数据库，不碰真实系统）
make selftest   # GUI 无头自检：真实加载包列表并校验命令生成
make run        # 直接跑开发树 GUI
make deb        # 构建 .deb 并现场核验 control/清单/sha256
```

CI（GitHub Actions）在 ubuntu-22.04 / 24.04 / latest 三档跑单测 + CLI 冒烟 + deb 构建。

## 已知限制 / Known limits

- Zypper/DNF 的模拟预览输出解析是 best-effort（表格格式随版本变化），解析失败时退回请求列表并在 UI 中明示；
- Zypper 无 auto-installed 标记，列表中不显示 `[auto]`；
- APK 不支持模拟预览与独立 autoremove（`apk del` 自带依赖回收）；
- 未做 Flatpak/Snap 后端（可作为后续扩展点，后端接口已抽象好）。

## License

GPL-3.0-or-later © 2026 huanxiang666
