---
description: "[DEPRECATED v0.11.0] compressing-context skill の thin wrapper"
---

# /compress-context (deprecated)

> **v0.11.0 (v2 skill 統合)**: この command は元々 `compressing-context` skill の
> thin wrapper。v2.0.0 で削除予定。

## 引数

圧縮対象の範囲を自然言語で指定可能 (例: 「直近 50 turn」「Phase 1 設計の議論」)。
省略時は context-compressor が自然な区切りを判断。

## 実行内容

1. `compressing-context` skill を起動する (skill が subagent 起動 → episode 生成まで完結)

## 関連

- skill (本体): `skills/compressing-context/SKILL.md`
- subagent: `agents/context-compressor.md`
