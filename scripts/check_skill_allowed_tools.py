#!/usr/bin/env python3
"""skill の `allowed-tools` に、Bash のワイルドカード付与が入っていないか検証する。

## なぜ検査するのか

`allowed-tools` は「確認なしで使える」付与で、制限ではない。
`Bash(python3 ".../builder.py" *)` のように `*` を含めると、読み取り専用のつもりの
付与が書き込み系の option (`--commit` など) まで確認なしで通す。argparse は省略形
(`--com` → `--commit`) も受け付けるので、`*` で特定の option だけを除く書き方も
抜ける。`Bash` 単独 (括弧なし) は、すべての Bash コマンドを確認なしにする。

## 規則

`plugins/*/skills/*/SKILL.md` の frontmatter にある `allowed-tools` のうち、Bash の
付与は**完全一致の形だけ**を許す。次を FAIL にする:

- `Bash(... *)` のように、括弧の中に `*` を含む形 (`Bash(git:*)` の旧記法も同じ)
- `Bash` 単独 (括弧なし)

例外が要る skill は ALLOWLIST に (SKILL.md の相対パスか `/` 終わりのディレクトリ,
付与の文字列) を足す。許可リストは「その範囲のその付与」だけに効く。他の skill や、
同じ skill の別のワイルドカード付与は通さない。

## 読めない形は FAIL にする (fail closed)

frontmatter は YAML の部分集合として自前で読む (CI に YAML ライブラリを足さない
ため)。読み方が YAML とずれると、違反が黙って通る。そこで、次の形は違反とは別に
FAIL にする:

- 付与の形 (`Tool` か `Tool(...)`) に合わない項目 (読み違えの兆候)
- 括弧の中に ` #` を含む項目 (YAML ではそこからコメントになる)
- `allowed-tools` のキーが 2 回以上ある (YAML では後勝ちで、どちらを読むかで結果が変わる)
- トップレベルにあるのにキーとして読めない `allowed-tools` の行
- 閉じていない flow list
- キーの値の生テキストに `Bash` があるのに、Bash の付与を 1 つも読み取れなかった

## 使い方

    python3 scripts/check_skill_allowed_tools.py [--root <repo root>]

exit 0 = 違反なし、exit 1 = 違反あり (または検査対象の skill が 0 件)。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

# 例外 (許可の範囲, 付与の文字列)。範囲が `/` で終わるものは、そのディレクトリ配下の
# SKILL.md すべてに効く (skill の追加・削除で書き換えが要らない)。それ以外は
# その SKILL.md の 1 件だけに効く。root からの相対パスで書く。
# 現状はすべて「Bash 単独」で、この検査の導入前から付いていたもの。
# - llms-docs の skill は、引数が質問ごとに変わるスクリプトを打つので完全一致に絞れない
#   (各 SKILL.md に理由がある)。skill 単位でなく skills 配下で許可する
# - codex-pr-review は 1 skill 単位の許可で、絞り終えたら外す
# 付与が 1 件も無い許可は、下の「実体の無い許可リスト」検査が FAIL にする。
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("plugins/llms-docs/skills/", "Bash"),
        ("plugins/codex-pr-review/skills/iterating-pr-codex-reviews/SKILL.md", "Bash"),
    }
)


def _allow_covers(scope: str, rel: str) -> bool:
    """許可の範囲が SKILL.md の相対パスに効くか (`/` 終わりは配下、他は完全一致)。"""
    if scope.endswith("/"):
        return rel.startswith(scope)
    return rel == scope


# `allowed-tools:` のキー行 (引用符付きのキー、コロンの前の空白も同じキーとして扱う)
_KEY_RE = re.compile(r"""^\ufeff?["']?allowed-tools["']?\s*:""")
# 付与 1 件の形。これに合わない項目は読み違えの兆候として FAIL にする
_ENTRY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*(\(.*\))?", re.DOTALL)
_BASH_WORD_RE = re.compile(r"\bBash\b")
# block list の項目 (`- X`。`-` の後は空白かタブ)
_ITEM_RE = re.compile(r"-(?:\s+|$)")


def _frontmatter_lines(text: str) -> list[str] | None:
    """先頭の `---` で囲まれた frontmatter の行を返す。無ければ None。"""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return None


def _strip_comment(line: str) -> str:
    """括弧と引用符の外で、行頭か空白の直後にある `#` から行末までを捨てる。"""
    depth = 0
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "#" and depth == 0 and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line


def _split_tokens(raw: str) -> list[str]:
    """空白かカンマで区切る。括弧と引用符の中では区切らない。"""
    tokens: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = ""
    for ch in raw:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'" and depth > 0:
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif depth == 0 and (ch.isspace() or ch == ","):
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return tokens


def _unquote(item: str) -> str:
    item = item.strip()
    if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
        return item[1:-1]
    return item


def _is_top_level(line: str) -> bool:
    """次のトップレベルキー (値の範囲の終わり) になりうる行か。"""
    return bool(line.strip()) and not line[0].isspace() and not line.startswith(("#", "-"))


def _raw_value(frontmatter: list[str], idx: int, rest: str) -> str:
    """キーの値の生テキスト (キーの後ろから次のトップレベルキーまで。コメントも含む)。"""
    raw = [rest]
    for follow in frontmatter[idx + 1:]:
        if _is_top_level(follow):
            break
        raw.append(follow)
    return "\n".join(raw)


def parse_allowed_tools(frontmatter: list[str]) -> tuple[list[str], list[str]]:
    """frontmatter から `allowed-tools` の各付与と、読めなかった理由を取り出す。

    YAML の block list (`- X`)、flow list (`[X, Y]`。複数行も)、空白区切りの scalar
    (引用符付きも) を扱う。キーが無ければ両方とも空 list。
    """
    problems: list[str] = []
    # キーの読み方そのものの保険: トップレベルに `allowed-tools` の語があるのに、キーとして
    # 読めない行は FAIL にする (読めないキーは値の保険も素通りする)
    for line in frontmatter:
        if (
            "allowed-tools" in line.split(":", 1)[0]
            and line[:1].strip()
            and not line.startswith("#")
            and not _KEY_RE.match(line)
        ):
            problems.append(f"`allowed-tools` のキーとして読めない行 `{line.strip()}`")
    key_lines = [i for i, line in enumerate(frontmatter) if _KEY_RE.match(line)]
    if not key_lines:
        return [], problems
    if len(key_lines) > 1:
        problems.append("`allowed-tools` のキーが 2 回以上ある (YAML では後勝ち)")
    idx = key_lines[-1]
    line = frontmatter[idx]
    rest = line[_KEY_RE.match(line).end():]
    raw_value = _raw_value(frontmatter, idx, rest)
    rest = _strip_comment(rest).strip()

    entries: list[str] = []
    follows = frontmatter[idx + 1:]
    if rest.startswith("["):
        # flow list: `]` が出るまで後続の行を連結する
        buf = rest
        consumed = 0
        while not buf.endswith("]") and consumed < len(follows):
            buf += " " + _strip_comment(follows[consumed]).strip()
            consumed += 1
        if buf.endswith("]"):
            entries.extend(_unquote(t) for t in _split_tokens(buf[1:-1]))
        else:
            problems.append("flow list `[` が閉じていない")
        follows = follows[consumed:]
    elif rest and rest not in ("|", ">", "|-", ">-"):
        entries.extend(_unquote(t) for t in _split_tokens(_unquote(rest)))
    # 続く行: 字下げされた list 項目か scalar の継続行
    for follow in follows:
        stripped = follow.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not follow[0].isspace() and not _ITEM_RE.match(follow):
            break
        stripped = _strip_comment(stripped).strip()
        item = _ITEM_RE.match(stripped)
        if item:
            value = stripped[item.end():]
            if value:
                entries.append(_unquote(value))
        else:
            entries.extend(_unquote(t) for t in _split_tokens(_unquote(stripped)))

    entries = [e.strip() for e in entries]
    for entry in entries:
        if not _ENTRY_RE.fullmatch(entry):
            problems.append(f"解釈できない付与 `{entry}`")
        elif re.search(r"\s#", entry):
            # 括弧の中の ` #` も YAML ではコメントの始まりで、読みがずれる
            problems.append(f"付与の中に ` #` がある (YAML ではコメントになる) `{entry}`")
    if _BASH_WORD_RE.search(raw_value) and not any(
        e == "Bash" or e.startswith("Bash(") for e in entries
    ):
        problems.append("解析できない Bash の付与 (値に `Bash` があるが読み取れない)")
    return entries, problems


def violation(entry: str) -> str | None:
    """違反ならその理由、問題なければ None。"""
    if entry == "Bash":
        return "Bash 単独の付与 (すべての Bash コマンドが確認なしになる)"
    if entry.startswith("Bash(") and entry.endswith(")") and "*" in entry:
        return "Bash の付与にワイルドカード `*` を含む (完全一致の形だけを許す)"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--root",
        default=None,
        help="検証対象の repo root (既定: このスクリプトの 1 つ上)",
    )
    args = parser.parse_args()
    explicit_root = args.root is not None
    root = pathlib.Path(
        args.root if explicit_root else pathlib.Path(__file__).resolve().parent.parent
    ).resolve()

    skills = sorted((root / "plugins").glob("*/skills/*/SKILL.md"))
    if not skills:
        # 検査対象が 0 件なら構造的に常に通る空チェックになるので fail にする
        print("ERROR: plugins/*/skills/*/SKILL.md が 1 件も見つかりません", file=sys.stderr)
        return 1

    failures: list[str] = []
    used_allow: set[tuple[str, str]] = set()
    for path in skills:
        rel = path.relative_to(root).as_posix()
        # symlink で範囲の外の実体を指す SKILL.md には、範囲の許可を効かせない
        real = path.resolve()
        real_root = root.resolve()
        real_rel = real.relative_to(real_root).as_posix() if real.is_relative_to(real_root) else ""
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f"{rel}: 読み取れません ({exc})")
            continue
        fm = _frontmatter_lines(text)
        if fm is None:
            continue
        entries, problems = parse_allowed_tools(fm)
        failures.extend(f"{rel}: {p}" for p in problems)
        for entry in entries:
            reason = violation(entry)
            if reason is None:
                continue
            covering = {
                a
                for a in ALLOWLIST
                if a[1] == entry and _allow_covers(a[0], rel) and _allow_covers(a[0], real_rel)
            }
            if covering:
                used_allow.update(covering)
                continue
            failures.append(f"{rel}: `{entry}` — {reason}")

    # 実体の無い許可リストは残さない (ワイルドカードを直したのに例外だけ残ると、
    # 後で同じ付与が戻っても黙って通ってしまう)。既定の root では、範囲に該当する付与が
    # 1 件も無ければ FAIL にする (1 件指定の許可は、その skill の rename・削除でも FAIL)。
    # --root で別のツリーを指したときだけ、範囲に該当する SKILL.md を検査した許可に
    # 限る (fixture のツリーでの誤検出を避ける)
    scanned = {p.relative_to(root).as_posix() for p in skills}
    for scope, entry in sorted(ALLOWLIST - used_allow):
        if explicit_root and not any(_allow_covers(scope, r) for r in scanned):
            continue
        failures.append(
            f"{scope}: ALLOWLIST の `{entry}` に対応する付与が無い (許可リストから外す)"
        )

    if failures:
        print("skill の allowed-tools の検査に失敗しました:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"OK: {len(skills)} 件の SKILL.md の allowed-tools に違反なし")
    return 0


if __name__ == "__main__":
    sys.exit(main())
