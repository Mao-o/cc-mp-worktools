# Changelog

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
