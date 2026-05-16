---
description: agent-org plugin が使う state ディレクトリ群を初期化する (.claude/agent-memory/, .claude/episodes/, .claude/agent-org/approvals/, ~/.claude/agent-org/state/<proj-hash>/)
---

# /org-init

agent-org plugin が使う以下のディレクトリを冪等に作成する。

## 作成対象

repo 内 (`memory: project` 系):

- `.claude/agent-memory/decision-keeper/`
- `.claude/agent-memory/architect-reviewer/`
- `.claude/agent-memory/context-compressor/`
- `.claude/episodes/`
- `.claude/agent-org/approvals/`

home 配下 (`memory: user` 系 + cross-session 共有 state):

- `~/.claude/agent-memory/regression-watcher/`
- `~/.claude/agent-memory/regression-fixer/`
- `~/.claude/agent-org/state/<proj-hash>/detections/`
- `~/.claude/agent-org/state/<proj-hash>/fixes/`
- `~/.claude/agent-org/state/<proj-hash>/learnings/`

`<proj-hash>` は **cwd を canonicalize して sha256 した先頭 8 桁**。複数プロジェクトを
跨いでも cross-session state が混じらないようにするための識別子。

## 手順

以下の Bash コマンドを実行してください。

### 1. `<proj-hash>` を計算する

```bash
PROJ_HASH=$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
")
echo "proj-hash: $PROJ_HASH"
echo "cwd:       $(pwd -P)"
```

### 2. ディレクトリを一括作成 (冪等)

```bash
mkdir -p \
  .claude/agent-memory/decision-keeper \
  .claude/agent-memory/architect-reviewer \
  .claude/agent-memory/context-compressor \
  .claude/episodes \
  .claude/agent-org/approvals \
  ~/.claude/agent-memory/regression-watcher \
  ~/.claude/agent-memory/regression-fixer \
  ~/.claude/agent-org/state/"$PROJ_HASH"/detections \
  ~/.claude/agent-org/state/"$PROJ_HASH"/fixes \
  ~/.claude/agent-org/state/"$PROJ_HASH"/learnings
```

### 3. 作成結果を表示する

```bash
echo "=== repo 内 ==="
ls -la .claude/agent-memory/ 2>&1
ls -la .claude/agent-org/ 2>&1
ls -la .claude/episodes/ 2>&1

echo "=== home 配下 ==="
ls -la ~/.claude/agent-memory/ 2>&1
ls -la ~/.claude/agent-org/state/"$PROJ_HASH"/ 2>&1
```

### 4. 環境変数の設定方法を案内する

`/run-review` (Phase 3 で実装予定) で agent teams を使う場合、ユーザー側
`.claude/settings.json` に以下を追加する必要がある:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

Phase 3 がリリースされるまでは不要。

## 冪等性について

- `mkdir -p` は既存ディレクトリでもエラーにならない。再実行しても安全
- `<proj-hash>` は cwd が同じなら毎回同じ値になるため、同じ project では
  常に同じ state dir を指す
- 既に MEMORY.md / approval ファイル等が書かれていても影響しない (新しく作る
  ものは空のディレクトリのみ)

## 注意事項

- subagent 起動時、Claude Code フレームワークが自動的に **scoped name dir**
  (`.claude/agent-memory/agent-org-<name>/`、`:` を `-` に置換した命名) を空で
  作成する。本 command は **plain name dir** のみ mkdir するため、scoped name dir
  は subagent 初回起動時に並存する (実機検証 ADR-002 参照)。subagent の書込先は
  plain name dir、auto-inject 対象は scoped name dir という不整合があるため、
  `agents/*.md` / `skills/*/SKILL.md` の指示に従って明示 Read 経路で運用する
- 実行は project root (`.claude/` の親) で行う想定。それ以外のディレクトリで
  実行すると意図しない場所に `.claude/` が作られる
- `~/.claude/agent-memory/` 配下は全プロジェクト共通の領域 (worktree 隔離の
  対象外)。Phase 4 の `--bg` 起動 subagent はここに memory を書く
- `~/.claude/agent-org/state/<proj-hash>/` は project ごとに分離されるため、
  別プロジェクトの detection / fix と混ざらない

## 関連

- Phase 1: `commands/compress-context.md`, `agents/context-compressor.md`,
  `hooks/postcompact-episode.sh`
- Phase 2: `agents/decision-keeper.md`, `skills/recording-decision/`,
  `skills/consulting-memory/`
- Phase 3 (未実装): architect-reviewer + running-review + Stop/TaskCompleted hooks
- Phase 4 (未実装): regression-watcher + regression-fixer + post-commit-trigger
