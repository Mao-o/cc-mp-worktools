# worktree-cwd-guard

`git worktree` で隔離した作業ディレクトリ (linked worktree) で動く Claude Code の session が、
**同じ repo の別の checkout** (main 側や他の worktree) を書き換えるのを止める PreToolUse hook です。

複数の agent を worktree で並列に動かすと、次のような事故が起きます。

- `cd <main 側> && git checkout -b ...` のような複合コマンドで、自分の worktree ではなく
  main 側の branch を切り替えてしまう
- 再開した agent の作業ディレクトリが repo の root に戻り、別の lane の worktree に書き込む
- 別の worktree のファイルを、自分の worktree のファイルだと思って編集する

## 目的と限界

**目的はうっかりの予防で、意図的なすり抜けを防ぐ仕組みではありません。**

- 行き先が変数 (`cd "$DIR"` / `git -C "$REPO"`) などで静的に決まらない操作は、止めずに注意だけ
  出します。誤検知で agent の作業が止まるのを避けるためです
- Bash の `>` / `cp` / `mv` / `rm` などによるファイル書き込みは見ません (git の書き込み操作と
  Write / Edit だけが対象です)
- session の作業ディレクトリが main checkout に戻ってしまった場合 (上の 2 つ目の事故の
  一部) は、その時点で「自分の worktree」が分からないため検知できません
- 次の書き方は中身を読まないため対象外です (通常の agent の git 操作では使われない形):
  - `bash -c "..."` / `sh -c "..."` / `eval "..."` の文字列の中の git
  - shell 関数・alias・script ファイル (`./deploy.sh` など) の中の git
  - `"$(...)"` や `` `...` `` の中の git
  - `for` / `case` などの制御構文で作業ディレクトリが変わる場合
- 次の書き方は、shell と読みがずれることがあります。別の checkout への書き込みを見逃すことも、
  書き込まない操作を止めることもあります (通常の agent の git 操作では使われない形):
  - コマンド語や行き先より前に置いたリダイレクト (`>/dev/null git -C <dir> ...` / `cd >/dev/null <dir>`) と、
    閉じ括弧の直後のリダイレクト (`(cd <dir>)>/dev/null; git ...` は止めすぎます)
  - 成否の細かい読み。`exit` (`cd <dir> && git log && cd <own> || exit 1; git add .` は書き込まないのに止めます)、
    wrapper の後ろの `:` (`env : || git -C <dir> ...`)、`time ( ... )` / `if ( ... )` の成否、パイプの途中の `!`
    (`! printf a | cd <dir> || git ...`) は読みません
  - ディレクトリスタックの操作: `cd -`、`popd --` / `popd +0` は行き先を決めず注意だけにします。
    `pushd +N` の後の `popd` は行き先を取り違えます
  - 波括弧・`if`・サブシェルを `&` やパイプでつないだ形 (`{ cd <dir>; } & git ...` は止めすぎます。
    `cd <dir>; cd <own> && { true; } & git ...` と `cd <dir>; true | if true; then cd <own>; fi; git ...` は見逃します)、
    `&&` / `||` の直後の改行 (`false &&` + 改行 + `git -C <dir> ...` は止めすぎます)、
    zsh だけが受け付ける形 (`false && ; cd <dir>`)
  - 環境変数 `GIT_DIR` / `GIT_WORK_TREE` の値は、実行の分岐ごとには持ちません。`cd` と成否は
    `&&` / `||` の分岐ごとに追いますが、環境変数は全体で 1 つで、`export` / `unset` / 代入を出てきた順に
    反映します。分岐の片方やパイプの要素の中だけで変わる形 (`test -f x && unset GIT_DIR; git reset` /
    `git status || unset GIT_DIR; git add .` / `unset GIT_DIR | cat; git reset --hard`) は、
    どの経路でも変わったものとして読みます
  - `export` 以外の書き方で環境変数を渡す形 (`declare -x` / `typeset -x` / `set -a` /
    `GIT_DIR=<dir>/.git export GIT_DIR` / `builtin export`)、`unset -f GIT_DIR`、`GIT_DIR` / `GIT_WORK_TREE`
    以外の環境変数 (`GIT_INDEX_FILE=<dir>/.git/index git add .`)、綴りの違う git dir
    (大文字小文字を区別しない FS での `<dir>/.GIT`)
  - backtick の中の改行で始めた here-document (`` echo `cat <<E `` + 改行 + `` `; git -C <dir> ... `` + 改行 + `E`)、
    語の途中の `#` (`x=(a b)#c ; git -C <dir> ...`)、bash 3.2 だけが走らせる形
    (`git commit -m "$(cat <<'EOF'` + 改行 + `msg` + 改行 + `)" && git -C <dir> ...` + 改行 + `EOF`)
  - 一覧に無い wrapper の後ろの git (`nice git -C <dir> ...` / `find . -exec git --git-dir=<dir>/.git add . \;`)
  - 行き先の一部だけが変数の形。`git --git-dir=<dir>/.git --work-tree=$X add .` は、ほかに分かる行き先があっても
    注意だけにし、`git -C $Y --work-tree=<dir> ...` は注意も出しません
- shell が構文エラーにする書き方 (閉じない引用符・括弧、"$(...)" の中の comment など) を含む行から後ろは読みません。
  ただし、閉じない括弧・引用符・backtick か、終端行の無い here-document が残ったときは、読みが shell とずれている
  ことがあるため、comment や行の継続を整えない旧来の読みと、終端行の無い here-document を here-document と読まない
  読みでも判定し、どれかで別 checkout への書き込みが見つかれば止めます (止める側に倒すため、実際には何も実行されない
  壊れたコマンドを止めることがあります)

## インストール

```
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install worktree-cwd-guard@mao-worktools
```

## 動作する条件

hook の入力に含まれる作業ディレクトリ (`cwd`) が **linked worktree の中**のときだけ動きます。
main checkout や repo の外で動く session では何もしません。

「自分の worktree」はその linked worktree の root、「別の checkout」は `git worktree list` に
並ぶ他の root (main checkout を含む) です。worktree が main checkout の中に置かれている
(`.claude/worktrees/<name>` など) 場合も、パスは最も深い root に属するものとして判定します。

## 止める操作

| ツール | 止める条件 |
|---|---|
| Bash | 別の checkout を対象にした git の書き込み操作。対象は `cd` / `chdir` / `pushd` / `popd`、`git -C <dir>` / `--work-tree` / `--git-dir`、環境変数 `GIT_DIR` / `GIT_WORK_TREE` (コマンド前の代入・`env`・`export`。`env -i` / `env -u` による削除も反映) から決める。`--git-dir` / `GIT_DIR` には各 checkout の `<root>/.git` と `.git/worktrees/<name>` のどちらの形も読む。`--git-dir` / `GIT_DIR` だけが効いて作業ツリーの指定が無いときは、作業ツリーは今の場所になるので、git dir の checkout と今の場所の両方を見る (`GIT_DIR=<自分の worktree>/.git git -C <main> reset --hard` は main の作業ツリーを上書きする)。`git add` / `git commit` のように作業ツリーを書かない操作も同じに読む。`&&` / `||` は直前のコマンドの成否ごとに状態を分けて追う (`cd` は成功するものとして扱う。ただし `cd` / `pushd` / `popd` などの直後の `||` の右辺は、移動が失敗してその場に残った場合も判定する。右辺の後ろの git は移動が成功したものとして読む)。`&` は、bash では AND-OR list 全体を、zsh では最後の pipeline だけをサブシェルで動かすものとして扱う (`cd <main> && true & git ...` の `cd` は zsh では今の shell に残る)。pipeline の途中の要素や background で動く `cd` は外に残らないものとして扱う。shell で動きが割れる形は、コマンド全体を zsh の読みと bash の読みの両方で判定し、どちらかで別 checkout への書き込みになれば止める (pipeline の最後の要素の `cd` は zsh だけ残る。`&` の手前の `cd <dir> &&` も zsh だけ残る。`command cd` は bash だけ移動する。`chdir` / `cd -q` / `pushd -q` は zsh だけ移動する。`pushd -n` はどちらも移動しない)。`env` / `nohup` / `sudo` / `exec` の後ろの `cd` は子プロセスで走るので移動として読まない。`! <コマンド>` は成否を読まず、続く `&&` / `||` の後ろはどちらも実行しうるとして読む。書き込み操作 = `checkout` `switch` `commit` `reset` `restore` `stash` (`stash list` / `show` / `create` を除く) `add` `rm` `mv` `merge` `rebase` `cherry-pick` `revert` `pull` `am` `apply` (`--check` / `--stat` / `--numstat` / `--summary` だけで `--apply` を伴わないものを除く) `bisect` `clean` `checkout-index` `update-index`、`branch -m` / `-M` (checkout 中の branch の改名)、`submodule update` / `add` / `deinit` など、`sparse-checkout set` / `add` / `init` など。here-document の本文 (`cat > x.sh <<'EOF'` の中身) は実行されないので読まない。行末の comment (`# ...`) は改行の手前までを読み捨て、`\` + 改行で折り返した行は 1 行につないで読む (どちらも bash / zsh と同じ読み)。`env -C <dir>` のように wrapper が作業ディレクトリを変える形と、`&&` / `||` の後ろで実行されないかもしれない `cd` (移動した場合としない場合の両方を判定する) も追う。`builtin cd <dir>` は `cd` と同じに読む (`env builtin cd` のように外部プロセスで動く形は移動が残らないので読まない。`builtin pushd` / `builtin popd` と、行き先が決まらない `builtin cd` も読まない)。Windows では、行き先の `/c/...` を Git Bash の読み (`/c/x` は `C:\x`、`/c` は `C:\`。Git Bash の pwd もこの形を返す) と Windows の読み (今のドライブの `\c\x`) の両方で判定し、どちらかで別 checkout なら止める |
| Bash | `git worktree remove` / `move` で別の worktree を対象にしたもの。相対パスは、git dir・作業ツリー・今の場所のどの基点でも別の worktree なら止める。git はパスとして読む前に worktree の名前 (パスの末尾。`b` / `worktrees/b` / `.claude/worktrees/b`) としても読むので、名前に一致する worktree (自分の worktree と main checkout を含む) がちょうど 1 つで、それが別の worktree のときも止める (2 つ以上に一致すると、git と同じくパスとして読む。git が読む repo がこの repo のときだけ。大文字小文字を区別する比べ方と区別しない比べ方のどちらかで決まれば止める) |
| Write / Edit / MultiEdit / NotebookEdit | 別の checkout 配下のファイルへの書き込み |

止めないもの:

- `git log` / `git diff` / `git status` / `git stash list` / `git apply --check` などの読み取り
- 自分の worktree の中での操作
- repo の外 (`/tmp`、ホームの設定ファイルなど) への書き込み
- `git worktree add` (新しい worktree を作るのは別の checkout の書き換えではない)

## 設定

| 環境変数 | 既定 | 内容 |
|---|---|---|
| `WORKTREE_CWD_GUARD_MODE` | `enforce` | `warn` = 止めずに伝える / `off` = 何もしない |
| `WORKTREE_CWD_GUARD_ALLOW` | (空) | 書き込みを許す checkout の root。複数は OS のパス区切り (`:` / Windows は `;`) で並べる |

supervisor 役の session が worktree の中から意図して main 側を操作する場合は、
`WORKTREE_CWD_GUARD_ALLOW` に main checkout の root を入れてください。

## 例

```text
[worktree-cwd-guard] 別の checkout を書き換える操作を止めた。
この session の worktree: /path/to/repo/.claude/worktrees/lane-a
- `git checkout` が別の checkout (/path/to/repo) を対象にしている
自分の worktree の中で作業する。意図した操作なら、対象の checkout を WORKTREE_CWD_GUARD_ALLOW に加えるか WORKTREE_CWD_GUARD_MODE=warn で実行する。
```

## 外部送信

ありません。ローカルの `git rev-parse` / `git worktree list` を起動するだけです
(git を含まない Bash コマンドや main checkout の session では git も起動しません)。

## 要件

- Python 3.11+ / git
