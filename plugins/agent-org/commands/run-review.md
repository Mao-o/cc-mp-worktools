---
description: architect-reviewer subagent を多視点 (3-5 名) で並列起動し、verdict を集約して bd approval issue として記録する。task-completed-gate / stop-quality-gate と連携する approval workflow のエントリポイント
---

# /run-review

`running-review` skill を経由して `architect-reviewer` を複数視点で並列起動し、
集約 verdict を **bd approval issue** (`bd create -t approval`) として記録する。
v0.7.0 から approval は bd issue に一本化 (旧 `.claude/agent-org/approvals/<task-id>.json`
は廃止、`/migrate-approvals-to-beads` で変換可能)。v0.8.0 (ADR-007) で bd は
`<repo>/.beads/` に repo-local 配置 (git worktree-aware)。

## 引数

```text
/run-review <task-id> [perspectives...]
```

| 引数 | 説明 |
|---|---|
| `<task-id>` (必須) | bd label `task:<id>` で task issue / approval issue を結ぶ識別子。kebab-case 推奨 (例: `PR-42`, `design-2026-05-18`, `pr-42-perf`)。`/` や空白を含めない |
| `perspectives...` (任意) | カンマ区切りで視点を明示 (例: `security,api-design,performance`)。省略時は対象の性質から自動選定 (3-5 個) |

`<task-id>` を省略した場合、command 側で `review-<ISO timestamp>` を自動採番する
(末尾サマリでユーザーに通知)。

## 前提条件

- `bd` CLI install 済 (`brew install beads` / Mac)
- `<repo>/.beads/` 初期化済 (未なら `/org-init` 先に実行、v0.8.0 から repo-local)
- `jq` install 済 (`brew install jq`)
- git repo 内 (`<repo>/.beads/` 配置のため必須)

未充足なら command 側で abort し `/bd-check` を案内する。

## 実行内容

1. **対象を確認する**
   - 直近の会話 / 引数 / 開いている PR から、レビュー対象 (PR 番号 / commit
     range / 設計 doc / 実装 path) を抽出
   - 不明確な場合はユーザーに `AskUserQuestion` で確認
2. **task issue を find-or-create する** (Phase 6.1.0 規約)
   ```bash
   # v0.8.0: cd <repo> で bd 自動 resolve (BEADS_DIR 明示指定不要、ADR-007)
   REPO_ROOT="$(git rev-parse --show-toplevel)"
   task_bd="$(cd "$REPO_ROOT" && bd list -l "task:${task_id}" -t task --json 2>/dev/null \
     | jq -r '.[0].id // empty')"
   if [ -z "$task_bd" ]; then
     task_bd="$(cd "$REPO_ROOT" && bd create "task: ${task_id}" \
       -t task -p 2 \
       -l "task:${task_id}" \
       -l "agent-org" \
       --json | jq -r .id)"
   fi
   ```
3. **`running-review` skill を起動する**
   - skill に対象と perspectives を渡す
   - skill 内部で agent teams または Task tool 経由で
     `agent-org:architect-reviewer` を 3-5 名 spawn
4. **verdict YAML を集約する**
   - skill 出力から各 reviewer の verdict YAML を回収
   - `aggregate_overall` を最重 verdict (`reject` > `request_changes` >
     `approve_with_conditions` > `approve`) で決定
   - severity 別件数 (`critical` / `major` / `minor` / `nit`) を合計
   - `min_confidence` を全 reviewer の中で最低の値に
5. **approval bd issue を作成する** (Phase 6.1.1 規約)
   - aggregate → priority マッピング:

     | aggregate_overall | priority | semantic |
     |---|---|---|
     | `reject` | 0 | rejected (blocker) |
     | `request_changes` | 0 | rejected (blocker) |
     | `approve_with_conditions` | 1 | conditional (passes gate with warn) |
     | `approve` | 2 | approved |
     | (informational) | 3 | info-only (gate skip) |

   - 既存 approval issue を検出した場合 (再 review): 旧 approval を supersedes
     dep で繋いだ後に close (詳細は「再 review」セクション)
   - description body は集約 verdict YAML (全 reviewer 分を含む)。秘密 / API key
     等が含まれていれば `***REDACTED***` に置換してから書く
   - **必須 label セット**:
     ```bash
     # 集約 verdict YAML を変数経由で渡す (G3: heredoc 直書き禁止)
     verdict_body="$(cat <<'EOF'
     schema_version: "1"
     task_id: ${task_id}
     target:
       type: pr | commit_range | design_doc | implementation
       ref: ${target_ref}
     reviewed_at: ${iso_ts}
     reviewer: agent-org/run-review
     perspectives_reviewed: [security, api-design, performance]
     missing_perspectives: []
     aggregate_overall: ${aggregate}
     min_confidence: ${min_conf}
     concerns_summary:
       critical: 0
       major: 1
       minor: 3
       nit: 2
     verdicts:
       - perspective: security
         reviewer: architect-reviewer
         overall: approve
         confidence: high
         concerns: [...]
         strengths: [...]
         questions: [...]
         references: []
         retrieval_keys: [...]
     EOF
     )"

     # perspective ごとに -l を増やす
     extra_labels=()
     for p in $perspectives_reviewed; do
       extra_labels+=(-l "perspective:$p")
     done

     appr_bd="$(cd "$REPO_ROOT" && bd create "approval: ${task_id} (${aggregate})" \
       -t approval -p "${prio}" \
       -l "approval" \
       -l "task:${task_id}" \
       -l "agent-org" \
       -l "aggregate:${aggregate}" \
       "${extra_labels[@]}" \
       -d "$verdict_body" \
       --json | jq -r .id)"
     ```
6. **dep を張る** — `bd dep add <task> <approval>`
   ```bash
   # approval が task を blocks (rejected/conditional approval が open の間
   # task は bd ready から除外される)
   (cd "$REPO_ROOT" && bd dep add "$task_bd" "$appr_bd")
   ```
   - approved (priority=2) の場合は dep 作成後すぐ `(cd "$REPO_ROOT" && bd close "$appr_bd")` で
     blocker 解除
   - informational (priority=3) は dep 不要 (`bd close` してから dep skip でも可)
7. **reviewer 学習を bd remember に永続化**
   - 各 reviewer が verdict YAML 内に `learnings_to_persist:` を付けてきた場合、
     1 行ずつ `bd remember "review-heuristic: <summary> [keys: <k1>,<k2>]"` で
     永続化 (`bd remember` は bd 1.0.4+ の learning store)
   - 失敗しても (`bd remember` 未サポート / 一時 error)、approval issue 作成は
     完了させる (curate は best-effort)
8. **ユーザーに結果を通知**
   - approval status / concern 件数 / approval bd id / task bd id / 各 reviewer
     の overall を 1 つのテーブルとして表示

## approval issue schema (bd label / priority + description)

| 項目 | 表現 |
|---|---|
| type | `approval` |
| priority | 0=rejected / 1=conditional / 2=approved / 3=informational |
| 必須 label | `approval` / `task:<task_id>` / `agent-org` / `aggregate:<verdict>` |
| 追加 label | `perspective:<persp>` (per reviewer、複数付与可) |
| description body | 集約 verdict YAML (`schema_version` / `task_id` / `target` / `verdicts[]` / `concerns_summary` 等) |
| dep | `bd dep add <task> <approval>` で approval が task を blocks |
| status | open=未解決 (priority=0/1) / closed=解決 (approved or 上書き済) |

### gate 判定規則 (task-completed-gate / stop-quality-gate)

```bash
# rejected approval (priority=0 かつ open) が 1 件でもあれば block
# v0.8.0: cd <repo> で bd 自動 resolve
rejected_count="$(cd "$REPO_ROOT" && bd list -l "task:${task_id}" -t approval --status open --json \
  | jq '[.[] | select(.priority==0)] | length')"
```

- `rejected_count > 0` → exit 2 で block
- `rejected_count == 0` かつ open conditional (priority=1) が残っている → pass + warn
- task に approval issue が 1 件も無い → pass (opt-in 設計、`/run-review` 未実行 task)

詳細は `hooks/task-completed-gate.sh` / `hooks/stop-quality-gate.sh` の本体参照。

## 再 review

同じ `<task-id>` で `/run-review` を再実行した場合の挙動:

1. `(cd "$REPO_ROOT" && bd list -l "task:${task_id}" -t approval --status open --json | jq -r '.[].id')`
   で既存 open approval を列挙
2. 新しい approval を `bd create` (上記 step 5 と同じ手順)
3. **supersedes dep を張る** — bd 1.0.4 で `--type supersedes` 公式サポート (U13 検証済):
   ```bash
   for old_appr in $existing_approvals; do
     (cd "$REPO_ROOT" && bd dep add "$new_appr" "$old_appr" --type supersedes)
     (cd "$REPO_ROOT" && bd close "$old_appr")
   done
   ```
4. 新 approval の dep を task に張る (step 6)

過去 verdict は description body + supersedes dep で履歴追跡可能。`bd show
<new_appr>` で `DEPENDS ON → <old_appr> ... via supersedes` が表示される。

## 値や秘密の取り扱い

- approval description (verdict YAML) に API key / トークン等の値そのものを書かない
- reviewer が verdict YAML 内に秘密を含めてきた場合、command 側で
  `***REDACTED***` に置換してから書く
- `target.ref` に PR URL を書く場合、URL のみ。Authorization header 等は書かない
- bd description は plain text として保存される (暗号化なし)。秘密が紛れ込んだ
  場合は `bd update <id> -d "..."` で書き換え可能

## ファイル書込の権限

- approval は bd 経由で `<repo>/.beads/` (v0.8.0 から repo-local) に書込
- bd は git worktree-aware に動作し、`--bg` 隔離下でも main repo の DB を
  共有する (ADR-007 evidence) — つまり worktree 隔離による split-brain は無い
- ただし `--bg` セッションでは plugin slash command が解決されないため、
  `/run-review` は **main session 限定** (foreground)

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

## 既存 approval JSON との互換性

v0.6.0 までに `.claude/agent-org/approvals/<task-id>.json` を蓄積していた
プロジェクトは、`/migrate-approvals-to-beads` で bd issue に変換する。
旧 JSON は `.claude/agent-org/approvals.legacy/` に mv され、rollback できる
状態で残る (Phase 9 で物理削除)。詳細は `commands/migrate-approvals-to-beads.md`。

`/run-review` 自体は v0.7.0 以降、新規 JSON を書かない。

## 関連

- skill: `skills/running-review/SKILL.md`
- subagent: `agents/architect-reviewer.md`
- hook (gate): `hooks/task-completed-gate.sh` (bd query で判定)
- 関連 hook (gate): `hooks/stop-quality-gate.sh` (`kind: approvals_clean` で同じ query)
- migration: `commands/migrate-approvals-to-beads.md`
- bd 規律: `skills/using-beads/SKILL.md`
- diagnose: `commands/bd-check.md`
