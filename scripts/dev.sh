#!/usr/bin/env bash
# Usage: ./scripts/dev.sh [plugin-name]
# 例:    ./scripts/dev.sh example-plugin
#
# claude --plugin-dir で指定 plugin を marketplace 登録なしに直接ロードする。
# インストール済み同名 plugin より優先されるため、手元の編集を即テストできる。
set -euo pipefail

PLUGIN="${1:-example-plugin}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="${REPO_ROOT}/plugins/${PLUGIN}"

if [ ! -d "$TARGET" ]; then
  echo "Plugin not found: $TARGET" >&2
  echo "Available plugins:" >&2
  ls "${REPO_ROOT}/plugins" >&2 2>/dev/null || true
  exit 1
fi

echo "Loading plugin from: $TARGET"
exec claude --plugin-dir "$TARGET"
