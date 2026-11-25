# Changelog

## 0.10.3

**Windows で、hook の入力の作業ディレクトリ (`cwd`) が Git Bash の綴り (`/c/Users/...`) で届くと、guard 全体が素通しになっていたのを
直した (patch bump)。** あわせて、テストの harness の 2 つの前提 (共有の repo に書かない・worktree を変えたら `forget_family()`) を、
破ったテストを FAIL にする検査にした (テスト整理。挙動の変更なし)。

### 修正: hook の入力の cwd も、Git Bash の読みとの和で読む

- 0.10.2 までの `family.detect` は、cwd を Windows の読みだけで読んでいた。`/c/Users/<u>/repo/.claude/worktrees/a` は今のドライブの
  `\c\Users\...` と読まれて存在せず、linked worktree の外とみなして何も出さなかった (Bash の git も Write / Edit も止めない)。
  0.9.2 と 0.10.2 で Bash の行き先・Write / Edit のパス・`WORKTREE_CWD_GUARD_ALLOW` は 2 つの読みの和で読むようにしたが、cwd だけが残っていた
- `family.detect_readings` が、cwd を Windows の読みと Git Bash の読み (`C:\Users\...`) の両方で試し、linked worktree と分かった読みごとに
  (その読みの cwd, Family) を返す。hook は読みごとに判定し、どちらかで別の checkout なら止める。相対パス (`../../x.py`・
  `cd ../.. && git reset --hard`) は、Family を見つけた読みの cwd から解決する (入力の `/c/...` を Windows の読みのまま使うと、
  自分の worktree の外を指して止めない)
- 2 つの読みが別々の linked worktree に当たるときは、両方の読みで判定する (止めすぎの側)。見出しには両方の worktree を ` / ` で並べ、
  同じ行き先の理由は 1 回だけ並べる。読み取りの期限 (3 秒) は、読みが 2 つでも全体で 1 つ
- `family.detect` は、読みの順 (Windows の読みが先) で最初に linked worktree と分かった Family を返す (hook は `detect_readings` を使う)
- Windows 以外と、cwd が `C:\...` の形で届いたときは、読みは 1 つで、判定も文面も変わらない。読み替える綴りは Bash の行き先と同じ
  (`/c/x` と `/c` だけ)。読み替えの関数 (`drive_spellings` ほか) は、family.py から使うため command.py から family.py に移した (中身は同じ)
- Claude Code の挙動: 公式 docs の hooks の入力の節には cwd の形の記述が無く、Windows の実機でも確かめていない。`C:\...` の形で届くなら、
  この修正は判定を変えない。README の「目的と限界」に書いた

### 比較

0.10.2 と 0.10.3 の `evaluate` に同じ入力を流した。入力は cwd 18 形 (`C:\...` / `c:\...` / `C:/...` / `/c/...` / `/C/...` / 末尾 `/` /
subdirectory / `/c/../...` / `/cc/...` / `/d/...` / `\c\...` / main / 別の worktree / repo の外) × (Write / Edit / MultiEdit / NotebookEdit のパス
15 形 + Bash の git 16 形) × 許可リスト 5 形 × MODE (enforce / warn)。Windows の読みは `os.path` を `ntpath` に差し替え、今のドライブを `C:` とし、
detect は「checkout の一覧を固定した 1 つの読みの detect」にした (0.10.2 と 0.10.3 で同じもの)。POSIX は本物の git の repo で流した。

| 世界 | 件数 | 停止 → 非停止 | 通過 → 停止 | 通過 → 注意 | 判定は同じで文面が変わる |
|---|---|---|---|---|---|
| Windows | 13,680 | 0 | 517 | 567 | 0 |
| Windows (`C:\c\Users\...` にも linked worktree がある) | 13,680 | 0 | 451 | 461 | 448 |
| POSIX (本物の git。cwd に symlink・大文字小文字だけ違う綴り・`/c/...` を含む) | 5,664 | 0 | 0 | 0 | 0 |

- 変わったのはすべて、linked worktree の中を `/c/...` / `/C/...` の綴りで書いた cwd。Windows の世界では、その cwd の 0.10.3 の出力が、
  同じ場所を `C:\...` で書いた cwd の 0.10.2 の出力と全件一致した
- `C:\c\Users\...` にも linked worktree がある世界では、`/c/...` の cwd の判定が「Windows の読み (0.10.2 の判定) と Git Bash の読みの和」に
  なることを全件で確かめた。文面だけが変わったものは、見出しに両方の worktree を並べ、両方の読みの理由を並べたもの

### テスト整理 (挙動の変更なし): harness の前提を、破ったテストを FAIL にする検査にする

- 0.10.1 で `GuardBase` (`tests/test_guard.py`) に入れた 2 つの速くする仕組みの前提は、コメントにしか無かった。前提を破るテストを
  作っても suite は green のまま通った (マージ前レビューで作った陽性対照 5 つが、すべて OK で通った)
- 共有の側 (`SHARE_REPO = True` のクラス): テストの前後で、クラスの一時 dir の中 (repo と、その兄弟。symlink は辿らない) を比べ、
  変わっていたらそのテストを FAIL にする (`tree_state`)。比べるのはファイルの中身 (hash)・symlink の行き先・ディレクトリの有無で、
  mtime は比べない (中身の変わらない書き直しは、同じクラスの他のテストに漏れない)。suite を 3.14 / 3.11 で 3 回ずつ流して誤検出は 0
  (detect の git の読み取りで、`.git` の中身は変わらなかった)
- 覚えの側: detect の結果を覚えるときに、結果を決めるファイルの形 (`family_fingerprint`: cwd があるか、cwd とその上で最初に見つかる
  `.git`、`<common>/worktrees` の一覧と各 `gitdir` の中身、各 checkout の `.git` の symlink の行き先・種類・中身) を一緒に覚え、覚えた値に
  当てるたびに比べる。変わっていたら「worktree を変えたら forget_family() を呼ぶ」で FAIL にする (detect し直さずに止めるのは、
  前提を明示のまま保つため)。`<common>/worktrees` の mtime は比べない (一覧と `gitdir` の中身で、足す・消す・動かすが分かる)
- `tests/test_harness.py`: 前提を破るテストを `GuardBase` の子クラスとして実際に走らせ、ERROR ではなく FAIL になることを固定する。
  共有の側は、repo に書く・同じ大きさで書き直す・消す・git で branch を作る・repo の兄弟にファイル / ディレクトリ / symlink を作る。
  覚えの側は、worktree の add / remove / move、`gitdir` の書き換え、`.git` ファイルの書き換え・symlink への置き換え、main の `.git` が
  symlink の配置での add、cwd の上に repo を作る、cwd を消す。前提を守るテスト (読むだけ・中身の同じ書き直し・`forget_family()` を
  呼んだ add・worktree の中の書き込み / commit / branch の切り替え / `worktree lock`) は通ることも固定する
- 検査の費用: suite 全体 (`test_harness.py` を除く 260 件) で、共有の側の比較が 192 回・計 0.33 秒 (1 回 1.7 ms)、覚えの側の比較が
  1,648 回・計 0.30 秒 (1 回 0.18 ms。`evaluate` を 258 回呼ぶテストで約 46 ms)。suite の実行時間は 0.10.2 の 42〜48 秒に対し 45〜49 秒
  (足したテスト 25 件を含む。`test_harness.py` は約 3 秒)

### テスト

- `NtpathEmulationTest`: `/c/...` の cwd を linked worktree と読み (`/cc/...`・別のドライブ・main・`\c\...` は読まない)、`C:\...` の cwd と
  同じ判定と文面で止めること (相対パスを含む)、cwd の 2 つの読みが別々の linked worktree に当たるときは両方で判定すること
  (片方の行き先を許可リストで許しても、もう片方で止める)、同じ worktree に当たるときは同じ理由を 2 回並べないこと、
  読み取りの期限が 1 つであること

テスト件数: 255 → 280 (追加 25・削除 0・統合 0)。既存のテストの assert は変えていない (`GuardBase` などが差し替える関数の名前を
`detect` から `detect_readings` に変え、`command.drive_spellings` の参照を `family.drive_spellings` にした)。

## 0.10.2

**Windows で、Write / Edit のパスと `WORKTREE_CWD_GUARD_ALLOW` に書いた Git Bash の綴り (`/c/Users/...`) を、Bash の行き先と同じく
Git Bash の読み (`C:\Users\...`) でも読むようにした (patch bump)。** あわせて、0.9.2 の Git Bash の読みの前提を Windows の CI で測るテストを足した。
0.10.1 までは、Bash の行き先だけを 2 つの読みの和で判定し、Write / Edit のパスと許可リストは Windows の読み (今のドライブの `\c\Users\...`) だけで読んでいた。

### 修正: Write / Edit のパスと許可リストも、Git Bash の読みとの和で読む

- Write / Edit / MultiEdit / NotebookEdit のパスが `/c/Users/<u>/repo/x.py` の綴りのまま届くと、`\c\Users\...` と読み、別の checkout
  (`C:\Users\<u>\repo`) への書き込みでも止めなかった。Bash の行き先と同じく、Windows の読みと Git Bash の読みの両方で判定し、
  どちらかで別の checkout なら止める
- 許可リストに `/c/Users/<u>/repo` と書くと、`\c\Users\...` と読み、`git -C /c/Users/<u>/repo ...` の deny の行き先 (`C:\Users\<u>\repo`) と
  一致しなかった (コマンドに書いた `/c/...` の綴りや Git Bash の `pwd` の出力をそのまま許可リストに足しても止まったまま。
  deny の文面に出る `c:\users\...` の綴りなら 0.10.1 でも一致する)。許可リストの各 root も 2 つの読みの両方で読む。
  Windows の読みも残すので、これまで許していた root は許したまま
- Claude Code の挙動: 公式 docs (hooks の PreToolUse の入力) は、Write / Edit / Read の `file_path` は常に絶対パスで、Claude Code が
  `~` と相対パスを hook の前に展開し、Windows では Git Bash で動く hook にも `\` 区切りで渡す (例は `C:\\project\\src\\index.ts`) と書いている。
  docs のとおりなら `/c/...` の綴りのまま届くことはなく、この修正は効かない (その値は書き込む先と同じものとして判定する)。
  Claude Code が `/c/x` を `C:\x` と今のドライブの `\c\x` のどちらに展開するかは docs に無く、Windows の実機でも確かめていない。
  docs と実装がずれて `/c/...` のまま届いた場合に、Bash の行き先と同じ読みで止める側に倒した
- Bash の行き先と同じ読み替え (`command.drive_spellings`。中身は 0.9.2 の `_msys_drive`) を使うので、読み替える綴りも同じ
  (`/c/x` と `/c` だけ。`/c\x`・`/cygdrive/c/x`・`/cc/x` は読み替えない)。Windows 以外では読み替えない。
  README の「目的と限界」・「止める操作」・「設定」と AGENTS.md の指摘しないものに書いた

### 比較

0.10.1 と 0.10.2 の `evaluate` に同じ入力を流した (git は起動せず、checkout の一覧を固定した)。入力は、Write / Edit / MultiEdit / NotebookEdit
のパス (checkout ごとの綴り 16 形 / 9 形と、その下のファイル、相対パス) と Bash の git (`git -C` / `cd` / `GIT_DIR` × 同じ綴り) に、
許可リストの値 (なし / 綴りの変種 / `;` で並べた形) を掛けたもの。Windows の読みは `os.path` を `ntpath` に差し替え、今のドライブを `C:` とした。

| 世界 | 件数 | 通過 → 停止 | 停止 → 通過 | 形 |
|---|---|---|---|---|
| Windows | 39,372 | 2,916 | 702 | 通過 → 停止はすべて、`/c/...` / `/C/...` の綴りのパスで別の checkout を指す Write / Edit (許可リストなしでも停止になるもの)。停止 → 通過はすべて、許可リストの `/c/...` / `/C/...` の綴りの Git Bash の読みが、止めていた checkout と一致したもの |
| Windows (`C:\c\proj` などに checkout がある) | 92,628 | 10,548 | 1,202 | 上と同じ |
| POSIX (`/c/Users/...` に checkout がある) | 12,390 | 0 | 0 | |
| POSIX (`/c/...` は checkout の外) | 13,497 | 0 | 0 | |

docs の形 (`C:\...`) のパスと、読み替えない綴り (`/cc/x` / `//c/x` / `/c\x` / `/cygdrive/c/x`) は、判定も文面も変わらない。
別のドライブの `/d/...` は `D:\...` と読むので、C: の checkout は指さない (比較では D: に checkout を置いていない)。
2 つの読みの綴りが違っても同じ行き先になる形 (`/c/../Users/...` のようにドライブの root より上に出る `..`) は、同じ理由を 1 回だけ並べる
(比較の入力にこの形は無い)。

### テストの追加: Git Bash が `/c/x` を `C:\x` と読む前提を Windows の CI で測る

- 0.9.2 で Bash の行き先を Git Bash の読みでも判定するようにしたのは、「Git Bash は `cd /c/x`・`git -C /c/x`・`--git-dir=/c/x`・`GIT_DIR=/c/x` を
  `C:\x` と読む」という前提に依る。この前提は実機で測っていなかった
- `GitBashDrivePathTest` (`tests/test_git_dir_targets.py`): Windows で Git for Windows の bash.exe (`which bash` は WSL の bash に当たることが
  あるので、git の場所から探す) があるときだけ走る。guard の判定は通さず、本物の Git Bash と git の解決結果 (`rev-parse --show-toplevel` /
  `--absolute-git-dir`、`pwd -W`) が main と一致することだけを見る。比べる main は git の出力の側に揃える (runner の一時 dir が 8.3 の短縮名に
  なりうるため)。Windows 以外では skip する
- このテストが Windows の CI で落ちたら、前提が外れている。和で判定しているので、外れていても見逃しは増えず、止めすぎになる。
  そのときは 0.9.2 の Git Bash の読みを見直す

### テスト

- `NtpathEmulationTest`: Write / Edit / MultiEdit / NotebookEdit の `/c/...` のパスを止め、自分の worktree・`/cc/x`・別のドライブ・repo の外は
  止めないこと、Windows の読みも残すこと、docs の形のパスと、綴りの違う 2 つの読みが同じ行き先になるパスで、同じ理由を 2 回並べないこと。
  2 つの読みが別々の checkout を指すとき、片方を許可リストで許しても、もう片方で止めること (両方を許したら通すこと)。
  許可リストの `/c/...` の綴りで止めていた checkout を許し、許していない checkout は止めたままのこと、許可リストの Windows の読みも残すこと
- `GitBashDrivePathTest` は、Git Bash の既定の読みを測るため、起動する bash の env から MSYS のパス変換を止める変数
  (`MSYS_NO_PATHCONV` / `MSYS2_ARG_CONV_EXCL` / `MSYS2_ENV_CONV_EXCL`) を外す
- `PosixDrivePathTest`: Windows 以外では、Write / Edit のパスと許可リストの `/c/x` を読み替えないこと

テスト件数: 246 → 255 (追加 9・削除 0・統合 0)。

## 0.10.1

**テスト整理 (挙動の変更なし)。suite の実行時間を約 85 秒から約 38 秒にし、worktree の下の subdirectory を作業ディレクトリにしたテストを
足した (patch bump)。** テストは消してもまとめてもいない (件数は足した 1 件だけ増えて 246 件)。

### テスト整理 (挙動の変更なし): detect の結果を覚え、repo に何も書かないクラスで repo を共有する

- 時間の半分以上は、製品の `family.detect` (`evaluate` を呼ぶたびに `git rev-parse` と `git worktree list` を起動する) が、同じテストの
  同じ作業ディレクトリで git を繰り返し起動していたこと (1 件の中で `evaluate` を 258 回呼ぶテストもある)。残りの多くはテストごとの
  repo の作成 (`make_repo`) だった
- `GuardBase` (`tests/test_guard.py`) は、テストの間、作業ディレクトリごとに 1 回だけ本物の detect を走らせて覚え、`evaluate` に渡す
  (entry module の `detect` を差し替える)。`family.detect` を直接呼ぶテストと、hook を subprocess で起動するテストは、今までどおり本物の
  detect を通る。テストの途中で worktree を足す・消す・`.git` ファイルを書き換えた後に同じ作業ディレクトリで読むときは、`forget_family()`
  で覚えを捨てる
- repo とその周りに何も書かない 18 クラス (96 件) は、`SHARE_REPO = True` で、クラスの全テストが 1 つの repo と detect の覚えを共有する
- 確かめたこと: 覚えた値を返しながら毎回本物の detect も呼んで比べる版で suite を流し、1,648 回の呼び出しで不一致は 0 件。共有したクラスの
  各テストの前後で repo の木 (種類・大きさ・mtime・中身) が変わったものも 0 件。どちらの検査も、わざと壊した版 (worktree を足した後に覚えを
  捨てない / symlink を作るクラスを共有にする) では検出した。製品コードの変異 14 件で、落ちるテストの集合が書き換え前と一致した (足したテストの
  分を除く)

### テストの追加: worktree の下の subdirectory を作業ディレクトリにする

- worktree の下の subdirectory を作業ディレクトリにしたテストが無く、この session の worktree (`Family.home`) を `--show-toplevel` ではなく
  作業ディレクトリから取る退行を検出できなかった。この退行のもとでは、subdirectory で作業する agent が subdirectory の外の自分の worktree
  (root の `README.md`、`cd ../..` の後の git) に書くのを、別の checkout への書き込みとして止める
- `GuardTest.test_subdirectory_of_own_worktree_is_still_home`: subdirectory から自分の worktree への Write と git の書き込みを止めないこと、
  別の checkout への書き込みは subdirectory からでも止めることを見る

## 0.10.0

**相対パスの移動を続けた形などで hook が時間切れになる (コマンドがそのまま走る) 経路を読み取りの期限で閉じ、`git worktree remove|move` の対象の語が
変数の形に注意を出すようにした (minor bump)。**
`cd a;` を 2,000 回並べると約 6 秒、`x || cd d<i>;` を 6 回並べた後に `cd a;` を 400 回並べると約 17 秒かかっていた (0.9.3 まで。hook の timeout は 10 秒)。
`git worktree remove --force "$Z"` は、どの worktree を消すか静的に決まらないのに何も出さずに通っていた (ほかの書き込みの `git -C "$Z" reset --hard` は注意を出す)。

### 修正: 読み取りに期限 (3 秒) を付けた

- 遅くなる根は 2 つある。1 つは相対パスの解決で、移動のたびに realpath でパスの全要素を辿り直す (要素ごとに lstat する) ので、相対パスの移動を
  続けると、パスの深さの 2 乗の時間になる。状態の数 (`x || cd d<i>;` で割れた行き先) と、閉じない here-document の安全網の読み直しの回数も係数に
  掛かる。もう 1 つは状態の数 × pushd のスタックの深さ (`x || cd d<i>;` 6 回の後に `pushd /t/p<i>;` を 4,000 回で約 7 秒)。形ごとに閉じても
  同じ根の別の経路が残るので、時間切れの経路そのものを閉じる
- 相対パスの解決は 0.9.3 と同じ式のままで、値は常に 0.9.3 と同じ。深い相対パスの移動は期限で注意になる。正規化済みの基点から移動した分の要素だけを
  辿る速い解決は入れなかった: 値を合わせる先の realpath の内部の振る舞い (Python 3.12 以前は symlink の環で辿るのをやめ、3.13 以降は 1 回の呼び出しの
  中で symlink の解決を覚える) に依存し、マージ前レビューで 2 回続けて、0.9.3 が止めていた git を通す形 (symlink の環を `..` で横切った後の移動) が
  見つかったため
- 期限は 1 つのコマンドの読み取り全体 (zsh と bash の読み、閉じない here-document の安全網の読み直しを含む) で 1 つで、最初の読みを字句に分け終えた
  ところから数える。字句ごとと、1 つのコマンドを状態ごとに読むところ (移動と git) で見て、過ぎたら残りを読まずに、それまでの判定に注意
  (「コマンドの読み取りが期限 (3 秒) を超えたため、途中から後ろの git を確かめていない」) を 1 つ足して返す。それまでに見つけた停止は残し、
  注意で止めることはない。安全網の読み直しは、期限を過ぎたら字句に分けることからやめる
- 状態ごとにも見るのは、区切りの字句が 1 つの単純コマンドを状態の数だけまとめて読むため。字句の間でしか見ないと、`x || cd d<i>;` 6 回の後の
  1 つの長い相対パスの語 (`cd a/a/.../a` の 4 万要素、78 KB) と、語の多い 1 つの git (`git -C /t/a -C /t/a ...` の 64,000 個、500 KB) は、どちらも
  約 9 秒かかり、期限を大きく越えていた (マージ前レビューの指摘)。どちらも約 3.1〜3.3 秒で注意になる
- 期限を見ない処理: 字句に分ける処理 (正規化と shlex。shlex は 1 語の長さの 2 乗の時間で読む) と、1 つの状態の 1 回の読み。1 つの相対パスの語の解決は
  要素数の 2 乗 (パスの複製) で、10 万要素 (195 KB) で約 0.7〜0.8 秒。1 つの git の `-C <相対パス>` を続けた形は、1 つの状態の読みの中で語の数の 2 乗になる
  (`-C` のたびに、深くなった場所から全要素を辿り直す)。期限を過ぎた後もその状態の読みを終えるまで待つので、`git -C a` を 2,000 個並べた 1 つの git (10 KB) は
  hook 全体で約 4.3〜5.3 秒、4,000 個 (20 KB) で約 8.6〜10 秒かかり、Python 3.11 では hook が時間切れになる (0.9.3 は読みごとに読むので 2,000 個で約 7〜8 秒、
  3,000 個で時間切れ)。最初の読みを字句に分ける時間は期限に入れないので、引用や空白の無い長い 1 語を含む形も、0.9.3 と同じく読み終えて手前の git を
  止める (先頭に `git -C <main> reset --hard` を置いた 768 KB の 1 語で約 4.4 秒、1 MB で約 8 秒。1.1 MB ほどを超えると、0.9.3 と同じく hook が時間切れになる)。終わらない here-document が残った形では、
  安全網の旧来の読みの字句分解も、期限の手前で始めれば最後まで走る (768 KB の文字列で約 8〜9 秒。0.9.3 は約 13 秒で時間切れ)。普段の形ではないので、
  この版では扱わない
- 判定が変わる形: 0.9.3 で読むのに 3 秒を超え、hook の timeout (10 秒) までに止めていた形のうち、止める理由の git を読む位置が期限より後ろにあるもの。
  最初の zsh の読みの中で期限を過ぎる深さの相対パスの移動 (`cd a;` を 2,300 回ほど続けた後の main への git)、bash だけが移動する `command cd <main>` を
  重い前半の後に置いた形 (zsh の読みが前半を読み終えた後、bash の読みの途中で期限に当たる)、安全網の読み直しだけが読む git (重い前半を読み直しの回数だけ
  読んだ後に期限に当たるので、もっと短い入力で変わる) がこれに当たる (下の表の「注意」の行)。どの読みでも止まる形は、最初の zsh の読みが期限の手前で
  読み終えれば止めたままで、0.9.3 で 10 秒を超えていた形 (実際には時間切れで素通し) は注意になる。同じ形でも、Python の版や機械の速さで読む時間が
  変わるので、境目は動く (Python 3.11 は 3.14 より 1.2〜2 倍ほど遅い)。実コマンドと下の比較の生成した入力で、期限に当たったものは 0 件。README の
  「目的と限界」と AGENTS.md の指摘しないものに書いた

macOS、Python 3.14 と 3.11 で測った (`command.analyze`。Windows は測っていない):

| 形 (最後は `cd <main>; git reset --hard`) | 大きさ | 3.14: 0.9.3 | 3.14: 0.10.0 | 3.11: 0.9.3 | 3.11: 0.10.0 |
|---|---|---|---|---|---|
| `cd a;` 1,000 回 | 6.0 KB | 停止 (2.1 秒) | 停止 (2.1 秒) | 停止 (2.5 秒) | 停止 (2.4 秒) |
| `cd a;` 2,000 回 | 11.8 KB | 停止 (7.0 秒) | 停止 (3.0 秒。bash の読みを期限で打ち切り、注意も出す) | 停止 (8.2 秒) | 停止か注意 (3.0 秒。zsh の読みを期限の手前で読み終えるかどうかの境目) |
| `cd a;` 2,300 回 | 13.6 KB | 停止 (9.2 秒) | 注意 (3.0 秒) | 停止 (10.9 秒。実際は時間切れで素通し) | 注意 (3.0 秒) |
| `cd a;` 1,500 回で、最後の移動が bash だけ移動する `command cd <main>` | 8.9 KB | 停止 (4.0 秒) | 停止 (3.0 秒。注意も出す) | 停止 (4.9 秒) | 注意 (3.0 秒) |
| 同 2,000 回 | 11.8 KB | 停止 (6.9 秒) | 注意 (3.0 秒) | 停止 (8.3 秒) | 注意 (3.0 秒) |
| `x \|\| cd d<i>;` 6 回 + `cd a;` 400 回 | 2.5 KB | 停止 (27 秒。実際は時間切れで素通し) | 注意 (3.0 秒) | 停止 (31 秒。同) | 注意 (3.0 秒) |
| `x \|\| cd d<i>;` 6 回 + `cd a;` 100 回 + 閉じない here-document 9 個 | 0.8 KB | 停止 (19 秒。同) | 停止 (3.0 秒。注意も出す) | 停止 (19 秒。同) | 停止 (3.0 秒。注意も出す) |
| `x \|\| cd d<i>;` 6 回 + `pushd /t/p<i>;` 2,500 回 | 38.1 KB | 停止 (3.5 秒) | 停止 (3.0 秒。注意も出す) | 停止 (6.2 秒) | 停止 (3.0 秒。注意も出す) |
| 同 3,500 回 | 53.8 KB | 停止 (6.3 秒) | 停止 (3.0 秒。注意も出す) | 停止 (12.5 秒。実際は時間切れで素通し) | 注意 (3.0 秒) |
| 同 5,000 回 | 77.2 KB | 停止 (15 秒。実際は時間切れで素通し) | 注意 (3.0 秒) | 停止 (24 秒。同) | 注意 (3.0 秒) |
| 同 3,500 回で、最後の移動が bash だけ移動する `command cd <main>` | 53.8 KB | 停止 (7.2 秒) | 注意 (3.0 秒) | 停止 (11.0 秒。実際は時間切れで素通し) | 注意 (3.0 秒) |
| `x \|\| cd d<i>;` 6 回 + `pushd /t/p<i>;` 450 回 + 終わらない here-document 7 個の後ろの git (7 回目の読み直しだけが読む) | 6.7 KB | 停止 (3.0 秒) | 停止 (3.0 秒。注意も出す) | 停止 (4.1 秒) | 注意 (3.0 秒) |
| 同 600 回 | 8.9 KB | 停止 (4.3 秒) | 注意 (3.0 秒) | 停止 (6.0 秒) | 注意 (3.0 秒) |
| `x \|\| cd d<i>;` 6 回 + `cd a/a/.../a` (2 万要素の 1 語) | 39.2 KB | 停止 (7.5 秒) | 停止 (3.1 秒。注意も出す) | 停止 (10.0 秒。時間切れの境目) | 注意 (3.1 秒) |

### 修正: `git worktree remove|move` の対象の語が変数の形に注意を出す

- `git worktree remove --force "$Z"` / `"../$Z"` / `git worktree move "$Z" <dest>` は、どの worktree の名前にもパスにもなりうるのに、今の場所が決まっていても
  決まっていなくても何も出さなかった (0.9.3 で、今の場所が決まらないときの名前と相対パスには注意を出すようにしたが、変数の語は外していた)。
  README の「行き先が変数などで静的に決まらない操作は注意を出す」と食い違っていた
- git が読む repo がこの repo のときと、repo が決まらないとき (行き先の決まらない `cd "$Y"` / `git -C "$Y"` の後ろや、状態の上限を超えた後) は、注意を出す (止めない)。
  他の repo の中と repo の外では、この repo の worktree に届かないので何も出さない。自分の worktree を変数の先へ動かす `git worktree move a "$Z"`、
  `git worktree add "$Z"` / `list` / `prune` / `lock` も何も出さない。止める形 (別の worktree の名前や絶対パス) は止めたままで、注意は足さない
- 内部バックログで挙がっていた再現 (`cd "$Y"; git worktree remove --force b`) は 0.9.3 で注意になっていた。回帰テストで固定した

### 比較

0.9.3 と 0.10.0 の読み (`command.analyze`) に同じ入力を流した (macOS、本物の main + worktree 2 つ。自分の worktree の中に main への symlink、
main の中に自分の worktree と別の worktree への symlink を置いた)。Python 3.14 と Python 3.11 で流し、どちらも下の表と同じ数になった。

| 入力 | 件数 | 判定か文面が変わったもの | 停止 → 停止でないもの | 消えた停止・注意 |
|---|---|---|---|---|
| git を含む実コマンド | 12,725 | 67 (通過 → 注意 63、注意 → 注意 (注意が 1 つ加わった) 4) | 0 | 0 |
| 生成: 移動の語彙 15 形 × 行き先 16 形 (相対パスの変種と symlink を通る形を含む) × 分岐の形 11 形 × 回数 1〜2 × 前置き 6 形 × 後ろの git 18 形 (200,000 件を抜き出し) と、回数 3・6・7 (15,000 件) | 215,000 | 35,540 (すべて通過 → 注意) | 0 | 0 |

変わったものは、すべて対象の語が変数の `git worktree remove|move` に注意が 1 つ足されたもの (実コマンドは `git -C "$X" worktree remove "$Y"` の形 60 件・
`for` の中の形を含む。生成は後ろの git が `git worktree remove --force "$Z"` / `"../$Z"` / `git worktree move "$Z" ../moved` のもの)。
実コマンドの読み取りは最大で約 3 ミリ秒 (0.9.3 と同じ) で、期限に当たったものは 0 件。相対パスの解決は 0.9.3 と同じ式なので、symlink の環を `..` で
横切る形 (この fixture には無い) も 0.9.3 と同じ値になる。`SymlinkedPlaceDecisionTest` と `CachedLoopDecisionTest` で 3.11〜3.14 のそれぞれに流した。

### テスト

- `SymlinkedPlaceDecisionTest`: 相対パスの途中の symlink を辿って持ち主を決めること (自分の worktree の中の main への symlink は止め、main の中の
  自分の worktree への symlink は止めない)。自分の worktree の中の symlink の環を `..` で横切った後の相対パスの移動 6 形を止めること (どの版でも)。symlink の後ろの `..` は、辿った先から上がること
  (`git -C <main の中の dir への symlink>/..` を止める。POSIX)
- `CachedLoopDecisionTest`: checkout をまたぐ symlink の環 (main の中の `E` と自分の worktree の中の `S` が互いを指す) を `..` で外した後の移動 4 形を、
  0.9.3 と同じ持ち主に決めること (Python 3.13 以降は止める。3.12 以前は realpath が環で辿るのをやめ、自分の worktree のままになるので止めない。どの版でも流す)
- `DeepRelativePathTest`: 相対パスを深くし続ける形 (区切りの空白の有無、引用した語、`..` / `.` を含む語、分岐で状態を上限ちょうどにした形、閉じない
  here-document で安全網の読み直しを掛けた形、内部バックログに挙がった形) が期限で注意になり、期限を過ぎた後すぐに返ること。期限の手前で見つけた停止を
  残すこと、期限の手前で読み終える深さは 0.9.3 と同じく後ろの git まで読むこと (期限を短くして流す)
- `DeadlineTest`: 期限を過ぎたら残りを読まないこと (移動を読む数で数える)、それまでに見つけた停止を残して注意を 1 つ足すこと、期限の手前では
  何も変わらないこと (読みの数は Windows で倍になるので、読みの数から期限を決める)、安全網の旧来の読みと読み直しにも期限が効き、過ぎたら字句に
  分けることからやめること、1 つの移動を状態ごとに読む途中でも期限で止まること、字句に分ける時間は期限に入れないこと、期限の起点は最初の
  `start` だけで動かないこと。時計を差し替えて決定的に流す
- `DeadlineInOneGitTest`: 1 つの git を状態ごとに読む途中でも期限で止まること
- `ReadTimeHookTest`: hook 全体が hooks.json の timeout の半分より短い時間で返ること (期限が無ければ 30 秒ほどかかる形は期限の注意を返す)、
  期限が timeout の半分より短いこと、README と AGENTS.md に書いた値がコードと同じこと
- `WorktreeNameTest`: 対象の語が変数の `git worktree remove|move` は、今の場所が決まっていても決まっていなくても注意になり、他の repo の中・repo の外・
  自分の worktree を動かす形・remove / move 以外では何も出さないこと (0.9.3 の「変数の語は何も出さない」を確かめていたテストを置き換えた)
- `StateLimitTest`: 状態の上限を超えた後の変数の語も注意になること。時間の伸び方を測るテストは期限を外して測る

テスト件数: 224 → 245 (追加 21・削除 0・統合 0)。

## 0.9.3

**`&&` / `||` の分岐ごとに行き先が割れる形で、追う状態が倍々に増えて hook が時間切れになる (コマンドがそのまま走る) 経路を、状態の数の上限で閉じた (patch bump)。**
`x || cd d0; x || cd d1; ...` (相対パス) を 18 回並べると約 24 秒、`pushd a0 && pushd b0 && true & ...` を 12 回並べると約 21 秒かかっていた (0.9.2 まで。hook の timeout は 10 秒)。
どれも普段は書かない形だが、時間切れは止めずに素通しになる。形ごとに閉じず、状態の数で閉じる。

### 修正: 状態が 64 を超えたら、行き先の決まらない状態にまとめる

- 追う状態 (作業ディレクトリ・pushd のスタック・直前の成否の組) の集合は、今の状態・`cd` などの移動が失敗した経路 (`||` の右)・`&` で戻す時点の 3 つある。
  演算子を読むたびに、どれかが 64 を超えたら、その集合を成否ごとに 1 つの状態にまとめる。成否ごとに、その成否の全状態で同じ作業ディレクトリと
  スタックはそのまま残し、割れた作業ディレクトリは決まらないものにし、割れたスタックは、その成否のどの状態でも積まれている深さだけ
  行き先の分からない要素で残す
  (空のスタックにすると、popd が失敗してその場に残る読みになって止めすぎる。`pushd /t/x; <上限を超える形>; cd <main>; popd; git reset --hard` の popd は、
  どの状態でも `/t/x` の手前に戻る。深さを成否をまたいで取ると、`x && pushd /t/z && x && cd <main> && popd; git reset --hard` のように
  成功の側だけが積んだ形で、成功の側が失敗の側の浅いスタックを持ち、同じく止めすぎる)。`&` で戻す時点をまとめても、export 済みの
  `GIT_DIR` / `GIT_WORK_TREE` と export していない代入は残す
- まとめた後の git は、行き先の決まらない `cd "$X"` の後ろと同じく注意だけにする (止めない)。まとめた後でも、絶対パスの `cd <dir>` / `pushd <dir>` /
  `git -C <dir>` の行き先は読むので、`<上限を超える形>; cd <main> && git reset --hard` は止まる
- `git worktree remove|move <worktree>` も、まとめた後は (export 済みの `GIT_DIR` で repo が決まり、名前で別の worktree に決まる形を除いて) 注意にする。
  git が読む repo か今の場所が決まらないので、名前 (パスの末尾) が別の worktree に一致するか、名前で決まらない相対パス (`../wt-b` や、
  symlink を通る `wt/<name>` など。基点が決まらないので、綴りに関係なくどの worktree にも届きうる) なら注意を出す。注意にしないと、
  上限を超えた後のこの形だけが何も出ずに通る (0.9.2 は行き先ごとに読んで止めていた)。
  同じ読みなので、上限に関係なく、行き先の決まらない `cd "$X"` / `git -C "$X"` / `--work-tree="$X"` の後ろの同じ形も、通過から注意に変わる
  (ほかの書き込みの `cd "$X"; git reset --hard` と同じ)。自分の worktree の名前、変数の語 (`"$Y"`)、別の worktree を指さない絶対パスは今までどおり何も出さない (別の worktree の絶対パスは、今の場所に関係なくパスとして読んで止める)。
  どの worktree の末尾にも一致しない素の相対パス (`b` / `x/b`) も注意にする。綴りが末尾に一致しなくても、途中か最後の symlink を通って
  別の worktree に届く (main の中の `wt` が別の worktree の親への symlink なら、`wt/<name>` はその worktree を消す)
- 64 の根拠: git を含む実コマンド 12,725 件で状態の最大は 6、この suite の入力で 4。上限に張り付いた形は状態の数に比例して遅くなるので、
  普段の最大の 10 倍ほどにした (`x || cd d<i>;` は 6 回でちょうど 64 に届き、7 回で超える)
- 上限を超えた後は、測った形 (`x || cd d<i>;` / `pushd a<i> && pushd b<i> && true &` などの繰り返しと、それに閉じない here-document を付けたもの) が、
  繰り返しの数に比例する時間で読み終える。その中で最も遅い形 (`pushd a<i> && pushd b<i> && true &` に閉じない here-document を付けたもの) で
  4,000 回 (約 146 KB) が約 2.1 秒。全状態で同じスタックを残すので、スタックが伸び続ける形 (`pushd /t/p<i>; x || cd d<i>;` の繰り返し) は比例より速く伸びる
  (4,000 回・約 119 KB で約 2.4 秒)。状態の数とスタックの深さの積で遅くなる経路は上限の手前にもあり (0.9.2 から)、内部バックログで別に扱う
- 判定が変わる形: 状態が 64 を超えた後に、読みの 1 つが別の checkout に届いていた形は、止めずに注意だけになる (`cd <main>; x || cd d0; ...` を 7 回以上
  並べた後の `git reset --hard` など。`x` がずっと成功すれば main で走る)。zsh の読みで `&` の手前に `&&` で移動を 64 個以上つないだ形
  (`cd <main> && cd /t/d1 && ... && true & git reset --hard`) も、`&` で戻す候補が 64 を超えてまとまるので同じ。README の「目的と限界」と AGENTS.md の
  指摘しないものに書いた
- 内部バックログで同じ問題として挙がっていた `x | pushd /t/d0 | ... | pushd /t/d17` は、0.9.2 の時点で既に状態が増えない形だった (pipeline の途中の要素はサブシェルで動き、
  最後の要素の読みだけが残る)。増えないことをテストで固定した

### 比較

0.9.2 と 0.9.3 の読み (`command.analyze`) に同じ入力を流した (macOS)。生成した入力は 3 組で、後ろの git の形と前置きのそれぞれが、0.9.2 で 1 回は止まる
ことを先に確かめた (読み取りの `git status` と、生成 1 の `git worktree remove b` を除く。`b` は fixture のどの worktree の名前にも一致しないので、生成 2 で `wt-b` に替えた)。

| 入力 | 件数 | 0.9.2 で状態が 64 を超えたもの | 判定か文面が変わったもの |
|---|---|---|---|
| git を含む実コマンド | 12,725 | 0 | 1 (判定は注意のままで、行き先の決まらない `cd` の後ろの `git worktree remove <相対パス>` の注意が 1 つ足されたもの) |
| 生成 1: 移動の語彙 18 形 × 行き先 5 形 × 分岐の形 14 形 × 回数 × 前置き 6 形 × 後ろの git 11 形 (回数 1〜3 は直積のすべて、5〜8 は 12,000 件を抜き出し) | 261,480 | 1,527 | 6,672 (状態が 64 を超えたもの 768、64 以下のもの 5,904) |
| 生成 2: 生成 1 の後ろの `git worktree remove b` を `git worktree remove --force wt-b` (名前が別の worktree に一致) に替え、後ろに `popd; git reset --hard`、前置きに `pushd /t/x;` を足したもの | 329,520 | 1,617 | 6,574 (状態が 64 を超えたもの 791、64 以下のもの 5,783) |
| 生成 3: 生成 2 に、後ろ 3 形 (`..` を含む相対パスの `git worktree remove`、`git worktree move wt-b`、`export GIT_DIR; git add .`)、前置き 2 形 (`cd <main>; pushd /t/x;` でスタックに main を積む形、export しない `GIT_DIR=<main>/.git;`)、分岐の形 1 形 (`x || M; x; true &`) を足したもの | 558,750 | 1,627 | 22,040 (状態が 64 を超えたもの 849、64 以下のもの 21,191) |

状態が 64 以下で変わったものは、すべて `git worktree remove|move` の後ろに注意が 1 つ足されただけ (上の 3 つ目の項目の、行き先の決まらない移動の後ろの形。
判定は通過 → 注意か、止めたまま注意が加わる)。生成 1 で通過 → 注意 5,904 件 (後ろの `git worktree remove b` の前に `cd ${HOME};` か、
行き先の決まらない `popd` / `cd -` があるもの)、生成 2 で通過 → 注意 3,057 件・停止 → 停止 2,726 件、生成 3 で通過 → 注意 12,437 件・停止 → 停止 8,754 件。

状態が 64 を超えた入力で変わったもの:

| 0.9.2 → 0.9.3 | 生成 1 | 生成 2 | 生成 3 | 形 |
|---|---|---|---|---|
| 停止 → 停止 (文面) | 358 | 392 | 327 | 止める理由は同じで注意が加わったもの (生成 1 で 340 件)、止める checkout の一部が注意に変わったもの (同 18 件) |
| 通過 → 注意 | 318 | 289 | 368 | 自分の worktree の中で分岐を重ねた形 |
| 停止 → 注意 | 92 | 110 | 154 | `cd <main>; x || cd d0; ...` のように分岐を重ねた後の git。`bash` と `zsh -f` に偽の git を置いて走らせると、生成 1 の 78 件・生成 2 の 93 件・生成 3 の 120 件はどちらの shell でも別の checkout で書き込みが走り (上の「判定が変わる形」)、残り (14 / 17 / 34 件) はどちらの shell でも走らない (0.9.2 の止めすぎ)。片方の shell だけで走るものは 0 件 |
| 通過・注意 → 停止 | 0 | 0 | 0 | |
| 停止 → 通過 (注意も無い) | 0 | 0 | 0 | |

### テスト

- `StateLimitTest`: 内部バックログに挙がった形 (と同じ形で数を増やしたもの) が注意になること、上限ちょうどまでは今までどおり読み (止める)、1 回の分岐で超えた形から注意になる境界、
  zsh の `&` の手前の `&&` で候補が上限を超える境界、まとめた後も絶対パスの行き先を読むこと、まとめるときに失敗の側を残すこと (`||` の右を読む)、
  移動の失敗の経路もまとめること、まとめても全状態で同じ作業ディレクトリとスタックを残し、割れたスタックの深さを残すこと (popd の後ろの git)、
  作業ディレクトリとスタックの深さは成否ごとに決めること (成功の側だけが pushd で積んだ形)、まとめても export 済みの `GIT_DIR` と export していない代入を残すこと、
  まとめた後の `git worktree remove|move` の名前と相対パス (実在の dir と symlink で組んだ、symlink を通る相対パスを含む) が注意になること、
  時間が繰り返しの数に比例すること、README / AGENTS.md に書いた値がコードと同じこと。コマンドを読むたびに状態の集合の大きさを数え、上限を超えたらその場で落とす
  (上限が効かない版で固まらない)
- `HookTimeTest`: hook 全体 (git の起動を含む) が、hooks.json の timeout の半分より短い時間で注意を返すこと
- `WorktreeNameTest`: 今の場所か git dir が決まらないときの `git worktree remove|move` は止めずに、名前が別の worktree に一致するか、名前で決まらない
  相対パス (symlink を通る `lnk` / `wt/wt-b` を含む。今の場所が決まれば止める) なら注意を出し、自分の worktree の名前・変数の語・repo の外の絶対パスでは
  何も出さないこと (0.9.2 の「止めない」だけを確かめるテストを置き換えた)

テスト件数: 208 → 224 (追加 16・削除 0・統合 0)。

## 0.9.2

**Windows で、Git Bash の綴りのパス (`/c/Users/...`) で別の checkout を指す git の書き込みが止まらなかった不具合を直した (patch bump)。**
`git -C /c/Users/<u>/repo reset --hard` / `cd /c/Users/<u>/repo; git reset --hard` は main を書き換えるのに通っていた (0.9.1 まで。`C:/Users/...` と書けば止まっていた)。
Git Bash の pwd はこの形を返すので、Windows では普段の書き方になる。

### 修正: `/c/...` を Git Bash の読みでも解決する

Windows の Bash tool は Git Bash で動き、`/c/x` は `C:\x`、`/c` は `C:\` になる (`cd` などの組み込みも、git に渡す引数も)。
Windows の Python は `/c/x` を今のドライブの `\c\x` と読むので、checkout の `C:\Users\...` と一致しなかった。

- Windows では、コマンド全体を Windows の読みと Git Bash の読みの両方で判定し、どちらかで別 checkout への書き込みになれば止める
  (zsh の読みと bash の読みの両方で判定するのと同じ形)。Windows の読みを残すのは、`MSYS_NO_PATHCONV=1` などで変換を止めた git が
  `\c\x` を読むためで、和にしたので 0.9.1 が止めていた形は止まったまま
- 読み替えるのは、行き先を読むすべての入り口 (`cd` / `chdir` / `pushd` / `pushd -n` の行き先、`git -C`、`--git-dir` / `GIT_DIR`、
  `--work-tree` / `GIT_WORK_TREE`、`git worktree remove|move` のパス、`env -C` / `sudo -D`)。ドライブの文字は大文字も読む (`/C/...`)
- 読み替えるのは `/` の後に 1 文字と `/` (か語の終わり) が続く形だけ。`/cc/x` や、Windows 以外の `/c/x` (そこにあるパスのまま) は読み替えない
- 次の形は Git Bash の読みにならないまま: 区切りに `\` を混ぜた `/c\x`、`/cygdrive/c/x`、ドライブの root より上に出る `..`
  (`/c/../c/x`)、Git Bash の root の下のパス (`/tmp` / `/usr` など)。Write / Edit のファイルパスの読みは変えていない。
  README の目的と限界と AGENTS.md の指摘しないものにも同じ形を書いた

### 比較

0.9.1 と 0.9.2 に同じ 5,264 件の入力 (行き先の綴り 39 形 / 17 形 × 操作 47 形) を、4 つの世界で流した。
Windows の読みは `os.path` を `ntpath` に差し替え、今のドライブを `C:` とした。実際の Git Bash では確かめていない。

| 世界 | 通過・注意 → 停止 | 停止 → 通過・注意 | 形 |
|---|---|---|---|
| Windows | 472 | 0 | Git Bash の綴りで main / 別の worktree を指す形 (`/c/...` / `/C/...` / 引用 / 末尾の `/` や `/.` / `//`)、`cd /c; cd Users/...`、自分の worktree から `cd ..` で main に出る形 |
| Windows (`C:\c\proj` に worktree がある) | 472 | 0 | 上と同じ。`git -C /c/proj ...` は 0.9.1 と同じく止まる (Git Bash の読みに置き換えるだけにすると、この世界の 102 件が止まらなくなる) |
| POSIX (`/c/Users/...` に checkout がある) | 0 | 0 | |
| POSIX (`/c/...` は checkout の外) | 0 | 0 | |

ドライブではない綴り (`/cc/x` / `//c/x` / `/c:/x` / `/cygdrive/c/x` / 別のドライブ `/d/...`) と、自分の worktree を指す形 (`cd ..` で main に出るものを除く) は、どの世界でも変わらない。

### テスト

- `NtpathEmulationTest`: Git Bash の綴りを行き先の各入り口で止めること、`/cc/x` と自分の worktree を止めないこと、Windows の読みも残すこと。
  `os.name` を `nt` にし、ドライブの無いパスを `C:` で補う realpath に差し替えた (POSIX の上の ntpath はドライブを補わず、実機は今のドライブで補う)
- `GuardTest`: Windows 以外では `/c/x` を読み替えないこと (読み替えると main の下の相対パスになって止めすぎる)。Windows の実機では、本物の repo を Git Bash の綴りで指して止めること
- `PosixDrivePathTest`: POSIX の `/c/...` は、そこにある checkout をそのまま指すこと

テスト件数: 202 → 208 (追加 6・削除 0・統合 0)。

## 0.9.1

**README の「目的と限界」に、shell と読みがずれる書き方を範囲外として明記し、AGENTS.md の指摘しないものに同じ形を足した (docs のみ。patch bump)。**
判定とコードは変えていない (挙動の変更なし)。

### docs: 範囲外とした書き方の明記

`bash` と `zsh -f` に偽の `git` を置いて実際に走らせ、現行の版の判定と突き合わせた形だけを載せた。載せた形のうち、別の checkout を書き換えるのに通るもの、書き込まないのに止めるもの、注意だけにするものを分けて書いた。

- リダイレクトの位置 (`>/dev/null git -C <dir> ...` / `cd >/dev/null <dir>` を通し、`(cd <dir>)>/dev/null` を止めすぎる)
- 成否の細かい読み (`exit`、wrapper の後ろの `:`、`time ( ... )` / `if ( ... )`、パイプの途中の `!`)
- ディレクトリスタック (`cd -`、`popd --` / `popd +0` は注意だけ、`pushd +N` の後の `popd`)
- `&` やパイプでつないだ複合コマンド、`&&` / `||` の直後の改行、zsh だけが受け付ける形
- 環境変数 `GIT_DIR` / `GIT_WORK_TREE` の値を実行の分岐ごとには持たないこと (`test -f x && unset GIT_DIR; git reset`。出てきた順に全体へ反映する今の挙動のまま)
- `export` 以外の書き方・`unset -f`・他の環境変数・綴りの違う git dir
- backtick の中の改行で始めた here-document・語の途中の `#`・bash 3.2 だけが走らせる形
- 一覧に無い wrapper (`nice` / `find -exec`)、行き先の一部だけが変数の形

現行の版で既に直っていた形 (`<<\EOF` の本文を読まない、閉じない here-document の安全網、スタックが空の `popd`、`cd -x` の失敗、2 つの pipeline の読みの混合) と、実測で案の判定と違った形 (backtick の値を使った `GIT_DIR` は注意が出る) は載せていない。

## 0.9.0

**`git worktree remove|move` の `<worktree>` を、git と同じく worktree の名前 (パスの末尾) としても読み、別の worktree を名前で指す形を止めるようにした (minor bump)。**
`git worktree remove --force b` / `worktrees/b` / `.claude/worktrees/b` と `git worktree move b <dest>` は、今の場所に関係なく `.../b` の worktree を消す / 動かすのに、止まらなかった (0.8.1 まで。0.8.0 で見逃しとして挙げていた形)。
対象を足す変更で、0.8.1 が止めていた形は止まったまま (比較の全入力で 0 件)。

### 修正: `<worktree>` を worktree の名前として読む

git は `<worktree>` をパスとして読む前に、`<worktree>` で終わる worktree のパス (区切りの直後から一致するもの) を数え、ちょうど 1 つならそれを選ぶ (2 つ以上ならパスとして読む)。0.8.1 まではパスとしての一致だけを見ていた。

- 名前を照合するのは、git が読む repo がこの repo のときだけ (git dir の指定があればそれが対応付く checkout、無ければ今の場所で決まる)。他の repo の中 (`cd <他の repo>; git worktree remove b`) と repo の外では読まない。今の場所も git dir も決まらない形 (`cd "$Y"; git worktree remove b`) は、パスの読みと同じく止めない
- 比べ方は core.ignorecase で変わる (macOS では既定で true で、`B` も `b` の worktree を選ぶ)。大文字小文字を区別する読みと区別しない読み (ASCII だけを小文字にし、NFC にそろえる。macOS の git は引数を NFC にそろえる) のそれぞれで数え、どちらかで別の worktree 1 つに決まれば止める
- 名前は `git worktree list` が返すパス (realpath) と比べる。symlink を通して作った worktree も realpath の末尾で止め、symlink の綴り (`<link>/b`) は名前にならない (git も選ばない)
- 自分の worktree に決まる名前 (`git worktree remove a`) は止めない (0.8.1 と同じ)

### 実 git の実測

bash 3.2 と `zsh -f` で本物の git (2.50) を走らせた結果 (○ = 別の worktree が消える / 動く):

| 形 | 0.8.1 | 0.9.0 | 実測 |
|---|---|---|---|
| `git worktree remove --force b` / `-f b` / `-- b` / `b --force` / `worktrees/b` / `.claude/worktrees/b`、`git worktree move b <dest>` | allow | deny | ○ |
| 上の形の前に `cd <main>;` / `git -C <main>` / `GIT_DIR=<main>/.git` / `--work-tree=<main>` / `env -C <main>` / `pushd <main>;` を付けたもの | allow | deny | ○ |
| `git worktree remove --force B` / `.CLAUDE/Worktrees/B` / 大文字小文字だけ違う絶対パス (大文字小文字を区別しない FS) | allow | deny | ○ |
| `git worktree remove --force café` を NFD で書いたもの | allow | deny | ○ |
| `<repo の root からの相対パス>` / `/private` を外した `tmp/...` のように、パスの末尾として一致する語 | allow | deny | ○ |
| `git worktree remove --force a` (自分の worktree) | allow | allow | 自分の worktree が消える |
| `git worktree remove --force b/` / `./b` / `x/b` / `b/.` / `a/../b` / `t-b` (`wt-b` の途中から) / `-fb` / `--force=b` | allow | allow | git が失敗する |
| `cd <他の repo>; git worktree remove --force b` / `GIT_DIR=<他の repo>/.git ...` / `git -C <他の repo> ...` | allow | allow | 他の repo の worktree が消える |
| 2 つの worktree が同じ名前で終わるときの `git worktree remove --force b` | allow | allow | git が失敗する (パスとして読む) |

0.8.1 と 0.9.0 に同じ 3,771 件の入力 (置き場 42 形 × 操作 12 形 × 語の組み合わせと、演算子・redirect・comment・置換・引用を付けた形) を流した。
止める側を緩めた形は 0 件。通過 → 停止は 905 件で、全件を本物の git で走らせた:

| 実測 | 件数 |
|---|---|
| 別の worktree が消える / 動く (止めた理由の worktree と一致) | 865 |
| `sudo git ...` (測定の sudo は偽物で走らない。`sudo` を外した同じ形は消える) | 24 |
| `mv ../b ../b2; git worktree remove --force b` (`mv` が動かす。git も同じ worktree を対象にする) | 1 |
| git が失敗する (下の「受け入れている」を参照) | 15 |

次の形は読みが実際と違うが、受け入れている:

- git が失敗する形も止まる: main の名前 (`git worktree remove repo`。git は main を消さない。0.8.1 も main の絶対パスは止めていた) / `git -c core.ignorecase=false worktree remove B` (区別しない読みで止める) / 移動先の無い `git worktree move b`
- 今の場所が決まらない形は止めない (見逃し。0.8.1 と同じ): `cd "$Y"; git worktree remove --force b` / `git -C "$Y" ...` (`Y` が空なら今の場所のまま走り、別の worktree が消える)
- 置換の中の語は読まない (これまでどおり対象外): `git worktree remove --force "$(echo b)"`
- repo の中に置いた別の repo (`cd <main>/vendor/<別の repo>; git worktree remove b`) は、この repo と読んで止まる (他の書き込み操作のパスの読みと同じ)
- `git worktree list` が返すパスと realpath が食い違う置き場では、名前の一致を取り違えることがある (git 2.50 は worktree を作るときに realpath で持つ)
- 同じコマンドの前の操作で worktree の一覧が変わる形は止めない: 消える worktree と名前が重なっていたときの `git worktree prune && git worktree remove --force b` / 自分の worktree を消すか動かした後の `git worktree remove --force b` (名前は hook の時点の一覧で数え、git は走るときの一覧で数える)
- 大文字小文字や NFC / NFD だけが違う相対パス (`git worktree remove ../B` / `cd ..; git worktree remove ./B`) は読まない (見逃し。0.8.1 と同じ。git はパスとしての読みでも core.ignorecase で比べる)

### テストの追加

- `tests/test_worktree_names.py`: `WorktreeNameTest` / `NameReadingTest` (大文字小文字だけが違う worktree など、FS で作れない一覧を組んで確かめる) / `NtpathNameTest` (Windows の区切りと大文字小文字) / `RealGitNameTest` (本物の git と本物の shell で、名前で指した worktree が消える / 動くことと、その形を止めることを固定する)

テスト件数: 184 → 200 (追加 16・削除 0・統合 0)。

## 0.8.1

**Windows で `cd <main>; cd ~+; git reset --hard` が止まらなかった不具合を直し、Windows の CI を直した (patch bump)。**

### 修正: Windows で、home の無い `~name` を home と読まない

Python の Windows 用 `expanduser` は名前を調べずに `~name` を `<home の親>\name` と推測する。`~+` が `C:\Users\+` になり、手前の `cd <main>` が消えて、main で動く git を見逃していた。POSIX は知らない名前をそのまま返すので、この形を止めていた。
`~name` は、その home が実在するときだけ展開する。`~+` / `~-` / `~1` / 知らない名前は、どの OS でも今の場所の下の語として読む。

比較は同じ 2013 件の入力を POSIX の読みと Windows の読み (`os.path` を `ntpath` に差し替え) で 0.8.0 と 0.8.1 に流した。止める側を緩めた形は両方で 0 件。

| 読み | 通過 → 停止 | 停止 → 通過 | 形 |
|---|---|---|---|
| POSIX | 16 | 0 | home が実在しない利用者の `~name` の後ろ (実際の shell は移動に失敗して main に残る) |
| Windows | 220 | 0 | `~+` / `~-` / `~1` / 知らない名前の後ろ (ntpath の差し替えでの数。`C:\` を実在に含めていないので、下の `~..` の分だけ多い) |

次の形は Windows で止まらないまま: `cd <main>; cd ~.` / `cd ~..` / `cd ~:` と、`~Public` のように `C:\Users` の下に実在する名前 (名前がパスの部品として読める。普通には書かない形)。

### テスト: Windows の前提の違いを直した

0.8.0 の Windows の CI の 20 件のうち、製品の不具合は上の 1 件。残りはテストの前提で、どれも Windows で判定を緩めていない。

- `git worktree remove` の相対パスを引用なしで `..\wt-b` と書いていた。引用なしの `\` は shell が消し、git が受け取るのは `..wt-b` で、止めないのが正しい。`/` 区切りにし、引用した `\` の形を足した
- 行の継続のテストが `C:\` の直後で分けていて、`\\` が escape になり継続ではなくなっていた。分ける位置を変えた
- `.git` ファイルの上書きが Windows で `PermissionError` になる。消してから作り直す (Windows の実機が手元に無く、隠しファイルが理由という読みは未検証)
- `RealGitTest` は `/bin/bash` が無ければ skip する。hook を呼ばず、git と shell の前提だけを確かめるテストで、判定は `WorktreeBaseTest` などが確かめる

`NtpathEmulationTest` を足した。`os.path` を `ntpath` に差し替えて、Windows のパスの読みをどの OS でも確かめる。

テスト件数: 180 → 184 (追加 4・削除 0・統合 0)。

## 0.8.0

**`GIT_DIR` / `--git-dir` だけが効いているときの作業ツリー (今の場所) と、`<checkout>/.git` の形の git dir を読み、別 checkout への git の書き込みを止めるようにした (minor bump)。**
`export GIT_DIR=<自分の worktree>/.git; git -C <main> reset --hard` は main の作業ツリーを上書きし、`git --git-dir=<別の worktree>/.git add .` は別の worktree の index に書くのに、止まらなかった (0.7.0 まで)。
どちらも対象を足す変更で、0.7.0 が止めていた形は止まったまま (比較の全入力で 0 件)。

### 修正: git dir だけが効くとき、作業ツリーは今の場所 (config の `core.worktree` が無ければ)

`--work-tree` / `GIT_WORK_TREE` が無ければ、git は今の場所を作業ツリーにする。0.7.0 までは git dir の checkout だけを見ていた。今の場所 (`cd` / `git -C` の後) も対象に足す。
今の場所が決まらない形 (`cd "$Y"; GIT_DIR=<自分の worktree>/.git git reset --hard`) は注意を出す。git dir の checkout が別の checkout なら、これまでどおり止める。

### 修正: `<checkout>/.git` を、その checkout の git dir として読む

linked worktree の `.git` はファイルで、git は中身をたどる。0.7.0 までは `.git/worktrees/<name>` の形だけを対応付けていて、`<worktree>/.git` の形は別の checkout だと分からなかった。

### 修正: `git worktree remove|move` の相対パスは、どの基点でも別の worktree なら止める

`git worktree remove|move` の相対パスは、git dir・作業ツリー・今の場所のどの基点でも別の worktree なら止める。git dir と作業ツリーの指定で基点が変わるので、先頭の対象だけで読まない。`GIT_DIR=<main>/.git git worktree remove --force ../<別>` と `cd <main>; GIT_DIR=<自分>/.git git worktree remove --force <別への相対パス>` は別の worktree を消すのに、止まらなかった (0.7.0 まで)。

止める理由の文面は「`git <sub>` が別の checkout (...) を対象にしている」に変えた。`cd <main>; GIT_DIR=<自分>/.git git add .` のように書かない形でも出るため。

### 実 git の実測

bash 3.2 と `zsh -f` で本物の git を走らせた結果 (○ = 別 checkout に書く):

| 形 | 0.7.0 | 0.8.0 | 実測 |
|---|---|---|---|
| `export GIT_DIR=<自分>/.git; git -C <main> reset --hard` / `... cd <main>; git reset --hard` | allow | deny | ○ main の作業ツリー |
| `cd <別>; git --git-dir=<自分>/.git clean -fd` | allow | deny | ○ 作業ツリー |
| `cd <main>; GIT_DIR=<repo の外>/.git git reset --hard` | allow | deny | ○ main の作業ツリー |
| `export GIT_DIR=<別>/.git; git add .` / `git --git-dir=<別>/.git commit -m x` | allow | deny | ○ 別の worktree の index / HEAD |
| `git --git-dir=.git add .` / `export GIT_DIR=<自分>/.git; git reset --hard` / `git --git-dir=<自分>/.git --work-tree=<自分> add .` | allow | allow | 自分の worktree だけ |
| `git -C <main> --git-dir=<自分>/.git status` / `GIT_DIR=<別>/.git git status` | allow | allow | 書かない |
| `git --git-dir=<repo の外>/.git add .` | allow | allow | repo の外の index だけ |

次の形は読みが実際と違うが、受け入れている:

- 今の場所が別の checkout なら、作業ツリーを書かない操作も止まる: `cd <main>; GIT_DIR=<自分>/.git git add .` / `git commit -m x` / `git reset --soft` (git dir の側にだけ書く)。操作ごとの読みは足していない
- git が失敗して何も書かない git dir も同じに読む: `GIT_DIR= git -C <main> reset --hard` / `GIT_DIR=<自分> git -C <main> reset --hard`
- 綴りが違う git dir は対応付けない (見逃し。0.7.0 と同じ): `GIT_DIR=<別>/.GIT git add .` (大文字小文字を区別しない FS) / `.git` ファイルの hard link
- `git worktree remove|move` の相対パスは、実際の基点が 1 つでも全部の基点で読むので、git が失敗する形も止まる: `GIT_DIR=<main>/.git git worktree remove <main からの相対パス>` / `cd sub; git --work-tree=<自分> worktree remove ../<別>`
- `git worktree remove|move` の、パスの末尾だけの指定は読まない (見逃し。0.7.0 と同じ): `git worktree remove --force <別の名前>` (git は末尾の一致で別の worktree を選ぶ)

### テストの追加

- `tests/test_git_dir_targets.py`: `GitDirOnlyWorkTreeTest` / `GitFileMappingTest` / `WorktreeBaseTest` / `RealGitTest` (本物の git と本物の shell で、書く / 書かないの前提を固定する)

テスト件数: 158 → 180 (追加 22・削除 0・統合 0)。

## 0.7.0

**zsh の `&` が最後の pipeline だけを background にすることと、移動の失敗で `||` の右に進む経路を読み、別 checkout への git の書き込みを止めるようにした (minor bump)。**
`cd <main> && true & git reset --hard` (zsh) と `cd <main>; cd <無いパス> || git reset --hard` は別 checkout に書くのに止まらなかった (0.6.0 まで)。
どちらも読みを足す変更で、0.6.0 が止めていた形は止まったまま。

### 修正: zsh の `&` は最後の pipeline だけを background にする

`a && b &` を、bash は AND-OR list 全体を background で動かすが、zsh は `b` だけを background で動かし、`a` は今の shell で動かす。
0.6.0 までは list 全体をサブシェルと読んでいたので、手前の `cd` が残らないと読んでいた。zsh の読みでは `&&` / `||` の手前の `cd` / `pushd` / `chdir` と `export` を残す
(bash の読みは変えていない)。手前の移動が失敗すればその前の場所に残るので、list の開始時点からの候補をすべて判定する。

| 形 | zsh | bash |
|---|---|---|
| `cd <main> && true & git reset --hard` | main で走る (止める) | worktree で走る |
| `cd <main> \|\| true & git reset --hard` | main で走る (止める) | worktree で走る |
| `true && cd <main> & git reset --hard` / `cd <main> & git reset --hard` | worktree で走る | worktree で走る |
| `export GIT_DIR=<main>/.git && true & git reset --hard` | main へ書く (止める) | worktree で走る |

`&&` / `||` の直後の改行とコメントでは list は切れないので、折り返した形も同じ行の形と同じに読む
(`cd <main>; cd <worktree> &&` と改行のあとの `true & git reset --hard` は bash では main で走る。`cd <無いパス> ||` と改行で折り返した形は zsh でも main で走る)。
bash は折り返しを切る読みも通し、どちらかで見つかれば止める (`export GIT_DIR=<worktree>/.git; command export GIT_DIR=<main>/.git &&` と改行のあとの `true & git -C <main> reset --hard` は main の作業ツリーを上書きする)。

### 修正: 移動の失敗で `||` の右に進む経路を読む

`cd` は成功するものとして読むので、`||` の右は実行されないと読んでいた。`cd` / `chdir` / `pushd` / `popd` の直後の `||` の右辺を、移動が失敗してその場に残った状態でも判定する
(`cd <main>; cd <無いパス> || git reset --hard` / `cd <無いパス> || git -C <main> reset --hard` / `cd }` / `cd +1` / `popd +9`)。
`||` の後ろの改行とコメント、`(` と改行、`! (`、`{` と改行、`if`、`x=$(...)` / `echo $(...)` の中も右辺として読む。
移動の行き先の語の `$(...)` も移動の手前の場所で読む (`cd <main>; cd $(git reset --hard)` は main で走るので、注意だけでなく止める)。

この経路は右辺の中だけで読み、後ろには残さない。移動が成功する前提の普段の形は止めない:
`cd <worktree> || exit 1; git add .` / `cd <main>; cd <worktree> || { echo no; exit 1; }; git add .` / `if ! cd <worktree>; then exit 1; fi; git add .` / `cd <worktree> && git add .`。

次の形は読みが実際と違うが、受け入れている:

- 右辺に別 checkout への書き込みそのものを書いた形は、行き先が移動できる場所でも止まる: `cd <main>; cd <worktree> || git add .` (`<worktree>` があれば走らない。静的には区別しない)
- 右辺の外の経路は読まない (見逃し。0.6.0 と同じ): `cd <main>; cd <無いパス> || true; git reset --hard` / `... || true && git reset --hard` / `... && false || git reset --hard`
  (最後の形は `cd <main>; cd <worktree> && false || git ...` を止めないため) / `... || { echo x; git reset --hard; }` の 2 つ目以降のコマンド / `cd <無いパス> || cd <main>; git reset --hard` / `{ cd <無いパス>; } || git reset --hard`
- `!` の後ろの移動の失敗は読まない (0.6.0 と同じ): `cd <main>; ! ! cd <無いパス> || git reset --hard`
- 右辺の最初のコマンドより後ろと、移動の失敗で進む `if` / `while` / `until` の本体は読まない (見逃し。0.6.0 と同じ): `cd <main>; cd <無いパス> || if true; then git reset --hard; fi` / `... || while true; do git reset --hard; break; done` / `cd <main>; if ! cd <無いパス>; then git reset --hard; fi` / `cd <main>; if cd <無いパス>; then :; else git reset --hard; fi` / `cd <main>; until cd <無いパス>; do git reset --hard; break; done`
- `"$(...)"` と backtick の中の git は、右辺でも読まない (これまでどおり対象外)
- zsh の `&` の手前の候補は和集合なので、`cd <main> && cd <worktree> && true & git add .` は worktree で走るが止まる。`chdir <main>; chdir <worktree> && true & git reset --hard` も同じ (0.6.0 と同じ deny)

### テストの追加

- `tests/test_chdir_readings.py`: `BackgroundListTest` / `FailedMoveTest`
- `tests/test_guard.py`: `cd <main> && true & wait; git reset --hard` の期待値を、zsh の実測に合わせて deny に変更

テスト件数: 143 → 158 (追加 15・削除 0・統合 0)。

## 0.6.0

**`cd` / `chdir` / `pushd` / `popd` を、今の shell で動くかと shell ごとの違いまで含めて読むようにした (minor bump)。**
`cd <main>; env cd <worktree>; git reset --hard` や `chdir <main>; git reset --hard` のように、別 checkout へ書くのに止まらなかった形を
止め、`env cd <main>; git reset --hard` のように自分の worktree で走るのに止まっていた形を止めなくなる。

### 修正: 判定を zsh の読みと bash の読みの両方で通す

Bash tool の shell は環境で違い、次の形は shell で動きが割れる。コマンド全体を zsh の読みと bash の読みでそれぞれ通して判定し、
どちらかで別 checkout への書き込みになれば止める。片方の shell の移動ともう片方の失敗を組み合わせて読むことは無いので、
`chdir <main>; chdir <worktree>; git reset --hard` は止まらない。pipeline の最後の要素の `cd` も、この読みに移した (zsh では残り、
bash では残らない)。

どの形をどう読むかは、bash 3.2 / 5.2 と `zsh -f` で偽の git を走らせた実測に合わせている。bash の読みは bash 3.2 と 5 の和で、
どちらかで移動するなら移動すると読む:

| 形 | 読み |
|---|---|
| `env` / `nohup` / `sudo` / `exec` の後ろの `cd` / `chdir` / `pushd` / `popd` | どの shell でも移動しない (0.5.0 までは移動として読んでいた) |
| 代入の後ろの `time` / `time -f x` の後ろ | どの shell でも移動しない |
| `time --` / `time -p --` / 2 つ目の `time` (`time time` / `time -p time` / `time -- time`) の後ろ | bash 5.2 は移動し、bash 3.2 と zsh は外部コマンドで移動しない |
| `command cd` / `command -p cd` / `command -- cd` | bash は移動し、zsh は外部コマンドで移動しない |
| `command -v cd` / `command -V cd` / `command <代入> cd` | どちらの shell でも移動しない |
| `time -p cd` | bash は移動し、zsh は `-p` をコマンドとして起動して失敗する |
| `chdir` | zsh は `cd` と同じに移動し、bash は失敗する (`builtin chdir` も同じ) |
| `cd -q` / `cd -s` (`builtin cd` も) | zsh は移動し、bash は失敗する |
| `pushd -q` / `-s` / `-L` / `-P` | zsh は移動し、bash は失敗する |
| `pushd -n <dir>` | bash は移動せずスタックに積む (次の `popd` がそこへ移動する)。zsh は失敗する |
| `popd -q` | zsh は戻り、bash は失敗する |
| `cd -e` (`builtin cd` も) | bash 5.2 は移動し、bash 3.2 と zsh は失敗する |
| `cd -n` / `cd -x` / `cd -@` など読めない option | どの shell でも失敗して移動しない |
| `cd <a> <b>` / `pushd <a> <b>` | bash 3.2 は a へ移動し、bash 5.2 と zsh は失敗する |
| `{ cd <dir> }` の語の末尾の `}` | zsh は閉じとして読むので語に数えない (bash は構文エラーで何も走らせない)。開き括弧が無い `cd }` の `}` はただの語で、`}` というディレクトリへの移動と読む |
| `time -p ! x` / `time -- ! x` / `time time ! x` の `!` | 後ろのコマンドが今の shell で動く shell でだけ予約語になる (それ以外の shell では外部の time の引数) |
| `FOO=1 ! x` の `!` | どの shell でもコマンド名で、後ろの `x` も動かない (`! FOO=1 x` の `!` は予約語) |

### 修正: 同じ読み落としの近くにあったもの

- `popd >/dev/null` のように redirect を付けた `popd` を、行き先の分からない `popd` と読んでいた。語の無い `popd` と同じに読む
- スタックが空に見える `popd` は、行き先が分からないという注意に加えて、失敗してその場に残る読みでも判定する
  (`cd <main>; popd; git reset --hard` は main で走るので止める)
- `! <コマンド>` の成否は読まず、続く `&&` / `||` の後ろはどちらも実行しうるとする (`! cd <main> || git reset --hard` は main で走るので止める)。
  移動の読みは `!` の無い形と同じで、`cd <main>; ! cd <worktree> && exit 1; git reset --hard` は止めない。2 回書いた `! !` は打ち消す
- 今の shell で動かない `env export GIT_DIR=...` / `nohup unset GIT_DIR` / `env GIT_DIR=...` (コマンドなし) は、変数を変えたと読まない。
  zsh の `command export` / `command unset` も同じ

次の形は読みが実際と違うが、受け入れている:

- `cd -e`: bash 3.2 と zsh は失敗するが、bash 5.2 は移動するので、新しい bash の読みで判定する。
  `cd -e <main>; git reset --hard` は bash 3.2 と zsh では worktree で走るが止まる (0.5.0 と同じ deny)
- `!` の成否を読まないので、実際には git が走らない形も止まる: `! cd <main> && git reset --hard` / `! true && git -C <main> reset --hard` (0.5.0 と同じ)、
  `! chdir <main> && git reset --hard` (zsh は移動して git が走らず、bash は worktree で走る。0.5.0 は止めなかった)
- `!` の後ろの移動の失敗は読まない (見逃し): plain の `cd` と同じで移動は成功すると読むので、`cd <main>; ! cd <無いパス> && git reset --hard` と
  `cd <main>; if ! cd <無いパス>; then git reset --hard; fi` は main で走るが止まらない (0.5.0 と同じ。絶対パスの行き先。相対パスは main の下への移動と読むので止まる)
- `cd <main>; ! cd +0 || git reset --hard` のように `!` の後ろで dir stack を指す形は、行き先が分からないので注意だけ出す
- `(` の手前の予約語は成功したコマンドと読むので、`false || if ( git -C <main> reset --hard ); then :; fi` / `false || { ( git ... ); }` /
  `false || ! ! ( git ... )` の括弧の中は読まない (0.5.0 と同じ)
- `&&` / `||` の後ろの改行は list を切るので、`false &&` + 改行 + `git -C <main> reset --hard` は git が走らないが止まり、
  `cd <無いパス> ||` + 改行 + `git -C <main> reset --hard` は同じ行の形と違って止まる (どちらも 0.5.0 と同じ)
- bash 3.2 と 5 を 1 つの読みにまとめている: 版で移動の組が変わる形を、どちらかの版の組として区別しない
- `exec cd <dir>; git ...`: shell が置き換わるので実際には後ろの git は走らないが、読まない (`cd <main>; exec cd <worktree>; git ...` は止まる)
- 今の shell で動かない `cd` の成否: 外部コマンド次第として両方を判定する。`cd <main>; env cd <worktree> || git reset --hard` は
  macOS では `/usr/bin/cd` が成功して git が走らないが止まる
- `command builtin cd` / `builtin pushd` / `builtin popd`: 0.5.0 と同じで読まない
- 引用した `!` (`'!' x` / `"!" x` / `\! x`): コマンド名として動くが、shlex が引用を外すので予約語の `!` と区別できず、後ろを移動として読む。
  `cd <main>; '!' cd <worktree>; git reset --hard` は main で走るが止まらない (`\time` と同じ)
- `FOO=1 ! x` の `!` は一般のコマンドと同じく成否の両方を読む。実際には見つからず失敗するので、`FOO=1 ! x && git ...` は git が走らないが止まる
- `cd <main>; cd <worktree> }; git ...` の `}` は語で、語が 2 つの `cd` (bash 3.2 だけ移動する) になる

### テストの追加

- `tests/test_chdir_readings.py`: 上の読みごとの判定

テスト件数: 104 → 143 (追加 39・削除 0・統合 0)。

## 0.5.0

**`builtin cd <main>; git reset --hard` のように `builtin` を前に付けた形を読み、別 checkout への git の書き込みを止めるようにした
(minor bump)。**

### 修正: `builtin` を前に付けた組み込みを読む

`builtin cd <dir>` は bash でも zsh でも今の shell の `cd` で移動が残るのに、`builtin` を一般のコマンドとして読んでいたので、
後ろの git を元のディレクトリで動くものとして判定していた (0.4.0 まで)。`builtin` の後ろの `cd` / `export` / `unset` /
`true` / `false` / `:` を、`builtin` の無い形と同じに読む。`builtin cd` は、次の形だけを読む: 引数なし、または行き先の語が 1 つで
shell が展開しないもの (`~` と `~/...` は読む)。option は `-L` / `-P` / `--` だけ。`builtin export` / `builtin unset` は、option と
`$` を含む語が無いときだけ読む。

次の形は、bash 3.2 と `zsh -f` で偽の git を走らせた実測に合わせて読まない (これまでの読みのまま):

- 外部プロセスで動く形: `env builtin cd` / `nohup builtin cd` / `sudo builtin cd` / `exec builtin cd` (macOS の外部コマンド
  `builtin` が子プロセスで走るので移動は残らない)。`time time builtin cd` も同じ。先頭の `time builtin cd` は今の shell で動くので読む
- shell によって読みが割れる形: `command builtin cd` / `builtin command cd` / `builtin -- cd` / `builtin - cd` / `builtin chdir` /
  `time -p builtin cd` (bash の `time -p` は今の shell で動き、zsh は `-p` をコマンドとして起動する) / `builtin cd -q` /
  `builtin cd <a> <b>` (zsh は置換として読んで失敗する)
- どちらの shell でも走らない形: `builtin git` / `builtin env git` など (組み込みが無い)
- `builtin popd`: スタックが空だと失敗して移動しない形を、読むと止められなくなる
- `builtin pushd`: 対の `builtin popd` を読まないので、読むと戻り先を取り違える。`builtin pushd <main>; git reset --hard` は見逃す
- 移動しない形と行き先の分からない形: `builtin cd +1` / `-1` / `-` / `-x <dir>` / `-e <dir>`、`$` / `` ` `` / glob / `{` を含む語
- 変数の扱いが違う形: `builtin unset -f GIT_DIR` (関数を消すだけ)、`builtin export -x` / `+x` / `-n`、`builtin export GIT_DIR=$x`

`cd <main>; builtin cd <worktree>; git reset --hard` のように `builtin` で自分の worktree へ戻る形は、実際には自分の worktree で
走るので、誤って止めなくなる (実測で確認)。

次の形は読みが実際と違うが、受け入れている:

- `\time builtin cd` / `'time' builtin cd`: 外部の `time` で動くが、shlex が引用を外すので keyword の `time` と区別できず、今の shell で
  動くものとして読む
- bash 3.2 の `! time builtin cd` / `if time builtin cd`: 外部の `time` になるが、Bash tool の zsh に合わせて今の shell で動くものとして読む
- `builtin cd <dir>/missing` や開けない redirect の付いた `builtin cd` など失敗する形、`builtin cd -` の行き先、`HOME=` を書き換えた
  後の `builtin cd`: `builtin` の無い `cd` と同じ読み (`cd` は成功するものとして扱う)
- 引用した行き先の `[` / `{` を含むパス (`builtin cd "a[1]"`): shlex が引用を外すので glob と区別できず、読まない
- `builtin cd <dir> >/dev/null extra` (redirect の後ろの語を数えない): zsh だけ失敗する形で、読む

### テストの追加

- `tests/test_builtin_prefix.py`: 上の読みごとの判定

テスト件数: 93 → 104 (追加 11・削除 0・統合 0)。

## 0.4.0

**pipeline の最後の要素の `cd` (`printf a | cd <main> && git commit -m x`) が残った場合も判定し、後ろの別 checkout への
git の書き込みを止めるようにした (minor bump)。**

### 修正: pipeline の最後の要素の cd を両方の読みで追う

zsh (Claude Code の Bash tool の shell) は pipeline の最後の要素を今の shell で動かすので、そこでの `cd` / `pushd` は
後ろのコマンドに残る。`printf a | cd <main> && git commit -m x` や `cat <<EOF | cd <main> && git add .` の git は、zsh では
main checkout で走るのに止まっていなかった (pipeline の要素をすべてサブシェルとして扱っていた。0.3.0 まで)。bash は最後の
要素もサブシェルで動かすので残らない。

最後の要素の `cd` は、残った場合 (zsh) と残らない場合 (bash) の両方を追い、どちらかで別 checkout への書き込みになれば
止める。`printf a | { cd <main>; git commit -m x; }` のように、最後の要素の brace group の中で cd の後ろに書いた git
(どちらの shell でも移動先で走る) も止まる。pipeline の途中の要素の `cd` (`printf a | cd <main> | cat`) は、どちらの shell
でもサブシェルで動くので、これまでどおり外に残らないものとして扱う。

`|` の直後で改行して続けた pipeline (`printf a |⏎ cd <dir>`) も、改行の後ろの要素を最後の要素として読む (改行で
pipeline が終わったとみなし、最後の要素の `cd` を今の shell の `cd` として読んでいた。0.3.0 まで)。

### テストの追加

- `tests/test_guard.py`: 最後の要素の `cd` / `pushd` / `|&` / brace group で止まること、途中の要素の `cd` は残らない
  こと、最後の要素で自分の worktree へ戻る `cd` は bash の読みで main のまま判定すること、読み取りを止めないこと

テスト件数: 92 → 93 (追加 1・削除 0・統合 0)。

確認:

- 判定の比較 (14,400 件: 前置き 4 × pipeline の左 7 × `|` / `|&` × 最後の要素 24 形 × 後置き 10 + 途中の要素 960) を
  0.3.0 と 0.4.0 に流し、偽の git で zsh -f と bash 3.2 の実際の起動と並べた。0.3.0 で止めて 0.4.0 で止めないものは 0 件。
  0.3.0 で止めず 0.4.0 で止めるものは 3,723 件 (加えて注意だけ出すものが 198 件)。止める側の差は、zsh で別 checkout への
  書き込みが実際に起動するものと、パイプでない同じ形を 0.3.0 も止めていたもの (`env cd` / `nohup cd` / `sudo cd` /
  `command cd` / `cd -e`、途中の要素の brace group) だけ

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
