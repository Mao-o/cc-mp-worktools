---
description: regression-fixer subagent を `--bg` + `/goal` で起動し、condition 達成まで自律修正ループを回す。foreground preflight (gh auth / git remote / branch 衝突) 必須。failure 時は --bg を起動せずセットアップ手順を案内
---

# /fix-regression

`regression-fixer` subagent を background session (`--bg`) + `/goal` 駆動で
起動し、与えられた condition (例: 「CI green」「PR#42 の指摘解消」) まで
自律的に修正ループを回す。修正成果は git remote (push + gh pr) 経由で main
に戻す。

## 引数

```text
/fix-regression <target> [condition] [--turn-cap N]
```

| 引数 | 説明 |
|---|---|
| `<target>` (必須) | 修正対象。`PR#42` / `detection:detection-2026-05-18T03Z` / `task:fix-auth-2026` / 自由記述 |
| `condition` (任意) | `/goal` の達成条件。省略時は target から自動推定 (e.g. PR なら「CI green + reviewer comments resolved」) |
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
# /fix-regression preflight
set -u

errors=()
warnings=()

# 1. gh CLI install + auth
if ! command -v gh >/dev/null 2>&1; then
  errors+=("gh CLI が見つかりません (https://cli.github.com/)")
else
  if ! gh auth status >/dev/null 2>&1; then
    errors+=("gh CLI が未認証です。`gh auth login` を実行してください")
  fi
fi

# 2. git remote origin の存在
if ! git remote get-url origin >/dev/null 2>&1; then
  errors+=("git remote 'origin' が未設定です。fixer は push + gh pr で main に戻すため必須")
fi

# 3. claude CLI が利用可能か
if ! command -v claude >/dev/null 2>&1; then
  errors+=("claude CLI が見つかりません (--bg 起動に必須)")
fi

# 4. 作業ツリーがクリーンか
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  warnings+=("作業ツリーに未 commit の変更があります。fixer の修正と混ざる可能性")
fi

# 5. branch 衝突チェック (target が PR# の場合)
target="${1:-}"
case "$target" in
  PR#*|pr#*|pr:*|PR:*)
    pr_num="${target##*[#:]}"
    pr_branch="$(gh pr view "$pr_num" --json headRefName -q .headRefName 2>/dev/null || true)"
    if [ -n "$pr_branch" ]; then
      # 既存 PR branch に fixer が追加 push する想定 (衝突 OK)
      echo "info: PR #$pr_num は branch '$pr_branch' に push されます"
    else
      warnings+=("gh pr view $pr_num が失敗。PR が存在しないか権限不足")
    fi
    ;;
  detection:*|task:*)
    # 新規 branch を fixer が作成する想定
    suggested="fix/${target//[^a-zA-Z0-9-]/-}"
    if git ls-remote --heads origin "$suggested" 2>/dev/null | grep -q .; then
      warnings+=("提案 branch '$suggested' が既に origin に存在 (衝突回避が必要)")
    fi
    ;;
esac

# 6. gh repo view (リポジトリ疎通)
if command -v gh >/dev/null 2>&1; then
  if ! gh repo view >/dev/null 2>&1; then
    warnings+=("gh repo view が失敗。origin に gh アクセス権が無い可能性")
  fi
fi

# 7. proj-hash 計算
proj_hash="$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
" 2>/dev/null || echo "")"

if [ -z "$proj_hash" ]; then
  errors+=("python3 で proj-hash 計算に失敗")
fi

# 8. state dir 準備 (冪等)
mkdir -p ~/.claude/agent-org/state/"$proj_hash"/fixes \
         ~/.claude/agent-memory/agent-org-regression-fixer 2>/dev/null || true

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

echo "preflight OK: proj-hash=$proj_hash"
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

**detection-id を直す**:

```
/goal The failing test reported in detection <detection-id> passes locally with the same bash command, ~/.claude/agent-org/state/<proj-hash>/fixes/<fix-id>.json is written with goal_status=achieved, or stop after <N> turns
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

# fixer が書き出した state file (完了時に出る)
ls -la ~/.claude/agent-org/state/<proj-hash>/fixes/

# PR の状況確認
gh pr view <PR#>
gh pr checks <PR#>
```

`fixes/<fix-id>.json` の `goal_status` を見る:

- `achieved`: 修正完了、`pr_url` で内容確認
- `turn_limit`: turn cap で停止、未完了。`gh pr view` で進捗確認 + 再投入
- `error`: secret / 仕様判断 / 大規模変更等で中断。`notes` を確認

## preflight 失敗時のユーザー案内テンプレ

| 失敗内容 | 対処 |
|---|---|
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
