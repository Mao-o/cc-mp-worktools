"""accounts.local.json の builder (唯一の正規書込経路)。

accounts.local.json の編集は builder 経由で行う運用に統一する。動作の安定や
フォーマット統一のため、書込先パス・JSON フォーマット・既存キーの扱い・
stdout の値表示制御を builder 側で一元管理する。Agent Skill (`accounts-init`
`accounts-show` `accounts-migrate`) が対話フローを提供し、Claude は skill
経由で builder を呼ぶ。

設計判断 (D1〜D7):

- **D1**: builder が唯一の正規経路。書込パスの固定、JSON フォーマットの
  一貫化、既存キーの温存、CLI 現在値との突合、旧パス統合を一元管理する。
- **D2**: 書込対象パスは `core/paths.accounts_file_new()` に固定。argv から
  上書きできない (`_ALLOWED_BASENAME` で basename を assertion)。
- **D3**: stdout の値表示は既定で隠蔽。`--show-values` 明示時のみ値を
  stdout に出す。
- **D4**: 3-tier lookup で競合検出時は deny。migrate で統合する。
- **D5**: `migrate` サブコマンドで旧パス → 新パスへの統合を提供。
- **D6**: `set` / `remove` サブコマンドで既存値の更新・削除を提供する。
  `--host` で dict 値の特定キー (hostname / alias) だけを追加・上書き・削除
  できる。最後の 1 キーを remove すると空 dict を残さずキー自体を削除する
  (空 dict は github/firebase/gcloud の `verify()` が permanent deny する形の
  ため)。
- **D7**: `init` / `set` / `migrate` は書込前に `_validate_entry_shape()` で
  値の形 (str または service ごとの許容 dict 形) を検証し、不正なら exit 1。
  `migrate` は取り込み元 (旧パス) の値だけを検証対象にし、かつ dict の未知
  キーは許容する (`strict_keys=False`) — `verify()` 自体が寛容な形を migrate
  でだけ厳格化すると、migrate が触ってすらいない既存データが理由で無関係な
  統合作業まで exit 1 になる退行を生むため。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import IO, Any

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from core import paths  # noqa: E402
from services import ALL as SERVICES  # noqa: E402

_SERVICE_NAMES = [svc.ACCOUNT_KEY for svc in SERVICES]
_SERVICE_BY_KEY = {svc.ACCOUNT_KEY: svc for svc in SERVICES}

_VALUE_HIDDEN_MARK = "(value hidden. use --show-values to reveal)"

# accounts.local.json と同じディレクトリに同梱する Claude 向け案内ファイル。
# sensitive-files-guardrail 等で *.local.json への直接アクセスが deny される事情と
# builder 経由の正規経路を Claude (LLM) に signpost する。
_PROJECT_CLAUDE_MD_FILENAME = "CLAUDE.md"
_PROJECT_CLAUDE_MD_TEMPLATE = _HERE / "templates" / "project_claude.md"


class _BuilderError(Exception):
    """builder 内部のビジネスエラー。exit 1 に繋げる。"""


def _project_dir() -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise _BuilderError(
            f"既存 {path} の JSON が不正です: {e.msg} (行 {e.lineno})。"
            "手動で修正してから再実行してください。"
        )
    except OSError as e:
        raise _BuilderError(f"{path} の読み込みに失敗しました: {e}")
    if not isinstance(data, dict):
        raise _BuilderError(
            f"{path} は JSON オブジェクト ({{...}}) である必要があります。"
        )
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _ensure_gitignore_entry(project_dir: str, stdout: IO[str]) -> None:
    """accounts.local.json を .gitignore に追加する (best-effort)。

    既に該当エントリがあればスキップ。.gitignore が存在しなければ作成しない
    (プロジェクトに .gitignore が無い環境で勝手に作ると意図しない副作用になる)。
    """
    gitignore = Path(project_dir) / ".gitignore"
    entry = ".claude/verify-cloud-account/accounts.local.json"
    if not gitignore.exists():
        return
    try:
        content = gitignore.read_text(encoding="utf-8")
        if any(line.strip() == entry for line in content.splitlines()):
            return
        if not content.endswith("\n"):
            content += "\n"
        content += f"{entry}\n"
        gitignore.write_text(content, encoding="utf-8")
        print(f"updated: {gitignore} ({entry} を追加)", file=stdout)
    except OSError as e:
        print(f"warning: .gitignore の更新に失敗しました: {e}", file=stdout)


def _ensure_project_claude_md(project_dir: str, stdout: IO[str]) -> None:
    """新パスと同じディレクトリに Claude 向け signpost (`CLAUDE.md`) を同梱する。

    - 既に CLAUDE.md が存在する場合は何もしない (ユーザー編集を尊重)
    - テンプレート読み込み・書き込みのいずれかが失敗しても警告を 1 行出すだけで
      builder 自体は成功させる (best-effort)
    - dispatcher 等が読みに来るパスではないため、ここで失敗しても plugin 本体
      の動作に影響しない

    plugin 同士の疎結合を保つための signpost: sensitive-files-guardrail が
    `*.local.json` への直接アクセスを deny する事情と、builder 経由の正規経路
    (Agent Skill / Bash) を Claude (LLM) に伝える。
    """
    target_dir = paths.accounts_file_new(project_dir).parent
    md_path = target_dir / _PROJECT_CLAUDE_MD_FILENAME
    if md_path.exists():
        print(f"(skipped: {md_path} already exists)", file=stdout)
        return
    try:
        content = _PROJECT_CLAUDE_MD_TEMPLATE.read_text(encoding="utf-8")
    except OSError as e:
        print(
            f"warning: project CLAUDE.md template の読み込みに失敗しました: {e}",
            file=stdout,
        )
        return
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(content, encoding="utf-8")
    except OSError as e:
        print(f"warning: {md_path} の書き込みに失敗しました: {e}", file=stdout)
        return
    print(f"created: {md_path}", file=stdout)


def _format_value_for_display(value: Any, show_values: bool) -> str:
    if show_values:
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return f"<dict with {len(value)} key(s)> {_VALUE_HIDDEN_MARK}"
    return _VALUE_HIDDEN_MARK


def _print_change_line(
    status: str,
    key: str,
    value: Any,
    show_values: bool,
    stdout: IO[str],
) -> None:
    display = _format_value_for_display(value, show_values)
    if show_values:
        print(f"{status}: {key} -> {display}", file=stdout)
    else:
        print(f"{status}: {key}", file=stdout)
        print(f"  {display}", file=stdout)


def _parse_value(raw: str) -> Any:
    """`--value` の生文字列を解釈する。

    dict 形 (JSON object) だけを構造化データとして受理し、それ以外
    (JSON として不正 / dict 以外の JSON 型) は生文字列のまま返す。
    services の期待値スキーマは str または dict[str, str] のみ
    (services/*.py の verify() 参照) で、数値・真偽値・配列を暗黙変換すると
    "12345" のような数字だけの username が int になり検証が壊れる
    (内部バックログ: --value の JSON が文字列保存されたまま gcloud.verify が
    dict 期待値と JSON 文字列を比較して永久不一致になる不具合の修正)。
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if isinstance(parsed, dict):
        return parsed
    return raw


def _validate_entry_shape(service, value: Any, *, strict_keys: bool = True) -> str | None:
    """value が service の accounts.local.json 値として許容される**形**か検証する。

    services/*.py の verify() が受理する型 (str または dict) と揃えた形だけの
    チェック。実 verify() (CLI を叩く) は書込時に対象 CLI へ到達できるとは
    限らないため呼ばない。不正なら理由文字列、OK なら None を返す。

    strict_keys=False (migrate が旧パスから取り込む既存データ向け) では dict の
    **未知キー**を許容する — gcloud.verify() 等は宣言外のキーを黙って無視する
    だけで拒否しないため、migrate でだけ厳格化すると verify() では通っていた
    形を後から書けなくする退行になる (migrate が触っていない新パス側の
    既存エントリまで巻き込んで書込全体を deny すると、この plugin が解消しよう
    としている「手動 JSON 編集の要求」を復活させてしまう)。型不正
    (list/int/None 等) と空 dict は verify() 自体が拒否するため strict_keys に
    関わらず常に検証する。
    """
    if isinstance(value, str):
        return None
    if isinstance(value, dict):
        if not getattr(service, "ACCEPTS_DICT", False):
            return (
                f"{service.ACCOUNT_KEY}: この service はオブジェクト形式の値を"
                "受け付けません。文字列で指定してください。"
            )
        if not value:
            return f"{service.ACCOUNT_KEY}: オブジェクトが空です。"
        allowed_keys = getattr(service, "DICT_ALLOWED_KEYS", None)
        for k, v in value.items():
            if not isinstance(k, str):
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトのキーは文字列である"
                    "必要があります。"
                )
            if strict_keys and allowed_keys is not None and k not in allowed_keys:
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトのキー '{k}' は未対応です"
                    f" (許容: {', '.join(sorted(allowed_keys))})。"
                )
            if not isinstance(v, str) or not v:
                return (
                    f"{service.ACCOUNT_KEY}: オブジェクトの値は空でない文字列で"
                    f"ある必要があります (キー '{k}')。"
                )
        return None
    return (
        f"{service.ACCOUNT_KEY}: 値は文字列またはオブジェクトで指定してください "
        f"(現在: {type(value).__name__})。"
    )


def _cmd_init(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    service_key = args.service
    svc = _SERVICE_BY_KEY.get(service_key)
    if svc is None:
        print(
            f"error: unknown service '{service_key}'. "
            f"Available: {', '.join(_SERVICE_NAMES)}",
            file=stderr,
        )
        return 2

    project_dir = _project_dir()
    target = paths.accounts_file_new(project_dir)

    # 旧パス (deprecated/legacy) が存在する場合、init で新パスに書くと
    # dispatcher の _find_accounts_file が複数パス conflict で fail-closed deny に
    # 回帰するため refuse。先に migrate --commit で統合してから init を実行させる。
    found = paths.discover_all_accounts_files(project_dir)
    legacy_paths = [(kind, path) for kind, path in found if kind != "new"]
    if legacy_paths:
        print(
            "error: 旧パスに accounts.local.json が存在します。"
            "init で新パスに書き込むと複数パス conflict で fail-closed deny に"
            "回帰するため拒否します。",
            file=stderr,
        )
        for kind, path in legacy_paths:
            print(f"  - {path} ({kind})", file=stderr)
        print(
            "先に migrate --commit で新パスへ統合してから init を実行してください: "
            "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/"
            "accounts_builder.py migrate --commit",
            file=stderr,
        )
        return 1

    try:
        existing = _load_existing(target)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return 1

    if args.value is not None:
        new_entry: Any = _parse_value(args.value)
    else:
        new_entry = svc.suggest_accounts_entry(project_dir)
        if new_entry is None:
            print(
                f"error: {service_key} の現在値を CLI から取得できませんでした。"
                " --value で明示するか、CLI ログイン後に再実行してください。",
                file=stderr,
            )
            return 1

    shape_error = _validate_entry_shape(svc, new_entry)
    if shape_error:
        print(f"error: {shape_error}", file=stderr)
        return 1

    existing_value = existing.get(service_key)
    if existing_value is None:
        action = "add"
    elif existing_value == new_entry:
        action = "unchanged"
    else:
        action = "skipped"

    print(f"=== changes to {target} ===", file=stdout)
    if action == "add":
        _print_change_line("+ add", service_key, new_entry, args.show_values, stdout)
    elif action == "unchanged":
        _print_change_line("= unchanged", service_key, existing_value, args.show_values, stdout)
    else:
        print(
            f"! skipped: {service_key} already exists with a different value.",
            file=stdout,
        )
        print(
            "  init does not overwrite existing entries. Use "
            f"`set --service {service_key} --value <new-value> --commit` "
            "(or `--from-cli`) to update it, or "
            f"`remove --service {service_key} --commit` to delete it first.",
            file=stdout,
        )
        _print_change_line("  existing", service_key, existing_value, args.show_values, stdout)
        _print_change_line("  proposed", service_key, new_entry, args.show_values, stdout)

    if args.commit:
        updated = dict(existing)
        if action == "add":
            updated[service_key] = new_entry
        try:
            _write_json(target, updated)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {target}", file=stdout)
        _ensure_project_claude_md(project_dir, stdout)
        _ensure_gitignore_entry(project_dir, stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _refuse_if_legacy_paths_exist(
    command: str, project_dir: str, stderr: IO[str]
) -> bool:
    """旧パスが存在すれば True を返しつつ refuse 理由を stderr に書く。

    init と同じ理由 (新パスへの書込で dispatcher が複数パス conflict の
    fail-closed deny に回帰する) を set / remove にも適用する。
    """
    found = paths.discover_all_accounts_files(project_dir)
    legacy_paths = [(kind, path) for kind, path in found if kind != "new"]
    if not legacy_paths:
        return False
    print(
        "error: 旧パスに accounts.local.json が存在します。"
        f"{command} で新パスを操作すると複数パス conflict で fail-closed deny に"
        "回帰するため拒否します。",
        file=stderr,
    )
    for kind, path in legacy_paths:
        print(f"  - {path} ({kind})", file=stderr)
    print(
        f"先に migrate --commit で新パスへ統合してから {command} を実行してください: "
        "python3 ${CLAUDE_PLUGIN_ROOT}/hooks/verify-cloud-account/scripts/"
        "accounts_builder.py migrate --commit",
        file=stderr,
    )
    return True


def _cmd_set(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    service_key = args.service
    svc = _SERVICE_BY_KEY.get(service_key)
    if svc is None:
        print(
            f"error: unknown service '{service_key}'. "
            f"Available: {', '.join(_SERVICE_NAMES)}",
            file=stderr,
        )
        return 2

    if args.host is not None and not getattr(svc, "ACCEPTS_DICT", False):
        print(
            f"error: {service_key} はオブジェクト形式の値を受け付けないため "
            "--host は使えません。--host を外して --value に文字列を指定して"
            "ください。",
            file=stderr,
        )
        return 2

    project_dir = _project_dir()
    target = paths.accounts_file_new(project_dir)

    if _refuse_if_legacy_paths_exist("set", project_dir, stderr):
        return 1

    try:
        existing = _load_existing(target)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return 1

    if args.value is not None:
        raw_new_value: Any = _parse_value(args.value)
    else:
        raw_new_value = svc.suggest_accounts_entry(project_dir)
        if raw_new_value is None:
            print(
                f"error: {service_key} の現在値を CLI から取得できませんでした。"
                " --value で明示するか、CLI ログイン後に再実行してください。",
                file=stderr,
            )
            return 1

    existing_value = existing.get(service_key)

    if args.host is not None:
        if not isinstance(raw_new_value, str):
            print(
                "error: --host 指定時は書き込む値が文字列である必要があります "
                f"(現在: {type(raw_new_value).__name__})。--from-cli が複数"
                "host/alias 分の dict を返す場合は --host を外して実行するか、"
                "--value で単一の値を明示してください。",
                file=stderr,
            )
            return 1
        if isinstance(existing_value, str):
            print(
                f"error: {service_key} の既存値は文字列です。--host は既存値が"
                " オブジェクトのとき (または未設定のとき) だけ使えます。先に "
                f"`remove --service {service_key} --commit` で削除してから "
                "オブジェクト形式で作り直すか、--host を外して上書きして"
                "ください。",
                file=stderr,
            )
            return 1
        base_dict = dict(existing_value) if isinstance(existing_value, dict) else {}
        new_entry: Any = {**base_dict, args.host: raw_new_value}
    else:
        new_entry = raw_new_value

    shape_error = _validate_entry_shape(svc, new_entry)
    if shape_error:
        print(f"error: {shape_error}", file=stderr)
        return 1

    if existing_value is None:
        action = "add"
    elif existing_value == new_entry:
        action = "unchanged"
    else:
        action = "update"

    print(f"=== changes to {target} ===", file=stdout)
    if action == "add":
        _print_change_line("+ add", service_key, new_entry, args.show_values, stdout)
    elif action == "unchanged":
        _print_change_line(
            "= unchanged", service_key, existing_value, args.show_values, stdout
        )
    else:
        _print_change_line(
            "- current", service_key, existing_value, args.show_values, stdout
        )
        _print_change_line("+ new", service_key, new_entry, args.show_values, stdout)

    if args.commit:
        updated = dict(existing)
        updated[service_key] = new_entry
        try:
            _write_json(target, updated)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {target}", file=stdout)
        _ensure_project_claude_md(project_dir, stdout)
        _ensure_gitignore_entry(project_dir, stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _cmd_remove(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    service_key = args.service
    svc = _SERVICE_BY_KEY.get(service_key)
    if svc is None:
        print(
            f"error: unknown service '{service_key}'. "
            f"Available: {', '.join(_SERVICE_NAMES)}",
            file=stderr,
        )
        return 2

    if args.host is not None and not getattr(svc, "ACCEPTS_DICT", False):
        print(
            f"error: {service_key} はオブジェクト形式の値を受け付けないため "
            "--host は使えません。--host を外してキー全体を削除してください。",
            file=stderr,
        )
        return 2

    project_dir = _project_dir()
    target = paths.accounts_file_new(project_dir)

    if _refuse_if_legacy_paths_exist("remove", project_dir, stderr):
        return 1

    try:
        existing = _load_existing(target)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return 1

    existing_value = existing.get(service_key)
    if existing_value is None:
        print(f"{service_key} は {target} に存在しません。何もしません。", file=stdout)
        return 0

    if args.host is not None:
        if not isinstance(existing_value, dict):
            print(
                f"error: {service_key} の既存値はオブジェクトではありません "
                f"(現在: {type(existing_value).__name__})。--host は既存値が"
                "オブジェクトのときだけ使えます。--host を外してキー全体を"
                "削除してください。",
                file=stderr,
            )
            return 1
        if args.host not in existing_value:
            print(
                f"{service_key} に host/alias '{args.host}' は存在しません。"
                "何もしません。",
                file=stdout,
            )
            return 0
        removed_value = existing_value[args.host]
        remaining = {k: v for k, v in existing_value.items() if k != args.host}
        drop_whole_key = not remaining
    else:
        removed_value = existing_value
        remaining = {}
        drop_whole_key = True

    print(f"=== changes to {target} ===", file=stdout)
    if args.host is not None and not drop_whole_key:
        _print_change_line(
            "- remove", f"{service_key}[{args.host}]", removed_value,
            args.show_values, stdout,
        )
    elif args.host is not None:
        # 最後の 1 つの host/alias を消す = キー自体を残す意味が無い
        # (dict が空になると github/firebase/gcloud の verify() は
        # 「オブジェクトが空です」で永久 deny になるため、空 dict を残さず
        # 未記載 = 検証対象外の状態に戻す)。
        _print_change_line(
            "- remove (最後の host/alias のためキー全体を削除)",
            f"{service_key}[{args.host}]", existing_value,
            args.show_values, stdout,
        )
    else:
        _print_change_line(
            "- remove", service_key, existing_value, args.show_values, stdout
        )

    if args.commit:
        updated = dict(existing)
        if drop_whole_key:
            updated.pop(service_key, None)
        else:
            updated[service_key] = remaining
        try:
            _write_json(target, updated)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {target}", file=stdout)
        _ensure_project_claude_md(project_dir, stdout)
        _ensure_gitignore_entry(project_dir, stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _entries_equal(expected: Any, current: Any) -> bool:
    if expected == current:
        return True
    if isinstance(expected, str) and isinstance(current, dict):
        # services/github.py::verify と整合: scalar expected の場合は
        # multi-host current の最初のホスト (= next(iter(active.values())))
        # のみと比較する。show と verify (実 hook) で挙動を一致させ、
        # multi-host で show が [match] と表示するのに hook 側で deny される
        # 乖離を防ぐ。
        return next(iter(current.values()), None) == expected
    if isinstance(expected, dict) and isinstance(current, str):
        # Firebase の alias map (例: {"default":"p1","prod":"p2"}) に対し、
        # CLI 由来の current が scalar (アクティブ project ID) のとき、
        # map の任意 value に一致すれば match (firebase.verify と同じ意味論)
        return any(v == current for v in expected.values())
    if isinstance(expected, dict) and isinstance(current, dict):
        return all(current.get(k) == v for k, v in expected.items())
    return False


def _cmd_show(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    project_dir = _project_dir()
    found = paths.discover_all_accounts_files(project_dir)

    if not found:
        target = paths.accounts_file_new(project_dir)
        print(f"no accounts.local.json found at {target}", file=stdout)
        print(
            "run `accounts_builder.py init --service <name> --commit` to create one.",
            file=stdout,
        )
        return 0

    if len(found) >= 2:
        print(
            "error: 複数のパスに accounts.local.json が存在します (fail-closed).",
            file=stderr,
        )
        for kind, path in found:
            print(f"  - {path} ({kind})", file=stderr)
        print(
            "run `accounts_builder.py migrate --commit` to integrate.",
            file=stderr,
        )
        return 1

    kind, path = found[0]
    try:
        existing = _load_existing(path)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return 1

    print(f"=== {path} ({kind}) ===", file=stdout)
    if not existing:
        print("(empty)", file=stdout)
        return 0

    services_filter = [args.service] if args.service else None

    for key in sorted(existing.keys()):
        if services_filter and key not in services_filter:
            continue
        expected = existing[key]
        svc = _SERVICE_BY_KEY.get(key)

        expected_display = _format_value_for_display(expected, args.show_values)
        status_marker = ""
        detail = ""

        if svc is not None:
            try:
                current = svc.get_active_account(project_dir)
            except Exception as e:  # noqa: BLE001 — CLI 失敗は握り潰す
                current = None
                detail = f" (CLI error: {e})"
            if current is None:
                status_marker = "[CLI unavailable or not logged in]"
            elif _entries_equal(expected, current):
                status_marker = "[match]"
            else:
                status_marker = "[mismatch]"
                if args.show_values:
                    detail = f" current={json.dumps(current, ensure_ascii=False)}"
        else:
            status_marker = "[unknown service]"

        print(f"{key}: {expected_display}  {status_marker}{detail}", file=stdout)

    if kind != "new":
        print("", file=stdout)
        print(
            f"warning: このファイルは {kind} パスです。"
            " migrate --commit で新パスへ統合してください。",
            file=stdout,
        )

    return 0


def _cmd_migrate(
    args: argparse.Namespace,
    stdout: IO[str],
    stderr: IO[str],
) -> int:
    project_dir = _project_dir()
    new_path = paths.accounts_file_new(project_dir)
    found = paths.discover_all_accounts_files(project_dir)

    if not found:
        print("no accounts.local.json found in any path. nothing to migrate.", file=stdout)
        return 0

    if len(found) == 1 and found[0][0] == "new":
        print(f"only new path exists; nothing to migrate:\n  {new_path}", file=stdout)
        return 0

    sources: dict[str, dict[str, Any]] = {}
    source_paths: dict[str, Path] = {}
    for kind, path in found:
        try:
            sources[kind] = _load_existing(path)
        except _BuilderError as e:
            print(f"error: {e}", file=stderr)
            return 1
        source_paths[kind] = path

    merged: dict[str, Any] = dict(sources.get("new", {}))
    additions: list[tuple[str, Any, str]] = []  # (key, value, source_kind)
    conflicts: list[tuple[str, Any, Any, str]] = []  # (key, new_val, old_val, old_kind)

    for kind in ("deprecated", "legacy"):
        if kind not in sources:
            continue
        for key, value in sources[kind].items():
            if key not in merged:
                merged[key] = value
                additions.append((key, value, kind))
            elif not _entries_equal(merged[key], value):
                # _entries_equal は「意味的に同じアカウント」を判定する既存の
                # show/verify 用の関数だが、ここでは新旧どちらも accounts.local.json
                # の**設定値**である (CLI 実測値ではない) ため、new 側 (merged[key])
                # を expected、old 側 (value) を current として渡す。例:
                # new="Mao-o" (scalar) / old={"github.com":"Mao-o"} (dict) は
                # 意味的に同一アカウントなので conflict にせず new 側を維持する
                # (内部バックログ: 意味的に等価な新旧値も値衝突として手動解決を
                # 要求していた不具合の修正)。
                conflicts.append((key, merged[key], value, kind))

    if conflicts:
        print(
            "error: 同一キーで値が衝突しています (自動マージは安全でないため deny):",
            file=stderr,
        )
        for key, new_val, old_val, old_kind in conflicts:
            new_display = _format_value_for_display(new_val, args.show_values)
            old_display = _format_value_for_display(old_val, args.show_values)
            print(
                f"  - {key}: new={new_display}, {old_kind}={old_display}",
                file=stderr,
            )
        print(
            "手動で正しい値に合わせてから再実行してください。",
            file=stderr,
        )
        return 1

    # additions (旧パスにしかなかったキー) だけを書込前に形チェックする。
    # merged 全体 (new 側の既存エントリを含む) を検証すると、migrate が触って
    # すらいない既存データの形が (verify() では許容されている緩い形でも) 不合格
    # というだけで、無関係な統合作業まで exit 1 にしてしまう
    # (strict_keys=False: gcloud の未知キー等、verify() が黙って無視するだけの
    # 形は migrate では許容し、型不正 (list/int/None 等) と空 dict だけを拒否する)。
    # 内部バックログ: 旧ファイルの値型を検証せず list 等がそのまま merged に入り、
    # 後で dispatcher が deny する (書込時ではなく実行時に初めて発覚する) 不具合の修正。
    invalid: list[tuple[str, str]] = []
    for key, value, _kind in additions:
        svc = _SERVICE_BY_KEY.get(key)
        if svc is None:
            continue
        reason = _validate_entry_shape(svc, value, strict_keys=False)
        if reason:
            invalid.append((key, reason))
    if invalid:
        print(
            "error: 旧パスから取り込む値の形式が不正です (書き込みを中止します):",
            file=stderr,
        )
        for _key, reason in invalid:
            print(f"  - {reason}", file=stderr)
        print(
            "旧ファイルの該当キーを手動で修正するか削除してから再実行してください。",
            file=stderr,
        )
        return 1

    print(f"=== migrate to {new_path} ===", file=stdout)
    existing_new = sources.get("new", {})
    for key in sorted(merged.keys()):
        in_new = key in existing_new
        if in_new:
            _print_change_line("= unchanged", key, merged[key], args.show_values, stdout)
        else:
            source_kind = next((k for k, v, kk in additions if k == key), "old")
            matching = [(v, kk) for (k2, v, kk) in additions if k2 == key]
            if matching:
                source_kind = matching[0][1]
            _print_change_line(
                f"+ merged from {source_kind}",
                key,
                merged[key],
                args.show_values,
                stdout,
            )

    if args.commit:
        try:
            _write_json(new_path, merged)
        except OSError as e:
            print(f"error: 書き込みに失敗しました: {e}", file=stderr)
            return 1
        print(f"\nwritten: {new_path}", file=stdout)
        _ensure_project_claude_md(project_dir, stdout)
        _ensure_gitignore_entry(project_dir, stdout)
        retained = [
            (kind, path)
            for kind, path in source_paths.items()
            if kind != "new"
        ]
        if retained:
            print(
                "\n旧パスは保持されています (安全のため自動削除しません)。"
                "不要なら手動削除してください:",
                file=stdout,
            )
            for _kind, path in retained:
                print(f"  rm {path}", file=stdout)
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="accounts_builder",
        description="accounts.local.json の唯一の書込経路 (D1-D5).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="新規 service entry を追加")
    p_init.add_argument(
        "--service",
        required=True,
        choices=_SERVICE_NAMES,
        help="対象サービス",
    )
    p_init.add_argument(
        "--value",
        default=None,
        help=(
            "値を明示指定 (省略時は CLI から suggest_accounts_entry() で取得)。"
            "JSON object (例: '{\"project\":\"p\",\"account\":\"a\"}') は dict "
            "として保存、それ以外 (不正な JSON や dict 以外の JSON 型) は"
            "生文字列として保存する"
        ),
    )
    mx_init = p_init.add_mutually_exclusive_group()
    mx_init.add_argument("--dry-run", action="store_true")
    mx_init.add_argument("--commit", action="store_true")
    p_init.add_argument(
        "--show-values",
        action="store_true",
        help="stdout に値を露出する (デフォルトは隠蔽)",
    )

    p_show = sub.add_parser("show", help="現在の accounts.local.json を表示")
    p_show.add_argument(
        "--service",
        default=None,
        choices=_SERVICE_NAMES,
        help="対象サービスで絞り込む (省略時は全件)",
    )
    p_show.add_argument("--show-values", action="store_true")

    p_migrate = sub.add_parser("migrate", help="旧パスから新パスへ統合")
    mx_migrate = p_migrate.add_mutually_exclusive_group()
    mx_migrate.add_argument("--dry-run", action="store_true")
    mx_migrate.add_argument("--commit", action="store_true")
    p_migrate.add_argument("--show-values", action="store_true")

    p_set = sub.add_parser(
        "set", help="既存 service entry を更新 (無ければ新規追加)"
    )
    p_set.add_argument(
        "--service",
        required=True,
        choices=_SERVICE_NAMES,
        help="対象サービス",
    )
    mx_set_value = p_set.add_mutually_exclusive_group(required=True)
    mx_set_value.add_argument(
        "--value",
        default=None,
        help=(
            "新しい値を明示指定。JSON object は dict として保存、それ以外は"
            "生文字列として保存する (init と同じ解釈)"
        ),
    )
    mx_set_value.add_argument(
        "--from-cli",
        action="store_true",
        help="CLI から現在値を取得して使用する (suggest_accounts_entry())",
    )
    p_set.add_argument(
        "--host",
        default=None,
        help=(
            "dict 値の特定キー (GitHub の hostname / Firebase の alias 等) "
            "だけを追加・上書きする。省略時はキー全体を置き換える"
        ),
    )
    mx_set = p_set.add_mutually_exclusive_group()
    mx_set.add_argument("--dry-run", action="store_true")
    mx_set.add_argument("--commit", action="store_true")
    p_set.add_argument(
        "--show-values",
        action="store_true",
        help="stdout に値を露出する (デフォルトは隠蔽)",
    )

    p_remove = sub.add_parser(
        "remove", help="既存 service entry (または dict の特定キー) を削除"
    )
    p_remove.add_argument(
        "--service",
        required=True,
        choices=_SERVICE_NAMES,
        help="対象サービス",
    )
    p_remove.add_argument(
        "--host",
        default=None,
        help=(
            "dict 値の特定キー (GitHub の hostname / Firebase の alias 等) "
            "だけを削除する。省略時はキー全体を削除する"
        ),
    )
    mx_remove = p_remove.add_mutually_exclusive_group()
    mx_remove.add_argument("--dry-run", action="store_true")
    mx_remove.add_argument("--commit", action="store_true")
    p_remove.add_argument(
        "--show-values",
        action="store_true",
        help="stdout に値を露出する (デフォルトは隠蔽)",
    )

    return parser


def main(
    argv: list[str] | None = None,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    parser = _build_parser()
    # argparse が stderr に直書きするため、stderr injection を反映するには
    # parse 中のみ sys.stderr を差し替える
    original_stderr = sys.stderr
    try:
        sys.stderr = stderr
        try:
            args = parser.parse_args(argv)
        except SystemExit as e:
            return int(e.code) if e.code is not None else 2
    finally:
        sys.stderr = original_stderr

    if args.command == "init":
        return _cmd_init(args, stdout, stderr)
    if args.command == "show":
        return _cmd_show(args, stdout, stderr)
    if args.command == "migrate":
        return _cmd_migrate(args, stdout, stderr)
    if args.command == "set":
        return _cmd_set(args, stdout, stderr)
    if args.command == "remove":
        return _cmd_remove(args, stdout, stderr)

    parser.print_help(file=stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
