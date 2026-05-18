---
description: architect-reviewer subagent を多視点 (3-5 名) で並列起動し、verdict を集約して .claude/agent-org/approvals/<task-id>.json に書き出す。Phase 3 で TaskCompleted/Stop quality gate と連携する approval workflow のエントリポイント
---

# /run-review

`running-review` skill を経由して `architect-reviewer` を複数視点で並列起動し、
集約 verdict を `.claude/agent-org/approvals/<task-id>.json` に書き出す。

## 引数

```text
/run-review <task-id> [perspectives...]
```

| 引数 | 説明 |
|---|---|
| `<task-id>` (必須) | approval JSON のファイル名に使う識別子。kebab-case 推奨 (例: `PR-42`, `design-2026-05-18`, `pr-42-perf`)。`/` や空白を含めない |
| `perspectives...` (任意) | カンマ区切りで視点を明示 (例: `security,api-design,performance`)。省略時は対象の性質から自動選定 (3-5 個) |

`<task-id>` を省略した場合、command 側で `review-<ISO timestamp>` を自動採番する
(末尾サマリでユーザーに通知)。

## 実行内容

1. **対象を確認する**
   - 直近の会話 / 引数 / 開いている PR から、レビュー対象 (PR 番号 / commit
     range / 設計 doc / 実装 path) を抽出
   - 不明確な場合はユーザーに `AskUserQuestion` で確認
2. **`running-review` skill を起動する**
   - skill に対象と perspectives を渡す
   - skill 内部で agent teams または Task tool 経由で
     `agent-org:architect-reviewer` を 3-5 名 spawn
3. **verdict YAML を集約する**
   - skill 出力から各 reviewer の verdict YAML を回収
   - `aggregate_overall` を最重 verdict (`reject` > `request_changes` >
     `approve_with_conditions` > `approve`) で決定
   - severity 別件数 (`critical` / `major` / `minor` / `nit`) を合計
   - `min_confidence` を全 reviewer の中で最低の値に
4. **approval JSON を書き出す** (`.claude/agent-org/approvals/<task-id>.json`)
   - 既存ファイルがあれば上書き (再レビュー対応)
   - 親 dir が無ければ作成 (`/org-init` 未実行のプロジェクト対応)
5. **ユーザーに結果を通知**
   - approval status / concern 件数 / 保存先パス / 各 reviewer の overall を
     1 つのテーブルとして表示

## approval JSON schema

`.claude/agent-org/approvals/<task-id>.json`:

```json
{
  "schema_version": "1",
  "task_id": "PR-42",
  "target": {
    "type": "pr | commit_range | design_doc | implementation",
    "ref": "PR#42"
  },
  "reviewed_at": "2026-05-18T03:45:00Z",
  "reviewer": "agent-org/run-review",
  "perspectives_reviewed": ["security", "api-design", "performance"],
  "missing_perspectives": [],
  "aggregate_overall": "approve | approve_with_conditions | request_changes | reject",
  "approval_status": "approved | conditional | rejected",
  "min_confidence": "high | medium | low",
  "concerns_summary": {
    "critical": 0,
    "major": 1,
    "minor": 3,
    "nit": 2
  },
  "verdicts": [
    {
      "perspective": "security",
      "reviewer": "architect-reviewer",
      "overall": "approve",
      "confidence": "high",
      "concerns": [
        {
          "id": "C1",
          "severity": "minor",
          "summary": "...",
          "detail": "...",
          "suggestion": "..."
        }
      ],
      "strengths": ["..."],
      "questions": ["..."],
      "references": [],
      "retrieval_keys": ["..."]
    }
  ]
}
```

### approval_status 決定規則

| aggregate_overall | approval_status |
|---|---|
| `approve` | `approved` |
| `approve_with_conditions` | `conditional` |
| `request_changes` | `rejected` |
| `reject` | `rejected` |

`task-completed-gate.sh` (Phase 3 で追加) は `approval_status` を見て:

- `approved`: gate pass
- `conditional`: gate pass (warn message のみ出力)
- `rejected`: gate block (exit 2)

## ファイル書込の権限

- `.claude/agent-org/approvals/` ディレクトリは `/org-init` で作成される
  ことを期待しているが、未作成でも command 側で `mkdir -p` する (冪等)
- 書込先は repo 内 (`.claude/` 配下) なので worktree 隔離の対象。main session
  からの `/run-review` 起動を前提とする (`--bg` 起動した subagent からは
  呼ばない設計)

## 値や秘密の取り扱い

- approval JSON 本文に API key / トークン等の値そのものを書かない
- reviewer が verdict YAML 内に秘密を含めてきた場合、command 側で
  `***REDACTED***` に置換してから書く
- `target.ref` に PR URL を書く場合、URL のみ。Authorization header 等は書かない

## 後続再レビュー

同じ `<task-id>` で `/run-review` を再実行すると approval JSON が上書きされる。
過去 verdict を保持したい場合は事前に rename しておく:

```bash
mv .claude/agent-org/approvals/PR-42.json \
   .claude/agent-org/approvals/PR-42-v1.json
```

## 使用例

```text
/run-review PR-42 security,api-design,testability
```

```text
/run-review design-auth-rewrite
（perspectives 自動選定、3-5 名 spawn）
```

```text
/run-review pr-42-perf performance,architecture,dx
```

## 関連

- skill: `skills/running-review/SKILL.md`
- subagent: `agents/architect-reviewer.md`
- hook (gate): `hooks/task-completed-gate.sh` (approval JSON を読んで判定)
- 関連 hook (gate): `hooks/stop-quality-gate.sh`
  (`.claude/agent-org/quality-gates.json` ベース、別経路)
