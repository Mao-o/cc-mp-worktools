# sensitive-files-guardrail 設計詳細 (DESIGN.md)

利用者向けの要約は [README.md](../README.md)、保守者向けの実務ガイドは
[MAINTAINING.md](./MAINTAINING.md)、判定結果の完全マトリクスは [MATRIX.md](./MATRIX.md)、
パターン設定の詳細は [PATTERNS.md](./PATTERNS.md) を参照。

本ドキュメントは「**なぜこの設計にしたか**」の根拠と実測ログを集約する。

## 設計原則

1. **Fail-closed in doubt** — read 側の内部失敗は `ask` (bypass モード時は `deny`)
   にフォールバック。Stop 側は応答停止を招かないため fail-open (stderr warning +
   空出力)。
2. **値そのものは出さない、デバッグ情報は積極的に返す** (0.9.0 で Read 側を
   拡張、0.10.0 で Bash 側にも適用) — minimal info の核は鍵名・順序・型・
   件数だが、思想 2 (block 時は意図を汲んだメッセージを返す) を満たすため、
   値の **品質情報** (set / empty / placeholder / short / long / looks_truncated)
   と長さ (生バイト数)、識別子型の prefix (sk_live_ / AKIA / ghp_ 等) を
   併せて返す。0.10.0 で Bash deny でも operand path の dotenv を実 read して
   Read 同等の minimal info を reason 内に埋め込むようにし、grep family では
   pattern から抽出した env-var 名と dotenv parse 結果を照合した
   `matched_pattern_keys` を出す。実値そのもの (鍵名 prefix を除く一切) は
   LLM の文脈に入れない原則は維持。
3. **Secrets never in logs** — path・値・展開後情報を一切記録しない。
4. **Latency <100ms 目標** — timeout 2 秒、文字列処理のみ、外部コマンド呼出なし。
5. **情報注入は `permissionDecisionReason` 一択** — `systemMessage` 非依存
   (後述 Phase 0 実測参照)。

## Phase 0 実測結果

### 2026-04-11 — PreToolUse envelope と reason 配信経路

- `permissionDecisionReason` は deny 時に 1KB/8KB/32KB までモデルに完全配信される
- `systemMessage` トップレベルは **モデルに届かない** (公式 docs の誤り)。依存禁止
- `ask` reason はモデルには届かず、ユーザー UI のみ。bypass モードでは自動 allow
- envelope には `permission_mode` フィールドがあり bypass / plan 等の検出に使える
- `tool_input` 形状: `Read:file_path` / `Bash:command,description` など

バイト上限や envelope の生データなど詳細な実測ログは保守者の手元メモに恒久記録し、
公開 docs には上記の要点のみ転記している (再実測の手順は
[MAINTAINING.md](./MAINTAINING.md#cli-バージョンアップ時の再実測手順-runbook))。

### 2026-04-22 — plan mode での hook 発火有無 (0.3.3 → 0.6.0 で撤去)

`hooks/_debug/capture_envelope.py` (一時スクリプト) で実測。
当時の CLI (2.1.101 系) では **plan mode で PreToolUse hook が発火しない** 観測
(= Case C)。

0.3.3 では「将来 CLI が plan mode でも hook を発火させるよう変わったときの
前方互換層」として `LENIENT_MODES` に `"plan"` を加えていたが、0.6.0 で
**「想像できる将来のための dead code は思想に反する」** という方針に基づき
撤去した (REVIEW_TASKS_2026-05-06.md A5)。

### 2026-05-18 — plan mode で Bash hook が発火する観測 (0.13.0 で再追加)

ユーザー実機で plan mode 中に `grep ... | head -50` のような調査ワンライナーが
sensitive-files-guardrail の `ask_or_allow` を経由して ask に倒れ、確認ダイアログが
出る現象を確認。2026-04-22 時点の "Case C: 非発火" 観測と乖離している。
CLI バージョンアップ (2.1.101 → 2.1.x 系) のどこかで plan mode 中も Bash
PreToolUse hook を発火する仕様変更が入ったものとみなす。

0.13.0 で `LENIENT_MODES` に `"plan"` を再追加し、`ask_or_allow` (Bash 静的解析
不能ケース) の plan 挙動を allow に倒す。plan mode は副作用が plan 承認まで
保留される dry-run 的な状態のため、autonomous (auto / bypass) と同等の lenient
扱いで操作性を優先する。機密 path 確定 match (`make_deny`) と Read/Edit handler
の `ask_or_deny` は plan mode でも引き続き安全側 (deny / ask) を維持。

## ハーネス委譲方針 (defense-in-depth の一層)

本 plugin は **defense-in-depth の一層** であり、「最後の砦」を担う設計ではない。
**静的解析が困難な Bash ケースは plugin 側で deny / ask を強制せず、Claude Code
ハーネス側の別軸監視に委ねる**。「判断困難 → ユーザーに必ず判断させる」を強制
すると false positive が日常コマンドを止めて UX を阻害し、最終的にユーザーが
plugin を無効化する (= 防御がゼロになる) という事故が実際に起きている (2026-05-21
離脱、`CHANGELOG.md` 0.14.0 の離脱分析参照)。

### 判断困難ケースは ask_or_allow → autonomous で allow

「判断困難」とは以下のような **静的解析不能** な segment:

- `_has_hard_stop` 該当 (`$` / バッククォート / `(` / `)` / `{` / `}` / `<` / `\r`)
- `_is_opaque_first_token` 該当 (env-assignment / 任意 path exec / opaque wrapper)
- `_SHELL_KEYWORDS` (`if` / `for` / `do` 等)
- `_segment_has_residual_metachar` (非 `_SAFE_READ_FIRST_TOKENS` での `>` / `&` 等)
- `shlex.split` 失敗 / `normalize` 失敗
- glob operand が dotenv stem に一致しないケース

これらは `ask_or_allow` に倒し、`LENIENT_MODES` (`auto` / `bypassPermissions` /
`plan`) の autonomous モードでは `allow` に降格する。default / acceptEdits /
dontAsk では `ask` 維持。**autonomous モードでは Claude Code ハーネス自体が別軸
で Bash コマンドを監視している前提に立ち、plugin が二重に止めない**。

### plugin 側で deny 固定が許される境界

`make_deny` 固定は以下のいずれにも該当する **確信ケース** のみに限る:

- **機密 operand 確定**: literal path / VCS pathspec / URI のいずれかで
  `_operand_is_sensitive` が True、あるいは glob operand が
  `_glob_operand_is_dotenv_match` で dotenv stem に一致
- **内容出力 / 破壊操作**: `cat` / `head` / `grep` / `git show` 系の内容出力、
  `cp` / `mv` の複製、Edit/Write tool の新規作成、`ls > .env` 等の機密 path
  への redirect 書込み

「内容を出さない」コマンド (`_METADATA_ONLY_FIRST_TOKENS` + `_GIT_METADATA_SUBCOMMANDS`
の `check-ignore` / `ls-files`) は機密 operand でも `allow` に倒す
(0.14.0 G2)。**「判断困難だから deny 強制」の特例は作らない**。

### 撤去した「判断困難 → deny 強制」特例

過去にこの方針と矛盾する特例を入れた結果、思想ドリフトが観測された:

| 特例 | 撤去版 | 撤去理由 |
|---|---|---|
| `<` 入力リダイレクトの character-level parser | 0.7.0 (A1) | `cat <(echo \(\)) < .env` の escape paren depth tracking など敵対的バイパス対策が思想 1 に反する |
| FOO=1 / env / sudo 等の prefix normalize | 0.8.0 (A4) | 「`FOO=1 cat .env` を `cat .env` に書き戻す」は敵対的解釈で思想 1 に反する |
| 既定 rules 候補列挙 (glob × literal stem の連結候補化) | 0.8.0 (B3) | `*.log` が `.env.log` 連結で巻き込まれる false positive |
| git ls-files hard-stop 特例 (`--format='%(objectname)'` 等を hard-stop 内でも deny 強制) | 0.14.1 (commit 584afd1) | pathspec 無し形 (`-s` 単体) は通常パスで既に deny になるため特例不要。`--format` の hard-stop 形だけは ask に降格してハーネス委譲 (0.18.0 で `_has_hard_stop` が quote-aware になり、この形は **特例なしで** 通常の operand scan に到達して deny する) |

外部レビュー (Codex 等) が判断困難ケースの deny 強制を要求してきた場合も、
本方針を根拠に「ask_or_allow で十分、それ以上の deny 強制はハーネス委譲する」と
返す。

### 例外: `patterns.txt` 読込失敗

`load_patterns()` が `FileNotFoundError` / `OSError` で失敗したときは
**全 mode で `make_deny` 固定** (autonomous でも allow しない)。これは「policy
が無いのに lenient で素通り」を避けるため。`ask_or_allow` 系列は「policy はある
が静的解析が届かない」ケース専用。

## LENIENT_MODES 方針

`core/output.py::ask_or_allow` は bash handler の静的解析不能ケースで使う三態
判定。`permission_mode` が `LENIENT_MODES` に含まれれば allow に、そうでなければ
ask に倒す。

| mode | `ask_or_allow` | 理由 |
|---|---|---|
| `default` | ask | 明示的にユーザー介在を期待 |
| `plan` | allow | plan 承認まで副作用が保留される dry-run 状態のため (0.13.0 で auto と同等の lenient 扱いに復活) |
| `acceptEdits` | ask | Edit/Write 専用モード。Bash lenient の意図なし |
| `auto` | allow | CLI 前段 classifier モード、autonomous 実行意図 |
| `dontAsk` | ask | 明示的な非 lenient 判断として既存方針維持 |
| `bypassPermissions` | allow | 全確認スキップモード、autonomous 実行意図 |

Read/Edit handler の `ask_or_deny` は別 frozenset で `bypassPermissions` のみ
deny に倒す (機密可能性があるものは ask 維持で bypass だけ deny する)。

## Bash handler の対応文法範囲

Bash handler の静的解析は **shlex.split (POSIX mode)** ベース。
`bash_handler.py::handle` 内で `_split_command_on_operators` (quote-aware セグメント
分割) 後の各 segment を shlex.split し、コマンド token 単位で解析する
(opaque first token 判定, shell keyword 検出, operand scan)。

### read-only first_token allow-list (0.12.0)

`_SAFE_READ_FIRST_TOKENS` (副作用なしの read-only コマンド: `ls cat head tail
nl tac bat less more view wc file stat du df tree grep egrep fgrep rg ag ack
od xxd hexdump`) を `handlers/bash/constants.py` に定義。第一トークンがこの
セットに該当する segment は、`_segment_has_residual_metachar` の ask 経路を
**スキップ** して operand scan に直行する。

導入背景: 0.11.0 までの実測ログで `bash_classify` の ask 発火の **約 80%** が
`segment_residual_metachar_lenient` (= `>` 出力リダイレクトや `&` background
を含むコマンド) 起因だった。`grep foo README.md > /tmp/out` のような調査用
ワンライナーが ask に倒れて UX を阻害していたため、副作用なしの read-only
コマンドに限り redirect / background を許容する。

安全 net:
- 機密 redirect target (`grep foo > .env`) は operand scan で `.env` を捕まえて
  deny 固定。
- hard-stop (`$(...)` / backtick / heredoc / `<`) は依然 `ask_or_allow` (= 静的
  解析不能、shell 展開で別コマンド出力が混入する経路を塞げないため)。
- `_OPAQUE_WRAPPERS` (`bash -c`, `eval`, `python -c`, `sudo` 等。0.17.0 で `awk` /
  `sed` は除外) / `_SHELL_KEYWORDS`
  (`if`, `for` 等) は allow-list と disjoint なので、これらの ask 経路は不変。
- `find` は `-delete` / `-exec` で副作用持ちうるため allow-list **外**。
- `echo` は stdout 出力で「見る・数える」とは異なるため allow-list **外**。

**0.11.0 (F1)**: hard-stop char (`$`, バッククォート, `(`, `)`, `{`, `}`, `<`,
`\r`) は **segment 単位で再評価** する。0.10.0 までは command 全体に hard-stop
が 1 つでもあると `ask_or_allow` で early return していたため、
`cat .env | sed 's/(=)/X/'` のような複合で sed segment の `(` が原因で全体
ask に倒れ autonomous で `cat .env*` が素通りしていた。0.11.0 では segment ごとに
`_has_hard_stop` を再判定し、hard-stop / shlex 失敗の segment は `pending_ask`
に格納して continue (deny 確定 segment を優先し、無ければ最後に `pending_ask` を
畳む)。攻撃シナリオ `cat <(echo \(\)) < .env` は全 segment が hard-stop と
なるため挙動不変 (思想 1 整合)。

**0.18.0 (bd_092a232e-y5y)**: `_has_hard_stop` を **quote-aware** にした。Bash は
シングルクォート内を一切展開しないため、そこに現れる hard-stop char は静的解析を
妨げない。これで 0.17.0 の残り穴 (`awk '{print}' .env` が `{` `}` `$` により
opaque 判定より前で ask に倒れる) が閉じ、awk / sed の最頻形が operand scan に
到達する。`git ls-files --format='%(objectname)' .env` も本来の
`_GIT_LS_FILES_OBJECT_OPTS` 経路 (= `-s` / `--stage` と同じ deny) に到達する。

緩めない側は意図的に非対称にしてある (guard を落とさないため):

- **ダブルクォート内は hard-stop 維持** — `echo "$(cat .env)"` は展開される
- **クォート外のバックスラッシュは quote を開かない** — `\'` は literal `'`
  であってシングルクォート開始ではない (Bash 仕様)。取り違えると
  `cat \'$(cat .env)\'` で guard が落ちる。一方 `\$` `\(` 自体は hard-stop として
  数え続けるため `cat <(echo \(\)) < .env` は挙動不変
- **`\r` はクォート内でも hard-stop** — CR は展開ではなく端末表示偽装の guard

**splitter と hard-stop は 1 つの lexer (`_lex`) を共有する** (PR #38 review R7
で統一)。`_lex` が行継続 `\<newline>` を除去し (クォート外とダブルクォート内。
シングルクォート内とコメント内は literal)、各文字に quote / escape / comment の
字句状態を付けた列を作る。分割 (`&&` `||` `;` `|` 改行、および単独 `&` =
非同期リスト。`2>&1` `&>` の `&` は除く) と hard-stop 判定はその列だけを見る
ので、`ls &\` ⏎ `& cat .env` のように継続をまたいで合成される演算子も正しく
区切れる。字句規則を直すときは `_lex` だけを直す。以下は統一前に個別に
見つかった desync の記録。

**splitter と hard-stop の字句状態は完全に同期させる** (PR #38 review で判明)。
`_split_command_on_operators` がクォート外の `\'` を quote 開始と誤認すると、
`echo \' ; cat .env ; echo '{'` が 1 segment に潰れ、`_has_hard_stop` は
`'{'` をクォート内と見て False を返すため、先頭 token `echo` の metadata-only
経路で `.env` read が素通りする。「分割側の desync は segment 境界がずれるだけ」
ではなく、**どちらの desync でも guard は落ちる**。よって splitter にも同じ
クォート外エスケープ規則 (`\'` は quote を開かない / `\;` は区切らない /
`\<newline>` は行継続) を入れ、両 scanner を同じ字句状態で走らせる。

**Bash コメントも両 scanner で認識する** (同 review R2)。`#` 〜 行末は Bash に
解釈されないが、コメント内の `'` で quote 状態を反転させると
`echo ok # ' {` ⏎ `cat .env` ⏎ `echo ok # '` が 1 segment に潰れ、同じ経路で
`.env` read が素通りする。クォート外・単語先頭 (直前が空白 / `;` / `|` / `&` /
文字列先頭) の `#` から行末までをコメントとして両 scanner が読み飛ばす
(splitter は segment から落とし、改行は区切りとして残す)。検出範囲は Bash より
狭く保つ — コメントでないものをコメント扱いすると実コマンドを落とし guard が
落ちるが、逆は ask に倒れるだけ。`\r` 入りコメントだけは丸ごと残して表示偽装
guard に到達させる。「単語先頭」は直前の生文字ではなく **字句状態**
(`word_start`) で判定する — Bash は `\<newline>` (行継続) を先に取り除くので、
`echo safe\` ⏎ `#joined; cat .env` は `safe#joined` の 1 単語でコメントではない
(同 review R3)。

**シングルクォートは Bash には不活性でも、呼び出されるプログラムには解釈される**
(同 review R3)。`awk 'BEGIN { system("cat .env") }'` は hard-stop を抜けた後、
operand scan でも `.env` がプログラム文字列の内側にあるため見えない。
`handlers/bash/interpreters.py` (`_program_dynamic_construct`) が awk の
`system(` / `getline` / `|` / `>` / `-f progfile`、sed の `e` (command /
`s///e`) / `r` `R` / `w` `W` / `-f script` を検出し、**operand scan の後** で
`ask_or_allow` (`bash_lenient("program_dynamic")`) に戻す。sed は regex 近似
ではなくスクリプトを先頭から走査する小さな parser (`_sed_script_dynamic`) で
コマンド位置を求める (アドレス / `s` `y` の区切り本文 / `a` `i` `c` テキスト /
ラベル / コメントを読み飛ばす)。regex 近似は密着引数 `r.env` と `-e` 密着
スクリプトが `e` で終わる形を取りこぼした (review R4)。sed のオプション引数
(`-l N` 等) は値を読み飛ばし、GNU / BSD で引数の有無が異なる `-l` / `-i` の
裸形は次の token を候補に入れつつ positional とは数えない (review R5)。

**シングルクォート内 hard-stop の緩和は inert な first token に限定する**
(review R5 → R6)。シングルクォートは Bash の展開を止めるだけで、`find -exec
sh -c 'cat ${X:-.env}' ';'` / `ssh host '…'` / `git -c alias.x='!cat ${X}' x` の
ように引数を別のシェル / インタプリタに渡すコマンドでは `$` `{` が生きている。
「委譲するコマンド」の有限 allowlist (R5) では git の shell alias 等を追い切れ
なかったので、**緩和する側** を有限 allowlist にした (`_QUOTE_RELAX_FIRST_TOKENS`:
safe-read から pager 系を除いたもの + metadata-only + awk / sed + `git` `find`
`sort` `cut` `tr` `jq` `cp` `rm` … 引数文字列を shell に渡さず、自分でも glob
展開しないと判っているコマンド。`curl` は URL の `{a,b}` を自分で展開するので
含めない (review R8))。git はさらにサブコマンドで絞る (`_GIT_INERT_SUBCOMMANDS`:
`rebase --exec` / `bisect run` / `submodule foreach` / `grep -O` /
`--upload-pack` のように shell コマンドを受け取る option を持つサブコマンドと
未知のサブコマンド (= 設定済み alias かもしれない) は委譲扱い)。inert でも
外部プログラムを受け取る option (`rg --pre` / `ag --pager` / `sort
--compress-program`) は委譲扱い (`_DELEGATING_OPTIONS`)。sed の long option は GNU の一意 prefix 省略
(`--expr`) を解決してから引数の有無を判定する。

**スコープ (0.18.0 review で確定)**: この plugin の目的は **エージェントが
うっかり書くコマンド** による露出の予防であり、悪意を持った抜き取りの防止では
ない (思想 1)。quote-aware 化で保つべきは「splitter / hard-stop が普通のコマンドを
正しく区切る」こと (`# don't` のようなコメント内の `'`、`find -exec sh -c '…'`、
単独 `&`、行継続) で、これは本リリースで対応した。一方「exec option を持つ
コマンドの列挙」(`ag --pager` / `git rebase --exec` / `curl` の URL glob /
sed の密着引数 …) は inert allowlist が本質的に不完全である以上終わらないため、
本リリース以降は **follow-up に集約し、個別対応しない**。未知のコマンドは
0.17.0 と同じ ask (fail-closed) に倒れており、autonomous では allow なので、
列挙漏れがあっても「うっかり露出」の観点では 0.17.0 から後退しない。`_has_quoted_hard_stop` (hard-stop が False の segment に
シングルクォート内の hard-stop char が残っているか) が真のとき
`_quoted_hard_stop_reason` を適用し、allowlist 外の first token (`docker` /
`make` / `less '+!…'` / 未知) は 0.17.0 と同じ hard-stop (ask) に倒す
(fail-closed)。inert でも委譲する形 — find の `-exec` 系、first token 以外の
`_OPAQUE_WRAPPERS` / awk / sed、git の global option `-c` / `--config-env` — は
ask に戻す。`git -c alias.<name>=!…` は alias 本文が operand scan に見えない
ので、クォート状態に関係なく常に ask (`_inline_shell_delegation`)。クォート内
hard-stop が無い普通のコマンド (`grep -r python3 …`) には適用しないので、
0.17.0 → 0.18.0 で緩んだ範囲の一部を戻すだけで後退はない。機密 operand 確定の
deny が先なので `awk '{print}' .env` は deny のまま。`>` (比較) や `||` の
false positive は ask で済ませる (0.17.0 まで `$` `{` で ask だった形)。
他のインタプリタ (`python -c` / `perl -e` / `bash -c`) は `_OPAQUE_WRAPPERS`
が先に ask に倒す。

副次効果として非機密 operand の `grep '(=)' notes.txt` / `awk '{print $1}' notes.txt`
は ask → allow に緩む (0.16.0 の `sed -n p notes.txt` と同じ方向、false positive 減)。

### metadata-only first_token allow-list (0.14.0)

`_METADATA_ONLY_FIRST_TOKENS` (`ls tree stat file du df test wc basename
dirname realpath readlink echo printf`) と `_GIT_METADATA_SUBCOMMANDS`
(`check-ignore` / `ls-files` / `status`、`git <sub>` 直書き形のみ) を
`handlers/bash/constants.py` に定義。該当 segment は **operand scan 自体を
スキップして allow** に倒す (機密 operand でも deny しない)。`find` は単体
集合に含めず、`_FIND_DANGEROUS_ACTIONS` (`-exec` / `-execdir` / `-ok` /
`-okdir` / `-delete` / `-fprint*` / `-fls`) を含まない場合のみ metadata-only
として扱う条件付き判定にする (後述)。

導入背景: 離脱分析 (2026-05、transcript 実測) で、実 deny 15 件のうち
`find -name X` / `ls -la X` / `git check-ignore X` のような所在・属性確認が
1/3 を占めた。これらは operand の **内容** を stdout に出さないため、deny して
も露出予防効果がなく、ユーザー離脱 (plugin 無効化) だけが起きていた。
「値が LLM コンテキストに載らない操作は思想 1 (うっかり**露出**予防) の射程外」
として allow に倒す。

`_SAFE_READ_FIRST_TOKENS` (0.12.0) との関係:
- SAFE_READ は「residual metachar の ask をスキップする」リスト (cat / grep
  等の内容出力系を含む)。METADATA_ONLY は「operand scan をスキップする」リスト
  (内容を出さないコマンドのみ)。直交する 2 軸で、`ls` 等は両方に属する。
- 判定順序: opaque → residual metachar (非 SAFE_READ のみ) → shell keyword →
  **metadata-only** → operand scan。residual より後段のため、`echo KEY=val >
  .env` (書込み形、echo は SAFE_READ 外) は従来通り residual の ask に倒れ、
  metadata-only では緩まない。

安全 net:
- 内容出力系 (`cat` / `head` / `grep` / `od` 等) と `cp` / `mv` (複製で漏洩面が
  広がる)、`git show` / `git diff` / `git add` は従来通り deny 固定。
- **`find` の内容出力・副作用アクション** (`-exec` / `-execdir` / `-ok` /
  `-okdir` / `-delete` / `-fprint*` / `-fls`) を含む形は metadata-only から
  除外して operand scan → deny。`find -exec cat {} +` は `{}` が hard-stop の
  ため segment 単位で ask に倒れるが、`find -exec cat .env ';'` のように `{}`
  を使わず literal path + クォート `;` で hard-stop を回避する形は
  `_is_metadata_only` の `_FIND_DANGEROUS_ACTIONS` 判定で捕捉する (Codex P1,
  2026-06-12)。`-print` / `-printf` / `-ls` (stdout への metadata 出力) は安全
  なので metadata-only 維持。
- **`file` / `wc` / `du` / `tree` の「ファイル名リスト読込」オプション**
  (`file -f` / `--files-from`、`wc`/`du` の `--files0-from`、`tree --fromfile`
  = `_METADATA_CONTENT_READING_OPTS`) を含む形も metadata-only から除外して
  deny。これらは operand ファイルの **中身** を別パスのリストとして読み、その
  名前 (= 中身) を stdout / エラーに echo するため。`file -f .env` は .env の
  各行を `<行>: cannot open` でエラー出力し実値を漏らす (Codex P2 第2弾,
  2026-06-12)。`file .env` / `wc -l .env` (通常形、型判定・行数のみ) は安全で
  metadata-only 維持。分離形 (`-f .env`) / 値結合形 (`--files0-from=.env` /
  `-f.env`) 両対応。
- `git -C dir check-ignore` のような global option 前置形は保守的に対象外
  (従来通り operand scan → deny)。
- **`git status` は allowlist から除外** — `-v` / `--verbose` が staged 変更の
  diff (機密の旧値/新値) を出力するため (`git status -v -- .env` で実値が漏れる)。
  option-gate するより allowlist から外す方が単純で穴も無い。`check-ignore`
  (gitignore ルール表示) / plain な `ls-files` (名前のみ) は内容を出さないため
  維持。**裸の `git status` は機密 operand が無いため
  operand scan で allow に倒れる** (常用ケース無影響)、`git status [-v] -- .env`
  等 operand 明示形は deny (Codex P1 第2弾, 2026-06-12)。
- **`git ls-files` は object-name 出力オプション付き形を除外** — plain な
  `git ls-files .env` / `--error-unmatch` は名前一覧のみなので metadata-only
  維持。`-s` / `--stage` / `-sz` 等は blob object name (= 内容の安定した指紋)
  を出せるため operand scan → deny に倒す (Codex P2 第3弾, 2026-06-12)。
  `--format=%(...)` の quote 内に `(` を含む形は 0.17.0 まで segment hard-stop に
  該当し ask_or_allow に降格していた (0.14.1、commit 584afd1 のハーネス委譲方針整理)。0.18.0 の
  quote-aware 化で segment が静的解析可能になり、**特例を足すことなく** `-s` /
  `--stage` と同じ deny 経路に到達する。
  pathspec 無し形 (`git ls-files -s` 単体、機密 path operand 無し) も
  operand scan が allow に倒す (内容露出には機密 path commit が前提のため)。
- **機密 path への redirect 書込み** (`ls > .env` で .env を truncate) は
  metadata-only ∩ safe_read コマンドだと residual metachar 判定を skip して
  shortcut allow に倒れる穴があった (0.14.0 の regression)。`_sensitive_redirect_target`
  で書込み target を抽出し機密なら deny に倒す (Codex P2)。`>` / `>>` / `n>` /
  `&>` の spaced / fused 形に対応。内容露出ではなく破壊的書込みの懸念であり、
  Edit/Write の機密書込み deny と整合させる。`ls -la .env > /tmp/x` (read operand
  のみ機密、書込み先非機密) は allow 維持。
- **0.19.0: 次善策コマンドの追加** (bd_092a232e-snw.3) — `chmod` / `chown` /
  `chgrp` / `touch` を `_METADATA_ONLY_FIRST_TOKENS` に、`git rm --cached` を
  条件付き (`_git_rm_is_index_only`: `--cached` が `--` より前に exact match、
  `--pathspec-from-file` 無し) で追加。両 hook の reason が「tracked なら
  `git rm --cached` で untrack」「`chmod 600 .env`」と案内しながら Bash hook 自身
  がそれらを deny する自己矛盾があった。いずれも内容を出力せず実ファイルも
  消さないため、確信 deny の条件 (機密 operand 確定 × 内容出力 / 破壊) に該当
  しない。plain `git rm` (作業ツリー削除) と `--pathspec-from-file` (operand の
  中身を pathspec として読み不一致行を echo、`file -f` と同クラス) は deny 維持。
  書込み形 (`chmod 600 x > .env`) は safe_read 外のため residual metachar で
  従来通り ask_or_allow (echo と同じ、緩めない)。`git rm --cached -r` は index
  除去の範囲が広がるだけで内容出力も削除も無く allow、`touch -r` /
  `chmod --reference` は timestamp / mode を読むだけで allow。git は long option
  の **一意な接頭辞** を受理する (`--no-cach` = `--no-cached` で後勝ちにより
  作業ツリーも削除、`--pathspec-from-fil` = `--pathspec-from-file`) ため、危険な
  option を exact-token で deny-list しても省略形がすり抜ける (Codex review P1)。
  省略形の展開を自前実装せず、**既知の安全な option (`_GIT_RM_KNOWN_LONG_OPTS`
  完全一致 / `_GIT_RM_SAFE_SHORT_FLAGS` の束ね) 以外が 1 つでもあれば index-only
  と見なさない** fail-closed 規則にした。`--cached` 自体の省略形 (`--cache`) も
  展開せず保守側 (通常経路 → deny) に倒れるだけで露出は無い。

### 対応 (deny/allow 確定できる)

| カテゴリ | 構文 | 使用 parser |
|---|---|---|
| セグメント区切り | `cmd1 && cmd2`, `cmd1 \| cmd2` | `_split_command_on_operators` (0.3.0) |
| operand scan (literal) | `cmd file1 file2`, URI/VCS | shlex token (`_operand_is_sensitive`) |
| operand scan (glob, dotenv stem 一致) | `cat .env*`, `cat *.envrc`, `cat .e[n]v` | shlex token + ``fnmatchcase`` (`_glob_operand_is_dotenv_match`, 0.8.0) |

### 対応外 (opaque `ask_or_allow` 扱い)

| 構文 | 備考 |
|---|---|
| `<` 入力リダイレクト全般 (`cmd < t`, `cmd<t`, `cmd 0<t`, `cmd < "t"` 等) | 0.3.4〜0.6.x で character-level parser による target 抽出 + literal/glob 一致 deny を行っていたが、escape paren depth tracking や `[[ ... ]]` 引数位置判定など敵対的バイパス対策のコード負債が思想 1 (うっかり露出予防、敵対的防御は非目的) と整合しないため 0.7.0 で撤廃。`<` を含む segment は他の hard-stop と同じ ``ask_or_allow``。0.11.0 から segment 単位で再評価するため `cat $X | ls .env | head` のように後段で literal match があれば deny に到達する |
| `<<` heredoc, `<<-` | delimiter/body は read 対象外 |
| `<<<` herestring | literal 渡しで file read ではない |
| `<&N`, `<&-` fd dup | 既存 fd 複製、file read ではない |
| `<(cmd)` process sub | Bash 拡張、`hard_stop` 経由で opaque |
| `$(cmd)`, `` `cmd` `` | 動的展開。静的解析不能 |
| `[[ cond ]]`, `(( expr ))` | Bash 条件式 / 算術。hard_stop で opaque |
| `bash -c "..."`, `eval`, `python -c` | wrapper。内部 script は未解析 |
| 第一トークンが env-assignment (`FOO=1`) / `env` / `command` / `builtin` / `nohup` / 任意 path exec (`/bin/cat`, `./script`) | 0.3.2〜0.7.x で透過剥がし (`FOO=1 cat .env` を `cat .env` と解釈) で deny に倒していたが、思想 1 (うっかり露出予防、敵対的防御は非目的) に整合しないため 0.8.0 で撤廃。これらは ``ask_or_allow`` |
| operand glob (`*` / `?` / `[` 含む) で dotenv literal stem (`.env` / `.envrc`) に fnmatch しないもの (`id_rsa*` / `*.key` / `cred*.json` / `*.log` / `.env.*` / `.env.example*` 等) | 0.3.2〜0.7.x で既定 rules 候補列挙 (`_glob_candidates` / `_glob_operand_is_sensitive`) で deny に倒していたが、`*.log` 等の日常 glob まで巻き込む False positive と思想 1 不整合のため 0.8.0 で撤廃。dotenv stem 一致のみ deny を維持 |

### 観測ログ

`L.log_info("bash_classify", ...)` の tag:

- `opaque_prefix_lenient` / `segment_residual_metachar_lenient` /
  `shell_keyword_lenient:<kw>` / `shlex_fail:<err>` / `hard_stop_lenient` — 各 lenient 経路
- `match:<first_token>` / `glob_match:<first_token>` — operand scan で deny 確定

## Bash handler 判定フロー

0.3.2 で確定した三態判定の骨格は現在も同じ。最新 (0.19.0) の mermaid フローは
[MAINTAINING.md](./MAINTAINING.md#bash-handler-判定フロー-0190) 側に集約。コマンド別の
deny/allow/ask 一覧は [MATRIX.md](./MATRIX.md) を参照。

### 責務境界 (0.3.3 再設計、0.8.0 で簡素化)

0.3.3 では `bash_handler.py` が 662 行に肥大化していたのを、責務境界で以下のよう
に分割した:

| モジュール | 責務 | 依存先 |
|---|---|---|
| `bash_handler.py` | orchestration + plugin ステート依存 + test seam | `core.*`, `handlers/bash/*` |
| `handlers/bash/constants.py` | compile-time 定数 (regex / frozenset) | なし |
| `handlers/bash/segmentation.py` | quote-aware セグメント分割 / hard-stop 検出 | `constants` |
| `handlers/bash/operand_lexer.py` | glob 判定 / dotenv glob 一致 / path 候補抽出 | `constants` |
| `handlers/bash/redirects.py` | 安全リダイレクト剥離 / 残留 metachar 判定 | `constants` |

`handlers/bash/` 配下のモジュールは **副作用なし・plugin ステート非依存**
(SFG_CASE_SENSITIVE 環境変数の参照のみ、`is_sensitive` 側と整合)。

テストは `handlers.bash_handler.X` の形で patch / import するため、
`bash_handler.py` は以下の symbol を **再 export** して従来の import path を
維持する:

- `handle` (orchestration)
- `_operand_is_sensitive` (plugin ステート依存)
- `_glob_operand_is_dotenv_match` (0.8.0 新設、operand glob と dotenv stem の fnmatch)
- `load_patterns` (test_failclosed.py の `mock.patch` 対象)
- 各定数 (test が直接参照する可能性に備えて)

> 0.7.x までの patch seam だった `_normalize_segment_prefix` (prefix 透過処理)、
> `_literalize` / `_glob_candidates` / `_glob_operand_is_sensitive` (既定 rules
> 候補列挙) は 0.8.0 で撤廃。`_is_absolute_or_relative_path_exec` も同時撤廃。

## 判定ロジックの詳細

### Read handler

| ケース | 判定 |
|---|---|
| パターン非該当 | no-op |
| 機密 + 通常ファイル成功 | `deny` + minimal info を `permissionDecisionReason` |
| 機密 + symlink | `ask_or_deny` |
| 機密 + 特殊ファイル (FIFO/socket/device) | `ask_or_deny` |
| 機密 + 読み取り失敗 (権限/IO) | `ask_or_deny` (fail-closed) |
| redaction engine 内部例外 | `ask_or_deny` (fail-closed) |
| patterns.txt 読込失敗 | `ask_or_deny` + stderr 警告 |
| サイズ 32KB 超 | keyonly_scan で streaming 鍵名抽出 |

### dotenv minimal info の拡張 (0.9.0, E1 + E2)

`redaction/dotenv.py` で生成する minimal info に以下を追加 (実値は出さない):

| 項目 | 内容 | 例 |
|---|---|---|
| `<type=...>` | 値クラス (14 種、0.9.0 で 8 種拡張) | `<type=stripe_secret>` |
| `prefix="..."` | 識別子型のみ、公開済み prefix を表示 (Q3 採用) | `<type=stripe_secret prefix="sk_live_">` |
| `<set>` / `<empty>` / `<placeholder>` | 値の有無・placeholder 一致 | `<set>` |
| `<short>` / `<long>` / `<looks_truncated>` | 型整合性・truncation ヒント (複数併記可) | `<set> <short>` |
| `length=<N>` | 値のバイト長 (Q2 採用、bucket せず生長さ) | `length=68` |
| `matched="..."` | placeholder 一致時の辞書 literal / pattern label | `matched="your_jwt_secret_here"` |

**型推定 (0.9.0 拡張)**: `str` / `bool` / `null` / `num` / `jwt` (既存) +
`url` / `email` / `uuid` / `aws_access_key` (AKIA / ASIA) / `stripe_secret`
(sk_live_ / sk_test_ / rk_live_ / rk_test_) / `stripe_pk` (pk_live_ / pk_test_) /
`github_pat` (ghp_ / gho_ / ghu_ / ghs_ / ghr_) / `openai_key` (sk-)。

**prefix を返す型**: jwt (`ey`)、aws_access_key (`AKIA` / `ASIA`)、stripe_secret
(`sk_live_` / `sk_test_` / `rk_live_` / `rk_test_`)、stripe_pk (`pk_live_` /
`pk_test_`)、github_pat (`ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_`)、openai_key
(`sk-`)。

**short の閾値**: jwt < 30 / aws_access_key < 16 / stripe_* < 25 /
github_pat < 30 / openai_key < 20 / url < 8 / uuid < 36 / email < 6。
**long の閾値**: 4096 byte 超 (デバッグダンプ混入の検知)。

**placeholder 判定**: `redaction/placeholders.py::looks_placeholder` が
PLACEHOLDER_LITERALS (21 個) と PLACEHOLDER_PATTERNS (5 個 regex) で判定。
ユーザー拡張点 (placeholders.local.txt) は **作らない** (Q1 = 簡易版で開始)。

### json/toml/yaml の status 拡張 (0.14.0, E5)

dotenv (E1+E2) で導入した value status / length / placeholder hint を、
`redaction/jsonlike.py` / `redaction/tomllike.py` / `redaction/opaque.py` に
横展開する (REVIEW_TASKS_2026-05-06.md L408-425、commit 3189d907 で実装。tag
`v0.14.0` に同梱され 0.14.0 で出荷済み、docs への反映は 0.14.1)。
B1 (「json/toml/yaml を opaque 統一」) を撤回して逆方向 (status 拡張) に倒した
経緯は同ファイル L275-278 を参照。

#### JSON / TOML — str scalar 値への status 付与

`redaction/jsonlike.py::_classify_str_status` で文字列 scalar 値に status タグと
length を付与する:

| status | 条件 |
|---|---|
| `<empty>` | 空文字列 `""` (length=0、length 列は表示しない) |
| `<placeholder>` | `placeholders.py::looks_placeholder` 一致 (`matched="..."` 併記) |
| `<set>` | 上記以外で値あり |
| `<long>` | 4096 byte 超 (dotenv と同じ閾値、ダンプ混入の検知) |
| `<looks_truncated>` | 末尾が `...` / `<truncated>` / バックスラッシュ |

- bool / num / null / array / object には **status を出さない** (構造側は値を
  持たないため意味がない、行内に `<set>` 等が紛れない実装)
- **`<short>` は対象外**: dotenv は jwt / url 等の型別最低長 (`_MIN_LENGTH_BY_TYPE`) を
  持つが、json / toml にはそれに相当する型ヒントが無いため判定不能。「判定の根拠を
  失わせる項目は出さない」原則
- 再帰ネストでも適用 (`{"outer": {"inner": "..."}}` で inner にも付与、`_walk`
  の object 分岐内 `sub_type == "str"` で status 計算)
- **TOML は jsonlike の `_walk` を共有**経由で同じ status が自動付与される
  (`redaction/tomllike.py::format_toml` は `format_jsonlike(info)` 直流用)。
  toml の独自フォーマット維持コストはゼロ

#### YAML — 簡易 top-level key 抽出

`redaction/opaque.py::_redact_yaml` で簡易 top-level key 抽出を行う:

- `^([A-Za-z_][A-Za-z0-9_\-]*)\s*:` 行 → top-level key を順序維持で収集
  (`_YAML_MAX_TOP_KEYS=500` で cap、健全性確保)
- `^\s+([A-Za-z_]...)` 行 → nested 件数のみカウント (key 名は出さない、
  `nested entries: N (not parsed)` として件数表示)
- list 形式 (`- item:`) と comment 行 (`#`) はスキップ
- **完全パースしない** (思想 1 = うっかり露出予防の射程、anchor / alias /
  flow style / multi-document などは対象外)
- 構造を追わない分、yaml 内のネスト値が `<placeholder>` か否かは判定できない。
  「主要セクション (database / features 等) は何か」「設定の規模感 (nested N
  件あり)」を伝える設計に留める
- 既存の opaque (yaml 以外の未知形式) は従来通り `keyonly_scan` の streaming
  抽出経由で fallback

#### dotenv との status タグ整合表

| status | dotenv | json/toml str | yaml |
|---|---|---|---|
| `<set>` | ✓ | ✓ | (top-level key 名のみ) |
| `<empty>` | ✓ | ✓ | — |
| `<placeholder>` (+ `matched`) | ✓ | ✓ | — |
| `<short>` | ✓ (型クラス前提) | ✗ (型ヒント無し) | — |
| `<long>` | ✓ | ✓ | — |
| `<looks_truncated>` | ✓ | ✓ | — |
| `length=N` | ✓ | ✓ | — |
| `prefix="..."` | ✓ (識別子型) | ✗ | — |

dotenv と json/toml の差分は `<short>` / `prefix` の 2 項目のみ (型クラス前提)。
yaml は構造未パースのため status 系は全て出さず、key 名と件数のみ。

### Bash deny の category 別 reason (0.10.0, E3 + E4)

`core/messages.py::bash_deny` を first_token カテゴリ別 dispatch に再編し、
コマンド意図 → 提供する情報・代替案を切り替える (思想 2 を Bash 側でも実装)。

| category | first_token | 返す情報 |
|---|---|---|
| `read_full` | `cat` / `less` / `more` / `bat` / `xxd` / `od` / `hexdump` / `base64` | 「全体閲覧」note + Read 同等 minimal info。Read tool 推奨は **minimal info を出せなかったときのみ** (0.16.0 で条件反転) |
| `read_partial` | `head` / `tail` | 「先頭/末尾 N 行確認」note + 鍵 list の N 件 (head=先頭、tail=末尾)。`-n N` / `-N` (BSD) / `--lines=N` から N を抽出 |
| `search` | `grep` / `rg` / `ag` / `ack` / `egrep` / `fgrep` | 「検索」note + `matched_pattern_keys: [...]` / `nomatch_pattern_keys: [...]` (E4 で抽出した env-var 名と dotenv の照合結果)。pattern 抽出 / 照合とも失敗時は全鍵 list (minimal info) に降りる |
| `mutate` | `awk` / `sed` | 「加工」note + minimal info + patch / diff 適用推奨。**0.17.0 で到達可能になった** (それ以前は `_OPAQUE_WRAPPERS` 判定が先行して handler からは到達しない dead branch だった) |
| `load` | `source` / `.` | 「shell load」note + minimal info + direnv (.envrc) / dotenv-cli / 1Password CLI 推奨 |
| `move` | `cp` / `mv` | 「コピー / 移動」note + 1Password CLI / pass / git-secret + .env.example 派生推奨 |
| `history` | `git` (全 subcommand) | **0.19.0 で subcommand 別**: 閲覧系 (`show` / `diff` / `log` / `cat-file` 等) は「commit / 差分閲覧」note + 「tracked なら漏洩済みの可能性」+ `git rm --cached <basename>` + rotate 推奨。操作系 (`add` / `rm` / `mv` / `restore` / `checkout` / `reset` / `stash` / `clean` / `update-index` / `apply` / `commit` = `_GIT_OPERATE_SUBCOMMANDS`) は「index / 作業ツリーへの操作」note + subcommand 別の代替案 (`rm` は allow される `--cached` 形を案内し、deny された理由は断定しない)。subcommand は `command` 全体から `_git_subcommand_of` で推定 (複合コマンドは operand を含む `git` window を優先、`-C` / `-c` / `--git-dir` 等の global option は読み飛ばし。`bash_deny` の signature は変えない)。VCS pathspec の `:` 後尾から basename 抽出 |
| `transfer` | `curl` / `wget` / `scp` / `rsync` | 「転送」note + Vault / SOPS / 1Password CLI 推奨 |
| `archive` | `tar` / `zip` / `gzip` | 「アーカイブ」note + `--exclude=<basename>` / `-x <basename>` 推奨 |
| `generic` | 上記以外 | 0.7.0〜0.9.0 と同等の note + minimal info (新規) |

**file_render の流れ** (`redaction/file_render.py::render_for_bash`、0.16.0 で
4-tuple `(reason, info, status, resolved_base)` 化):

1. `normalize(operand, cwd)` で path を解決 (失敗 → status `normalize_failed`)
2. `classify(path)` で regular ファイルか確認
   (`missing` → `unresolved` / `symlink` `special` → `not_regular` /
   `error` および lstat 例外 → `stat_failed`)
3. `open_regular(path)` で fd と size を取得 (`O_NOFOLLOW`、失敗 → `open_failed`)
4. format 判定 (`_detect_format`):
   - dotenv → `redact_dotenv` で info dict を取得 → `format_dotenv` で body
     文字列 → `build_reason` で `<DATA untrusted>` 包装 → (reason, info, "", "") を返す
   - dotenv 以外 (json / toml / yaml / opaque / 32KB 超) → `engine.redact` /
     `redact_large_file` で reason を取得 → (reason, None, "", "") を返す
5. 内部例外は握り潰し status `redact_failed`

**status の使われ方** (0.16.0):

- 失敗 status は `core.messages._append_minimal_info` が
  `minimal info: unavailable (<理由>)` + kind 別 next action に変換する。
  0.15.0 までは minimal info セクションを **黙って省略** していたため、
  情報も next action も無い deny reason になり、モデルが Read へ切り替えず
  別コマンドで迂回する原因になっていた (2026-08-17 実観測)
- `handlers/bash_handler.py` が `bash_render_failed` / `bash_render_project_root`
  でログに残す (kind は固定 slug のみ、path / basename / 値は含めない)

**project root フォールバック** (0.16.0):

相対 operand が `cwd` 基準で `missing` になったときだけ、
`_shared.patterns._resolve_project_key` (`$CLAUDE_PROJECT_DIR` 優先 → `.git`
上方探索) が返す project root を基準に **1 回だけ** 再解決する。hook は
コマンド実行 **前** に走るため、同一コマンド内の先行 `cd` は `cwd` に反映
されず、`cd <repo> && grep KEY sub/.env` のような形で minimal info が丸ごと
落ちていた。

- `missing` 以外の失敗では再解決しない (`cwd` 基準でファイルが実在する以上、
  別基準を試すのは別ファイルを読むことになる)
- 絶対 operand も再解決しない (基準を変えても同じ path)
- 再解決で得た情報は **別ディレクトリの同名ファイルの可能性**があるため
  status を `project_root` にし、reason 側でラベル (「project root 基準で
  解決した候補」) と「確実に特定するなら Read tool に絶対パス」の注記を付ける
- あわせて 4 番目の戻り値 `resolved_base` (project root の basename **1 要素**)
  を `resolved_base: <name>/` 行として出す。ラベルだけでは「候補かもしれない」
  としか言えず、読み手が **実際に取り違えたのか** を確認できないため。
  絶対 path は出さない (`matched_operand` が相対 path を出す現行方針の範囲)。
  `resolved_base` は **ログには渡さない** (ログ規則で basename は禁止)

**E4 の grep extraction** (`handlers/bash/grep_extract.py::extract_grep_keys`):

- 抽出対象: env-var 形式 (`[A-Z][A-Z0-9_]{2,}`) を `re.finditer` で全 token から
  拾う
- `-e PATTERN` / `-E PATTERN` / `-G PATTERN` (次 token consume)、`--regex=...` /
  `--pattern=...` / `-e=...` (RHS) に対応
- `--` 以降は positional 扱いで pattern 抽出停止、short option (`-i` 等) は skip
- `|` 分割は `re.finditer` の境界処理で自然に処理 (`A_KEY|B_KEY` から両方抽出)
- 出現順 dedup された list[str] を返す

**`bash_deny` シグネチャ** (positional 互換維持):

```python
def bash_deny(
    first_token: str,
    operand: str,
    *,
    command: str = "",          # head/tail の -n N 抽出に使う
    file_render: str = "",      # render_for_bash の 1 番目の戻り値
    dotenv_info: dict | None = None,  # render_for_bash の 2 番目の戻り値
    grep_keys: list[str] | None = None,  # extract_grep_keys の戻り値
    render_status: str = "",    # render_for_bash の 3 番目の戻り値 (0.16.0)
    resolved_base: str = "",    # render_for_bash の 4 番目の戻り値 (0.16.0)
) -> str: ...
```

旧 0.7.0〜0.9.0 の `bash_deny(first_token, operand)` 呼び出しは generic
builder で 0.9.0 とほぼ同等の出力を生成するため互換維持。

### Bash handler (三態判定)

コマンド別の deny / allow / ask は [MATRIX.md](./MATRIX.md) を参照。mermaid
フロー図は [MAINTAINING.md](./MAINTAINING.md#bash-handler-判定フロー-0190) 側にある。

**unified operand scan**: 全セグメントで非 option トークンを一律
`_operand_is_sensitive` (literal path / URI / VCS pathspec) または
`_glob_operand_is_dotenv_match` (glob 含み、dotenv stem 一致) に通す。コロンを
含む operand (`HEAD:.env`, `user@host:/p/.env`) はコロン分割後の各片の basename も
判定。コマンドが実際に file を読むかどうかは静的に判別しないため false positive
(`echo .env`, `ls .env`, `mkdir .env`) が出るが、`patterns.local.txt` の
`!<basename>` exclude で個別対処できる。glob で dotenv stem と一致しないものは
``ask_or_allow`` (0.8.0)。

### Edit/Write handler

| ケース | 判定 |
|---|---|
| 機密 path への新規/既存 書き込み (通常ファイル) | **`deny` 固定** + dotenv ならキー名を reason に添える |
| 機密 path + symlink / special | **`deny` 固定** + 対応の extra note |
| `.env.example` 等テンプレ除外 | allow |
| 親ディレクトリが symlink / 特殊 / 不在 | `ask_or_deny` (判定不能、fail-closed) |
| patterns.txt 読込失敗 / normalize 失敗 / stat 失敗 | `ask_or_deny` (fail-closed) |

deny reason のキー名ガイド:
- dotenv 系 basename (`_detect_format(basename) == "dotenv"`) の時だけ
  `tool_input` からキー名抽出 (Edit=new_string / Write=content)
- 抽出結果を reason に箇条書きで添え、`.env.example` への移行を促す
- 値そのものは一切 reason に含めない (キー名のみ、既存の minimal-info 原則と一致)

`ask_or_deny`: `permission_mode == "bypassPermissions"` なら `deny`、それ以外は
`ask`。**機密検出済み** のケースは `ask` を挟まず常に `deny` 固定 (うっかり
承認防止)。

### Stop handler

| ケース | 判定 |
|---|---|
| `stop_hook_active=true` | exit 0 (ループ防止) |
| cwd が git 管理下でない | exit 0 |
| tracked でパターン一致 | `decision: block` (`.gitignore` 済みでも) |
| untracked でパターン一致 + `.gitignore` 未登録 | `decision: block` |
| 現在の (status, path) 集合 ⊆ 同一 session で報告済みの集合 (= 新規ファイル無し。0.19.0) | exit 0 (`session_id` 無し / 不正なら従来通り block) |
| 新しい機密ファイルが増えた / untracked → tracked に変わった (0.19.0) | `decision: block` (再通知、報告済み集合を更新) |
| patterns.txt 読込失敗 (FileNotFoundError / OSError) | exit 0 + stderr warning (fail-open) |

#### session 単位の once-only (0.19.0, bd_092a232e-snw.2)

0.18.0 までの Stop hook は `stop_hook_active` しか見ず「報告済みか」の状態を
持たなかったため、「意図的に管理対象とする」と承認された tracked ファイル
(Next.js 慣例の `.env` commit / committed CA 証明書 `*.pem` / direnv の `.envrc`)
で **毎ターン同じ block** が出続け、0.14.0 離脱と同型の体験になっていた。

- 状態: `~/.claude/sensitive-files-guardrail/stop-ack/<session_id>`
  (`hooks/check-sensitive-files/stop_ack.py`)。`patterns.local.txt` と同じ
  `Path.home()` 基準で、plugin cache の更新で消えず、テストは `HOME` 差し替えで
  隔離する。内容は 1 行 1 `sha256("<status>\t<絶対パス>")` で、平文 path は HOME
  に残さない (ログ規則と同じ方針)。鍵は `realpath(<repo root>)/<prefix><path>`
  (root だけを物理パスに正規化し、entry は lexical なまま。`prefix` =
  `git rev-parse --show-prefix`、cwd の root からの相対) — `git ls-files` は cwd
  相対で出力するため、(1) 別 repo の同じ相対 path (`tracked\t.env`) を報告済みと
  誤認しない、(2) root とサブディレクトリで同じ物理ファイルが別 digest にならない
  (Codex R2 P2-2)、(3) superproject から `--recurse-submodules` で拾った `sub/.env`
  と submodule 内 cwd (toplevel が submodule root に変わる) で拾った `.env` が同じ
  digest になる (Codex R4 P2-1)、(4) 別 repo の `.env` symlink が同じ共有ファイル
  を指しても digest は別のまま (entry を dereference すると 1 つ目の ack で 2 つ目
  の repo の通知が消える、Codex R5 P2-1)、を同時に満たす。block reason の表示は
  従来通り cwd 相対 (`git rm --cached <path>` を cwd でそのまま実行できる)。
  toplevel / prefix は `checker.repo_context` (`rev-parse --show-toplevel
  --show-prefix` 1 回) で得て `is_git_repo` の呼出を置き換えるので git 呼出回数は
  不変。スキャン範囲は従来どおり cwd の subtree (sub で block した後 root に戻る
  と、root 直下の未報告ファイルだけが新規として再 block する)
- 判定: 現在の digest 集合が報告済み集合の **部分集合** なら exit 0。増えていれば
  block し、`報告済み ∪ 現在` を保存する。hash 1 本ではなく集合を持つのは
  「1 件対応して残りが減った」ときに再 block しないため。status を digest に
  含めるので untracked → tracked (`git rm --cached` が必要になる) は新しい事象
  として再 block する
- `session_id` は `^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$` のみ受理 (path traversal /
  dotfile 防止)。欠落・不正は state を使わず従来通り毎回 block
- 失敗 (読取 / 書込 / mkdir 不能 / 壊れた行) は全て「状態なし」= block 側に倒す。
  state 機構の不具合で block が **消える** 方向には倒さない
- 書込み時に「最後の block から 7 日」を過ぎた session ファイルを best-effort
  GC する (state の mtime は block 時のみ更新)。削除するのは内容が digest 行だけ
  の「自分が書いた形」のファイルのみ (`_looks_like_state_file`、L2 review)
- state の読取 / 書込失敗は `warn` callback 経由で stderr に
  `stop_ack_unavailable: load:<Exc>` / `save:<Exc>` を 1 行出す (不在 = 初回は
  出さない)。判定は従来通り block
- block reason には恒久除外レシピ (`[project:$CLAUDE_PROJECT_DIR]` + `!<basename>`
  行、`_shared.patterns.exclude_recipe_lines`) と「このセッションでは同じ集合を
  再 block しない」注記を載せる。絶対パスは出さない (ヘッダーは環境変数名で示し、
  書き込む側が実パスに置き換える)。`$CLAUDE_PROJECT_DIR` は Bash tool の環境で
  未設定なので unquoted echo だと空に展開され、quoted heredoc / Write だと literal
  に残る — どちらも一致しないため、`_parse_local_patterns_text` の
  `header_warn_callback` が固定トークン (`project_header_empty` /
  `project_header_unexpanded_placeholder`) で警告する (read 側は
  `local_patterns_header_invalid` として logfile + stderr、Stop 側は stderr)。
  判定は変えない (そのセクションは非 active のまま)。placeholder 判定は未展開の
  変数参照の standalone 形 (ヘッダー値が `$NAME` / `${NAME}` そのもの、または
  それで始まり直後が `/`。`$CLAUDE_PROJECT_DIR` もこの形のみ) に限定し、
  `/work/project$prod` のような `$` 入り literal パスや
  `/work/repo$CLAUDE_PROJECT_DIR-prod` のように予約語を部分文字列として含むパス
  は正当なヘッダーとして比較する (Codex R2 P2-1 / R4 P2-2。当初の「`$` を含めば
  placeholder」はその repo の project スコープの include / exclude を黙って無効化
  していた)

## 既知制限 (0.14.0 で整理、以降の変更は各項目に版を付記)

1. **MCP 経路は対象外** — MCP server 経由のファイルアクセスは hook が介在しない
2. **Bash 間接アクセス (静的解析不能)** — `bash -c`, `eval`, `python3 -c`, `sudo`,
   `xargs`, heredoc, process substitution, `/bin/cat`, `./script`
   などは静的解析できず、default モードでは ask、auto/bypass モードでは
   **allow** に倒す (`awk` / `sed` は 0.17.0 で opaque を外れ、0.18.0 の
   hard-stop quote-aware 化で `awk '{...}' .env` 形も operand scan に到達する)。0.8.0 で `FOO=1 cat .env`, `env cat .env`,
   `command cat .env`, `nohup cat .env`, `/usr/bin/env FOO=1 cat .env` も
   ``ask_or_allow`` に格下げした (0.3.2〜0.7.x の prefix normalize は撤廃)。
3. **`<` 入力リダイレクトは ask_or_allow 扱い (0.7.0、0.11.0 で segment 単位
   再評価)** — 0.3.4〜0.6.x では character-level quote-aware parser で
   `cat < .env` / `cat<.env` / `cat 0<.env` / `cat < ".env"` などから target
   を抽出し literal/glob 一致で deny に倒していたが、`cat <(echo \(\)) < .env`
   の escape paren depth tracking や `[[ ... ]]` 引数位置判定など敵対的バイパス
   対策のコードが思想 1 (うっかり露出予防が目的、敵対的防御は非目的) に反する
   ため 0.7.0 で撤廃。`<` を含む segment は他の hard-stop と同じ
   ``ask_or_allow`` (default で ask、autonomous で allow) に倒す。0.11.0 で
   segment 単位再評価に細粒度化したため、`<` が含まれる segment は当該 segment
   のみ ask に倒り、他 segment が literal match すれば deny に到達する
   (`cat $X | ls .env | head` 等)。攻撃シナリオ `cat <(echo \(\)) < .env` は
   全 segment が hard-stop となるため挙動不変。
4. **glob operand の判定は dotenv stem 限定 (0.8.0)** — 0.3.2〜0.7.x で行っていた
   既定 rules 候補列挙 (`_glob_candidates` / `_glob_operand_is_sensitive`) は
   `cat *.log` `cat *.json` のような日常 glob まで「`.env` rule との連結候補」で
   deny に巻き込む False positive があり、思想 1 と整合しないため 0.8.0 で撤廃。
   現在は operand glob が dotenv literal stem (`.env` / `.envrc`) に
   ``fnmatchcase`` で一致するときだけ deny 固定 (`cat .env*`, `cat *.envrc`,
   `cat .e[n]v`, `cat .en?`, `cat [.]env`)。それ以外の glob (`id_rsa*`,
   `*.key`, `cred*.json`, `*.log`, `.env.*`, `.env.example*` 等) は
   ``ask_or_allow`` (default=ask, autonomous=allow)。
5. **autonomous モードでの opaque 緩和** — `bash -c 'cat .env'` の
   ような shell wrapper 内に機密 path があっても auto/bypass では allow に
   倒る。wrapper 内部の script を解析しないため検出できない。autonomous モード
   を選んだユーザーが「日常コマンドを止めない」意図と平等な扱いとしての設計上の
   トレードオフ。完全防御を求める場合は default モードで運用する。
6. **`__main__.py` catch-all は未緩和** — bash handler 内部で未捕捉
   例外が起きた場合、`__main__` 側の catch-all は従来通り `ask_or_deny`
   (auto=ask / bypass=deny)。tool 種別だけで一律 lenient にすると fail-closed
   境界が粗くなる。
7. **親ディレクトリ差し替え race** — `O_NOFOLLOW` は最終要素のみ保護し、
   途中要素の symlink 差し替え race は対象外 (原理的に完全防御不能)
8. **TOCTOU 完全排除は非目的** — hook 読取と Claude 実 Read/Write の分離は範囲外
9. **`<DATA untrusted>` モデル解釈保証なし** — 包装 + sanitize + DATA タグ
   エスケープで多段防御するが、モデルが敵対的文脈として扱う保証は無い
10. **Windows は fail-closed で deny exit** — SIGALRM 非対応のため hook 冒頭で
    deny exit する (Step 0-c 実測結果確定前の暫定方針)
11. **submodule 内 untracked は非対象** — `git ls-files --recurse-submodules` は
    tracked のみ。untracked を submodule 内まで拾う git native オプションは無い
12. **Git バージョン依存** — `--recurse-submodules` は git 1.7+ が必要
13. **`!` プレフィックス (Claude Code bash mode) は防御対象外** — ユーザーが
    プロンプトに `! cat .env` と直接入力してシェルコマンドを実行した場合、
    公式仕様により **stdout が transcript に追加されて LLM コンテキストに流れ
    込む**。これはユーザーの明示的な意思操作なので hook の介在外
14. **Bash redirect 書込み (`echo KEY=val > .env` / heredoc `cat > .env <<EOF`)
    は受容** — residual metachar / hard-stop 経由の `ask_or_allow` に倒るため、
    autonomous モードでは allow で通る。Edit/Write tool 経路の deny 固定とは
    非対称だが、**ユーザー確認 (2026-06-12) で「受容」に確定**: 本 plugin の
    主目的は悪意のないうっかり露出の予防であり、セキュリティを担保する
    plugin ではない。redirect / heredoc で機密 path に書き込む形はうっかりの
    範疇を超えるため対象外として通す (「うっかり予防のついでに少し守れれば
    十分」の思想)。ただし **metadata-only ∩ safe_read コマンド**
    (`ls` / `stat` / `wc` 等) の `ls > .env` 形だけは 0.14.0 で metadata-only
    shortcut を入れた結果 regression したため、`_sensitive_redirect_target` で
    deny を復活させている (Codex P2)
15. **`>|` clobber override redirect は未対応** — `tree >| .env` の `>|` は
    `|` が segment 分割で pipe として割られ `tree >` と `.env` に分離するため、
    機密 redirect target を検出できず allow に倒る。`>|` を意図的に書くのは
    `noclobber` を理解した上級者で「うっかり」ではない (思想 1 射程外) ため
    既知限界とする。`>` / `>>` / `n>` / `&>` の通常 redirect は検出する

## Edit/Write hook の発火経路 (2026-04-18 実機観測)

Claude Code CLI 2.1.112 における **Edit/Write tool の PreToolUse hook** は、tool
呼び出しの状況によって発火の有無が変わる:

| 操作 | 既存ファイル | 新規作成 |
|---|---|---|
| `Edit` | **hook 未到達** (Read 前提チェックで先に `File must be read first`) | — (Edit は既存前提) |
| `Write` | **hook 未到達** (同上、`Error writing file`) | **hook 発火** (redact-sensitive-reads deny で block) |

現在の防御は二層構造:

1. **本線 (hook)**: 新規作成 Write → edit_handler → deny
2. **副次 (Claude Code 内蔵)**: 既存ファイル Edit/Write → Read 前提チェック → 内部エラー

redact hook が Read を deny している状態では、Claude が Read を試みると失敗 →
Claude が Edit/Write を試みても「Read 済み」にならないため Claude Code が先に弾く。
この **Read 前提チェックの副次防御** により、既存機密ファイルの Edit/Write は
hook まで到達しなくても block される。

将来 Claude Code がこの仕様を変更した場合 (例: bypass モードで Read 前提を緩和)、
副次防御が消えるため**本線の hook が唯一の防御になる**。したがって Edit/Write の
matcher と edit_handler は dead code ではなく、**設計上の必須コンポーネント**。

## glob operand 判定の歴史 (0.3.2 → 0.8.0)

operand glob (`*` / `?` / `[`) の判定は数世代を経ている:

- **0.3.2〜0.7.x**: `_glob_candidates` で operand glob と既定 rules の literal stem を
  fnmatch 交差させて候補化し、`_glob_operand_is_sensitive` で is_sensitive 判定。
  プランの初期案に「op_stem + pt_stem 連結候補」を加える項目もあったが、`*.log`
  に対して `.env` rule との連結 `.env.log` が候補化されて `cat *.log` が deny に
  巻き込まれる False positive があり、連結候補は不採用としていた。
- **0.8.0**: `_glob_operand_is_sensitive` / `_glob_candidates` / `_literalize` を全
  撤廃。dotenv literal stem (`.env` / `.envrc`) に operand glob が ``fnmatchcase``
  で一致するかだけ見る `_glob_operand_is_dotenv_match` に置換。
  `cat *.key` `cat id_rsa*` `cat cred*.json` `cat *.log` `cat .env.example*` 等は
  すべて ``ask_or_allow`` (default=ask, autonomous=allow) に格下げ。思想 1
  (うっかり露出予防、敵対的防御は非目的) に整合させた結果。

## Step 0-c 実測 (将来更新予定)

プラン v3 の Step 0-c (outer timeout 発火時の Claude 挙動実測) は未実施。
暫定方針として Case A (timeout kill → allow/fail-open の最悪ケース想定) で
Windows (SIGALRM 非対応) を hook 冒頭で deny exit にしている。

実測手順は [MAINTAINING.md](./MAINTAINING.md#step-0-c-実測結果-将来更新予定) の
「Step 0-c 実測結果」セクション参照。
