#!/usr/bin/env bash
# 构建 pkgpurge 的 .deb 包（不依赖 debhelper/dh，只需 dpkg-deb + fakeroot）。
# 产物：dist/pkgpurge_<version>_all.deb，并现场核验 control 字段与文件清单。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="$(cd "$ROOT" && python3 -c 'from pkgpurge import __version__; print(__version__)')"
STAGE="$ROOT/build/pkgpurge_${VERSION}_all"
OUT="$ROOT/dist/pkgpurge_${VERSION}_all.deb"
HELPER="/usr/lib/pkgpurge/pkexec_helper.py"

echo "== building pkgpurge ${VERSION} =="
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" \
         "$STAGE/usr/bin" \
         "$STAGE/usr/lib/pkgpurge/pkgpurge/backends" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/polkit-1/actions" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/metainfo" \
         "$ROOT/dist"

# --- Python 包（排除 __pycache__）---
(cd "$ROOT" && find pkgpurge -name '*.py' -not -path '*__pycache__*' -print0) \
  | while IFS= read -r -d '' f; do
      install -m 644 -D "$ROOT/$f" "$STAGE/usr/lib/pkgpurge/$f"
    done

# --- /usr/bin 入口 ---
cat > "$STAGE/usr/bin/pkgpurge" <<'EOF'
#!/usr/bin/python3
import sys
sys.path.insert(0, "/usr/lib/pkgpurge")
from pkgpurge.__main__ import main
sys.exit(main())
EOF
chmod 755 "$STAGE/usr/bin/pkgpurge"

# --- pkexec helper 入口（polkit exec.path 指向它）---
cat > "$STAGE$HELPER" <<'EOF'
#!/usr/bin/python3
import sys
sys.path.insert(0, "/usr/lib/pkgpurge")
from pkgpurge.helper import main
sys.exit(main(sys.argv))
EOF
chmod 755 "$STAGE$HELPER"

# --- postinst / postrm ---
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
exit 0
EOF
cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
case "$1" in
  remove|purge|abort-upgrade|failed-upgrade)
    command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
    ;;
esac
exit 0
EOF
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# --- desktop / policy / icon / metainfo ---
install -m 644 "$ROOT/data/pkgpurge.desktop" "$STAGE/usr/share/applications/pkgpurge.desktop"
sed "s|@HELPER_PATH@|$HELPER|g" "$ROOT/data/com.github.pkgpurge.policy" \
  > "$STAGE/usr/share/polkit-1/actions/com.github.pkgpurge.policy"
chmod 644 "$STAGE/usr/share/polkit-1/actions/com.github.pkgpurge.policy"
install -m 644 "$ROOT/data/pkgpurge.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/pkgpurge.svg"
install -m 644 "$ROOT/data/pkgpurge.metainfo.xml" "$STAGE/usr/share/metainfo/pkgpurge.metainfo.xml"

# --- copyright（N10：GPL 二进制分发必须携带机器可读版权文件）---
install -d "$STAGE/usr/share/doc/pkgpurge"
cat > "$STAGE/usr/share/doc/pkgpurge/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: pkgpurge
Source: https://github.com/huanxiang666/pkgpurge

Files: *
Copyright: 2026 huanxiang666
License: GPL-3.0+

License: GPL-3.0+
 This program is free software: you can redistribute it and/or modify
 it under the terms of the GNU General Public License as published by
 the Free Software Foundation, either version 3 of the License, or
 (at your option) any later version.
 .
 This program is distributed in the hope that it will be useful,
 but WITHOUT ANY WARRANTY; without even the implied warranty of
 MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 GNU General Public License for more details.
 .
 On Debian systems, the complete text of the GNU General Public
 License version 3 can be found in /usr/share/common-licenses/GPL-3.
EOF

# --- DEBIAN/control（数据文件全部就位后生成，Installed-Size 用实测值）---
INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: pkgpurge
Version: ${VERSION}
Section: admin
Priority: optional
Architecture: all
Installed-Size: ${INSTALLED_SIZE}
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, pkexec | policykit-1
Recommends: apt | dnf | zypper | pacman | apk
Maintainer: huanxiang666 <huanxiang666@users.noreply.github.com>
Homepage: https://github.com/huanxiang666/pkgpurge
Description: Cross-distro Linux package uninstaller (GTK4)
 PkgPurge lists every package installed through the system package
 manager (APT, DNF, Zypper, Pacman, APK), supports multi-select
 removal with a dependency-cascade simulation preview, and executes
 removals via pkexec behind a strict command whitelist.
 Ships a headless CLI with JSON output for scripting.
 GTK >= 4.10 uses native AlertDialogs; older GTK4 (>= 4.6, e.g.
 Ubuntu 22.04) automatically falls back to MessageDialog.
EOF

# --- 构建 ---
rm -f "$OUT"
fakeroot dpkg-deb --build --root-owner-group "$STAGE" "$OUT" >/dev/null

# --- 核验 ---
echo "== control =="
dpkg-deb -I "$OUT"
echo "== contents =="
dpkg-deb -c "$OUT"
echo "== sha256 =="
sha256sum "$OUT"
echo "== DONE: $OUT =="
