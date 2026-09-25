# Changelog

## 0.2.0

### Changed

- テストを並列に実行する。suite ごとに直列で走らせていたため、複数の plugin にまたがる PR では
  合計時間が制限時間 (既定 90 秒) を超えて「ゲートを完了できなかった」になっていた
  (実測: 5 plugin の PR で 90 秒超 → 57 秒)。並列数は CPU 数と 8 の小さい方。結果の記録は
  plugin の順で、どの suite が落ちたかの帰属は変わらない
- 並列実行中に 1 本が失敗 (起動できない・時間切れ) したら、残りの suite を kill してすぐに
  止める。残りを待つと hook 自体の timeout を超え、Claude Code が PR 作成をそのまま通してしまう

### Added

- 手動実行 `check` に `--strict-validate` を追加 (validate の warning を FAIL にする)。独自の
  ゲートスクリプトから共通の検査として呼ぶため

## 0.1.0

初版。marketplace の運用で手元のスクリプトとして使っていた PR 前の検査を、
plugin repo 一般で使える PreToolUse hook として切り出した。

- `gh pr create` / `gh pr ready` を検知し、version bump・CHANGELOG・テスト・
  `claude plugin validate`・base との競合などを検査する。FAIL があれば PR 作成を止める
- ゲートを完了できない場合 (制限時間切れ・設定の破損・想定外のエラー) も止める。
  PreToolUse hook の時間切れはコマンドをそのまま通す仕様のため、ゲート内部に
  hook の timeout より短い制限時間を持たせた
- 元のスクリプトから一般化した点:
  - 検査対象の plugin を差分から自動で見つける (元は引数で 1 つ指定)。1 PR = 1 plugin の
    強制は `single_plugin_per_pr` で有効化する opt-in にした
  - base は `origin` の default branch か `--base` の値 (元は `origin/main` 固定)
  - テストコマンドを `test_command` で差し替え可能にした (元は Python unittest 固定)
  - `claude` / PyYAML / 新しい git が無い環境では該当検査を SKIP する
  - 別 worktree の shared checkout を見る検査は、作業ツリーの未 commit 変更を見る検査に
    置き換えた (worktree を使わない repo でも意味を持つ形にするため)。検査対象の plugin
    内は FAIL、それ以外は WARN
- hook の標準入出力は UTF-8 に固定した。Windows の既定の文字コードでは日本語の PR タイトルや
  判定結果で例外になり、JSON を返せないまま PR が素通りするため
- `cd "$DIR" && gh pr create` のように検査対象の repo が静的に決まらない場合も止める
- version は semver として比較し、下げを FAIL にする (semver でなければ WARN)
- `gh pr create --head <branch>` が現在の checkout と違う場合、`gh pr ready` で PR の参照に
  失敗した場合も止める。`gh -R <repo> pr create` のように subcommand の前に置いた
  `--repo` も検出する
- `--head owner:branch` (fork) と、`origin` と別の repo を指す `--repo` も止める
- base の決め方を `gh pr create` に合わせた (`--base` → `branch.<name>.gh-merge-base` →
  default branch)
- 1 つのコマンド内の PR 操作をすべて検査する (`gh pr create --draft && gh pr ready` 対策)
- `gh pr new` (create の別名) と、`if ...; then gh pr create; fi` のような制御構文の中も検出する
- `gh pr ready` は PR の branch と head commit が手元と一致しなければ止める
- 検査対象の plugin に未 commit の変更があれば止める (作業ツリーの修正で commit 済みの
  失敗が隠れるため)
- 削除した plugin の entry が marketplace.json に残っていれば止める
- 設定ファイルは commit 済みのものだけを読む (未 commit の書き換えで検査を弱められないように)
- `--repo` の比較に host を含める
- `(true); gh pr create` のように記号が続く区切りも分割する
- 実行位置にある PR 操作の数と構文解析で認識できた数が合わない場合 (コマンド置換・関数・
  `pushd` など) は、検査対象を特定できないとして止める。書き方ごとに個別対応すると取りこぼす
  ため、検出できない形はまとめて fail closed にした
- `GH_REPO` (コマンド前置・環境変数) と `gh repo set-default` の既定が origin と別の repo なら止める
- git repo の判定自体が失敗・時間切れした場合も止める (「repo の外」と区別する)
- `bash -c` / `sh -c` / `eval` の中の PR 操作も検出できないものとして止める
- `gh pr -R <repo> create` (pr と action の間の --repo) を検出する
- `--head` 指定時は origin/<head> が手元の HEAD と一致しなければ止める (gh は push を省くため)
- `gh pr ready <url>` の URL が origin と別の repo なら止める
- README / SECURITY.md に脅威モデルを明記した: 目的はうっかりの予防で、意図的なすり抜けへの
  対策ではない
- テストを `PYTHONDONTWRITEBYTECODE=1` で実行し、`__pycache__` / `*.pyc` は未 commit の変更として
  数えない (`.gitignore` に無い repo で、ゲート自身の実行結果を「変更あり」と誤判定していた)
- `gh pr create --draft` と `VERIFY_PLUGIN_RELEASE_MODE=warn` では止めずに結果だけ伝える
- `python3 hooks/verify-plugin-release check` で同じ検査を手動実行できる
