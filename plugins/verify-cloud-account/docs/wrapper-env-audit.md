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
