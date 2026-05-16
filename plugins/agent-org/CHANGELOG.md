# Changelog

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
