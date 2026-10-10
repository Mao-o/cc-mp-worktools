# Changelog

## 0.1.4

**テスト整理 (挙動の変更なし)。テストが起動する git の隔離を固めた (patch bump)。** repo を作る helper の
git を 1 つだけ迂回しても気付けなかった穴と、外側の env にある repo / config / template を指す変数が helper と
基底クラスの git に漏れる穴を塞いだ。hook・`hooks.json`・README の挙動は変わらない。

### テスト: helper が起動する git の全件に env が届くことを見る

0.1.3 の trace の床は、maintenance を起動する git (`commit`) しか見ない。`make_repo` の `worktree add` を
2 回とも env を渡さない起動にしても、床には差が出ず生き残った (迂回した git は開発者の `core.hooksPath` などを
読む)。起動した git の `(argv, 実際に渡った env)` を `subprocess.Popen` の spy で全部記録し、全件が
`HERMETIC_GIT_ENV` の全項目を持つことを `all` で見るテストを足した
(`test_every_git_launched_by_the_helpers_carries_the_env`)。前提として、起動が 8 件以上 (init / config x3 /
add / commit / worktree add x2) で、種類も記録できていることを確かめる。既存の `any` の検査は、commit の
迂回を trace で殺す役としてそのまま残した。

### テスト: 外側の repo / config / template を指す変数を helper と基底クラスで外す

`_testutil.OUTER_GIT_LEAK_ENV` (`GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_COMMON_DIR` /
`GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` / `GIT_NAMESPACE` / 旧来の `GIT_CONFIG` /
`GIT_CONFIG_PARAMETERS` / `GIT_TEMPLATE_DIR`) を足し、`hermetic_env()` (helper `sh()` が使う) と
`HermeticGitTestCase` が外すようにした。`HERMETIC_GIT_ENV` は外したあとに足すので、定数が勝つ。

実測 (0.1.3 の suite を scratch コピーで流した。HOME は空。git 2.50.1): 外側の env に別 repo を指す
`GIT_DIR` を置くと 45 件中 30 件が errors で落ち、別 repo に commit が 2 件書き込まれた (helper の `init` /
`config` / `commit` が漏れる)。除外ファイルと失敗する pre-commit を持つ `GIT_TEMPLATE_DIR` を置くと 31 件が
errors で落ちた。`GIT_CONFIG_PARAMETERS` (`maintenance.auto=true`) だけでは 0.1.3 の suite は green のまま
だったが、`GIT_CONFIG_PARAMETERS` は `GIT_CONFIG_COUNT` に勝つ (`COUNT` で false・`PARAMETERS` で true を
渡すと true が返る) ので、外さないと helper の自動 maintenance の抑止が外側の値に負けうる。旧来の `GIT_CONFIG` が
あると `git config --global --list` は exit 129 で落ちる。修正後は、3 つすべてを置いても 60 件 green で、
別 repo は変わらない。

変更したファイル:

- `tests/_testutil.py`: `OUTER_GIT_LEAK_ENV`・`hermetic_env()` の追加、`sh()` が毎回 `hermetic_env()` を使う、
  `HermeticGitTestCase` が外側の変数を外す
- `tests/test_hermetic_env.py`:
  - `isolate_git_config` が外側の repo / config / template を指す変数も外し、`home` に空の `.gitconfig` を置く
    (global の config が無いと `git config --global --list` が exit 128 になり、空の global を見られない)
  - 外側の変数の一覧は `_testutil` とは別にリテラルで持つ (`OUTER_REPO_ENV_NAMES`)。`_OuterRepoEnvChecks` が
    起動の仕方 (helper / 基底クラス) ごとに、床に置いた変数が git の env に届かないこと、helper が外側の repo を
    変えないこと (全 file の中身の前後比較・旧来の `GIT_CONFIG` が指す file が作られないこと)、外側の template の
    `hooks/pre-commit` が helper の commit で走らないこと、template の除外ファイルが helper の commit に効かない
    ことを見る。template が `git init` に写ることを前提として先に確かめる
  - 床が当てる側の値を持たないことの確認を、当てる側を呼ぶ直前にも置いた。`assert_the_floor_stops_nothing`
    を helper の呼び出しと push の直前で呼ぶ (床の setUp の後、当てる側を呼ぶまでに足した値は、setUp 直後の
    確認では見えない)。問い合わせの床 (`_HermeticConfigChecks`・`_OuterRepoEnvChecks`) は、当てた直後の env
    (`applied_env`) から問い合わせの入口までと、起動の中で env が変わっていないことを `query` /
    `launched_env` の前後で確かめる。継承先がこの 2 つを上書きしていないことも `setUp` で確かめる
    (上書き用の継承先メソッドは `query` から `launch` に改名)
  - 床の側の道具が値を足す退行: spy (`recorded_git_launches`) が渡された env をそのまま記録すること
    (`TestTheSpyRecordsTheEnvAsPassed`)、`isolate_git_config` の直後の env が止める側の値を持たず外側の変数が
    残っていないこと (`TestTheIsolatedEnvStopsNothing`)
  - 陽性対照の git が失敗したときは、例外のまま crash させず失敗として報告する

テスト件数: 45 → 60 (追加 15・削除 0・統合 0)。

確認:

- 変異 46 件を、外側の env に `GIT_DIR` (別 repo)・`GIT_CONFIG_PARAMETERS`・`GIT_TEMPLATE_DIR` を置き、HOME は
  空 / 自動 maintenance を止める 5 設定を持つ / 既定の除外ファイルを持つ の 3 つで流した。46 件すべてが
  assertion の失敗 (`failures=`) で落ち、`errors=` は 0 だった (生き残りなし。1 件ずつ decoy の repo を作り直した)。
  - helper の git の迂回 7 件: `worktree add` の 1 回目だけ・2 回目だけ・2 回とも (`test_every_git_launched_by_the_helpers_carries_the_env`
    が 3 つの HOME すべてで落ちる)、`init`・`config`・`add`・`commit` のそれぞれ
  - `sh()` が `hermetic_env()` を使わない・外側の変数を外さない、`hermetic_env()` が外さない・外側の env が定数に
    勝つ・定数を足さない、`OUTER_GIT_LEAK_ENV` から 10 個の名前をそれぞれ外す、基底クラスが外さない・定数を
    当てない
  - 床: `isolate_git_config` が外側の変数を外さない・`GIT_CONFIG_*` を外さない・空の `.gitconfig` を置かない・
    HOME を向け直さない。wrapper の区間で床に `GIT_CONFIG_NOSYSTEM` / 止める側の `GIT_CONFIG_COUNT` / fixture を指す
    `GIT_CONFIG_GLOBAL` を足す 5 件 (helper の呼び出しの前・全件 env の検査の前・push の前)
  - 床の側の道具: spy が定数の値を足す・env なしの起動で空を記録する・渡された env を捨てる、問い合わせの床の
    控えの取り方 (基底クラスの setUp の前後)、起動の中で env を足す、継承先が `query` / `launched_env` を上書きする、
    外側の変数を置く順序、外側の repo を作らない、`GIT_TEMPLATE_DIR` を置かない・template を戻さない
  - HOME ごとに落ちるテストの集合が変わるのは、床が HOME を向け直さない変異だけ (開発者の config を読む
    ほど、陽性対照が落ちる数が増える。3 つとも assertion で落ちる)

## 0.1.3

**テスト整理 (挙動の変更なし)。自動 gc / maintenance 対策の床が、テストの git に global / system の
config を読ませない設定の当て損ねを見逃していた穴を塞いだ (patch bump)。** hook・`hooks.json`・README
の挙動は変わらない。

### テスト: 床の側で `GIT_CONFIG_NOSYSTEM` を立てるのをやめ、env を当てる各点の当て損ねを見えるようにした

0.1.2 の床は、「patch していない」状態を作る側 (`empty_global_config`) が `GIT_CONFIG_NOSYSTEM=1` を
立てていた。床の立てた値は、repo を作る helper (`_testutil.sh`) が `os.environ` ごと引き継ぐので、helper が
`GIT_CONFIG_NOSYSTEM` だけを渡し損ねても床は通った。maintenance を止めることは固定できていたが、
「テストの git に system の config を読ませない」は helper の側では固定されていなかった。

実測 (git 2.50.1。`tests.test_hermetic_env` だけを scratch コピーで流した。HOME は空 / 自動 maintenance を
止める 5 設定を持つ / 既定の除外ファイルを持つ の 3 つ): env を当てる 3 点 (定数 `HERMETIC_GIT_ENV` /
helper / 基底クラス) で、`GIT_CONFIG_COUNT` だけを当てる・`GIT_CONFIG_NOSYSTEM` 抜き・`GIT_CONFIG_GLOBAL`
抜きの部分適用 9 件を流した。修正前に生き残ったのは helper の `GIT_CONFIG_NOSYSTEM` 抜きの 1 件だけで
(3 つの HOME すべて)、残り 8 件は assertion で落ちた (基底クラスの 3 件は 0.1.2 の hook の git の床が
殺していた)。参考に足した 5 件 (3 点それぞれの `GIT_CONFIG_COUNT` 抜きと、helper・基底クラスが env を
混ぜる向きを逆にして外側の env が定数に勝つ 2 件) のうち、基底クラスの `GIT_CONFIG_COUNT` 抜きと、向きを
逆にする 2 件の計 3 件も、3 つの HOME すべてで生き残った。

変更したファイル:

- `tests/test_hermetic_env.py`:
  - `isolate_git_config` が `GIT_CONFIG_NOSYSTEM` を立てないようにした (`empty_global_config` は統合)。
    `GIT_CONFIG_*` を外し、`HOME` / `XDG_CONFIG_HOME` を空の dir に、`GIT_CONFIG_SYSTEM` を目印の file
    (`hermetic.system = read`) に向ける。helper が `GIT_CONFIG_NOSYSTEM` を渡し損ねることは、
    `HERMETIC_GIT_ENV` の全項目が helper の git に届いている前提 (既存の床の spy) でも拾える
  - 起動の仕方 (定数だけ / helper / hook の git) ごとに同じ 3 本を流す (`_HermeticConfigChecks`):
    (1) repo 自身の config に反対の値を置き、git が見る値が止める側であること (`GIT_CONFIG_COUNT`)、
    (2) `git config --global --list` が fixture の 5 設定と完全一致すること、(3) system の目印が読まれない
    こと (exit 1・出力なし)。(3) は先に、`GIT_CONFIG_NOSYSTEM` を外した env で目印が読めることを確かめる
    (目印が読めない環境では「読まれない」が何も見ていない空の床になるため)
  - 外側の env に、止めない側の値 (`GIT_CONFIG_GLOBAL` = 空の file、`GIT_CONFIG_COUNT` で
    `maintenance.auto=true`) を置いてから流し、当てる側が勝つことを見る。外側の env に
    `GIT_CONFIG_NOSYSTEM` があっても、先に外すので当て損ねは隠れない
  - 床が当てる側の値を持たないことも見る (`test_the_floor_alone_stops_nothing`。マージ前レビューの指摘)。
    当てる側が当てる前の env (床の env) だけで起動した git で、3 本が見るもののどれにも当てる側の値が
    見えないこと (repo 自身の止めない側の値が見え、global の一覧が空で、system の目印が読める)。床が当てる側の値
    (`GIT_CONFIG_NOSYSTEM`、fixture を指す `GIT_CONFIG_GLOBAL`、止める側の `GIT_CONFIG_COUNT`) を持つ形に
    戻ると、当てる側の当て損ねを床が埋めて、上の 3 本が黙って通るため
  - hook の git (`family._git`) は、非ゼロ終了も起動の失敗も `None` にして握りつぶす。そのため
    `subprocess.run` の結果を捕まえて returncode を見る (起動できなかったときは前提の assertion で落ちる)
  - 陽性対照: 止める設定が無い commit では maintenance の起動が trace に見える (見えない git の版では
    「0 件」を見る床は何も見ていない)
  - 置き換えた床 (3 件): env だけの 4 項目、fixture の完全一致、hook の git の 3 つ。どれも上の 3 本が
    含む (殺していた変異は、下の「確認」で新しい床が殺すことを確かめた)。床は 6 件から 16 件 (追加 13・
    置き換え 3)
- `tests/_testutil.py`: コメントだけ。次の 3 点を訂正した (2 点目は 0.1.2 の項の同じ記述にも当てはまる)
  - `gc.auto=0` は起動された maintenance の gc を止める設定で、git 2.50 ではこれだけでは起動は止まらない
    (実測: `maintenance.auto=false` だけが起動を 0 にする。`gc.auto=0` だけでは起動も `--detach` も残る)。
    「そもそも自動 maintenance を起動しない」のは `maintenance.auto=false`
  - 「ローカルの実行だけでは気付けない」は、gc 戦略が既定の版 (git 2.50 など) で流すだけでは気付けない、
    が正しい。この flaky は同じ作りの別 plugin の suite で観測したもので、この suite ではまだ観測していない
  - テストが揺れる設定の例を、この suite に効くものにした: `core.hooksPath` (テストの commit / worktree add で
    開発者の hook が走る)、`worktree.useRelativePaths` (linked worktree の `.git` の gitdir 行が相対パスに
    なる。hook はこのファイルを直接読む)。既定の除外ファイルは `GIT_CONFIG_GLOBAL` では外れないが、この
    suite は未追跡の一覧・status・diff を読まず、`make_repo` が add するのも README だけなので、HOME を
    テストクラスごとには向けない理由を書いた

確認:

- 修正後は、上の部分適用 9 件と参考 5 件の計 14 件に、床が空にならないことの 2 件 (陽性対照が止める設定も
  渡す / 目印が何も設定しない)、置き換えた床が殺していた変異 4 件 (env の 1 項目を外す・`GIT_CONFIG_COUNT`
  を 1 つ少なく数える・fixture の 1 設定を外す・fixture に余計な設定を足す) を足した 20 件すべてが、
  assertion の失敗 (`failures=`) で落ちた (`errors=` は 0)。落ちるテストの集合は 3 つの HOME で同一。
  外側の env に `GIT_CONFIG_NOSYSTEM=1` を入れても、定数 / helper / 基底クラスの `GIT_CONFIG_NOSYSTEM`
  抜きの 3 件を検出する
- 床が当てる側の値を持つ形に戻る変異 3 件 (床が `GIT_CONFIG_NOSYSTEM` を立てる / それに加えて helper が
  `GIT_CONFIG_NOSYSTEM` を渡し損ねる / 床の外側の `GIT_CONFIG_GLOBAL` が fixture を指し、基底クラスが
  `GIT_CONFIG_GLOBAL` を渡し損ねる) は、`test_the_floor_alone_stops_nothing` を足す前は空の HOME で
  生き残った。足した後は 3 つの HOME すべてで、このテスト (3 クラス分) だけが assertion の失敗で落ちる
  (`errors=` は 0)
- suite 全体 (空の HOME): 45 件 OK (skip 1)。`claude plugin validate` の warning は 0

## 0.1.2

**テスト整理 (挙動の変更なし)。テストの tearDown が偶発的に `Directory not empty` で落ちうる CI の
flaky を、予防的に塞いだ (patch bump)。** hook・`hooks.json`・README の挙動は変わらない。この suite
での失敗はまだ観測していない。同じ作りのテストで、別の plugin (external-ai-assist) の suite が CI で
実際に落ちたため、同じ止め方に揃えた。

### テスト: テストが作る git repo で自動 gc / maintenance を止めた

`make_repo` の `git commit` は、終わりに `git maintenance run --auto --detach` を起動する (修正前の
suite は、commit 27 回に対して起動が 27 回だった。git 2.50 で `GIT_TRACE2_EVENT` により実測した)。
CI の git 2.55 は auto maintenance の既定の戦略が geometric で、小さな repo でも `.git/objects/17` に
loose object が 2 件あるだけで repack を始めうる。背景へ切り離された repack が `.git/objects/pack` に
書いている最中に tempdir の後始末が走ると、`OSError: [Errno 39] Directory not empty` で落ちる
(object の hash 次第の偶発的な失敗。git 2.50 は同じ条件でも起きないので、ローカルの実行だけでは
気付けない)。

テストが作る repo と、hook が起動する git の両方に、次の 2 本で止める設定を渡すようにした
(`HERMETIC_GIT_ENV`)。そもそも自動 maintenance を起動せず、何かが走っても背景へ切り離さない。

- env の `GIT_CONFIG_COUNT` (git 2.31 以上): `maintenance.auto=false` / `maintenance.autoDetach=false` /
  `gc.auto=0` / `gc.autoDetach=false`。repo 自身の config より優先される
- `GIT_CONFIG_GLOBAL` が指す `tests/hermetic.gitconfig`: 同じ 4 設定と `receive.autogc=false`。
  あわせて `GIT_CONFIG_NOSYSTEM=1` で system の config も読ませない。従来のテストは開発者の
  `~/.gitconfig` と system の config (Windows では Git for Windows の既定) を読んでいたので、
  テストの環境はここで hermetic になる (判定に効く設定は見当たらない)。`git push` の受け側
  (`receive-pack`) には env が届かず (git が repo 用の env を外して起動する)、この file だけが届く。
  この suite の hook とテスト本体は push しない (床だけが push する) が、同じ作りの他の suite と
  揃えてある

変更したファイル:

- `tests/_testutil.py`: 上の定数 (`NO_BACKGROUND_GIT_SETTINGS` / `HERMETIC_GIT_ENV`) を追加。repo を
  作る `sh()` は毎回この env を足すので、env を patch していないテストクラスが `make_repo` を呼んでも
  止まる。基底クラス `HermeticGitTestCase` (同じ env を `os.environ` に当てる) も足し、hook が起動する
  git (`family._git`) にも届くようにした。`test_guard.py` の `GuardTest` / `EntryTest` はこれを使う
- `tests/hermetic.gitconfig`: global の fixture (新規)
- `tests/test_hermetic_env.py`: 床 6 件 (新規)。期待値は `_testutil` とは別のリテラル
  (定数から導くと、1 項目消えても期待値ごと消えて通るため)
  - 挙動: `GIT_TRACE2_EVENT` で git の子プロセスの起動を数え、`make_repo` の間に maintenance / gc の
    起動が 0 件であること (commit が trace に載っていることを前提として確かめる)。fixture を外し、
    helper が渡す `GIT_CONFIG_COUNT` だけでも 0 件であること。設定値を問い合わせるだけの床は、
    `make_repo` の commit だけが env を持たずに起動されても、問い合わせの側が env を足し直すので気付けない。
    そのため起動された git の挙動を見る。「patch していない」状態は、`GIT_CONFIG_*` を外すだけでなく
    global / system の config も空にして作る (開発者の `~/.gitconfig` に `maintenance.auto=false` があると、
    迂回した git も起動せず、床が黙って通る)
  - 出どころ別: env の 4 設定は 1 項目ずつ `git config --get`、global の fixture は
    `git config --global --list` の完全一致 (5 設定だけを持つこと)。2 本は同じ値を持つので、有効値だけを
    見る床は、片方が欠けてももう片方が埋めて通る。env に設定が無い bare repo への `push` で、受け側の
    `receive-pack` (trace に載ることを前提として確かめる) が maintenance を起動しないことも見る
  - hook が起動する git (`family._git`): 4 設定が見えることに加え、global として fixture を読むこと
    (`--global --list` の完全一致) と、system の config を読まないこと (`GIT_CONFIG_SYSTEM` に目印の
    file を指しておく)。床自身は `GIT_CONFIG_NOSYSTEM` を立てず、基底クラスの当て損ねを埋めない
  - global を空にする上書きは `/dev/null` ではなく実体のある空 file にした (この suite は Windows の
    CI でも流れ、`nul` を git が config として読めるかに依存したくないため)

確認:

- suite 全体を `GIT_TRACE2_EVENT` 付きで 1 回ずつ流し、起動された `maintenance` / `gc` の数を数えた
  (空の HOME。床のテストは自分用の trace に差し替えるので、その区間の git はここに数えられない):
  修正前は 29 件・commit 27 回に対して 27 回、修正後は 35 件・commit 28 回に対して 0 回
  (実行時間は 6.8 秒から 7.5 秒)。`maintenance.auto=false` を既に持つ HOME では修正前も 0 回で、
  開発者の `~/.gitconfig` が問題を隠す。床はこの影響を受けない
- 床の各テストは、対応する実装を壊した scratch コピーで、`errors=` ではなく assertion の失敗
  (`failures=`) になることを確かめた: helper が env を足さない / `make_repo` の commit だけが env を持たずに
  起動される / `GIT_CONFIG_GLOBAL` を外す・`/dev/null` にする / `GIT_CONFIG_COUNT` の経路を外す / env の設定を
  1 つ外す・件数を 1 つ少なく数える / fixture の設定を 1 つ外す・余計な設定や重複を足す / 基底クラスが env を
  当てない・`GIT_CONFIG_COUNT` だけを当てる・`GIT_CONFIG_NOSYSTEM` だけ抜ける・`GIT_CONFIG_GLOBAL` だけ抜ける。
  結果が実行者の global config に左右されないことも、HOME が空の場合と、5 設定すべてを
  `~/.gitconfig` に持つ場合のどちらでも落ちる集合が同じになることで確かめた (HOME を空にする隔離を
  外した対照では、後者でだけ push の床が黙って通る)

## 0.1.1

動作の変更なし。Windows の CI (`tests-windows`) の対象に加えるにあたり、テストを直した。

- テストのコマンドでパスを引用する (引用なしの Windows パスは bash が `\` を消すため)
- `/` 区切り・大文字小文字・8.3 短縮名の別名を同じ checkout と判定するテストを追加

## 0.1.0

初版。複数の agent を linked worktree で並列に動かしたときに、agent が別の checkout
(main 側や他の worktree) を書き換える事故を止める PreToolUse hook。

- 作業ディレクトリが linked worktree のときだけ動く。main checkout や repo の外では何もしない
  (git を起動する前に `.git` ファイルの有無で早期に抜ける)
- Bash: 別の checkout を対象にした git の書き込み操作 (`checkout` / `commit` / `reset` など) と、
  別の worktree を対象にした `git worktree remove` / `move` を止める。対象は `cd` /
  `git -C` / `--work-tree` / `--git-dir` から決める
- Write / Edit / MultiEdit / NotebookEdit: 別の checkout 配下への書き込みを止める
- 対象の判定は shell の意味に沿わせる: `( ... )` の中の `cd` は外に持ち越さない、`cd -- <dir>` /
  `cd -P <dir>` のオプションは読み飛ばす、`-C` / `--git-dir` が指す repo 側 (HEAD / index) と
  `--work-tree` が指す作業ツリー側の両方を判定する、linked worktree の git dir
  (`.git/worktrees/<name>`) はその worktree の root に対応づける
- `git bisect` を書き込み操作に含める。`env -i` / `env -u NAME` / `sudo -u USER` など wrapper の
  オプションを読み飛ばし、`env -C <dir>` / `--chdir` による移動も追う。`&&` / `||` の後ろの `cd` は
  実行されないことがあるため、移動した場合としない場合の両方を判定する
- 環境変数 `GIT_DIR` / `GIT_WORK_TREE` (コマンド前の代入・`env`・`export`) で対象を決める。
  `pushd` / `popd` のディレクトリスタックを追う。pipeline の要素と `&` の非同期コマンドは
  サブシェルで動くため、その中の `cd` は外に持ち越さない
- `&&` / `||` は直前のコマンドの成否ごとに状態を分けて追う (`cd A && cd B` のように、どちらに
  転んでもありえない状態を作らない)。`git branch -m` / `-M` を書き込み操作に含める。対象が複数ある
  操作は対象ごとに判定し、許可リストで 1 つ外れても残りで止める。`env -i` / `env -u` と、代入の
  あとで `export NAME` した変数を反映する
- here-document の本文を読まない (`cat > x.sh <<'EOF'` で git を含む script を書くだけで止めていた)。
  `git submodule update` など / `git sparse-checkout set` などを書き込み操作に含める
- 行き先が静的に決まらない操作は止めずに注意だけ出す。README / SECURITY.md に脅威モデル
  (うっかりの予防で、意図的なすり抜けへの対策ではない) と、読まない書き方 (`bash -c` / `eval` /
  関数 / script ファイル / コマンド置換) を初版から明記した
- レビュー用の AGENTS.md (`## Code Review Rules`) を置き、脅威モデルの範囲外の書き方を指摘対象から外す
- `WORKTREE_CWD_GUARD_MODE` (enforce / warn / off) と `WORKTREE_CWD_GUARD_ALLOW` で調整できる
