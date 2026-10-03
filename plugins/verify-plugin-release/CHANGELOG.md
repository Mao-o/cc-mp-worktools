# Changelog

## 0.2.1

テスト整理 (挙動の変更なし)。hook・`hooks.json`・README の挙動は変わらない (patch bump)。

### Changed

- テストが作る git repo と、ゲートが起動する git で、自動 gc / maintenance を止めた。CI (git 2.55) では
  `git commit` / `fetch` の終わりに起動される自動 maintenance (`git maintenance run --auto --detach`) の
  既定の戦略が geometric で、小さな repo でも loose object が数件あるだけで repack が走りうる。
  `--detach` は repack の自動条件を判定する前に背景へ切り離すので、commit が戻った後も背景で
  `.git/objects/pack` に書き続け、その最中に tempdir の後始末が走ると、tearDown が
  `OSError: [Errno 39] Directory not empty` で落ちる。object の hash 次第で偶発的に起きる。git 2.50 は
  戦略が gc でしきい値も高く、同じ条件でも起きないので、gc 戦略が既定の版 (2.50 など) で流すだけでは
  気付けない。この suite ではまだ落ちていないが、同じ作り (commit する repo を作り、tempdir ごと捨てる)
  の external-ai-assist の suite では CI で実際に落ちた。CI は全 suite を 1 つの job で流すので、どれかが
  落ちると全 plugin の CI が赤くなる。起きる前に同じ止め方を入れた
  - 設定は 2 本で渡す (`tests/_testutil.py` の `HERMETIC_GIT_ENV`)。`GIT_CONFIG_COUNT` (git 2.31 以上) で
    `maintenance.auto=false` / `maintenance.autoDetach=false` / `gc.auto=0` / `gc.autoDetach=false`
    (repo 自身の config より優先される)。`GIT_CONFIG_GLOBAL` が指す `tests/hermetic.gitconfig` に
    同じ 4 設定と `receive.autogc=false`。後者は開発者の `~/.gitconfig` の代わりに読ませる global config で、
    `GIT_CONFIG_NOSYSTEM=1` で system の config も読ませない
  - fixture が要るのは `git push` の受け側。ローカルの path へ送るとき、git は `receive-pack` を repo 用の
    env (`GIT_CONFIG_COUNT` など) を外して起動するので、env の設定は届かない (実測: env だけだと、
    受け側の repo に設定が無ければ `receive-pack` が自動 maintenance を起動する)。外されない
    `GIT_CONFIG_GLOBAL` の fixture は届くので、`git init --bare` を直接呼んだ bare repo でも止まる。
    push 先の bare repo を作る `init_bare_origin` は、同じ設定を repo 自身の config にも書く二重の備え
  - repo を作る `sh()` は毎回この env を足す。ゲートを subprocess で起動する `run_hook` / 手動実行
    `check` / UTF-8 の入力を渡すテストも env に足し、in-process で動かす `GateTest` /
    `ReadyRefsTest` は基底クラス `HermeticGitTestCase` が `os.environ` に張る。`run_hook` が従来
    していた「global の config を空 file にする」は、この fixture に置き換えた (global の config を
    開発者の `~/.gitconfig` から切り離す点は変わらない)
- 床を足した (`tests/test_hermetic_env.py` と `test_main.HookLaunchEnvTest`)。`GIT_TRACE2_EVENT` で
  git の子プロセスの起動を数え、maintenance / gc の起動が 0 件であることを見る。前提として、trace に
  `commit` / `receive-pack` / `fetch` が載っていること (空の床にしない) と、ゲートの `fetch` が成功した
  こと (`Report.notes` が空。失敗した `fetch` は終わりの maintenance の起動まで進まないので、前提が無いと
  何も止めていなくても通る) を確かめる。止める経路の env と fixture は 1 本ずつ単独で見て、fixture は
  `git config --global --list` の完全一致で固定する (誤って `git config --global` で書かれても、次の
  実行で落ちる)。`run_hook` が hook に渡す env は `subprocess.run` を包んで見る。期待値は `_testutil`
  の定義とは別のリテラル (同じ定数から導くと、1 項目消えても期待値ごと消えて通るため)
  - 「patch していない」状態は `GIT_CONFIG_*` を外すだけでなく、HOME / XDG_CONFIG_HOME を空にして作る
    (開発者の `~/.gitconfig` に `maintenance.auto=false` があると、helper を迂回した git も起動せず、
    床が黙って通る)。system の config は、`GIT_CONFIG_SYSTEM` で目印の file (`hermetic.system = read`
    だけを持つ) に向ける。**床の側では `GIT_CONFIG_NOSYSTEM` を立てない**: 立てると、helper や基底
    クラスがそれを渡し損ねても、床が埋めて通ってしまう。`GIT_CONFIG_NOSYSTEM` が効いていれば目印は
    読まれないので、効いているかを値 (`hermetic.system` が未設定) で見られる
  - ゲートが起動する git の床は、4 設定の `--get` (`GIT_CONFIG_COUNT` だけで満たせる) に加えて、
    `git config --global --list` が fixture の 5 設定と完全一致すること (`GIT_CONFIG_GLOBAL` が届いて
    いる) と、`hermetic.system` を読まないこと (`GIT_CONFIG_NOSYSTEM` が届いている) を見る。外側の env
    (開発者の shell など) に `GIT_CONFIG_NOSYSTEM` があると、基底クラスが当て損ねても system は読まれず、
    最後の確認が素通りする。そのためこの床の `setUp` は、外側の `GIT_CONFIG_*` を先に外してから基底
    クラスの patch を張る
  - Windows の CI でも走る suite なので、global の config を外す側は `os.devnull` ではなく実在する空
    file を使う
- 確認: suite 全体を `GIT_TRACE2_EVENT` 付きで、修正の前後に 1 回ずつ流した (HOME は空)。修正前 (73 件) は
  git プロセス 1358 個のうち `maintenance run --auto --quiet --detach` の起動が 87 回 (commit 86 回と
  push の受け側 1 回) で、`maintenance.auto=false` を見ていたプロセスは 0 個。修正後 (82 件) は
  maintenance / gc の起動が 0 回で、git プロセス 1326 個のすべてが `maintenance.auto=false` を見ていた
  (環境を作り直す床テスト自身が別の trace に書く git を除く)。床の各テストは、対応する実装を壊した
  scratch コピーで、`errors` ではなく assertion の失敗 (`failures`) になることを確かめた:
  env を渡さない (`sh()` が足さない / `GIT_CONFIG_COUNT` を渡さない) / `GIT_CONFIG_COUNT` だけ・
  fixture だけ外す / `GIT_CONFIG_NOSYSTEM` だけ外す (定数から消す / `sh()` が外す / 基底クラスが外す) /
  設定を 1 つ外す・件数を 1 つ少なく数える / fixture の設定の不足・余計・重複 / `init_bare_origin` が
  設定を書かない / `run_hook` が足さない / 基底クラスが `GIT_CONFIG_COUNT` だけ張る・
  `GIT_CONFIG_GLOBAL` を外す・何も張らない / ゲートの `fetch` が失敗する (origin の URL を壊す・
  存在しない branch)。落ちるテストの集合は、HOME が空の場合と、5 設定すべてが `~/.gitconfig` にある
  場合のどちらでも同じ。mutation は外側の env に `GIT_CONFIG_NOSYSTEM` を置かずに流した (置くと、内側で
  同じ変数を外す変異が黙って通る)

## 0.2.0

### Changed

- テストを並列に実行する。suite ごとに直列で走らせていたため、複数の plugin にまたがる PR では
  合計時間が制限時間 (既定 90 秒) を超えて「ゲートを完了できなかった」になっていた
  (実測: 5 plugin の PR で 90 秒超 → 57 秒)。並列数は CPU 数と 8 の小さい方。結果の記録は
  plugin の順で、どの suite が落ちたかの帰属は変わらない
- 並列実行中に 1 本が失敗 (起動できない・時間切れ) したら、残りの suite を kill してすぐに
  止める。残りを待つと hook 自体の timeout を超え、Claude Code が PR 作成をそのまま通してしまう
- suite は独立した process group (Windows は新しい process group) で起動し、止めるときは
  子孫ごと止める (`make test` の下の python など)。直下だけ止めると孫が残り、checkout を
  触り続けて次の実行と干渉する

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
