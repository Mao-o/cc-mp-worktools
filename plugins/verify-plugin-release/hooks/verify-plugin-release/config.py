"""repo ごとの設定 (`<repo>/.claude/verify-plugin-release.json`) の読み込み。

ファイルが無ければ既定値で動く。壊れた設定は ConfigError にして、呼び出し側で
「ゲートを完了できなかった」扱い (= PR 作成を止める) にする。黙って既定値に
戻すと、利用者が有効にしたつもりの検査が効いていないことに気付けないため。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

CONFIG_RELPATH = Path(".claude") / "verify-plugin-release.json"

# hooks.json の timeout (120 秒) より短くしておく。hook 自体が時間切れになると
# Claude Code はコマンドをそのまま実行する (公式仕様) ため、ゲート内部で先に
# 打ち切って「止める」判断を返す必要がある。
DEFAULT_TIMEOUT = 90
MAX_TIMEOUT = 110


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    # 1 つの PR で複数の plugin (または plugin と repo 直下のファイル) を
    # 同時に変更していたら FAIL にする。
    single_plugin_per_pr: bool = False
    # `claude plugin validate` の warning を FAIL として扱う (既定は WARN)。
    strict_validate: bool = False
    # base branch を検査前に `git fetch` する。
    fetch: bool = True
    # ゲート全体の制限時間 (秒)。
    timeout_seconds: int = DEFAULT_TIMEOUT
    # None = 自動検出 (Python unittest) / False = テストを走らせない /
    # list[str] = 変更された各 plugin のディレクトリで実行するコマンド。
    test_command: list[str] | bool | None = None


_BOOL_KEYS = ("single_plugin_per_pr", "strict_validate", "fetch")


def load(repo_root: Path) -> Config:
    path = repo_root / CONFIG_RELPATH
    if not path.is_file():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"{CONFIG_RELPATH.as_posix()} を読めない: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_RELPATH.as_posix()} は JSON object である必要がある")

    known = set(_BOOL_KEYS) | {"timeout_seconds", "test_command"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(f"未知の設定キー: {', '.join(unknown)}")

    kwargs: dict[str, object] = {}
    for key in _BOOL_KEYS:
        if key in data:
            if not isinstance(data[key], bool):
                raise ConfigError(f"{key} は true / false で指定する")
            kwargs[key] = data[key]

    if "timeout_seconds" in data:
        t = data["timeout_seconds"]
        if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
            raise ConfigError("timeout_seconds は正の整数で指定する")
        kwargs["timeout_seconds"] = min(t, MAX_TIMEOUT)

    if "test_command" in data:
        tc = data["test_command"]
        if tc is None or tc is False:
            kwargs["test_command"] = tc
        elif isinstance(tc, list) and tc and all(isinstance(x, str) and x for x in tc):
            kwargs["test_command"] = list(tc)
        else:
            raise ConfigError(
                "test_command は null / false / 空でない文字列の配列で指定する"
            )

    return Config(**kwargs)
