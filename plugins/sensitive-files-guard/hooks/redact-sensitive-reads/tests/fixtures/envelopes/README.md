# envelope fixtures

Phase 0 実測結果 (CLAUDE.md に恒久記録済み) を元に作成した PreToolUse envelope のサンプル。
`test_envelope_shapes.py` は必須キーの存在のみを検証する。

## 採取方針

- Claude Code CLI のバージョン差で envelope のマイナーキーが変わる可能性があるため、
  テストは「必須キーの存在確認」のみに留める
- 実機採取を再度行いたい場合は `hooks/_debug/capture_envelope.py` を一時的に
  作成し、`sys.stdin.read()` を `/tmp/envelope-<tool>.json` に保存してから
  `claude --plugin-dir .` で起動して各 tool を 1 回ずつ実行する
- 採取後はこのディレクトリに転記して一時スクリプトを削除する

## 既知の必須キー (Phase 0 実測)

共通:
- `hook_event_name`: "PreToolUse"
- `tool_name`: "Read" | "Bash" | "Edit" | "Write" | "MultiEdit" | ...
- `tool_input`: dict
- `cwd`: string (現在の作業ディレクトリ絶対パス)
- `permission_mode`: "default" | "plan" | "acceptEdits" | "auto" | "dontAsk" | "bypassPermissions"
  - "default": ユーザーに都度確認
  - "plan": plan mode (tool 実行は行われない)
  - "acceptEdits": Edit/Write を自動承認
  - "auto": CLI 2.1.83+ で追加。前段 classifier が tool call を審査
  - "dontAsk": ユーザーへの ask を抑制 (現在の plugin 実装では lenient 扱いしない)
  - "bypassPermissions": 全確認をスキップ (root 不可)
  - 0.3.3 以降、bash handler の `ask_or_allow` は "auto" / "bypassPermissions" /
    "plan" の 3 つで allow に倒す。Read/Edit handler の `ask_or_deny` は
    "bypassPermissions" のみ deny に倒す。`acceptEdits` / `dontAsk` は明示的に
    非 lenient を維持 (ask に倒る)。
  - 上記 6 値は `core/output.py::LENIENT_MODES` と `tests/test_envelope_shapes.py`
    の `_KNOWN_PERMISSION_MODES` で突合される。CLI 側が新しい mode を追加したら
    両方を同時に更新すること (Runbook は `CLAUDE.md` の "CLI バージョンアップ時の
    再実測手順" を参照)。

tool_input の内訳:
- Read: `file_path`
- Bash: `command`, `description`
- Edit: `file_path`, `old_string`, `new_string`
- Write: `file_path`, `content`
- MultiEdit: `file_path`, `edits` (list of `{old_string, new_string}`)
