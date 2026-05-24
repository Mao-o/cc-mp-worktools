---
description: regression-fixer subagent を `--bg` + `/goal` で起動し、condition 達成まで自律修正ループを回す。foreground preflight (bd CLI / .beads / gh auth / git remote / branch 衝突) 必須。target 未指定時は bd ready から最優先 detection を自動選択。v0.6.0 から beads が hard dependency、v0.8.0 から `<repo>/.beads/` に repo-local (ADR-007、git worktree-aware)
---

# /fix-regression

`regression-fixer` subagent を background session (`--bg`) + `/goal` 駆動で
起動し、与えられた condition (例: 「CI green」「PR#42 の指摘解消」) まで
自律的に修正ループを回す。修正成果は git remote (push + gh pr) 経由で main
に戻す。

## 引数

```text
/fix-regression [target] [condition] [--turn-cap N]
```

| 引数 | 説明 |
|---|---|
| `[target]` (任意) | 修正対象。`PR#42` / `detection:<bd-issue-id>` (例: `detection:proj-abc123`) / `task:fix-auth-2026` / 自由記述。**未指定時は `bd ready -t detection --json` から priority 最高 (= 0 が先頭) の detection を自動選択** |
| `condition` (任意) | `/goal` の達成条件。省略時は target から自動推定 (e.g. PR なら「CI green + reviewer comments resolved」、detection なら「bd issue が closed」) |
| `--turn-cap N` (任意) | turn 上限。省略時は規模に応じて 25 (small) / 50 (medium) / 80 (large) のいずれかを採用 |

## 実行内容

1. **foreground preflight** を実行する (失敗したら `--bg` を起動せず、
   ユーザーにセットアップを促す)
2. preflight が通ったら **`/goal` condition を組み立てる**
   - 必ず `or stop after N turns` 句を含めて turn cap を設ける
   - target が PR なら CI / review 観点を含む condition を作る
3. `claude --agent agent-org:regression-fixer --bg '/goal <condition>'` を発射
4. 起動結果 (session id / agent view 上のラベル / 完了通知の見方) を
   ユーザーに通知

## Foreground preflight (必須)

`--bg` で起動された session は permission prompt を出せず auto-deny される。
事前に main session 内で前提条件を確認する。1 つでも失敗したら `--bg` 起動を
中止し、対処を案内する。

```bash
#!/usr/bin/env bash
# /fix-regression preflight (v0.6.0: bd hard dependency, v0.8.0: bd repo-local at <repo>/.beads/)
set -u

errors=()
warnings=()

# 1. bd CLI install 確認
if ! command -v bd >/dev/null 2>&1; then
  errors+=("bd CLI が見つかりません (Mac: 'brew install beads')")
fi

# 1b. jq install 確認 (bd ready --json | jq 等で必須。preflight 自身も
#     bd ready の解析に jq を使うため、未導入だと「no work」と誤判定する)
if ! command -v jq >/dev/null 2>&1; then
  errors+=("jq が見つかりません (Mac: 'brew install jq')")
fi

# 2. gh CLI install + auth
if ! command -v gh >/dev/null 2>&1; then
  errors+=("gh CLI が見つかりません (https://cli.github.com/)")
else
  if ! gh auth status >/dev/null 2>&1; then
    errors+=("gh CLI が未認証です。'gh auth login' を実行してください")
  fi
fi

# 3. git remote origin の存在
if ! git remote get-url origin >/dev/null 2>&1; then
  errors+=("git remote 'origin' が未設定です。fixer は push + gh pr で main に戻すため必須")
fi

# 4. claude CLI が利用可能か
if ! command -v claude >/dev/null 2>&1; then
  errors+=("claude CLI が見つかりません (--bg 起動に必須)")
fi

# 5. 作業ツリーがクリーンか
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  warnings+=("作業ツリーに未 commit の変更があります。fixer の修正と混ざる可能性")
fi

# 6. proj-hash 計算 (MEMORY.md project section + label prefix 用、v0.8.0 から bd path には不要)
proj_hash="$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
" 2>/dev/null || echo "")"

if [ -z "$proj_hash" ]; then
  errors+=("python3 で proj-hash 計算に失敗")
fi

# 7. <main_repo>/.beads/ 初期化済み確認 (v0.8.0 ADR-007: repo-local 配置)
#    worktree 内で実行された場合に備えて git common-dir 経由で main repo を解決
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$REPO_ROOT" ]; then
  errors+=("not in a git repository。v0.8.0 から bd は <repo>/.beads/ に配置されるため git repo 内での実行が必須")
  MAIN_REPO=""
  BEADS_DIR=""
else
  MAIN_REPO="$(cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
  [ -n "$MAIN_REPO" ] || MAIN_REPO="$REPO_ROOT"
  BEADS_DIR="$MAIN_REPO/.beads"
  if [ ! -d "$BEADS_DIR" ]; then
    errors+=("$BEADS_DIR が未初期化。main repo root ($MAIN_REPO) で '/org-init' を実行してください")
  fi
fi

# 7b. legacy path 検出 (v0.7.x 残骸警告)
if [ -n "$proj_hash" ] && [ -d "$HOME/.beads/$proj_hash/.beads" ]; then
  echo "info: ~/.beads/$proj_hash/.beads (v0.7.x legacy path) が残存しています。'/migrate-beads-to-repo-local' で <repo>/.beads/ に統合可能"
fi

# 8. bd doctor (DB の健全性確認)
#    v0.8.0: cd で bd 自動 resolve (worktree でも main repo .beads/ にアクセス)
if command -v bd >/dev/null 2>&1 && [ -d "$BEADS_DIR" ]; then
  if ! (cd "$REPO_ROOT" && bd doctor >/dev/null 2>&1); then
    errors+=("bd doctor が失敗。'(cd $REPO_ROOT && bd doctor)' を foreground で実行して診断")
  fi
fi

# 9. target 別の事前チェック
target="${1:-}"
case "$target" in
  "")
    # target 未指定: bd ready から自動選択可能か確認
    if [ -d "$BEADS_DIR" ]; then
      ready_count="$(cd "$REPO_ROOT" && bd ready -t detection --json 2>/dev/null | jq 'length' 2>/dev/null || echo 0)"
      if [ "$ready_count" = "0" ]; then
        warnings+=("bd ready -t detection に open issue がありません (no work to do)")
      else
        echo "info: bd ready -t detection に $ready_count 件の open issue があります (fixer が priority 最高を自動選択)"
      fi
    fi
    ;;
  PR#*|pr#*|pr:*|PR:*)
    pr_num="${target##*[#:]}"
    pr_branch="$(gh pr view "$pr_num" --json headRefName -q .headRefName 2>/dev/null || true)"
    if [ -n "$pr_branch" ]; then
      echo "info: PR #$pr_num は branch '$pr_branch' に push されます"
    else
      warnings+=("gh pr view $pr_num が失敗。PR が存在しないか権限不足")
    fi
    ;;
  detection:*)
    # detection:<bd-issue-id> 形式、bd で issue 存在確認
    bd_id="${target#detection:}"
    if [ -d "$BEADS_DIR" ] && [ -n "$bd_id" ]; then
      if ! (cd "$REPO_ROOT" && bd show "$bd_id" >/dev/null 2>&1); then
        errors+=("detection issue '$bd_id' が bd に見つかりません ('(cd $REPO_ROOT && bd list -t detection)' で確認)")
      fi
    fi
    # 新規 branch 衝突チェック
    suggested="fix/${target//[^a-zA-Z0-9-]/-}"
    if git ls-remote --heads origin "$suggested" 2>/dev/null | grep -q .; then
      warnings+=("提案 branch '$suggested' が既に origin に存在 (衝突回避が必要)")
    fi
    ;;
  task:*)
    suggested="fix/${target//[^a-zA-Z0-9-]/-}"
    if git ls-remote --heads origin "$suggested" 2>/dev/null | grep -q .; then
      warnings+=("提案 branch '$suggested' が既に origin に存在 (衝突回避が必要)")
    fi
    ;;
esac

# 10. gh repo view (リポジトリ疎通)
if command -v gh >/dev/null 2>&1; then
  if ! gh repo view >/dev/null 2>&1; then
    warnings+=("gh repo view が失敗。origin に gh アクセス権が無い可能性")
  fi
fi

# 11. memory dir 準備 (冪等)
mkdir -p ~/.claude/agent-memory/agent-org-regression-fixer 2>/dev/null || true

# 結果出力
if [ ${#warnings[@]} -gt 0 ]; then
  echo "warnings (継続可):"
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

warnings は表示するが起動を妨げない。errors は表示して `--bg` 起動を中止する。

## /goal condition の組み立て (preflight 通過後)

`<condition>` を引数から取得 (省略時は target ベースで自動生成)。**必ず**
`or stop after N turns` 句を末尾に追加する。

### target 別の condition 雛形

**PR を直す**:

```
/goal CI is green on PR#<n> and gh pr checks <n> shows all required checks PASS and gh pr view <n> shows mergeable=MERGEABLE, or stop after <N> turns
```

**detection:<bd-issue-id> を直す**:

```
/goal The failing test reported in bd detection issue <bd-issue-id> passes locally with the same bash command; the corresponding fix issue is created via `bd create -t fix` and `bd dep add <bd-issue-id> <fix-id>` (detection blocked-by fix); both issues are closed with `bd close` after PR is merged; or stop after <N> turns
```

**target 未指定 (bd ready から自動選択)**:

```
/goal Pick the highest-priority open issue from `bd ready -t detection --json`, claim it with `bd update --claim`, create a corresponding fix issue with `bd create -t fix`, fix the underlying problem, push to origin with a PR, then close both issues with `bd close`; or stop after <N> turns
```

**task を直す**:

```
/goal Task <task-id> is implemented per its description, tests pass via <command>, a PR is opened against <base-branch> with the changes, or stop after <N> turns
```

### turn-cap の決め方

`--turn-cap` で明示された値を使う。省略時の default:

- target が「test 1 つ修正」「typo 修正」のように小規模: **25**
- target が「機能 1 つ修正」「PR 全体」: **50**
- target が「大規模リファクタ」「設計改修」: **80**

上限 100 を超えない (それ以上必要なタスクは `--bg` 自律ループの範囲を超える
ため、main session で分割する)。

## --bg 起動 (preflight 通過後のみ)

condition が確定したら以下を Bash で発射する:

```bash
claude --agent agent-org:regression-fixer --bg "/goal ${condition}"
```

**重要**:

1. `--agent` には **scoped name** (`agent-org:regression-fixer`) を渡す
2. `/goal` の引数は **1 行**で渡す (`stop after N turns` 等の条件を改行で
   分けると評価器が文を分断する。`;` や ` and ` で 1 行に収める)
3. condition 文字列内のシングルクォート / ダブルクォート escape に注意
   (`gh` 等の引用が必要なら `\"`)

## 起動後の確認

`--bg` 起動が成功すると別 supervisor process が立ち上がる。

```bash
# 起動中の background session 一覧
claude agents

# fixer が作成した fix issue を bd 上で確認 (完了時に description が確定)
# v0.8.0: cd <repo> で bd 自動 resolve
REPO_ROOT="$(git rev-parse --show-toplevel)"
(cd "$REPO_ROOT" && bd list -t fix --status open --json) | jq
(cd "$REPO_ROOT" && bd list -t fix --status closed --json) | jq
# 個別確認
(cd "$REPO_ROOT" && bd show <fix-id>)

# PR の状況確認
gh pr view <PR#>
gh pr checks <PR#>
```

`bd show <fix-id>` の description に格納された `goal_status` を見る:

- `achieved`: 修正完了、`pr_url` で内容確認
- `turn_limit`: turn cap で停止、未完了。`gh pr view` で進捗確認 + 再投入
- `error`: secret / 仕様判断 / 大規模変更等で中断。`notes` を確認

error 終了した fix は `outcome:error` label が付くため、まとめて確認するには:

```bash
(cd "$REPO_ROOT" && bd list -t fix -l outcome:error --json) | jq
```

## 完了 report の learnings_to_persist 回収 (Phase 7+、v0.10.0)

fixer は完了 description body (v0.5.x 互換 schema、`commits` / `pr_url` /
`goal_status` 等を含む) とは別に、**会話出力 YAML として
`learnings_to_persist:` セクション**を返す (詳細は
`agents/regression-fixer.md` の同名 section)。これは「次の fixer session で
再利用したい修正パターン」のリストで、handler 経由で `bd remember` 永続化する
(`/run-review` と同 pattern、ADR-010 で 4 subagent に展開された経路の 1 つ)。

main session が PR 確認時 (`gh pr view <URL>`) または fix close 確認時に、
以下の手順で回収して `bd remember` で永続化する:

```bash
REPO_ROOT="$(git rev-parse --show-toplevel)"

# 最新の closed fix を取得 (もしくは特定 fix_id を直接指定)
FIX_ID="$(cd "$REPO_ROOT" && bd list -t fix --status closed -l agent-org --json \
  | jq -r 'sort_by(.closed_at) | reverse | .[0].id')"

# fixer の会話出力に含まれる learnings_to_persist (YAML) から各行を抽出して
# bd remember で永続化する。description body は v0.5.x 互換 schema 維持の
# ため learnings は本文に含めない設計 (会話出力経由で main session が回収):
(cd "$REPO_ROOT" && bd remember "fix-pattern: JSONL parse fallback で EOF 改行欠落を救済" \
  --key fix-pattern-jsonl-parse-eof 2>/dev/null) || true
(cd "$REPO_ROOT" && bd remember "fix-pattern: Closes #<n> 形式の commit message を強制" \
  --key fix-pattern-commit-style-closes 2>/dev/null) || true
```

- **key prefix は `fix-pattern-` 固定** (ADR-010 規約、`consulting-memory`
  skill の「key 命名規約」table 参照)。同 key 再 invoke で update in place
- **失敗許容**: `bd remember` 未サポート / 一時 error でも PR レビュー本体は
  止めない (`|| true`、curate は best-effort、`/run-review` reviewer 学習と
  同方針)
- **横断 retrieval**: `bd memories fix-pattern` で list、`bd recall <key>` で
  個別 fetch (詳細は `consulting-memory` skill)
- **無効化**: `bd forget <key>` で明示削除 (古い fix-pattern が
  `bd memories` 検索結果を埋める時のみ、急がない)
- **auto-inject**: `bd prime` の default 挙動で memory は次セッションに
  inject される (`using-beads` skill 参照)。次 fixer 起動時に過去の
  fix-pattern が自動で context に到達する

watcher / decision-keeper との分業:

| subagent | 経路 | 理由 |
|---|---|---|
| `regression-fixer` (本 command) | handler 経由 (main session が会話 YAML 取出) | 完了が単発 launch、`run-review` と同 pattern |
| `regression-watcher` | subagent prompt 内 Bash 直接 invoke | `--bg` 常駐性質、handler 不在 |
| `architect-reviewer` (`/run-review`) | handler 経由 (`/run-review` step 7) | 同期 launch (foreground reviewer spawn) |
| `decision-keeper` (`recording-decision`) | handler 経由 (skill 末尾) | 同期 launch (Task tool 経由) |

## preflight 失敗時のユーザー案内テンプレ

| 失敗内容 | 対処 |
|---|---|
| `bd CLI が見つかりません` | Mac: `brew install beads`、他は <https://github.com/steveyegge/beads> 参照 |
| `jq が見つかりません` | Mac: `brew install jq` |
| `<repo>/.beads が未初期化` | project root で `/org-init` を実行 (v0.8.0: ADR-007、repo-local 配置) |
| `bd doctor が失敗` | `(cd <repo> && bd doctor)` を foreground で実行して診断 |
| `not in a git repository` | v0.8.0 から bd は `<repo>/.beads/` 配置のため git repo 内での起動が必須 |
| `detection issue '<id>' が bd に見つかりません` | `(cd <repo> && bd list -t detection)` で実在 ID を確認、または `bd ready -t detection` で別 target を選択 |
| `gh CLI が見つかりません` | <https://cli.github.com/> から install (`brew install gh`) |
| `gh CLI が未認証です` | `! gh auth login` (foreground で実行) |
| `git remote 'origin' が未設定です` | `git remote add origin <url>` |
| `claude CLI が見つかりません` | claude code install / PATH 確認 |
| `python3 で proj-hash 計算に失敗` | python3 install |

warnings は対処推奨だが起動可能:

| 警告 | 対処 |
|---|---|
| `作業ツリーに未 commit の変更があります` | `git stash` または commit してから再実行 |
| `提案 branch が既に origin に存在` | 別 target 名を指定するか fixer が自動で suffix 付き branch にする |
| `gh repo view が失敗` | origin の権限を確認、または `--bg` 起動を中止して foreground で `gh repo view` を試す |

## 値や秘密の扱い

- preflight bash 内で `gh auth status` 等を実行する際、token 値そのものを
  echo / 表示しない (gh コマンドは標準で token を隠す)
- `/goal condition` 文字列内に secret を埋め込まない (履歴に残るため)
- fixer が `--bg` 内で env 値を必要とする場合、その env を起動 shell 経由で
  渡す形にする (`condition` には書かない)

## 関連

- subagent: `agents/regression-fixer.md`
- 検出元: `agents/regression-watcher.md` + `commands/start-watcher.md`
- 連携 hook: `hooks/post-commit-trigger.sh`
- 公式 docs:
  - `/goal`: <https://code.claude.com/docs/en/goal>
  - agent view / `--bg`: <https://code.claude.com/docs/en/agent-view>
