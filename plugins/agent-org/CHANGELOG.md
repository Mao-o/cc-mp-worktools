# Changelog

## 0.8.1 (2026-05-23) — Bugfix: bd-check の bd doctor 判定 + bd-export の source field

v0.8.0 release 後のドッグフーディング (worktools 自体への v0.8.0 適用) で
発覚した副次的 bug 2 件を修正する patch release。機能変更なし。

### Fixed

- **`commands/bd-check.md` [4] bd doctor**: bd 1.0.4 embedded mode (現 default)
  は `bd doctor` 未サポートで「Note: 'bd doctor' is not yet supported in
  embedded mode」を **exit 0** で返すため、従来の `[ "$doctor_exit" = "0" ]`
  判定が「PASS: bd doctor reports DB healthy」と**誤判定**していた。output に
  "not yet supported" を含む場合は WARN 扱い (skip DB health check) に変更。
  server mode (`bd init --server`) のみ PASS/FAIL 判定が有効
- **`hooks/bd-export.sh` source field**: meta 出力
  `source: ${exported_method:-bd_export}` で `exported_method` 変数が script
  内で一度も set されておらず、bd export 経路でも fallback 経路でも常に
  "bd_export" と記録されて診断時の誤情報源となっていた。各分岐で
  `exported_method="bd_export"` / `"bd_list_fallback"` /
  `"bd_list_fallback_empty"` を set するよう修正

### Verification

- `claude plugin validate plugins/agent-org` warning 0 通過
- 修正後 worktools 自体で `/bd-check` 再実行: `[4] bd doctor` が WARN
  ("not supported in embedded mode") に正しく変わる
- bd-export hook 再発火: meta `source` field が "bd_export" になることを実機確認

## 0.8.0 (2026-05-22) — BREAKING: bd path 規約変更 (`<repo>/.beads/`) + stealth mode

ADR-007 (2026-05-22) 採用に伴う **breaking change**。bd の物理配置を
`~/.beads/<proj-hash>/.beads/` から **`<repo>/.beads/`** に変更し、
`bd init --stealth --skip-agents` で個人 git exclude (`.git/info/exclude`)
を活用する形に統合。`BEADS_DIR` 明示指定を不要化し、ユーザー手動での
`.gitignore` 編集も不要化 (bd init --stealth が必要な dolt-related ignore を
自動追記する)。

### Why (ADR-007 + amendment)

v0.6.0 (ADR-006) で D 案 (`<repo>/.beads/` repo-local) を否決し C 案
(`~/.beads/<proj-hash>/`) を採用したが、否決根拠だった「`--bg` セッションの
worktree 隔離下での split-brain」推論が 2026-05-22 実機 PoC (`/tmp/bd-wt-poc-*/`)
で覆った。bd は **git worktree-aware** に設計されており、`--bg` 隔離下でも
main repo の `.beads/` を共有する。さらに ADR-006 の C 案では bd 1.0.4 の
`bd init` reject 問題 (`cannot initialize bd inside a .beads directory`) が
発生していた (bd_092a232e-z69)。本 release で D 案に切り替えることで両問題が
原生的に解消し、bd の中核価値 (branch 切替で issue 状態分岐 / commit-level
audit trail / git history 統合) が完全に動作する。

加えてユーザー (Mao) の指摘で bd 1.0.4 の `--stealth` mode の存在が共有され、
ADR-007 amendment で `bd init --stealth --skip-agents` を default に採用。
`.git/info/exclude` (個人専用 git exclude、git で track されない) に `.beads/`
を自動追加するため、`.beads/` 配下のデータ (issue / dolt db) は確実に commit
されない。ユーザーが手動で `.gitignore` を編集する必要もない (bd init が
generic な dolt-related ignore を自動追記する)。"personal use without affecting
repo collaborators" (bd 公式) の本旨は **`.beads/` 配下データが流出しない** 点で
あり、bd 利用の事実そのものは `.gitignore` 差分から collaborators に見える。

### Breaking changes

- **bd 物理配置の変更**: `~/.beads/<proj-hash>/.beads/` → `<repo>/.beads/`
  - 既存 v0.6.0/v0.7.x ユーザーは `/migrate-beads-to-repo-local` で新 path に
    移行する必要がある。bd export → init --stealth → bd import 経路で
    issue ID / labels / deps / priority / memories を完全保持
  - 旧 path は migration tool で **削除される** (v0.8.0 は breaking cut、
    rollback したい場合は `/migrate-from-beads` で旧 YAML/JSON に書き戻して
    `claude plugin update agent-org -v 0.5.0` で pin)
- **`BEADS_DIR` export 規律の廃止**: 全 subagent / hook / skill / command で
  `cd <repo> && bd <subcmd>` パターンに統一。`BEADS_DIR` 明示指定は optional
  (bd 自動 resolve に委ねる)
- **`<repo>/.gitignore` の plugin 固有セクション廃止**: v0.6.0+ で追加していた
  `agent-org plugin (v0.6.0+)` セクション (`!.beads/issues.jsonl`
  `.beads/embeddeddolt/` `.beads/dolt/`) は v0.8.0 で書かない。bd init --stealth
  が `.git/info/exclude` に `.beads/` を追加する個人 git exclude を活用する。
  なお bd init --stealth は副次的に `.gitignore` に generic な dolt ignore
  (`.dolt/` `*.db` `.beads-credential-key`) を自動追記する (bd 1.0.4 仕様、
  agent-org plugin は touch しない)

### Changed

- `commands/org-init.md`: 手順 4 を `(cd "$REPO_ROOT" && bd init --stealth
  --skip-agents --non-interactive --prefix "$PROJ_HASH")` に書き換え。
  手順 5 (`.gitignore` 編集) を `.git/info/exclude` verify に置換。
  ADR-007 amendment への参照を追加
- `skills/using-beads/SKILL.md`: Rule 1 を「v0.8.0: bd は `<repo>/.beads/` から
  自動 resolve、`BEADS_DIR` 明示指定は optional」に書き換え。トラブルシュート
  表に「旧 `~/.beads/<proj-hash>/.beads/` 残存時の `/migrate-beads-to-repo-local`
  案内」を追加
- `commands/bd-check.md`: section 3 (beads database directory) を `<repo>/.beads/`
  検査に変更。section 3b で旧 path 残存検出 + migration 案内追加。section 8 を
  `.git/info/exclude` の `.beads/` 行検出に変更 (stealth mode active 確認)。
  全 bd invoke を `(cd $REPO_ROOT && bd ...)` パターンに統一
- `commands/migrate-to-beads.md` / `migrate-from-beads.md` /
  `migrate-approvals-to-beads.md`: `BEADS_DIR=~/.beads/<proj-hash>/.beads`
  export を `<repo>/.beads/` ベースに変更、bd invoke を cd パターンに統一
- `commands/run-review.md` / `start-watcher.md` / `fix-regression.md`:
  preflight bd dir check + 起動後の確認 command を `<repo>/.beads/` ベースに、
  bd invoke を cd パターンに統一。preflight 失敗時の案内テンプレも更新
- `agents/regression-watcher.md` / `regression-fixer.md`: 起動時 preflight の
  `BEADS_DIR` export を削除、`REPO_ROOT="$(git rev-parse --show-toplevel)"` +
  `<repo>/.beads/` 存在チェックに置換。`bd prime` 含む全 bd invoke を
  `(cd "$REPO_ROOT" && bd ...)` パターンに統一
- `agents/architect-reviewer.md`: 真 RO 規律内の「approval 書込先」description
  に v0.8.0 path note を追加 (構造変更なし、説明文のみ)
- `hooks/bd-export.sh`: proj-hash 計算ロジックを削除、`<repo>/.beads/` を
  source/output として直接利用 (cd `$repo_root` で bd 自動 resolve)
- `hooks/task-completed-gate.sh`: BEADS_DIR 解決ロジックを
  `git rev-parse --show-toplevel` ベースに置換、proj-hash 計算は削除。
  bd invoke を cd パターンに統一
- `hooks/stop-quality-gate.sh`: `approvals_clean` kind を `<repo>/.beads/`
  ベースに同様変更
- `docs/ARCHITECTURE.md`: Phase 5/6 path 記述、subgraph label、ファイルパス
  規約表を v0.8.0 + ADR-007 reference に更新

### Added

- `commands/migrate-beads-to-repo-local.md` (新規): v0.7.x で蓄積された
  `~/.beads/<proj-hash>/.beads/` を `<repo>/.beads/` に移行する one-shot
  migration。`bd export` (旧 path) → `bd init --stealth` (新 path = `<repo>/`)
  → `bd import` 経路で issue ID / labels / deps / priority / memories
  を完全保持 (bd 1.0.4 round-trip サポート)。idempotent、`--dry-run` /
  `--keep-old` (旧 path 保持 opt-in) 対応、foreground 専用。
  default は migration 完了時に旧 path を `rm -rf` する (breaking cut)
- ADR-007 yml の `amendments:` セクション (immutability 保持、本文不変)
  に stealth mode 採用を記録

### Migration from 0.7.x

```text
# 1. /org-init を再実行 (新 <repo>/.beads/ が初期化される)
/org-init

# 2. 旧 path から新 path へ migration (issue 完全保持)
/migrate-beads-to-repo-local
# (default で旧 ~/.beads/<proj-hash>/ は削除される。
#  rollback したいなら事前に /migrate-from-beads で v0.5.x YAML/JSON 保存)
```

`.gitignore` に v0.6.0+ / v0.7.x の `# agent-org plugin (v0.6.0+)` セクションが
残っている場合、`/bd-check` が WARN で残存通知する。手動で削除する。

### Worktree-aware preflight 修正 (2026-05-22 検証で発見)

`--bg` セッションは `.claude/worktrees/<id>/` に worktree 隔離されるため、
worktree 内で `git rev-parse --show-toplevel` を呼ぶと **worktree path** が
返り、`[ -d $REPO_ROOT/.beads ]` チェックで false になり「FATAL: not
initialized」で abort する bug が判明。bd 自身は worktree-aware で動作する
ため bd invoke 自体は worktree から main repo `.beads/` に到達するが、
plugin preflight ロジック側が盲点だった。

修正: 全 preflight + hooks で git common-dir 経由の main_repo 解決を採用:

```bash
MAIN_REPO="$(cd "$(dirname "$(git rev-parse --git-common-dir 2>/dev/null)")" 2>/dev/null && pwd -P)"
[ -n "$MAIN_REPO" ] || MAIN_REPO="$REPO_ROOT"
# 以降は MAIN_REPO/.beads/ ベースで判定
```

修正対象 (本 entry の "### Changed" に統合済):

- preflight 4 files: `agents/regression-watcher.md` / `regression-fixer.md`,
  `commands/start-watcher.md` / `fix-regression.md`
- hooks 3 files: `hooks/bd-export.sh` / `task-completed-gate.sh` /
  `stop-quality-gate.sh`
- aux 2 files: `commands/bd-check.md` (section 3 で worktree 検出表示),
  `commands/org-init.md` (bd init を MAIN_REPO で実行)

bd-export.sh の output (`issues.jsonl`) も MAIN_REPO 側に書込むよう修正
(worktree 内 Stop でも main repo の audit snapshot が更新される)。

### Verification (2026-05-22 実機 PoC)

| 検証 | 結果 |
|---|---|
| `/tmp/bd-wt-poc-*/` git worktree-aware (bd は main repo `.beads/` を共有) | ✓ split-brain なし |
| `/tmp/bd-stealth-poc-*/` `bd init --stealth` (`.git/info/exclude` 追加) | ✓ `.gitignore` 不要 |
| bd export → bd init --stealth → bd import round-trip | ✓ issue 完全保持 (1.0.4 公式サポート) |
| `/tmp/bd-v080-bg-poc-*/` `--bg` 隔離模擬 (git worktree) からの bd 共有 | ✓ worktree → main repo `.beads/` 共有確認 |
| `/tmp/bd-v080-wt-recheck-*/` worktree-aware preflight + hook ロジック | ✓ preflight 通過 + bd export → main_repo に出力 |
| ADR-006 否決根拠の再評価 | ✓ split-brain 推論が PoC で覆る |
| bd 1.0.4 init reject 問題 (bd_092a232e-z69) | ✓ D 案で自然解消 |

### 関連 ADR

- ADR-005 (paired-with): SoT 採用判断 (不変)
- ADR-006 (superseded): C 案 (`~/.beads/<proj-hash>/`) 配置 → 物理配置のみ訂正、
  本文は immutable な歴史記録として保持
- ADR-007 (本リリース): D 案 (`<repo>/.beads/`) 配置 + stealth mode amendment

## 0.7.2 (2026-05-22) — Hotfix: /migrate-approvals-to-beads idempotency

Phase 6 検証で発見した bug の修正。`migrate-approvals-to-beads` の
idempotency check が `bd list -l "legacy-id:..." -t approval --json` で
`--status` を未指定だったため、`bd` デフォルトの `--status open` で
動作し、priority=2 (approved) で migration 後に close された approval を
検出できなかった。再 migrate で **重複 issue** が作られる。

### Fixed

- `commands/migrate-approvals-to-beads.md`: idempotency check に
  `--status all` を追加。`bd list -l "legacy-id:$legacy_id" -t approval
  --status all --json` で open / closed 両方を検出するように

### Verification (bd 1.0.4 Homebrew で実機確認)

```bash
bd list -l "legacy-id:PR-100" -t approval --json
# → (none)  ← bug: closed approval が見えない
bd list -l "legacy-id:PR-100" -t approval --status all --json
# → p6test-aq8  ← fix: closed approval も検出
```

実機検証では PR-100 (approve→priority=2→close) と PR-99 (reject→priority=0→open)
の 2 個を migrate、再実行時 PR-100 を空文字判定して重複作成する挙動を確認、
修正で両方 skip するようになる。

### Phase 6 検証サマリ (本 hotfix の動機)

| 検証 | 結果 |
|---|---|
| approval workflow (bd 直接、find-or-create/rejected/close/supersedes) | ✅ 全 9 ステップ |
| task-completed-gate.sh | ✅ 4 ケース (rejected→block / close→pass / opt-in / fail-open) |
| stop-quality-gate.sh | ✅ 5 ケース (rejected→block / close→pass / config 不在 / stop_hook_active) |
| migrate-approvals-to-beads | ⚠️ 機能動作、idempotency に bug 発見 → 本 hotfix で修正 |

## 0.7.1 (2026-05-22) — Hotfix: G2 規律を `types.custom` に revert

v0.7.0 で `bd config set types.custom` を `custom.types` に変更したが、
bd 1.0.4 実機検証で **U13 PoC 結論が誤り** と判明:

- 実機: `bd config set types.custom ...` は warning 表示するが `bd types` 出力に **反映される** (effective)
- 実機: `bd config set custom.types ...` は warning なしだが `bd types` に **反映されない** (`No custom types configured`)
- bd 1.0.4 の `bd types` ヘルプ自体が `Configure with: bd config set types.custom "..."` と指示

つまり v0.7.0 の修正は新規 install ユーザーで `/org-init` を実行すると
`bd types` に custom types が登録されず、`bd create -t detection` が
"invalid issue type" で reject される **致命的 regression**。

### Reverted

- `commands/org-init.md`: `bd config set custom.types` → `bd config set types.custom`。
  warning は false alarm として comment 追加。verify は v0.7.0 で導入した
  `bd types | grep -E "^  (detection|fix|approval|episode|task)$"` ベースを
  **維持** (これは正しい改善)。task type 追加 (5 types) も維持
- `commands/bd-check.md`: 同様 revert。手動修復コマンドの `custom.types` も
  `types.custom` に戻す
- `skills/using-beads/SKILL.md`: trouble shoot 表の `custom.types` を `types.custom` に
- `agents/regression-watcher.md`: `custom.types` を `types.custom` に

### 注意

`Warning: "types.custom" is not a recognized config key. Use 'custom.*' for
user-defined keys.` は bd 1.0.4 で表示されるが、設定は実際に保存され
`bd types` 出力に反映される。bd 内部の `bd types` ヘルプと `bd config --help`
の namespace 列挙が **inconsistent** な状態 (bd 側の課題)。実用上は warning
を許容して `types.custom` を使う方が確実。将来 bd が `custom.*` namespace に
完全移行したら再修正する。

### Verification (bd 1.0.4 Homebrew で実機確認)

```bash
TEST_PARENT=/tmp/bd-verify
mkdir -p "$TEST_PARENT" && cd "$TEST_PARENT"
bd init --skip-agents --non-interactive --prefix "test"
BEADS_DIR=$TEST_PARENT/.beads bd config set types.custom "detection,fix,approval,episode,task"
# → Warning は出るが Set types.custom = ... と表示
BEADS_DIR=$TEST_PARENT/.beads bd types
# → "Configured custom types:" セクションに detection,fix,approval,episode,task が列挙
```

## 0.7.0 (2026-05-22) — Phase 6: approval → bd issue 化

Phase 6 of the agent-org plugin. `/run-review` が書いていた
`.claude/agent-org/approvals/<task-id>.json` を廃止し、beads issue (type=approval)
に一本化した。task-completed-gate / stop-quality-gate も bd label-based query で
動くように書き換え。再 review は `bd dep add --type supersedes` で履歴を残す。

### Changed (approval workflow → beads)

- `commands/run-review.md`: step 4 を `bd create -t approval` 化。
  必須 label セットは `approval` / `task:<task_id>` / `agent-org` /
  `aggregate:<approve|approve_with_conditions|request_changes|reject>` /
  `perspective:<persp>` (per reviewer)。priority で status を encode
  (`0`=rejected / `1`=conditional / `2`=approved / `3`=informational)。
  description body には reviewer 別 verdict YAML をそのまま埋込
- `hooks/task-completed-gate.sh`: 102 行 → ~40 行に縮小。
  `bd list -l "task:${task_id}" -t approval --status open --json | jq '[.[]|select(.priority==0)]|length'`
  で rejected approval を検出し、>0 なら exit 2 で block。
  approval JSON 不在 → opt-in 設計の踏襲 (task に approval 1 件も無ければ pass)
- `hooks/stop-quality-gate.sh`: `kind: approvals_clean` を同様の bd query に
  置換。`.claude/agent-org/approvals/*.json` 検査は廃止
- `agents/architect-reviewer.md`: 真 RO 規律内の「approval 書込先」を
  bd approval issue に更新 (構造変更なし、説明文のみ)
- `skills/running-review/SKILL.md`: verdict 集約後の永続化指示を
  bd approval issue 作成経路に更新

### Added

- `commands/migrate-approvals-to-beads.md` (新規): 既存
  `.claude/agent-org/approvals/*.json` を bd issue (type=approval) に変換する
  one-shot migration。task issue find-or-create + approval issue 作成 +
  `bd dep add <task> <approval>` + 旧 JSON を `.claude/agent-org/approvals.legacy/`
  に mv (Phase 9 で物理削除)。idempotent (label `legacy-id:<basename>` で重複検出)、
  `--dry-run` 対応、yq+jq+bd preflight。/migrate-to-beads と同じ規律 (G1-G8) 遵守

### task issue 規約 (Phase 6.1.0 で確定)

- `task_id` (`PR-42` / `design-auth-rewrite` 等の人間可読 ID) は
  `bd list -l "task:${task_id}" -t task --json | jq -r '.[0].id // empty'` で find、
  見つからなければ `bd create -t task -l "task:${task_id}" -l "agent-org" -p 2 "task: <id>"`
  で生成
- 生成タイミングは `/run-review <task-id>` 実行時に find-or-create のみ。
  TaskCreate hook 自動連携は v1.1.0 検討事項
- task と Phase 5 detection/fix の関係: 「task」は人間 / レビューワークフロー単位、
  「detection/fix」は regression-watcher が自動生成する技術的単位。Phase 6 では
  両者は別 issue type として併存

### Fixed (G2 規律: deprecated config key)

PoC 検証 (U13、bd 1.0.4) で `bd config set types.custom` が deprecated warning
を吐き、`bd types` 出力にも反映されないことが判明。正解は
`bd config set custom.types`。本 PR で以下を一括修正:

- `commands/org-init.md`: `bd config set types.custom ...` →
  `bd config set custom.types ...`。verify ロジックも `bd types | grep -E
  "^  (detection|fix|approval|episode|task)$"` で done 判定 (bd config get では
  deprecated key でも値を返すため不確実)
- `commands/migrate-to-beads.md`: trouble shoot 内記述を `custom.types` に置換
- `commands/bd-check.md`: section 5 (custom type 登録) の check ロジックを
  `bd types` 出力 grep ベースに変更
- `skills/using-beads/SKILL.md`: trouble shoot 表内記述を `custom.types` に置換

bd 1.0.4 では `-t approval` / `-t task` は custom types 未登録でも create
できるが、登録すると `bd types` 出力で可視化される + 将来の validation 強化
に備える。

### Migration from 0.6.0

- 既存 `.claude/agent-org/approvals/*.json` がある場合は `/migrate-approvals-to-beads`
  を実行して bd issue に変換。旧 JSON は `.claude/agent-org/approvals.legacy/`
  に mv される (rollback 用に保持、Phase 9 で物理削除)
- `bd config set custom.types ...` を再実行する (`/org-init` 再実行でも OK、
  idempotent)。既存 `types.custom` 設定は harmless に残るが効果なし

### 参考 PoC (U13、bd 1.0.4)

`~/.beads-poc-phase6/` で以下を実機検証してから着手:

| # | 検証項目 | 結果 | Phase 6 への影響 |
|---|---|---|---|
| V1 | `bd gate create --type=human --blocks <task>` | 動く | gate には verdict YAML / perspective を載せられないため不採用、approval issue 方式を維持 |
| V2 | `bd merge-slot create / acquire / release` | 動く (1 rig=1 slot) | Phase 6 では不要 (1 task=1 approval)、Phase 8/v2 で利用検討 |
| sup | `bd dep add <new> <old> --type supersedes` | 動く (bd 1.0.4 で公式列挙) | label fallback (`supersedes:<id>`) は採用せず `--type supersedes` で書く |

## 0.6.0 (2026-05-20) — Phase 5: beads (detection/fix) hard dependency

Phase 5 of the agent-org plugin. detection / fix の単一情報源を旧
`~/.claude/agent-org/state/<proj-hash>/{detections,fixes}/` (YAML/JSON) から
**beads (Steve Yegge 製 git-backed graph issue tracker)** に切替。bd CLI が
hard dependency になり、未 install / `.beads/` 未初期化なら subagent は
immediate error で abort する fail-closed 設計に倒した。

### Added

- `skills/using-beads/SKILL.md` (新規): bd CLI 利用規律集約。`BEADS_DIR` の
  指し方、`bd prime` 必須、query は `--json` + `jq`、`bd update --claim` atomic、
  description body は v0.5.x 互換 YAML/JSON、Bash 直接 invoke、label / priority
  規約、close 順序 (fix → detection) 等
- `commands/bd-check.md` (新規): bd CLI install / `bd doctor` / `~/.beads/<proj-hash>/`
  存在 / custom type 登録 / AGENTS.md 配置を PASS/FAIL/WARN 表示する diagnostic
- `commands/migrate-to-beads.md` (新規): v0.5.x の `detections/*.yaml` /
  `fixes/*.json` を bd issue に変換する one-shot migration。idempotent
  (`legacy-id:<basename>` label で重複検出)、`--dry-run` 対応、yq+jq+bd preflight、
  fix close 後に detection close する dep ガード遵守
- `commands/migrate-from-beads.md` (新規): rollback。bd issue を旧 YAML/JSON
  形式に書き戻す。pin from v0.7.x → v0.5.x 用、idempotent、foreground 専用
- `hooks/bd-export.sh` (新規、Stop hook): bd の open/closed issue を
  `<repo>/.beads/issues.jsonl` に export する git audit trail 補償。opt-in
  workflow (`.beads/issues.jsonl` のみ git 管理対象、`embeddeddolt/` /
  `dolt/` は gitignore)

### Changed

- `commands/org-init.md`: `~/.beads/<proj-hash>/.beads/` の `bd init`
  (`--skip-agents --non-interactive --prefix=<proj-hash>`) を追加。
  `bd config set types.custom "detection,fix,approval,episode"` で custom type
  登録 + verify (idempotent)。v0.5.x の `detections/` / `fixes/` ディレクトリは
  作成しない (bd に移行)。`.gitignore` に `agent-org plugin (v0.6.0+)` marker +
  beads 関連 3 行を idempotent 追記
- `agents/regression-watcher.md`: detection 出力先を YAML ファイルから
  `bd create -t detection` に変更。`BEADS_DIR` export 必須、`bd prime` 起動冒頭
- `agents/regression-fixer.md`: fix 入力を `bd ready -t detection`、
  `bd update --claim` で atomic 取得。完了時 `bd create -t fix` +
  `bd dep add <detection> <fix>` + `bd close <fix>` (achieved 時のみ
  detection も close、order: fix → detection)
- `commands/start-watcher.md` / `commands/fix-regression.md`: preflight に
  bd CLI / `BEADS_DIR` 健全性チェックを追加 (`/bd-check` 相当の subset)
- `hooks/hooks.json`: 既存 hooks に Stop hook (bd-export.sh) を追加
- `hooks/post-commit-trigger.sh`: `git -C <target>` で確実に worktree を指す
  ように修正 (R1 P1/P2)、TaskCompleted gate schema 修正と合わせて bug fix

### 規律 (R1 self-review で確定、Phase 6+ でも維持)

| # | 規律 | 経緯 |
|---|---|---|
| G1 | `BEADS_DIR` は `.beads/` を直接指す (親 dir は NG) | U8 (2026-05-20) |
| G2 | `bd config set types.custom` は exit=0 でも verify、未登録なら fatal exit 1 (v0.7.0 で `custom.types` に変更したが、v0.7.1 で revert: U13 PoC 結論が誤り、bd 1.0.4 実機では `types.custom` が effective、`custom.types` は無視される) | R1 P2-1 |
| G3 | description body は変数代入経由で渡す (`bd update -d "$(...)"` 直書きは禁止) | R1 P1-3 |
| G4 | migration script で skip 時も map 更新 (idempotent 再実行で fix 側 dep 解決破壊防止) | R1 P1-2 |
| G5 | preflight に `jq` install 確認必須 | R1 P2-2 |
| G6 | bd は Bash 直接 invoke、plugin slash command 経由禁止 (`--bg` で未解決) | D2 |
| G7 | hard dependency 違反は abort、runtime fallback で旧形式書き戻し禁止 (split-brain 防止) | D2 |
| G8 | `hooks.json` で同一 event 複数 hook は `hooks: [...]` 配列内に並べる | Phase 5 |

## 0.5.0 (2026-05-18) — PR 4: regression watcher + /goal fixer

Phase 4 of the agent-org plugin. background session (`--bg`) で常駐する
監視エージェント `regression-watcher` (`/loop` 駆動) と自律修復エージェント
`regression-fixer` (`/goal` 駆動)、それぞれの起動 command、commit を契機に
last-commit.json を更新する PostToolUse(Bash) hook を追加した。

`v0.5.0` をもって親プランの全コンポーネントが揃った。後続は実機運用フィードバック
を反映する細部調整を経て `v1.0.0` で機能セット凍結予定。

### Added

- `regression-watcher` subagent (`memory: user`, `model: haiku`,
  `tools: Read,Bash,Grep,Glob`): bg + `/loop` で常駐し、定期 smoke check
  (テスト / ビルド / lint) で regression を検出。検出結果を
  `~/.claude/agent-org/state/<proj-hash>/detections/<ts>.yaml` に書き出す。
  自分では修正しない (fixer に委譲)
- `regression-fixer` subagent (`memory: user`, `model: sonnet`,
  `tools: Read,Write,Edit,Bash,Grep,Glob`): bg + `/goal` で自律修復ループを
  回し、condition 達成まで修正を継続。完了時は git push + `gh pr create` /
  PR 更新 + `~/.claude/agent-org/state/<proj-hash>/fixes/<ts>.json` 書込が
  **必須手順**
- `commands/start-watcher.md` (`/start-watcher [interval]`): foreground
  preflight (gh auth / git remote / claude CLI / python3) を実行後、
  `claude --agent agent-org:regression-watcher --bg "/loop <interval> smoke
  check"` を発射。preflight 失敗時は `--bg` を起動せずセットアップ手順を
  案内
- `commands/fix-regression.md` (`/fix-regression <target> [condition] [--turn-cap N]`):
  foreground preflight (gh auth / git remote / 作業ツリー clean / branch
  衝突チェック / gh repo view 疎通) を実行後、`claude --agent
  agent-org:regression-fixer --bg '/goal <condition> or stop after N turns'`
  を発射。warnings は表示するが起動可能、errors では `--bg` 起動を中止
- `hooks/post-commit-trigger.sh` (PostToolUse Bash matcher): `tool_input.command`
  に `git commit` を含み `exit_code=0` の場合に、cwd を canonicalize+sha256 して
  proj-hash を計算し `~/.claude/agent-org/state/<proj-hash>/last-commit.json`
  を更新。watcher が次 `/loop` iteration で新規 commit 以降の変更を起点に
  smoke check できるようにする。fail-open + jq/python3/shasum/sha256sum
  fallback chain で堅牢化
- `hooks/hooks.json`: 既存 PostCompact + Stop + TaskCompleted に
  PostToolUse(Bash matcher) を追加

### Worktree 隔離と統合経路

`claude --bg` で起動された session は working directory 配下への書込が
`.claude/worktrees/<id>/` に**自動隔離**される (公式 docs `agent-view`)。
この影響を回避するため:

- watcher / fixer の **memory は `user` scope** (`~/.claude/agent-memory/`)
  に置き、worktree 隔離の対象外にする
- watcher の **detection state** は `~/.claude/agent-org/state/<proj-hash>/`
  (working dir 外) に置く
- fixer の **修正成果統合は git remote 経由** (`git push` + `gh pr create`/
  update)。worktree 隔離はローカル書込のみに影響し、git remote 操作は
  影響を受けない
- fixer は完了時に PR URL を `~/.claude/agent-org/state/<proj-hash>/fixes/
  <ts>.json` に記録し、main session が `gh pr view <URL>` で内容確認できる

### cross-project 混入対策

`memory: user` の subagent (watcher / fixer) は全プロジェクト共通の memory
領域を使うため、`MEMORY.md` を `## Project: <proj-hash>` セクションで
分離する規律を subagent prompt に明記。重いプロジェクト固有学習は
`~/.claude/agent-org/state/<proj-hash>/learnings/<agent-name>.md` に分離可能。

### /goal 暴走ガード

公式 docs に `/goal` の数値 hard cap は存在しない。実装は condition 末尾に
**必ず `or stop after N turns`** 句を含めることで bound する設計。
`/fix-regression` command は target 規模に応じた default turn-cap
(25 / 50 / 80) を持ち、`--turn-cap N` で明示上書き可。上限 100 を超えない。

### Schemas

- last-commit.json (`~/.claude/agent-org/state/<proj-hash>/last-commit.json`)
  の schema_version=1 を導入 (`commit_sha` / `branch` / `committed_at` /
  `cwd` / `project_hash` / `triggered_by` / `command_excerpt`)
- detection YAML (`~/.claude/agent-org/state/<proj-hash>/detections/<id>.yaml`)
  の schema を確定 (詳細は `agents/regression-watcher.md`)
- fix state JSON (`~/.claude/agent-org/state/<proj-hash>/fixes/<id>.json`)
  の schema_version=1 を導入 (`fix_id` / `branch` / `pr_url` / `commits` /
  `goal_status` / `turns_used` / `summary` 等)

### Notes

- Phase 4 で追加した hook (PostToolUse Bash) は fail-open ベース
  (jq/python3 不在 / 入力 parse 失敗 / hash 計算手段不在で exit 0)
- `--agent` には必ず **scoped name** (`agent-org:regression-watcher` /
  `agent-org:regression-fixer`) を渡す。plain name だと plugin agent が
  解決されず default session に fallback する罠あり (ADR-002→003 で実証済)
- `--bg` 起動 session は permission prompt を出せず auto-deny されるため、
  起動前の foreground preflight が必須
- ADR 化推奨項目:
  - 「fixer 成果統合は git remote 経由」設計の根拠
  - `memory: user` cross-project 混入対策 (project セクション分離)
  - `<proj-hash>` 生成元を `$CLAUDE_PROJECT_DIR` ではなく hook input cwd
    (canonicalize 後) から取る方針
  - **ADR-004 (候補)**: `memory: user` scope の plugin subagent も `memory:
    project` (ADR-003) と同じく scoped name dir
    (`<plugin>-<agent>/`) に解決される (2026-05-18 PoC で実証、下記
    Verification 参照)

### Verification (実機検証 2026-05-18)

実装後の hook 単体テスト + subagent PoC で以下を確認・修正した。

**Hook 単体テスト**:

- `post-commit-trigger.sh`: 模擬入力で 8 ケース検査 (基本 commit / chained `;` /
  chained `&&` / `git -C path commit` / 非 git command / failed commit /
  非 Bash tool / echo 内の偽 git commit)
- `task-completed-gate.sh`: 5 ケース検査 (review_required=false / approval 不在
  / approved / rejected / conditional)
- `stop-quality-gate.sh`: 7 ケース検査 (config 無し / 再入ガード / failing
  required / failing non-required / passing / approvals_clean reject /
  approvals_clean clean)

**実装後に発見・修正したバグ 3 件**:

1. `stop-quality-gate.sh`: `jq -r '.required // true'` が boolean `false` を
   null と同等に扱う仕様で `false || true = true` に化けて required 扱い化。
   `if has("required") then .required else true end` に修正して boolean
   `false` を真の false として保持
2. `stop-quality-gate.sh`: `wc -l` 出力末尾の改行を `tr -d ' '` が除去せず
   `"0\n" != "0"` で false-positive BLOCK 発生。`tr -d '[:space:]'` +
   empty チェックに修正
3. `post-commit-trigger.sh`: regex separator `[[:space:];&|\`]` が
   `"; git commit"` のような separator + space + git のパターンを捕捉
   できず chained command で false-negative。`tr ';|&' '\n'` で chained
   command を行に分割してから検査する形に整理

**Plugin subagent memory PoC** (plan 残不確実性 #1 完全解消):

`claude --plugin-dir ./plugins/agent-org --agent agent-org:regression-watcher
-p "..."` で起動した subagent の system prompt の Persistent Agent Memory
パスを実機確認:

```
/Users/mao/.claude/agent-memory/agent-org-regression-watcher/
```

`memory: user` scope でも plugin scoped name (`<plugin>-<agent>/`) で
解決されることが確定 (`memory: project` は Phase 1 PoC で既に同様確認済)。
旧 plain name dir (`regression-watcher/`) は使われない。ADR-003 の scoped
name dir 採用判断が `user` scope にも適用できる。

**未検証 (実運用時に PoC 予定)**:

- `claude --bg --agent agent-org:<name> "/loop ..."` の supervisor process
  立ち上げ + iteration 動作
- `claude --bg --agent agent-org:regression-fixer "/goal ..."` の評価ループ
- `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` での running-review teammate
  並列 spawn
- `--bg` 内での `gh auth status` 有効性 (preflight で緩和済)

### Codex review 対応 (R1, 2026-05-19, 同 PR 内)

PR #16 への `@codex review` で 2 件の指摘を受領 (P1 / P2) し、同 PR 内で修正:

- **P1 (`task-completed-gate.sh`)**: 公式 TaskCompleted hook schema を
  doc-researcher で verbatim 再確認 (`code.claude.com/docs/en/hooks.md`
  Hook events/TaskCompleted)。input JSON は **全 field が top-level フラット**
  (`task_id` / `task_subject` / `task_description` / `teammate_name` /
  `team_name`) で、`task` / `metadata` / `review_required` のような nested
  設定 field は schema 上**存在しない**。既存の `.task.metadata.review_required`
  / `.metadata.review_required` を見る実装は常に false に解決され gate が
  完全 no-op だった。**設計を approval JSON opt-in に変更**: top-level
  `task_id` を取って `.claude/agent-org/approvals/<task_id>.json` を探し、
  - 不在 → pass (gate skip、通常 task)
  - `approval_status=approved` / `conditional` → pass
  - `approval_status=rejected` → exit 2 で block
  という規則。「review を必須にしたい」場合は単に `/run-review <task_id>` を
  回すだけで opt-in できる。README / ARCHITECTURE / `commands/run-review.md`
  の関連説明も更新
- **P2 (`post-commit-trigger.sh`)**: `git -C <path> commit` を検出した際に
  HEAD/branch を hook input cwd から読んでいたため、別 repo の commit が
  current project の `last-commit.json` を誤って上書きする可能性があった。
  command から `git -C <path>` の path を grep + sed で抽出し、target dir
  として canonicalize (relative path は cwd 基準で resolve、resolve 失敗時
  は cwd にフォールバック)。target dir をベースに proj-hash 計算と
  `git rev-parse` を実行し、別 repo の commit は別 proj-hash の
  `last-commit.json` に書き分ける。last-commit.json schema に
  `hook_cwd` field を追加 (cwd と hook input cwd の関係を保存)

### R1 検証

- task-completed-gate.sh 単体テスト 7 ケース (Case A: task_id 不在 →
  fail-open, B: approval 不在 → pass, C: approved → pass, D: rejected →
  block, E: conditional → pass with warn, F: 古い payload 形式
  (`.task.id` のみ) → fail-open, G: teammate_name 含む正規 payload → pass)
  すべて期待通り
- post-commit-trigger.sh 単体テスト 5 ケース (Case A: cwd 内通常 commit,
  B: `git -C OTHER_REPO commit` → OTHER に書き cwd には触らない,
  C: `git -C ./sub commit` (relative) → SUB に書く, D: chained 検出維持
  (regression なし), E: 存在しない path → cwd フォールバック) すべて期待通り

## 0.4.0 (2026-05-18) — PR 3: authority / review + quality gates

Phase 3 of the agent-org plugin. 真 RO `architect-reviewer` subagent と、
複数視点で並列レビューを実行する `running-review` skill、verdict 集約
+ approval JSON 書込を担う `/run-review` command、TaskCompleted hook での
review-required ゲート、Stop hook での quality-gates.json ベースのゲートを
追加した。

### Added

- `architect-reviewer` subagent (`memory: project`, `model: sonnet`,
  `tools: Read,Glob,Grep` の **真 RO**): 渡された PR / 設計 / 実装を
  multi-perspective でレビューし、verdict YAML を会話に返す。ファイル書込
  は呼び出し側 command が責任を持つ設計 (監査面で reviewer の権限を最小化)
- `running-review` skill: `architect-reviewer` を 3-5 perspective で並列
  spawn する手順を提供。default は agent teams 経路
  (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`)、未設定環境では Task tool で
  sequential invoke する fallback も明記
- `/run-review <task-id> [perspectives]` slash command: `running-review`
  skill 起動 + verdict 集約 + `.claude/agent-org/approvals/<task-id>.json`
  への書込まで一括処理。`aggregate_overall` / `min_confidence` /
  `concerns_summary` を集計し `approval_status` (`approved` / `conditional` /
  `rejected`) を決定
- `hooks/task-completed-gate.sh` (TaskCompleted hook): matcher 非対応・全件
  発火のため hook 内部で `task.metadata.review_required` を確認し、true なら
  approval JSON の `approval_status` を検査。`approved` / `conditional` で
  pass、`rejected` または approval JSON 不在で exit 2 (block)
- `hooks/stop-quality-gate.sh` (Stop hook): `.claude/agent-org/quality-gates.json`
  が存在する場合に各 gate を実行。`kind: command` (任意 shell)、
  `kind: approvals_clean` (approvals dir の rejected を検査) をサポート。
  `required: true` の failing で exit 2、`required: false` は warn のみ。
  `stop_hook_active=true` で即座に抜けて無限ループ回避
- `hooks/hooks.json`: 既存 PostCompact に Stop / TaskCompleted 登録を追加

### Schemas

- approval JSON (`.claude/agent-org/approvals/<task-id>.json`) の schema_version=1
  を導入 (`schema_version` / `task_id` / `target` / `aggregate_overall` /
  `approval_status` / `concerns_summary` / `verdicts[]` 等)。詳細は
  `commands/run-review.md` 参照
- quality-gates.json (`.claude/agent-org/quality-gates.json`) の schema を
  導入 (`gates[]` + `kind` + `required`)。詳細は `hooks/stop-quality-gate.sh`
  ヘッダコメント参照

### Notes

- Phase 3 で追加した hook は全て fail-open ベース (jq 不在 / 入力 parse 失敗
  / config 不在で exit 0)。明示的に `exit 2` を返すのは「gate が確実に
  failing と判明した」ケースのみ
- agent teams は experimental 機能。reviewer spawn の安定性は本番運用で要検証
- reviewer は `tools: [Read, Glob, Grep]` で固定、write 系 tool 無し
  (plugin subagent では `permissionMode` 指定不可のため tools allowlist で
  代替している)
- ADR 化推奨項目:
  - 真 RO reviewer + 呼び出し側 command による write 分離設計の根拠
    (Codex #2 修正の背景)
  - approval JSON schema v1 の確定
  - quality-gates.json schema v1 の確定

## 0.3.0 (2026-05-17) — scoped name dir adoption (BREAKING)

**Breaking change**: 全 plugin subagent の memory dir 命名を **scoped name**
(`agent-org-<agent-name>/`、`:` を `-` に置換した命名) に統一。Claude Code
v2.1.33+ の subagent memory auto-inject 機能を活用する設計に倒した
(ADR-003 参照)。

### Background

2026-05-17 の PoC で v0.2.1 (ADR-002) の前提が覆った。`claude --plugin-dir
./plugins/agent-org --agent agent-org:decision-keeper -p ...` で plugin
subagent を main session として起動した場合、scoped name dir
(`.claude/agent-memory/agent-org-decision-keeper/`) に置いた MEMORY.md
先頭 200 行/25 KB がシステムプロンプトに **正常に auto-inject される**ことを
確認 (subagent システムプロンプトに `Persistent Agent Memory:
...agent-org-<name>/` のパスが明示注入される)。

ADR-002 の検証ミスの原因は `--plugin-dir` フラグなしで subagent invocation を
試していたこと。plugin が解決失敗し、デフォルト Claude (main session agent)
として fallback 起動するため、subagent memory 機能が発火しない罠があった。

### Changed (Breaking)

- 全 subagent の memory dir 解決先を scoped name dir に統一:
  - `decision-keeper`: `.claude/agent-memory/agent-org-decision-keeper/`
  - `context-compressor`: `.claude/agent-memory/agent-org-context-compressor/`
  - `architect-reviewer` (Phase 3 で使用): `.claude/agent-memory/agent-org-architect-reviewer/`
  - `regression-watcher` (Phase 4 で使用): `~/.claude/agent-memory/agent-org-regression-watcher/`
  - `regression-fixer` (Phase 4 で使用): `~/.claude/agent-memory/agent-org-regression-fixer/`
- `agents/decision-keeper.md`: auto-inject 前提に書き直し。
  - 明示 Read 指示を削除、auto-inject される MEMORY.md の `next_adr_sequence`
    から連番を取得する規律に変更
  - MEMORY.md は ADR index + 連番カウンタのみ、本文は個別
    `ADR-<id>-<slug>.yml` ファイルに分離 (auto-inject 範囲を圧縮)
- `agents/context-compressor.md`: auto-inject 前提に書き直し。memory path を
  scoped name dir に統一
- `skills/recording-decision/SKILL.md`: 明示注入指示
  (「既存 ADR 連番を Read で取得して prompt に含める」) を削除。subagent が
  auto-inject 経由で連番を取得する設計に統一
- `skills/consulting-memory/SKILL.md`: path 規約表を scoped name に統一、
  plain name 言及を削除
- `skills/compressing-context/SKILL.md`: subagent_type を
  `agent-org:context-compressor` (scoped name) に明示
- `commands/org-init.md`: mkdir 対象を scoped name dir に統一、v0.2.x からの
  データ移行手順を追記
- `docs/ARCHITECTURE.md`: Phase 1/2 の全 path 規約表・mermaid 図を scoped name に更新

### Migration from 0.2.x

`/org-init` を再実行すると新しい scoped dir が作成される。旧 plain dir に
蓄積した MEMORY.md / ADR ファイルは自動移行されないため、`commands/org-init.md`
の「v0.3.0 移行時の注意」セクションに記載の `mv` コマンドで移行すること:

```bash
mv .claude/agent-memory/decision-keeper/* \
   .claude/agent-memory/agent-org-decision-keeper/ 2>/dev/null || true
mv .claude/agent-memory/context-compressor/* \
   .claude/agent-memory/agent-org-context-compressor/ 2>/dev/null || true
rmdir .claude/agent-memory/decision-keeper 2>/dev/null || true
rmdir .claude/agent-memory/context-compressor 2>/dev/null || true
```

### Notes

- 0.2.1 (ADR-002) で採用した「明示注入設計」は ADR-003 で supersede
- Phase 番号体系を更新: Phase 2.5 (v0.3.0) として scoped name dir 統一を
  挿入。Phase 3 (v0.4.0) authority/review、Phase 4 (v0.5.0) regression、
  v1.0.0 で全機能揃った時点で bump
- PoC 詳細とその後の判断は ADR-003 (`.claude/agent-memory/
  agent-org-decision-keeper/ADR-003-scoped-name-dir-adoption.yml`) 参照

## 0.2.1 (2026-05-16) — Phase 2 verification followup

Phase 2 検証 (ADR-001) で plugin agent の memory dir 命名規則を実機確認した
結果、Claude Code フレームワーク側は scoped name `agent-org:<agent>` の `:`
を `-` に置換した dir (`agent-org-decision-keeper/` 等) を auto-create する
一方、subagent は SKILL.md 指示通り plain name dir (`decision-keeper/` 等) に
書き込むため、**2 dir 並存と auto-inject 失敗の不整合**が判明した (ADR-002)。
当面 plain name dir を subagent 書込先として維持し、skill 側で明示的に過去
情報を prompt 注入する設計に倒す。

### Changed

- `agents/decision-keeper.md`: auto-inject されない前提で動く旨を明記、
  起動時の `MEMORY.md` Read を必須化
- `skills/recording-decision/SKILL.md`: 注意事項に「skill 経由起動時は
  既存 ADR 連番を必ず prompt に含めること」を追記
- `skills/consulting-memory/SKILL.md`: 注意事項に「plain name dir を必ず
  Read で読みに行く必要」を追記
- `commands/org-init.md`: 注意事項に「フレームワーク側 scoped name dir の
  並存」を追記

### Notes

- Phase 1 の `context-compressor` にも同種の不整合が潜在 (`agent-org-context-
  compressor/` がフレームワーク auto-create される)。当面同じ「skill 経由で
  明示的に文脈注入」設計で動かす
- フレームワーク命名 (`agent-org-<name>`) への全面移行 (Phase 1+2 の全 dir
  パスを書き換える破壊的変更) は v0.3.0 で別途検討
- 検証結果と設計判断は ADR-001 / ADR-002 として `.claude/agent-memory/
  decision-keeper/` に保存 (worktools repo の commit 対象外、`.claude/` 配下)

## 0.2.0 (2026-05-16) — PR 2: decision-keeper

Phase 2 of the agent-org plugin. ADR (Architecture Decision Record) を構造化
形式で蓄積する `decision-keeper` subagent と、必要なディレクトリを冪等に
初期化する `/org-init` command を追加。

### Added

- `decision-keeper` subagent (`memory: project`, `model: sonnet`,
  `tools: Read,Write,Edit,Grep,Glob`): 設計判断を ADR YAML として
  `.claude/agent-memory/decision-keeper/MEMORY.md` に immutable に追記。
  `status: superseded_by:<id>` 更新のみ既存 ADR への許容操作
- `recording-decision` skill: decision-keeper を Task ツール経由で
  invoke する手順を提供 (`agent-org:decision-keeper` scoped name)
- `consulting-memory` skill: 別 subagent の `MEMORY.md` / learnings を
  Read で取り込む横断参照スキル。memory scope (project/user/local) ごとの
  パス規約を提供
- `/org-init` slash command: agent-org plugin が使うディレクトリ群
  (`.claude/agent-memory/{各 agent}/`, `.claude/episodes/`,
  `.claude/agent-org/approvals/`, `~/.claude/agent-memory/{各 agent}/`,
  `~/.claude/agent-org/state/<proj-hash>/{detections,fixes,learnings}/`)
  を冪等に作成

### Notes

- `<proj-hash>` は cwd を canonicalize して sha256 した先頭 8 桁。複数
  プロジェクトを跨いでも cross-session state が混じらない識別子
- decision-keeper は scope `project` で repo 内に蓄積、main session で
  foreground 動作するため worktree 隔離の影響を受けない
- Phase 4 で使う `~/.claude/agent-memory/regression-{watcher,fixer}/` も
  `/org-init` 時に先行作成 (Phase 4 で個別に作成しなくて済む)
- subagent memory の plugin scoped name (`agent-org:decision-keeper`) で
  どの memory dir が解決されるかは Phase 2 着手以降の実機検証で確認予定

## 0.1.0 (2026-05-13) — PR 1: plugin skeleton + context-compressor

Phase 1 of the agent-org plugin. AI organizational engineering toolkit の
最小骨格。後続 Phase で decision-keeper / architect-reviewer / regression-watcher
/ regression-fixer を追加予定。

### Added

- `context-compressor` subagent (`memory: project`, `model: haiku`): 直近会話を
  `.claude/episodes/<id>.yaml` に構造化圧縮する専用 agent
- `compressing-context` skill: context-compressor を invoke する手順を提供
- `/compress-context` slash command: skill を起動するエントリポイント
- PostCompact hook (`hooks/postcompact-episode.sh`): 通常の compact 実行後に
  `compact_summary` を `.claude/episodes/compact-<ts>.yaml` に転写。
  `compact_summary` フィールド優先、空・欠落時は `transcript_path` を JSONL
  parse する fallback ロジック付き

### Notes

- Phase 1 では `agent-org` directory (`.claude/agent-org/`) は使わず、
  `.claude/episodes/` のみ。後続 Phase で approvals / state を追加
- `memory: project` は repo 内 `.claude/agent-memory/context-compressor/` に書く
  ため、`--bg` で起動された場合 worktree 隔離の影響を受けることに注意
  (Phase 1 では `--bg` 起動シナリオなし)
- PostCompact hook の入力 schema は公式 docs (`doc-researcher` skill で verbatim
  確認済み): `trigger` + `compact_summary` の 2 フィールド + common fields
