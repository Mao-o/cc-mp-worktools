# worktree-cwd-guard — エージェント向けメモ

## Code Review Rules

この plugin は**並列 agent のうっかりした書き換えを予防する**ための PreToolUse hook で、
意図的なすり抜けを防ぐセキュリティ境界ではない。レビューはこの脅威モデルに沿って行うこと
(README の「目的と限界」、repo root の SECURITY.md も参照)。

### 指摘すべきもの

- agent が普通に書く形のコマンドで、別の checkout への git の書き込み操作や Write / Edit が
  **止まらずに通る**もの。例: `cd <dir> && git ...`、`git -C <dir> ...`、`--work-tree` /
  `--git-dir`、`GIT_DIR` / `GIT_WORK_TREE` 環境変数、`pushd` / `popd`、`env` / `sudo` などの
  wrapper を前に付けた形
- 自分の worktree の中の操作や読み取り (`git log` / `git diff` / `git status`) を**誤って止める**もの。
  誤検知は agent の作業を止めるため、見逃しと同じくらい重い
- hook がクラッシュする・timeout を超える・stdin / stdout の文字コードで壊れるなど、判定そのものが
  動かなくなるもの

### 指摘しないもの (対象外として README に明記済み)

- `bash -c` / `sh -c` / `eval` に渡した文字列の中の git
- shell 関数・alias・script ファイル (`./deploy.sh` など) の中の git
- `"$(...)"` や `` `...` `` の中の git、変数で組み立てたコマンド
- `for` / `case` などの制御構文で作業ディレクトリが変わる場合
- Bash の `>` / `cp` / `mv` / `rm` などによるファイル書き込み (git の書き込み操作と
  Write / Edit だけが対象)
- 行き先が変数で決まり静的に解決できない操作を、止めずに注意だけ出すこと (設計どおり)
- 利用者が環境変数 (`WORKTREE_CWD_GUARD_MODE` / `WORKTREE_CWD_GUARD_ALLOW`) で無効化・緩和できること

上の書き方で「すり抜けられる」ことは、この plugin にとって不具合ではない。対象を広げる提案は
P1 ではなく、別 issue で扱う改善提案として P3 までに留めること。

### 優先度の目安

- P1: 上の「指摘すべきもの」のうち、通常の書き方で別 checkout の書き込みが通る / 自分の
  worktree の操作が止まる / hook が動かなくなる
- P2: 判定は正しいが、deny / 注意のメッセージが誤っていて agent が対処を誤る
- P3: 対象を広げる提案、可読性・保守性
