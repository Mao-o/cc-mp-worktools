---
name: starting-watcher
description: |
  regression-watcher subagent を `--bg` + `/loop` で起動して定期 smoke check
  を開始するスキル。foreground preflight から `--bg` 起動まで skill 内で完結
  する (v0.11.0, v2 skill 統合)。
  Use proactively when: プロジェクトの regression 監視を開始したい時、
  ユーザーが「監視して」「CI を見張って」「定期チェックして」と言った時。
  Triggers: starting-watcher, /start-watcher, start watcher,
  regression 監視, smoke check, 定期チェック, CI 監視,
  監視を開始, watcher 起動, 見張って
---

# Starting Watcher Skill

`regression-watcher` subagent を background session (`--bg`) + `/loop` 駆動で
起動し、プロジェクトの定期 smoke check を開始する。preflight から起動通知まで
本 skill 内で完結する。

## 起動条件

以下のいずれかが該当する時:

- プロジェクトの regression を定期監視したい
- CI / テストの状態を継続的にチェックしたい
- ユーザーが watcher の起動を依頼した

逆に以下では起動しない:

- 既に watcher が起動中 (`claude agents` で確認)
- 単発のテスト実行 (直接 Bash で実行する方が速い)
- bd / gh / git remote が使えない環境 (preflight で弾く)

## 引数

`$ARGUMENTS` から以下をパースする:

| 引数 | 説明 |
|---|---|
| `[interval]` (任意) | `/loop` の間隔。`30m` (default), `5m`, `1h`, `dynamic` 等 |

## 手順

### 1. Foreground preflight を実行する

`--bg` session は permission prompt を出せず auto-deny される。事前に
main session で前提条件を確認する。**1 つでも error があれば `--bg` を起動
しない**。

```bash
#!/usr/bin/env bash
set -u

errors=()

# 1. bd CLI
if ! command -v bd >/dev/null 2>&1; then
  errors+=("bd CLI が見つかりません (Mac: 'brew install beads')")
fi

# 1b. jq
if ! command -v jq >/dev/null 2>&1; then
  errors+=("jq が見つかりません (Mac: 'brew install jq')")
fi

# 2. gh CLI + auth
if ! command -v gh >/dev/null 2>&1; then
  errors+=("gh CLI が見つかりません (https://cli.github.com/)")
else
  if ! gh auth status >/dev/null 2>&1; then
    errors+=("gh CLI が未認証です。'gh auth login' を実行してください")
  fi
fi

# 3. git remote origin
if ! git remote get-url origin >/dev/null 2>&1; then
  errors+=("git remote 'origin' が未設定です")
fi

# 4. claude CLI
if ! command -v claude >/dev/null 2>&1; then
  errors+=("claude CLI が見つかりません")
fi

# 5. proj-hash
proj_hash="$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
" 2>/dev/null || echo "")"

if [ -z "$proj_hash" ]; then
  errors+=("python3 で proj-hash 計算に失敗")
fi

# 6. <repo>/.beads/ 初期化済み確認 (v0.8.0 ADR-007)
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$REPO_ROOT" ]; then
  errors+=("git repository 外です")
  BEADS_DIR=""
else
  MAIN_REPO="$(cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
  [ -n "$MAIN_REPO" ] || MAIN_REPO="$REPO_ROOT"
  BEADS_DIR="$MAIN_REPO/.beads"
  if [ ! -d "$BEADS_DIR" ]; then
    errors+=("$BEADS_DIR が未初期化。'/org-init' を実行してください")
  fi
fi

# 6b. legacy path 警告
if [ -n "$proj_hash" ] && [ -d "$HOME/.beads/$proj_hash/.beads" ]; then
  echo "info: legacy path ~/.beads/$proj_hash/.beads が残存。'/migrate-beads-to-repo-local' で統合可能" >&2
fi

# 7. bd doctor
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  if ! (cd "$REPO_ROOT" && bd doctor >/dev/null 2>&1); then
    errors+=("bd doctor が失敗。'(cd $REPO_ROOT && bd doctor)' で診断")
  fi
fi

# 8. memory dir 準備
mkdir -p ~/.claude/agent-memory/agent-org-regression-watcher 2>/dev/null || true

# 結果
if [ ${#errors[@]} -gt 0 ]; then
  echo "preflight FAILED:"
  for e in "${errors[@]}"; do echo "  - $e"; done
  exit 1
fi

echo "preflight OK: proj-hash=$proj_hash, bd=$BEADS_DIR"
exit 0
```

preflight が失敗したらユーザーに対処を案内して終了する。

### 2. `--bg` session を起動する

```bash
interval="${interval:-30m}"
claude --agent agent-org:regression-watcher --bg "/loop ${interval} smoke check"
```

- `--agent` には scoped name `agent-org:regression-watcher` を渡す
- scoped name なしだとデフォルト session に fallback する罠がある (ADR-003)

### 3. 起動結果をユーザーに通知する

- session id / agent view ラベル
- `claude agents` で一覧確認可能であること
- 停止方法: `claude agents` から terminate、またはアイドル 1 時間で自動停止
- `claude respawn --all` で復元可能
- 各 iteration で watcher が `bd create -t detection` で bd issue を作成すること

### 4. detection の確認方法を案内する

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
(cd "$REPO_ROOT" && bd list -t detection --status open --json) | jq
# または
(cd "$REPO_ROOT" && bd ready -t detection)
```

detection が見つかったら `fixing-regression` skill で修正を起動できる。

## Preflight 失敗時の対処

| 失敗 | 対処 |
|---|---|
| bd CLI 未 install | Mac: `brew install beads` |
| jq 未 install | Mac: `brew install jq` |
| `<repo>/.beads` 未初期化 | `/org-init` を実行 |
| bd doctor 失敗 | `(cd <repo> && bd doctor)` で診断 |
| git repo 外 | git repo 内で実行 |
| gh CLI 未認証 | `! gh auth login` |
| git remote 未設定 | `git remote add origin <url>` |

## 関連

- subagent: `agents/regression-watcher.md`
- 修正: `skills/fixing-regression/SKILL.md`
- subagent 定義: `agents/regression-watcher.md`
- 連携 hook: `hooks/post-commit-trigger.sh`
