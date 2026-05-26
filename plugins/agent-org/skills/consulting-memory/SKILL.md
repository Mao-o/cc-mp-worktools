---
name: consulting-memory
description: |
  agent-org subagent の蓄積知識（ADR・verdict・episode）を参照するスキル。
  subagent memory は agent 間で isolation されているため、scoped name dir
  規約を知る本スキル経由でないと正確なパスが解決できない。
  ADR の確認・過去の判断の参照には直接ファイル検索ではなくこのスキルを使うこと。
  Use proactively when: ADR を確認したい、過去の判断を振り返りたい、
  subagent の記憶を参照したい時。
  Triggers: consulting-memory, ADR を確認, ADR を読む, 過去の判断,
  判断履歴, 前の決定, subagent の memory, MEMORY.md 横断参照,
  他 agent の memory 参照, agent memory 共有
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

2. **memory scope に応じて scoped name dir のパスを計算する**

   Claude Code は plugin subagent の memory dir を **scoped name** で解決する
   (`<plugin-name>:<agent-name>` の `:` を `-` に置換した命名)。
   agent-org plugin の場合、すべて `agent-org-<agent-name>/` 形式になる
   (ADR-003 採用判断、v0.3.0)。

   | scope | パス |
   |---|---|
   | `project` | `.claude/agent-memory/agent-org-<agent-name>/MEMORY.md` |
   | `user` | `~/.claude/agent-memory/agent-org-<agent-name>/MEMORY.md` |
   | `local` | `.claude/agent-memory-local/agent-org-<agent-name>/MEMORY.md` |

   agent-org plugin の各 subagent の scope:

   | agent | scope | パス |
   |---|---|---|
   | `decision-keeper` | `project` | `.claude/agent-memory/agent-org-decision-keeper/MEMORY.md` |
   | `architect-reviewer` | `project` | `.claude/agent-memory/agent-org-architect-reviewer/MEMORY.md` |
   | `context-compressor` | `project` | `.claude/agent-memory/agent-org-context-compressor/MEMORY.md` |
   | `regression-watcher` | `user` | `~/.claude/agent-memory/agent-org-regression-watcher/MEMORY.md` |
   | `regression-fixer` | `user` | `~/.claude/agent-memory/agent-org-regression-fixer/MEMORY.md` |

3. **Read tool で `MEMORY.md` を読む**
   - ファイルが存在しない場合は subagent がまだ起動されていない / 何も
     書いていない状態。空として扱う

4. **decision-keeper の場合は個別 ADR yml も併読する**
   - MEMORY.md の index で関連 ADR を特定したら、本文を
     `.claude/agent-memory/agent-org-decision-keeper/ADR-<id>-<slug>.yml`
     で Read する

5. **project 固有 learnings がある場合は併読する**
   - パス: `~/.claude/agent-org/state/<proj-hash>/learnings/<agent-name>.md`
   - `<proj-hash>` は `/org-init` 時に計算した値 (cwd を canonicalize して
     sha256 した先頭 8 桁)
   - `memory: user` の subagent が project 固有学習を MEMORY.md から分離して
     書く場所 (cross-project 混入対策)

6. **関連 episode を Grep で検索する**
   - パス: `.claude/episodes/*.yaml`
   - 検索キーワードは MEMORY.md 内の `retrieval_keys` をヒントに選ぶ
   - ADR archive (`adr-archive-*.yaml`) もここに含まれる

7. **bd 上の cross-session learning を取り込む** (Phase 7+, v0.10.0)
   - v0.10.0 (ADR-010) から 4 subagent (`architect-reviewer` /
     `regression-fixer` / `regression-watcher` / `decision-keeper`) は
     `learnings_to_persist:` を会話出力 YAML として返し、各 handler が
     `bd remember "<prefix>: <summary>" --key <prefix>-<slug>` で永続化する
   - `bd prime` の default 挙動により、subagent 起動冒頭で memory が
     **auto-inject される**。同セッション内で明示 retrieval する必要は
     原則ない (詳細は `using-beads` skill の `bd prime` section 参照)
   - **明示 retrieval が必要なケース**:
     - **別の prefix の learning** を検索したい時 (例: fixer 内で過去
       reviewer が残した `review-heuristic-*` を参照したい)
     - **bd prime の inject 範囲を超えた古い learning** を取りに行きたい時
     - **main session** から特定 key を深掘りしたい時

   ```bash
   # 全 memory を list (key と summary のみ)
   REPO_ROOT="$(git rev-parse --show-toplevel)"
   (cd "$REPO_ROOT" && bd memories)

   # prefix で絞り込み (例: review-heuristic-*)
   (cd "$REPO_ROOT" && bd memories review-heuristic)

   # phrase 検索 (空白区切りの語を含む memory)
   (cd "$REPO_ROOT" && bd memories "JSONL parse fallback")

   # 個別 key をフル取得
   (cd "$REPO_ROOT" && bd recall fix-pattern-jsonl-parse-eof)
   ```

   - **`bd memories <keyword>`**: list / 検索。summary だけ返るので overview に最適
   - **`bd recall <key>`**: 単発 fetch。検索結果の中から特定 key の本文を読みたい時のみ
   - **無期限保持** (bd default、ADR-010)、`bd forget <key>` で明示削除

### key 命名規約 (ADR-010、Phase 7+ で 4 subagent 全部に展開済)

prefix で書き手 subagent を判別できる規約。`bd memories <prefix>` で絞込検索する想定。

| subagent | key prefix | 例 | 書き方 |
|---|---|---|---|
| `architect-reviewer` | `review-heuristic-` | `review-heuristic-mock-only-tests` | verdict YAML の `learnings_to_persist`、`/run-review` が `bd remember` する |
| `regression-fixer` | `fix-pattern-` | `fix-pattern-jsonl-parse-eof` | 完了 report の `learnings_to_persist`、`/fix-regression` が `bd remember` する |
| `regression-watcher` | `watch-heuristic-` / `false-positive-` | `watch-heuristic-go-test-short-flag` | subagent prompt 内で **Bash 直接** `bd remember`、handler 経由しない (`--bg` 常駐性質) |
| `decision-keeper` | `decision-meta-` | `decision-meta-supersede-pattern` | 会話出力の `learnings_to_persist`、`recording-decision` skill が `bd remember` する |

`<slug>` は kebab-case、英数字 + ハイフンのみ。同 key 再 `bd remember` で update
in place (bd 1.0.4 仕様)。

## 典型ケース

### architect-reviewer から decision-keeper の ADR を参照したい

```
1. Read .claude/agent-memory/agent-org-decision-keeper/MEMORY.md
2. index で関連 ADR を特定
3. Read .claude/agent-memory/agent-org-decision-keeper/ADR-<id>-<slug>.yml
4. 関連 episode を .claude/episodes/ で Grep
5. 必要なら archived ADR (.claude/episodes/adr-archive-*.yaml) を Read
```

### regression-fixer から過去の同種 fix を参照したい

```
1. Read ~/.claude/agent-memory/agent-org-regression-fixer/MEMORY.md
2. Read ~/.claude/agent-org/state/<proj-hash>/learnings/regression-fixer.md
3. Glob で ~/.claude/agent-org/state/<proj-hash>/fixes/*.json を取得、
   直近 fix の PR URL を取り出す
```

### メインセッションから過去 ADR を確認したい

```
1. Read .claude/agent-memory/agent-org-decision-keeper/MEMORY.md (index)
2. 関連 ADR を ADR-<id>-<slug>.yml で Read
3. Grep で .claude/episodes/adr-archive-*.yaml も検索 (archived ADR)
```

## 注意事項

- **agent-org plugin の subagent は全て scoped name dir** (`agent-org-<name>/`)
  に書く設計 (ADR-003 で採用、v0.3.0 から)。Claude Code v2.1.33+ の
  auto-inject はこの scoped name dir の MEMORY.md を参照するため、
  consulting-memory も同じ dir を Read する
- `MEMORY.md` の先頭 **200 行または 25 KB (先に達した方)** は subagent 起動時に
  **auto-inject** される (Claude Code v2.1.33+ の仕様)。明示的に Read する必要が
  あるのは以下のケース:
  1. **別の subagent の memory** (自分の memory ではない)
  2. **200 行 / 25 KB を超えた範囲**
  3. **MEMORY.md 以外のファイル** (個別 ADR yml, learnings.md, episode YAML,
     ADR archive 等)
- 大量に Read すると context を消費するため、retrieval_keys で範囲を絞る
- decision-keeper の ADR を参照する場合、`status: superseded_by:<id>` で
  置き換えられた古い ADR を誤って引用しないよう、必ず `status` を確認する

## 関連

- decision-keeper: `agents/decision-keeper.md`
- context-compressor: `agents/context-compressor.md`
- episode 蓄積場所: `.claude/episodes/`
- `<proj-hash>` 計算方法: `commands/org-init.md`
