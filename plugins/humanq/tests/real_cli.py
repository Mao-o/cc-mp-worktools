"""本物の bd / br を打つテストを、env を立てたときだけ走らせる判定 (test_cli_contract と test_server が共有する)。

env は HUMANQ_TEST_BD / HUMANQ_TEST_BR。値は起動コマンドの文字列で、例は "bd"、"br"、起動の前置き付きの
"<launcher> <args> -- br"。"1" なら PATH の bd / br を使う。空・未設定なら、bd / br を 1 回も起動せずに None を返す。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess


def clean_env() -> dict:
    """手元の設定が別のプロジェクトを指さないように、beads 系の env と偽物の env を外す。"""
    e = {k: v for k, v in os.environ.items() if not k.startswith(("FAKE_BEADS_", "BEADS_", "BD_", "BR_"))}
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    return e


def env_name(prog: str) -> str:
    return "HUMANQ_TEST_" + prog.upper()


def skip_reason(prog: str) -> str:
    """本物の脚を skip するときの理由 (変数名を出す)。"""
    return f"本物の {prog} の脚は env {env_name(prog)} を立てたときだけ走る (立っていないか、起動できない)"


def real_command(prog: str) -> list[str] | None:
    """本物の起動コマンド (argv の前置き)。env が無い・見つからない・起動できなければ None (--version も打たない)。"""
    spec = os.environ.get(env_name(prog), "").strip()
    if not spec:
        return None
    cmd = ([found] if (found := shutil.which(prog)) else None) if spec == "1" else shlex.split(spec)
    if not cmd:
        return None
    try:
        p = subprocess.run([*cmd, "--version"], capture_output=True, text=True, timeout=60, env=clean_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cmd if p.returncode == 0 else None
