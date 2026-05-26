---
description: agent-org plugin が使う state ディレクトリ群を初期化する (.claude/agent-memory/agent-org-<agent>/, .claude/episodes/, .claue/agent-org/approvals/, <repo>/.beads/, .gitignore 更新)。v0.6.0 から beads (`bd init`) が hard dependency、v0.8.0 から bd は `<repo>/.beads/` に repo-local 配置 (ADR-007)
---

# /org-init

agent-org plugin が使うディレクトリと **beads database** (`<repo>/.beads/`)
を冪等に初期化する。

## v0.8.0 で path 規約が変更されました (BREAKING)

v0.7.x までは bd を `~/.beads/<proj-hash>/.beads/` に配置していたが、ADR-007
(2026-05-22) で **`<repo>/.beads/`** に変更された (D 案採用、bd の
git worktree-aware 設計を活用)。`~/.beads/<proj-hash>/` を持つ既存
プロジェクトは `/migrate-beads-to-repo-local` で新 path に移行する。

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

beads database (v0.6.0 から hard dependency、v0.8.0 から `<repo>/.beads/` に repo-local):

- `<repo>/.beads/` (`bd init` が `<repo>/.beads/embeddeddolt/` を生成)
- `bd config set types.custom "detection,fix,approval,episode,task"`
  (v0.7.0 で `task` を追加 / 5 types)。bd 1.0.4 は `Warning: "types.custom" is
  not a recognized config key` を吐くが **設定は effective** で `bd types`
  出力にも反映される (v0.7.1 hotfix で実機確認、v0.7.0 で試した
  `custom.types` は逆に無視されることを実測で確認)
- `git config beads.role maintainer` (warning 抑制、`<repo>/.git/config` に書く)

すべての agent memory dir は **scoped name** (`agent-org-<agent-name>/` 形式) で
作成する。Claude Code v2.1.33+ は plugin scoped name (`agent-org:<agent>`) の
`:` を `-` に置換した dir を memory として解決するため、scoped name dir に
書けば auto-inject (200 行/25KB) が動作する (ADR-003 採用判断、v0.3.0)。

`<proj-hash>` は **cwd を canonicalize して sha256 した先頭 8 桁**。v0.8.0 から
bd path には不要だが、以下の用途で算出ロジックは保持する:

- `~/.claude/agent-org/state/<proj-hash>/learnings/` (memory dir 分離)
- bd の `--prefix <proj-hash>` (issue ID prefix、ADR-007 で継続)
- 旧 path 検出 (`/migrate-beads-to-repo-local`)

## 引数

```text
/org-init [--dry-run]
```

| 引数 | 説明 |
|---|---|
| `--dry-run` (任意) | 実際にディレクトリ作成 / `bd init` / `.gitignore` 書込を行わず、実行予定の内容のみ表示 |

## 手順

以下の Bash コマンドを実行してください。

### 1. 前提チェック (bd CLI 必須、`<repo>` 内であること)

```bash
command -v bd >/dev/null 2>&1 || {
  echo "FATAL: bd CLI not installed. Install with 'brew install beads' (Mac) and re-run /org-init"
  exit 1
}
echo "bd version: $(bd version 2>&1 | head -1)"

# git repo であることを確認 (v0.8.0 から <repo>/.beads/ 配置のため必須)
# worktree 内で実行された場合は git common-dir 経由で main repo を解決
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
if [ -z "$REPO_ROOT" ]; then
  echo "FATAL: not in a git repository. v0.8.0 から bd は <repo>/.beads/ に配置されるため git repo 内での実行が必須"
  exit 1
fi
MAIN_REPO="$(cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
[ -n "$MAIN_REPO" ] || MAIN_REPO="$REPO_ROOT"
echo "repo root: $REPO_ROOT"
if [ "$REPO_ROOT" != "$MAIN_REPO" ]; then
  echo "main repo: $MAIN_REPO (worktree detected — bd は main repo に init される)"
fi
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

### 4. beads database を初期化する (v0.6.0 hard dependency、v0.8.0 から `<main_repo>/.beads/` + stealth)

```bash
# v0.8.0: bd は main repo (worktree でない方) に init する。
# worktree 内で /org-init を実行しても MAIN_REPO で bd init される
# (bd の git worktree-aware 設計により、worktree からも同じ DB にアクセス可能)
BEADS_DIR="$MAIN_REPO/.beads"

if [ -d "$BEADS_DIR" ]; then
  echo "skip: beads db already initialized at $BEADS_DIR"
else
  # v0.8.0: main repo ルートで bd init --stealth する (ADR-007 D 案 + stealth amendment)
  # - `--stealth`: bd が自動で `.git/info/exclude` に `.beads/` 等を追加。
  #                個人専用 git exclude (collaborators の git に影響しない)
  # - `--skip-agents`: bd が AGENTS.md / CLAUDE.md / .claude/settings.json を
  #                    自動生成するのを抑制。agent-org plugin 側で独自管理する
  # - `--non-interactive`: prompt なし
  # - `--prefix $PROJ_HASH`: bd issue ID prefix (cross-project hash 衝突防止)
  (cd "$MAIN_REPO" && bd init --stealth --skip-agents --non-interactive --prefix "$PROJ_HASH")
fi

# git config beads.role maintainer (warning 抑制、bd 1.0.4 で要求される設定)
# v0.8.0: <main_repo>/.git/config に書く (bd が main repo の git を共有するため)
(cd "$MAIN_REPO" && git config beads.role maintainer 2>/dev/null || true)

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
#
# v0.8.0: BEADS_DIR の export なしで cd <repo> から bd 自動 resolve (ADR-007)
(cd "$MAIN_REPO" && bd config set types.custom "detection,fix,approval,episode,task" 2>&1)
config_exit=$?
if [ "$config_exit" -ne 0 ]; then
  echo "FATAL: bd config set types.custom failed (exit=$config_exit)"
  exit 1
fi

types_out="$(cd "$MAIN_REPO" && bd types 2>/dev/null || echo "")"
missing=()
for t in detection fix approval episode task; do
  echo "$types_out" | grep -qE "^  ${t}$" || missing+=("$t")
done
if [ ${#missing[@]} -eq 0 ]; then
  echo "verified: bd types includes detection, fix, approval, episode, task"
else
  echo "FATAL: bd types missing: ${missing[*]}"
  echo "       expected to contain: detection, fix, approval, episode, task"
  echo "       Run: (cd $MAIN_REPO && bd config set types.custom 'detection,fix,approval,episode,task')"
  exit 1
fi

echo "BEADS_DIR (auto-resolved by bd from $MAIN_REPO): $BEADS_DIR"
(cd "$MAIN_REPO" && bd doctor 2>&1 | head -5)
```

### 5. stealth mode が設定した `.git/info/exclude` を確認する

v0.8.0 で `bd init --stealth` を使うため、ユーザーが手動で `.gitignore` を
編集する必要はない (bd 自身が `.git/info/exclude` に `.beads/` 等を追加し、
generic な dolt-related ignore `.dolt/` `*.db` `.beads-credential-key` は
`.gitignore` に自動追記する)。本手順は verify のみ:

```bash
# git common-dir/info/exclude に書かれる (main repo の .git/info/exclude を直接参照)
GIT_EXCLUDE="$(git -C "$MAIN_REPO" rev-parse --git-common-dir)/info/exclude"
if [ -f "$GIT_EXCLUDE" ] && grep -q "^\.beads/" "$GIT_EXCLUDE" 2>/dev/null; then
  echo "verified: $GIT_EXCLUDE excludes .beads/ (stealth mode)"
else
  echo "warn: $GIT_EXCLUDE does not exclude .beads/ - re-run /org-init or bd init --setup-exclude"
fi
```

`.git/info/exclude` は **git で track されない個人 git exclude**。同じ repo を
clone した他の collaborator には影響しない (= stealth)。

`.gitignore` は bd init 自身が `.dolt/` `*.db` `.beads-credential-key` を追加
する場合があるが (bd 1.0.4+ default 挙動)、agent-org plugin はこれ以上 `.gitignore`
を編集しない (Phase 9 で stealth 採用時のクリーンアップを ADR-007 amendment に
記載)。

audit trail (git track された `issues.jsonl`) を希望する場合は、ユーザーが
`git add -f .beads/issues.jsonl` で **明示的に force add** する。
default は personal use 想定で audit trail なし。

### 6. 作成結果を表示する

```bash
echo "=== repo 内 ==="
ls -la .claude/agent-memory/ 2>&1
ls -la .claude/agent-org/ 2>&1
ls -la .claude/episodes/ 2>&1

echo "=== home 配下 ==="
ls -la ~/.claude/agent-memory/ 2>&1
ls -la ~/.claude/agent-org/state/"$PROJ_HASH"/ 2>&1

echo "=== beads (<main_repo>/.beads/) ==="
ls -la "$MAIN_REPO/.beads/" 2>&1
(cd "$MAIN_REPO" && bd types 2>&1 | head -20)
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
- `<proj-hash>` は cwd が同じなら毎回同じ値になる
- 既に MEMORY.md / approval ファイル等が書かれていても影響しない
- `bd init` 済み (`<repo>/.beads/` 存在) なら skip。`bd config set types.custom` は再実行で同じ値を set (idempotent)
- `.git/info/exclude` は bd init が idempotent に追記する (重複行は作らない)

## v0.7.x からのアップグレード時の注意 (v0.8.0)

v0.7.x で `~/.beads/<proj-hash>/.beads/` に bd を持っていたプロジェクトは、
`/org-init` を再実行すると `<repo>/.beads/` に新規 bd db が初期化される
(旧 path はそのまま残る = 2 つの bd db が並存する状態)。これを解消するには:

```bash
# 旧 path → 新 path に migration (bd export → init → bd import)
/migrate-beads-to-repo-local
```

`/migrate-beads-to-repo-local` は idempotent、`--dry-run` 対応、foreground 専用。
詳細は `commands/migrate-beads-to-repo-local.md` 参照。

旧 path (`~/.beads/<proj-hash>/`) は migration 完了時に削除される
(v0.8.0 では rollback path を残さない、breaking cut)。rollback したい場合は
事前に `/migrate-from-beads` で旧 YAML/JSON 形式に書き戻すこと。

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

- 実行は **git repo root** (`.git/` の親、`.claude/` の親) で行うこと。
  v0.8.0 から `<repo>/.beads/` に bd db が配置されるため、それ以外の dir で
  実行すると bd が違う場所に init される
- `~/.claude/agent-memory/` 配下は全プロジェクト共通の領域 (worktree 隔離の
  対象外)。`--bg` 起動 subagent はここに memory を書く
- `~/.claude/agent-org/state/<proj-hash>/` は project ごとに分離されるため、
  別プロジェクトの learnings と混ざらない
- `<repo>/.beads/` は **bd の git worktree-aware 設計** で main repo と worktree
  間で同じ DB が共有される (ADR-007 evidence 参照)
- `bd init` は repo の既存 `.git/` を使うため、独立 git repo を作らない
  (`.beads/embeddeddolt/` のみ生成)
- bd の `<repo>/.beads/` から `.git/` への直接書込は無い。`bd export` で
  `<repo>/.beads/issues.jsonl` に書き、ユーザーが `git add` する opt-in workflow

## 関連

- episode: `skills/compressing-context/`, `agents/context-compressor.md`,
  `hooks/postcompact-episode.sh`
- ADR: `agents/decision-keeper.md`, `skills/recording-decision/`,
  `skills/consulting-memory/`
- review: `agents/architect-reviewer.md`, `skills/running-review/`,
  Stop/TaskCompleted hooks
- regression: `agents/regression-watcher.md`, `agents/regression-fixer.md`,
  `skills/starting-watcher/`, `skills/fixing-regression/`,
  `hooks/post-commit-trigger.sh`
- Phase 5 (v0.6.0): `skills/using-beads/`, `commands/bd-check.md`,
  `commands/migrate-to-beads.md`, `commands/migrate-from-beads.md`,
  `hooks/bd-export.sh`
- v0.8.0 (ADR-007): `commands/migrate-beads-to-repo-local.md`
- beads 公式: <https://github.com/steveyegge/beads>
