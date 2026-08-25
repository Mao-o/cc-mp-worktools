# D16: 透過 wrapper × インライン env 伝播の監査

このドキュメントは v0.7.0 (D11) で導入した「行頭インライン env を検証 subprocess に
伝播する」設計について、**透過 wrapper のランタイム semantics と静的解析の乖離**を
網羅的に監査した結果を記録する。コードの分類宣言は
`core/command_parser.py` の `_WRAPPER_ENV_CLASS`、回帰テストは
`tests/test_command_parser.py` の `TestWrapperEnvClassificationGuard` /
`TestWrapperEnvPropagationContract` に対応する。

実装者ガイド本体は `CLAUDE.local.md` (gitignore 済みのため worktree には来ない)。
本ファイルは公開 repo に追跡される監査記録。

## 背景: なぜ監査したか

D11 は「静的に解析した行頭インライン env = コマンド実行時の env」という前提で、
剥がした `AWS_PROFILE=prod` 等を検証 subprocess (`aws sts get-caller-identity`
など) に渡す。この前提は **透過 wrapper を跨ぐと崩れる**ことが PR #33 の Codex
レビューで連続して露呈した:

- round1: 複合コマンドの per-service 集約で後段 profile を検証せず誤 allow (c542c18)
- round2: 透過 wrapper 跨ぎの env override 漏れ (c731faf, inner-wins)
- round3 / 8zr: `sudo` が `-E` 無しに継承 env を **scrub** する挙動を未考慮
  → 「検証は prod / 実行は別アカウント」の false-allow (cd13724, `_sudo_preserves_env`)

懸念は **whack-a-mole 化** — wrapper を足すたびに env 挙動の穴が出るのではないか。
本監査でリスト全体を体系化し、再発防止の guard を入れる。

## wrapper × env 挙動の体系化

判定軸: **pre-wrapper の行頭インライン env が、後続コマンドの実行環境に実際に届くか**。

| wrapper | 実行時の env 挙動 | parser の扱い | 伝播可否 | 根拠 |
|---|---|---|---|---|
| `sudo` (preserve 無し) | 継承 env を **scrub** (root の安全環境にリセット) | pre-sudo env を **破棄** (8zr) | **不可** → 破棄 | 実機: `PROBE=x sudo env` に PROBE 出ない (sudoers env_reset 既定) |
| `sudo -E` / `--preserve-env` / `--preserve-env=LIST` | 継承 env を保持 (LIST 形式は一部) | pre-sudo env を収集 | 可 | `_sudo_preserves_env` が flag 領域を走査 |
| `time` (shell keyword / `/usr/bin/time`) | 透過 (子プロセスは env 継承) | pre-wrapper env を収集 | 可 | 実機: `PROBE=x time env` / `/usr/bin/time env` に PROBE 出る |
| `nohup` | 透過 (SIGHUP 無視のみ、env は継承) | 収集 | 可 | 実機確認 |
| `command` | 透過 (shell builtin、外部 CLI を env 継承で起動) | 収集 | 可 | 実機: `PROBE=x command env` に PROBE 出る |
| `builtin` | shell builtin **のみ**起動 (外部 CLI を起動しない) | 収集 (無害) | 可 (無害) | service の PATTERNS は外部 CLI のみ match するため `builtin <cli>` は実質発生しない |
| `exec` | 透過 (現プロセスを置換、env 継承) | 収集 | 可 | 実機: `bash -c 'exec env'` に PROBE 出る |
| `npx` | 透過 (node を env 継承で起動 → 対象を起動) | 収集 | 可 | 実機: `PROBE=x npx node -e ...` に PROBE 届く |
| `pnpm exec` / `pnpm dlx` | 透過 (対象を env 継承で起動) | 収集 | 可 | node ランチャ semantics (env filter なし) |
| `mise exec --` | 透過 (mise の env 注入 **に加えて** 継承 env も渡す) | 収集 | 可 | 実機: `PROBE=x mise exec -- env` に PROBE 出る |
| `bun x` | 透過 (対象を env 継承で起動) | 収集 | 可 | bun ランチャ semantics |
| `env` (オプション無し: `env FOO=bar cmd`) | 継承 env + 指定 `KEY=VAL` を渡す | 剥がして collect (pre/post 両方) | 可 | POSIX env |
| `env -i [...]` | 環境を **空にリセット**してから起動 | **剥がさない** (opaque) | 不可 → スキップ | 実機: `PROBE=x env -i env` に PROBE 出ない |
| `env -u NAME [...]` | NAME のみ **unset** | **剥がさない** (opaque) | 不可 → スキップ | POSIX env |
| `env -- [...]` | flag 終端 (以降を素の env で起動) | **剥がさない** (opaque) | スキップ (安全側) | parser は `-` 始まりトークンで env strip を中止 |
| `timeout DURATION cmd` | 透過 (env 継承) | 収集。DURATION も消費 | 可 | GNU coreutils timeout(1) |
| `nice [-n N] cmd` | 透過 (env 継承) | 収集 | 可 | ローカル man page nice(1) |
| `stdbuf -o L cmd` | 透過 (env 継承。`LD_PRELOAD` 系を**追加**するだけ) | 収集 | 可 | ローカル man page stdbuf(1) |
| `setsid cmd` | 透過 (新セッションで起動、env は継承) | 収集 | 可 | util-linux setsid(1) |
| `caffeinate [-disu] cmd` | 透過 (env 継承) | 収集 | 可 | ローカル man page caffeinate(8) |
| `watch -n N cmd` | 透過 (`sh -c` 経由で繰り返し起動、env は継承) | 収集 | 可 | procps-ng watch(1) |
| `xargs [opts] cmd` | 透過 (env 継承。引数のみ stdin 由来) | 収集 | 可 | ローカル man page xargs(1) |

(実機 probe は darwin / bash / mise 2.x / node 25 で 2026-06 に確認。詳細は本監査の
コミットメッセージ参照。)

### v0.9.0 追加分の根拠と未確認事項

上表の下 7 行は v0.9.0 で透過 wrapper に追加した。`nice` / `stdbuf` /
`caffeinate` / `xargs` は**開発機にインストール済みの man page から option 表を
逐語確認**した。`timeout` / `setsid` / `watch` は開発機 (darwin) に man page が
無く、GNU coreutils / util-linux / procps-ng の公式リファレンス記述に基づく。

option 表を取り違えた場合の劣化方向は**片側だけ**である点に注意:

- 値を取る flag の登録漏れ → 値トークンで剥がしが止まり、セグメントが不透明なまま
  残る = **v0.8.0 と同じ「検証スキップ」に戻るだけ**
- bool flag を値付きとして誤登録 → 次のトークン (CLI 名) を食って同じく検証スキップ

どちらも誤 deny にはならず、既存の lenient 方針の範囲に収まる。逆に言えば
**取り違えても静かに検証が消える**ので、`watch` / `timeout` / `setsid` を実際に
使う環境で挙動が怪しいときは `_WRAPPER_FLAGS_WITH_VALUE` の該当エントリを
実機の `--help` と突き合わせること。

`ionice` は「cloud CLI と組み合わせる現実的な形が確認できず、開発機で man page も
参照できない」ため**追加していない** (推測で allow-list を広げない方針)。

## 伝播してよい env / 保守的にスキップすべき経路の方針

### 1. passthrough wrapper — 伝播してよい

`time` / `nohup` / `command` / `builtin` / `exec` / `npx` / `pnpm exec` /
`pnpm dlx` / `mise exec --` / `bun x` は継承 env を素通すため、pre-wrapper の
インライン env を収集して検証 subprocess に渡す (D11 本来の意図どおり)。
`_WRAPPER_ENV_CLASS` で `"passthrough"` と宣言する。

### 2. conditional_scrub wrapper — フラグ依存、既定はスキップ側

`sudo` のみ。`-E` / `--preserve-env` / `--preserve-env=LIST` があれば継承 env を
保持するので伝播してよいが、無ければ scrub するので **pre-sudo env を破棄する**
(8zr の `_normalize_segment` 補正)。`--preserve-env=LIST` のリスト内容や sudoers の
`env_keep` / `env_reset` まで静的には不可知なので、**preserve 指定があれば保守的に
伝播を許す** (保持しすぎ方向は誤 deny を増やすだけで安全側)。

### 3. env のリセット系 — wrapper として剥がさない (opaque)

`env -i` / `env -u NAME` / `env --` は **透過 wrapper として剥がさない**。剥がして
後続を裸のコマンド扱いにすると「実行は空/縮小環境 / 検証は親環境」の非対称が生じ、
本来 deny すべきケースを誤って検証スキップしてしまう。剥がさず opaque のまま残すと、
セグメント (`env -i aws ...`) は service の PATTERNS (`^aws\b`) に match せず、検証
自体がスキップされる。これは「静的解析不能なら検証しない (= allow 相当)」という
既存ポリシー (`bash -c`/`eval` と同じ) に合致する。

### スキップに倒すと安全側である理由 (誤 deny 回避ポリシーとの整合)

verify-cloud-account の方針は **誤 allow より誤 deny を回避** (ログイン済みなら通す)。
env を伝播しない方向に倒したとき:

- **env を破棄 (sudo scrub)**: 検証はデフォルト env で走る。
  - ログイン済み & デフォルト env でアカウント一致 → allow (誤 deny にならない)
  - 不一致 / 未ログイン → deny (安全側。false-allow を防ぐ本来の目的)
- **検証スキップ (env -i 等)**: そのセグメントは検証されず素通り (allow 相当)。
  - これは「静的解析できない経路は検証しない」既存ポリシーと同じ譲歩。env -i で
    環境を空にして cloud CLI を叩く運用は稀で、叩いても CLI 側が credentials を
    見つけられず失敗するため実害は限定的。

いずれも **「検証 env = 実行 env」を保てない経路では、誤った env で検証を通すより
保守的に倒す**という一貫した判断。誤 allow (未承認アカウントで mutating 実行) だけは
確実に避ける。

## 結論: 現リストは健全。再設計は不要

8zr の `sudo` scrub 補正で、現行 wrapper リストの env 挙動はすべて正しく分類・処理
されている。**sudo が唯一の conditional_scrub であり、env -i/-u/-- が唯一の reset
形式で、どちらも対応済み**。残る passthrough wrapper は実機で env 素通しを確認済み。

過剰な再設計 (例: 全 wrapper を allow-list 化 / 汎用 scrub 検出) は誤 deny を増やす
リスクがあり、誤 deny 回避ポリシーに反するため **採用しない**。代わりに、再発防止の
**分類 guard** (`_WRAPPER_ENV_CLASS` + テスト) を入れて、将来の wrapper 追加時に
env 挙動の分類を機械的に強制する。

## wrapper flag 表の全件監査 (v0.9.0 / PR #48 Codex P1 x2 を受けて)

env 挙動とは**別軸**の監査。「その flag が次の token を消費するか」を取り違えると、
コマンド本体を見失って**検証がまるごと消える**。実際に 2 件の取り違えが
**逆方向に 1 つずつ**出た (optional を必須扱い / 必須を bool 扱い)。

### 3 分類

| 分類 | 挙動 | 実装 |
|---|---|---|
| 値を取らない | 次の token を消費しない | どちらの集合にも載せない |
| **必須**引数 | 分離形も `=` 形も値を取る | `_WRAPPER_FLAGS_WITH_VALUE` |
| **optional** 引数 | **`=` 形 (long) / 引っ付け形 (short) でのみ**値を取る。bare 形は消費しない | `_WRAPPER_FLAGS_OPTIONAL_VALUE` |

`--key=value` 形と短縮連結形 (`-oL` / `-I{}`) は**登録の有無に関わらず 1 トークンで
消費される**ため、登録が要るのは「分離形で値を取る flag」だけ。

### 監査結果 (出所付き)

`[local]` = 開発機の man page を逐語確認 / `[ref]` = 上流の公式リファレンス記述のみ。

| wrapper | 必須引数として登録 | 出所 | 備考 |
|---|---|---|---|
| `sudo` | `-u -g -U -p -C -D -h -r -t -T -R -a` + long 形 | [local] sudo(8) | `-C num` `-D dir` `-g group` `-h host` `-p prompt` `-R dir` `-T timeout` `-u user` `-U user` を synopsis で確認。`-a`/`-r`/`-t` は Linux 版 |
| `time` | `-o` (+ GNU `-f`) | [local] time(1) | `time [-al] [-h \| -p] [-o file] utility` |
| `exec` | `-a` | [local] bash(1) | `exec [-cl] [-a name] [command [arguments]]` |
| `command` | (なし) | [local] bash(1) | `command [-pVv]` — 値を取る flag は無い。`-v`/`-V` は別扱い (存在確認) |
| `nice` | `-n` (+ GNU `--adjustment`) | [local] nice(1) | `nice [-n increment] utility` |
| `stdbuf` | `-i -o -e` + long 形 | [local] stdbuf(1) | `stdbuf [-e bufdef] [-i bufdef] [-o bufdef] [command]`。bufdef は `L`/`B` 等**非数値** |
| `caffeinate` | `-t -w` | [local] caffeinate(8) | `caffeinate [-disu] [-t timeout] [-w pid] [utility ...]` |
| `xargs` | `-E -I -J -L -n -P -R -S -s` | [local] xargs(1) | BSD/macOS man が**分離形で値を取ると明記**している短縮形のみ |
| `timeout` | `-s --signal -k --kill-after` | [ref] GNU coreutils | 開発機に man 無し。SIGNAL は非数値 (`KILL`) なので登録が必須 |
| `npx` | `-p --package -c --call --node-options --node-arg` | [ref] npx | 開発機に npx 無し。**v0.8.0 から存在**するので削除すると検証が新たに失われる |
| `watch` | **(空)** | — | 開発機に man 無し。値付き option の値は**すべて数値**なので下の安全網に委ねる |
| `setsid` | (なし) | [ref] util-linux | `-c/--ctty` `-f/--fork` `-w/--wait` のみで値を取る flag が無い |

**optional 引数として登録**: `xargs` の `--replace` / `--eof` / `--max-lines`
([ref] GNU findutils: `--replace[=R]` / `--eof[=eof-str]` / `--max-lines[=max-lines]`)。
短縮の `-i[R]` / `-l[N]` は引っ付け形でのみ値を取るので bool 扱いで正しい。

### xargs の全 option 分類 (GNU + BSD)

xargs だけは**完全列挙**する。piecemeal に足すと同じ穴が繰り返し出るため。
**値が数値か非数値かを併記**するのが要点 — 数値は `_ARG_LIKE_RE` の安全網が拾うので
登録漏れが致命傷にならないが、**非数値は安全網が効かないので登録が必須**。

| option | 分類 | 値 | 出所 |
|---|---|---|---|
| `-0` / `--null` | 値なし | — | [local] xargs(1) |
| `-a FILE` / `--arg-file=FILE` | **必須** | **非数値** | [ref] GNU `xargs --help` 逐語 (外部レビュー環境で確認) |
| `-d CHAR` / `--delimiter=CHARACTER` | **必須** | **非数値** | [ref] 同上 |
| `-E END` | **必須** | **非数値** | [local] xargs(1) |
| `-e[END]` / `--eof[=END]` | optional | 非数値 | [ref] GNU findutils |
| `-I R` | **必須** | **非数値** | [local] xargs(1) |
| `-i[R]` / `--replace[=R]` | optional | 非数値 | [ref] GNU findutils |
| `-J replstr` | **必須** | **非数値** | [local] xargs(1) (BSD 固有) |
| `-L N` | **必須** | 数値 | [local] xargs(1) |
| `-l[N]` / `--max-lines[=N]` | optional | 数値 | [ref] GNU findutils |
| `-n N` / `--max-args=N` | **必須** | 数値 | [local] xargs(1) |
| `-P N` / `--max-procs=N` | **必須** | 数値 | [local] xargs(1) |
| `-p` / `--interactive` | 値なし | — | [local] xargs(1) |
| `--process-slot-var=VAR` | **必須** | **非数値** | [ref] GNU findutils |
| `-R replacements` | **必須** | 数値 | [local] xargs(1) (BSD 固有) |
| `-r` / `--no-run-if-empty` | 値なし | — | [local] xargs(1) |
| `-S replsize` | **必須** | 数値 | [local] xargs(1) (BSD 固有) |
| `-s N` / `--max-chars=N` | **必須** | 数値 | [local] xargs(1) |
| `--show-limits` | 値なし | — | [ref] GNU findutils |
| `-t` / `--verbose` | 値なし | — | [local] xargs(1) |
| `-o` | 値なし | — | [local] xargs(1) (BSD 固有) |
| `-x` / `--exit` | 値なし | — | [local] xargs(1) |
| `--help` / `--version` | 終端 | — | [local 実機] `xargs --help` |

`-a` / `-d` は当初「開発機の man (BSD) に無く裏が取れない」として**登録を見送って
いた**が、その判断を報告で開示したところ外部レビューが GNU `xargs --help` の逐語を
提示してくれたため登録に切り替えた。BSD xargs にはこの 2 つが存在しないので、
登録しても BSD 側の短縮形と衝突しない。**裏が取れないものを黙って落とさず開示する
運用が、そのまま解消につながった実例**。

### 他 wrapper の非数値必須引数の再確認

xargs と同じ穴 (非数値の必須引数の取り逃し) が無いか、参照元で再確認した結果:

| wrapper | 非数値の必須引数 | 状態 |
|---|---|---|
| `timeout` | `-s/--signal SIGNAL` | 登録済み。他は `-k` (数値) と bool のみで**全 option 列挙済み** |
| `watch` | **無し** | 値を取るのは `-n/--interval` `-q/--equexit` (いずれも数値) と `-d[=permanent]` (optional) だけ。**全て安全網でカバー**されるため表は空のままで正しい |
| `setsid` | **無し** | `-c/--ctty` `-f/--fork` `-w/--wait` のみ (値を取る option 自体が無い) |
| `time` | `-o FILE` / `-f FORMAT` | 登録済み。他は bool のみ |
| `nohup` | **無し** | POSIX の `nohup utility [args]`。option を取らない |
| `command` | **無し** | `command [-pVv]` (bash(1) 逐語)。値を取る option 無し |
| `exec` | `-a name` | 登録済み。他は `-c` `-l` の bool のみ (bash(1) 逐語) |
| `npx` | `--package` `-c/--call` `-w/--workspace` | `-w/--workspace` を今回追加 (`npx --help` 逐語)。npm のグローバル option は開集合で列挙不能 (下記に開示) |
| `sudo` / `nice` / `stdbuf` / `caffeinate` | 登録済み | ローカル man page で全 option 確認済み |

### `((` の解釈は 1 箇所に集約する

`((` が「算術評価」なのか「入れ子の subshell」なのかを**経路ごとに推測すると必ず
食い違う**。v0.9.0 開発中に実際、`split_on_operators` は算術として保護している
のに `_strip_leading_syntax` はただの括弧として剥がしており、`(( gh ))` が `gh` に
正規化されて**実行されないコマンドで誤 deny**していた (bash は算術式として変数を
評価するだけで CLI を起動しない)。

判定は `_arithmetic_span()` **のみ**が行う。現在この関数を共有している経路:

| 経路 | 用途 |
|---|---|
| `split_on_operators` | 算術内の `<<` を左シフトとして扱う (heredoc と誤認しない) |
| `_strip_heredoc_bodies` | 同上 (セグメント内の再スキャン時) |
| `_strip_leading_syntax` | 算術コマンドの括弧を**剥がさない** (候補にしない) |

`_strip_trailing_syntax` は「開き括弧と対応しない閉じ括弧だけを落とす」規則なので、
括弧が均衡している算術コマンドには作用しない (整合済み)。
`$(( ... ))` は `$(` の subshell 追跡側で保護される。
**新しく `((` を見る経路を足すときは、必ず `_arithmetic_span` を呼ぶこと。**

### heredoc delimiter はシェルの 1 語として解決する

`man 1 bash` Here Documents 節の逐語:「If any characters in word are quoted, the
delimiter is the result of quote removal on word」。つまり word は**隣接する断片の
連結**で、`E"OF"` も `"EO"F` も `EOF` に解決される。断片の先頭だけを読むと
delimiter を読み違え、一致する行が現れないまま**後続コマンドを本文として飲み込む**
= 検証が消える。

解決できる word 形 (すべて `EOF` に解決):

| 書き方 | 種別 |
|---|---|
| `EOF` | bare |
| `'EOF'` | single quote |
| `"EOF"` | double quote |
| `\EOF` | backslash |
| `E"OF"` / `E'OF'` / `"EO"F` / `EO'F'` | **混在 (断片の連結)** |
| `E\OF` | 途中の backslash |
| `$'EOF'` | ANSI-C quoting (中のエスケープ展開までは未対応 — 下記に開示) |

クォートが閉じない等で解決できないときは **heredoc と見なさない**
(本文行が候補になる = 過剰検証側)。terminator が実在するときだけ本文として
畳む規律とあわせて、「delimiter を読み違えて検証が消える」経路を塞いでいる。

### 引数の実行経路 (シェル経由 or 直接 exec)

wrapper が引数列を **`sh -c` に渡すか、直接 exec するか**で、クォートの扱いが
真逆になる。シェル経由の wrapper はクォート内が**コマンド文字列**なので剥がして
解析しないと `watch 'gh pr create'` が検証されない。直接 exec の wrapper では
クォート塊は「空白入りのコマンド名」なので、剥がすと**実行されないコマンド**で
誤 deny する。

| wrapper | 実行経路 | 出所 |
|---|---|---|
| `watch` (既定) | **シェル経由** (`sh -c`) | [ref] procps-ng `watch --help`: `-x, --exec` = 「pass command to exec instead of `sh -c`」= 既定は sh -c |
| `watch --exec` / `-x` | 直接 exec | [ref] 同上 |
| `sudo -s` / `--shell` / `-i` / `--login` | **シェル経由** | **[local man]** sudo(8):「If a command is specified, it is passed to the shell for execution via the shell's -c option」 |
| `sudo` (それ以外) | 直接 exec | [local man] sudo(8) |
| `npx -c` / `--call` の**値** | **シェル経由** | **[local 実機]** `npx --help` の usage 行 `npm exec -c '<cmd> [args...]'` |
| `npx` (それ以外) | 直接 exec | [local 実機] `npx --help` |
| `timeout` / `nice` / `stdbuf` / `setsid` / `caffeinate` / `xargs` / `time` / `nohup` / `env` / `command` / `exec` | 直接 exec | [local man] いずれも synopsis が `utility [argument ...]` 形 |

シェル経由の wrapper では、クォート内が**複合コマンド**のこともある
(`watch 'gh pr create && aws s3 rm x'`)。`extract_candidates` はクォートを剥がした
結果を**もう一度セグメント分割**して各段を候補にする。

`npx -c '<cmd>' <positional>` の positional は npm では package spec 扱いで実行され
ない**はず**だが、その意味論の裏が取れていない。取りこぼしで検証が消えるより
過剰検証側が安全なので、`-c` の値と残りの引数列の**両方**を候補として返している。

### 終端 option (表示して終了する option)

`--help` / `--version` 付きで呼ばれた wrapper は**後続コマンドを実行しない**。
剥がすと「実行されないコマンド」で検証が走り、誤 deny する
(`watch --help gh pr create` → `gh pr create` として deny)。誤 deny は本 plugin が
最も避けたい離脱要因なので、終端 option を検出したら wrapper を剥がさない。

| wrapper | 終端扱い | 出所 |
|---|---|---|
| `nice` | `--help` `--version` | **[local 実機]** `nice --help` → `nice: -help: invalid nice value` (BSD。エラー終了し utility を実行しない) |
| `stdbuf` | `--help` `--version` | **[local 実機]** `stdbuf --help` → `illegal option -- -` + usage |
| `xargs` | `--help` `--version` | **[local 実機]** `xargs --help` → `unrecognized option '--help'` + usage |
| `npx` | `--help` `--version` | **[local 実機]** `npx --help` → help を表示して終了 |
| `sudo` | `--help` `--version` `-V` | **[local man]** sudo(8) `-h, --help` / `-V, --version`、第 1 synopsis 形 `sudo -h \| -K \| -k \| -V` |
| `caffeinate` | `--help` `--version` | [local man] caffeinate(8) に記載は無いが、未知 option は getopt がエラーにし utility を実行しない |
| `timeout` / `watch` / `setsid` | `--help` `--version` | [ref] 開発機に無く実機確認できない。GNU coreutils / procps-ng / util-linux はいずれも `--help` を "display this help and exit" と記載 |
| `time` / `nohup` / `command` / `exec` | `--help` `--version` | [ref] BSD 版は未知 option としてエラー、GNU/builtin 版は help 表示。**どちらも後続を実行しない** |

**GNU 版は「help を表示して終了」、BSD 版は「不正 option でエラー終了」と挙動は
違うが、後続コマンドを実行しない点は同じ**。開発機で実機確認できた 4 例
(nice / stdbuf / xargs / npx) がいずれもこの結論だったため、同じ慣行に従う
残りにも適用する。

**短縮形 (`-h` / `-v` / `-V`) は既定で終端扱いしない**。`sudo -h host` のように
値を取る別 option と衝突し、終端と誤判定すると**検証が消える**ため、過剰検証側に
倒す。曖昧さの無い `sudo -V` だけ個別に登録した。この結果 `sudo -h gh pr create`
(help のつもりの形) は `-h` の値として `gh` を消費し検証されないが、これは
v0.8.0 と同じ挙動で新たな退行ではない。

**効果は「その wrapper が包む引数列」に限定**する。`watch --help; gh pr create` の
ように別コマンドとして続く形は、セグメント分割が先に走るため従来どおり検証される
(ここを取り違えるとバイパスになる)。

### 裏が取れず「消費しない」に倒した flag

GNU 専用で開発機の man page に無く、値が**非数値**のもの:
`xargs -a file` / `-d delim` / `--arg-file` / `--delimiter` / `--process-slot-var`。
登録しないと `xargs -a list.txt gh pr close` は候補が不透明のまま残り検証されないが、
これは **v0.8.0 と同じ「検証スキップ」**であって新たな退行ではない。誤って登録して
コマンド名を食う方が危険なので、消費しない側に倒す。

### 数値引数の安全網

flag 表の取り違えに対する**構造的な保険**として、flag 領域を抜けた後に残る
「純粋な数値 (+ 任意の時間単位)」トークンを読み飛ばす (`_ARG_LIKE_RE`)。
実行可能ファイル名が純粋な数値になることは実質無いので、読み飛ばしがコマンドを
食う心配がない。これがあるおかげで:

- 値付き flag の**登録漏れ**があっても、値が数値ならコマンド本体に到達できる
- `watch` のように一次情報を取れない wrapper の flag 表を**空にできる**
  (`watch -q 5 cmd` が bool + 数値なのか値付き flag なのか判らなくても正しく動く)

位置引数を持つ `timeout` だけは自前の DURATION 処理と衝突するため無効にしている。

## 将来 wrapper を追加するときのチェックリスト

`ssh` / `docker run -e` / `kubectl exec` / `xargs` / `timeout` / `stdbuf` /
`setsid` などを透過 wrapper に足したくなったら、**必ず以下を順に実施**する。
途中を飛ばすと `TestWrapperEnvClassificationGuard` が落ちる。

1. **env 挙動を実機で確認**する。`PROBE=x <wrapper> <args> env` (または
   `/usr/bin/env`) に `PROBE` が出るか:
   - 出る → `passthrough`
   - 出ない / 一部のみ → `conditional_scrub` (または非対応)
2. `_WRAPPERS_SINGLE` / `_WRAPPERS_TWO` / `_WRAPPERS_THREE` に wrapper を追加する。
3. `_WRAPPER_ENV_CLASS` に **同じキーで分類を追加**する
   (`"passthrough"` / `"conditional_scrub"`)。
4. `conditional_scrub` の場合は **scrub 補正ロジックと回帰テストを追加**する
   (`sudo` の `_sudo_preserves_env` + `_normalize_segment` の `collected.clear()`
   が雛形)。`test_only_sudo_is_conditional_scrub` も更新する。
5. wrapper の flag を **3 分類** (値を取らない / 必須引数 / optional 引数) に
   確定させ、根拠を一次情報 (インストール済み man page または `--help` の出力) で
   取って上の監査表に追記する。**記憶や推測で分類しない**。
   - 必須引数 → `_WRAPPER_FLAGS_WITH_VALUE`
   - optional 引数 (`--key[=value]`) → `_WRAPPER_FLAGS_OPTIONAL_VALUE`
     (bare 形は次の token を消費しない。必須側に入れると `xargs --replace gh ...` の
     `gh` を食って検証が消える)
   - 裏が取れない flag は **登録しない** (消費しない側に倒す)。値が数値なら
     `_ARG_LIKE_RE` の安全網が拾う
6. `TestWrapperEnvPropagationContract` の `PASSTHROUGH_CASES` 等に
   **env 伝播/非伝播の固定化ケースを追加**する。
7. 本ドキュメントの表と README の wrapper 節を更新する。

### 特に注意が要る将来 wrapper

| 候補 | env 挙動の罠 | 推奨分類 |
|---|---|---|
| `ssh host cmd` | リモートで実行され **ローカル env は届かない** (`SendEnv`/`AcceptEnv` 次第)。そもそも別ホストなのでローカル CLI 検証の意味が薄い | 透過 wrapper に **足さない** (検証スキップが妥当) |
| `docker run -e FOO ...` | コンテナ内 env はホスト行頭 env と無関係。`-e`/`--env`/`--env-file` を解析しないと誤伝播 | 足すなら専用解析が必須。安易な passthrough は不可 |
| `kubectl exec -- cmd` | Pod 内で実行。ローカル env は届かない | 足さない (検証スキップ) |
| `xargs cmd` | stdin からの引数で cmd を起動。env は継承するが起動回数・引数が動的 | ✅ v0.9.0 で `passthrough` として追加 |
| `timeout 5 cmd` | 透過 (env 継承)。値を取る第1引数 (duration) の消費に注意 | ✅ v0.9.0 で `passthrough` + DURATION 消費として追加 |
| `stdbuf -oL cmd` / `setsid cmd` | 透過 (env 継承) | ✅ v0.9.0 で `passthrough` として追加 |

`ssh` / `docker` / `kubectl exec` のように **「別の実行コンテキストへ移送する」
wrapper は、ローカル行頭 env が届かないので透過 wrapper に足さない**のが原則
(足すと「検証 env ≠ 実行 env」を再生産する)。検証スキップ (allow 相当) に倒すのが
誤 deny 回避ポリシーと整合する。
