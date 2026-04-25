"""accounts.local.json の builder (唯一の正規書込経路)。

accounts.local.json の編集は builder 経由で行う運用に統一する。動作の安定や
フォーマット統一のため、書込先パス・JSON フォーマット・既存キーの扱い・
stdout の値表示制御を builder 側で一元管理する。Agent Skill (`accounts-init`
`accounts-show` `accounts-migrate`) が対話フローを提供し、Claude は skill
経由で builder を呼ぶ。

設計判断 (D1〜D5):

- **D1**: builder が唯一の正規経路。書込パスの固定、JSON フォーマットの
  一貫化、既存キーの温存、CLI 現在値との突合、旧パス統合を一元管理する。
- **D2**: 書込対象パスは `core/paths.accounts_file_new()` に固定。argv から
  上書きできない (`_ALLOWED_BASENAME` で basename を assertion)。
- **D3**: stdout の値表示は既定で隠蔽。`--show-values` 明示時のみ値を
  stdout に出す。
- **D4**: 3-tier lookup で競合検出時は deny。migrate で統合する。
- **D5**: `migrate` サブコマンドで旧パス → 新パスへの統合を提供。
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

    try:
        existing = _load_existing(target)
    except _BuilderError as e:
        print(f"error: {e}", file=stderr)
        return 1

    if args.value is not None:
        new_entry: Any = args.value
    else:
        new_entry = svc.suggest_accounts_entry(project_dir)
        if new_entry is None:
            print(
                f"error: {service_key} の現在値を CLI から取得できませんでした。"
                " --value で明示するか、CLI ログイン後に再実行してください。",
                file=stderr,
            )
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
            "  init does not overwrite existing entries. Edit the file manually "
            "or use a future switch subcommand to change it.",
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
    else:
        print("\n(dry-run; pass --commit to write)", file=stdout)

    return 0


def _entries_equal(expected: Any, current: Any) -> bool:
    if expected == current:
        return True
    if isinstance(expected, str) and isinstance(current, dict):
        return any(v == expected for v in current.values())
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
            elif merged[key] != value:
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
        help="scalar 値を明示指定 (省略時は CLI から suggest_accounts_entry() で取得)",
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

    parser.print_help(file=stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
