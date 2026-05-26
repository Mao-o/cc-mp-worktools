---
description: "[DEPRECATED v0.11.0] starting-watcher skill の thin wrapper。preflight・launch ロジックは skill 側に統合済み"
---

# /start-watcher (deprecated)

> **v0.11.0 (v2 skill 統合)**: preflight・`--bg` 起動は
> `starting-watcher` skill に統合済み。この command は引数パースと skill 起動の
> thin wrapper として残す。v2.0.0 で削除予定。

## 引数

```text
/start-watcher [interval]
```

| 引数 | 説明 |
|---|---|
| `interval` (任意) | `/loop` の間隔。`30m` (default), `5m`, `1h`, `dynamic` 等 |

## 実行内容

1. 引数をパースする
2. `starting-watcher` skill を起動する (skill が preflight → launch → 通知まで完結)

## preflight / 起動 / 停止方法

`skills/starting-watcher/SKILL.md` を参照。

## 関連

- skill (本体): `skills/starting-watcher/SKILL.md`
- subagent: `agents/regression-watcher.md`
- 修正者: `skills/fixing-regression/SKILL.md`
