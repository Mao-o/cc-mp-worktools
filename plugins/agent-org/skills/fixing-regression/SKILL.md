---
name: fixing-regression
description: |
  regression-fixer subagent を `--bg` + `/goal` で起動し、condition 達成まで
  自律修正ループを回すスキル。foreground preflight から `--bg` 起動、完了後の
  learnings 永続化まで skill 内で完結する (v0.11.0, v2 skill 統合)。
  bd 未設定環境では persist を best-effort skip。
  Use proactively when: CI failure / test failure / regression が検出された時、
  ユーザーが「テストが落ちている」「CI が赤い」「修正して」と言った時。
  Triggers: fixing-regression, /fix-regression, regression fix,
  テスト失敗, CI 失敗, regression 修正, fix regression, fix CI,
  テストが落ちている, CI が赤い, 壊れている
---

# Fixing Regression Skill

`regression-fixer` subagent を background session (`--bg`) + `/goal` 駆動で
起動し、condition 達成まで自律修正ループを回す。修正成果は git remote
(push + gh pr) 経由で main に戻す。preflight から learnings 永続化まで
本 skill 内で完結する。

## 起動条件

以下のいずれかが該当する時:

- watcher が detection を報告した (bd ready -t detection に open issue)
- CI / テストが失敗して修正が必要
- ユーザーが regression / 失敗の修正を依頼した
- PR のレビュー指摘を自律修正したい

逆に以下では起動しない:

- 新機能の実装 (fixer は「壊れたものを直す」専用)
- main session で手動修正する方が速い小規模修正 (typo 等)
- bd / gh / git remote が使えない環境 (preflight で弾く)

## 引数

`$ARGUMENTS` から以下をパースする:

| 引数 | 説明 |
|---|---|
| `[target]` (任意) | `PR#42` / `detection:<bd-issue-id>` / `task:<id>` / 自由記述。未指定時は `bd ready -t detection --json` から priority 最高を自動選択 |
| `[condition]` (任意) | `/goal` の達成条件。省略時は target から自動推定 |
| `[--turn-cap N]` (任意) | turn 上限。省略時は規模に応じて 25/50/80 |

## Phase 1: Preflight + Launch

### 1. Foreground preflight を実行する

`--bg` session は permission prompt を出せず auto-deny される。事前に
main session で前提条件を確認する。**1 つでも error があれば `--bg` を起動
しない**。

```bash
#!/usr/bin/env bash
set -u

errors=()
warnings=()

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

# 5. 作業ツリー
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  warnings+=("作業ツリーに未 commit の変更があります")
fi

# 6. proj-hash
proj_hash="$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
" 2>/dev/null || echo "")"

if [ -z "$proj_hash" ]; then
  errors+=("python3 で proj-hash 計算に失敗")
fi

# 7. <repo>/.beads/ 初期化済み確認 (v0.8.0 ADR-007)
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

# 7b. legacy path 警告
if [ -n "$proj_hash" ] && [ -d "$HOME/.beads/$proj_hash/.beads" ]; then
  echo "info: legacy path ~/.beads/$proj_hash/.beads が残存。'/migrate-beads-to-repo-local' で統合可能"
fi

# 8. bd doctor
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  if ! (cd "$REPO_ROOT" && bd doctor >/dev/null 2>&1); then
    errors+=("bd doctor が失敗。'(cd $REPO_ROOT && bd doctor)' で診断")
  fi
fi

# 9. target 別チェック
target="${1:-}"
case "$target" in
  "")
    if [ -d "$BEADS_DIR" ]; then
      ready_count="$(cd "$REPO_ROOT" && bd ready -t detection --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)"
      if [ "$ready_count" = "0" ]; then
        warnings+=("bd ready -t detection に open issue がありません")
      else
        echo "info: $ready_count 件の open detection"
      fi
    fi
    ;;
  PR#*|pr#*|pr:*|PR:*)
    pr_num="${target##*[#:]}"
    pr_branch="$(gh pr view "$pr_num" --json headRefName -q .headRefName 2>/dev/null || true)"
    if [ -n "$pr_branch" ]; then
      echo "info: PR #$pr_num → branch '$pr_branch'"
    else
      warnings+=("gh pr view $pr_num が失敗")
    fi
    ;;
  detection:*)
    bd_id="${target#detection:}"
    if [ -d "$BEADS_DIR" ] && [ -n "$bd_id" ]; then
      if ! (cd "$REPO_ROOT" && bd show "$bd_id" >/dev/null 2>&1); then
        errors+=("detection '$bd_id' が bd に見つかりません")
      fi
    fi
    suggested="fix/${target//[^a-zA-Z0-9-]/-}"
    if git ls-remote --heads origin "$suggested" 2>/dev/null | grep -q .; then
      warnings+=("branch '$suggested' が origin に既存")
    fi
    ;;
  task:*)
    suggested="fix/${target//[^a-zA-Z0-9-]/-}"
    if git ls-remote --heads origin "$suggested" 2>/dev/null | grep -q .; then
      warnings+=("branch '$suggested' が origin に既存")
    fi
    ;;
esac

# 10. gh repo view
if command -v gh >/dev/null 2>&1; then
  if ! gh repo view >/dev/null 2>&1; then
    warnings+=("gh repo view が失敗。origin 権限を確認")
  fi
fi

# 11. memory dir 準備
mkdir -p ~/.claude/agent-memory/agent-org-regression-fixer 2>/dev/null || true

# 結果
if [ ${#warnings[@]} -gt 0 ]; then
  echo "warnings:"
  for w in "${warnings[@]}"; do echo "  - $w"; done
fi

if [ ${#errors[@]} -gt 0 ]; then
  echo "preflight FAILED:"
  for e in "${errors[@]}"; do echo "  - $e"; done
  exit 1
fi

echo "preflight OK: proj-hash=$proj_hash, bd=$BEADS_DIR"
exit 0
```

preflight が失敗したらユーザーに対処を案内して終了する。

### 2. /goal condition を組み立てる

condition を引数から取得 (省略時は target ベースで自動生成)。
**必ず** `or stop after N turns` 句を末尾に追加する。

target 別の condition 雛形:

| target | condition 雛形 |
|---|---|
| `PR#<n>` | `CI is green on PR#<n> and gh pr checks <n> shows all required checks PASS and gh pr view <n> shows mergeable=MERGEABLE, or stop after <N> turns` |
| `detection:<id>` | `The failing test reported in bd detection issue <id> passes locally; fix issue created via bd create -t fix + bd dep add; both closed after PR merged; or stop after <N> turns` |
| 未指定 (auto) | `Pick highest-priority open issue from bd ready -t detection --json, claim with bd update --claim, create fix issue, fix problem, push PR, close both issues; or stop after <N> turns` |
| `task:<id>` | `Task <id> implemented per description, tests pass, PR opened; or stop after <N> turns` |

turn-cap default: 小規模=25 / 中規模=50 / 大規模=80。上限 100。

### 3. `--bg` session を起動する

```bash
claude --agent agent-org:regression-fixer --bg "/goal ${condition}"
```

- `--agent` には scoped name `agent-org:regression-fixer` を渡す
- `/goal` 引数は 1 行 (改行で分けない)
- condition 内に secret を埋め込まない

### 4. 起動結果をユーザーに通知する

- session id / agent view ラベル / 停止方法
- `claude agents` で一覧確認可能であること
- 完了時に Phase 2 (learnings 回収) が必要であること

## Phase 2: 完了後の確認と Learnings 永続化

fixer の `--bg` session が完了した後、main session で以下を実行する。

### 5. fix 結果を確認する

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
# fix issue の確認
(cd "$REPO_ROOT" && bd list -t fix --status closed --json) | jq
# または特定 fix
(cd "$REPO_ROOT" && bd show <fix-id>)
```

`goal_status` の判定:

| status | 意味 | 次のアクション |
|---|---|---|
| `achieved` | 修正完了 | PR を確認、learnings 回収 |
| `turn_limit` | turn cap で停止 | PR 進捗確認、再投入を検討 |
| `error` | 中断 | notes 確認、手動対応 |

### 6. Learnings を永続化する (best-effort)

fixer が会話出力 YAML に `learnings_to_persist:` を含めた場合、
`bd remember` で永続化する。失敗しても fix 確認は止めない。

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
# 例: fixer が返した learning を永続化
(cd "$REPO_ROOT" && bd remember "fix-pattern: <summary>" \
  --key fix-pattern-<slug> 2>/dev/null) || true
```

- key prefix は `fix-pattern-` 固定 (ADR-010 規約)
- 同 key 再 invoke で update in place
- `bd memories fix-pattern` で一覧、`bd recall <key>` で個別取得
- `bd forget <key>` で明示削除
- `bd prime` の default 挙動で次セッションに auto-inject

bd 未設定環境では persist skip し、fix 結果の通知のみ返す。

### 7. ユーザーに結果を通知する

- fix status / PR URL / concern があれば表示
- learnings 永続化の成否
- error 終了の場合は `(cd $REPO_ROOT && bd list -t fix -l outcome:error --json) | jq` で一括確認を案内

## Preflight 失敗時の対処

| 失敗 | 対処 |
|---|---|
| bd CLI 未 install | Mac: `brew install beads` |
| jq 未 install | Mac: `brew install jq` |
| `<repo>/.beads` 未初期化 | `/org-init` を実行 |
| bd doctor 失敗 | `(cd <repo> && bd doctor)` で診断 |
| git repo 外 | git repo 内で実行 |
| detection 未発見 | `(cd <repo> && bd list -t detection)` で確認 |
| gh CLI 未認証 | `! gh auth login` |
| git remote 未設定 | `git remote add origin <url>` |

## 関連

- subagent: `agents/regression-fixer.md`
- 検出元: `agents/regression-watcher.md` + `skills/starting-watcher/SKILL.md`
- subagent 定義: `agents/regression-fixer.md`
- 横断参照: `skills/consulting-memory/SKILL.md`
