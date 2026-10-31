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

例外が要る skill は ALLOWLIST に (SKILL.md の相対パス, 付与の文字列) を足す。
許可リストは「その skill のその付与」だけに効く。他の skill や、同じ skill の別の
ワイルドカード付与は通さない。

## 使い方

    python3 scripts/check_skill_allowed_tools.py [--root <repo root>]

exit 0 = 違反なし、exit 1 = 違反あり (または検査対象の skill が 0 件)。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

# 例外 (SKILL.md の root からの相対パス, 付与の文字列)。
# 現状はすべて「Bash 単独」で、この検査の導入前から付いていたもの。完全一致の形へ
# 絞る改修は各 plugin 側の課題で、絞り終えたらここから外す (外し忘れは下の
# 「実体の無い許可リスト」検査が拾う)。
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("plugins/llms-docs/skills/researching-ai-sdk/SKILL.md", "Bash"),
        ("plugins/llms-docs/skills/researching-claude-docs/SKILL.md", "Bash"),
        ("plugins/llms-docs/skills/researching-firebase/SKILL.md", "Bash"),
        ("plugins/llms-docs/skills/researching-library-docs/SKILL.md", "Bash"),
        ("plugins/codex-pr-review/skills/iterating-pr-codex-reviews/SKILL.md", "Bash"),
    }
)


def _frontmatter_lines(text: str) -> list[str] | None:
    """先頭の `---` で囲まれた frontmatter の行を返す。無ければ None。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return None


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


def parse_allowed_tools(frontmatter: list[str]) -> list[str]:
    """frontmatter から `allowed-tools` の各付与を取り出す。

    YAML の block list (`- X`)、flow list (`[X, Y]`)、空白区切りの 1 行 scalar を扱う。
    キーが無ければ空 list。
    """
    entries: list[str] = []
    for idx, line in enumerate(frontmatter):
        if not line.startswith("allowed-tools:"):
            continue
        rest = line[len("allowed-tools:"):].strip()
        if rest.startswith("[") and rest.endswith("]"):
            entries.extend(_unquote(t) for t in _split_tokens(rest[1:-1]))
        elif rest and rest not in ("|", ">", "|-", ">-"):
            entries.extend(_unquote(t) for t in _split_tokens(rest))
        # 続く行: 字下げされた list 項目か scalar の継続行
        for follow in frontmatter[idx + 1:]:
            if follow.strip() and not follow[0].isspace() and not follow.startswith("- "):
                break
            stripped = follow.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                entries.append(_unquote(stripped[2:]))
            elif stripped == "-":
                continue
            else:
                entries.extend(_unquote(t) for t in _split_tokens(stripped))
        break
    return entries


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
        default=str(pathlib.Path(__file__).resolve().parent.parent),
        help="検証対象の repo root (既定: このスクリプトの 1 つ上)",
    )
    args = parser.parse_args()
    root = pathlib.Path(args.root).resolve()

    skills = sorted((root / "plugins").glob("*/skills/*/SKILL.md"))
    if not skills:
        # 検査対象が 0 件なら構造的に常に通る空チェックになるので fail にする
        print("ERROR: plugins/*/skills/*/SKILL.md が 1 件も見つかりません", file=sys.stderr)
        return 1

    failures: list[str] = []
    used_allow: set[tuple[str, str]] = set()
    for path in skills:
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            failures.append(f"{rel}: 読み取れません ({exc})")
            continue
        fm = _frontmatter_lines(text)
        if fm is None:
            continue
        for entry in parse_allowed_tools(fm):
            reason = violation(entry)
            if reason is None:
                continue
            if (rel, entry) in ALLOWLIST:
                used_allow.add((rel, entry))
                continue
            failures.append(f"{rel}: `{entry}` — {reason}")

    # 実体の無い許可リストは残さない (ワイルドカードを直したのに例外だけ残ると、
    # 後で同じ付与が戻っても黙って通ってしまう)
    # (検査した SKILL.md に限る。--root で別のツリーを指したときの誤検出を避ける)
    scanned = {p.relative_to(root).as_posix() for p in skills}
    for rel, entry in sorted(ALLOWLIST - used_allow):
        if rel not in scanned:
            continue
        failures.append(
            f"{rel}: ALLOWLIST の `{entry}` に対応する付与が無い (許可リストから外す)"
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
