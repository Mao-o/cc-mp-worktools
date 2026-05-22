---
description: agent-org plugin の beads 関連セットアップを diagnostic check する。bd CLI install 状態 / `bd doctor` の DB 健全性 / `<repo>/.beads/` 存在 / custom type 登録状態 / AGENTS.md 配置を PASS/FAIL 表示する。v0.8.0 (ADR-007) で bd は `<repo>/.beads/` に repo-local 配置
---

# /bd-check

agent-org plugin v0.6.0 から beads (bd CLI) が hard dependency となったため、
セットアップ状態を 1 コマンドで diagnose するユーティリティ。`/start-watcher`
や `/fix-regression` の preflight が失敗した時、何が原因か切り分けるのに使う。

v0.8.0 (ADR-007) で bd の物理配置が `~/.beads/<proj-hash>/.beads/` から
**`<repo>/.beads/`** に変更されたため、本 check は新 path をベースに動作する。
旧 path が残っているプロジェクトは別 section で warn + migration 案内を出す。

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
# /bd-check diagnostic (v0.8.0 — bd repo-local at <repo>/.beads/)
set -u

pass() { echo "  ✓ PASS: $1"; }
fail() { echo "  ✗ FAIL: $1"; failures=$((failures + 1)); }
warn() { echo "  ! WARN: $1"; warnings=$((warnings + 1)); }
failures=0
warnings=0

echo "=== /bd-check (agent-org v0.8.0) ==="
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

# --- 2. proj-hash 計算 (旧 path 検出 + label prefix 用、bd path には不要) ---
echo "[2] proj-hash (legacy path detection / label prefix)"
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

# --- 3. repo root + <main_repo>/.beads/ ディレクトリ (v0.8.0 path) ---
# worktree 内で実行された場合 (--bg `.claude/worktrees/<id>/`) は
# git rev-parse --show-toplevel が worktree root を返す。bd は worktree-aware
# で main repo `.beads/` を共有するため、git common-dir 経由で main_repo を解決
echo "[3] beads database directory (v0.8.0: <repo>/.beads/、worktree-aware)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$REPO_ROOT" ]; then
  fail "not in a git repository. v0.8.0 (ADR-007) では bd を <repo>/.beads/ に配置するため git repo 内での実行が必要"
  MAIN_REPO=""
  BEADS_DIR=""
else
  MAIN_REPO="$(cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
  [ -n "$MAIN_REPO" ] || MAIN_REPO="$REPO_ROOT"
  if [ "$REPO_ROOT" != "$MAIN_REPO" ]; then
    echo "    (worktree detected: $REPO_ROOT → main_repo=$MAIN_REPO)"
  fi
  BEADS_DIR="$MAIN_REPO/.beads"
  if [ -d "$BEADS_DIR" ]; then
    pass "$BEADS_DIR exists"
  else
    fail "$BEADS_DIR not initialized (run /org-init at main repo root: $MAIN_REPO)"
  fi
fi
echo ""

# --- 3b. legacy path 検出 (~/.beads/<proj-hash>/.beads/、v0.7.x までの配置) ---
echo "[3b] legacy beads path (~/.beads/<proj-hash>/, v0.7.x default)"
LEGACY_BEADS_DIR="$HOME/.beads/$PROJ_HASH/.beads"
if [ -n "$PROJ_HASH" ] && [ -d "$LEGACY_BEADS_DIR" ]; then
  warn "$LEGACY_BEADS_DIR が残存しています (v0.7.x からの未完了 migration)"
  echo "    対処: '/migrate-beads-to-repo-local' を実行して新 path (<repo>/.beads/) に統合"
elif [ -n "$PROJ_HASH" ]; then
  pass "legacy path not present (clean v0.8.0+ setup)"
else
  warn "legacy path check skipped (proj-hash unavailable)"
fi
echo ""

# --- 4. bd doctor (DB 健全性) ---
echo "[4] bd doctor"
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  doctor_out="$(cd "$REPO_ROOT" && bd doctor 2>&1)"
  doctor_exit=$?
  if [ "$doctor_exit" = "0" ]; then
    pass "bd doctor reports DB healthy"
    echo "$doctor_out" | head -3 | sed 's/^/    /'
  else
    fail "bd doctor failed (exit=$doctor_exit). Output:"
    echo "$doctor_out" | head -5 | sed 's/^/    /'
  fi
else
  warn "bd doctor skipped (bd or <repo>/.beads/ missing)"
fi
echo ""

# --- 5. custom type 登録 (`bd types` 出力 grep で verify) ---
# v0.7.0 で verify を `bd types` grep ベースに変更 (v0.7.1 hotfix: 登録 key は
# `types.custom` に revert、warning は false alarm、設定は effective)。
# 確認は `bd types` 出力に列挙されることで行う (実際に登録されたか直接見る)。
# v0.8.0: cd <repo> で bd 自動 resolve、BEADS_DIR 明示指定なし
echo "[5] bd custom types (detection / fix / approval / episode / task)"
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  types_out="$(cd "$REPO_ROOT" && bd types 2>/dev/null || echo "")"
  missing=()
  for t in detection fix approval episode task; do
    echo "$types_out" | grep -qE "^  ${t}$" || missing+=("$t")
  done
  if [ ${#missing[@]} -eq 0 ]; then
    pass "bd types: detection, fix, approval, episode, task all registered"
  else
    fail "bd types missing: ${missing[*]}. Run: (cd $REPO_ROOT && bd config set types.custom 'detection,fix,approval,episode,task') (warning は false alarm)"
  fi
else
  warn "custom type check skipped"
fi
echo ""

# --- 6. beads.role git config (warning 抑制用) ---
# v0.8.0: <repo>/.git/config に書く (bd init が repo の既存 git を共有)
echo "[6] git config beads.role"
if [ -n "$REPO_ROOT" ] && [ -d "$REPO_ROOT/.git" ]; then
  role="$(cd "$REPO_ROOT" && git config beads.role 2>/dev/null || echo "")"
  if [ "$role" = "maintainer" ]; then
    pass "beads.role=maintainer"
  else
    warn "beads.role not set (run: cd $REPO_ROOT && git config beads.role maintainer)"
  fi
else
  warn "$REPO_ROOT/.git not found"
fi
echo ""

# --- 7. AGENTS.md (optional、bd setup claude で生成可能) ---
echo "[7] AGENTS.md (optional)"
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/AGENTS.md" ]; then
  pass "AGENTS.md exists at $REPO_ROOT/AGENTS.md"
else
  warn "AGENTS.md not found (agent-org は --skip-agents で bd init するため optional。手動で 'bd setup claude' を実行すると生成される)"
fi
echo ""

# --- 8. .git/info/exclude に beads stealth 設定 (v0.8.0+ ADR-007 amendment) ---
echo "[8] .git/info/exclude (stealth mode、v0.8.0+)"
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/.git/info/exclude" ]; then
  if grep -q "^\.beads/" "$REPO_ROOT/.git/info/exclude" 2>/dev/null; then
    pass ".git/info/exclude excludes .beads/ (stealth mode active)"
  else
    warn ".git/info/exclude does not exclude .beads/ (run /org-init again or 'bd init --setup-exclude --stealth')"
  fi
else
  warn "$REPO_ROOT/.git/info/exclude not found (run /org-init)"
fi
# legacy .gitignore marker (v0.6.0〜0.7.x) の残存検出
if [ -n "$REPO_ROOT" ] && [ -f "$REPO_ROOT/.gitignore" ]; then
  if grep -q "agent-org plugin (v0\.[0-7]" "$REPO_ROOT/.gitignore" 2>/dev/null; then
    warn ".gitignore に v0.7.x 以前の agent-org marker が残存しています (v0.8.0 stealth では不要)。手動で削除可能"
  fi
fi
echo ""

# --- 9. open issues snapshot ---
echo "[9] open bd issues snapshot"
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  for t in detection fix approval; do
    count="$(cd "$REPO_ROOT" && bd list -t $t --status open --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)"
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
| WARN | 推奨設定が未適用だが起動は可能 / 旧 path 残存 | 余裕があるときに対処 |

## 典型シナリオ

### 新規プロジェクトで `/bd-check` を初実行した

最初は全項目が FAIL になる可能性が高い。順番に:

1. `brew install beads` → bd CLI 入手
2. 該当プロジェクト root (`<repo>/`) で `/org-init` → `<repo>/.beads/` 作成 +
   custom type 登録 + `.gitignore` 更新
3. もう一度 `/bd-check` → 全 PASS / WARN のみ

### v0.7.x プロジェクトを v0.8.0+ で開いた

`[3b] legacy beads path` が WARN になり `~/.beads/<proj-hash>/.beads/` の
残存が報告される。`/migrate-beads-to-repo-local` で `<repo>/.beads/` に統合する:

```text
/migrate-beads-to-repo-local
```

統合後 `/bd-check` を再実行して全 PASS / WARN のみであることを確認。

### `bd doctor` が FAIL になる

bd DB が破損している可能性。よくある原因:

- `<repo>/.beads/embeddeddolt/` を手動で削除した / mv した
- `<repo>/.git/` の状態が壊れた (`git status` で確認)

対処: 退避 (`mv <repo>/.beads <repo>/.beads.bak`) してから `/org-init` を
再実行、必要なら `bd import` で .bak から復旧。

### custom types が未登録で FAIL になる

`/org-init` 実行時に `bd config set types.custom` がスキップされた可能性。
手動で:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
(cd "$REPO_ROOT" && bd config set types.custom \
  "detection,fix,approval,episode,task")

# verify
(cd "$REPO_ROOT" && bd types | grep -E "^  (detection|fix|approval|episode|task)$")
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
- v0.8.0 から bd は `<repo>/.beads/` に repo-local 配置 (ADR-007)。
  `--bg` 隔離下でも bd は main repo の `.beads/` を共有する (git worktree-aware)

## 関連

- 初期化: `commands/org-init.md`
- watcher 起動 preflight: `commands/start-watcher.md`
- fixer 起動 preflight: `commands/fix-regression.md`
- migration: `commands/migrate-to-beads.md`, `commands/migrate-from-beads.md`,
  `commands/migrate-beads-to-repo-local.md` (v0.7.x→v0.8.0 path 移行)
- 設計判断: ADR-007 (`<repo>/.beads/` repo-local 配置採用)
- beads 公式: <https://github.com/steveyegge/beads>
