# external-ai-assist

Cursor / Codex などの外部 AI CLI を Claude Code に並走・クロスレビューに活用する hook 集。

## 同梱 hook

| Hook | 発火イベント | 役割 |
|---|---|---|
| `explore-parallel` | `PreToolUse(Agent)` + `PostToolUse(Agent)` | `Explore` サブエージェント起動時に Cursor Agent を並走させ、完了時に `additionalContext` として親 Claude に注入 |
| `exitplan-review` | `PreToolUse(ExitPlanMode)` | プラン承認前に **Cursor (既存コードベース整合) + Codex (要件・アーキ) を並列クロスレビュー** し、指摘を `decision: block` で Claude に差し戻す |
| `post-implementation-review` | `PreToolUse(Bash)` + `PostToolUse(Write/Edit/NotebookEdit/Bash)` + `Stop` | **そのターンにこのセッションが編集したファイルだけ**を Cursor でレビューし、影響範囲・リグレッションリスク等を `decision: block` で Claude に返す |

## インストール

```
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install external-ai-assist@mao-worktools
```

## 前提

- **Python 3.11+** (標準ライブラリのみ使用)
- `cursor` CLI: `explore-parallel` / `exitplan-review` / `post-implementation-review` の全てで使う。
  3 hook とも読み取り専用 (`cursor agent --mode plan`) で起動し、作業ツリーは書き換えさせない
  (read-only は cursor-agent の help 記述「`--mode plan` = read-only/planning (no edits)」に
  基づく。実機で書込が抑止されることは本 plugin 側では検証していない)
- `codex` CLI: `exitplan-review` の要件・アーキ観点担当 (`codex exec -s read-only --ephemeral`)

**どちらの CLI も未インストールでも Claude Code 本体の動作には影響しない** (fail-open)。
片方だけインストールされていれば、その片方の観点だけでレビューが成立する。

## 動作サマリ

### explore-parallel

`Explore` サブエージェントの視野を Cursor Agent で広げる。詳細は `hooks/explore-parallel/CLAUDE.md`。

Cursor Agent は読み取り専用 (`--mode plan`) で並走させる (0.4.1 から。それ以前は書込可能な
`-p` 単独で起動していた)。

`EXTERNAL_AI_EXPLORE_PARALLEL=0` で止められる (0.6.0)。止まるのは**起動側 (pre) だけ**で、
結果の回収 (post) は常に動く — 直前のターンで起動済みの Cursor Agent と一時ファイルを
孤児にしないため。

### exitplan-review (クロスレビュー)

`ExitPlanMode` 呼び出し時に Cursor と Codex を **並列実行** し、両者の出力を統合して `decision: block` で Claude に返す。

- **Cursor (primary)**: コードベース上の具体的な根拠を持つ観点
  - 既存コードベースとの整合性・影響範囲・依存の妥当性・見落とし箇所・テスト戦略
- **Codex (補完)**: 要件とアーキ方針の観点
  - 要件取り違え・スコープ過不足・アーキ上の危険信号・非機能要件・早期固定すべき前提

両者のプロンプトは `hooks/exitplan-review/prompts/planning-{cursor,codex}.md` に外部化されている。出力は 5 項目立ての箇条書きに固定され、ノイズが少ない。

- **`EXTERNAL_AI_REVIEW_MAX` は「指摘ありで返ってきた回数」の上限で、0.7.0 から
  プラン (hash) 単位**になった (既定 2 回、`0` で無効化)。指摘なし (`REVIEW_CLEAN`)
  とレビュアー失敗は枠を戻すのでカウントされない。0.6.0 までは単一のセッション
  累積カウンタだったため、無関係な別プランの block が積み重なると以後**すべての**
  プランがレビュー無しで通ってしまっていた (内部バックログ zh5.10)。上限に
  達したターンは「見送った」旨を `systemMessage` で通知する (黙って素通りしない)。
  **プラン単位化した結果、この上限はもはや「差し戻しの往復 (差し戻し → 修正 →
  再 ExitPlanMode) の総回数」を縛らない** — 修正のたびにプラン本文が変わり
  hash も変わるため、同じ内容を変えずに出し直した場合にだけ効く。往復回数
  そのものを縛る仕組みは現状無い (後述の「待ち時間とコストの見積り」を参照)
- プラン内容の SHA-256 ハッシュ (先頭 2000 文字の正規化版) で同一性判定。マーカーは
  `$TMPDIR/plan-review-markers/<session_id>.exitplan.marker` に JSON で
  `{hash: 該当プランの block 回数}` を持つ。同時に「未確定 (レビュー実行中に
  hook が kill された)」仮予約を `RESERVATION_TTL_SEC` (両レビュアーの timeout
  上限 + 300 秒) 経過後に回収し、記録エントリ数は 50 件を超えたら古い順に捨てる。
  マーカーファイルおよび `$TMPDIR/plan-review-*.txt` (0.6.0 以前の形式を含む) は
  mtime 48 時間で GC する
- レビュー結果は `$TMPDIR/plan-review-<session_id>.txt` にも保存
- 両方のレビュアーが失敗した場合は fail-open (exit 0)
- **完了時に所要時間と結果を `systemMessage` で表示** (0.6.0)。
  `[exitplan-review] クロスレビュー完了 (4分12秒): codex=clean, cursor=指摘あり → プランを差し戻し`。
  レビュー本文は通知に含めない (差し戻し本文は `reason`、参照コピーは上記ファイル)
- **`EXTERNAL_AI_PLAN_REVIEW_MODE=context` で非ブロック運用** (0.6.0、opt-in)。差し戻さずに
  レビュー所見を `additionalContext` で Claude の文脈へ渡す。「差し戻し → プラン修正 →
  再 ExitPlanMode で再度フルレビュー」の往復が消えるので、承認前の待ちが 1 ラウンド分になる。
  **プランの承認ゲート自体は残る** (hook は `permissionDecision` を返さないので、
  利用者がプランを見て承認する流れは変わらない)。この出力形は公式ドキュメントに直接の
  記述が無いため既定にはしていない
- レビュアーの選択 (`EXTERNAL_AI_PLAN_REVIEW_REVIEWERS`) と timeout
  (`EXTERNAL_AI_PLAN_REVIEW_TIMEOUT`) は環境変数で調整する (0.6.0)
- 「指摘なし」は `REVIEW_CLEAN` sentinel で判定する。コードフェンス・装飾行・「指摘なし」を
  述べる短い前置き 1 文は許容し、sentinel + 指摘本文は block する
  (判定規則は `hooks/_common/sentinel.py`。両 hook 共通)

### post-implementation-review (ターンスコープ差分レビュー)

Claude の作業が一段落した時点 (Stop) で Cursor に差分レビューを依頼し、影響範囲・リグレッション・不足テストを指摘させる。

**レビュー対象は「前回 Stop がレビュー対象として消費した時点以降に、このセッションが変更したファイル」だけ**。
作業ツリー全体の `git diff HEAD` は使わない。同一ディレクトリで複数セッションが動くと、一行も編集して
いないセッションが隣のセッションの編集を 5〜10 分かけてレビューしてしまうため。

変更パスの収集経路は 2 つ:

| 経路 | hook | 拾えるもの |
|---|---|---|
| 編集ツール | `PostToolUse(Write/Edit/NotebookEdit)` | `tool_input.file_path` (サブエージェント経由の編集も親セッションに帰属) |
| Bash | `PreToolUse(Bash)` + `PostToolUse(Bash)` | `sed -i` / フォーマッタ / スクリプト生成。実行前後の `git status` を突き合わせて検出 (Bash 1 回あたり約 70 ms) |

動作:

- **編集 0 件のターンは cursor を起動しない** (即 exit 0)
- `stop_hook_active` が true なら skip (再帰防止の公式パターン)
- 前回レビュー時と diff が 1 バイトも変わっていないパスは載せない
- 作業ツリー外の絶対パスは対象外
- 差分は**パスごとに HEAD 基準**で取得 (レビュアーにファイル全体の変更文脈を渡すため。
  判断根拠は `hooks/post-implementation-review/gitscan.py` の docstring)
- 同一作業ツリーで `cursor agent` が同時に 2 つ起動しないよう flock で直列化
- レビュー結果を配信できた時だけ消費を確定し、cursor 失敗時は次ターンに持ち越す
- **機密ファイル・非コードファイルは外部に送らない** (後述の除外規則。除外は恒久で、
  次ターンにも再掲しない)
- 1 ターンあたり 60 パスまで。超過分は捨てずに次ターンへ繰り越す
- 差分は **ファイル単位で** 合計 40 KB の予算に積む。収まらないファイルは送らずに次ターンへ
  繰り越す (レビュー済みにはしない。繰り越し分は次ターンの先頭に来る)。1 ファイルが 32 KB を
  超える場合だけ先頭を `(truncated)` 付きで送り、そのファイルはレビュー済みとして扱う
  (超えた分の hunk は、そのファイルの差分が変わるまでレビューされない)
- 除外・繰り越し・切り詰めが起きたターンは、対象ファイル名と理由を `systemMessage` と
  stderr に出す (ファイルの内容は出さない)
- **レビューを走らせたターンは所要時間と結果を `systemMessage` で表示** (0.6.0)。
  `[post-implementation-review] 差分レビュー完了 (3分41秒, 4 ファイル) → 指摘あり (Claude に対応を依頼しました)`。
  編集 0 件のターンは従来どおり無出力
- **頻度を落とす設定** (0.6.0): `EXTERNAL_AI_POST_REVIEW_MIN_LINES` (変更行数のしきい値) と
  `EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC` (前回レビューからの間隔)。どちらの見送りも
  pending を消費しないので、貯まった変更は次のレビューへまとめて載る
- レビュー結果は `$TMPDIR/post-implementation-review/reviews/<session_id>.txt` にも保存
- 状態ファイルは 48 時間で GC (旧 `$TMPDIR/post-review-markers/` の残骸も掃除する)

プロンプトは `hooks/post-implementation-review/prompts/post-implementation-cursor.md` に外部化され、出力は 5 項目 (直接影響 / 間接影響 / 未検証ケース / 追加テスト / マージ前確認) に固定。

#### 外部に送らないファイル (除外規則)

差分を Cursor に渡す前に、パスごとに次の順で判定し、どれかに当たれば**そのファイルの差分は
送らない** (判定は `hooks/post-implementation-review/exclusion.py`。他 plugin には依存しない):

0. **`EXTERNAL_AI_POST_REVIEW_EXCLUDE` の `!glob`** に当たるファイルは**必ず送る** (以下より優先。
   既定除外にコードが巻き込まれた時の逃げ道。例: `!credentials-service/*`)
1. **既定除外** — 名前からして機密なもの (`EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS=0` で無効化):
   - glob: `.env` `.env.*` `*.env` `.envrc` / `*.pem` `*.key` `*.p12` `*.pfx` `*.p8` `*.jks`
     `*.keystore` `*.ppk` `*.gpg` `*.pgp` `*.asc` `*.kdbx` / `id_rsa*` `id_dsa*` `id_ecdsa*`
     `id_ed25519*` / `*service-account*.json` `*service_account*.json` `kubeconfig*` `*.ovpn` /
     `.htpasswd` `*.htpasswd` `.netrc` `_netrc` `.npmrc` `.pypirc` `.pgpass` `.git-credentials` /
     `*.tfstate` `*.tfstate.*` `*.tfvars` `*.tfvars.*`
   - 語: パス (ディレクトリ名を含む) に `secret` / `secrets` / `credential` / `credentials` を
     **単語として** (前後が英数字以外) 含む。`client_secret.json` `config/secrets/db.yaml`
     `aws_credentials` は当たり、`secretary.py` `secretsanta.ts` は当たらない
2. **`EXTERNAL_AI_POST_REVIEW_EXCLUDE`** — カンマ区切りで追加する glob
   (例: `docs/, *.csv, notes/*.md`。`dir/` は `dir/*`、先頭の `./` `/` は無視。brace 展開
   `*.{py,js}` は非対応。`docs` とだけ書くと `docs` という名前のファイルにしか当たらない)
3. **`EXTERNAL_AI_POST_REVIEW_CODE_ONLY=1`** — コード以外を外す。対象は拡張子で判定:
   文書 (`.md .markdown .rst .txt .text .adoc .asciidoc .org .tex .rtf .pdf .doc .docx .odt .xls .xlsx .ods .ppt .pptx .odp`)、
   データ (`.csv .tsv .jsonl .ndjson .log .parquet .avro .sqlite .sqlite3 .db`)、
   メール等 (`.eml .msg .mbox .ics .vcf`)、画像・音声・動画、アーカイブ。
   JSON / YAML / TOML / XML / HTML / CSS と拡張子無し (Makefile 等) はコード扱いで送る

glob は **basename と作業ツリー相対パスの両方**に、**大文字小文字を区別せず**当てる。`*` は `/` にも
マッチするので `docs/*` は深い階層も拾う。symlink は編集時のパス (途中のディレクトリ名やリンク名を
そのまま残した lexical なパス)、実体 (realpath)、および作業ツリー内の symlink から作った別名
(`credentials/` → `ordinary/` なら `ordinary/data.json` に `credentials/data.json`) のどれかが
当たれば除外。別名は Bash 経由の変更 (`sed -i` 等。`git status` が実体名しか返さない) のために
必要で、tracked の symlink は全て、untracked の symlink は作業ツリー直下から 3 階層まで
(走査 5000 エントリ / 500 件で打ち切り) を見る。
git へのパス渡しは literal pathspec (`app/[id]/page.tsx` のような名前を glob として解釈させず、
claim していないファイルの差分が混入しない)。

除外されたファイルは pending にも reviewed にも残さない (恒久除外)。該当ターンの末尾に
`[post-implementation-review] 2 ファイルを外部 AI レビューから除外 (内容は送信していません): .env (既定除外: .env), docs/meeting-notes.txt (CODE_ONLY: .txt)`
のような通知が出る (`systemMessage`。同じ内容を stderr にも書く)。

```bash
# 議事録と CSV も送らない
EXTERNAL_AI_POST_REVIEW_EXCLUDE="docs/, *.csv" claude
# コードだけ送る
EXTERNAL_AI_POST_REVIEW_CODE_ONLY=1 claude
```

## 設計原則

1. **hook は絶対に失敗させない** — 全ての外周で例外を捕捉し exit 0
2. **fail-open** — CLI 未インストール / タイムアウト / 応答空 の各ケースで Claude Code の動作を止めない
3. **観点の分離** — タイミングごとに担当観点を変え、プロンプトを外部ファイルに分離して保守性を確保
4. **クロスレビュー時は並列実行** — Cursor と Codex を `ThreadPoolExecutor` で並行起動。片方だけ取れても block 成立
5. **共通化は同型が出揃ってから** — hook 間で同型だった処理 (sentinel 判定 / 外部 CLI 起動 /
   flock / ログ / 環境変数の解釈 / 通知の組み立て) は `hooks/_common/` に集約し、各 hook は
   `__main__.py` 冒頭で `hooks/` を `sys.path` に載せて参照する (plugin root 内の相対配置なので
   cache コピーでも壊れない)。hook 固有の状態機械は共通化しない
6. **timeout の上限はコード側に持つ** — hook timeout は `hooks.json` の静的値なので、環境変数で
   伸ばせる値には `MAX_TIMEOUT_SEC` の上限を設けて clamp する。超えるとハーネスの kill が
   先に来て、枠を戻す / pending を戻すといった後始末に到達しない (各 hook の tests が式で固定)
7. **待たせるなら結果を見せる** — exit 0 の hook の stderr は debug log にしか出ない。
   数分ブロックする処理は完了時に `systemMessage` で所要時間と結果を出す
   (書いてよい内容の線引きは `hooks/_common/notify.py`)
8. **外部 CLI は独自 process group で起動** — timeout 時は `os.killpg` でグループごと停止
   (SIGTERM → 猶予 → SIGKILL) し、同じグループに居る孫プロセス (stdout を継承した
   helper 等) を取り残さない。killpg は reap 前の子にだけ送る (pid 再利用の誤送信防止)。
   kill 猶予は hooks.json の hook timeout に織り込んである (各 tests が式で固定)
9. **外部 AI は読み取り専用で起動する** — cursor は `--mode plan`、codex は
   `exec -s read-only --ephemeral`。調査 (explore-parallel) もレビューも外部 AI に作業ツリーを
   書き換えさせない。cursor の起動 argv は `hooks/_common/cursorcli.readonly_argv` に一本化し、
   3 hook それぞれの偽 CLI テストが `--mode plan` を固定する

## 環境変数

命名規則は **`EXTERNAL_AI_<機能>` が on/off、`EXTERNAL_AI_<機能>_<設定>` がその機能の設定**。
`<機能>` は hook 名に対応する (`EXPLORE_PARALLEL` / `PLAN_REVIEW` / `POST_REVIEW`)。
解釈できない値は既定値に倒す (タイプミスで機能が黙って止まらない)。

### 機能の on/off (3 hook を独立に切れる)

| 変数 | 既定値 | 対象 |
|---|---|---|
| `EXTERNAL_AI_EXPLORE_PARALLEL` | `1` | `explore-parallel` (0.6.0 で新設) |
| `EXTERNAL_AI_PLAN_REVIEW` | `1` | `exitplan-review` (0.6.0 で新設) |
| `EXTERNAL_AI_POST_REVIEW` | `1` | `post-implementation-review` |

`0` / `false` / `off` / `no` が無効。全部止めるなら:

```bash
EXTERNAL_AI_EXPLORE_PARALLEL=0 EXTERNAL_AI_PLAN_REVIEW=0 EXTERNAL_AI_POST_REVIEW=0 claude
```

### exitplan-review

| 変数 | 既定値 | 意味 |
|---|---|---|
| `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` | `600` | 両レビュアー共通の timeout (秒)。上限 `1500` (hooks.json の hook timeout で決まる。超える指定は clamp) |
| `EXTERNAL_AI_PLAN_REVIEW_REVIEWERS` | 全件 | 走らせるレビュアーをカンマ区切りで選択 (例: `cursor`)。未知の名前だけを指定した場合は何も走らせない (既定へ fallback しない) |
| `EXTERNAL_AI_PLAN_REVIEW_MODE` | `block` | `context` にすると差し戻さず、レビュー所見を `additionalContext` で Claude に渡すだけにする |
| `EXTERNAL_AI_REVIEW_MAX` | `2` | **指摘ありで返ってきた回数**の、**プラン (hash) 単位**の上限 (`MODE=block` なら差し戻した回数、`context` なら所見を出した回数。0.7.0 でプラン単位化)。指摘なし・レビュアー失敗は枠を戻すので数えない。`0` は特別扱いで hook 自体を止める (レビューを 1 回も走らせない)。0.2.0 からの名前で、`EXTERNAL_AI_PLAN_REVIEW` と **AND** で効く。上限到達時は `systemMessage` で通知する |

**待ち時間の見積り**: 2 つを分けて考える必要がある。

| 何 | 上限 |
|---|---|
| **プラン 1 本あたりの待ち** | `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` (既定 10 分)。レビュアーは並列なので合計ではなく**最大側** |
| **同一プランを変えずに出し直す回数** | `EXTERNAL_AI_REVIEW_MAX` 回 (既定 2)。**プラン本文が 1 バイトも変わらない**再提出だけがこの上限に当たる |
| **セッション全体の総待ち時間 / 差し戻しの往復総数** | **上限なし** |

`EXTERNAL_AI_REVIEW_MAX` が 0.7.0 でプラン単位になったことの帰結として、**この変数は
もはや「差し戻し → 修正 → 再 ExitPlanMode」という往復そのものの総回数を縛らない**
(内部バックログ zh5.10)。修正のたびにプラン本文が変わり hash も変わるため、
新しいプランとして毎回フルの予算 (`EXTERNAL_AI_REVIEW_MAX` 回) が与えられる。
0.6.0 までの「セッション累積カウンタ」はこの往復を打ち止めにできていたが、
無関係な別プランの block まで同じ枠を消費してしまう副作用があったため、
0.7.0 はその副作用を潰すことを優先した。往復回数そのものを抑えたい場合は後述の
timeout / レビュアー選択 / hook の無効化で待ち時間を縮めること。

総量を抑えたいときに効くのは次の 3 つ:

- `EXTERNAL_AI_PLAN_REVIEW_TIMEOUT` — 1 本あたりの待ちを直接下げる (最も効く)
- `EXTERNAL_AI_PLAN_REVIEW_REVIEWERS` — レビュアーを 1 つに絞る (並列なので待ちは
  遅いほうに引きずられる。遅いレビュアーを外すと待ちが縮む)
- `EXTERNAL_AI_PLAN_REVIEW=0` — この hook を止める

`MODE=context` は差し戻しの往復を無くすので**その分**は縮むが、プランごとの
1 回分の待ちは変わらない。

### post-implementation-review

| 変数 | 既定値 | 意味 |
|---|---|---|
| `EXTERNAL_AI_POST_REVIEW_TIMEOUT` | `300` | cursor の timeout (秒)。上限 `600` (超える指定は clamp) |
| `EXTERNAL_AI_POST_REVIEW_MIN_LINES` | `0` (無効) | 送る diff の変更行数がこれ未満のターンはレビューを見送る (typo 修正で有料レビューを走らせない) |
| `EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC` | `0` (無効) | 前回レビュー完了から N 秒未満のターンはレビューを見送る (セッション単位) |
| `EXTERNAL_AI_POST_REVIEW_BASH_TRACKING` | `1` | Bash 経由の変更検出。`0` にすると Bash 前後の `git status` を打たなくなる (巨大 repo での軽量化用。`sed -i` 等の変更は拾えなくなる) |
| `EXTERNAL_AI_POST_REVIEW_EXCLUDE` | — | 外部に送らないファイルをカンマ区切り glob で追加 (例: `docs/, *.csv`)。既定除外に加算される。`!glob` は逆に「必ず送る」 (例: `!credentials-service/*`) |
| `EXTERNAL_AI_POST_REVIEW_EXCLUDE_DEFAULTS` | `1` | 既定除外 (`.env*` / `*.pem` / 語 `secret` `credential` 等) の有効/無効。`0` で無効化 (追加 glob と CODE_ONLY は残る) |
| `EXTERNAL_AI_POST_REVIEW_CODE_ONLY` | `0` | `1` でコード以外 (`.md` / `.txt` / `.csv` / `.pdf` / 画像等。一覧は上の除外規則) を外部に送らない。**「docs だけの変更でレビューを走らせたくない」用途はこれで足りる** |
| `EXTERNAL_AI_POST_REVIEW_MAX` | — | **v0.3.0 で撤廃** (レビュー回数の予算)。`0` を無効化スイッチとして使っていた環境のため、`0` のときだけ後方互換で無効化として解釈する (経緯は `CHANGELOG.md` の 0.3.0 Deprecated 節) |

**見送り (`MIN_LINES` / `COOLDOWN_SEC`) は pending を消費しない**。見送った変更は捨てられず、
次に走るレビューへまとめて載る。

### コストの目安

外部 AI CLI の課金は各サービス側で発生する (本 plugin は課金しない)。**呼び出し回数**は
次のとおりで、料金は利用中のプラン・モデルによる:

| hook | 起動タイミング | 1 セッションあたりの回数 |
|---|---|---|
| `explore-parallel` | `Explore` サブエージェント起動ごと | Explore の呼び出し回数と同じ |
| `exitplan-review` | `ExitPlanMode` ごと (プラン内容が変わったときだけ) | **上限なし** — プラン内容が変わるたび 1 回。`EXTERNAL_AI_REVIEW_MAX` が数えるのは「指摘ありで返った回数」だけなので、呼び出し回数の上限にはならない |
| `post-implementation-review` | **編集のあったターンの Stop ごと** | ターン数に比例 (上限なし) |

回数が伸びるのは 2 つ目と 3 つ目。長時間セッションや `/loop` では毎ターン走るので、
`EXTERNAL_AI_POST_REVIEW_MIN_LINES` / `_COOLDOWN_SEC` で頻度を落とすか、
`EXTERNAL_AI_POST_REVIEW=0` で切る。1 回あたりに送る diff は最大 40 KB
(`MAX_DIFF_BYTES`) に制限されている。

### プロジェクト単位で切る

環境変数はプロジェクトの `.claude/settings.json` の `env` に書ける (公式の `env` は
「Set environment variables for every session and for the subprocesses Claude Code
starts from it」で、hook コマンドはその subprocess に含まれる):

```json
{
  "env": {
    "EXTERNAL_AI_PLAN_REVIEW": "0",
    "EXTERNAL_AI_POST_REVIEW_COOLDOWN_SEC": "1800"
  }
}
```

**project / local settings の `env` が適用されるのは「workspace を trust した後」か
`-p` モードの起動時**である点に注意 (公式 docs 記載)。自分だけに効かせたいなら
`.claude/settings.local.json`、全プロジェクトに効かせたいなら
`~/.claude/settings.json` に書く。

## ファイル構成

```
external-ai-assist/
├── .claude-plugin/plugin.json
├── README.md                               ← このファイル
├── CHANGELOG.md                            ← 版ごとの変更履歴・設計判断
└── hooks/
    ├── hooks.json                          ← 6 hook を定義
    ├── _common/                            ← hook 間の共通ヘルパー (sys.path 経由で参照)
    │   ├── sentinel.py                     ← REVIEW_CLEAN 判定
    │   ├── subproc.py                      ← 外部 CLI 起動 (process group + timeout)
    │   ├── cursorcli.py                    ← cursor agent の存在確認 / 読み取り専用 (--mode plan) 起動 argv (3 hook 共通)
    │   ├── settings.py                     ← EXTERNAL_AI_* のパーサ (命名規則と解釈を 3 hook で統一)
    │   ├── notify.py                       ← systemMessage の組み立て (所要時間の書式 / 何を書いてよいか)
    │   ├── flock.py                        ← flock 付き read-modify-write
    │   ├── hooklog.py                      ← stderr ログ
    │   └── tests/
    ├── explore-parallel/
    │   ├── __main__.py
    │   ├── cursor.py                       ← 読み取り専用でバックグラウンド起動 + 待機
    │   ├── state.py
    │   ├── CLAUDE.md
    │   └── tests/                          ← 起動引数 (--mode plan) と注入の unittest
    ├── exitplan-review/
    │   ├── __main__.py                     ← 並列実行 + マーカー管理
    │   ├── cursor.py                       ← コードベース整合観点
    │   ├── codex.py                        ← 要件・アーキ観点
    │   ├── prompts/
    │   │   ├── planning-cursor.md
    │   │   └── planning-codex.md
    │   └── tests/                          ← block / 非 block 判定と偽 CLI の unittest
    └── post-implementation-review/
        ├── __main__.py                     ← 3 phase (pre-tool / post-tool / stop) の振り分け
        ├── state.py                        ← pending/in-flight 状態機械 + flock
        ├── stategc.py                      ← $TMPDIR の TTL GC
        ├── gitscan.py                      ← パス正規化 + status スナップショット + パス単位 diff
        ├── exclusion.py                    ← 外部に送らないファイルの判定 (既定 glob / 追加 glob / CODE_ONLY)
        ├── cursor.py                       ← 差分レビュー
        ├── prompts/
        │   └── post-implementation-cursor.md
        └── tests/                          ← 受け入れ基準の unittest スイート
```

テストは各 `tests/` の親ディレクトリで `python3 -m unittest discover tests` を回す
(cursor / codex は起動しない。モックか PATH 先頭の偽 CLI で検証する)。hook ごとに同名の
モジュール (`cursor` / `state`) を持つため、plugin root からの pytest 一括実行には対応しない。

## 拡張ポイント

### 新しいレビュアー (Gemini 等) を追加

`exitplan-review/` 以下に同形のモジュールを追加し、`__main__.py` の `REVIEWERS` に加える。

```python
# exitplan-review/gemini.py
NAME = "gemini"              # EXTERNAL_AI_PLAN_REVIEW_REVIEWERS で指定する名前
TIMEOUT_SEC = 600            # 既定
MAX_TIMEOUT_SEC = 1500       # env で伸ばせる上限 (hooks.json の hook timeout 内に収める)
def is_available() -> bool: ...
def timeout_sec() -> float: ...          # settings.duration(ENV_TIMEOUT, TIMEOUT_SEC, MAX_TIMEOUT_SEC)
def review(plan_text: str) -> str | None: ...
```

```python
# exitplan-review/__main__.py
import gemini
REVIEWERS = [cursor, codex, gemini]
_HEADERS["gemini"] = "## Gemini レビュー (xxx 観点)"
```

プロンプトは `prompts/planning-gemini.md` として追加。

### 他のタイミングへの拡張

`mid-implementation-review` (実装途中) や `pr-review` (PR 作成前) を追加する場合は、
同じ hook 構造 (`__main__.py` + `cursor.py` + `prompts/*.md`) を踏襲して新ディレクトリを作り、
`hooks.json` に `PreToolUse(Bash)` matcher + `gh` コマンド検出などを組み合わせる。

## ライセンス

MIT
