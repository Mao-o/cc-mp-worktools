---
name: consulting-memory
description: |
  他の subagent が蓄積した `MEMORY.md` (および project 固有 learnings) を
  読みに行くスキル。Claude Code の subagent memory は agent 間で
  isolation されているため、横断参照したい場合は Read tool で対象
  MEMORY.md を明示的に取り込む必要がある。
  Use when: 別 subagent の蓄積知識 (例: decision-keeper の ADR、
  architect-reviewer の verdict 履歴) を現在のコンテキストに取り込みたい。
  Triggers: consulting-memory, 他 agent の memory 参照, MEMORY.md 横断参照,
  他 subagent の決定参照, agent memory 共有, ADR を読む
---

# Consulting Memory Skill

別 subagent の `MEMORY.md` を Read で取り込むスキル。subagent memory は
agent ごとに isolation されているため、横断参照には明示的なファイル読み込みが必要。

## 起動条件

以下のいずれかが該当する時:

- 別 subagent (例: decision-keeper) が蓄積した知識を現在の subagent context
  に取り込みたい
- メインセッションから過去の ADR / verdict / 圧縮 episode を再確認したい
- 複数 subagent の意思決定履歴を横断的に検索したい

## 手順

1. **参照対象の agent name を確認する**
   - 例: `decision-keeper`, `architect-reviewer`, `context-compressor`,
     `regression-watcher`, `regression-fixer`

2. **memory scope に応じてパスを計算する**

   | scope | パス |
   |---|---|
   | `project` | `.claude/agent-memory/<agent-name>/MEMORY.md` |
   | `user` | `~/.claude/agent-memory/<agent-name>/MEMORY.md` |
   | `local` | `.claude/agent-memory-local/<agent-name>/MEMORY.md` |

   agent-org plugin の各 subagent の scope:

   | agent | scope | パス |
   |---|---|---|
   | `decision-keeper` | `project` | `.claude/agent-memory/decision-keeper/MEMORY.md` |
   | `architect-reviewer` | `project` | `.claude/agent-memory/architect-reviewer/MEMORY.md` |
   | `context-compressor` | `project` | `.claude/agent-memory/context-compressor/MEMORY.md` |
   | `regression-watcher` | `user` | `~/.claude/agent-memory/regression-watcher/MEMORY.md` |
   | `regression-fixer` | `user` | `~/.claude/agent-memory/regression-fixer/MEMORY.md` |

3. **Read tool で `MEMORY.md` を読む**
   - ファイルが存在しない場合は subagent がまだ起動されていない / 何も
     書いていない状態。空として扱う

4. **project 固有 learnings がある場合は併読する**
   - パス: `~/.claude/agent-org/state/<proj-hash>/learnings/<agent-name>.md`
   - `<proj-hash>` は `/org-init` 時に計算した値 (cwd を canonicalize して
     sha256 した先頭 8 桁)
   - `memory: user` の subagent が project 固有学習を MEMORY.md から分離して
     書く場所 (cross-project 混入対策)

5. **関連 episode を Grep で検索する**
   - パス: `.claude/episodes/*.yaml`
   - 検索キーワードは MEMORY.md 内の `retrieval_keys` をヒントに選ぶ
   - ADR archive (`adr-archive-*.yaml`) もここに含まれる

## 典型ケース

### architect-reviewer から decision-keeper の ADR を参照したい

```
1. Read .claude/agent-memory/decision-keeper/MEMORY.md
2. 関連 ADR の retrieval_keys を確認して grep で広げる
3. 関連 episode を .claude/episodes/ で Grep
4. 必要なら archived ADR (.claude/episodes/adr-archive-*.yaml) を Read
```

### regression-fixer から過去の同種 fix を参照したい

```
1. Read ~/.claude/agent-memory/regression-fixer/MEMORY.md
2. ~/.claude/agent-org/state/<proj-hash>/learnings/regression-fixer.md を Read
3. ~/.claude/agent-org/state/<proj-hash>/fixes/*.json を Glob して直近 fix の
   PR URL を取得
```

### メインセッションから過去 ADR を確認したい

```
1. Read .claude/agent-memory/decision-keeper/MEMORY.md (現役 ADR)
2. retrieval_keys から該当を絞る
3. Grep で .claude/episodes/adr-archive-*.yaml も検索 (archived ADR)
```

## 注意事項

- `MEMORY.md` の先頭 **200 行または 25 KB (先に達した方)** は subagent 起動時に
  **auto-inject** される (Claude Code v2.1.33+ の仕様)。明示的に Read する必要が
  あるのは以下のケース:
  1. **別の subagent の memory** (自分の memory ではない)
  2. **200 行 / 25 KB を超えた範囲**
  3. **MEMORY.md 以外のファイル** (learnings.md, episode YAML, ADR archive 等)
- 大量に Read すると context を消費するため、retrieval_keys で範囲を絞る
- decision-keeper の ADR を参照する場合、`status: superseded_by:<id>` で
  置き換えられた古い ADR を誤って引用しないよう、必ず `status` を確認する

## 関連

- decision-keeper: `agents/decision-keeper.md`
- context-compressor: `agents/context-compressor.md`
- episode 蓄積場所: `.claude/episodes/`
- `<proj-hash>` 計算方法: `commands/org-init.md`
