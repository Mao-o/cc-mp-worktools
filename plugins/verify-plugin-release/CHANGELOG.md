# Changelog

## 0.2.3

テスト整理 (挙動の変更なし)。テストの実行時間を約 33 秒から約 17 秒に縮めた。hook・`hooks.json`・README の
挙動は変わらない (patch bump)。

### Changed

- テストが起動するゲートの PATH から claude を外し、手元でも CI と同じく `claude plugin validate` を SKIP にした
  (`_testutil.path_without_claude`)。これまで手元では PATH 上の claude が見つかり、ゲートのたびに validate の実物を
  起動していた (1 回約 0.3 秒、suite 全体で 18 回前後)。手元だけ遅く、通る経路も CI と違い (validate の WARN で
  context を返す経路)、結果が claude CLI の版にも左右された。claude と git が同じディレクトリにある環境では外せず、
  従来どおりの挙動に留まる。hook プロセスの PATH から claude が外れていることは `test_hermetic_env.py` が見る
- テストごとの使い捨て repo の作成 (git 7 回、約 0.12 秒) を、plugin の組ごとに 1 度だけ作ってコピーする形にした
  (`make_marketplace`)。helper が起動する git を数える床は、コピーでは何も起動されず空になるため、毎回 git で作る
  `build_marketplace` を直接呼ぶ
- `GateTest` は、`tests[...]` を確かめるテスト以外では plugin の suite を走らせない (`test_command=False`)。
  `gate._POLL` を縮めて待ちを減らす (job を待たずに止めることを確かめる 2 本)
- 同じ経路を別のテストが確かめているものを整理した:
  - 削除: `test_complete_release_is_allowed_silently_or_with_context` (条件を満たした release を止めないことは
    `test_gate_does_not_trip_over_its_own_bytecode` が確かめる)、`test_failing_suite_fails` (落ちた suite が
    `tests[<plugin>]` の FAIL になることは `test_parallel_suites_attribute_results_to_their_plugin` ほかが確かめる)
  - 書き換え: `test_pr_command_in_substitution_is_denied` は repo に触れる前に決まる判定なので、hook の起動をやめて
    `evaluate` を直接呼ぶ (`UnresolvedCommandTest`)。`ConfigTest` は製品が使っていない `config.load` ではなく
    `config.parse` を確かめる
- `test_gate_does_not_trip_over_its_own_bytecode` を強くした。守りは 2 つ (suite に bytecode を書かせない
  `gate._TEST_ENV`、`gate._is_bytecode` で `git status` から除外) あり、元のテストは両方を壊さないと落ちなかった。
  既存の `.pyc` を置いて hook を 1 回だけ起動し、(a) 止めない (b) 新しい `.pyc` を作らない を別々に確かめる。
  テストの実行環境に `PYTHONDONTWRITEBYTECODE` があっても結果が変わらないよう、hook の起動で外す

### Verified

- 変異を入れた scratch コピーで、0.2.2 と今回の木を比べた (HOME は空 / 自動 maintenance を止める設定を持つ の 2 つ)
  - 床 (`test_hermetic_env.py`): helper の env 渡しを外す・`HERMETIC_GIT_ENV` から COUNT を外す・外側の repo 変数を
    外さない・repo 作成の `init` / `commit` が env を渡さない・hook の env が `hermetic_env` を通らない・bare repo が
    設定を書かない、の 7 件は 0.2.2 でも今回でも同じ件数の assertion で落ちる。新しい床も、PATH から claude を外さない
    変異と `path_without_claude` が何も外さない変異を assertion で検出する
  - ゲートの変異 15 件を suite 全体に当てた。0.2.2 で落ちた 9 件は今回も落ちる。0.2.2 で生き残った 4 件
    (suite に bytecode 抑止の環境変数を渡さない 2 形・bytecode を未 commit の変更に数える・条件を満たした
    release を止める) は今回落ちる。残りの 2 件 (jobs 数の条件・custom の分岐) は、どちらの木でも検出されない

## 0.2.2

テスト整理 (挙動の変更なし)。0.2.1 で入れた「自動 gc / maintenance が止まっている」ことを見る床が見逃していた
当て損ねの形を塞ぎ、テストの git が開発者の既定の除外ファイルと、外側の repo / config / template を指す環境変数を
読まないようにした。hook・`hooks.json`・README の挙動は変わらない (patch bump)。

### Changed

- 床 (0.2.1 の `tests/test_hermetic_env.py` と `test_main.HookLaunchEnvTest`) が見逃していた形を塞いだ。
  実測 (git 2.50.1。0.2.1 の木から取り出した床だけを scratch コピーで流した。HOME は 空 / 自動 maintenance を
  止める 5 設定を持つ / 既定の除外ファイル (`.env`) を持つ の 3 つ): 当て損ねの変異 25 件 (env を当てる 4 点 =
  定数 `HERMETIC_GIT_ENV` / repo を作る helper `sh` / ゲートを in-process で動かす基底クラス
  `HermeticGitTestCase` / hook プロセスの env `run_hook` の部分適用、混ぜる向きの逆転、検出器や床の退行、helper の
  git の迂回) のうち、12 件 (定数・helper・基底クラスの部分適用 11 件と、`run_hook` が env を足さない 1 件) は
  assertion で落ち、13 件が 3 つの HOME すべてで生き残った
  - 混ぜる向きの逆転 3 件 (helper / 基底クラス / hook プロセスの env で、外側の env が当てる側の値に勝つ)。
    床の外側の env が空で、混ぜる値が無かった
  - 基底クラスの `GIT_CONFIG_COUNT` 抜き 1 件。ゲートの git の床は 4 設定の有効値だけを見るので、global の
    fixture が同じ値で埋める
  - system の目印が何も設定しない 1 件。「目印が読まれない」を見る床に陽性対照が無く、目印が読めない環境
    では何も見ていない
  - trace の検出器が maintenance を数えない 1 件。陽性対照が無かった
  - helper の git を 1 つだけ env 無しで起動する 2 件 (`config` / `add`)。maintenance を起動しない git の迂回は、
    maintenance の起動を数える trace の床では差が出ず、起動した git の env を全件見る床も無かった
  - 床の退行 5 件。床が `GIT_CONFIG_NOSYSTEM` を立てる (床だけ / helper が落とす)、床が止める側の
    `GIT_CONFIG_COUNT` を持つ + helper が落とす、床の `GIT_CONFIG_GLOBAL` が fixture を指す + 定数が落とす、
    ゲートの床の外側の `GIT_CONFIG_GLOBAL` が fixture を指す + 基底クラスが落とす。床が当て損ねを埋めるので
    検査が黙って通る
- 別に実測した 2 点 (どちらも 0.2.1 の木)
  - 既定の除外ファイル (`$XDG_CONFIG_HOME/git/ignore`) は `GIT_CONFIG_GLOBAL` では外れない。suite 全体 (82 件) を、
    既定の除外ファイルが `.claude/` と `*.py` を除外する HOME で流すと 17 件が落ちた (`failures=6, errors=11`)。
    repo を作る helper の `git add -A` が、設定 file (`.claude/verify-plugin-release.json`) や `.py` を commit
    しないため。空の HOME と、`.env` だけを除外する HOME では 82 件とも通る
  - 外側の env の `GIT_DIR` などは、helper の git にそのまま届く (0.2.1 の `sh` は `os.environ` ごと継ぐ)。0.2.1 には、
    これが届かないことを見るテストが無かった。git が別の repo や別の config を見る変数 (実測): `GIT_DIR` /
    `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_COMMON_DIR` / `GIT_OBJECT_DIRECTORY` /
    `GIT_ALTERNATE_OBJECT_DIRECTORIES` は repo の位置・index・object の置き場 (alternates を含む) を変え、旧来の `GIT_CONFIG` は
    `git config` の読み書き先をその file にし (`--global --list` は exit 129)、`GIT_CONFIG_PARAMETERS` は
    `GIT_CONFIG_COUNT` に勝つ。`GIT_NAMESPACE` は `symbolic-ref HEAD` では差が出なかった (影響は未確認だが、
    同じ組の変数として外す)。外側の `GIT_TEMPLATE_DIR` も helper の `git init` に届き、template の `hooks/pre-commit` が
    helper の commit で走り、template の `info/exclude` が除外する file (`*.json` にすると `marketplace.json` /
    `plugin.json`) が commit から外れる (実測)
- `tests/_testutil.py` の変更
  - `HERMETIC_GIT_ENV` に `XDG_CONFIG_HOME` (プロセスごとに作る空の dir) を足した。repo を作る helper・ゲートの
    git (基底クラス)・hook プロセスのすべてで、既定の除外ファイルを読ませない。0.2.1 では `test_main.run_hook` と
    床だけが空にしていた
  - 外側の env にある、git が別の repo や別の config、別の template を見てしまう変数 10 個 (`OUTER_GIT_LEAK_ENV`。
    上の 9 個と `GIT_TEMPLATE_DIR`) を、repo を作る helper (`hermetic_env()`) と基底クラスで外す。`GIT_TEMPLATE_DIR`
    は空の template dir に向けず、外すだけにした (外せば `git init` は既定の template を使う。global の
    `init.templateDir` は、`GIT_CONFIG_GLOBAL` が指す fixture に無い)
  - hook を subprocess で起動する経路を `launch_hook` (env は `hook_process_env`) の 1 本にした。`test_main` に 3 通り
    あった env の組み立てを寄せ、`run_hook` もここに移した
- 足した床 (`tests/test_hermetic_env.py`。0.2.1 の 9 件 (`test_hermetic_env` 8 件と `HookLaunchEnvTest` 1 件) から
  43 件。`HookLaunchEnvTest` は hook プロセスの env を見る床に統合した)
  - 起動の仕方 (定数だけ / helper / ゲートの git / hook プロセスの env) ごとに同じ 5 本を流す
    (`_HermeticConfigChecks`)。env を当てる各点の部分適用 (`COUNT` だけ・`NOSYSTEM` 抜き・`GLOBAL` 抜き・
    `COUNT` 抜き・`XDG_CONFIG_HOME` 抜き) と、混ぜる向きの逆転は、どれかがこの 5 本のどれかで落ちる
    - `GIT_CONFIG_COUNT`: repo 自身の config に止めない側の値を置き、git が見る値が止める側であること。env は
      repo 自身の config に勝ち、global の fixture は負けるので、env の経路だけを見られる
    - `GIT_CONFIG_GLOBAL`: `git config --global --list` が fixture の 5 設定と完全一致すること
    - `GIT_CONFIG_NOSYSTEM`: system の config の代わりに置いた目印が読まれないこと。前提として、
      `GIT_CONFIG_NOSYSTEM` が無い env では目印が読めることを先に確かめる (陽性対照)
    - `XDG_CONFIG_HOME`: 検査用の file を除外する既定の除外ファイルが外側にあっても、その file が未追跡として
      見えること
    - 床が当てる側の値を持たないこと (`test_the_floor_alone_stops_nothing`。下の「床を作る点ごと」の項)
  - 外側の env に、止めない側の値 (`GIT_CONFIG_GLOBAL` = 空の file、`GIT_CONFIG_COUNT` で `maintenance.auto=true`、
    検査用の file を除外する既定の除外ファイルを持つ `XDG_CONFIG_HOME`) を置いてから流す。当てる側が外側の
    値に勝つことを見る。この除外ファイルを `*` のようにすべてを除外する形にすると、当てる側が
    `XDG_CONFIG_HOME` を向け直し損ねたとき、helper の `git add -A` が何も add せず `git commit` が exit 1 で落ちて
    (実測)、assertion ではなく crash になる。そのため除外するのは検査用の file だけにした
  - 床を作る点ごと: 床の env だけで起動した git で、当てる側の値が見えないこと (と、床が置いたはずの止めない側の
    値が見えること) を、他の床と同じ `git_in` で確かめる。床が当てる側の値 (`GIT_CONFIG_NOSYSTEM`、fixture を指す
    `GIT_CONFIG_GLOBAL`、止める側の `GIT_CONFIG_COUNT`、除外ファイルを持たない `XDG_CONFIG_HOME`) を持つと、当てる側が
    同じ値を落としても床が埋めて、検査が黙って通るため
    - `isolate_git_config` (床を作る関数の自己確認): `TestTheIsolatedEnvStopsNothing`。床を作る前の env に、当てる側と
      同じ値と、開発者の global の config に見立てた file (外側の `HOME` の `.gitconfig`、`XDG_CONFIG_HOME` の
      `git/config`) を置いてから呼ぶので、実行者の HOME に左右されない。見るのは床を作った直後だけなので、trace /
      spy / push の床は、区間ごとに当てる側の直前で見る (次の 3 行)
    - trace の床 (`_spawned_while_making_a_marketplace`): helper を呼ぶ直前に `assert_the_floor_stops_nothing` (床の
      env で、`maintenance.auto` が未設定・global が空・system の目印が読める)
    - spy の床 (`test_every_git_launched_by_the_helpers_carries_the_env`): helper を呼ぶ直前に同じ確認と、床が
      `HERMETIC_GIT_ENV` のどの key も同じ値で持たないこと (spy の検査は記録した値を key ごとに比べるので、3 つの
      問い合わせが見ない `XDG_CONFIG_HOME` も key ごとに見る)
    - push の床 (`test_push_into_a_plain_bare_repo`): push の直前に同じ確認 (この床が守る当てる側は受け側の
      `receive-pack` なので、repo と bare repo を作る間に足した値も含めて見る)
    - 上の 5 本の床 (起動の仕方ごと): `test_the_floor_alone_stops_nothing`
    - 外側の repo / config / template を指す変数の床 (下): `test_the_floor_holds_the_outer_variables` と、置く変数が
      名前の一覧と同じであること (`test_every_placed_variable_is_in_the_name_list`)
    - 上の 2 つ (mixin の setUp) の自己確認は setUp のスナップショット (`self.floor_env`) を見るので、当てた直後の env
      (`self.applied_env`) から問い合わせの時点まで変わっていないことを、`query` / `launched_env` の中で起動の前後に
      確かめる (setUp の後段や継承先の setUp、継承先の起動の中身で足した値は、自己確認からは見えず、問い合わせには
      届くため)。この確認は mixin の `query` / `launched_env` に置き、継承先は起動の中身 (`launch` /
      `launch_and_capture_env`) だけを実装する。継承先が `query` / `launched_env` を上書きしていないことは mixin の
      `setUp` で見る。`query` / `launched_env` を通らずに直接起動するテスト (外側の repo を変えないこと・外側の
      template が効かないことは前後、ゲートの `fetch` の trace は前) も同じ確認を呼び、継承先がその確認を上書き
      していないことも `setUp` で見る。起動の中身が当てる側の値を自分で足す形 (`mock.patch.dict(os.environ, ...)`
      など範囲を限って変えて戻す・git に渡す env に足す・床の env が指す file の中身を書き換える) は、前後の確認
      では見えない (残り)
  - 外側の repo / config / template を指す変数 10 個が、起動の仕方 (helper / 基底クラス / hook プロセスの env) ごとに
    git に届かないこと。前提として、記録した env が床の `HOME` を持つこと (記録が空だと「届かない」が素通りする)。
    helper は外側の repo の全 file を書き換えないこと、外側の template (`hooks/pre-commit` と `info/exclude`) が
    helper の `init` / `commit` に効かないことも見る (前提として、その template が届けば `git init` に写ることを
    先に確かめる)
  - 床の側の道具: spy (`recorded_git_launches`) が、起動に渡った env をそのまま記録すること (値を足さない・落とさない。
    env を渡さない起動はその時点の `os.environ`)。spy が記録に当てる側の値を足すと、spy の床が helper の当て損ねを
    見逃す。hook プロセスの env を捕まえる fake (`env_passed_to_the_hook`) も同じ型なので、渡った env をそのまま返す
    ことを見る。どちらも、渡す env が当てる側のどの key も同じ値で持たないことを key ごとに前提にする (同じ値で持つ
    key は、spy / fake がその key を足しても記録が変わらない)
  - 陽性対照: trace の床は、止める設定が無い commit では maintenance の起動が trace に見えること (見えない git の版では
    「0 件」は何も見ていない)
  - helper (`make_marketplace` / `init_bare_origin`) が起動する git の全部が `HERMETIC_GIT_ENV` の全項目を持つこと。
    `subprocess.Popen` を包んで全件記録し、`any` ではなく `all` で見る。前提として、起動の件数 (13 件以上) と種類を確かめる
- コメント・docstring の訂正 (挙動の変更なし)
  - `tests/_testutil.py`: `gc.auto=0` は起動された maintenance の gc を走らせない設定で、git 2.50.1 ではこれだけでは起動は
    止まらない (実測: `maintenance.auto=false` だけが `maintenance run --auto` の起動を 0 にする。`gc.auto=0` だけでは起動が
    残る)。0.2.1 は「`maintenance.auto=false` / `gc.auto=0`: そもそも自動 maintenance を起動しない」と書いていた。0.2.1 の項
    (下) は履歴なので直さない
  - `tests/test_hermetic_env.py`: system の config に `maintenance.auto=false` (「2.55 では `gc.auto=0` でも同じ」) があると
    …の括弧書きを削除した (git 2.55 では実測していない)
  - `tests/_testutil.py`: 既定の除外ファイルについて、「空にしているのは `test_main.run_hook` と床だけ」を、全部で空にすることと、
    外さないと何が落ちるか (実測) に書き換えた
- 確認 (git 2.50.1。Windows と git 2.55 では流していない)
  - 修正後の床に、当て損ねの変異 69 件 (修正前に流した 25 件と、足した床・helper の迂回・外側の repo の変数・除外ファイル
    に合わせて足した 44 件) を、HOME 3 種 (空 / 5 設定を持つ / `.env` を除外) で流した。全件が assertion (`failures`) で
    落ち、`errors` は 0 件。落ちるテストの組は 3 つの HOME で同じ。例外は、床が `HOME` / `XDG_CONFIG_HOME` を向け直さない
    2 件で、実行者の HOME が結果を決める変異そのものなので、組の同一性は見ず、どの HOME でも落ちることだけを見た
    (`HOME` を向け直さない 1 件は、5 設定を持つ HOME でだけ落ちるテストが 1 件多い)
  - 修正前に生き残った 13 件を落とすテスト
    - 混ぜる向きの逆転 3 件: 起動の仕方ごとの `test_env_beats_the_repos_own_config` と
      `test_default_excludes_are_not_read`。hook プロセスの env は、外側の repo / config を指す変数が git に届かない床
      (`test_none_of_the_outer_variables_reaches_git`) でも落ちる
    - 基底クラスの `GIT_CONFIG_COUNT` 抜き: `TestGateLaunchedGit` の `test_env_beats_the_repos_own_config` と
      `test_fetch_started_by_the_gate_starts_no_maintenance`
    - system の目印が何も設定しない: 各起動の仕方の `test_system_config_is_not_read` (陽性対照の前提) と
      `test_the_system_marker_is_readable`
    - trace の検出器が数えない: `test_the_trace_sees_maintenance_when_nothing_stops_it`
    - helper の git を 1 つ env 無しで起動する 2 件: `test_every_git_launched_by_the_helpers_carries_the_env` と
      `test_the_helpers_leave_the_outer_repo_alone`
    - 床の退行 5 件: 床を作る点ごとの床 (`TestTheIsolatedEnvStopsNothing`、各起動の仕方の
      `test_the_floor_alone_stops_nothing`) と、当てる側の値の検査 (`test_system_config_is_not_read` /
      `test_global_is_the_fixture_only` / `test_env_beats_the_repos_own_config`)
  - マージ前レビューの指摘で足した確認 (上の 69 件はその前の版で流した)。どれも assertion で落ち、`errors` は 0 件
    - 床の自己確認の時点 (HOME 3 種で同じ件数): mixin の setUp の後段で床が `GIT_CONFIG_NOSYSTEM` を立てる (MS) は
      21 件、MS + 基底クラスが `GIT_CONFIG_NOSYSTEM` を当てない は 28 件、`TestGateLaunchedGit` の setUp の後段で
      `GIT_CONFIG_NOSYSTEM` を立てる (MSG) + 基底クラスが当てない は 7 件、`TestOuterRepoEnvAndTheBaseClass` の setUp の
      後段で外側の変数を外す (MOB) + 基底クラスが外さない は 1 件、外側の変数の mixin の setUp の後段で外側の変数を
      外す + 基底クラスが外さない は 3 件が落ちる。MS は修正前の版では suite 全体で生き残った。継承先が `query` /
      `launched_env` を上書きする形は mixin の setUp の前提で落ちる
    - wrapper の区間 (HOME 3 種で同じ件数): trace の床の区間で床が止める側の `GIT_CONFIG_COUNT` を持つ は 2 件、spy の床の
      区間で同じ値を持つ は 1 件、push の床で `isolate_git_config` の直後に `GIT_CONFIG_NOSYSTEM` を立てる / bare repo を
      作った後・push の前に立てる は各 1 件が落ちる。spy の床の区間で床が `HERMETIC_GIT_ENV` を持つ (W3') + helper の
      `add -A` が当てる側を渡さない、と、床が空の `XDG_CONFIG_HOME` を持つ (W3-xdg) + `add -A` が `XDG_CONFIG_HOME` を
      渡さない、は spy の床が落とす。後者は key ごとの確認を外すと生き残る (3 つの問い合わせは `XDG_CONFIG_HOME` を見ない)
    - spy が env を渡さない起動を空の env として記録する + 基底クラスが外側の変数を外さない は、spy の自己確認を外しても
      「届かない」の前提 (記録した env が床の `HOME` を持つ) で落ち、その前提も外すと生き残る
    - spy / hook の env の fake が記録に当てる側の値を足す + 当てる側が値を渡さない は、それぞれの自己確認で落ち、自己確認を
      外すと生き残る
    - helper と基底クラスが `GIT_TEMPLATE_DIR` を外さないと、外側の template の pre-commit が helper の commit で走り、
      pre-commit の検査を外しても `info/exclude` の検査 (`marketplace.json` / `plugin.json` が commit に無い) で落ちる
    - 起動の中身で env を変える形 (HOME 3 種で同じ件数): `TestOuterRepoEnvAndTheBaseClass` の起動の中で外側の変数を
      外す + 基底クラスが外さない は 1 件 (起動の後の確認を外すと生き残る)、`TestGateLaunchedGit` の起動の中で
      `GIT_CONFIG_NOSYSTEM` を立てる + 基底クラスが当てない は 7 件が落ちる。起動の中で `mock.patch.dict` で範囲を
      限って立てて戻す形は生き残る (上の「残り」)
    - helper が `GIT_TEMPLATE_DIR` を外さない + `TestOuterRepoEnvAndTheHelpers` の setUp で床から外す は、外側の
      template のテストを単独で流しても先頭の確認で落ちる (先頭の確認を外すと生き残る)
    - spy / hook の env の fake に渡す env が、当てる側の 1 key (`GIT_CONFIG_NOSYSTEM`) だけを同じ値で持つ形は、key ごとの
      前提で落ちる
  - suite 全体 (`tests/` の `unittest discover`) は 82 件から 116 件。Python 3.14 では 4 種の HOME (上の 3 種と、
    既定の除外ファイルが `.claude/` と `*.py` を除外する HOME) で全件通り、Python 3.11 は空の HOME で全件通る。
    `claude plugin validate` は警告なしで通り、ruff 0.16.8 は plugin 全体で通る

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
