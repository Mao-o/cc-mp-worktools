# Changelog

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
