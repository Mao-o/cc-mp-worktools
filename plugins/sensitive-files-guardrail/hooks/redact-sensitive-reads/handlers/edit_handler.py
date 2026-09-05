"""Edit / Write tool 用 handler (Step 6, 0.2.0 で deny 固定化)。

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

dotenv 系 (``.env`` / ``.env.*`` / ``foo.env`` / ``*.envrc``) への書き込みを block
する際、``tool_input`` から追加予定のキー名を抽出して reason に添える。
ユーザーが「どのキーを ``.env.example`` に移せばよいか」を見てすぐ代替行動できる
(``.envrc`` family は direnv の shell script なので、実際の basename から
動的に組み立てた ``<basename>.example`` を案内する。0.29.0、マージ前レビュー
の指摘で basename 派生化、``engine.is_envrc_basename`` 参照)。

### 状況別の deny 文面 (E6, 0.20.0)

``classify`` の結果を ``edit_deny(kind=...)`` に渡し、文面を 4 分岐する。
0.19.1 までは ``missing`` (新規作成) と ``regular`` (既存上書き) が同一文面に
落ち、``symlink`` / ``special`` は ``extra_note`` 1 行の違いしかなかった。

| ``classify`` | ``kind`` | 文面 |
|---|---|---|
| ``missing`` | ``new`` | 同じキー名で ``.env.example`` を作る案内 (dotenv かつ追加キーありのとき。``.envrc`` family は ``<basename>.example``) |
| ``regular`` | ``overwrite`` | **既存ファイルの Read 同等 minimal info** + dotenv-cli merge の案内 (``.envrc`` family は direnv 前提の案内) |
| ``symlink`` | ``symlink`` | 実体側が書き換わる旨と symlink 運用の確認 |
| ``special`` | ``special`` | FIFO / socket / device への書き込みである旨 |

**判定 (deny / ask / allow) は 0.19.1 から一切変えていない。** 変わるのは
reason 文字列だけで、``error`` は従来どおり ``ask_or_deny`` に倒れる。
``overwrite`` の minimal info 取得が失敗しても deny は動かない
(``_render_existing`` を参照)。

両 tool (Edit / Write) とも ``tool_input.file_path`` を共通キーとして持つため、
同じ dispatch で処理する。MultiEdit は CLI 2.1.x で非搭載のため 0.6.0 で
対応コードを撤去 (`hooks.json` の matcher も除外済み)。再搭載時は本 docstring と
``__main__.py`` argparse の `choices` / `_dispatch` 分岐に追加し、
``_extract_dotenv_keys`` に edits 連結ブランチを足す。

親ディレクトリ検査:
- ``path.parent`` を ``is_regular_directory`` で検査し、symlink / special / missing
  なら ``ask_or_deny`` に倒す。親ディレクトリ差し替え race (途中要素の差し替え)
  は範囲外 (README 既知制限)
"""
from __future__ import annotations

from pathlib import Path

from core import logging as L
from core import messages as M
from core import output
from _shared.matcher import is_sensitive, root_relative
from _shared.patterns import resolve_project_root
from core.patterns import load_patterns
from core.safepath import classify, is_regular_directory, normalize
from redaction.dotenv import redact_dotenv
from redaction.engine import _detect_format, is_envrc_basename
from redaction.file_render import render_for_bash


def _extract_dotenv_keys(envelope: dict, tool_label: str, basename: str) -> list[str]:
    """envelope から書き込み対象の新規 dotenv キー名を抽出する。

    dotenv 系 basename のときだけ有効。他 format (pem / json / toml / ...) では
    空リスト。

    - ``Write``: ``tool_input.content`` を dotenv として parse
    - ``Edit``: ``tool_input.new_string`` を parse

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

    if not text:
        return []

    try:
        info = redact_dotenv(text)
    except (ValueError, UnicodeDecodeError, AttributeError, TypeError) as e:
        # L2 (0.4.3): bare except を狭め、種別をログに残す。
        # ValueError: dotenv parse の構文系エラー
        # UnicodeDecodeError: bytes 風入力
        # AttributeError / TypeError: 想定外の input 形状
        L.log_info("dotenv_parse_failed", type(e).__name__)
        return []
    return [k["name"] for k in info.get("keys", [])]


def _render_existing(path: Path, cwd: str) -> tuple[str, dict | None, str]:
    """上書き対象の既存ファイルを Read 同等 minimal info にする (E6, 0.20.0)。

    ``redaction.file_render.render_for_bash`` を再利用する。渡すのは normalize
    済みの **絶対パス** なので、同関数の project root 再解決 (相対 operand 専用)
    には入らず、``resolved_base`` / ``status == "project_root"`` は発生しない。

    この関数が返すのは reason 文字列の材料だけで、**判定 (deny/ask/allow) には
    一切影響しない**。呼出時点で deny は確定しており、失敗しても
    ``core.messages.edit_deny`` が unavailable 行に降りるだけ。
    ``render_for_bash`` は内部で例外を握り潰す実装だが、将来の変更で例外が
    漏れても verdict が動かないよう、ここでも捕捉して空文字を返す。

    Returns:
        ``(reason, dotenv_info, status)`` — ``render_for_bash`` の 4 戻り値の
        うち ``resolved_base`` (project root 再解決専用、絶対パス入力のここ
        では常に空になる) を除いた 3 つ。0.26.0 より前は ``reason``
        以外の 3 つを丸ごと捨てていたため、失敗理由が
        ``core.messages.edit_deny`` に一切伝わらず、unavailable の理由ラベル
        と next action が常に同一の (render 失敗時は事実に反する) 文言に
        潰れていた。``dotenv_info`` は既存キー集合の判定にも使う。
    """
    try:
        reason, info, status, _base = render_for_bash(str(path), cwd)
    except Exception as e:  # noqa: BLE001 (verdict 不変を優先した意図的な捕捉)
        L.log_error("edit_existing_render_failed", type(e).__name__)
        return "", None, ""
    if status:
        # ``handlers.bash_handler`` の
        # ``log_info("bash_render_failed", render_status)`` と同じ計測基盤に
        # 乗せる (0.26.0)。空文字 (成功) はログしない — 失敗分布の計測が目的。
        L.log_info("edit_render_failed", status)
    return reason or "", info, status


def handle(envelope: dict, tool_label: str = "Edit/Write") -> dict:
    """Edit/Write 共通 dispatch。

    Args:
        envelope: PreToolUse envelope (``tool_input.file_path`` を持つこと)
        tool_label: reason 文言で使う tool 名 (``Edit`` / ``Write``)
    """
    tool_input = envelope.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    cwd = envelope.get("cwd", "")

    if not isinstance(raw_path, str) or not raw_path:
        return output.make_allow()

    try:
        rules = load_patterns(cwd=cwd)
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

    # root は [project:] セクションの key と同じ値 (path 形 rule の基準、0.24.0)
    root = resolve_project_root(cwd)
    if not is_sensitive(path, rules, root=root):
        return output.make_allow()
    # 除外案内を path 形 (1 ファイルだけ) にするための root 相対 path。root 不明 /
    # root 配下でなければ空 (basename 形のみ案内)。判定には影響しない
    relpath = root_relative(path, root) or ""

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

    is_dotenv = _detect_format(basename) == "dotenv"
    # .envrc family (``*.envrc``、大文字小文字問わず。direnv 用 shell script)
    # は _detect_format 上は dotenv 系に含まれる (KEY=value 行のスーパー
    # セットとして parse できるため) が、テンプレート慣習は .env.example
    # ではなく basename から動的に組み立てた <basename>.example なので
    # 助言文面だけ別軸で分ける (内部バックログ、engine.is_envrc_basename 参照)。
    is_envrc = is_envrc_basename(basename)

    if cls == "symlink":
        return output.make_deny(M.edit_deny(
            tool_label, basename, new_keys,
            kind="symlink", is_dotenv=is_dotenv, is_envrc=is_envrc, relpath=relpath,
        ))
    if cls == "directory":
        return output.make_deny(M.edit_deny(
            tool_label, basename, new_keys,
            kind="directory", is_dotenv=is_dotenv, is_envrc=is_envrc, relpath=relpath,
        ))
    if cls == "special":
        return output.make_deny(M.edit_deny(
            tool_label, basename, new_keys,
            kind="special", is_dotenv=is_dotenv, is_envrc=is_envrc, relpath=relpath,
        ))
    if cls == "error":
        return output.ask_or_deny(
            M.edit_pause("io_error", tool_label=tool_label),
            envelope,
        )

    if cls == "regular":
        # 既存上書き: Read 同等 minimal info で「今そのファイルに何が入って
        # いるか」(鍵名まで) を返す。deny は既に確定しているので、この情報は
        # reason の情報量だけを増やす。
        existing_render, existing_info, existing_status = _render_existing(
            path, cwd,
        )
        # 既存キー名の集合。dotenv かつ render 成功時のみ非 None。
        # 非 dotenv / render 失敗 / 32KB 超 (streaming scan は構造化 info を
        # 返さない) では空集合のままにし、``edit_deny`` 側で「更新」と
        # 誤って言い切らない (docstring 参照)。
        existing_keys = (
            frozenset(k["name"] for k in existing_info["keys"])
            if existing_info
            else frozenset()
        )
        return output.make_deny(M.edit_deny(
            tool_label, basename, new_keys,
            kind="overwrite", is_dotenv=is_dotenv, is_envrc=is_envrc,
            existing_render=existing_render,
            existing_render_status=existing_status,
            existing_keys=existing_keys,
            relpath=relpath,
        ))

    # missing (新規作成) も deny 固定
    return output.make_deny(M.edit_deny(
        tool_label, basename, new_keys, kind="new",
        is_dotenv=is_dotenv, is_envrc=is_envrc,
        relpath=relpath,
    ))
