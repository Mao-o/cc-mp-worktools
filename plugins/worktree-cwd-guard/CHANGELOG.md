# Changelog

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
- 行き先が静的に決まらない操作は止めずに注意だけ出す。README / SECURITY.md に脅威モデル
  (うっかりの予防で、意図的なすり抜けへの対策ではない) と、読まない書き方 (`bash -c` / `eval` /
  関数 / script ファイル / コマンド置換) を初版から明記した
- `WORKTREE_CWD_GUARD_MODE` (enforce / warn / off) と `WORKTREE_CWD_GUARD_ALLOW` で調整できる
