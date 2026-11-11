#!/usr/bin/env bash
# 可复现变异检查（N22/README 佐证）：故意破坏 helper 的动词白名单 →
# 对应测试必须变红；还原 → 必须变绿。红绿都验证才算通过。
# 用法：make mutation-check
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TARGET="pkgpurge/helper.py"
BACKUP="$(mktemp /tmp/pkgpurge-mutation-backup.XXXXXX.py)"
TEST="tests.test_helper.TestValidateOpsReject.test_apt_install_verb_blocked"

cp "$TARGET" "$BACKUP"
restore() { cp "$BACKUP" "$TARGET"; rm -f "$BACKUP"; }
trap restore EXIT

python3 - <<'EOF'
p = "pkgpurge/helper.py"
s = open(p, encoding="utf-8").read()
old = '''                if spec["verbs"]:
                    if arg not in spec["verbs"]:
                        return f"op[{i}]: verb not allowed: {arg!r}"
                    continue'''
new = '''                if spec["verbs"]:
                    continue'''
assert old in s, "mutation target not found — helper.py changed, update this script"
open(p, "w", encoding="utf-8").write(s.replace(old, new))
EOF

echo "== mutant: verb whitelist removed =="
if python3 -m unittest "$TEST" >/dev/null 2>&1; then
  echo "FAIL: test stayed GREEN under mutation — the assertion is vacuous"
  exit 1
fi
echo "ok: test turned RED under mutation"

restore
trap - EXIT

echo "== restored =="
if ! python3 -m unittest "$TEST" >/dev/null 2>&1; then
  echo "FAIL: test still RED after restore"
  exit 1
fi
echo "PASS: red under mutation, green after restore"
