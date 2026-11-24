# Changelog

## 0.4.0

確かでない cd の後ろの PR 操作を、plugin repo でない repo と repo の外では止めないようにした (minor bump)。これまでは
cd が走ったかを決められないと repo を見る前に止めたので、`git push && cd A; gh pr create` や `[ -d A ] && cd A; gh pr create`
が、この hook の対象でない repo でも PR を作れなかった。あわせて、Windows の Git Bash の `/c/Users/...` の綴りを読むようにした。

### Changed

- 確かでない cd の後ろでは、shell が居うる cwd をすべて挙げ、どれも plugin repo でなければ何もしない。挙げるのは、hook の cwd に
  それまでの cd を「走った / 走らなかった」の組み合わせで順に当てた先 (ループと関数の外では、cd は出てきた順に高々 1 回走り、
  走らないか失敗すれば cwd は変わらないため)。括弧の中・パイプの左・`env` の後の cd も足すので、候補は多めになる (止める側)。
  挙げた cwd の 1 つでも plugin repo なら、これまでどおり止める
- 挙げられない形は、これまでどおり止める: 引数なしの `cd`、変数・glob・波括弧・`~user` / `~+` / `~-` / `+1` / `=x` を含む引数、
  `cd -`、ループ・関数・`case` の中の cd、`chdir`、`cd A B`、語の並びで cd と読まなかった cd の語 (引用や `trap` の本文の中)、
  `eval` / `source` / `.` か展開で決まるコマンド語がある形、括弧が閉じない形、挙げた cwd が 16 を超える形
- hook の環境に `CDPATH` があるときは、相対の名前 (`cd A` / `cd A/B`) を含む形を挙げない (bash は cwd より先に、zsh は
  cwd に無ければ CDPATH の下を探す)。`./A`・`../A`・`~`・絶対パスは CDPATH を引かないので挙げる
- hook の時点で行き先が無い組み合わせが 1 つでもある形は挙げない (止める)。同じコマンドの前のほうで作られうるため
  (`git clone URL d && cd d; gh pr create` / `mkdir -p d && cd d; gh pr create` / `git worktree add` の先)。確かな cd の
  「移動先が存在しない」と同じ扱いで、前の cd の先から当てた組み合わせ (`(cd A); cd A` の `A/A`) だけが無い形も止める
- Windows でドライブの無い絶対パス (`/tmp`) の cd を含む形は、行き先が在っても挙げない (止める)。Git Bash は別の場所
  (`/tmp` は Windows の一時 dir) に読み替えるが、Python は hook の cwd のドライブの下と読む
- `CDPATH+=` / `cdpath+=(...)` / `HOME+=` の追記も、cd の行き先を変える代入と読む (`CDPATH=` と同じく、後ろの cd は
  確かな cd でも止める)
- hook の時点で在る dir を同じコマンドの前のほうで plugin repo に変える形 (空の dir への clone・symlink の付け替え・
  `rm -rf d && git clone URL d`) は、確かでない cd の後ろでも検査しない。0.3.10 は確かでない cd の後ろを一律に止めていたので
  止まっていた (確かな cd では 0.3.10 も検査していない)。README の目的と限界に書いた

### Fixed

- Windows の Git Bash の `/c/Users/...` を `C:/Users/...` と読む。これまでは hook の cwd のドライブの下の `c` (`C:\c\Users\...`)
  と読み、plugin repo かどうかに関係なく「cd の移動先が存在しない」で止めていた (止めすぎ。検査せずに通す向きの抜けではない)。
  hook の cwd が同じ綴りで渡されたときも同じく読む。POSIX の `/c/...` はそのまま
- ntpath に差し替えて Windows のパスの読みを確かめるテストと、Windows の CI でだけ走る実際のパスのテストを足した

### Tests

- 挙げた cwd に、bash と `zsh -f` で実際に gh が走った dir が必ず入ることを、前のコマンドの成否と dir の有無を変えて確かめる
  テストを足した (止めていた形の表と、新しく数え上げる形の表の全件)。前のコマンドが行き先を作る形 (本物の `mkdir` /
  `git clone` / `git worktree add`) も流す
- plugin repo でない repo と repo の外で、確かでない cd の後ろを止めない形・止める形 (行き先に plugin repo がある、挙げられない、
  `CDPATH` / `..`、hook の時点で無い行き先、上限、repo の確認の失敗) のテストを足した

### Docs

- README の「目的と限界」に、plugin repo かどうかは hook の時点の作業ツリーで決めるので、hook の時点で在る空の dir に
  前のコマンドが clone する形 (`git clone URL empty && cd empty && gh pr create`) は確かな cd でも検査しないことを書いた

## 0.3.10

README のみの変更 (hook 本体・テストの挙動の変更なし、patch bump)。「目的と限界」に、うっかり予防の範囲外として塞がない形をまとめて書いた。

### Docs

- 安全網が数だけを突き合わせるので、`\gh pr create -B main; echo "$(gh pr create -B other)"` のように `\gh` と引用の中の `$(gh ...)` を並べた形では、数が合って別の `gh` の検査が抜けることを範囲外として明記した (同じ `gh` を単独で書けば従来どおり止まる)。位置で突き合わせる改修と、`\gh` / 引用した `gh` も数える小さい案は採らない。後者は、この再現そのものを塞げず、前者は判定の境界が変わる大きい改修になるため
- 見つけられずに通すことがある、普段書かない形を列挙した (`sudo` / `xargs` / `/usr/bin/env` の後ろ、`g\h`、`gh` と `pr` の間のリダイレクトや展開、`bash -c` の中の入れ子の引用、`X+=1` などの珍しい代入、展開で組み立てた `cd` など)。止めすぎる形 (comment や引用の中の文字列、検査先は 1 つに決まるが読みを足していない `cd` の形) も書き、抜け方 (`cd <絶対パス> &&` / `--body-file`) を添えた
- 先頭の箇条の「検出できない形は止める側に倒している」を、「読めないと分かった形は止める側に倒しているが、普段書かない形では気づかず通すことがある」に直した (入れ子の引用などで成り立っていなかった)
- 載せた形は、すべて現行の版で 1 つずつ流して、止まる / 通るの区別が文面と一致することを確かめた。一致しなかった形は載せていない

## 0.3.9

テストの修正 (hook 本体の挙動の変更なし、patch bump)。Windows の CI で、子の Python に実時間を測らせるテストが落ちていた。

### Fixed

- 子の Python が理由 (日本語) を stdout に書くテストで、子の stdout を UTF-8 に固定し (`PYTHONIOENCODING=utf-8`)、親も UTF-8 で読むようにした。Windows では pipe につないだ子の stdout が cp1252 になり、日本語を書けずに子が落ちていた (0.3.7 で入ったテストで、これまで Windows の CI を通っていなかった)。環境が子の stdout を cp1252 にしていても通ることを見るテストを足した
- 同じ plugin のテストで子の Python を起こしている他の箇所は、stdout に書くのが数値と JSON だけ (JSON は ASCII にエスケープされる) なので変更不要。hook 本体は起動時に stdout / stderr を UTF-8 に固定しているので、Windows でも日本語の出力は書ける

## 0.3.8

テスト整理 (挙動の変更なし、patch bump)。読み取りと hook 全体に期限が入ったので、子で読む形は素通しではなく止まるだけになり、形ごとの実時間の上限を別々に持つ価値が下がった。CI の時間と、遅い Windows runner での揺れを減らすため、その上限を 1 本にまとめた。

### Tests

- 子で読む最悪の形の実時間の上限を、「期限 + 余裕 (`READ_SECONDS` + 5 秒) で必ず終わる」の 1 本にまとめた。上限ちょうどの 2 乗の形と、以前に直した 3 乗の形 (`sh -c` の並び + 長いオプション語) を、期限つきの読み取りで流して同じ上限で測る。3 乗の形だけを同じプロセスで測っていたテスト (8 秒) は、この 1 本の 2 つ目の形に統合した (3 乗の形は、期限の中で読み終えて時間切れで止まらないことも見る)
- 残したもの: 時間切れ・子の異常終了・結果を読めないときに止まるテスト、hooks.json の timeout と hook 全体の予算の関係のテスト、README に書いた値の固定、上限を超えるコマンド (子に渡さず同じプロセスで語だけを見るので、期限が効かない) の 1 MB の時間のテストとその秒数、線形時間のテスト (`assert_linear`) の形 (91 形のまま)。`assert_linear` の形を絞る作業は、統合でケース数を減らさないという削除の条件と両立しないので行わなかった

## 0.3.7

hook の時間切れでコマンドが素通しになる経路を、期限で閉じた (patch bump)。hook が時間切れになるとコマンドはそのまま走る。読み取りが遅い
形を個別に閉じる修正を重ねても、読み取りに制限時間が無いので `sh -c` を詰めた語のような形が残っていた (32768 文字以内で hook が 125 秒を
超え、実シェルでは `gh pr create` が走る)。形ではなく、時間切れの経路そのものを閉じる。

### Fixed

- hook 全体の予算でも返せなかったときの最後の手段を足した。Windows の `subprocess.run` は時間切れの後に孫プロセスが pipe を閉じるまで待つので、
  117 秒で固まっていれば止める判断を返して終わる (hook の 120 秒の手前)
- コマンドの読み取りに期限を付けた。4096 文字以上で `gh` と `pr` の文字を含むコマンドは制限時間 (15 秒) つきの子プロセスで読み、時間切れ・
  子の異常終了・結果を読めないときは止める (`--body-file` を案内する)。POSIX と Windows で同じ経路。小さいコマンドは同じプロセスで読むので、
  起動の時間は増えない。4096 文字の手前の最悪の形は同じプロセスで約 0.8 秒、32768 文字の最悪の形は約 9 秒で、Windows の 4 倍を見込むと
  期限を超えるが、その形は止まる側に倒れる
- hook 全体で 1 つの予算 (105 秒) を持つようにした。読み取り・repo の確認・設定の読み・ゲートは予算の残りを上限に動き、予算が尽きたら止める。
  これまでは `timeout_seconds` が 110 のときに repo の確認と設定の読みが重なる形 (実測 124.9 秒) と、1 つのコマンドの複数の PR 操作
  (`gh pr create --draft && gh pr ready`) で hook の 120 秒を超えて素通しになっていた。`timeout_seconds` が予算で縮んで止まったときは理由に書く
- 32768 文字を超えるコマンドは読まない。`gh` という語、単独の `pr`、`create` / `new` / `ready` が揃えば止め、揃わなければ通す。大きさは読む前の
  文字数で数える。手元の実コマンドで PR 操作の文字列を含むものの最大は 17575 文字、実際に PR 操作として読めたものの最大は 7678 文字で、誤停止は 0 件
- 判定が変わる形: 上限を超えて語が揃うものは、`gh pr ready --undo` や別々の場所にある語でも止める (これまでは読んで判定していた)。`gh` の `--repo` の
  9 個以上の並びは、語が揃わなくても上限を超えれば読まずに止める (語の途中の展開を挟む `gh -R a/b` 9 個 + `pr cre$()ate` もこれまでどおり止まる)
- 上限を超える `through pr create` のように語の一部が `gh` で終わるものは、語が揃わないので通す (括弧の並びなどの副作用で止まっていた形を含む。
  これらは `gh` を起動しない)
- `bash -c` の検出の正規表現の 3 乗の時間を直した (語の長さの 2 乗 × 始まりの数。32768 文字で 3300 秒 → 0.81 秒)

個別の上限は残した (外すと判定が緩むか、時間切れを超える)。32768 文字以下のコマンドの判定は、期限に当たるものを除いて変わらない。

### Tests

- 期限で止まること (時間切れ・子の異常・実 hook)、子と同じプロセスの読みが同じ結果になること、予算の共有、理由の文面、hooks.json の timeout と
  予算の関係、README に書いた値の固定、上限の境界、語が揃うときと揃わないとき。線形時間のテストは上限の手前の読みを測るので、測るときだけ上限を外す

## 0.3.6

CI の赤を直した (patch bump)。Linux の線形時間のテストの赤はタイマーの揺れではなく、実在する 2 乗の時間だった。

### Fixed

- `${${${...` や `a=$(a=$(...` のように 1 つの語に実行位置の始まり (`(` `{` backtick) が並ぶコマンドで、PR 操作の数え上げが語の
  長さの 2 乗の時間になっていた (`a=$(` 4000 個で約 7 秒。hook が時間切れになるとコマンドはそのまま走る)。gh の前のパスと代入の
  値の読みが、始まりごとに語の残りを読み直していた。空白も区切りも無い 1 つの語にこの文字が 256 個以上あり、PR 操作もある
  コマンドは、読まずに止める
- `$(a=" "` のように語を引用符・`\ `・`$(b c)`・backtick の中で割っても同じ 2 乗が残っていた (24000 個 168 KB で約 178 秒)。代入の値と
  リダイレクトの対象は語の区切りをまたいで読むため。`(` `{` backtick の直後に代入かリダイレクトが続くものがコマンド全体で 256 個以上
  あり、PR 操作もあるコマンドも読まずに止める
- 改行の並びが、改行ごとに残りの空行を読み直して 2 乗の時間になっていた (引用のある heredoc の本文は改行だけにして数えるので、
  本文 32000 行で約 90 秒)。読む前に空行の並びを畳む。`\f` や全角の空白を挟む並びも同じ
- `(gh -R (gh -R ...` のように `--repo` の値が次の gh を読む形も 2 乗の時間だった。`--repo` を 9 個以上続けて並べた形は止める
- 括弧などが多すぎて止めるときの案内を `--body-file` に直した (単独のコマンドにしても止まるため)

### Tests

- 線形時間のテストは、大小の入力を 2000 個 / 8000 個に増やし、交互に測った最小値の比を形ごとに見る `assert_linear` にまとめた。
  「0.05 秒未満の揺れは見ない」の条件が手元で上の 2 乗を隠していたので外した。秒数の上限は機械の速さに左右されるので、固まって
  いないことの確認 (10 秒) だけにした
- `assert_linear` は大きい入力が小さい入力の 1.5 倍を超えて伸びないと落とす。早期終了で大きい方だけ速くなるテストは線形を測っていない
- 引用なしの Windows のパス (`cd C:\Users\...`) をコマンドに埋めていたテストは `/` の区切りで書く。シェルは `\` を外すので、
  ゲートが移動先を確かめられずに止まるのは正しい読みで、テストの前提の誤りだった。Windows のパスの綴りの読みを確かめるテストを足した
- 偽の gh を PATH の POSIX の形で置く `RealShellTest` は Windows では走らせない。読みの表のテストは Windows でも走る

## 0.3.5

cd が後ろの PR 操作の cwd を変えるかを 3 つに分け、「変えるか確かでない cd」の後ろは止め、「変えない cd」の後ろは元の cwd を
検査するようにした (patch bump)。これまで cd が 1 回の形は、シェルの cwd を変えない cd でも移動先を検査先と読み、不備のある
repo でゲートを見ずに素通しになっていた。

読みを足す案は入れなかった。`&&` の左が成功するか・分岐を通るかは語の並びから決まらないので、走るか確かでない cd は読まずに止める。

### Fixed

- シェルの cwd を変えない cd の後ろの PR 操作は、元の cwd を検査する (これまでは cd の移動先を検査していた):
  `(cd X && make) && gh pr create` / `(cd X); gh pr create` / `(cd X) && Y=1 gh pr create` / `ROOT=$(cd .. && pwd); gh pr create` /
  `X=$(cd Y);(gh pr create)` / `cd X | cat; gh pr create` / `cd X | gh pr create` / `cd X & gh pr create` / `env cd X; gh pr create` /
  `nohup -- cd X; gh pr create`。この cd に相対の cd が続く形は、これまでどおり止める
- cwd を変えるか確かでない cd の後ろの PR 操作は止める (`gh` が走る dir がシェルや実行時の値で変わる):
  - `cd A || cd B`、`cd A || true`、`true || cd A` (cd が 1 回の形も。`cd A || exit` の A は決まる)
  - パイプの右の cd (`true | cd A; gh pr create`。zsh だけ移る)
  - `&&` の前に別のコマンドがあり後ろが `;` か改行で続く形 (`[ -d A ] && cd A; gh pr create` / `make && cd ..; gh pr create`)。
    `make && cd A && gh pr create` は止めない
  - `&&` と改行の間に空白やコメントがある形 (`false && ` + 改行 + `cd A; gh pr create`)。改行で区切りの種類を上書きしていた
  - `if` の分岐を閉じた後や `else` の後 (`if [ -d A ]; then cd A; fi; gh pr create`)、ループの中、`case` の分岐、関数の中の cd
    (関数があると読みをやめる)
  - `&` の前のリストの cd (`cd A && true & gh pr create`)、`{ cd A; } | cat`。`cd A; true & gh pr create` の gh は実際には
    常に A で走るが、`&` の前を切り分ける読みを足していないので、これも止める
  - 語の並びでは読まないが移る cd (`X=$(a b) cd A`、`$x cd A`、`command -p cd A`、`builtin -- cd A`)
  - コマンド語が展開で cd を書いた形 (`$(echo cd a); gh pr create`、`` `true` cd a; gh pr create ``)。cd を書いていない形
    (`$PY -m pytest && gh pr create`、`$c d; gh pr create`) は読まない
  - シェルで読みが割れる形: `command cd A` (zsh は外部コマンドを探し、`/usr/bin/cd` があると移らない)、`time cd A`
    (bash 3.2 の `if time cd A; then` は移らない)、`exec cd A`、`chdir A`、`builtin chdir A`、`cd A B` (zsh は `$PWD` の A を B に
    置き換える)
  - 値にコマンド置換がある代入の後ろの `&&` の cd (`X="$(make)" && cd A; gh pr create`。`make` が失敗すると gh は元の cwd で走る)
  - `CDPATH=` / `cdpath=` / `HOME=` の代入があるコマンドの cd (行き先が変わる)
  - 引数なしの cd (shell は `$HOME` に移るが、hook の cwd を検査していた)
- `builtin cd A` を cd として読む。これまでは読まず、hook の cwd を検査していた
- 引用かエスケープで記号だけになった語 (`git log --grep ')'` / `foo \(` / `find ... -exec x {} \;`)、括弧の対応が取れていない
  `${x#(}`を、括弧や区切りと読まない。これまでは `foo ';' cd A; gh pr create` の引数を cd と読み、括弧の深さの読みもずれていた。
  二重引用符の中の置換のさらに内側の二重引用符の中身に記号があるときは、cwd を変えうる語がコマンドに無ければ括弧や区切りと読まず、
  あれば「解析できない」として止める (`foo "$(foo ${x%(*})" && cd A && foo ")" && gh pr create`。引用の走査が内側の括弧を数え違え、
  本物の記号を隠して cd を読み違えるため)。`$'a\'b'` の形は shlex が引用符の対応を読み違えるので、コマンド全体を
  「解析できない」として止める。括弧が閉じないまま括弧の中で読んだ PR 操作も止める
- 引数の置換の中の区切りを cd の区切りと読まない。`cd $(a | b); gh pr create` をパイプの左の cd と読むと、元の cwd を検査してしまう。
  この形の cd の後ろは止める。backtick と `${` の中も同じ (`` cd `a | b`; gh pr create `` / `cd ${X:-a|b}; gh pr create`)
- 空になりうる展開がコマンド語の位置にある形の後ろの PR 操作を止める。`true && ${x} gh pr create` / `$x gh pr create` /
  `"$x" gh pr create` / `$(true) gh pr create` / `` `true` gh pr create `` / `${x:-${y}} gh pr create` / `"$@" gh pr create` /
  `$* gh pr create` を見失い、ゲートを走らせずに通していた
- wrapper にオプションが付いた形 (`env -i gh pr create` / `nohup -- gh pr create` / `command -- gh pr create` / `time -p gh pr create`) と、
  `case` の分岐と同じ行の `gh pr create` も同じく止める
- `pushd` / `popd` は語としてどこかにあれば止める (`if pushd A; then ...` / `command pushd A` / `Y=1 pushd A` / `'pushd' A` / `! pushd A` /
  `time pushd A`)。区切りの直後の素の `pushd` だけを見ていて、hook の cwd が repo の外だと素通しになっていた。
  語が引数でも止める (`echo pushd; gh pr create`)

### Known gaps

- `eval "cd A"`、`. ./env.sh` の中の cd は読まない
- `sudo gh pr create` / `xargs gh pr create` のように、前置きとして読まない語の後ろの PR 操作は数えない
- `$(a)#b` の後ろの cd は、cwd を変えうる語があるときの従来の読み (`#` からコメント) で読み落とすことがある
- コマンド語が展開で cd を書いていない形 (`$c d`、`${c} d`、`$x; gh pr create`、`g=cd; $g A`、`c${x}d A`) は、値で cd になっても
  読まない。うっかりの予防の範囲の外で、`$PY -m pytest` のような普段の形を止める害の方が大きい
- 語の並びで cd と読まない前置き: `noglob cd A`、`nocorrect cd A`、`repeat 1 cd A` (zsh だけ移る)、`eval cd A`。
  閉じ括弧の直後に `fi` を続けた `if false; then { cd A; } fi` の分岐の cd は、走ったものとして読む

### 止めすぎとして残す形

走る cwd は 1 つに決まる、または確かめられるが、読みを足さずに止める。この「止める」は plugin repo でない repo と repo の外でも効く
(検査先が決まらないので、検査先が plugin repo かどうかを確かめられない)。`cd <絶対パス> && gh pr create` の形にすれば通る。

- `if ! cd A; then exit 1; fi; gh pr create` (`cd A || exit 1` の形なら通る)
- `cat x | (cd A && gh pr create)`
- `[ -d A ] && { cd A; gh pr create; }`
- `cd A; make & wait; gh pr create`
- `$(cd .. && pwd)/x.sh && gh pr create` (コマンド語の展開が cd を書いている形と区別しない)
- `$PY x.py cd; gh pr create`、`$PY -m pytest -k cd -q; gh pr create`、`$G --msg 'cd a'; gh pr create` (コマンド語の展開の後ろの引数の cd を、
  移動しうる cd と区別しない)
- `cd A; X="$(foo ")")"; gh pr create` (二重引用符の中の置換の内側の記号と cwd を変えうる語が同じコマンドにある形)。
  `gh pr create` の本文の heredoc の中の記号は、この形に当たらない

## 0.3.4

代入語の値に置換がある形の後ろの PR 操作を素通しにせず止めるようにし、PR 本文の定型
(`--body "$(cat <<'EOF' ... EOF)"`) の本文に奇数個の `"` があっても、cwd を変えうる語が無いコマンドでは止めないようにした
(patch bump)。

値を読んでゲートを走らせる案は入れなかった。shlex が引用を消す以上、語の並びから「確かな cd」を判定する allow-list が完全に
ならず、実際の cwd と違う場所を検査して素通しする形がマージ前レビューのたびに残ったため。

### Fixed

- 代入語 (`NAME=...`) の値に空白や置換がある形の後ろの `gh pr create` / `gh pr ready` を止める。これまでは次の形を、構文解析も
  安全網も見失い、ゲートを走らせずに通していた: 入れ子の置換 (`GH_REPO=$(basename $(pwd)) gh pr create`)、二重引用符の中の
  置換の引用符 (`X="$(echo "a b")" gh pr create`)、`${y:-a b}`、`$((1 + 2))`、配列 (`X=(a b) gh pr create`)、プロセス置換
  (`X=<(a) gh pr create`)、値の直後のリダイレクト (`X=$(a)>f gh pr create`)。backtick の中 (エスケープした入れ子を含む) と
  `function f { ... }` の中も同じ。止める理由は「解析できない」で、値を読んで検査先を決めることはしない
- 二重引用符の中のコマンド置換で本文が始まる heredoc (`--body "$(cat <<'EOF'` + 改行 + 本文 + `EOF` + 改行 + `)"`) の本文を
  空行にしてから構文解析する。本文に奇数個の `"` / `'` があると、これまで「解析できない」で止めていた。コマンドのどこかに
  `cd` / `chdir` / `pushd` / `popd` / `eval` / `source` / `builtin` / `trap` / `HOME=` / `CDPATH=` の文字列 (引用符と `\` を
  外して探す) があるときは、読み飛ばさず従来の読みのまま (判定も従来と同じ)
- `$(a)#b` / `$((1))#b` の `#` を、語の途中なのにコメントの始まりと読み、同じ行の後ろを落としていた。語の途中として読む
  (`(a)#b` の `#` は引き続きコメント)。上の語があるコマンドは従来の読みのまま
- 安全網は、`\X` と `$'...'` を shell と同じ組で読んだ数と、組で読まない数の大きい方を採る (`"$(V=\ y gh pr create)"` は前者、
  `` echo "`V=\"p q\" gh pr create`" `` は後者でしか数えられない)
- `gh pr create --base a >(gh pr create --base b)` のように PR 操作の引数の中に別の PR 操作があると、1 つの呼び出しと読み、
  後ろの `--base` で前の値を上書きしていた。位置を解析できないとして止める (止める側への変更)。二重引用符の中の置換
  (`gh pr create "$(gh pr ready 3)"`) も数えて止める。単一引用符の中の説明 (`--title 'use $(gh pr create)'`) は数えない

### Unchanged

- 上の語があるコマンドで本文に奇数個の `"` / `'` があると、今も止まる。本文の中の語も数える (`source` や `abcd` を含む本文でも
  止まる)。本文に語が無くても `cd a && gh pr create --body ...` の形は止まるので、`cd` は先に別のコマンドで済ませる
- 次の形は本文の範囲が shell で割れるので、これまでと同じ読み方 (本文もコードとして読む) のまま。本文に奇数個の `"` があると
  今も止まる: 終わりの行が無い本文、置換が閉じない形、本文の途中の `)` で置換が閉じ後ろの `"` で引用符も閉じる読み方が
  ある本文 (`1) say "hi`。bash 3.2 は引用符と括弧の対応だけで読み、後ろの行をコードとして走らせる)、引用の無い区切り語で
  本文に `$(` か backtick がある形 (shell が本文の置換を走らせる)
- 安全網は二重引用符の中の置換の本文も数える。本文の行頭に `gh pr create` があると止まる
- コマンドのどこかに `$'` があるときは、二重引用符の中の置換の本文を読み飛ばさない。shlex が `$'\''` の引用符の対応を shell と
  違う形で読み、ずれた読みが通ってしまうことがあるため
- 二重引用符の中の backtick の heredoc (``--body "`cat <<'EOF' ...`"``) は対象外
- bash 3.2 は二重引用符の中の置換の本文に奇数個の `"` / `'` があると構文エラーにする (zsh と新しい bash は走らせる)。
  本文の読み飛ばしは、走らせる側に合わせる

### Known gaps (これまでと同じ穴)

- 値の後ろの cd を読まない: `X=$(a b) cd sub && gh pr create` は元の cwd を検査する。`X=$(cd a) cd b; gh pr create` は a を検査する
- 代入の前置きではない形の値の中の cd (`ROOT=$(cd .. && pwd); gh pr create`)、cwd を変えないかもしれない cd の後ろの素の値
  (`(cd a) && X=1 gh pr create`) は、cd の移動先を検査先として読む
- 別の shell の中の入れ子の値: `bash -c "X=\$(p \$(q)) gh pr create"` は数えない。代入語ではない置換の後ろ (`$(p q) gh pr create`) も数えない
- `X=$(a b) b'as'h <<'EOF'` のように、値の後ろの引用符を挟んだ shell 名に渡す本文の PR 操作は数えない
- 上の語を含まない移動は本文の読み飛ばしを止めない: `. ./env.sh` が中で cd する形、`${c}d <dir>` のように展開で組み立てたコマンド語、
  `setopt AUTO_CD`。これらの後ろで本文に奇数個の引用符があると、cwd のまま検査する (これまでは本文の引用符で止まっていた)

## 0.3.3

cd が何度もあるコマンドで検査する repo を取り違えないようにし、heredoc の本文に奇数個の `'` があっても
止めないようにした (patch bump)。

### Fixed

- `cd plugins/x && make test && cd ../.. && gh pr create` のように cd が何度もあるコマンドを、最後の cd
  (`../..`) だけで解決していた。cwd が repo root だと repo の外と読み、ゲートを走らせずに通していた。
  cd を順に合成する: 相対パスは前の移動先に join し、`/` や `~` で始まるものは置き換える。移動先は shell の
  cd と同じく論理パスで 1 つずつ辿り (`..` は symlink を辿る前の親)、途中の移動先が無ければ止める
- 合成する cd の引数に `$` / backtick を含むもの、`-` (直前の dir) や `-P` などオプションで始まるものは、
  静的に決まらないとして止める (cd が 1 回のときの判定は変えていない。以前も、存在しない移動先か
  静的に解決できない移動先として止めていた)
- サブシェル・コマンド置換の中、パイプの両側、`&` の前、`env` / `nohup` / `exec` の後の cd は、shell の
  cwd を変えないことがある。この cd に相対パスの cd が続くと起点が決まらないので、静的に解決できない
  として止める (どちらかに読むと、shell と違う repo を検査するか、検査せずに通すため)
- heredoc の本文をシェルのコードとして読んでいた。本文の引用符の外に奇数個の `'` があると
  (`cat > b.md <<'EOF'` + `It's done` + `EOF` + `gh pr create --body-file b.md` など)、1 コマンドでも
  「PR 操作の位置を解析できない」で止めていた。構文解析の前に、本文と終わりの区切り語の行を空行にする。
  区切り語は `<<EOF` / `<<'EOF'` / `<<"EOF"` / `<<\EOF` / `<<'END MSG'` / `<<-EOF` (終わりの行の先頭の
  タブを許す) / `<<''` などを扱い、終わりの行は行全体が区切り語と一致する行だけ (前後の空白や `\r` が
  付いた行は本文の続き)。引用の無い区切り語の本文は shell と同じく `\` + 改行で行をつないでから比べる。
  引用符・コメントの中の `<<`、`<<<` (here-string)、`((` / `$((` の中の `<<` (シフト演算子)、`${...}` の中の
  `<<` は heredoc として扱わない。コマンド置換 (`$(...)` / backtick) の中の heredoc は、本文が始まる前に
  置換が閉じると本文を持たない (後ろの行はコマンドとして読む)
- heredoc の本文にある `cd` を移動として読まない
- コマンド語の前にリダイレクトがある形 (`<<'EOF' gh pr create --body-file -`、`2>/dev/null gh pr create`、
  `>/dev/null cd dir`) で、gh / cd を見失っていた。これまでは本文の `'` で止めていたため隠れていたが、
  `2>/dev/null gh pr create` は止めずに通していた。演算子とその対象 (fd 番号を含む) を外してから読む
- 区切りの直後にリダイレクトがある形 (改行や `;` の直後の `>/dev/null gh pr create`、`true&&>f gh pr create`) で
  gh を見失い、止めずに通していた。shlex が隣り合う記号を 1 つにまとめ、リダイレクトが区切りに飲まれていたため。
  `&&>` は `&&` の後ろのリダイレクトとして区切りに数える (これまでは `&>` と読み、区切りと見なしていなかった)
- 安全網 (実行位置にある PR 操作の数の突き合わせ) も、コマンド語の前にリダイレクトがある `gh pr create` を数える。
  構文解析がこれを見つけるようになったので、数えないと別の場所の見落とし (関数定義の中など) と数が合い、
  止めずに通してしまうため (止める側への変更)
- 安全網がコマンド語の前のリダイレクトを読む正規表現は、`>>f` を `>` + `>f` とも読めるなど 1 つのリダイレクトの
  読み方が複数あり、`2>&1 ` や `>>build.log ` を並べたコマンドでは一致しないときの後戻りが個数に対して指数時間に
  なっていた (24 個で 5 秒、30 個前後で hook が時間切れになりコマンドが素通しになる)。gh を含まないコマンドも
  対象だった。1 つのリダイレクトの読み方を 1 通りに固定し、演算子を shell の演算子どおりに読む
  (`2>-` の `-` はファイル名、`<<- x` の x は区切り語)。gh と pr を含まないコマンドは安全網を走らせない
- 同じ正規表現で、リダイレクトの対象・代入の値・gh のパスが `;` `&` `|` `<` `>` をまたいで読まれ、`&>f&>f...` の
  ように実行位置の始まり (`&` など) が並ぶ形では個数の 2 乗の時間になっていた (8000 個で約 10 秒)。対象と値は
  shell と同じく引用の外の `;` `&` `|` `<` `>` で終わる語として読み (`$(...)` / backtick / 引用の中はまたぐ)、
  前置きの繰り返しは 32 個までにする。33 個以上の前置きの後ろに PR 操作があるコマンドは、位置を確かめられない
  として止める (止める側への変更)
- cd を辿るときに、権限の無い dir・長すぎる名前・symlink の循環で例外が出ると (Python 3.11〜3.13)、hook が
  例外で終わりコマンドがそのまま走っていた。止める側に倒す。あわせて hook の入口でも、判定中の想定外の例外を
  「ゲートを完了できない」として止める (warn では止めずに伝える)
- 区切り語に引用がある heredoc (`<<'EOF'` / `<<"EOF"` / `<<\EOF` / `<<'END MSG'` など) の本文に
  `gh pr create` の行があると、安全網が加工前の本文を数えて止めていた。PR の本文で使い方を説明すると当たる。
  この本文は shell が展開しない文字列なので、安全網の数え上げからも外す。ただし、本文を実行しうるコマンド
  (`sh` / `bash` / `zsh` / `ksh` / `dash` / `ash` / `csh` / `tcsh` / `mksh` / `yash` / `fish` / `ssh` / `eval` /
  `source` / 実行位置の `.`) がコマンドのどこかにあるとき、コマンド語が引用符や `\` を挟んだ shell の名前
  (`b'as'h`) か展開で決まる語 (`"$BASH"` / `${SH:-bash}` / backtick) のとき、終わりの行が見つからない本文、
  置換の中で本文が始まり全体を従来の読み方に戻したときは、従来どおり本文も数える
- `csh -c '...'` / `tcsh -c` / `mksh -c` / `yash -c` / `fish -c` の中の PR 操作を見つけていなかった。`bash -c` と同じく
  別の shell の中にあるとして止める
- `cd A || cd B && gh pr create` を `A/B` に合成していた。`||` の右の cd は A に移れたときには走らず、左の cd は
  失敗しても後ろが続くので、どちらも shell の cwd を変えないことがある cd として扱う (続く相対の cd と合わせて
  静的に解決できないとして止める)。`cd A || exit` / `cd A || return` の左は、失敗すると後ろが走らないので A に決まる。
  `cd A || { echo fail >&2; exit 1; }` のように波括弧の中で何かしてから exit / return する形も A に決まる (数えるのは
  波括弧の一番外側に `;` / 改行で並んだ exit / return だけ。`&&` / `||` / パイプの後ろ、`if` などの中のものは数えない)。
  `cd A || exit 1 | tee` のようにパイプの中や `&` の前に置いた exit、`cd A || { ...; exit 1; } | tee log` /
  `... } &` のように閉じ括弧の後ろがパイプか `&` の波括弧の中の exit は shell を終わらせないので数えない
- shell の cwd を変えないことがある cd (`||` の右・サブシェルの中など) が絶対パスでも、前の cd の移動先を
  置き換えていた (`cd A || cd /abs && gh pr create` で /abs だけを検査していた)。前に cd があるときは、静的に
  解決できないとして止める

### Unchanged

- 安全網 (実行位置にある PR 操作の数の突き合わせ・`bash -c` の中の検出・pushd / popd の検出) は加工前の
  文字列を見続ける。引用の無い区切り語の本文のコマンド置換 (`$(gh pr create)`) は shell が実行するので
  止める。終わりの行が無い heredoc の後の行も、本文として読み飛ばしたうえで安全網が数えて止める
- pushd / popd は今も追跡せずに止める (popd の行き先はコマンドの外の dir stack で決まるため)
- 二重引用符の中のコマンド置換 (`--body "$(cat <<'EOF' ... EOF)"`) の heredoc は今も本文を引用符の中の
  文字列として読む。本文に奇数個の `"` があると「解析できない」で止まる
- 引用符の外のコマンド置換の中で本文が始まる heredoc (`x=$(cat <<'EOF'` + 改行 + 本文 ...) があるコマンドは、
  全体を 0.3.2 と同じ読み方 (heredoc を読み飛ばさない) で読む。本文がどこで終わるかが bash (閉じ括弧で切る)
  と zsh (区切り語の行まで読む) で割れるため。本文に奇数個の `'` があると今も止まる
- `bash -c` の中身と pushd / popd の検出は、引用のある本文の中のものも加工前の文字列で見る (止める側)
- `mkdir -p build && cd build && ... && cd .. && gh pr create` は、途中の移動先 (`build`) がまだ無いので止まる。
  `;` で区切ると shell は cd に失敗したまま後ろを走らせるため、途中の確認を外すかは別途判断する
- `\(` / `\)` (エスケープした括弧) を構文解析が括弧として数え、サブシェルの深さがずれて止めすぎることがある。
  `$[1<<2]` (古い算術の書き方) の `<<` を heredoc と読む。どちらも止める側の誤りで、今回は直していない
- 区切り語に引用がある本文をファイルに書いてから実行する形 (`cat > x.sh <<'EOF'` ... `EOF` の後に `./x.sh`) と
  `make -f - <<'EOF'` は、スクリプトファイル経由の実行として対象外のまま。以前は本文の読み違いで偶然検査して
  いたが、今は本文を数えないので検査しない
- 本文の外のどこかに shell の名前 (`bash` / `sh` / `ssh` / `source` / `eval` など) があるコマンドでは、区切り語に
  引用がある heredoc の本文も数える (引用符の中の語でも。`--title 'source map を直す' --body-file - <<'EOF'` の
  本文の行頭に `gh pr create` があると止まる)。引用符の中の語を除こうとすると、コメントや引用の無い本文にある
  奇数個の `'` / `"` で引用符の対応を取り違え、本文を読む shell (`bash -s 'a b' <<'EOF'`) を見失うため
- 要件は Python 3.11+ のまま。0.3.2 と同じく 3.9 (macOS 標準の python3) でも読み込みで落ちないよう、安全網の正規表現に 3.11 からの構文 (atomic group) を使わない (3.9 で動くことは保証しない)

## 0.3.2

行末コメントの次の行の `gh pr create` / `gh pr ready` を見失わないようにした (patch bump)。

### Fixed

- `git status # check` の次の行に `gh pr create --fill` を置くと、2 行が 1 つにつながって PR 操作が見つからず、
  「PR 操作の位置を解析できない」という事実と合わない理由で止めていた。shlex の既定のコメント処理は、
  単語の途中の `#` (`feature#1`、`${x##*/}`) でもコメントと読み、読み捨てるときに改行まで消費するため。
  shlex にはコメントを扱わせず、shell と同じ規則 (単語の先頭の `#` から改行の手前まで。引用符の中・
  単語の途中・エスケープした `#` は落とさない。改行は残す) で自前で落とすようにした
- `\` + 改行の継続行を shell と同じくつなぐ。これまで `gh pr \` + 改行 + `create` は PR 操作として
  見つからず、止められずに通っていた (`gh pr create` が検査の対象になる側への変更)。
  本体の前処理だけでなく安全網 (引用符が閉じていないときの文字列一致、実行位置の数の突き合わせ、
  `bash -c` の中の検出) も同じ継続行の結合を通すので、`'` が奇数個ある heredoc の後の
  `gh pr \` + 改行 + `create` も検出され、`gh pr ready \` + 改行 + `--undo` は止まらない
- エスケープした空白 (`--title a\ #b`) の直後の `#` を、コメントと誤読して落とさない
- `--base feature#1` のように、引数の途中の `#` 以降が落ちて値が欠けることがなくなった

### Known limitation

- heredoc の本文は今もシェルのコードとして読むので、本文の引用符の外に奇数個の `'` があると
  「解析できない」で止まる。以前は、本文の単語の途中の `#` (`C# isn't great` など) を
  コメントと誤読して、`'` が偶然消えて通る形があった

## 0.3.1

テストの整理 (挙動の変更なし。patch bump)。

### Changed

- `test_hermetic_env.py` の陽性対照 (止める設定が無い commit で maintenance の起動が trace に見えることを確かめる
  テスト) が起動する git から、外側の repo を指す変数 (`GIT_DIR` / `GIT_WORK_TREE` など 10 個) を外した。
  床 (`isolate_git_config`) がこれらを外し損ねる退行があると、git の hook の中から suite を流したときに
  外側の repo へ commit が書き込まれていた。床の外し損ねは `TestTheIsolatedEnvStopsNothing` が別に検出するので、
  ここで外しても検出は消えない
- 回帰テスト `test_the_positive_control_does_not_write_to_an_outer_repo` を追加: 床の後で本物の別 repo を指す
  `GIT_DIR` / `GIT_WORK_TREE` を足して陽性対照を流し、別 repo の commit 数が不変であることを確かめる。
  外側の repo の refs と objects のどちらに書いても落ちる

## 0.3.0

`claude plugin validate` の plugin directory checks を FAIL に数えないようにした (minor bump)。

### Changed

- `validate[<plugin>]` は、validate の出力のうち「Plugin directory checks for ...」から「These plugin directory
  checks do not change the result below.」までの節を切り分け、その中の warning と提出を拒まれる問題 (`✘`) を
  WARN で表示するだけにした。`strict_validate` / `--strict-validate` でも FAIL にしない。この節は公式の
  プラグインディレクトリ (カタログ) に申請するとき向けの検査で、CLI 自身が結果を変えないと明記している。
  CLI がこの区分に検査を足すたびに、カタログに申請しない plugin まで FAIL していた (2.1.306 の `ICON_MISSING` など)
  節の中が `✘` だけのときは、これまで PASS (無出力) だったが WARN で表示するようになる
- 見出しの後に締めの行が無い節 (何も見つからなかった節) は、次の見出しで探索を打ち切る。後ろの節の締めの行まで
  巻き込んで、間にある manifest の warning を advisory に入れてしまわないようにするため
- manifest の warning (version / description / author など) は従来どおり数える (`strict_validate` で FAIL)
- 節の見出しか締めの行が見つからない出力 (文言が変わった・節を出さない旧 CLI) では節として扱わず、従来どおり
  すべての warning を数える。黙って全部が advisory に倒れないようにするため

## 0.2.4

icon と導入手順の追加 (挙動の変更なし、patch bump)。

- plugin の icon (`assets/icon.png`) を足した。`claude plugin validate` の plugin directory checks が出す `ICON_MISSING` を解消する
- README に marketplace からのインストール手順の節を足した

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
