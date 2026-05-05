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
- `tool_name`: "Read" | "Bash" | "Edit" | "Write" | ...
  - MultiEdit は CLI 非搭載のため 0.6.0 で対応撤去 (再搭載時は README.md の手順)
- `tool_input`: dict
- `cwd`: string (現在の作業ディレクトリ絶対パス)
- `permission_mode`: "default" | "plan" | "acceptEdits" | "auto" | "dontAsk" | "bypassPermissions"
  - "default": ユーザーに都度確認
  - "plan": plan mode (tool 実行は行われない)
  - "acceptEdits": Edit/Write を自動承認
  - "auto": CLI 2.1.83+ で追加。前段 classifier が tool call を審査
  - "dontAsk": ユーザーへの ask を抑制 (現在の plugin 実装では lenient 扱いしない)
  - "bypassPermissions": 全確認をスキップ (root 不可)
  - 0.6.0 以降、bash handler の `ask_or_allow` は "auto" / "bypassPermissions" の
    2 つで allow に倒す ("plan" は 0.3.3〜0.5.x で前方互換のため含めていたが、
    Phase 0 実測で plan mode では hook が発火しない (dead entry) ことが判明し
    撤去)。Read/Edit handler の `ask_or_deny` は "bypassPermissions" のみ deny に
    倒す。`acceptEdits` / `dontAsk` / `plan` は明示的に非 lenient を維持
    (ask に倒る)。
  - 上記 6 値は `tests/test_envelope_shapes.py` の `_KNOWN_PERMISSION_MODES` で
    CLI 側の既知 mode として固定。`core/output.py::LENIENT_MODES` はその subset
    (auto / bypassPermissions の 2 値)。CLI が新 mode を追加したら両方を同時に
    更新すること (Runbook は `CLAUDE.md` の "CLI バージョンアップ時の再実測手順"
    を参照)。

tool_input の内訳:
- Read: `file_path`
- Bash: `command`, `description`
- Edit: `file_path`, `old_string`, `new_string`
- Write: `file_path`, `content`
