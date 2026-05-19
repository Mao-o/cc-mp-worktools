---
description: regression-watcher subagent を `--bg` + `/loop` で起動して定期 smoke check を開始する。foreground preflight (bd CLI / bd doctor / gh auth / git remote / claude CLI) を実行してから claude --agent agent-org:regression-watcher --bg を発射。v0.6.0 から beads が hard dependency
---

# /start-watcher

`regression-watcher` subagent を background session (`--bg`) + `/loop` 駆動で
起動し、プロジェクトの定期 smoke check を開始する。

## 引数

```text
/start-watcher [interval]
```

| 引数 | 説明 |
|---|---|
| `interval` (任意) | `/loop` の間隔。`30m` (default), `5m`, `1h`, `dynamic` 等。`/loop` 仕様に従う形式 |

## 実行内容

1. **foreground preflight** を実行する (失敗したら `--bg` を起動せず、
   ユーザーにセットアップを促す)
2. preflight が通ったら `claude --agent agent-org:regression-watcher --bg
   "/loop <interval> smoke check"` を発射する
3. 起動結果 (session id / agent view 上のラベル / 停止方法) をユーザーに通知

## Foreground preflight (必須)

以下を直接 Bash で実行し、**全て成功**してから `--bg` 起動に進む。1 つでも
失敗したら起動を中止し、失敗内容と対処手順を表示する。

```bash
#!/usr/bin/env bash
# /start-watcher preflight (v0.6.0: bd hard dependency)
set -u

errors=()

# 1. bd CLI install 確認 (v0.6.0: hard dependency)
if ! command -v bd >/dev/null 2>&1; then
  errors+=("bd CLI が見つかりません (Mac: 'brew install beads')")
fi

# 1b. jq install 確認 (watcher は bd list --json | jq で false-positive guard、
#     bd create の戻り解析にも依存する)
if ! command -v jq >/dev/null 2>&1; then
  errors+=("jq が見つかりません (Mac: 'brew install jq')")
fi

# 2. gh CLI が install 済みか + auth 済みか
if ! command -v gh >/dev/null 2>&1; then
  errors+=("gh CLI が見つかりません。インストール: https://cli.github.com/")
else
  if ! gh auth status >/dev/null 2>&1; then
    errors+=("gh CLI が未認証です。'gh auth login' を実行してください")
  fi
fi

# 3. git remote origin が設定されているか
if ! git remote get-url origin >/dev/null 2>&1; then
  errors+=("git remote 'origin' が未設定です。'git remote add origin <url>' で設定してください")
fi

# 4. claude CLI が利用可能か
if ! command -v claude >/dev/null 2>&1; then
  errors+=("claude CLI が見つかりません (--bg 起動に必須)")
fi

# 5. proj-hash を計算 (bd dir 確認 + state dir 共通)
proj_hash="$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
" 2>/dev/null || echo "")"

if [ -z "$proj_hash" ]; then
  errors+=("python3 で proj-hash 計算に失敗 (python3 が必要)")
fi

# 6. ~/.beads/<proj-hash>/.beads/ 初期化済み確認 (bd hard dependency)
BEADS_PARENT="$HOME/.beads/$proj_hash"
BEADS_DIR="$BEADS_PARENT/.beads"
if [ -n "$proj_hash" ] && [ ! -d "$BEADS_DIR" ]; then
  errors+=("$BEADS_DIR が未初期化。project root で '/org-init' を実行してください")
fi

# 7. bd doctor (DB の健全性確認、preflight 段階で異常検出)
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  if ! BEADS_DIR="$BEADS_DIR" bd doctor >/dev/null 2>&1; then
    errors+=("bd doctor が失敗。'BEADS_DIR=$BEADS_DIR bd doctor' を foreground で実行して診断")
  fi
fi

# 8. memory dir 準備 (冪等。state dir は v0.6.0 から bd に移行したため不要)
mkdir -p ~/.claude/agent-memory/agent-org-regression-watcher 2>/dev/null || true

# 結果出力
if [ ${#errors[@]} -gt 0 ]; then
  echo "preflight FAILED:"
  for e in "${errors[@]}"; do echo "  - $e"; done
  exit 1
fi

echo "preflight OK: proj-hash=$proj_hash, bd=$BEADS_DIR, gh authed, claude CLI present"
exit 0
```

`gh` は smoke check で `gh run list` / `gh pr view` 等が必要になるシナリオ
を見据えて preflight 段階で確認する (watcher 自体が gh を使わない構成でも、
fixer に渡せる情報を増やすために確認しておく)。

## --bg 起動 (preflight 通過後のみ)

preflight が通ったら以下を Bash で実行する:

```bash
interval="${1:-30m}"

claude --agent agent-org:regression-watcher --bg \
  "/loop ${interval} smoke check"
```

**重要**: `--agent` には **scoped name** (`agent-org:regression-watcher`) を
渡す。`regression-watcher` 単独だと plugin agent が解決されず、デフォルト
session として fallback 起動する罠がある (ADR-002 から ADR-003 で確認済)。

## 起動後の確認

`--bg` 起動が成功すると、別 supervisor process が立ち上がる。確認方法:

```bash
# 起動中の background session 一覧
claude agents

# 特定 session を foreground に戻す (確認・停止用)
# (claude agents から session id を取得して attach)
```

各 iteration で watcher が `bd create -t detection` で bd issue を作成する
(v0.6.0 から旧 YAML 形式は廃止)。検出があれば main session で:

```bash
PROJ_HASH=<preflight で表示された値>
BEADS_DIR=~/.beads/$PROJ_HASH/.beads bd list -t detection --status open --json | jq
# または
BEADS_DIR=~/.beads/$PROJ_HASH/.beads bd ready -t detection
```

で確認できる。

## preflight 失敗時のユーザー案内テンプレ

preflight bash script が `exit 1` で終わった場合、表示された errors[] を
そのままユーザーに見せた上で、典型対処を案内する:

| 失敗内容 | 対処 |
|---|---|
| `bd CLI が見つかりません` | Mac: `brew install beads`、他は <https://github.com/steveyegge/beads> 参照 |
| `jq が見つかりません` | Mac: `brew install jq` |
| `~/.beads/<proj-hash>/.beads が未初期化` | project root で `/org-init` を実行 |
| `bd doctor が失敗` | `BEADS_DIR=~/.beads/<proj-hash>/.beads bd doctor` を foreground で実行して診断 |
| `gh CLI が見つかりません` | <https://cli.github.com/> から install (Mac: `brew install gh`) |
| `gh CLI が未認証です` | `gh auth login` をユーザー自身が foreground で実行 (`! gh auth login` を案内) |
| `git remote 'origin' が未設定です` | `git remote add origin <git URL>` (PR 機能を将来使うために必要、現 watcher 用途では fixer 起動時にも preflight があるためここで止めている) |
| `claude CLI が見つかりません` | claude code を install / `PATH` を確認 |
| `python3 で proj-hash 計算に失敗` | python3 install (state dir 分離 hash 計算に必要) |

ユーザーが対処後、もう一度 `/start-watcher [interval]` を実行する。

## 停止方法

```bash
# claude agents から session を選んで kill
claude agents
# UI から該当 watcher session を terminate
```

または、background session は machine sleep / shutdown / アイドル 1 時間で
自動停止する (公式 docs: agent-view)。再開は `claude respawn --all` で
復元できる。

## 値や秘密の扱い

`--bg` 起動した watcher session は permission prompt を出せない (auto-deny)。
watcher の tool allowlist (`Read,Bash,Grep,Glob`) と `memory: user` の組合せ
で安全に運用する設計。秘密を含む環境変数を `bash` 内で参照する場合は、
プロジェクト側 `.env` 等を直接読まず、git で管理されている設定のみを参照
する規律にする (regression-watcher.md の prompt 側で明文化済)。

## 関連

- subagent: `agents/regression-watcher.md`
- 連携 hook: `hooks/post-commit-trigger.sh`
  (last-commit.json を更新、watcher が次 loop で読む)
- 修正者: `commands/fix-regression.md` + `agents/regression-fixer.md`
- 公式 docs:
  - agent view / `--bg`: <https://code.claude.com/docs/en/agent-view>
  - `/loop`: <https://code.claude.com/docs/en/scheduled-tasks>
