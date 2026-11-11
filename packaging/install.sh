#!/usr/bin/env bash
# 源码安装（任意发行版通用；Debian 系推荐直接用 .deb 包）。
# 用法：sudo ./install.sh [PREFIX]     默认 PREFIX=/usr/local
# 卸载：sudo ./install.sh --uninstall [PREFIX]
set -euo pipefail

ACTION="install"
if [ "${1:-}" = "--uninstall" ]; then
  ACTION="uninstall"
  shift
fi
PREFIX="${1:-/usr/local}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# N11：PREFIX 参与 rm -rf 路径拼接，必须校验为绝对路径且不是根
case "$PREFIX" in
  /*) ;;
  *) echo "error: PREFIX must be an absolute path (got: '$PREFIX')"; exit 1 ;;
esac
if [ -z "$PREFIX" ] || [ "$PREFIX" = "/" ]; then
  echo "error: PREFIX must not be /"; exit 1
fi

[ "$(id -u)" = 0 ] || { echo "error: run as root (sudo ./install.sh)"; exit 1; }

HELPER="$PREFIX/lib/pkgpurge/pkexec_helper.py"

if [ "$ACTION" = uninstall ]; then
  echo "== uninstalling pkgpurge from $PREFIX =="
  rm -f  "$PREFIX/bin/pkgpurge"
  rm -rf "$PREFIX/lib/pkgpurge"
  rm -f  "$PREFIX/share/applications/pkgpurge.desktop"
  rm -f  "$PREFIX/share/polkit-1/actions/com.github.pkgpurge.policy"
  rm -f  "$PREFIX/share/icons/hicolor/scalable/apps/pkgpurge.svg"
  rm -f  "$PREFIX/share/metainfo/pkgpurge.metainfo.xml"
  echo "done. (Python deps like python3-gi were installed by your distro; remove manually if desired.)"
  exit 0
fi

# --- 依赖自检：给出各发行版的安装命令而不是硬失败 ---
missing=""
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || missing="$missing python3(>=3.10)"
python3 -c 'import gi' 2>/dev/null || missing="$missing python3-gi(PyGObject)"
python3 -c 'import gi; gi.require_version("Gtk","4.0"); from gi.repository import Gtk' 2>/dev/null || missing="$missing gtk4 gir"
command -v pkexec >/dev/null || missing="$missing pkexec"
if [ -n "$missing" ]; then
  echo "WARNING: missing dependencies:$missing"
  echo "  Debian/Ubuntu : sudo apt install python3-gi gir1.2-gtk-4.0 pkexec"
  echo "  Fedora        : sudo dnf install python3-gobject gtk4 pkexec"
  echo "  Arch          : sudo pacman -S python-gobject gtk4 polkit"
  echo "  openSUSE      : sudo zypper install python3-gobject gtk4-branding polkit"
  echo "  Alpine        : doas apk add py3-gobject gtk4.0 polkit"
  echo "Installing anyway; the CLI works without GTK, the GUI needs it."
fi

VERSION="$(cd "$ROOT" && python3 -c 'from pkgpurge import __version__; print(__version__)')"
echo "== installing pkgpurge $VERSION to $PREFIX =="

install -d "$PREFIX/bin" "$PREFIX/lib/pkgpurge/pkgpurge/backends" \
           "$PREFIX/share/applications" "$PREFIX/share/polkit-1/actions" \
           "$PREFIX/share/icons/hicolor/scalable/apps" "$PREFIX/share/metainfo"

(cd "$ROOT" && find pkgpurge -name '*.py' -not -path '*__pycache__*' -print0) \
  | while IFS= read -r -d '' f; do
      install -m 644 -D "$ROOT/$f" "$PREFIX/lib/pkgpurge/$f"
    done

cat > "$PREFIX/bin/pkgpurge" <<EOF
#!/usr/bin/python3
import sys
sys.path.insert(0, "$PREFIX/lib/pkgpurge")
from pkgpurge.__main__ import main
sys.exit(main())
EOF
chmod 755 "$PREFIX/bin/pkgpurge"

cat > "$HELPER" <<EOF
#!/usr/bin/python3
import sys
sys.path.insert(0, "$PREFIX/lib/pkgpurge")
from pkgpurge.helper import main
sys.exit(main(sys.argv))
EOF
chmod 755 "$HELPER"

install -m 644 "$ROOT/data/pkgpurge.desktop" "$PREFIX/share/applications/pkgpurge.desktop"
sed "s|@HELPER_PATH@|$HELPER|g" "$ROOT/data/com.github.pkgpurge.policy" \
  > "$PREFIX/share/polkit-1/actions/com.github.pkgpurge.policy"
chmod 644 "$PREFIX/share/polkit-1/actions/com.github.pkgpurge.policy"
install -m 644 "$ROOT/data/pkgpurge.svg" "$PREFIX/share/icons/hicolor/scalable/apps/pkgpurge.svg"
install -m 644 "$ROOT/data/pkgpurge.metainfo.xml" "$PREFIX/share/metainfo/pkgpurge.metainfo.xml"

command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$PREFIX/share/icons/hicolor" || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q "$PREFIX/share/applications" || true

echo "done: $PREFIX/bin/pkgpurge"
echo "note: find_helper() locates $HELPER automatically; or export PKGPURGE_HELPER=$HELPER"
