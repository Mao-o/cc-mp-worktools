# Changelog

## 0.3.0

**字句の正規化が閉じない括弧・引用符・backtick か、終端行の無い here-document を残して読み終えたときは、別の読みでも
判定し、どれかで見つかった別 checkout への書き込みを止めるようにした (読みの和集合)。backtick の中で同じ行に閉じた
here-document は backtick の閉じで終え、次の行から後ろを本文として読み飛ばさないようにした (minor bump)。**

### 修正: backtick の中の here-document を backtick の閉じで終える

`` x=`cat <<E ` `` の次の行の `git -C <main> reset --hard` は、bash / zsh とも実行するのに止まっていなかった。
正規化が backtick の閉じで here-document を終えず、次の行から後ろを本文として飛ばしていた (0.1.5 も同じ)。
`$(...)` の閉じと同じく、backtick の閉じで、その中で始めた here-document を終えるようにした。外の深さで始めた
here-document (`` cat > x.sh <<'EOF' && echo `pwd` ``) の本文は、これまでどおり次の行から読み飛ばす。

後ろに区切り語と同じ行がある形 (`` x=`cat <<E ` `` / git / `E`、後ろに `git commit -F - <<'EOF'` が続く
`` v=`cat <<EOF ` `` など) は、その行を終端行と読んで閉じた入力になり、下の安全網も働かなかった。これも止まる。

### 変更: 読みが shell とずれたときの安全網

0.2.0 の正規化は、閉じない frame や終わらない here-document を残すと、その行から後ろを読まない。正規化の読みが
shell とずれている (shell は here-document と読まない `<<` を here-document と読んだ、など) と、後ろの別 checkout への
書き込みが止まらない。たとえば `${...}` の中の閉じない here-document (`echo ${x:-<(cat <<2E` / 本文 / `)}` の次の
行の git) は、bash 3.2 が後ろの git を実行するのに止まっていなかった。

正規化が閉じない frame か終わらない here-document (終端行が無いまま入力が終わったものと、本文が始まらないまま入力が
終わったもの) を残したときは、次の読みでも判定する。どれかで見つかったものを返し、同じ文面は 1 回にまとめる。

- 旧来の読み (0.1.5 まで): 行ごとに識別子の区切り語の here-document を除き、shlex の comment で読む
- 終わらない here-document を here-document と読まない読み。読み直して新たに終わらないものが出たら、それも外して
  読み直す (8 回まで)

閉じた入力の判定は変わらない (安全網は閉じない frame か終わらない here-document が残ったときだけ働く)。

### テストの追加

- `tests/test_unclosed_safety_net.py` (新規): 閉じない frame / 終わらない here-document の検出、backtick の閉じで
  here-document を終えること (後ろに区切り語と同じ行がある形、外の深さで始めた here-document の本文は飛ばすこと)、
  和集合の判定 (`${...}` の中の here-document・閉じない `"$(`・連鎖する読み直しと上限 8 回の境目・旧来の読みの comment と
  here-document の除去)、閉じた入力で別の読みを使わないこと、同じ finding を重ねないこと

テスト件数: 78 → 92 (追加 14・削除 0・統合 0)。

確認:

- 判定の比較 (10,619 件: 前置き 25 × 操作 31 × 後置き 10 + here-document 24 + 区切り語 23 形 × 本文 7 × 終端 3 × 配置 3
  + backtick の中の here-document の頭 18 形 × 操作 7 × 後ろの行 11 + 読み直しの連鎖 10) を 0.2.0 と 0.3.0 に流し、偽の
  git で zsh -f と bash 3.2 の実際の起動と並べた。0.2.0 で止めて 0.3.0 で止めないものは 0 件。0.2.0 で止めず 0.3.0 で
  止めるものは 1,333 件で、969 件は bash で別 checkout への書き込みが実際に起動する (うち 630 件は zsh でも)。残る 364 件は
  どちらのシェルでも何も実行されない壊れた here-document (終端行に tab を付けて `<<-` でないもの・終端を `)` と同じ行に
  書いたもの・終端行の無いもの) で、止める側に倒した結果。here-document の本文の中に書いた git (スクリプトを書き出す形) も、
  終端行が壊れていれば (字下げ・末尾の空白・`)` と同じ行) 止める
- 0.2.0 と 0.3.0 の差分 fuzz (平らな断片 2 万件 + 入れ子の文法型 3 万件 × 2 seed。backtick の中で閉じる here-document
  (後ろに区切り語と同じ行がある形・escape した backtick の入れ子を含む)・終わらない here-document・閉じない `$(` / `"$(` を
  生成に足した): 実際に走る git の引数列が 0.2.0 の候補にあり 0.3.0 で消えるものは 0 件

## 0.2.0

**行末の comment や `\` + 改行の折り返しをはさんだ別 checkout への git の書き込みを止めるようにし、別 checkout を
指す `git stash list` / `show` / `create` と `git apply --check` などの読み取りを止めないようにした (minor bump)。**

### 修正: comment が次の行を前の行につないでいた

`git status  # 状態を確認` の次の行に `git -C <main> checkout -b x` を書くと、2 行が 1 つのコマンドとして読まれ、
2 行目の書き込みが止まらなかった。`cd <main>  # main へ移る` の次の行の `git checkout` も同じ。字句に分ける
shlex は comment を改行ごと読み捨てるうえ、`feature#1` のような単語の途中の `#` からも comment を始めていた。

字句に分ける前の正規化 (`_Normalizer`) を足し、comment を bash / zsh と同じ規則で捨てるようにした。

- comment は単語の始まり (先頭・空白・タブ・改行・`;` `&` `|` `(` `)` `<` `>` の直後) の `#` から改行の手前まで。
  改行はコマンドの区切りとして残す。backtick の中では閉じの手前まで
- 単語の途中 (`feature#1`、`$(pwd)#x`)・引用符の中・`\#` の `#` は comment ではない
- comment の中の `\` は行を継続しない

### 修正: `\` + 改行の折り返しで git の引数が別のコマンドになっていた

`git -C <main> \` + 改行 + `  checkout -b x` は、`git -C <main>` だけが 1 つのコマンドとして読まれ、止まらなかった
(`cd <main> && git \` + 改行 + `  commit -m msg` も同じ)。引用符の外と二重引用符の中の `\` + 改行を取り除いて
1 行につなぐ。単一引用符と `$'...'` の中では文字どおり残す。継続は単語の始まりかどうかを変えない
(`--hard \` + 改行 + `# c` の `#` は comment)。

### 修正: 字句の読みを shell に揃えたことに伴う周辺の読み

- here-document の開始 (`<<EOF`) は引用符と comment の外でだけ認める。`echo "<<E"` や `git status  # a<<E` の後ろの
  行が本文として読み飛ばされていた。同じ行で閉じるコマンド置換の中で始まったもの (`echo $(cat <<E) && ...`) は、
  外の行を本文にしない
- here-document の区切り語は shell の 1 単語として読み、引用符と `\` を外したものを終端行と比べる。`<<\EOF`・
  `<<'END MSG'` (空白を含む)・`<<'2EOF'` (数字始まり)・`<<E"O"F`・`<<$'E'`・`<<E$x` (展開せずに文字どおり) も
  here-document として読む。読めないと本文をコードとして読み、本文の `don't` の `'` や対応の無い `(` で後ろの行を
  すべて捨てていた (`git commit -m "$(cat <<\EOF` … `)" && git -C <main> reset --hard` が止まらなかった)。`$(` や
  backtick を含む区切り語と、`(` が続く区切り語 (zsh は `<<E(x)` の `E(x)` までを区切り語にする) は here-document と
  読まない (手前で切ると、shell が終端と見る行の後ろを本文として飛ばす)
- 外の here-document の本文は、それを書いた深さの改行から始める。同じ行のコマンド置換 (`"$(...)"` / `$(...)` /
  backtick / `<(...)`) の中の改行からは始めない (`cat <<\E "$(cat <<'E' …)" ; git -C <main> reset --hard` の
  `; git` を本文として飛ばしていた)。算術の `((...))` と、単語に続けて書いた `(...)` (zsh の glob の group。bash は
  構文エラー) の中の改行からも始めない。サブシェルの `(` の中の改行からは始める (bash / zsh と同じ)
- 算術 (`((...))` と、複数行に分けた `$((...))`) の中の `<<` はシフトとして読み、here-document を始めない。
  区切り語に数字や `$var` を受け付けるようにしたため、`(( x = 1 << 2 ))` の後ろの行を本文として捨てていた
- 単語に続けて書いた `(...)` の閉じの直後の `#` は comment にしない (zsh は単語の一部として読む)
- 二重引用符の中の `$(...)` は中身を読まずに `$()` として扱う。`git commit -m "$(cat <<'EOF' ...)"` の本文に
  `"` や `'` があっても、外の引用符の対応が崩れない (中の git を読まないのは従来どおり)
- `$'...'` の中の `\'` は引用符を閉じない。`echo $'it\'s' && git -C <main> reset --hard` が分解できずに素通りしていた
- 閉じない引用符があると、従来はコマンド全体を判定しなかった。bash はその引用符を含む行の手前までを実行するので、
  その手前の行までを判定する。末尾の `\` は shell と同じく捨てる

### 修正: 別 checkout を指す読み取りの `git stash` / `git apply` を書き込みとして止めていた

`git -C <main> stash list` / `stash show -p` / `cd <main> && git stash list` と `git -C <main> apply --check` /
`--stat` が deny され、文面も「`git stash` が別の checkout を書き換える」と誤っていた。

- `git stash` は最初の引数が `list` / `show` / `create` のときだけ読み取り (subcommand は最初の引数にしか書けない。
  `git stash -m list` は message が `list` の push で、書き込みのまま)。引数なし・`push` / `pop` / `apply` / `drop`
  などは書き込みのまま
- `git apply` は `--check` / `--stat` / `--numstat` / `--summary` のどれかがあり、`--apply` (略記 `--ap` / `--app` /
  `--appl` を含む) が無いときだけ読み取り。`--` の後ろは option として読まない

### テストの追加・変更

- `tests/test_lexing_and_reads.py` (新規): comment・行の継続・here-document (区切り語の形と本文の始まる位置を含む)・
  `$'...'`・閉じない引用符・コマンド置換と backtick の読み、stash / apply の読み取りと書き込み
- `tests/test_guard.py`: 使い捨ての repo を用意する部分を `GuardBase` に分けた (新しい module が `GuardTest` の
  テストを重複して走らせないため)

テスト件数: 61 → 77 (追加 16・削除 0・統合 0)。

確認:

- comment・行の継続・`#` の位置ごとの読みを `/bin/bash` (3.2) と `zsh -f` で実測し、両方で同じ読みに揃えた
- 0.1.5 と 0.2.0 に同じコマンドを流し、git として読んだコマンドの引数列を、実際に bash / zsh で偽の git を
  走らせた引数列と突き合わせた (平らな断片 2 万件 + 入れ子の文法型 3 万件 × 2 seed。here-document の区切り語の
  形 (`\EOF`・`'END MSG'`・`'2EOF'`・`<<-`・`"EOF"`・`$x` など) と、本文の `'` / `(`、終端の位置を生成に含める)。
  「zsh で実際に走り、0.1.5 は読めて 0.2.0 は読めない」ものはどちらの生成でも 0 件。bash 3.2 だけで走るものが
  文法型で 70 件残った。どれも zsh が構文エラーで何も実行せず、bash 3.2 だけがコマンド置換・プロセス置換の中で
  閉じない here-document や comment を独自に読んで後ろの git を実行する形 (例: `git commit -m "$(cat <<\EOF` /
  本文 / `EOF)" && git x`、`true"$(#)<<E" ; git x`)
- here-document の区切り語 23 形 × 本文 7 種 × 終端の書き方 3 種 × 配置 3 種 (1,449 件) を zsh -f と bash 3.2 で偽の
  git を使って走らせ、0.1.5 と 0.2.0 の判定と並べた。0.1.5 で止まり 0.2.0 で止まらないもの (278 件) のうち、zsh で
  別 checkout への書き込みが走るものは 0 件 (どれも終端が閉じの行にあるか tab 付きで閉じない形で、zsh は何も
  実行しない。うち 80 件は bash 3.2 だけが実行する)。0.1.5 で止まらず 0.2.0 で止まるものは 271 件で、うち 200 件は
  zsh で実際に書き込みが走る。zsh で走るのに 0.2.0 が止めないのは、0.1.5 も止めない `<<E$(...)`・
  `` <<E`...` ``・`<<E(x)` を区切り語にした 24 件
- 判定の比較 (3,826 件): 0.1.5 で deny / 注意 → 0.2.0 で止めないのは、別 checkout を指す stash / apply の
  読み取りだけ (1,064 件)。0.1.5 で止めない → 0.2.0 で deny は 497 件で、すべて comment・行の継続・
  `$'...'`・引用符の中の `<<E` を前に置いた別 checkout への書き込み

## 0.1.5

icon と導入手順の追加 (挙動の変更なし、patch bump)。

- plugin の icon (`assets/icon.png`) を足した。`claude plugin validate` の plugin directory checks が出す `ICON_MISSING` を解消する
- README に marketplace からのインストール手順の節を足した

## 0.1.4

**テスト整理 (挙動の変更なし)。テストが起動する git の隔離を固めた (patch bump)。** repo を作る helper の
git を 1 つだけ迂回しても気付けなかった穴と、外側の env や開発者の設定が helper と基底クラスの git に漏れる穴を
塞いだ。hook・`hooks.json`・README の挙動は変わらない。

### テスト: helper が起動する git の全件に env が届くことを見る

0.1.3 の trace の床は、maintenance を起動する git (`commit`) しか見ない。`make_repo` の `worktree add` を
env なしで起動しても床には差が出なかった。起動した git の `(argv, 実際に渡った env)` を `subprocess.Popen` の
spy で全部記録し、全件が `HERMETIC_GIT_ENV` の全項目を持つことを見るテストを足した
(`test_every_git_launched_by_the_helpers_carries_the_env`)。

### テスト: 外側の repo / config / template を指す変数を外す

`_testutil.OUTER_GIT_LEAK_ENV` (`GIT_DIR` / `GIT_WORK_TREE` / `GIT_INDEX_FILE` / `GIT_COMMON_DIR` /
`GIT_OBJECT_DIRECTORY` / `GIT_ALTERNATE_OBJECT_DIRECTORIES` / `GIT_NAMESPACE` / 旧来の `GIT_CONFIG` /
`GIT_CONFIG_PARAMETERS` / `GIT_TEMPLATE_DIR`) を足し、`hermetic_env()` (helper `sh()` が使う) と
`HermeticGitTestCase` が外すようにした。`HERMETIC_GIT_ENV` は外したあとに足すので、定数が勝つ。外さないと、
別 repo を指す `GIT_DIR` では helper の `init` / `config` / `commit` がその repo に書き込み、
`GIT_TEMPLATE_DIR` では template の `pre-commit` や除外ファイルが helper の commit に効く。

### テスト: 開発者の既定の除外ファイルを外す

`$XDG_CONFIG_HOME/git/ignore` (未設定なら `~/.config/git/ignore`) は `GIT_CONFIG_GLOBAL` では外れず、
`*.md` があると `make_repo` の `add -A` が README を拾えず commit が失敗した。`HERMETIC_GIT_ENV` に空の
`XDG_CONFIG_HOME` を足した。除外ファイルの出どころ (`XDG_CONFIG_HOME` が指す dir / `HOME` 下の `.config`) ごとに
README が commit されることを見るテストを足した。

### テストの追加・変更

- `tests/_testutil.py`: `OUTER_GIT_LEAK_ENV`・`hermetic_env()`、空の `XDG_CONFIG_HOME` (`EMPTY_XDG_CONFIG_HOME`)
- `tests/test_hermetic_env.py`:
  - `_OuterRepoEnvChecks` が、起動の仕方 (helper / 基底クラス) ごとに、外側の変数が git の env に届かないこと、
    helper が外側の repo を変えないこと (外側の変数が指す場所に作った repo の全 file の中身の前後比較。その
    場所を指していることを前提として確かめる)、外側の template の `pre-commit` が走らず除外ファイルが効かない
    ことを見る
  - 床 (`isolate_git_config`) が外側の変数を外すこと、床が当てる側の値を持たないことの確認を当てる側を呼ぶ
    直前にも置いたこと、床の側の道具 (spy・問い合わせ) が値を足さないことの確認
  - 陽性対照の git は、外側の変数を置いたうえで env から外して起動する (外し損ねたら、外側に書き込む前に
    失敗として検出する)。失敗したときは例外のまま crash させず失敗として報告する

テスト件数: 45 → 61 (追加 16・削除 0・統合 0)。

確認:

- 変異 8 件 (`OUTER_GIT_LEAK_ENV` から `GIT_DIR` を外す (前提の行を外した形も)、外側の repo を 1 段深く作る旧形、
  陽性対照の env の除外を外す (床が外側の変数を外さない形も)、`XDG_CONFIG_HOME` を外す、`worktree add` を env なしで
  起動する) を、外側の env に `GIT_DIR` (別 repo) を置き、HOME は 空 / 自動 maintenance を止める 5 設定を持つ /
  既定の除外ファイルを持つ の 3 つで流した。すべてが assertion の失敗で落ち、別 repo は変わらなかった。
  `worktree add` を env なしで起動する変異は、`test_every_git_launched_by_the_helpers_carries_the_env` が
  3 つの HOME すべてで落とす

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
