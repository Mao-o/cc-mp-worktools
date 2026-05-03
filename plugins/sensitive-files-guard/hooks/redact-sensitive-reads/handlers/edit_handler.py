"""Edit / Write / MultiEdit tool 用 handler (Step 6, 0.2.0 で deny 固定化)。

方針: **新規 / 既存問わず** ``file_path`` が機密パターン一致なら **deny 固定**。
判定不能 (patterns 読み取り失敗、親ディレクトリ不明等) は ``ask_or_deny``
(fail-closed)。

テンプレ用途 (``.env.example``, ``.env.template`` 等) は既定 patterns.txt で
``!*.example`` / ``!*.template`` として除外済み。ユーザー固有のテンプレ名は
``patterns.local.txt`` に exclude を追加する運用。

### なぜ ask ではなく deny 固定か

実機観測 (0.2.0 beta) で Edit/Write の ``ask`` がユーザーに選択肢を出した際、
意思決定疲労でうっかり承認 → 機密 path への破壊的書き込み → 既存値喪失、の
事例が確認された。公式ハードニング指針も「機密ファイルは deny 一択」。
ask の柔軟性より、確実な block を優先する設計判断。

### deny reason に追加キー名を埋め込む (0.2.0)

dotenv 系 (``.env`` / ``.env.*`` / ``foo.env`` / ``.envrc``) への書き込みを block
する際、``tool_input`` から追加予定のキー名を抽出して reason に添える。
ユーザーが「どのキーを ``.env.example`` に移せばよいか」を見てすぐ代替行動できる。

3 tool とも ``tool_input.file_path`` を共通キーとして持つため、同じ dispatch で
処理する。MultiEdit は ``edits`` が array で全 ``new_string`` を連結して parse する。

親ディレクトリ検査:
- ``path.parent`` を ``is_regular_directory`` で検査し、symlink / special / missing
  なら ``ask_or_deny`` に倒す。親ディレクトリ差し替え race (途中要素の差し替え)
  は範囲外 (README 既知制限)
"""
from __future__ import annotations

from core import logging as L
from core import messages as M
from core import output
from _shared.matcher import is_sensitive
from core.patterns import load_patterns
from core.safepath import classify, is_regular_directory, normalize
from redaction.dotenv import redact_dotenv
from redaction.engine import _detect_format


def _extract_dotenv_keys(envelope: dict, tool_label: str, basename: str) -> list[str]:
    """envelope から書き込み対象の新規 dotenv キー名を抽出する。

    dotenv 系 basename のときだけ有効。他 format (pem / json / toml / ...) では
    空リスト。

    - ``Write``: ``tool_input.content`` を dotenv として parse
    - ``Edit``: ``tool_input.new_string`` を parse
    - ``MultiEdit``: ``tool_input.edits[].new_string`` を全連結して parse

    parse 失敗時 / dotenv 非該当時 / 該当キーなしは空リストを返す (silent fallback)。
    """
    if _detect_format(basename) != "dotenv":
        return []
    tool_input = envelope.get("tool_input") or {}
    text = ""
    if tool_label == "Write":
        raw = tool_input.get("content")
        if isinstance(raw, str):
            text = raw
    elif tool_label == "Edit":
        raw = tool_input.get("new_string")
        if isinstance(raw, str):
            text = raw
    elif tool_label == "MultiEdit":
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            parts: list[str] = []
            for e in edits:
                if isinstance(e, dict):
                    ns = e.get("new_string")
                    if isinstance(ns, str):
                        parts.append(ns)
            text = "\n".join(parts)

    if not text:
        return []

    try:
        info = redact_dotenv(text)
    except Exception:
        return []
    return [k["name"] for k in info.get("keys", [])]


def handle(envelope: dict, tool_label: str = "Edit/Write") -> dict:
    """Edit/Write/MultiEdit 共通 dispatch。

    Args:
        envelope: PreToolUse envelope (``tool_input.file_path`` を持つこと)
        tool_label: reason 文言で使う tool 名 (``Edit`` / ``Write`` / ``MultiEdit``)
    """
    tool_input = envelope.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    cwd = envelope.get("cwd", "")

    if not isinstance(raw_path, str) or not raw_path:
        return output.make_allow()

    try:
        rules = load_patterns()
    except (FileNotFoundError, OSError) as e:
        L.log_error("patterns_unavailable", type(e).__name__)
        return output.ask_or_deny(
            M.policy_unavailable("pause", tool_label=tool_label),
            envelope,
        )
    if not rules:
        return output.make_allow()

    try:
        path = normalize(raw_path, cwd)
    except (ValueError, OSError) as e:
        L.log_error("normalize_failed", type(e).__name__)
        return output.ask_or_deny(
            M.edit_pause("normalize_failed", tool_label=tool_label),
            envelope,
        )

    if not is_sensitive(path, rules):
        return output.make_allow()

    # 最終要素の分類 (新規/既存/symlink/special)
    cls = classify(path)
    L.log_info("edit_classify", cls)

    # 親ディレクトリが通常 dir でない → fail-closed
    parent = path.parent
    if str(parent) not in ("", "/", ".") and not is_regular_directory(parent):
        return output.ask_or_deny(
            M.edit_pause("parent_not_directory", tool_label=tool_label),
            envelope,
        )

    basename = path.name
    new_keys = _extract_dotenv_keys(envelope, tool_label, basename)

    if cls == "symlink":
        return output.make_deny(M.edit_deny(
            tool_label, basename, new_keys,
            extra_note="NOTE: symlink 経由だったため実体側の位置にも注意してください。",
            kind="sensitive_path_symlink",
        ))
    if cls == "special":
        return output.make_deny(M.edit_deny(
            tool_label, basename, new_keys,
            extra_note="NOTE: 非通常ファイル (FIFO/socket/device) への書き込みでした。",
            kind="sensitive_path_special",
        ))
    if cls == "error":
        return output.ask_or_deny(
            M.edit_pause("io_error", tool_label=tool_label),
            envelope,
        )

    # regular (既存上書き) / missing (新規作成) は deny 固定
    return output.make_deny(M.edit_deny(tool_label, basename, new_keys))
