---
description: "[DEPRECATED v0.10.1] running-review skill の thin wrapper。persist ロジックは skill 側に統合済み"
---

# /run-review (deprecated)

> **v0.10.1 (V9)**: persist ロジック (bd task/approval/dep/remember) は
> `running-review` skill に統合済み。この command は引数パースと skill 起動の
> thin wrapper として残す。v2.0.0 で削除予定。

## 引数

```text
/run-review <task-id> [perspectives...]
```

| 引数 | 説明 |
|---|---|
| `<task-id>` (必須) | bd label `task:<id>` の識別子。kebab-case (例: `PR-42`)。省略時 `review-<ISO timestamp>` を自動採番 |
| `perspectives...` (任意) | カンマ区切り (例: `security,api-design,performance`)。省略時は自動選定 |

## 実行内容

1. 引数をパースする
2. `running-review` skill を起動する (skill が spawn → 集約 → persist → 通知まで完結)

## approval issue schema / gate 判定 / 再 review

`skills/running-review/SKILL.md` を参照。

## 関連

- skill (実体): `skills/running-review/SKILL.md`
- subagent: `agents/architect-reviewer.md`
- gate hooks: `hooks/task-completed-gate.sh`, `hooks/stop-quality-gate.sh`
