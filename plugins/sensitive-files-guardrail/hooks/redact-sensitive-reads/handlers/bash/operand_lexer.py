"""operand トークンの glob 判定 / dotenv glob 判定 / path 候補抽出
(0.3.3 分解、0.8.0 で `_literalize` / `_glob_candidates` / `_is_absolute_or_relative_path_exec` 撤廃、
0.22.0 で path 候補抽出にコマンド別の option 知識 (``command_specs._CmdSpec``) を導入)。

このモジュールは副作用なし・plugin 状態非依存。``SFG_CASE_SENSITIVE`` 環境変数の
参照のみ外部状態 (全テストで同じ解釈をしたいため、``is_sensitive`` 側の opt-out
と整合させる)。

``_operand_is_sensitive`` は ``is_sensitive`` / ``normalize`` に依存する plugin
ステート側の処理なので ``bash_handler.py`` に残す。

### path 候補抽出の設計 (0.22.0)

0.21.x までの ``_find_path_candidates`` は「option で始まらない token = path 候補、
``--opt=value`` / ``-Xvalue`` の value も候補」の一律規則だった。このため

- grep 系 / jq / awk / sed の **第 1 positional** (pattern / filter / script) が
  path 候補になり、``grep .env README.md`` / ``jq '.env' package.json`` /
  ``sed -n 's/.env/X/p' README.md`` が「``.env`` を読む」と誤認されて hard deny
- 値が path ではない option の値 (``git log -S.env`` / ``--exclude='.env'``) が
  候補になり、**ユーザーが「.env を読ませない」と明示している** ``grep -rn TODO
  --exclude='.env'`` まで deny

していた (2026-08 精査。実ログの hard deny のうち git / grep / sed / awk で 48%)。

コマンド別の ``_CmdSpec`` (値を取る option とその値の種別、第 1 positional が
pattern か) を持ち、token 列を「option / option の値 / positional / redirect」に
字句分けしてから候補を決める。**spec に無いコマンド・option は 0.21.x までの
規則そのまま** (分離形の値は bare token として候補、``=`` / 密着形の値は候補)。
spec への登録は「deny を外す側」なので、漏れ = 現状維持 (false positive が残る
だけ)、誤登録 (値を取らない option を値あり、path の値を non-path と書く) だけが
保護の穴になる。登録する option は **全形で必ず値を取るもの** に限り、
``--color[=WHEN]`` のような値省略可の option は登録しない (分離形の次 token を
値と誤認して path を読み飛ばすため)。

metadata-only gate (``bash_handler._reads_file_content`` /
``_git_ls_files_exposes_object``) は **生の token 列** で判定しているため、ここでの
除外は gate の判定に影響しない (``file -f .env`` / ``wc --files0-from=.env`` /
``tree --fromfile .env`` / ``git ls-files -s .env`` は gate を抜けた後の operand
scan で従来どおり deny)。
"""
from __future__ import annotations

import os
from fnmatch import fnmatchcase

from handlers.bash.command_specs import (
    _SPECS,
    _VALUE_PATH,
    _CmdSpec,
    _split_kinds,
)
from handlers.bash.constants import _GIT_GLOBAL_VALUE_OPTS, _GLOB_CHARS
from handlers.bash.redirects import _WRITE_REDIRECT_RE

# dotenv ファミリーで「うっかり頻出」とする literal stem (0.8.0)。
# operand glob (例: ``.env*``) がこれらに shell の pathname expansion と同じ意味論で
# 一致するときだけ deny に倒す。それ以外の glob (``id_rsa*`` / ``*.key`` /
# ``cred*.json`` / ``*.log`` 等) は ``ask_or_allow`` に格下げ (思想 1: うっかり
# 露出予防が目的、敵対的防御は非目的)。
_DOTENV_GLOB_STEMS = (".env", ".envrc")


def _has_glob(token: str) -> bool:
    """operand に shell glob 文字 (``*``, ``?``, ``[``) が含まれるか。"""
    return any(c in _GLOB_CHARS for c in token)


def _glob_operand_is_dotenv_match(operand: str) -> bool:
    """operand glob が **shell の pathname expansion で** dotenv 系の literal stem
    (``.env`` / ``.envrc``) に展開されうるか。

    判定 (0.22.0 で shell の意味論に合わせた):

    1. 展開は path 要素ごとなので、operand の **最後の path 要素** (basename 側の
       glob) だけを stem と比較する。``*/.env`` は ``sub/.env`` に展開される
    2. ファイル名先頭の ``.`` は pattern 先頭の **literal ``.``** でしか一致しない
       (POSIX 2.13.3、bash 3.2 / zsh 5 実測、dotglob 未設定の既定挙動)。``*`` /
       ``?`` / bracket 式 (``[.]``) は先頭ドットに一致しない。stem は全て ``.``
       始まりなので、basename glob が ``.`` で始まらなければ一致しない
    3. その上で ``fnmatchcase(stem, basename_glob)``

    例:
    - ``.env*`` → deny (``.env`` ``.envrc`` に展開)
    - ``.en?`` / ``.e[n]v`` / ``.*`` → deny
    - ``*/.env`` / ``**/.env`` / ``*/.env*`` → deny (0.21.x は operand 全体を
      fnmatch して ask_or_allow だった)
    - ``*`` / ``?env`` / ``*env`` / ``[.]env`` → 一致しない → ask_or_allow
      (0.21.x は ``fnmatchcase(".env", "*")`` = True で裸の ``*`` が全 mode deny
      になり、``git add *`` / ``cp * dst/`` / heredoc 本文の ``kb * 1024`` が
      止まっていた)
    - ``*.envrc`` → ``.envrc`` には展開されない → ask_or_allow (``foo.envrc``
      への展開は ``*.key`` / ``cred*.json`` と同じ「既定 rules との交差」クラス。
      0.21.x は fnmatch の意味論で deny)
    - ``.env.*`` → ``fnmatchcase(".env", ".env.*")`` = False (".env." 以降が必要)
      → ask_or_allow
    - ``id_rsa*`` / ``*.key`` / ``cred*.json`` / ``*.log`` / ``.env.example*`` →
      どちらの stem にも一致しない → ask_or_allow

    0.3.2〜0.7.x で行っていた既定 rules への候補列挙
    (``_glob_candidates`` / ``_glob_operand_is_sensitive``) は思想 1 に対して
    deny 寄り過ぎる (``cat *.json`` / ``cat *.key`` / ``cat id_rsa*`` を全 mode
    deny する) と判断し 0.8.0 で撤廃した。dotenv stem (``.env`` / ``.envrc``)
    のうっかり頻出ケースだけを残す形に縮約。

    ``SFG_CASE_SENSITIVE=1`` 未設定時は lower 比較する (``is_sensitive`` 側の
    opt-out と整合)。
    """
    if not operand:
        return False
    cs = os.environ.get("SFG_CASE_SENSITIVE") == "1"
    op = operand if cs else operand.lower()
    stem_glob = op.rsplit("/", 1)[-1]
    if not stem_glob:
        return False
    for stem in _DOTENV_GLOB_STEMS:
        if stem.startswith(".") and not stem_glob.startswith("."):
            continue  # 先頭ドットは literal ``.`` でしか一致しない
        if fnmatchcase(stem, stem_glob):
            return True
    return False


# ``_lex_args`` が返す entry の種別。
_K_OPT = "opt"      # option 名 (``-e`` / ``--regexp``。束ねは 1 文字ずつ ``-x``)
_K_NON_PATH = "n"   # spec で non-path と分かっている option の値
_K_PATH = "p"       # spec で path と分かっている option の値 / redirect の書込み先
_K_UNKNOWN = "x"    # spec に無い option の ``=`` / 密着値 (0.21.x 互換で候補)
_K_POS = "pos"      # positional (pattern 枠の判定対象)


def _git_subcommand_index(tokens: list[str]) -> int | None:
    """``git`` の global option を読み飛ばし、サブコマンド token の index を返す。

    ``interpreters._git_global_options`` と同じ規則 (``_GIT_GLOBAL_VALUE_OPTS`` は
    次の token を値として消費、それ以外の ``-`` 始まりは 1 token)。
    サブコマンドが無ければ None。
    """
    i = 1
    n = len(tokens)
    while i < n:
        t = tokens[i]
        if t in _GIT_GLOBAL_VALUE_OPTS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        return i
    return None


def _resolve_long_option(name: str, values: dict[str, str]) -> str | None:
    """``--NAME`` を spec の long option に解決する (完全一致 or 一意な prefix)。

    GNU getopt_long / git parse-options は一意な prefix 省略 (``--reg`` =
    ``--regexp``) を受理する。spec が知らない option の prefix を spec 内の別 option
    に誤って解決しても、実コマンド側では曖昧 (error) か別の意味になるだけで、
    こちらは pattern 枠を閉じる / 値を読み飛ばす方向 (= deny 寄り or 実行されない)
    にしか倒れない。
    """
    if name in values:
        return name
    cands = [k for k in values if k.startswith("--") and k.startswith(name)]
    return cands[0] if len(cands) == 1 else None


def _lex_args(args: list[str], spec: _CmdSpec | None) -> list[tuple[str, str]]:
    """引数 token 列を ``(種別, 文字列)`` の entry 列に字句分けする。

    - ``--`` 以降は全て positional
    - 書込み redirect (``>`` ``>>`` ``2>`` ``&>`` ``>|``、safe-read コマンドでは
      residual metachar 判定を skip するためここまで届く) は、分離形なら次 token、
      密着形 (``>.env``) ならその target を path 値にする (書込み先が機密 path
      なら truncate なので候補に残す)
    - spec に登録された option は値の種別列に従って後続 token / 密着部 / ``=``
      の RHS を値として消費する
    - spec に無い option は 0.21.x までの規則 (``=`` の RHS / 短形の密着部は
      ``x``、分離形の値は消費せず次 token は positional)
    """
    values = spec.values if spec is not None else {}
    out: list[tuple[str, str]] = []
    pending = ""  # これから来る token を値として消費する種別列
    in_ddash = False
    for tok in args:
        if pending:
            out.append((pending[0], tok))
            pending = pending[1:]
            continue
        if in_ddash:
            out.append((_K_POS, tok))
            continue
        if tok == "--":
            in_ddash = True
            continue
        if tok == "-":
            # stdin。0.21.x までと同じく候補にしない (pattern 枠でもない)
            out.append((_K_OPT, tok))
            continue
        m = _WRITE_REDIRECT_RE.match(tok) if tok and tok[0] in ">&0123456789" else None
        if m:
            target = m.group("target")
            if not target:
                out.append((_K_OPT, tok))
                pending = _VALUE_PATH
            elif not target.startswith("&"):  # ``>&1`` / ``>&-`` の fd 複製は除外
                out.append((_K_PATH, target))
            else:
                out.append((_K_OPT, tok))
            continue
        if tok.startswith("--") and len(tok) > 2:
            name, eq, rhs = tok.partition("=")
            full = _resolve_long_option(name, values)
            if full is None:
                out.append((_K_OPT, name))
                if eq and rhs:
                    out.append((_K_UNKNOWN, rhs))
                continue
            out.append((_K_OPT, full))
            attached_only, kinds = _split_kinds(values[full])
            if eq:
                if rhs:
                    out.append((kinds[0], rhs))
                pending = kinds[1:]
            elif not attached_only:
                pending = kinds
            continue
        if tok.startswith("-") and len(tok) > 1:
            if "=" in tok:
                # ``-o=value`` (0.3.1 からの互換規則。GNU getopt は解釈しないが
                # 独自 parser のコマンドで使われる)
                name, _, rhs = tok.partition("=")
                out.append((_K_OPT, name))
                if rhs:
                    kinds = values.get(name)
                    kind = _split_kinds(kinds)[1][0] if kinds else _K_UNKNOWN
                    out.append((kind, rhs))
                continue
            letters = tok[1:]
            for k, ch in enumerate(letters):
                key = "-" + ch
                out.append((_K_OPT, key))
                kinds = values.get(key)
                if kinds is None:
                    continue
                attached_only, kinds = _split_kinds(kinds)
                attached = letters[k + 1:]
                if attached:
                    out.append((kinds[0], attached))
                    pending = kinds[1:]
                elif not attached_only:
                    pending = kinds
                break
            else:
                if len(tok) > 2 and spec is None:
                    # 値を取る option 文字が無い束ね / 密着形: spec の無いコマンド
                    # では 0.21.x までと同じく ``tok[2:]`` を候補に残す (``-f.env``
                    # 型の取りこぼし防止)。spec のあるコマンドは値を取る短形
                    # option を登録済みなので、残りは flag の束ね (``-rn``) として
                    # 扱う
                    out.append((_K_UNKNOWN, tok[2:]))
            continue
        out.append((_K_POS, tok))
    return out


def _command_spec(tokens: list[str]) -> tuple[_CmdSpec | None, int, int]:
    """first token (git はサブコマンド込み) の spec と、
    ``(global option 区間の終端 index, 引数の開始 index)`` を返す。

    git 以外は両 index とも 1。git はサブコマンド token を挟んで
    ``tokens[1:j]`` が global option、``tokens[j+1:]`` が引数。
    """
    first = tokens[0]
    if first == "git":
        j = _git_subcommand_index(tokens)
        if j is None:
            return None, 1, 1
        return _SPECS.get(f"git {tokens[j]}"), j, j + 1
    return _SPECS.get(first), 1, 1


def _find_path_candidates(tokens: list[str]) -> list[str]:
    """第 1 トークン以降から、path 候補を抽出。

    拾う形式:
    - ``--`` より後ろは無条件で path 扱い
    - 非 option トークン (``-`` で始まらない) はそのまま path 候補
    - ``--opt=value`` / ``-o=value`` の ``=`` 以降 (RHS) を候補に追加
    - 短形 option に value が **連結** した形 ``-X<value>`` (``-f.env`` 等) は
      ``tok[2:]`` を候補に追加

    0.22.0 からの例外 (コマンド別 ``_CmdSpec``、モジュール docstring 参照):
    - grep 系 / jq / awk / sed の **第 1 positional** (pattern / filter / script)
      は候補にしない。ただし pattern を option で与える形 (``-e`` / ``-f`` 等) が
      あれば positional は全て path
    - spec が「値は path ではない」と知っている option の値 (``-e PAT`` 等) は
      候補にしない。「値は path」と知っている option の値 (``-f FILE``) は候補
    - 書込み redirect の target (``> .env`` / ``>.env``) は path 候補 (0.21.x
      までは分離形のみ bare token として拾えていた)
    """
    if len(tokens) < 2:
        return []
    spec, opts_end, start = _command_spec(tokens)
    entries: list[tuple[str, str]] = []
    if opts_end > 1:
        # git の global option 区間 (``-C dir`` 等): 0.21.x までと同じ規則で
        # 候補化するが、pattern 枠の対象にはしない。サブコマンド token 自体
        # (``log`` / ``grep``) はコマンド語であって operand ではないので候補外
        for kind, text in _lex_args(tokens[1:opts_end], None):
            entries.append((_K_UNKNOWN if kind == _K_POS else kind, text))
    entries.extend(_lex_args(tokens[start:], spec))

    slot_open = spec is not None and spec.pattern_slot
    if slot_open and spec is not None and spec.pattern_opts:
        seen = {text for kind, text in entries if kind == _K_OPT}
        if seen & spec.pattern_opts:
            slot_open = False

    candidates: list[str] = []
    for kind, text in entries:
        if kind == _K_POS:
            if slot_open:
                slot_open = False  # pattern / filter / script 枠を消費
                continue
            candidates.append(text)
        elif kind in (_K_PATH, _K_UNKNOWN):
            candidates.append(text)
    return candidates
