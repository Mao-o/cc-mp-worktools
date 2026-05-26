---
description: "[DEPRECATED v0.11.0] fixing-regression skill の thin wrapper。preflight・launch・persist ロジックは skill 側に統合済み"
---

# /fix-regression (deprecated)

> **v0.11.0 (v2 skill 統合)**: preflight・`--bg` 起動・learnings persist は
> `fixing-regression` skill に統合済み。この command は引数パースと skill 起動の
> thin wrapper として残す。v2.0.0 で削除予定。

## 引数

```text
/fix-regression [target] [condition] [--turn-cap N]
```

| 引数 | 説明 |
|---|---|
| `[target]` (任意) | `PR#42` / `detection:<bd-issue-id>` / `task:<id>` / 自由記述。未指定時は `bd ready -t detection --json` から priority 最高を自動選択 |
| `condition` (任意) | `/goal` の達成条件。省略時は target から自動推定 |
| `--turn-cap N` (任意) | turn 上限。省略時は規模に応じて 25/50/80 |

## 実行内容

1. 引数をパースする
2. `fixing-regression` skill を起動する (skill が preflight → launch → persist まで完結)

## preflight / condition 組み立て / learnings 回収

`skills/fixing-regression/SKILL.md` を参照。

## 関連

- skill (本体): `skills/fixing-regression/SKILL.md`
- subagent: `agents/regression-fixer.md`
- 検出元: `agents/regression-watcher.md` + `commands/start-watcher.md`
