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
| Bash | 別の checkout を対象にした git の書き込み操作。対象は `cd` / `pushd` / `popd`、`git -C <dir>` / `--work-tree` / `--git-dir`、環境変数 `GIT_DIR` / `GIT_WORK_TREE` (コマンド前の代入・`env`・`export`。`env -i` / `env -u` による削除も反映) から決める。`&&` / `||` は直前のコマンドの成否ごとに状態を分けて追う (`cd` は成功するものとして扱う)。`&` の付いた AND-OR list 全体はサブシェルとして扱う。pipeline の途中の要素や `&` の中の `cd` は外に残らないものとして扱う。pipeline の最後の要素の `cd` (`printf a \| cd <dir> && git commit`) は、zsh では残り bash では残らないので、残った場合と残らない場合の両方を判定する。書き込み操作 = `checkout` `switch` `commit` `reset` `restore` `stash` (`stash list` / `show` / `create` を除く) `add` `rm` `mv` `merge` `rebase` `cherry-pick` `revert` `pull` `am` `apply` (`--check` / `--stat` / `--numstat` / `--summary` だけで `--apply` を伴わないものを除く) `bisect` `clean` `checkout-index` `update-index`、`branch -m` / `-M` (checkout 中の branch の改名)、`submodule update` / `add` / `deinit` など、`sparse-checkout set` / `add` / `init` など。here-document の本文 (`cat > x.sh <<'EOF'` の中身) は実行されないので読まない。行末の comment (`# ...`) は改行の手前までを読み捨て、`\` + 改行で折り返した行は 1 行につないで読む (どちらも bash / zsh と同じ読み)。`env -C <dir>` のように wrapper が作業ディレクトリを変える形と、`&&` / `||` の後ろで実行されないかもしれない `cd` (移動した場合としない場合の両方を判定する) も追う。`builtin cd <dir>` は `cd` と同じに読む (`env builtin cd` のように外部プロセスで動く形は移動が残らないので読まない) |
| Bash | `git worktree remove` / `move` で別の worktree を対象にしたもの |
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
- `git checkout` が別の checkout (/path/to/repo) を書き換える
自分の worktree の中で作業する。意図した操作なら、対象の checkout を WORKTREE_CWD_GUARD_ALLOW に加えるか WORKTREE_CWD_GUARD_MODE=warn で実行する。
```

## 外部送信

ありません。ローカルの `git rev-parse` / `git worktree list` を起動するだけです
(git を含まない Bash コマンドや main checkout の session では git も起動しません)。

## 要件

- Python 3.11+ / git
