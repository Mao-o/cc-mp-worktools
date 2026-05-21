---
description: agent-org plugin が使う state ディレクトリ群を初期化する (.claude/agent-memory/agent-org-<agent>/, .claude/episodes/, .claude/agent-org/approvals/, ~/.beads/<proj-hash>/, .gitignore 更新)。v0.6.0 から beads (`bd init`) が hard dependency
---

# /org-init

agent-org plugin が使うディレクトリと **beads database** (`~/.beads/<proj-hash>/`)
を冪等に初期化する。

## 作成対象

repo 内 (`memory: project` 系):

- `.claude/agent-memory/agent-org-decision-keeper/`
- `.claude/agent-memory/agent-org-architect-reviewer/`
- `.claude/agent-memory/agent-org-context-compressor/`
- `.claude/episodes/`
- `.claude/agent-org/approvals/` (v0.6.x までの approval JSON 互換用。
  v0.7.0 以降は `/run-review` が bd approval issue に書込むため新規には
  作られない。`/migrate-approvals-to-beads` で旧 JSON を bd に変換後は
  空 / `.claude/agent-org/approvals.legacy/` に mv される)
- `.gitignore` に beads 関連 entry を追記 (idempotent)

home 配下 (`memory: user` 系 + cross-session 共有 state):

- `~/.claude/agent-memory/agent-org-regression-watcher/`
- `~/.claude/agent-memory/agent-org-regression-fixer/`
- `~/.claude/agent-org/state/<proj-hash>/learnings/`
- `~/.claude/agent-org/state/<proj-hash>/last-commit.json` の格納先 (空ファイルは作らない、post-commit-trigger.sh が必要に応じて書く)

beads database (v0.6.0 から hard dependency):

- `~/.beads/<proj-hash>/` (working dir 外、bg-fixer が worktree 隔離下でも書ける場所)
- `~/.beads/<proj-hash>/.beads/` (bd init が生成、`BEADS_DIR` で指す path)
- `bd config set types.custom "detection,fix,approval,episode,task"`
  (v0.7.0 で `task` を追加 / 5 types)。bd 1.0.4 は `Warning: "types.custom" is
  not a recognized config key` を吐くが **設定は effective** で `bd types`
  出力にも反映される (v0.7.1 hotfix で実機確認、v0.7.0 で試した
  `custom.types` は逆に無視されることを実測で確認)
- `git config beads.role maintainer` (warning 抑制)

すべての agent memory dir は **scoped name** (`agent-org-<agent-name>/` 形式) で
作成する。Claude Code v2.1.33+ は plugin scoped name (`agent-org:<agent>`) の
`:` を `-` に置換した dir を memory として解決するため、scoped name dir に
書けば auto-inject (200 行/25KB) が動作する (ADR-003 採用判断、v0.3.0)。

`<proj-hash>` は **cwd を canonicalize して sha256 した先頭 8 桁**。複数プロジェクトを
跨いでも cross-session state が混じらないようにするための識別子。

## 引数

```text
/org-init [--dry-run]
```

| 引数 | 説明 |
|---|---|
| `--dry-run` (任意) | 実際にディレクトリ作成 / `bd init` / `.gitignore` 書込を行わず、実行予定の内容のみ表示 |

## 手順

以下の Bash コマンドを実行してください。

### 1. 前提チェック (bd CLI 必須)

```bash
command -v bd >/dev/null 2>&1 || {
  echo "FATAL: bd CLI not installed. Install with 'brew install beads' (Mac) and re-run /org-init"
  exit 1
}
echo "bd version: $(bd version 2>&1 | head -1)"
```

### 2. `<proj-hash>` を計算する

```bash
PROJ_HASH=$(python3 -c "
import hashlib, os
cwd = os.path.realpath(os.getcwd())
print(hashlib.sha256(cwd.encode()).hexdigest()[:8])
")
echo "proj-hash: $PROJ_HASH"
echo "cwd:       $(pwd -P)"
```

### 3. agent-org の state / memory dir を一括作成 (冪等)

```bash
mkdir -p \
  .claude/agent-memory/agent-org-decision-keeper \
  .claude/agent-memory/agent-org-architect-reviewer \
  .claude/agent-memory/agent-org-context-compressor \
  .claude/episodes \
  .claude/agent-org/approvals \
  ~/.claude/agent-memory/agent-org-regression-watcher \
  ~/.claude/agent-memory/agent-org-regression-fixer \
  ~/.claude/agent-org/state/"$PROJ_HASH"/learnings
```

v0.6.0 から `detections/` / `fixes/` ディレクトリは **作成しない** (bd に移行済)。
`learnings/` と `last-commit.json` 格納先は Phase 5 では維持。

### 4. beads database を初期化する (v0.6.0 hard dependency)

```bash
BEADS_PARENT="$HOME/.beads/$PROJ_HASH"
BEADS_DIR="$BEADS_PARENT/.beads"

if [ -d "$BEADS_DIR" ]; then
  echo "skip: beads db already initialized at $BEADS_DIR"
else
  mkdir -p "$BEADS_PARENT"
  (cd "$BEADS_PARENT" && bd init --skip-agents --non-interactive --prefix "$PROJ_HASH")
  # `--skip-agents`: bd が AGENTS.md / CLAUDE.md / .claude/settings.json を
  #                  自動生成するのを抑制。agent-org plugin 側で独自管理する
  # `--non-interactive`: prompt なし
  # `--prefix $PROJ_HASH`: bd issue ID prefix
fi

# git config beads.role maintainer (warning 抑制、bd 1.0.4 で要求される設定)
(cd "$BEADS_PARENT" && git config beads.role maintainer 2>/dev/null || true)

# custom type 登録 (v0.7.0: `task` を追加、5 types に / v0.7.1: hotfix for U13 PoC 誤り)
#
# bd 1.0.4 実機検証で確定:
#   - `bd config set types.custom ...` は warning を吐くが設定は effective
#     (`bd types` 出力に反映される)。bd の `bd types` ヘルプ自体が
#     `Configure with: bd config set types.custom "..."` を指示
#   - `bd config set custom.types ...` は warning なしだが `bd types` に
#     反映されない (`No custom types configured` のまま) — 無視される
#   - U13 PoC で逆と判定していたが、それは検証手順の誤り
#
# warning (`Warning: "types.custom" is not a recognized config key`) は
# false alarm として無視。verify は `bd types` 出力 grep で行う (これは
# 実装が config namespace ではなく "実際に登録されたか" を直接見る正しい
# 方法、v0.7.0 で導入した改善はそのまま維持)
BEADS_DIR="$BEADS_DIR" bd config set types.custom "detection,fix,approval,episode,task" 2>&1
config_exit=$?
if [ "$config_exit" -ne 0 ]; then
  echo "FATAL: bd config set types.custom failed (exit=$config_exit)"
  exit 1
fi

types_out="$(BEADS_DIR="$BEADS_DIR" bd types 2>/dev/null || echo "")"
missing=()
for t in detection fix approval episode task; do
  echo "$types_out" | grep -qE "^  ${t}$" || missing+=("$t")
done
if [ ${#missing[@]} -eq 0 ]; then
  echo "verified: bd types includes detection, fix, approval, episode, task"
else
  echo "FATAL: bd types missing: ${missing[*]}"
  echo "       expected to contain: detection, fix, approval, episode, task"
  echo "       Run: BEADS_DIR=\"$BEADS_DIR\" bd config set types.custom 'detection,fix,approval,episode,task'"
  exit 1
fi

echo "BEADS_DIR=$BEADS_DIR"
BEADS_DIR="$BEADS_DIR" bd doctor 2>&1 | head -5
```

### 5. `.gitignore` を更新する (idempotent、bd 関連 3 行 + agent-org marker)

```bash
GITIGNORE="$(git rev-parse --show-toplevel 2>/dev/null)/.gitignore"
if [ -z "$GITIGNORE" ] || [ ! -f "$(git rev-parse --show-toplevel 2>/dev/null)/.git/HEAD" ]; then
  echo "warn: not in a git repo, skip .gitignore update"
else
  if ! grep -q "agent-org plugin (v0.6.0+)" "$GITIGNORE" 2>/dev/null; then
    {
      echo ""
      echo "# agent-org plugin (v0.6.0+)"
      echo "!.beads/issues.jsonl"
      echo ".beads/embeddeddolt/"
      echo ".beads/dolt/"
    } >> "$GITIGNORE"
    echo "updated: $GITIGNORE (agent-org marker added)"
  else
    echo "skip: agent-org marker already in $GITIGNORE"
  fi
fi
```

`<repo>/.beads/issues.jsonl` のみを git 管理対象として残す (Stop hook の
`bd-export.sh` がここに export する、git audit trail 補償の opt-in workflow)。
`embeddeddolt/` / `dolt/` は bd の内部 DB なので git 視界外に置く。

### 6. 作成結果を表示する

```bash
echo "=== repo 内 ==="
ls -la .claude/agent-memory/ 2>&1
ls -la .claude/agent-org/ 2>&1
ls -la .claude/episodes/ 2>&1

echo "=== home 配下 ==="
ls -la ~/.claude/agent-memory/ 2>&1
ls -la ~/.claude/agent-org/state/"$PROJ_HASH"/ 2>&1

echo "=== beads ==="
ls -la ~/.beads/"$PROJ_HASH"/ 2>&1
BEADS_DIR=~/.beads/"$PROJ_HASH"/.beads bd types 2>&1 | head -20
```

### 7. 環境変数の設定方法を案内する

`/run-review` (Phase 3) で agent teams を使う場合、ユーザー側
`.claude/settings.json` に以下を追加する必要がある:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

## 冪等性について

- `mkdir -p` は既存ディレクトリでもエラーにならない。再実行しても安全
- `<proj-hash>` は cwd が同じなら毎回同じ値になるため、同じ project では
  常に同じ state dir / beads db を指す
- 既に MEMORY.md / approval ファイル等が書かれていても影響しない
- `bd init` 済みなら skip。`bd config set types.custom` は再実行で同じ値を set (idempotent)
- `.gitignore` は marker 行で重複検知 (`grep -q "agent-org plugin (v0.6.0+)"`)

## v0.5.x からのアップグレード時の注意

v0.5.x で `~/.claude/agent-org/state/<proj-hash>/detections/*.yaml` /
`fixes/*.json` を蓄積していたプロジェクトは、`/org-init` 後に
**`/migrate-to-beads`** を実行して bd issue に変換する。詳細は
`commands/migrate-to-beads.md` 参照。

旧 `detections/` / `fixes/` ディレクトリは `/migrate-to-beads` でも削除されない
(rollback `/migrate-from-beads` のための保険)。完全削除したい場合は Phase 9
の `/cleanup-legacy-state` で対応予定。

## v0.3.0 からのアップグレード時の注意

v0.2.x で plain name dir (`.claude/agent-memory/<agent-name>/`) を使っていた
プロジェクトは、v0.3.0 で scoped name dir (`agent-org-<agent-name>/`) に
切り替わる。`/org-init` は新しい scoped dir を作るが、旧 plain dir に蓄積
された MEMORY.md / ADR ファイルは自動的には移行されない。手動で:

```bash
mv .claude/agent-memory/decision-keeper/* \
   .claude/agent-memory/agent-org-decision-keeper/ 2>/dev/null || true
mv .claude/agent-memory/context-compressor/* \
   .claude/agent-memory/agent-org-context-compressor/ 2>/dev/null || true
mv .claude/agent-memory/architect-reviewer/* \
   .claude/agent-memory/agent-org-architect-reviewer/ 2>/dev/null || true

rmdir .claude/agent-memory/decision-keeper 2>/dev/null || true
rmdir .claude/agent-memory/context-compressor 2>/dev/null || true
rmdir .claude/agent-memory/architect-reviewer 2>/dev/null || true
```

しておくこと。

## 注意事項

- 実行は project root (`.claude/` の親) で行う想定。それ以外のディレクトリで
  実行すると意図しない場所に `.claude/` が作られる、`<proj-hash>` も別値に
  なるため beads db が repo と紐付かない
- `~/.claude/agent-memory/` 配下は全プロジェクト共通の領域 (worktree 隔離の
  対象外)。`--bg` 起動 subagent はここに memory を書く
- `~/.claude/agent-org/state/<proj-hash>/` は project ごとに分離されるため、
  別プロジェクトの detection / fix と混ざらない
- `~/.beads/<proj-hash>/` も同様に project ごとに分離 (cwd 移動で別 db を見る)
- `bd init` は内部で git repo 化 + 初回 commit を行う。`~/.beads/<proj-hash>/.git/`
  が作られるが、これは bd の内部実装で agent-org とは独立

## 関連

- Phase 1: `commands/compress-context.md`, `agents/context-compressor.md`,
  `hooks/postcompact-episode.sh`
- Phase 2: `agents/decision-keeper.md`, `skills/recording-decision/`,
  `skills/consulting-memory/`
- Phase 3: `agents/architect-reviewer.md`, `commands/run-review.md`,
  `skills/running-review/`, Stop/TaskCompleted hooks
- Phase 4: `agents/regression-watcher.md`, `agents/regression-fixer.md`,
  `commands/start-watcher.md`, `commands/fix-regression.md`,
  `hooks/post-commit-trigger.sh`
- Phase 5 (v0.6.0): `skills/using-beads/`, `commands/bd-check.md`,
  `commands/migrate-to-beads.md`, `commands/migrate-from-beads.md`,
  `hooks/bd-export.sh`
- beads 公式: <https://github.com/steveyegge/beads>
