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
- `permission_mode`: "default" | "acceptEdits" | "bypassPermissions" | "plan"

tool_input の内訳:
- Read: `file_path`
- Bash: `command`, `description`
- Edit: `file_path`, `old_string`, `new_string`
- Write: `file_path`, `content`
- MultiEdit: `file_path`, `edits` (list of `{old_string, new_string}`)
