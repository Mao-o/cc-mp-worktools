---
description: agent-org plugin の beads 関連セットアップを diagnostic check する。bd CLI install 状態 / `bd doctor` の DB 健全性 / `~/.beads/<proj-hash>/` 存在 / custom type 登録状態 / AGENTS.md 配置を PASS/FAIL 表示する
---

# /bd-check

agent-org plugin v0.6.0 から beads (bd CLI) が hard dependency となったため、
セットアップ状態を 1 コマンドで diagnose するユーティリティ。`/start-watcher`
や `/fix-regression` の preflight が失敗した時、何が原因か切り分けるのに使う。

## 引数

なし。

```text
/bd-check
```

## 実行内容

以下の Bash script を実行し、各チェックを **PASS / FAIL / WARN** で表示する。
FAIL があれば対処手順を案内する。

```bash
#!/usr/bin/env bash
# /bd-check diagnostic
set -u

pass() { echo "  ✓ PASS: $1"; }
fail() { echo "  ✗ FAIL: $1"; failures=$((failures + 1)); }
warn() { echo "  ! WARN: $1"; warnings=$((warnings + 1)); }
failures=0
warnings=0

echo "=== /bd-check (agent-org v0.6.0) ==="
echo "cwd: $(pwd -P)"
echo ""

# --- 1. bd CLI install 状態 ---
echo "[1] bd CLI"
if command -v bd >/dev/null 2>&1; then
  bd_version="$(bd version 2>&1 | head -1)"
  pass "bd installed: $bd_version"
else
  fail "bd CLI not found (Mac: 'brew install beads')"
fi
echo ""

# --- 1b. jq install (本 script + 各 subagent / hook が依存) ---
echo "[1b] jq"
if command -v jq >/dev/null 2>&1; then
  pass "jq installed: $(jq --version 2>&1)"
else
  fail "jq not found (Mac: 'brew install jq'). subagent / hook / migration が動作不能"
fi
echo ""

# --- 2. proj-hash 計算 ---
echo "[2] proj-hash"
PROJ_HASH=$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
" 2>/dev/null || echo "")
if [ -n "$PROJ_HASH" ]; then
  pass "proj-hash=$PROJ_HASH"
else
  fail "python3 で proj-hash 計算に失敗 (python3 install が必要)"
fi
echo ""

# --- 3. ~/.beads/<proj-hash>/ ディレクトリ ---
echo "[3] beads database directory"
BEADS_PARENT="$HOME/.beads/$PROJ_HASH"
BEADS_DIR="$BEADS_PARENT/.beads"
if [ -d "$BEADS_DIR" ]; then
  pass "$BEADS_DIR exists"
else
  fail "$BEADS_DIR not initialized (run /org-init from project root)"
fi
echo ""

# --- 4. bd doctor (DB 健全性) ---
echo "[4] bd doctor"
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  doctor_out="$(BEADS_DIR=$BEADS_DIR bd doctor 2>&1)"
  doctor_exit=$?
  if [ "$doctor_exit" = "0" ]; then
    pass "bd doctor reports DB healthy"
    echo "$doctor_out" | head -3 | sed 's/^/    /'
  else
    fail "bd doctor failed (exit=$doctor_exit). Output:"
    echo "$doctor_out" | head -5 | sed 's/^/    /'
  fi
else
  warn "bd doctor skipped (bd or BEADS_DIR missing)"
fi
echo ""

# --- 5. custom type 登録 (`bd types` 出力 grep で verify) ---
# v0.7.0 で verify を `bd types` grep ベースに変更 (v0.7.1 hotfix: 登録 key は
# `types.custom` に revert、warning は false alarm、設定は effective)。
# 確認は `bd types` 出力に列挙されることで行う (実際に登録されたか直接見る)。
echo "[5] bd custom types (detection / fix / approval / episode / task)"
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  types_out="$(BEADS_DIR=$BEADS_DIR bd types 2>/dev/null || echo "")"
  missing=()
  for t in detection fix approval episode task; do
    echo "$types_out" | grep -qE "^  ${t}$" || missing+=("$t")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    pass "bd types: detection, fix, approval, episode, task all registered"
  else
    fail "bd types missing: ${missing[*]}. Run: BEADS_DIR=$BEADS_DIR bd config set types.custom 'detection,fix,approval,episode,task' (warning は false alarm)"
  fi
else
  warn "custom type check skipped"
fi
echo ""

# --- 6. beads.role git config (warning 抑制用) ---
echo "[6] git config beads.role"
if [ -d "$BEADS_PARENT/.git" ]; then
  role="$(cd "$BEADS_PARENT" && git config beads.role 2>/dev/null || echo "")"
  if [ "$role" = "maintainer" ]; then
    pass "beads.role=maintainer"
  else
    warn "beads.role not set (run: cd $BEADS_PARENT && git config beads.role maintainer)"
  fi
else
  warn "$BEADS_PARENT/.git not found (bd init not run yet?)"
fi
echo ""

# --- 7. AGENTS.md (optional、bd setup claude で生成可能) ---
echo "[7] AGENTS.md (optional)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/AGENTS.md" ]; then
  pass "AGENTS.md exists at $REPO_ROOT/AGENTS.md"
else
  warn "AGENTS.md not found (agent-org は --skip-agents で bd init するため optional。手動で 'bd setup claude' を実行すると生成される)"
fi
echo ""

# --- 8. .gitignore に beads marker (opt-in audit trail 用) ---
echo "[8] .gitignore agent-org marker"
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/.gitignore" ]; then
  if grep -q "agent-org plugin (v0.6.0+)" "$REPO_ROOT/.gitignore" 2>/dev/null; then
    pass ".gitignore has agent-org marker"
  else
    warn ".gitignore missing 'agent-org plugin (v0.6.0+)' marker (run /org-init to add)"
  fi
else
  warn ".gitignore not found"
fi
echo ""

# --- 9. open issues snapshot ---
echo "[9] open bd issues snapshot"
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  for t in detection fix approval; do
    count="$(BEADS_DIR=$BEADS_DIR bd list -t $t --status open --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)"
    echo "    open $t: $count"
  done
fi
echo ""

# --- summary ---
echo "=== summary ==="
echo "failures: $failures"
echo "warnings: $warnings"

if [ "$failures" -gt 0 ]; then
  echo ""
  echo "Address FAILs above before running /start-watcher or /fix-regression."
  exit 1
fi

if [ "$warnings" -gt 0 ]; then
  echo ""
  echo "WARNs are non-blocking but recommended to address."
fi

exit 0
```

## PASS / FAIL / WARN の意味

| ラベル | 意味 | 対応 |
|---|---|---|
| PASS | チェック項目が想定どおり | なし |
| FAIL | hard dependency が欠落、`/start-watcher` / `/fix-regression` が起動不能 | 表示された対処コマンドを実行 |
| WARN | 推奨設定が未適用だが起動は可能 | 余裕があるときに対処 |

## 典型シナリオ

### 新規プロジェクトで `/bd-check` を初実行した

最初は全項目が FAIL になる可能性が高い。順番に:

1. `brew install beads` → bd CLI 入手
2. 該当プロジェクト root で `/org-init` → `~/.beads/<proj-hash>/` 作成 +
   custom type 登録 + `.gitignore` 更新
3. もう一度 `/bd-check` → 全 PASS / WARN のみ

### `bd doctor` が FAIL になる

bd DB が破損している可能性。よくある原因:

- `~/.beads/<proj-hash>/.beads/` を手動で削除した / mv した
- `~/.beads/<proj-hash>/.git/` の状態が壊れた (`git status` で確認)

対処: 退避 (`mv ~/.beads/<proj-hash> ~/.beads/<proj-hash>.bak`) してから
`/org-init` を再実行、必要なら `bd import` で .bak から復旧。

### custom types が未登録で FAIL になる

`/org-init` 実行時に `bd config set types.custom` がスキップされた可能性。
手動で:

```bash
PROJ_HASH=<your-proj-hash>
BEADS_DIR=~/.beads/$PROJ_HASH/.beads bd config set types.custom \
  "detection,fix,approval,episode,task"

# verify
BEADS_DIR=~/.beads/$PROJ_HASH/.beads bd types | grep -E "^  (detection|fix|approval|episode|task)$"
```

bd 1.0.4 は `Warning: "types.custom" is not a recognized config key` を吐くが
**設定は実際に effective** で `bd types` 出力に反映される (v0.7.1 hotfix で
実機確認)。warning は false alarm として無視してよい。v0.7.0 で試した
`custom.types` は逆に無視されることを実機検証で確認済 (CHANGELOG 0.7.1 参照)。

## 注意事項

- `/bd-check` 自体は読取専用 (`bd config get` / `bd list` / `bd doctor` /
  `git config`)。ファイル書込は行わない
- `bd version` / `bd doctor` は network access 不要 (ローカル DB のみ参照)
- `--bg` セッションでは plugin slash command が解決されないため、watcher /
  fixer 自身は `/bd-check` を呼べない (main session 専用)

## 関連

- 初期化: `commands/org-init.md`
- watcher 起動 preflight: `commands/start-watcher.md`
- fixer 起動 preflight: `commands/fix-regression.md`
- migration: `commands/migrate-to-beads.md`, `commands/migrate-from-beads.md`
- beads 公式: <https://github.com/steveyegge/beads>
