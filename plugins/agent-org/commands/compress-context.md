---
description: 直近の会話セグメントを構造化 episode YAML に圧縮して .claude/episodes/ に保存
---

`compressing-context` skill を起動し、直近のメインセッション会話を構造化
episode YAML に圧縮して `.claude/episodes/<id>.yaml` に保存する。

引数 (任意):
- 圧縮対象の範囲を自然言語で指定可能 (例: 「直近 50 turn」「Phase 1 設計の議論」)
- 省略時は context-compressor が自然な区切りを判断

実行内容:
1. `compressing-context` skill を invoke
2. skill 内部で `agent-org:context-compressor` subagent を Task ツール経由で起動
3. context-compressor が直近セグメントを読んで episode YAML を生成
4. `.claude/episodes/<id>.yaml` に保存
5. 結果 (episode id / topic / 保存パス) をメインセッションに通知

詳細は `compressing-context` skill (`skills/compressing-context/SKILL.md`)
および `context-compressor` subagent (`agents/context-compressor.md`) を参照。
