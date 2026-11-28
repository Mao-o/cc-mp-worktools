#!/usr/bin/env bash
# 全 plugin の unit test suite を 1 コマンドで実行する。
#
# .github/workflows/validate.yml の "Run plugin unit tests" ステップは
# このスクリプトを呼ぶだけにしてあるため、列挙ロジックは二重管理にならない
# (ロジックを変えるときはこのファイル 1 箇所を直せば CI にも反映される)。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 実行する Python interpreter を環境変数で切替可能にする (既定: python3)。
# 例: PYTHON=python3.12 scripts/test-all.sh
PYTHON="${PYTHON:-python3}"

# 時間を測るテスト (入力を大きくして処理が線形か・制限時間内に終わるかを確かめるもの) は
# 1 件で数秒の CPU を使うので、既定の suite では走らせず、RUN_TIMING_TESTS=1 のときだけ
# 走らせる。このスクリプトと CI は公開の前の全体検査なので、既定で立てる。
# 外から RUN_TIMING_TESTS=0 を渡したときは上書きしない (手元で飛ばしたいとき用)。
export RUN_TIMING_TESTS="${RUN_TIMING_TESTS:-1}"
if [ "$RUN_TIMING_TESTS" = "1" ]; then
  echo "時間を測るテストも走らせる (RUN_TIMING_TESTS=1)"
else
  echo "時間を測るテストは走らせない (RUN_TIMING_TESTS=$RUN_TIMING_TESTS)"
fi

count=0
while IFS= read -r tdir; do
  count=$((count + 1))
done < <(find plugins -type d -name tests | sort)
echo "検出したテストディレクトリ件数: $count"
if [ "$count" -eq 0 ]; then
  echo "ERROR: テストディレクトリが見つかりません (plugins/*/hooks/*/tests)" >&2
  exit 1
fi

fail=0
while IFS= read -r tdir; do
  pkg_root=$(dirname "$tdir")
  echo ""
  echo "== Running unit tests in $pkg_root =="
  if ! ( cd "$pkg_root" && "$PYTHON" -m unittest discover tests ); then
    echo "FAILED: $pkg_root" >&2
    fail=1
  fi
done < <(find plugins -type d -name tests | sort)

if [ "$fail" -ne 0 ]; then
  echo "::error::1 つ以上の plugin unit test スイートが失敗しました" >&2
  exit 1
fi
echo "全 unit test スイートが green でした"
