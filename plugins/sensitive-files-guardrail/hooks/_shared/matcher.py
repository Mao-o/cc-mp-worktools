"""機密パス判定 (last-match-wins 版) — 両 hook 共通実装。

rule は書き方 (形) で比較対象が決まる (0.24.0):

- **basename 形** (``/`` を含まない。``.env`` / ``*.pem`` / ``!*.example``):
  basename と ``pathlib.parts`` の各要素に対して ``fnmatchcase`` で評価する。
  - basename 一致: ``.env``, ``secrets.yaml`` 等の典型ケース
  - parts 一致: ``/foo/.env/bar`` のように親ディレクトリが機密名でも検出
    (現実的な用途は少ないが、symlink race 等で偽装されたケースを拾う)。
    実在ファイルの実パスを扱う Read / Edit / Stop 向け。Bash operand のように
    「path とは限らない文字列」を判定する側は ``parts=False`` で basename だけを
    使う (0.22.0。``sed -n 's/.env/X/p'`` の式が合成パス ``/cwd/s/.env/X/p`` に
    なり parts の ``.env`` で deny になっていた)
- **path 形** (``/`` を含む。``config/prod.pem`` / ``fixtures/`` / ``!/certs/**``):
  project root からの相対 path 全体に対して gitignore 準拠の意味論で評価する
  (``_translate_path_pattern``)。basename や parts とは**比較しない**。root を
  渡されなかった (``root=None``) / path が root 配下でない場合は一度も一致しない
  (= 0.23.0 までの挙動と同じ)。恒久除外レシピを「承認した 1 ファイル」に絞る
  ための階層で、既定 patterns.txt には path 形の行が無いので既存 rule の挙動は
  変わらない。

rules は ``list[tuple[str, bool]]`` 形式で、各 tuple は ``(pattern, is_exclude)``。
評価はリスト先頭から全件走査し、**最後にマッチしたルールの符号**を採用する
(gitignore 風 last-match-wins)。basename 形と path 形は 1 本のリストとして
出現順に評価する (形ごとに階層を分けない — 「書いた順が強さ」の契約を守るため)。
既定 patterns.txt 末尾に書いた exclude を、ユーザーが ``patterns.local.txt`` 側で
再び include に差し戻せるようにするため。
"""
from __future__ import annotations

import os
import re
from fnmatch import translate
from pathlib import PurePath

# パターン -> compiled regex のキャッシュ (0.23.0)。
#
# 0.20.0 までは ``fnmatch.fnmatchcase`` を直接呼んでいたが、その内部
# ``fnmatch._compile_pattern`` の ``lru_cache`` は **maxsize が Python
# バージョン依存** (3.9 = 256 / 3.11+ = 32768) で plugin 側から制御できない。
# ``_verdict`` は rules 全件を operand ごとに走査するため、
# rules 件数が maxsize を超えると **operand 数 x rule 数のオーダーで正規表現が
# 再コンパイル**され、破局的に遅くなる。
#
# 実測 (system python 3.9.6 / 50 operands):
#   256 rules ->    11.0 ms
#   306 rules ->   338.5 ms   (31 倍のジャンプ)
#   856 rules -> 5,380.0 ms
# 同条件の 3.11+ は 856 rules でも 34.3 ms で線形。
#
# 到達経路は「多数プロジェクトへの分散」ではなく **1 つの長寿命 repo が自分の
# ``[project:]`` セクションに 250 件超の承認除外を蓄積する**こと — 本 plugin の
# 恒久除外レシピが誘導する使い方そのもの。
#
# 自前のキャッシュを持つことで Python バージョンへの依存を断つ。
#
# **上限を設けない** — 上限付き LRU では「rules 件数 > maxsize」で同じ崖が
# 再現するため。初版は maxsize=8192 にしていたが、3.11+ の fnmatch (32768) より
# 小さいので **8,192 件を超える rules ではむしろ退行**した
# (実測: 8,002 rules / 10 paths = 139.7ms に対し 8,202 rules = 4,884.3ms)。
# 「rule 数を包含する十分大きな値」を選ぶ方針自体が、上流の rule 数に依存する
# 調整パラメータを増やすだけで筋が悪い。
#
# 上限なしで安全な理由: hook プロセスは **1 ツール呼び出しで終了する短命プロセス**
# で、rules は起動時に patterns ファイルから 1 度読むだけ。したがって dict の
# エントリ数は「patterns ファイルの行数 x case 変種」で有界であり、
# 長時間走るプロセスのような無制限成長は起きない。
_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}
# path 形 rule 用 (0.24.0)。fnmatch とは別の translator を使うため別キャッシュ。
# 上限を設けない理由は ``_PATTERN_CACHE`` と同じ。
_PATH_PATTERN_CACHE: dict[str, "re.Pattern[str]"] = {}


def _compiled(pattern: str) -> "re.Pattern[str]":
    """``fnmatchcase`` と同じ意味論の compiled regex を返す (キャッシュ付き)。

    ``fnmatch.translate`` は ``\\Z`` 終端付きの完全一致パターンを返すため、
    ``.match()`` で ``fnmatchcase`` と等価になる。
    """
    hit = _PATTERN_CACHE.get(pattern)
    if hit is None:
        hit = re.compile(translate(pattern))
        _PATTERN_CACHE[pattern] = hit
    return hit


def _fnmatch_cached(name: str, pattern: str) -> bool:
    """``fnmatchcase(name, pattern)`` と等価 (自前キャッシュ経由)。"""
    return _compiled(pattern).match(name) is not None


def is_path_rule(pattern: str) -> bool:
    """rule が path 形 (root 相対 path と比較する形) か。``/`` を含めば path 形。"""
    return "/" in pattern


def _translate_path_pattern(pattern: str) -> str:
    """path 形 rule を root 相対 path (POSIX 区切り、先頭 ``/`` 無し) 用の
    正規表現に変換する。意味論は **gitignore 準拠** (fnmatch の継承ではない):

    - ``*`` / ``?`` / ``[...]`` は ``/`` を跨がない (fnmatch の ``*`` は跨ぐので
      そのまま使うと ``!*/prod.pem`` が期待と違う動きをする)
    - ``**`` は ``/`` を跨ぐ: 先頭 ``**/`` = 任意の深さ、末尾 ``/**`` = 配下すべて、
      中間 ``/**/`` = 0 個以上のディレクトリ。それ以外の連続 ``*`` は ``*`` 扱い
    - 先頭 ``/`` と ``./`` は root アンカー (取り除く)。``/`` を途中に含む rule は
      常に root 相対 (アンカー付き) で、末尾 ``/`` だけの rule (``fixtures/``)
      は任意の深さのディレクトリに一致する (gitignore と同じ)
    - 末尾 ``/`` はディレクトリ: その配下すべてに一致し、同名の**ファイル**には
      一致しない。末尾 ``/`` が**無い** rule は path 1 本にだけ一致する
      (gitignore は「ディレクトリに一致したら配下も」だが、除外は狭い方が
      安全なので採らない。配下を含めたければ末尾 ``/`` か ``/**`` を書く)
    - ``\\`` はエスケープではなく literal (basename 形の fnmatch と同じ)。
      メタ文字を literal にしたいときは ``[*]`` のように文字クラスで包む
      (``_shared.patterns.escape_glob`` がレシピ生成時に行う変換と同じ)
    - 正規表現として不正な文字クラス (逆順の範囲 ``[z-a]`` 等) を含む rule は
      **何にも一致しない** (``_compiled_path`` が ``re.error`` を never-match に
      畳む)。``fnmatch.translate`` が空になった範囲を ``(?!)`` にするのと同じ
      扱いで、壊れた local rule 1 行で全 tool が internal error deny / Stop が
      無言終了になるのを防ぐ (Codex R2 P2)
    """
    pat = pattern
    anchored = pat.startswith(("/", "./"))
    while pat.startswith("./"):
        pat = pat[2:]
    pat = pat.lstrip("/")
    dir_only = pat.endswith("/")
    pat = pat.rstrip("/")
    if not pat:
        return r"(?!)"  # ``/`` / ``./`` だけの rule は何にも一致しない

    out: list[str] = ["^"]
    if not anchored and "/" not in pat:
        # 末尾 / だけだった rule (``fixtures/``): 任意の深さ
        out.append(r"(?:.*/)?")

    i, n = 0, len(pat)
    while i < n:
        c = pat[i]
        if c == "*":
            j = i
            while j < n and pat[j] == "*":
                j += 1
            if j - i >= 2:
                prev_sep = i == 0 or pat[i - 1] == "/"
                next_sep = j == n or pat[j] == "/"
                if prev_sep and next_sep:
                    if j == n:
                        # 末尾 ``/**``: 配下すべて (直前の / は literal で出力済み)
                        out.append(".*")
                    else:
                        # 先頭 ``**/`` / 中間 ``/**/``: 0 個以上のディレクトリ
                        out.append(r"(?:.*/)?")
                        j += 1  # 後続の / を消費
                    i = j
                    continue
            out.append(r"[^/]*")
            i = j
            continue
        if c == "?":
            out.append(r"[^/]")
            i += 1
            continue
        if c == "[":
            j = i + 1
            if j < n and pat[j] == "!":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\[")
                i += 1
                continue
            stuff = pat[i + 1 : j]
            stuff = stuff.replace("\\", r"\\")
            stuff = re.sub(r"([&~|])", r"\\\1", stuff)
            if stuff[0] == "!":
                stuff = "^" + stuff[1:]
            elif stuff[0] == "^":
                stuff = "\\" + stuff
            # 文字クラスも / には一致させない
            out.append(f"(?!/)[{stuff}]")
            i = j + 1
            continue
        out.append(re.escape(c))
        i += 1

    if dir_only:
        out.append("/.*")
    out.append(r"\Z")
    return "".join(out)


# 何にも一致しない regex (不正な path 形 rule のフォールバック)。
_NEVER_MATCH = re.compile(r"(?!)")


def _compiled_path(pattern: str) -> "re.Pattern[str]":
    """path 形 rule の compiled regex を返す (キャッシュ付き)。

    translator は文字クラスの中身をそのまま regex に渡すため、``[z-a]`` のような
    逆順の範囲は ``re.error`` になる。例外を hook の外まで通すと、PreToolUse は
    全 tool で internal error deny、Stop は block を出さずに終了する — 壊れた
    local rule 1 行の影響としては大きすぎるので、その rule は **何にも一致しない**
    ものとして扱う (fnmatch が空の範囲を ``(?!)`` にするのと同じ。exclude なら
    保護が残る側、include なら元々効いていない側に倒れる)。
    """
    hit = _PATH_PATTERN_CACHE.get(pattern)
    if hit is None:
        try:
            hit = re.compile(_translate_path_pattern(pattern))
        except re.error:
            hit = _NEVER_MATCH
        _PATH_PATTERN_CACHE[pattern] = hit
    return hit


def _path_match_cached(rel: str, pattern: str) -> bool:
    """root 相対 path ``rel`` が path 形 rule ``pattern`` に一致するか。"""
    return _compiled_path(pattern).match(rel) is not None


def root_relative(path: str | PurePath, root: str | PurePath | None) -> str | None:
    """``path`` の ``root`` からの相対 path (POSIX 区切り) を返す。lexical のみ
    (symlink 解決も実在確認もしない — hook は stat を増やさない方針)。

    - ``root`` が None / 空 → None (path 形 rule は評価しない)
    - 絶対 path: ``root`` 配下なら相対化。root 自身・配下でない・``/rx`` の
      ような文字列 prefix だけの一致は None
    - 相対 path: **既に root 相対**と見なして正規化だけ行う (Stop hook が
      ``git ls-files`` の cwd 相対 path を root 相対に組み立てて渡す経路)。
      ``..`` で root の外へ出るものは None
    """
    if not root:
        return None
    p = PurePath(os.path.normpath(str(path)))
    if p.is_absolute():
        try:
            rel = p.relative_to(PurePath(os.path.normpath(str(root))))
        except ValueError:
            return None
    else:
        rel = p
    s = rel.as_posix()
    if s in ("", ".", "..") or s.startswith("../"):
        return None
    return s


def _is_case_sensitive() -> bool:
    """環境変数 ``SFG_CASE_SENSITIVE=1`` で case-sensitive にフォールバック可能。

    既定は case-insensitive。旧挙動 (0.1.x 系互換) に戻したいときだけ
    ``SFG_CASE_SENSITIVE=1`` を設定する。
    """
    return os.environ.get("SFG_CASE_SENSITIVE") == "1"


def _verdict(name: str, rel: str | None, rules: list[tuple[str, bool]]) -> str:
    """最後にマッチしたルールの符号を返す (basename 形 + path 形の 1 パス評価)。

    basename 形 rule は ``name`` と、path 形 rule は ``rel`` (root 相対 path) と
    比較する。``rel`` が None なら path 形 rule は一度も一致しない。

    ``SFG_CASE_SENSITIVE=1`` 未設定時は lower 比較で case-insensitive に評価する。
    旧 `fnmatch.fnmatch` (OS 依存) ではなく `fnmatchcase` を使い、lower 化だけで
    挙動を正規化する (OS の大文字小文字扱いに依存しない)。

    Returns:
        "include" / "exclude" / "nomatch"
    """
    cs = _is_case_sensitive()
    target = name if cs else name.lower()
    rel_target = None if rel is None else (rel if cs else rel.lower())
    last: str | None = None
    for pattern, is_exclude in rules:
        pat = pattern if cs else pattern.lower()
        if is_path_rule(pattern):
            if rel_target is None or not _path_match_cached(rel_target, pat):
                continue
        elif not _fnmatch_cached(target, pat):
            continue
        last = "exclude" if is_exclude else "include"
    return last or "nomatch"


def _last_match_verdict(name: str, rules: list[tuple[str, bool]]) -> str:
    """単一の名前 (basename / 親 dir 名) を basename 形 rule だけで評価する。

    path 形 rule は名前 1 要素とは比較しない (``rel=None`` で skip)。
    """
    return _verdict(name, None, rules)


def is_sensitive(
    path: str | PurePath,
    rules: list[tuple[str, bool]],
    *,
    parts: bool = True,
    root: str | PurePath | None = None,
) -> bool:
    """path が機密パターンに該当するか判定。

    1. basename (basename 形 rule) と root 相対 path (path 形 rule) を 1 本の
       リストとして出現順に last-match-wins で評価。
       - include 決着 → True
       - exclude 決着 → False (明示除外を優先)
       - nomatch → parts へ fall through (``parts=False`` なら False)
    2. 親 dir 名を順に basename 形 rule で評価し、どれか 1 つでも include 決着
       なら True (path 形 rule は親 dir 名 1 要素とは比較しない)。
    3. どこにもマッチしなければ False。

    Args:
        parts: 親 dir 名 (``pathlib.parts``) も評価するか。実在ファイルの実パス
            (Read / Edit / Stop) は既定の True。Bash operand のように path とは
            限らない文字列を判定する側は False を渡し basename だけで決める
            (0.22.0)。既定 patterns.txt はすべて basename 形なので、本物の機密
            パスは basename で include 決着し、False にしても保護は落ちない。
            ``parts=False`` でも path 形 rule は評価する (Bash でレシピが効かな
            ければ 1 ファイルに絞る意味が無いため)。
        root: path 形 rule の基準となる project root (0.24.0)。両 hook とも
            ``_shared.patterns.resolve_project_root(cwd)`` (= ``[project:]``
            セクションの key と同じ値) を渡す。None なら path 形 rule は評価
            しない (0.23.0 までと同じ挙動)。``path`` が相対なら root 相対と
            見なす (``root_relative`` 参照)。
    """
    if not rules:
        return False

    p = PurePath(path)
    rel = root_relative(p, root)

    verdict = _verdict(p.name, rel, rules)
    if verdict == "include":
        return True
    if verdict == "exclude" or not parts:
        return False

    for part in p.parts[:-1]:
        if _last_match_verdict(part, rules) == "include":
            return True

    return False
