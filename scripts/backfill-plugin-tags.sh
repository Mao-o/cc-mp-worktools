#!/usr/bin/env bash
# 各 plugin の plugin.json の version に対応するリリース tag
# (`<plugin-name>/v<version>`) が無ければ annotated tag を作成し、origin へ push する。
#
#   scripts/backfill-plugin-tags.sh            # dry-run (既定。何も変更しない)
#   scripts/backfill-plugin-tags.sh --apply    # annotated tag を作成して origin へ push
#
# 2 つの用途で同じロジックを共有します (列挙・判定を二重管理しないため):
#
#   1. tag 規約を導入した時点での遡及付与 — 現在の checkout (= main の HEAD) に
#      対して各 plugin の現 version の tag をまとめて作る。これがこのファイル名
#      の由来です。
#   2. 以降の通常運用 — .github/workflows/validate.yml の tag-plugin-releases
#      job が main への push ごとに `--apply` で呼び、その回に bump された
#      plugin の tag だけが新規に作られる (既存の tag はスキップされる)。
#
# tag は常に **現在の HEAD** を指します。過去のリリース commit を指す tag を
# 打ち直すことはしません (どの commit で bump されたかは CHANGELOG と
# `git log -- plugins/<name>/.claude-plugin/plugin.json` で追えるため、履歴の
# 再構成より「現行版がどの commit に入っているか」を正確にすることを優先する)。
#
# 既存 tag は絶対に動かしません (force しない)。`--apply` は「現 version に
# 対応する tag 一式」をまとめて push しますが、既に同じ commit を指している
# tag の push は no-op です。ローカルと origin で指す先が食い違っている場合は
# push が reject され、CI が赤くなります (黙って打ち替えるより、tag が動いて
# いる事実を出す方を選んでいます)。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

apply=0
for arg in "$@"; do
  case "$arg" in
    --apply) apply=1 ;;
    -h | --help)
      sed -n '2,7p' "$0"
      exit 0
      ;;
    *)
      echo "ERROR: 不明な引数: $arg (使えるのは --apply / --help)" >&2
      exit 2
      ;;
  esac
done

# annotated tag は committer identity を要求するため、作成前に確認する。
# GitHub Actions の既定では未設定なので、workflow 側で git config してから
# 呼ぶ必要がある (未設定のまま git tag -a すると分かりにくいエラーになる)。
if [ "$apply" -eq 1 ]; then
  if ! git config user.email >/dev/null 2>&1 || ! git config user.name >/dev/null 2>&1; then
    echo "ERROR: annotated tag の作成には git の user.name / user.email が必要です" >&2
    echo "       (CI では workflow 側で git config user.name / user.email を設定してください)" >&2
    exit 1
  fi
fi

head_sha="$(git rev-parse HEAD)"
echo "対象 commit: $head_sha"
if [ "$apply" -eq 1 ]; then
  echo "モード: apply (tag を作成して origin へ push する)"
else
  echo "モード: dry-run (何も変更しない)"
fi
echo ""

# current_tags: 現 version に対応する tag の全件 (既存 + 新規)。--apply では
# この一式を push する。既存分も含めるのは、ローカルにだけ作られて origin へ
# 届いていない tag が永久に取り残されるのを防ぐため (push は冪等)。
current_tags=()
new_tags=()
new_names=()
new_versions=()
found=0

while IFS= read -r manifest; do
  found=$((found + 1))
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("name") or "")' "$manifest")"
  version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version") or "")' "$manifest")"

  if [ -z "$name" ] || [ -z "$version" ]; then
    echo "ERROR: $manifest に name / version が揃っていません (name='$name' version='$version')" >&2
    exit 1
  fi

  # tag 名に使う前に形を確認する。plugin 名は kebab-case、version は semver の
  # 前提で、想定外の文字が入った状態で ref を作らないようにするためのガード。
  if ! printf '%s' "$name" | grep -Eq '^[a-z0-9][a-z0-9-]*$'; then
    echo "ERROR: plugin 名が kebab-case ではありません: '$name' ($manifest)" >&2
    exit 1
  fi
  if ! printf '%s' "$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?$'; then
    echo "ERROR: version が semver ではありません: '$version' ($manifest)" >&2
    exit 1
  fi

  tag="$name/v$version"
  current_tags+=("$tag")

  if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    existing="$(git rev-list -n 1 "refs/tags/$tag")"
    if [ "$existing" = "$head_sha" ]; then
      echo "  keep   $tag (既存 / この commit を指している)"
    else
      echo "  keep   $tag (既存 / ${existing:0:7} を指している — 打ち替えはしない)"
    fi
    continue
  fi

  echo "  create $tag"
  new_tags+=("$tag")
  new_names+=("$name")
  new_versions+=("$version")
done < <(find plugins -mindepth 3 -maxdepth 3 -path '*/.claude-plugin/plugin.json' | sort)

echo ""
if [ "$found" -eq 0 ]; then
  echo "ERROR: plugins/*/.claude-plugin/plugin.json が見つかりません" >&2
  exit 1
fi
echo "plugin manifest: $found 件 / 新規に作る tag: ${#new_tags[@]} 件"

if [ "$apply" -eq 0 ]; then
  echo ""
  echo "dry-run のため何も変更していません (--apply で作成 + push)。"
  exit 0
fi

i=0
while [ "$i" -lt "${#new_tags[@]}" ]; do
  git tag -a "${new_tags[$i]}" \
    -m "${new_names[$i]} v${new_versions[$i]}" \
    -m "plugins/${new_names[$i]}/.claude-plugin/plugin.json の version に対応するリリース tag。"
  echo "created: ${new_tags[$i]}"
  i=$((i + 1))
done

echo ""
# 既存分も含めて push する (同一 commit を指す tag の push は no-op)。
git push origin "${current_tags[@]}"
echo "pushed: ${#current_tags[@]} 件 (新規 ${#new_tags[@]} 件)"
