# Changelog

external-ai-assist の変更履歴。0.3.1 以前は CHANGELOG が無く、各節は git log
(38e07e3 / af82155 / d1443d5 / ce17aff / ae57f87 / 547ba0d / dac1957) から起こした。
plugin.json の `version` は pin として働く (bump しない限り既存ユーザーに届かない) ため、
version 据え置きで main に入った後続 commit はその version の節に併記している。

## 0.5.0

**post-implementation-review: 機密・非コードファイルを外部に送らない除外機構 + diff 予算を
ファイル単位に変更 (切り落としたファイルをレビュー済みにしない)** (2026-08 精査バックログ
zh5.9 / zh5.8)。送信内容が変わるので minor bump。

### 1. 外部に送らないファイルの除外 (zh5.9)

0.4.1 までは編集パス全件の HEAD 基準 diff (untracked は全文) を Cursor に渡し、除外は
gitignore 済みのみだった。tracked の `.env` / `*.pem` / 認証情報、議事録や顧客メールの
`.txt` も外部 AI に送られていた。

- 新設 `hooks/post-implementation-review/exclusion.py`。判定は次の順で、先に当たったものが勝つ:
  0. **`EXTERNAL_AI_POST_REVIEW_EXCLUDE` の `!glob`** (否定) に当たれば必ず送る
  1. **既定除外** — glob: `.env` `.env.*` `*.env` `.envrc` / `*.pem` `*.key` `*.p12` `*.pfx`
     `*.p8` `*.jks` `*.keystore` `*.ppk` `*.gpg` `*.pgp` `*.asc` `*.kdbx` / `id_rsa*` `id_dsa*`
     `id_ecdsa*` `id_ed25519*` / `*service-account*.json` `*service_account*.json` `kubeconfig*`
     `*.ovpn` / `.htpasswd` `*.htpasswd` `.netrc` `_netrc` `.npmrc` `.pypirc` `.pgpass`
     `.git-credentials` / `*.tfstate` `*.tfstate.*` `*.tfvars` `*.tfvars.*`。
     語: `secret` `secrets` `credential` `credentials` をパス (ディレクトリ名含む) に**単語として**
     含む (`EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS=0` で無効化可)
  2. **`EXTERNAL_AI_POST_REVIEW_EXCLUDE`** — カンマ区切りの追加 glob (`dir/` は `dir/*`、先頭の
     `./` `/` は無視、brace 展開は非対応)
  3. **`EXTERNAL_AI_POST_REVIEW_CODE_ONLY=1`** — 文書 / データ / メール / 画像・音声・動画 /
     アーカイブの拡張子を外す (一覧は README)。JSON / YAML / TOML / XML / HTML / CSS と
     拡張子無しのファイルはコード扱いで残す
- glob は basename と作業ツリー相対パスの両方に、大文字小文字を区別せず当てる。`fnmatch` の
  `*` は `/` にもマッチするので `docs/*` は深い階層も拾う。symlink は **lexical なパス**
  (作業ツリー root だけ realpath で同定し、配下の symlink 構成要素名はそのまま残す) と実体
  (realpath。git に渡すのもこれ) の両方で判定し、どちらかが当たれば除外する。同じ実体が
  別名で複数回 claim されていても全部の名前を見てから判定する。通知には当たった名前を出す。
  lexical 判定は Codex PR レビュー P1 の反映: `credentials/` → `ordinary/` のような symlink
  ディレクトリ経由の claim は realpath でも親だけの realpath でも `ordinary/data.json` になり、
  `credentials` が判定から消えていた。root の別名 (`/tmp` → `/private/tmp`、symlink された
  親ディレクトリ) は引き続き realpath で吸収する
- **判断**: 既定は「名前からして機密」なものに限定した。`*token*` / `*password*` は
  tokenizer / password_validator などのコードを巻き込むため入れず、`id_*` も
  `id_generator.py` を拾うので SSH 鍵の実名 (`id_rsa*` 等) に限定。チケット案の `*secret*` /
  `*credential*` glob は `secretary.py` / `secretsanta.ts` / `nosecret` まで拾う (L2 レビュー)
  ため、英数字以外で区切られた単語としてだけ当てる方式に変えた。それでも
  `credentials-service/main.go` のようにディレクトリ名が当たると配下のコードが丸ごと永久に
  未レビューになるので、`!glob` で個別に戻せるようにした (既定の無効化スイッチは全既定を
  失うので逃げ道としては粗い)
- **除外は恒久**: `_resolve_paths` で作業ツリー外パスと同じく claim から落とし、pending にも
  reviewed にも残さない。`MAX_REVIEW_PATHS` の手前で落とすので枠を食わず、overflow として
  pending に戻ることもない。0.4.1 以前が state に積んだ機密パスも次の Stop で落ちる
- 除外したターンは `systemMessage` (Stop でも有効な公式の共通フィールド。block 時は
  `decision` / `reason` と同居) と stderr に件数・ファイル名・理由 (当たったパターン) を出す。
  内容は出さない。他 plugin (sensitive-files-guardrail 等) には依存しない

### 2. diff 予算をファイル単位に変更 (zh5.8)

結合テキストを `MAX_DIFF_BYTES=40000` で末尾から切る方式だったため、切り落とされた
ファイルの hash も `complete_claim` で記録され、Cursor が一度も見ていないファイルが
「レビュー済み」になって内容が変わるまで再掲されなかった。

- `_collect_diffs` がファイル (セクション) 単位で予算に積む。収まらないファイルは**送らずに
  pending へ戻し、hash を記録しない** (次ターンにそのまま再掲)。後続の小さいファイルは
  予算が残っていれば送る (first-fit)
- 1 ファイルが `MAX_FILE_DIFF_BYTES=32000` を超える場合だけ先頭を行境界で切り
  `(truncated for review: ...)` を明示して送り、**この場合のみ** hash (全文で計算) を記録する。
  32 KB (合計の 80%) にしたのは、切り詰めは「その diff の末尾を二度と見ない」恒久的な損失で
  繰り越しは次ターンまでの遅延に過ぎないため、1 ファイルはなるべく丸ごと送る側に倒した
  (チケット例示の 16 KB だと 30 KB の diff が半分しか見えない)
- `_resolve_paths` がパスをソートせず **claim 順 (= pending に積まれた順)** を保つよう変更。
  繰り越したパスは次ターンの pending 先頭に来るので、予算超過で繰り越されたファイルが
  新しい編集に毎回追い越されて永久に残らない (`MAX_FILE_DIFF_BYTES <= MAX_DIFF_BYTES` で
  先頭のファイルは必ず収まる)。繰り越しは claim 順 (予算超過 → 時間切れ → `MAX_REVIEW_PATHS`
  の overflow) で 1 回の `record_pending` に積み、`MAX_PENDING_PATHS` の上限超過は末尾 (新しい
  編集) から落とす (従来は先頭 = 繰り越し分から落としていた)
- 繰り越し・切り詰めも除外と同じく `systemMessage` / stderr で通知 (対象ファイル名のみ)
- README の「差分は 40 KB まで、超過分は truncate」を実装に合わせて書き換え。0.4.1 以前が
  「見ていないのに reviewed」として記録した hash は state に残る (pending に居残っていた分だけの
  狭い影響なので state の `v` は据え置き。該当ファイルは次に差分が変わった時点で再掲される)

### 3. git pathspec の literal 化と `--no-color` (L2 レビューで発見、0.2.0 からの潜在バグ)

- `gitscan._git` が渡すパスを git が **glob として解釈**していた (`*` `?` `[...]`)。
  `app/[id]/page.tsx` (Next.js の動的ルート) をこのセッションが編集すると `app/i/page.tsx` にも
  マッチし、別セッションが編集した (claim も除外判定も通っていない) ファイルの diff が同じ
  section に混入して cursor に送られていた。旧 state に残った `[.]env` のようなエントリが
  tracked の `.env` を拾う経路も同じ。除外機構の保証を成立させるため
  `git --literal-pathspecs` で全コマンドを起動する
- `git diff` に `--no-color` を付けた。`color.ui=always` / `color.diff=always` の環境では ANSI が
  本文に混ざり、レビュアーに渡す diff が汚れるうえ hash とバイト予算が狂っていた

### 4. docs

README (動作サマリ / 除外規則の節 / 環境変数表 / ファイル構成)、
`hooks/post-implementation-review/CLAUDE.md` (構成 / 復元表 / 除外と予算の節 / テスト表)。
`systemMessage` の表示は公式 docs (全イベント共通フィールド、Stop で discard されない)
に基づく。対話 UI 以外 (Agent SDK / `stream-json`) での表示は未確認のため、同じ内容を
stderr にも残している。

### テスト

累計 **248 件** (+62、post-implementation-review 102 → 164)。テストは git のグローバル /
システム設定を読まず (`GIT_CONFIG_GLOBAL=/dev/null` `GIT_CONFIG_NOSYSTEM=1`)、除外の環境変数を
中立値に固定して実行する (開発者 shell の `CODE_ONLY=1` や `color.ui=always` で揺れない)。

- `tests/test_exclusion.py` (新設 21 件): 既定 glob・語 34 種の除外 / コード 17 種の非除外
  (`id_generator.py` `tokenizer.py` `password_validator.py` `secretary.py` `secretsanta.ts`
  等) / 語境界 / 大文字小文字 / サブディレクトリ・ディレクトリ名 / 理由表記と当たった名前 /
  複数候補 / 追加 glob の加算と正規化 (`./` `/` `dir/` 空要素 `!`) / 異常パターンで例外なし /
  `*` の階層またぎ / 既定の無効化 / `!glob` が既定・追加 glob・CODE_ONLY に優先 /
  CODE_ONLY の拡張子 20 種と残す 14 種 / 真偽値
- `tests/test_stop_flow.py::TestExclusion` (12 件): 機密のみで cursor 不起動 + pending に
  残らない + systemMessage に内容が出ない / 機密 + コードでコードだけ送り次ターンに再掲しない /
  block 時の `decision` と `systemMessage` の同居 / 除外なしなら出力なし / 追加 glob /
  CODE_ONLY / Bash 経由で作った `.env` / 機密名の symlink / symlink ディレクトリ
  (`credentials/` → `ordinary/`) 経由の編集 / 旧 state の機密パス / 枠を食わない /
  `!glob` で credentials ディレクトリ配下のコードを送る
- `tests/test_stop_flow.py::TestLiteralPathspecFlow` (2 件): `app/[id]/page.tsx` の編集に別
  セッションの `app/i/page.tsx` が混入しない / 旧 state の `[.]env` で tracked `.env` が漏れない
- `tests/test_stop_flow.py::TestByteBudgetFlow` (5 件): 30 KB × 2 で 2 件目が pending に
  戻り hash 未記録、次ターンでレビュー / 繰り越しが新しい編集に追い越されない / 予算超過と
  overflow の繰り越し順 / 単一 50 KB が切り詰め付きで送られ hash 記録 (末尾だけ変えても再掲) /
  cursor 失敗で両方 pending
- `tests/test_review_set.py`: `_resolve_paths` の claim 順維持・除外・symlink 両名 (実体が先に
  claim されても / alias root 経由でも / symlink ディレクトリ経由でも lexical 名で除外、実体名の
  claim は送る、`_lexical_relative` の構成要素保持)・枠非消費、`_collect_diffs` の `ReviewBatch` 化、
  `TestByteBudget` (first-fit / 切り詰め / 上限の制約)、`_truncate_section` (上限内 / 行の
  途中に落ちる limit での行境界 / 1 行 / multibyte)
- `tests/test_gitscan.py`: literal pathspec (tracked / untracked の `[...]` 名、glob 風エントリが
  何にも当たらない) / `color.ui=always` でも ANSI が混ざらない
- `tests/test_state.py`: pending 上限は末尾から落とし繰り越し分を守る

## 0.4.1

**explore-parallel の cursor agent を読み取り専用 (`--mode plan`) で起動する** (2026-08 精査
バックログ zh5.2)。挙動変更はこの 1 点のみ。

1. **explore-parallel が書込可能モードで cursor を起動していた** — `cursor agent --trust -p`
   で起動しており `--mode plan` が無かった。cursor-agent (2026.08.11) の help では `-p/--print`
   単独は「Has access to all tools, including write and shell」、`--mode plan` が
   「read-only/planning (no edits)」。読み取り専用の Explore の裏で、作業ツリーを書き換えうる
   agent が走る状態だった (exitplan-review / post-implementation-review は 0.2.0 から
   `--mode plan` 付きで、この 2 hook の挙動は変わらない)
2. **起動 argv を 3 hook で統一** — `hooks/_common/cursorcli.review_argv` を `readonly_argv` に
   改名し、explore-parallel もこれを使う。3 hook とも
   `cursor agent --trust --print --mode plan <prompt>` で起動する (待機方式は hook ごとに従来
   どおり: explore-parallel はバックグラウンド Popen + 結果ファイル、review 系は
   `subproc.run_for_output`)
3. `--sandbox enabled` は付けていない — cursor-agent 側の既定値と `--mode plan` との組み合わせを
   本環境で確認できていない (実機起動なしで判断)。必要になれば別 issue
4. docs: README (前提 / explore-parallel 動作サマリ / 設計原則 7「外部 AI は読み取り専用で
   起動する」/ ファイル構成)、`hooks/explore-parallel/CLAUDE.md` (テスト節・設計判断の履歴)

### テスト

累計 **186 件** (+9: explore-parallel 5 新設 / `_common` 3 / post-implementation-review 1)。
全て cursor / codex を起動せず、PATH 先頭の偽 CLI (bash script) で検証する。

- `explore-parallel/tests/test_cursor_launch.py` (新設。TMPDIR 隔離 + 偽 cursor。
  `state.BASE_DIR` が import 時に決まるため `cursor` / `state` をテストごとに読み直す):
  pre が `--mode plan` 付きで起動し Explore の prompt を最後の 1 引数で渡す / argv が
  `cursorcli.readonly_argv` と完全一致 / Explore 以外では起動しない / post が結果を
  `additionalContext` に注入し pid・結果ファイルを掃除する / post の待機上限 + poll 1 刻みが
  hooks.json の PostToolUse(Agent) timeout に収まる (他 hook と同じ予算テスト)
- `_common/tests/test_cursorcli.py`: `readonly_argv` の argv 契約 (`--print --mode plan`、
  フラグに見える本文も 1 引数のまま末尾)
- `post-implementation-review/tests/test_cursor_argv.py`: `review()` の argv
  (`--print --mode plan`) と diff の埋め込み (従来は `review()` を mock しており未検証だった)
- exitplan-review は既存 `test_cursor_runs_print_mode_read_only_with_plan_in_prompt` が
  同じ契約を固定済み

## 0.4.0

**コードフェンス付き REVIEW_CLEAN の誤 block 修正 + hook 共通ヘルパー `hooks/_common/`
新設 + 外部 CLI の process group 停止 + CHANGELOG 新設** (2026-08 精査バックログ
zh5.1 / zh5.27 / zh5.15 / zh5.19 を 1 batch で対応)。

### 1. REVIEW_CLEAN 判定の修正 (zh5.1, 誤 block)

`is_clean_review` は「非空行が 1 行だけ」を要求していたが、prompts 自体が sentinel を
コードフェンス内で提示していたため LLM はそれを写し、

    critical 指摘はない
    ```
    REVIEW_CLEAN
    ```

という実出力 (2026-08-20 の Stop hook) が指摘扱いになって `decision: block` が出ていた。
exitplan-review ではこの誤 block が `EXTERNAL_AI_REVIEW_MAX` の枠も消費する。

- **判定を `hooks/_common/sentinel.py` に集約**し、次の規則に変更:
  1. コードフェンス行 (``` / ~~~、言語指定付き含む)・装飾だけの行 (`---` / `***` 等)・
     空行を除く
  2. 残りがすべて sentinel 行 (`` ` `` / `*` / `#` / 括弧 / 句読点を除いて `REVIEW_CLEAN`)
     なら clean
  3. 残りが「sentinel 1 行 + もう 1 行」で、その 1 行が **「指摘なし」を述べる短い 1 文**
     (箇条書き / パス / 行参照 / 強調 / インラインコード / 逆接を含まず 80 文字以下で、
     「指摘はない」「No critical issues」等の否定文で文が終わる) なら clean
  4. それ以外 (sentinel + 指摘本文、指摘のみ、3 行以上) は従来どおり clean ではない
- **判断**: 前置き 1 文を許容するかは安全側との trade-off。許容しないと実出力相当が
  block され続けるため許容するが、行全体が否定文の allow-list に **完全一致** し、指摘本文
  の兆候が無い行だけに限定した。「問題ない箇所もあるが X は壊れる」のように否定の後に
  本文が続く行、「認可チェックが抜けている点以外は問題なし」「Missing null check;
  otherwise no issues」のように本文の後ろに否定文が付く行 (L2 レビューで見つかった
  search + 末尾アンカーの穴)、"No critical issues, but ..." のような逆接、`src/app.py:12`
  などのパス参照、文の区切り (`。` `;` `,`) を含む行は前置きとして認めない。
  sentinel を含まないフェンス / 罫線だけの出力も指摘扱い。誤 block は「指摘なしの本文を
  Claude が読んで無視する」1 ターンの損失で済むが、誤 clean は critical feedback を
  silently drop するため、迷ったら clean にしない
- **prompts** (`planning-cursor.md` / `planning-codex.md` / `post-implementation-cursor.md`):
  sentinel をフェンス無しの 1 行として提示し、「コードフェンスで囲まない」を明記
- cursor の `--output-format json` 採用は見送り (sentinel 判定で吸収でき、出力契約を
  増やさない。必要になれば別 issue)

### 2. hook 共通ヘルパー `hooks/_common/` の新設 (zh5.27)

3 hook で同型だった処理を `hooks/_common/` に集約し、各 hook の `__main__.py` 冒頭で
`hooks/` を `sys.path` に載せて参照する。`__file__` 相対の plugin root 内配置なので、
`${CLAUDE_PLUGIN_ROOT}` が `~/.claude/plugins/cache/` のコピーを指していても解決する
(`_common/tests/test_bootstrap.py` が plugin root をコピーして各 hook を起動し、これを固定)。

| module | 置き換えた重複 |
|---|---|
| `sentinel.py` | `is_clean_review` ×2 (exitplan-review / post-implementation-review) |
| `subproc.py` | `subprocess.run` ラッパ ×3 (cursor ×2 / codex)、`is_available` の `shutil.which` ×4 |
| `cursorcli.py` | cursor agent の存在確認と review 用 argv (`--trust --print --mode plan`) |
| `flock.py` | flock 付き read-modify-write (exitplan マーカー / post-implementation 状態ファイル) |
| `hooklog.py` | `[<hook>] msg` の stderr ログ ×3 |

- 共通化しないもの: hook 固有の状態機械と、review 中ずっと保持する非ブロッキングの
  `cursor_lock` (ロックファイルを開けない環境で直列化を諦める fail-open 分岐が固有)
- `run_reviewers` の `overall_timeout` を除去。各レビュアーが自前の `TIMEOUT_SEC` で必ず
  返り、`ThreadPoolExecutor` の with 終端が全 future を待つため、`as_completed` の
  timeout は実質効いていなかった (dead logic)
- README の設計原則 5 (「hook 間の共通ヘルパーなし」) を現状に合わせて更新

### 3. 外部 CLI の timeout 時に process group ごと停止 (zh5.15)

cursor / codex の起動を `subprocess.run(capture_output=True, timeout=…)` から
`_common.subproc.run_captured` (`Popen(start_new_session=True)` + `communicate(timeout)` +
`os.killpg(SIGTERM)` → 猶予 5s → `os.killpg(SIGKILL)` + 残出力の読み捨て) に置き換えた。

- **再現結果 (confidence=medium のため着手前に実測)**: チケットの「孫が stdout を握ると
  hook が harness timeout まで停止する」は **Python 3.8+ では再現しない**。現行 `run()`
  は POSIX では timeout 後に `kill()` + `wait()` だけを行い `communicate()` を呼ばない
  (3.9.6 / 3.14.0 で 1.00s で返ることを確認)。旧 `run()` (≤3.7) 相当の経路でのみ 30s
  ハングした。実害は「stdout を継承した孫が取り残されて走り続ける」リソースリーク
  (cursor-agent / codex の課金・CPU) で、本修正の位置づけは **孫プロセスのリーク防止**
- 新ヘルパー自身は kill 後に `communicate()` で読み捨てるため、SIGTERM を無視する孫が
  いても無期限待ちにならないよう 2 段 (TERM → 猶予 → KILL) にし、それでも EOF が来ない
  (グループを抜けた孫が pipe を握る) 場合は read 端を閉じて直接の子だけ回収する
- `run_captured` が起動した子 (pgid == pid を保証) には `killpg(pid)` でグループ全体に送る。
  リーダーが未 reap の間は pid が再利用されないので常に安全で、リーダーが既に zombie
  (cursor-agent 本体が落ちて helper だけが pipe を握るケース) でもグループに届く。
  `getpgid` でリーダー確認してから送る案は、macOS が zombie に ESRCH を返して
  `send_signal` → 内部 `poll()` が reap → signal 不達 → 孫が残る経路になるため不採用
  (L2 レビューで実測)。reap 済みの場合はグループにメンバーが残っている限り pgid 番号が
  再割当てされないことを利用し、`killpg(pgid, 0)` で存在確認してから送る (空なら送らない)。
  外部で作った Popen に対してだけ `getpgid` でリーダーかを判定し、リーダーでなければ直接の
  子のみに送る。hook 自身の process group に signal が飛ぶ経路は無い
- 「止まった」の判定は pipe の EOF ではなく **process group に生きたメンバーが居ないこと**
  で行う (Codex PR レビュー P2)。リーダーが死んで EOF が来ても、SIGTERM を無視して出力を
  /dev/null に向けたメンバー (`trap '' TERM; sleep >/dev/null &`) は残りうるため、猶予内に
  居なくならなければ SIGKILL を送る。全員が TERM で死ぬ通常ケースは probe で即座に返る。
  CLI が正常終了した後も同じ probe で残存メンバーを確認し、居れば同じ手順で止める
  (結果は捨てない)
- 生死判定は `killpg(pgid, 0)` の存在確認に加えて **zombie を除外** する (Codex R2 P2)。
  PID 1 が孤児を reap しない Linux コンテナでは、メンバー全員が死んでも zombie として
  グループに残り `killpg(pgid, 0)` が成功し続けるため、存在確認だけでは停止ごとに猶予 2 回分
  (本番定数で 10 秒) を待ってしまう。Linux では `/proc/<pid>/stat` (state が `Z` 以外かつ
  pgrp 一致)、`/proc` が無ければ `ps -A -o pid=,pgid=,stat=` (POSIX。`ps -g` は procps で
  session 選択になるため不使用) で数え、zombie だけなら即座に停止扱い。どちらも使えない
  ときは SIGKILL 送信後 `KILL_SETTLE_UNKNOWN_SEC` (0.5s) で待機を打ち切る。最悪所要時間
  (timeout + 3 × KILL_GRACE_SEC) は変わらない
- kill 猶予 (最悪 3 × 5s) を hook timeout に織り込むため、hooks.json の Stop timeout を
  660 → **690** に変更 (cursor 600 + 猶予 15 + git 49 = 664 が 660 を超えていた)。
  Stop / ExitPlanMode の予算式をテストで固定
- stdin は `input_text` が無ければ `/dev/null` (hook 自身の stdin = payload の pipe を
  子に継承させない)。出力は `errors="replace"` でデコードし、非 UTF-8 出力で例外にしない
- explore-parallel のバックグラウンド起動 (`os.kill(pid, SIGTERM)` でリーダーのみ停止)
  は zh5.13 (残骸 GC / PID 再利用対策) と合わせて扱うため本リリースでは変更していない

### 4. exitplan-review のマーカー読み取りバグ修正 (新規テストで発見)

`release_slot` が hash を空にすると本文は `"\n<count>"` になるが、読み取り側が全体を
`strip()` してから分割していたため `hash=<count>` / `count=0` と誤読し、枠を 1 つ戻した
だけで上限カウントがリセットされていた (block → 別プラン clean → 以降 1 回多く block
できる)。行単位で読むよう修正。

### 5. CHANGELOG 新設 / docs (zh5.19)

- `CHANGELOG.md` を新設し 0.1.0 → 0.3.1 を git log から起こした。撤廃済み環境変数
  `EXTERNAL_AI_POST_REVIEW_MAX` と互換挙動は 0.3.0 の Deprecated 節に記載
- README: 前提を Python 3.11+ に統一、設計原則 5 を更新、環境変数表の参照先を CHANGELOG
  に、ファイル構成に `CHANGELOG.md` / `hooks/_common/` / `exitplan-review/tests/` を追加
  (存在しない `CLAUDE.md` の行を削除)

### テスト

累計 **177 件** (post-implementation-review 98 → 101、exitplan-review 26 新設、
`_common` 50 新設)。全て cursor / codex を起動せず、モックか PATH 先頭の偽 CLI
(bash script) で検証する。

- `_common/tests/test_sentinel.py`: 素の sentinel / フェンス・装飾 / フェンスや罫線だけの
  出力は非 clean / 許容する前置き 20 種 / 拒否する 1 行 28 種 (本文 → 否定文の鏡像を含む) /
  sentinel + 指摘 / 文中埋め込み
- `_common/tests/test_subproc.py`: 孫が stdout を握る timeout で猶予込みの上限内に返り
  孫が死ぬ / 親子とも SIGTERM 無視 → SIGKILL / リーダーが先に exit (zombie) / リーダーは
  TERM で死に孫だけ TERM 無視 / pipe を握らず TERM を無視するメンバーが猶予後に SIGKILL
  される (リーダーが TERM で死ぬ型・リーダーも TERM 無視の型) / 全員 TERM で死ぬ通常ケースは
  猶予を待たない / 正常終了後に残った background メンバーを止める (残存なしなら遅延しない) /
  zombie だけのグループは待たずに settle・live が残れば SIGKILL 昇格・判定不能なら
  SIGKILL 後に短い上限で打ち切り (probe を mock) / `_group_state` の分類・`/proc/<pid>/stat`
  パーサ・実機 probe / stdin 入力あり / hook 自身の process group に届かない / リーダーで
  ない子には killpg しない / reap 済みでグループも空なら送らない / 出力契約 (非 0・空・
  非 UTF-8・コマンド不在)。孫の死亡判定は zombie も死亡扱い (PID 1 が reap しない環境対応)
- `_common/tests/test_flock.py`: RMW の往復 / truncate / 例外後の解放 / 8 スレッド直列化
- `_common/tests/test_bootstrap.py`: plugin root をコピーして 6 経路の hook を起動し
  import エラーが無い / `hooks/` 直下に `.py` や import 可能な名前のディレクトリが無い
- `exitplan-review/tests/test_sentinel_flow.py`: 実出力相当の clean で block せず枠が戻る /
  指摘で block・マーカー消費・参照コピー / 片方 clean 片方指摘 / 両方失敗 fail-open /
  例外 / 同一 hash skip / 上限 / release 後の上限維持 / 早期 return 群
- `exitplan-review/tests/test_cli_timeout.py`: 偽 cursor / codex の timeout で孫が死ぬ /
  argv (`--print --mode plan` / `exec -s read-only --ephemeral`) と stdin / 非 0 / 空 /
  切り詰め / CLI 不在 / レビュアー timeout + kill 猶予が ExitPlanMode の hook timeout に収まる
- `post-implementation-review/tests`: フェンス付き clean で block せずレビュー済みに確定 /
  sentinel + 指摘は block (`TestFencedCleanSentinel`)、`TestIsCleanReview` に配線確認を追加、
  Stop の予算式に kill 猶予を反映

## 0.3.1 (2026-08-17, dac1957)

**入れ子 git repo の混入と bashsnap 孤児を修正**。0.3.0 を実環境に投入した直後の
state ファイルから発見。

1. **入れ子 git repo が pending に混入する** — `git status --porcelain -uall` は入れ子の
   git リポジトリを展開せず `dir/` のまま返す (`-uall` なら常に個別ファイルになるという
   前提が誤り)。中身は別リポジトリの変更なのでレビュー対象にしてはならない。実害は
   「空 diff → 破棄」で出ていなかったが、毎ターン pending を汚し無駄な git 呼び出しを
   生んでいた。`status_snapshot` で末尾 `/` のエントリを捨て (根本原因)、`_resolve_paths`
   でもディレクトリを弾く (0.3.0 が state に書いたエントリへの防御)
2. **bashsnap の孤児が 48 時間残る** — PreToolUse だけが走り Bash が実行されなかった場合
   (permission 拒否 / 別 hook の block / 中断) は PostToolUse が来ずスナップショットが
   孤児になる。`BASH_SNAPSHOT_TTL_SEC = 3600` を新設し GC でディレクトリ別に TTL を当てる

テスト 98 件 (回帰 5 件追加。入れ子 repo を実際に `git init` して `dir/` が返る前提自体も
assert)。

## 0.3.0 (2026-08-16, ae57f87 + 547ba0d)

**post-implementation-review をターンスコープ化**。Stop hook のレビュー対象を
「前回 Stop がレビュー対象として消費した時点以降に、このセッションが変更したファイル」に
限定した。従来は作業ツリー全体の `git diff HEAD` を投げていたため、同一ディレクトリで
2 セッションが動くと一行も編集していないセッションが隣の編集を 5〜10 分かけてレビューし、
cursor agent が同一プロジェクトで並走して失敗していた (実測 2026-08-14)。

1. `PostToolUse(Write/Edit/NotebookEdit/Bash)` で変更パスを session_id キーの `pending`
   に記録。`PreToolUse(Bash)` + `PostToolUse(Bash)` の `git status` スナップショット比較で
   `sed -i` 等の Bash 経由の変更も属性付きで拾う (`EXTERNAL_AI_POST_REVIEW_BASH_TRACKING`
   で無効化可)
2. `Stop` で `pending` を flock 下で `in-flight` へ原子的に予約 (drain-at-Stop)。割り込みで
   hook が kill されても TTL 超過分を後続 Stop が回収する
3. 復元は片側だけ: cursor 失敗 → pending に戻す / REVIEW_CLEAN・block → レビュー済みとして
   確定 (パス単位の HEAD 基準 diff hash を記録し、同じ差分を再レビューしない)
4. cwd キーの flock で cursor agent を直列化。`IN_FLIGHT_TTL_SEC = cursor.TIMEOUT_SEC + 300`
   (下回ると走行中の in-flight を横取りするため cursor 側 timeout から導出)
5. 状態ファイルの 48 時間 TTL GC を追加 (0.2.0 以前の `$TMPDIR/post-review-markers/` と
   `post-review-*.txt` の残骸も掃除)
6. 新しい無効化スイッチ `EXTERNAL_AI_POST_REVIEW=0`
7. **内部 git timeout を hook timeout 内に収める** (547ba0d, version 据え置き) —
   rev-parse 2s / status 5s / ls-files 10s / パス単位 diff 5s に再設定し、
   `_collect_diffs` に経過時間予算 `COLLECT_BUDGET_SEC=30` を追加 (超過分は deferred
   として pending に戻す)。hooks.json の timeout と内部 timeout の整合をテストで固定

### Deprecated

- **`EXTERNAL_AI_POST_REVIEW_MAX` を撤廃**。0.2.0 ではセッション単位のレビュー回数予算
  (`DEFAULT_MAX_REVIEWS = 2`) だったが、block 後の Stop は `stop_hook_active=true` で来る
  ため同一ターン内の再レビューは発生せず、事実上「ターンをまたいだ回数制限」としてしか
  効いておらず、長いセッションでは 3 ターン目以降レビューが黙って止まっていた。
  ターンスコープ化で不要になったため撤廃
- **互換挙動**: `EXTERNAL_AI_POST_REVIEW_MAX=0` を無効化スイッチとして使っていた環境の
  ため、`EXTERNAL_AI_POST_REVIEW` 未設定かつ `MAX=0` のときだけ無効化として解釈する。
  `0` 以外の値は無視される (回数制限は掛からない)。新しい正規のスイッチは
  `EXTERNAL_AI_POST_REVIEW=0`

テスト 89 件を新設 (受け入れ基準 11 項目、`post-implementation-review/tests/`)。

## 0.2.0 (2026-04-21, af82155 + d1443d5 + ce17aff)

**Cursor + Codex クロスレビュー + Stop hook 差分レビューを追加**。

1. `exitplan-review-codex` を `exitplan-review` にリネームし、Cursor (既存コードベース整合) +
   Codex (要件 / アーキ) の並列クロスレビューに再編 (`ThreadPoolExecutor`)
2. `post-implementation-review` を Stop hook として新設 (`git diff HEAD` + untracked を
   Cursor で差分レビュー)
3. プロンプトを `prompts/*.md` に外部化し、タイミング別の担当観点を明示
4. `EXTERNAL_AI_REVIEW_MAX` / `EXTERNAL_AI_POST_REVIEW_MAX` で無効化・回数制御
5. Codex セルフレビュー指摘への対応: `REVIEW_CLEAN` sentinel で「指摘なし」を検出し無用な
   block を回避 / `cursor agent --mode plan` で読み取り専用化 (`--trust` は workspace 信頼用) /
   外部 CLI の returncode != 0 を失敗扱い / fcntl.flock による原子的スロット取得 /
   plan・diff のハッシュを切り詰め前の全体で計算
6. **Codex PR レビュー指摘 R1-R6 対応** (d1443d5 / ce17aff, 同日・version 据え置き):
   - R1/R2: REVIEW_CLEAN や reviewer 失敗でクォータを消費しない (block 確定時のみ消費)
   - R3: 初回コミット前 repo では `git diff --cached` にフォールバック、worktree 外は素通り
   - R4: 51 番目以降の untracked 変更が重複判定に反映されない問題を修正
   - R5: `is_clean_review` を「非空行が 1 行のみで、その行が REVIEW_CLEAN」の厳密判定に
     (REVIEW_CLEAN + 後続指摘の混在で block がスキップされない)
   - R6: 並行実行で max を超えた block を許す問題を `reserve_slot` + `release_slot` で修正

後続 (version 据え置き): 個人環境パスの除去 (7e8ba2c, 2026-04-21)、実装者向け
`CLAUDE.md` の撤去 (767ade2, 2026-05-09。設計判断は README / hook 別 CLAUDE.md /
commit log に残す方針)。

## 0.1.0 (2026-04-12, 38e07e3)

初版。`explore-parallel` (Explore サブエージェント起動時に Cursor Agent を並走させ
`additionalContext` で注入) と `exitplan-review-codex` (ExitPlanMode 時に Codex で
プランレビューし最大 2 回 `decision: block`) の 2 hook。
