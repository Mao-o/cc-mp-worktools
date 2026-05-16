# Changelog

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
