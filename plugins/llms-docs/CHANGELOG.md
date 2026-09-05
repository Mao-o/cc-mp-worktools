# Changelog

All notable changes to this plugin will be documented here.

## [0.23.0] - 2026-09-05

### 検索結果の `Section:` 行に `URL#anchor` を付与 (claude-docs / firebase)

2026-08 精査で指摘された設計原則 (検索結果は「ソース URL + anchor」を含めるべき) の
未充足を解消。`search` / `search-content` の `Section:` 行は heading_path のみで
anchor が無く、ページ URL もエントリ単位にしか出ないため、引用元を「URL + セクション」
で答える側が anchor を自作して誤る余地があった。

- `_common.py` に `heading_anchor_slug()` (見出しタイトルから GitHub/Mintlify 互換の
  best-effort slug を生成: 小文字化・空白→ハイフン・記号除去) と `section_url_anchor()`
  (URL + heading_path の末尾セグメントから `  [<url>#<slug>]` を組み立て) を追加。
  claude-docs / firebase の `search` / `search-content` 計 4 箇所の `Section:` 行に適用。
  `→` ではなく角括弧にしたのは、同じ結果行の直下でスニペットの1行ヒットマーカーとして
  既に `→` を使っており、1 文字に 2 つの意味を持たせないため (`[partial match]` /
  `[before first heading]` の既存アノテーション規約に合わせた)
- anchor は heading_path の**末尾セグメントのみ**から生成する (実際にページに描画される
  見出しはそれ単体であり、内部の breadcrumb 表示である heading_path 全体ではないため)。
  同名見出しがページ内に複数ある場合の GitHub/Mintlify 側の `-1`/`-2` 連番までは
  再現しない best-effort であることを 3 SKILL.md に明記
- **firebase は index の生 URL (`.../query-limit.md.txt`、`URL:` 行に表示される raw
  markdown fetch URL) ではなく、`_entry_url_for_match()` (既存の page_ref 一致判定
  ヘルパーを流用) で正規化した人間向けページ URL (`.../query-limit`) を anchor の
  base にする**。プレーンテキスト応答に `#fragment` を付けても解決しないため、
  誤った URL を組み立てて配布しないよう修正前に実機確認した (自己レビューで発見)
- **ai-sdk は対象外**: llms-full.txt に URL 自体が無く anchor を組み立てられないため
  (`researching-ai-sdk` SKILL.md に明記)。解析層 (`split_documents` / URL 抽出) には
  一切手を入れていない — 既存の `doc["source_url"]` / `entry["url"]` を出力整形で
  使っているだけ

回帰テスト 16 件追加 (`test_common.py`: `heading_anchor_slug`/`section_url_anchor` の
fixture テスト 12 件、`test_parse_claude_docs.py`/`test_parse_firebase.py`: `search`/
`search-content` の CLI 統合テスト計 4 件)。

### テストを 1 件だけ指定して実行できない問題を修正 (共有ヘルパーが解決されない)

`scripts/tests/` 配下の共有ヘルパー `_loader` をトップレベル import (副作用専用) で
読み込んでいたため、`python3 -m unittest discover scripts/tests` (ディレクトリ探索)
は通るが、`python3 -m unittest scripts.tests.test_x.Class.method` や
`pytest scripts/tests/test_x.py::Class::method` (モジュールパス指定) は
`ModuleNotFoundError: No module named '_loader'` になっていた (内部バックログで
6 plugin 横断の共通症状として追跡)。

`scripts/tests/__init__.py` (既存は空) に `scripts/tests/` を sys.path へ追加する
初期化コードを追加。呼び出し側 (CI / README の実行手順) は一切変更していない。
README にクラス単位・メソッド単位で 1 件だけ指定する実行例を追記。

回帰テスト 3 件追加 (`test_single_test_invocation.py`: unittest 単体クラス/メソッド指定
+ pytest node id 指定をサブプロセスで実際に実行し確認)。

### plugin manifest の description を日本語に統一

6 plugin 中 llms-docs だけ英語のままだった `plugin.json` の `description` を、
`marketplace.json` 側の既存エントリと同じ日本語表記に統一した。

### 検証

`claude plugin validate plugins/llms-docs` warning 0。224 tests, all green
(前回 205 + 上記 19)。
- `search` サブコマンドの Section 行 anchor は、index の raw URL (`.md` 付き fetch 形) ではなく
  `Source:` 行と同じ正規形 (`normalize_doc_url`) に付ける。raw 形に `#fragment` を付けても
  ページ上で解決しないため (マージ前レビューの指摘)。表示用の `URL:` 行は従来どおり

## [0.22.1] - 2026-08-28

### 新規追加した --file が follow-up hint から脱落し別ドキュメントを指す事故を修正 (0.22.0 の追いコミット)

Codex R1指摘2件 (P1)。0.22.0 で claude-docs の `search` と ai-sdk の `fetch-index` に
`--file` を新規追加したが、実行後に表示される `Next: ...` の follow-up ヒントが `--file` を
含めておらず、そのヒントをそのまま実行すると **デフォルトのキャッシュ済みcorpusを再ロードし、
同じ番号 (page_ref/doc_idx) が別ドキュメントを指してしまう** (`corpus_hint_args` はまさに
この事故を防ぐために既に存在するヘルパーだが、新設した2箇所で呼び忘れていた)。

- claude-docs `search` (Codex指摘) / ai-sdk `fetch-index` (Codex指摘) を修正
- 修正のついでに同一パターンを全 `next_hint()` 呼び出しに対して監査した結果、
  **既存の (今回新規追加ではない) `--file` 対応コマンドにも同じ配線漏れが計6箇所** 見つかった:
  claude-docs の `search-content` / `sections`、ai-sdk の `sections` / `search-index` /
  `search-content` / `search`。これらは今回のPRの新規機能ではなく以前からのバグだが、
  同一ファイル内で同一パターンの指摘2件を直すのに隣の同型バグを放置するのは不自然なため
  合わせて修正した

回帰テスト4件追加 (`--file` 指定時に follow-up hint が `--file` を含むことを確認)。
既存の golden output テスト1件 (`test_sections_full_output_is_pinned`) は非デフォルト
`--cache-dir` を使っており、修正後は正しく `--cache-dir` がヒントに含まれるため期待値を更新。
205 tests, all green。

## [0.22.0] - 2026-08-28

### 3 script間のフラグ名・placeholder不統一を解消 (ランキング順の統一は別途)

2026-08 精査で指摘された「3 scriptで引数・placeholderが統一されていない」不整合を、
挙動を変えない範囲 (追加的な変更のみ) で解消。ランキング順の統一 (index_score優先 vs
body-hits優先 + changelog除外の扱い) はユーザー可視の出力順が変わる別種の変更のため、
今回は対象外とし別PRで扱う。

- **`--top-n` に統一**: claude-docs の `search` だけ `--index-limit` という別名だった。
  `--top-n` を正式名にし、`--index-limit` は非表示 (`--help` に出ない) だが引き続き動作する
  hidden alias として残した (既存の呼び出しを壊さないため)。`skills/researching-claude-docs/SKILL.md`
  の記載も追従
- **`--max-snippet-chars` を `_common.add_max_snippet_chars_arg` に集約**: ai-sdk・firebase の
  `search-content` にこのフラグが存在せず、スニペットが無制限に出力されていた
  (`search` にはあった)。共通ヘルパーを新設し、claude-docs/ai-sdk/firebase の `search` /
  `search-content` 計6箇所全てで使うよう統一。ai-sdk・firebase の `cmd_search_content` は
  `search_content_in_body` 呼び出しに `max_snippet_chars` を渡していなかった配線漏れも解消
- **`--file` を欠けていた箇所に追加**: ai-sdk の `fetch-index` (`_load_docs` は元々 `file_arg`
  を汎用サポートしていたが、`fetch-index` だけ `None` 固定で呼んでいた) と claude-docs の
  `search` (Phase 2 の llms-full.txt 読み込みを `_load_full_txt` 経由に変更し `--file` を反映)。
  claude-docs の `search` は `--source both` と `--file` の組み合わせ (どのファイルが「両方」に
  対応するか一意に決まらない) を明示的にエラーにする
- **placeholder を `<page_ref>` に統一 — チケット原案の `<doc_idx>` から変更**: 実装を確認した
  結果、`page_ref` は3 script共通で「整数 index / URL slug / 完全URL」を受け付ける実際の
  argparse引数名であり、`<doc_idx>` (search結果の `[N]` に表示される値を指す語) をヒント文言に
  使うと非整数の入力形態を誤って排除して見える。ai-sdk・firebaseの `next_hint()` は元々
  `<page_ref>` で統一済みだったため、claude-docs側の4箇所 (`<doc_index>` という3つ目の綴り) を
  それに合わせた。`<doc_idx>` は「search結果の `[N]` の値」を指す語として文書内では引き続き使う
- 不要になっていた未使用ヘルパー `_common.add_doc_index_arg` (呼び出し箇所0件) を削除

回帰テスト11件追加 (claude-docs: `--top-n`/`--index-limit` alias 4件 + `--file` 2件、
ai-sdk: `fetch-index --file` 1件 + `--max-snippet-chars` 2件、firebase:
`--max-snippet-chars` 2件)。203 tests, all green。`claude plugin validate` warning 0。

## [0.21.7] - 2026-08-28

### リダイレクト経由の304誤信頼を構造的に根絶: conditionalヘッダーをredirect時に剥がす (0.21.6 の追いコミット)

Codex R6指摘1件 (P2)。0.21.6の修正 (`resp.url != url` チェック) は200成功パスのみをカバーしており、
既存のsidecarが**リダイレクトが始まる前の正当なvalidator**を持っているケースでは、そのvalidatorが
リダイレクト先に転送されて偶然304を引き当てる可能性を防げていなかった。304のレスポンスは
`e.url` (最終到達URL) を持つことを実機確認したが、advisor相談のうえ「304分岐にもチェックを
追加する」対症療法ではなく、**根本原因 (conditionalヘッダーがredirectで転送されること自体)** を
断つ方向に修正した。

- `urllib.request.HTTPRedirectHandler` を継承した `_NoValidatorRedirectHandler` を追加し、
  `redirect_request()` でリダイレクト後のリクエストから `If-None-Match`/`If-Modified-Since` を
  剥がすようにした。`urllib.request.install_opener()` でプロセス全体のデフォルトopenerとして
  組み込むため、`fetch_url`側は従来どおり `urllib.request.urlopen()` を呼ぶだけで自動的に適用される
  (各`parse-*.py`は単機能プロセスとして実行されるため、プロセス全体への副作用も安全)
- これによりconditionalヘッダーがリダイレクト先に到達すること自体が無くなり、304分岐での
  誤信頼は構造的に発生し得なくなった。0.21.6で追加した `resp.url != url` チェック (200パス側) は
  多重防御として維持: 仮に将来このredirect handlerが外れても、リダイレクト越しに得たvalidatorを
  保存しない側で二重に守る

回帰テスト2件追加。実際の(モックしていない) `HTTPRedirectHandler.redirect_request()` 呼び出しで
ヘッダーが剥がれることと、`_NoValidatorRedirectHandler` がプロセスのデフォルトopenerに実際に
組み込まれていることを検証 (`test_fetch_and_cache.py`、192 tests, all green)。

## [0.21.6] - 2026-08-28

### リダイレクトを跨いだ場合にetag/last_modifiedを保存しないよう修正 (0.21.5 の追いコミット)

Codex R5指摘1件 (P2)。これまでの4ラウンドとは異なるカテゴリの指摘 (sidecarの検証ではなく
リダイレクト時のHTTPセマンティクスの正しさ)。

- **設定URLがリダイレクトする場合、`urllib.request.HTTPRedirectHandler` がconditionalヘッダー
  (`If-None-Match`/`If-Modified-Since`) を含む元リクエストのヘッダーをリダイレクト先にそのまま
  転送することを実機確認**: リダイレクト先が後で変わった場合、旧リダイレクト先向けのvalidatorが
  新しいリダイレクト先に送られてしまい、新リダイレクト先の`Last-Modified`がたまたま条件を満たすと
  304が返り、実際には別サーバーの別コンテンツであるにもかかわらず旧bodyを`--max-age`ごとに
  保持し続けてしまう。`resp.url` (urllibがリダイレクト追跡後の最終URLをセットする) と要求元の
  `url` を比較し、一致しない (=リダイレクトが発生した) fetchでは`etag`/`last_modified`を
  sidecarに保存しないよう修正。`content_hash`はサーバーに送信されないため引き続き保存する

回帰テスト1件追加。`If-None-Match`が実際に別ホストへのリダイレクトを跨いで転送されることを
`httpbin.org`への実リクエストで確認したうえで実装 (`test_fetch_and_cache.py`、190 tests,
all green)。

## [0.21.5] - 2026-08-28

### sidecarのstring validatorがHTTPヘッダーとして不正な場合の生tracebackを構造的に修正 (0.21.4 の追いコミット)

Codex R4指摘1件 (P2)。0.21.2〜0.21.4は「sidecarの値が期待した型/構造か」を`_load_fetch_meta`側で
個別にguardする対応を3ラウンド続けたが、今回の指摘は「型はstringだが中身がHTTPヘッダーとして不正
(改行を含む＝ヘッダーインジェクション、Latin-1範囲外の文字を含む)」というケースで、`_load_fetch_meta`
の型チェックでは列挙しきれない。advisorとの相談を経て、個別guardを積み増す方針ではなく、実際に
送信を試みる`urllib.request.urlopen`呼び出し自体を`ValueError`込みで捕捉する構造的な修正に切り替えた
(`UnicodeEncodeError`は`ValueError`のサブクラスであることを実機確認済み)。

- **既存の例外捕捉タプルに `ValueError` を追加**。try節内で唯一 `ValueError` を送出しうる箇所
  (`int(content_length)` の変換) は既に内側の try/except で個別に捕捉・無害化されており、
  この追加が意図しない挙動を隠す経路にならないことを確認済み
- `_load_fetch_meta` の型guard (0.21.2〜0.21.4) は**早期リジェクトの最適化**として維持: 不正な
  sidecarをネットワーク往復の前に弾く。今回追加した `ValueError` 捕捉は**その guard が列挙し
  きれない残り全部に対するbackstop** — 将来また新しいHTTPヘッダー制約が見つかっても、個別対応
  なしでこの1箇所が拾う

回帰テスト2件追加 (`test_fetch_and_cache.py`)。実際の (接続はしない) `http.client.putheader()`
を経由させて本物の `ValueError`/`UnicodeEncodeError` を発生させる方式で検証しており、将来の
Python側の仕様変更にも追従する。189 tests, all green。

## [0.21.4] - 2026-08-28

### `_load_fetch_meta` の型チェックに `content_hash` を追加し3フィールドを一貫させる (0.21.3 の追いコミット)

R3修正後の自主監査 (advisor起点、Codexの新規指摘ではない)。`etag`/`last_modified` のみ
非string値を弾いていたが `content_hash` は対象外だった。非string content_hashは
現在のファイルhash (常にstring) と一致し得ないため実害はないが、「このsidecarの値は
全てstring」という契約を`_load_fetch_meta`が一貫して保証するよう `content_hash` も
チェック対象に追加。あわせて以下2点を実機確認し、追加修正が不要と判断:

- `gzip.BadGzipFile` (非gzipボディにgzipヘッダーが付いていた場合の例外) は `OSError`
  を継承しており、既存の例外捕捉タプルで既にカバー済み
- `email.message.Message.get()` は重複ヘッダーがあっても常に先頭の値を単一のstringで
  返す (`get_all()` と異なりリストにはならない) ため、`resp.headers.get("ETag")` /
  `get("Last-Modified")` が非string値を返すケースは無い

回帰テスト1件追加 (`test_fetch_and_cache.py`、187 tests, all green)。

## [0.21.3] - 2026-08-28

### sidecarのetag/last_modifiedが非string値だとTypeErrorで生tracebackになるバグを修正 (0.21.2 の追いコミット)

Codex R3指摘1件 (P2)。

- **手編集・破損・将来のフォーマット変更などで sidecar の `etag`/`last_modified` が truthyな非string値
  (list/dict等) になっていた場合、content_hashが一致してさえいれば、その値がそのままリクエスト
  ヘッダーに渡っていた**: `int` は `http.client.putheader` が黙って `str()` 変換するため実害はないが、
  list/dict等は `putheader` 内部の `bytes.join()` が `TypeError` を投げ、documented なstale-cache
  フォールバックを迂回して生tracebackになることを実機確認。`_load_fetch_meta` に型チェックを追加し、
  `etag`/`last_modified` が存在するのにstringでない場合はsidecar全体を `{}` (無効) 扱いにするよう修正

回帰テスト1件追加 (`test_fetch_and_cache.py`、186 tests, all green)。

## [0.21.2] - 2026-08-28

### 304応答後のmtime更新失敗が生tracebackになるバグを修正 (0.21.1 の追いコミット)

Codex R2指摘1件 (P2)。

- **304ハンドラ内の `os.utime()` が投げる `OSError` が未捕捉だった**: 読み取り専用ファイルシステム・
  キャッシュファイルの並行削除・所有者変更などで `os.utime()` が失敗すると、この呼び出しは
  `except HTTPError` ブロックの内側で実行されるため、兄弟の `except (..., OSError, ...)` では
  捕捉できない (兄弟except節はtryブロックのみを対象とし、他のexcept節内の例外は対象外)。
  条件付きGET自体は成功しているにもかかわらず、生tracebackで終了していた。
  `os.utime()` を個別のtry/exceptで囲み、失敗時は他の失敗経路と同じ `_handle_fetch_failure`
  に流してstale cacheへフォールバックするよう修正

回帰テスト1件追加 (`test_fetch_and_cache.py`、185 tests, all green)。

## [0.21.1] - 2026-08-28

### 破損gzipストリームの未捕捉例外 + body/sidecarの非atomicなペア書き込みによる整合性崩れを修正 (0.21.0 の追いコミット)

Codex R1指摘2件 (P2)。

- **`gzip.decompress` が投げる `EOFError`/`zlib.error` が既存の例外捕捉範囲外だった**:
  破損・切り詰められたgzipストリームは `Content-Length` 不一致では検知できないケースがあり、
  これらの例外は既存の `(URLError, OSError, HTTPException)` に含まれず生tracebackとして
  露出していた。例外捕捉タプルに `EOFError, zlib.error` を追加
- **body書き込みとsidecar書き込みが別々のatomic writeで、ペアとしては非atomicだった**:
  同一の期限切れキャッシュを2プロセスが並行して再取得し、かつ2つのレスポンスの間で上流の内容が
  変わっていた場合、それぞれ独立にatomicな書き込みが競合して「片方のbody + もう片方のETag」の
  ような組み合わせで保存され得る。次回の条件付きGETがこの不整合なETagで304を受け取ると、実際の
  bodyとは対応しない304を「有効」と誤認し、誤ったbodyをさらに1 `--max-age` 分保持し続けてしまう。
  `<cache>.meta.json` に書き込み時点のbodyの `content_hash` (sha256) を追加保存し、条件付き
  ヘッダーを送る前にキャッシュファイルの現在のbytesのhashと突き合わせるよう修正。不一致なら
  条件ヘッダーを送らず無条件GETにフォールバックし、body・sidecarを揃って再書き込みして不整合を解消する。
  この突き合わせは `--max-age` 超過ごとにキャッシュファイル全体を読んでhash化するコストを追加するが、
  実測では24MB (platformページ相当の上限規模) のsha256計算が約7ms (`hashlib.sha256`、Python 3標準)
  であり、ネットワークI/Oが支配的な処理全体において無視できる

回帰テスト5件追加・既存3件をcontent_hash込みのfixtureに更新、既存1件にsidecar自己修復の検証を追加
(`test_fetch_and_cache.py`、184 tests, all green)。

## [0.21.0] - 2026-08-28

### HTTP取得にgzip圧縮転送 + 条件付きGET (ETag/Last-Modified) を追加し、無条件の全量再取得を削減

`fetch_url` は `Accept-Encoding` を送らず (非圧縮転送)、ETag/Last-Modifiedも保存しないため、
`--max-age` 超過時は内容が同じでも常に全量GETしていた。platformページ (text, ~23.8MB) はgzipで
1/4〜1/5に縮小できる見込みで、再取得の遅さはfork subagentの応答遅延=離脱要因になっていた。

- `Accept-Encoding: gzip` を常時送信。`Content-Encoding: gzip` の応答は `gzip.decompress` (stdlib)
  で展開してからキャッシュに書き込む (`urllib`は自動展開しないため)。`Content-Length` 検証は
  展開前の圧縮バイト数に対して行う (このヘッダーは転送サイズ=圧縮後サイズを表すため)
- `<cache>.meta.json` サイドカーに `ETag`/`Last-Modified` を保存。`--max-age` 超過時の再取得は
  `If-None-Match`/`If-Modified-Since` 付きの条件付きGETを送り、`304 Not Modified` (urllibは
  `HTTPError(code=304)` として送出、通常のレスポンスとしては返らない) ならボディ再取得・
  キャッシュ書き込みをせず `os.utime` でmtimeだけ更新 (`--max-age` の時計をリセット)。
  サイドカーが無い既存キャッシュ (この機能追加以前に書かれたもの) は条件ヘッダー無しの
  無条件GETにフォールバックする。304以外のHTTPエラー (404/500等) は既存のstale-serve/exit/raise
  と同じ扱い
- サイドカーは応答が両ヘッダーとも送らなかった場合も (空で) 上書きする。前回分のETag等が
  古いまま残り、サーバーが対応をやめた後も送り続けてしまう事故を防ぐため

回帰テスト12件追加 (`test_fetch_and_cache.py`、181 tests, all green)。

## [0.20.1] - 2026-08-28

### `search-content` の全ページスキャンが全fetch完了を待ってから結果を出す barrier になっていた問題を修正 (0.20.0 の追いコミット)

Codex R1 指摘 (P2)。0.20.0 で `search-content`/`search` 共通で `_fetch_pages_concurrently`
(ThreadPoolExecutor) を使うようにしたが、`--page-ref` 省略時 (index 全体、最大 ~7000件) は
全ページの fetch が完了するまで `cmd_search_content` が何も出力しないbarrierになっていた。
8並列・per-page timeout 30sの構成では、dead linkが33件混ざるだけで最低5波 (約150秒) かかり、
本来の修正目的だった「Bash toolの既定120秒timeoutでkillされる」を形を変えて再現してしまう。

`search-content` は逐次fetch (`raise_on_error=True` + 1件ずつskip) に戻し、元の実装同様
結果をfetchの進行に合わせてstreamingで出力するよう修正。並列化は境界が明確な `search`
(`--top-n`, 既定5) 側のみ維持する。

169 tests, all green (テスト内容の変更なし、既存のskipテストが引き続き通ることを確認)。

## [0.20.0] - 2026-08-28

### firebase の `search`/`search-content`: 候補ページ1件のHTTP失敗で全体がexit 1する問題を修正

`parse-firebase.py` の `search`/`search-content` は候補ページを `_fetch_page` で逐次取得しており、
`fetch_url` は取得失敗時 (キャッシュ無し) に即 `sys.exit(1)` していた。~7000件のindexにdead linkが
1件混ざるだけで、cache済みの他ページのhitsも出さずにコマンド全体が失敗していた。逐次取得のため
`timeout 120s × 候補数` が最悪ケースとなり、Bash toolの既定120sタイムアウトを超えてkillされる
こともあった。

- `_common.fetch_url()` に `raise_on_error: bool = False` を追加。`True` のとき、キャッシュが無く
  取得も失敗した場合は `sys.exit(1)` の代わりに新設の `FetchError` (url / cause を保持) を送出する。
  デフォルト (`False`) は既存の挙動を維持 (claude-docs/ai-sdkの単一index/full-text取得はこれまで通り
  fail-fastが適切なため変更しない)
- `parse-firebase.py` に `_fetch_pages_concurrently()` を追加。`concurrent.futures.ThreadPoolExecutor`
  (stdlib, 最大8並列) で候補ページを並列取得し、失敗したページは `(skip: fetch failed <url>: <error>)`
  をstderrに出して読み飛ばし、残りの候補で検索を継続する。per-page timeoutは120s→30sに短縮
  (逐次実行が前提だった120sは並列化後は長すぎる)。出力順は取得完了順に依存させず、元のindex順を
  維持したまま結果を表示する (golden出力テストとの互換性のため)
- `search`/`search-content` の末尾に `(N pages skipped — fetch failed, see stderr)` を追加
  (N > 0のときのみ表示)
- 3 SKILL.md のうち researching-firebase の失敗時対処表に、単一ページ取得 (`content`/`sections`) と
  複数ページ横断 (`search`/`search-content`) の失敗時挙動が異なる点を分けて追記。metadata version を
  patch bump: researching-firebase 2.1.5 → 2.1.6

回帰テスト4件追加 (`test_fetch_and_cache.py` 2件: `raise_on_error=True` でのFetchError送出 /
stale-serveがraise_on_errorの影響を受けないことの確認、`test_parse_firebase.py` 2件:
`search`/`search-content` それぞれでdead pageをskipしつつ生存ページのhitsを報告することの確認。
169 tests, all green)。

## [0.19.0] - 2026-08-27

### SessionStart hook: `resume` での再発火・スキル参照の分かりにくさを修正

`hooks/hooks.json` の `SessionStart` に `matcher` が無く、`resume`
(セッション再開) や `fork` でも毎回メッセージが出ていた。実際に
促したいのは「新しい会話状況になった」タイミング (`startup`/`clear`/`compact`)
のみで、`resume` は直前セッションの続きなので不要、`fork` も親から
状況が引き継がれるため同様。`matcher: "startup|clear|compact"` を追加して
両方を除外した。

あわせて printf メッセージが「researching-claude-docs」等のスキル名だけを
挙げており `Skill` ツールで起動する対象だと分かりにくかったため、
`Skill: llms-docs:researching-claude-docs` の形式に統一し、文末を
「Skill ツールで起動」に変更した。

### README: `search-content` の soft-AND フォールバックが未記載だった問題を修正

`search-content` の説明が「AND 検索、OR ではない」とだけ書かれており、
完全 AND が 0 件のとき自動でキーワード過半数一致にフォールバックする
既存の soft-AND 挙動 (`[partial match]` 表示) が記載から漏れていた。
フォールバック条件と `[partial match]` 表示を追記。

### SKILL.md: 禁止事項の明文化 + trigger word の誤爆防止 + 陳腐化した記述の修正

3 SKILL とも `allowed-tools` に Read/Bash があるため、段階的絞り込み
(`search`/`search-content`/`content` 等) を迂回してキャッシュファイルに
直接 `grep`/`cat`/行番号 Read することが技術的には可能だった。迂回は
全文読み込み相当になり進捗開示の設計意図に反するため、「禁止事項」表を
3 SKILL 共通で新設し、迂回パターンと正しい代替コマンドを明記した。

- **researching-claude-docs**: trigger word の `"effort"` / `"arguments"` /
  `"paths"` は汎用語すぎて無関係な会話でも誤発火しうるため、
  `"skill frontmatter effort"` / `"slash command arguments"` /
  `"skill frontmatter paths"` に具体化。あわせて `--source both` の説明
  「並列検索」が実装 (逐次実行) と食い違っていたため「順に検索」に修正
- **researching-ai-sdk**: trigger word の `"tool"` / `"tools"` /
  `"embed"` / `"embedMany"` / `"provider"` も同様に汎用語すぎるため
  `"AI SDK tool()"` / `"tool calling in AI SDK"` / `"embed()/embedMany()"` /
  `"@ai-sdk provider"` に具体化。`page_ref` の説明が「3 形式」と書かれて
  いたが ai-sdk の llms-full.txt は URL を持たないため実際は整数
  index とタイトル部分一致の「2 形式」のみだった (URL/slug を受け付けるのは
  claude-docs/firebase側) — 記載を修正。`--file` の説明が「すべての
  サブコマンドで」となっていたが `fetch-index` に `--file` 引数は無い
  (常に `--cache-dir` 側を使う) ため、対象範囲を明記
- **researching-ai-sdk/references/llms-txt-structure.md**: doc_index の
  数値範囲表 (`0-5` 等) が 0.14.0 以前の llms-full.txt 構造に基づくもので
  既に実態と乖離していた。今後も上流更新のたびに陳腐化するため、
  数値範囲を削除しカテゴリ名のみの一覧に置き換え、最新の割り当ては
  `fetch-index --compact` / `search` で確認する旨を追記した

metadata version を patch bump:
researching-claude-docs 3.4.4 → 3.4.5 / researching-ai-sdk 3.3.5 → 3.3.6 /
researching-firebase 2.1.4 → 2.1.5

## [0.18.4] - 2026-08-28

### silent-e複数形 (Responses/Releases/Databases/Caches) の誤stem化を既知の限界として明文化 (未修正)

Codex R4 で `_norm()` の sibilant-suffix 規則 (`ses`/`xes`/`zes`/`ches`/`shes`
終わりの語から2文字strip) が silent-e語根の複数形も誤って2文字stripしてしまう
指摘を受けた (`Responses`→`respons`, `Caches`→`cach` 等、単数キーワードは
末尾`e`を保持するため一致しなくなる)。別branch (`_common.py` の並行コピー)
と同一の指摘のため、同じ判断をこちらにも適用する。

調査の結果、これは文字パターンだけでは解決不可能な曖昧性と判断した:
`caches`(cache+s, silent-e語根) と `matches`(match+es, 硬子音語根) は
末尾が完全に同形で、辞書 (語根リスト) か本物のstemmerなしに文字列だけからは
判別できない。既存テストはこの硬子音側の挙動を一切pinしていなかったため
安全側 (どちらの挙動も壊さない) を優先し、現状挙動をcharacterization test
として明文化するに留めた。修正は内部バックログで追跡する。

回帰テスト1件追加 (`test_common.py`、163 tests, all green)。

## [0.18.3] - 2026-08-28

### レビュー指摘4件を修正 + 別branchで先に直した2件をこの `_common.py` にも適用 (0.18.2 の追いコミット)

- **絞り込みヒントに含める `--file`/`--cache-dir` がshell quoteされていなかった**:
  値をコピー&ペースト実行可能なシェルコマンド行にそのまま埋め込んでいたため、
  空白やシェル特殊文字を含むパスだとコマンドが分裂・意図しないシェル展開を
  起こしていた。`shlex.quote()`で両方の値をquoteするよう修正
- **絞り込みヒントが非既定の `--max-age` を引き継いでいなかった**: 意図的に
  7日デフォルトより古いキャッシュを許容していても、ヒントどおりに追いコマンドを
  打つと既定のmax-ageに戻り再取得が起きてしまう。`corpus_hint_args()`に
  `--max-age`の伝播を追加 (`--file`指定時は`--cache-dir`と同様に無関係になるため
  含めない)
- **`truncate_content`の安全な境界が見つからない場合、生スライスにfallbackしていた**:
  1行目単体で既にmax_charsを超える等の縮退ケースで、本来防ぐべき「境界を跨いだ
  切り詰め」を結局再現していた。安全な境界が無い場合は本文を一切出さず切り詰め
  通知のみを返すよう修正 (常に整形済み)
- **別branchで先に修正済みだった2件をこの`_common.py`のコピーにも反映**:
  `score_entry`の正規化空文字列キーワード除外・`_norm`の頭字語(mixed case)
  判定。分岐が異なるbranchで並行開発していたため、このコピーには未反映のまま
  残っていた

回帰テスト15件追加 (`test_common.py`、162 tests, all green)。

## [0.18.2] - 2026-08-27

### `fetch-index`/`search-index`のslug衝突・`content`切り詰めのMarkdown境界破壊・絞り込みヒントの corpus 選択欠落 (0.18.1 の追いコミット)

- **URL末尾が同じ複数ページで slug が衝突していた**: `.../hooks` と
  `.../agent-sdk/hooks` のように最後のパス要素だけが一致する2ページは、
  従来どちらも `[hooks]` と表示されており、その値を `sections`/`content` に
  そのまま渡すと `_resolve_page_ref` 自身の曖昧slugエラーで弾かれ、
  フォールバック参照として機能しなかった。`_build_unique_slugs()` を追加し、
  全ページの中で一意になるまでパス要素を後ろから伸ばして (`hooks` →
  `agent-sdk/hooks` 等) 表示するよう修正 (claude-docsの`fetch-index`/
  `search-index`のみ該当。他2 scriptはこの参照形式を使わない)
- **`--max-chars` の切り詰めが生の文字数スライスで、コードフェンス/表の
  途中で切れることがあった**: `truncate_content()` を行単位の走査に変更し、
  `FenceTracker` が閉じておりかつ表の行でもない、直近の安全な行境界まで
  戻ってから切り詰めるよう修正 (安全な境界が `max_chars` 未満になっても、
  フェンス/表を最後まで含めて `max_chars` を超えるよりは安全側に倒す設計)
- **`--max-chars` の絞り込みヒント / `sections`のサブセクション追いヒントが
  `--file`/`--cache-dir` を引き継いでいなかった**: 非既定の corpus 選択で
  実行した際、ヒント通りにフォローアップコマンドを打つと既定の corpus に
  フォールバックし、同じ番号が別のドキュメントを指す事故があった。
  `corpus_hint_args()` を追加し3 script共通で伝播するよう修正
  (`--file`と`--cache-dir`は排他 — `_load_docs`/`_load_full_txt`は`--file`
  指定時`cache_dir`を一切参照しないため、両方を出すと誤解を招く。
  `--file`があれば`--cache-dir`は出さない)

回帰テスト12件追加 (`test_common.py` 9件、`test_parse_claude_docs.py` 2件、
`test_parse_ai_sdk.py` 1件、既存golden出力テスト2件を新挙動に更新。
147 tests, all green)。

## [0.18.1] - 2026-08-27

### doc_idx 表示のレビュー指摘 1 件 + `sections` のドキュメント不整合 1 件を修正 (0.18.0 の追いコミット)

- **`fetch-index`/`search-index` が期限切れの llms-full.txt キャッシュからも doc_idx を join していた**:
  `--max-age` を過ぎたキャッシュは、後続の `sections`/`content` 呼び出しが実際に
  再取得する対象。再取得後は上流の並び替え/追加で doc_idx が変わり得るため、
  期限切れキャッシュとの join は「これから置き換わる番号」を表示する形になり、
  この機能が防ぐはずだった番号ズレを再現してしまっていた。`_load_url_to_idx_if_cached`
  に `max_age` を渡し、`fetch_url` 自身の再取得判定 (`age >= max_age`) と同じ基準で
  「未キャッシュ」と同様 slug 表示にフォールバックするよう修正
- **`sections` が (3 script 共通で) 生の見出しタイトルを表示しており、ネストした
  同名見出しが区別できなかった**: SKILL.md は `sections`/`search`/`content` の
  出力を「そのまま `content` の heading_path にコピーしてよい」と案内しているが、
  `sections` は完全パスではなく末尾のタイトルのみ (例: 2 つの `## Client`/
  `## Server` 配下にそれぞれ `### Examples` があると両方とも `Examples` とだけ
  表示) を出力していたため、コピーした値が `content` の完全一致ではなく部分一致
  (曖昧チェック対象) に落ちてしまうことがあった。`format_heading_path_for_display`
  (search 系が既に使っている表示関数) を再利用し、3 script 全ての `sections` で
  完全な heading_path を表示するよう修正 (トップレベル見出しは従来どおり
  タイトル単体と同じ表示になるため、既存の golden output テストへの影響は無い)

回帰テスト 5 件追加 (`test_parse_claude_docs.py` 3 件、`test_parse_ai_sdk.py` 1 件、
`test_parse_firebase.py` 1 件、135 tests, all green)。

## [0.18.0] - 2026-08-27

### `fetch-index` / `search-index`: llms.txt の一覧位置を doc_idx として表示していた問題を修正

`fetch-index` の `[N]` と `search-index` の `[idx]` は llms.txt の**一覧内の位置**
をそのまま表示していたが、`sections`/`content` は llms-full.txt の doc_idx を
期待する — 2 つのファイルの並び順が一致しない限り (上流が並び替え/追加した
時点で崩れる)、表示された番号をそのまま渡すと**別のページが開く**。

- llms-full.txt が既にキャッシュ済みなら URL join で正しい doc_idx を表示、
  未キャッシュなら (このためだけに数十 MB の llms-full.txt を fetch すると
  fetch-index が「軽量なフォールバック」でなくなるため) 代わりに URL slug を
  `[slug]` として表示し、`Next: sections <slug>` に変更する
  (slug も `sections`/`content` の有効な入力形式)
- `search-index` にあった「番号がずれるかもしれない」という無条件の注意書きは
  削除し、未キャッシュ時のみ表示する条件付きの Note に置き換えた
  (キャッシュ済みなら実際に正しいので注意書き自体が不要)
- `fetch-index` の variant グループ表示 (`Batches (Python)`/`Batches (Go)` 等)
  も同じ理由で `[0-1]` のような一覧内レンジ表示をやめ、variant ごとに
  `Python [ref]` の形式にした

### `content`: 出力サイズ上限が無く長いページで末尾のヒントが不可視化する問題を修正

`content` の出力に上限が無く、Platform ページ (平均 ~38KB) 等では Bash tool の
~30KB inline 表示上限を超えて別ファイルに退避され、先頭 2KB しか見えなくなる。
本文末尾にしか出していなかったサブセクション一覧 / `Next:` ヒントがこの状態で
不可視になり、`heading_path` を省略した 2 手目の深掘りができなくなっていた。

- `--max-chars` (既定 24000、`0` で無制限) を 3 script の `content` に追加。
  超過時は `... (N chars truncated; narrow with <script> content <ref>
  "<heading_path>")` を出す
- サブセクション一覧 + `Next:` ヒントを **metadata header 直後 (本文の前)
  にも** 出力するよう変更 (末尾には従来どおり出力、前後の二重掲載)。
  `--max-chars 0` 等で本文が長くても、前側のヒントは常に生き残る

### ai-sdk / firebase の `content` にもサブセクション一覧を追加 (claude-docs と同機能に統一)

claude-docs の `content` だけが持っていたサブセクション一覧 + `Next:` ヒントを
ai-sdk / firebase にも追加した。claude-docs 専用実装だった
`_print_subsection_hints` を `_common.print_subsection_hints()` として汎用化し
(min_level を引数化)、3 script が共有する。ai-sdk / firebase の `content` に
`--no-subsection-hints` フラグも新設 (claude-docs は既存)。

- **利用者向け挙動変更**: ai-sdk / firebase の `content` は既定で
  サブセクション一覧を出力するようになる (抑制したい場合は
  `--no-subsection-hints`)
- 本バッチでは firebase 本文中のリンクへの `→ [doc_idx N]` 注釈付与
  (claude-docs にある機能) は対象外とした — サブセクション一覧という
  中核機能のパリティを優先し、範囲を絞った

### SKILL.md

3 SKILL とも `content` のコマンドリファレンスと Quick Start 相当の説明に
`--max-chars` / (ai-sdk・firebase は新規) `--no-subsection-hints` / 前後二重
掲載の挙動を追記。metadata version を patch bump:
researching-claude-docs 3.4.3 → 3.4.4 / researching-ai-sdk 3.3.4 → 3.3.5 /
researching-firebase 2.1.3 → 2.1.4

### テスト

`scripts/tests/` に回帰テストを 13 件追加 (129 tests, 従来比 +13)。
`fetch-index`/`search-index` の doc_idx join (キャッシュ有無の両方)、
`--max-chars` の切り詰めと `--max-chars 0` での無効化、前後ヒントの二重
掲載と `--no-subsection-hints` によるその抑制を claude-docs/ai-sdk/firebase
それぞれで検証。ai-sdk/firebase の既存 golden 出力テストは今回の意図した
挙動変更 (サブセクション一覧の追加) に合わせて期待値を更新した
(golden テストが壊れた = 退行ではなく意図した機能追加の確認)。

## [0.17.4] - 2026-08-28

### silent-e複数形 (Responses/Releases/Databases/Caches) の誤stem化を既知の限界として明文化 (未修正)

Codex R4 で `_norm()` の sibilant-suffix 規則 (`ses`/`xes`/`zes`/`ches`/`shes`
終わりの語から2文字strip) が silent-e語根の複数形も誤って2文字stripしてしまう
指摘を受けた (`Responses`→`respons`, `Caches`→`cach` 等、単数キーワードは
末尾`e`を保持するため一致しなくなる)。

調査の結果、これは文字パターンだけでは解決不可能な曖昧性と判断した:
`caches`(cache+s, silent-e語根) と `matches`(match+es, 硬子音語根) は
末尾が完全に同形で、辞書 (語根リスト) か本物のstemmerなしに文字列だけからは
判別できない。既存テストはこの硬子音側の挙動を一切pinしていなかったため
安全側 (どちらの挙動も壊さない) を優先し、現状挙動をcharacterization test
として明文化するに留めた。修正は内部バックログで追跡する。

回帰テスト1件追加 (`test_common.py`、126 tests, all green)。

## [0.17.3] - 2026-08-27

### ALL-CAPSの通常複数形 (`HOOKS`等) が単複変換されず一致しなくなる回帰を修正 (0.17.2 の追いコミット)

0.17.2 の頭字語判定 (`_norm()` の大文字2文字以上をacronymとみなすguard) は
`HOOKS`/`SKILLS`のような**全て大文字の通常の複数形**も誤ってacronym扱い
していた。クエリ側は変換されず`"hooks"`のままなのに、コーパス側の
`"Hooks"`は通常どおり`"hook"`に変換されるため、本来完全一致するはずの
組み合わせが不一致になっていた (`score_entry("Hooks", "", ["HOOKS"])` が
0点)。

判定条件を「大文字2文字以上」から「先頭以外に大文字があり、かつ小文字も
含む (mixed case)」に絞り込み。`"iOS"`/`"macOS"`はこの条件を満たすため
引き続きacronym扱いされる一方、`"HOOKS"`(小文字を含まない全大文字)は
条件を満たさず通常どおり単複変換されるよう修正。

回帰テスト1件追加 (`test_common.py`、125 tests, all green)。

## [0.17.2] - 2026-08-27

### `score_entry` が頭字語 "iOS" 等を複数形と誤認し無関係な語に誤ヒットする問題を修正 (0.17.1 の追いコミット)

`_norm()` の単複変換 (末尾 `s` 除去) は "iOS" のような 2 文字以上の大文字を含む
頭字語にも無条件で適用されており、"iOS" → "io" に変換されていた。"io" は
"Configuration" や "Migrations" など無関係な多くの語にも部分文字列として
含まれるため、これらが偽陽性でヒットしていた。候補が上位 5 件までしか本文に
潜らない Firebase の `search` では、この偽陽性が本来ヒットすべきページを
候補から押し出しかねない実害があった。

元のスペルに大文字が 2 文字以上含まれるトークンは単複変換をスキップする
(通常の英語複数形が "DogS" のように大文字を複数含む形で書かれることは
まず無いため、これを頭字語/固有名詞の signal として扱う) よう修正。
小文字化・区切り文字除去自体はそれ以外のトークンと同様に行う。

回帰テスト3件追加 (`test_common.py`、124 tests, all green)。

## [0.17.1] - 2026-08-27

### `extract_content` / `score_entry` のレビュー指摘 2 件を修正 (0.17.0 の追いコミット)

- **大文字小文字違いの完全一致が、部分一致の曖昧判定より後に評価されていた**:
  見出しマッチングは大文字小文字を区別しないと文書化されているにも関わらず、
  完全一致の判定は大文字小文字を区別する形のみで、大文字小文字違いの完全一致
  (例: `## Configuration` に対する `"configuration"`) は完全一致扱いされず
  部分一致の曖昧チェックまで落ちていた。ネストした子見出し (`### Options`)
  があると、子の heading_path (`Configuration/Options`) も同じ部分文字列
  `"configuration"` を含むため、実際には曖昧でない入力が
  `Error: ambiguous heading` で失敗していた。完全一致の判定に大文字小文字を
  区別しない第2パスを追加し、部分一致の曖昧チェックより先に評価するよう修正
- **`score_entry` の正規化して空文字列になるキーワードが全件にマッチしていた**:
  `_norm()` が区切り文字のみを除去する性質上、`"_"` / `"--"` / `"_-"` のような
  キーワードは正規化後に空文字列になる。空文字列はどの文字列に対しても
  `in` 判定が真になるため、こうした縮退キーワードが実質「全件マッチ」として
  スコアされていた (`search-index "_"` 等が全件をヒットさせる)。正規化後に
  空文字列になるキーワードはスコア計算・all-keywords-matched ボーナス判定の
  両方から除外するよう修正

回帰テスト 5 件追加 (`test_common.py`、121 tests, all green)。

## [0.17.0] - 2026-08-26

### `content` の heading_path 解決: 曖昧な部分一致の無言解決 / `(top)` 未対応を修正

`_common.extract_content()` の見出し解決ロジックに 2 件の不具合があった。

- **曖昧な部分一致を無言で先頭候補に解決していた**: 完全一致が無い場合の
  部分一致検索は最初にマッチした 1 件を無条件で採用しており、`"config"` の
  ような曖昧な入力が複数セクション ("Client Configuration" / "Server
  Configuration" 等) に一致しても警告なく先頭を返していた。呼び出し側が
  どのセクションが実際に選ばれたか判別できず、誤引用の温床になっていた。
  部分一致の候補が 2 件以上ある場合は `Error: ambiguous heading '...'.
  Matches: ...` で候補一覧を出して exit 1 するよう変更 (`_resolve_page_ref`
  の slug 解決が既に持っていたのと同じ設計)。**完全一致は従来どおり最優先
  — 曖昧チェックより先に判定するため、`sections`/`content`/`search` の出力
  からそのままコピーした heading_path が新たに失敗することはない**
- **`search` が返す `Section: (top)` を `content` に渡すとエラーになっていた**:
  見出し前の本文ヒットは `heading_path` が `"(top)"` として返るが、
  `extract_content` にはこの値を認識する分岐が無く `Error: heading '(top)'
  not found` になっていた。`heading_path == "(top)"` を先頭〜最初の見出し
  直前までを返す専用分岐として処理するよう修正。`search`/`search-content`
  の `Section: (top)` 表示は `(top)  [before first heading]` という
  区切りのある注釈付きに変更 (値部分の `(top)` 自体はそのまま `content`
  にコピーできる — 表示だけを分かりやすくし、コピー&ペースト前提の
  値は壊さない設計)
- 上記に伴い `extract_content()` は `content` 単体の文字列ではなく
  `(content, resolved_heading_path)` のタプルを返すよう変更。3 script の
  `cmd_content` は解決済みの `resolved_heading_path` をヘッダー表示と
  サブセクションヒントに使うようになり、部分一致で入力とは異なる
  正式なパスに解決された場合もヘッダーに正しい値が表示される
  (従来は呼び出し側が渡した生の入力をそのままヘッダーにエコーしていた)
- `parse-claude-docs.py` の `_print_subsection_hints` が持っていた
  独自の部分一致検索 (`extract_content` と同じ「無言で先頭候補」ロジックの
  重複実装) を削除し、呼び出し側から渡される解決済みパスへの完全一致
  検索のみに簡略化 (二重実装が片方だけ直って表示とヒントが食い違う事故を防ぐ)

### 検索スコアリング: 単複同形の揺れ (skills ↔ Skill 等) が 0 点になる問題を修正

`_common.score_entry()` は単純な部分一致のみで、`score_entry('Skill', '',
['skills'])` や `score_entry('Hook events', '', ['hooks'])` が 0 点になって
いた (逆方向の `skill` → `Skills` は部分一致で 5 点)。上位 5 件しか本文に
潜らない `search` では、複数形で検索しただけで該当ページが候補から
落ちていた。

- `_norm()` を追加: 小文字化 + `-`/`_` 除去 + 末尾の軽量な単複変換
  (`ies→y`、`ses/xes/zes/ches/shes→` 語幹、それ以外の末尾 `s` 除去。
  `ss` で終わる語は対象外)。クエリキーワードと `tags` (元々 1 語単位の
  ラベル) はこれをそのまま適用
- `_norm_phrase()` を追加: title/description/headings (複数語の自由文) は
  `_norm()` を**単語単位**で適用してから再結合する。`_norm()` の長さ判定
  (`len(t) > 4` 等) は 1 語想定のため、複数語の文字列全体にそのまま
  適用すると閾値が「文字列全体の長さ」で評価され、同じ単語 (例:
  `"uses"`) が短いクエリでは 1 語として正しく変換されるのに、長い
  フレーズ (例: `"Common uses"`) に埋め込まれると末尾 2 文字を
  機械的に削るだけの変換になり、正規化前なら一致していたはずの
  部分一致 (`"uses" in "Common uses"`) を失う回帰を実装レビューで発見・
  修正した (`"Common uses"` → 誤: `"common us"` / 正: `"common use"`)。
  完全一致判定も正規化後の文字列で行う (同じ変換を同じ文字列に適用する
  だけなので、既存の完全一致判定が緩んで partial 側に落ちることはない
  — 新しい一致が増える方向にのみ働く)
- 変更前後の `score_entry` を実データ相当のタイトル/説明/クエリ
  (~2800 通りの組み合わせ) で突き合わせる差分スクリプトを実行し、
  「変更前は 0 点超だったのに変更後 0 点になった」組み合わせが 0 件、
  「変更前は完全一致 (10 点) だったのに変更後 10 点未満になった」
  組み合わせが 0 件であることを確認 (差分は新規一致 74 件・スコア
  上昇 28 件のみで説明不能な後退は無し)。うち代表的な組み合わせは
  `ScoreEntryRegressionFloorTest` として恒久的な回帰テストに固定した

### テスト

`scripts/tests/` に回帰テストを 30 件追加 (116 tests, 従来比 +30)。

- `test_common.py`: `extract_content` の `(top)` 処理 2 件、部分一致の
  正式パス解決 1 件、曖昧な部分一致の die 化 1 件、完全一致が曖昧チェックに
  先立つことを確認する回帰 1 件、`_norm`/`_norm_phrase` の単複/ハイフン
  変換 5 件、上記差分スクリプトで確認した後退ゼロを固定する
  `ScoreEntryRegressionFloorTest` 2 件、`format_heading_path_for_display`
  2 件を追加。既存の
  `test_plural_mismatch_scores_zero_known_limitation` (characterization
  test) は今回の修正で実際に挙動が変わったため
  `test_plural_query_now_matches_multiword_title` として「修正後の正しい
  挙動」に書き換えた (characterization test のまま放置すると退行検知に
  ならないため)
- `test_parse_claude_docs.py` / `test_parse_ai_sdk.py` /
  `test_parse_firebase.py` に CLI 統合テストを追加: `search` →
  `content "(top)"` の実際のコピー&ペースト経路を通しで検証する
  round-trip テスト (claude-docs / firebase)、`--max-age` の再取得境界を
  `urlopen` モック + `os.utime` で決定論的に検証するテスト、`--file`
  読み取り専用モード (fetch なし・存在しないパス・source 不一致) のテスト、
  argparse 自身の usage error (exit 2) を plugin 側の exit 2 (「上流
  フォーマット変化」) と混同しないことを確認するテスト、3 script
  それぞれの代表コマンドの標準出力を verbatim で固定する golden 風テスト
  (出力フォーマットは 3 script が独立実装のため、将来のドリフトを検知する)

### SKILL.md

3 SKILL とも `heading_path` の指定方法を見直した: 推奨を「`sections`/
`search`/`content` 出力の完全なパスをそのままコピー」に変更し (短い
部分一致は曖昧だと die するようになったため)、「曖昧な部分一致は die
する」「`(top)` は `content` にそのまま渡せる」を明記。metadata version
を patch bump: researching-claude-docs 3.4.2 → 3.4.3 / researching-ai-sdk
3.3.3 → 3.3.4 / researching-firebase 2.1.2 → 2.1.3

## [0.16.1] - 2026-08-25

### `default_cache_dir` / `fetch_url` のレビュー指摘 2 件を修正 (0.16.0 の追いコミット)

- **相対パスの `$XDG_CACHE_HOME` を無効値として扱っていなかった**: XDG Base
  Directory 仕様では `$XDG_CACHE_HOME` は絶対パスでなければならず、相対値は
  無効として無視するのが正しい。従来は `os.path.expanduser` を通すだけで
  絶対/相対を判定していなかったため、相対値を設定すると起動 cwd 相対で
  キャッシュ先が決まってしまい (再現性が無く、想定外のプロジェクト
  ディレクトリを汚染/書込失敗させ得る)、`~/.cache` へのフォールバックが
  効いていなかった。`os.path.isabs()` で判定し、絶対パスの場合のみ採用、
  相対 (空文字含む) は `~/.cache/llms-docs` にフォールバックするよう修正。
  `$LLMS_DOCS_CACHE_DIR` (プラグイン独自の完全上書き用エスケープハッチ、
  XDG 仕様の対象外) は意図的に対象外のまま維持 (相対値もそのまま受理)
- **`Content-Length` が非整数のとき `ValueError` が未捕捉だった**: レスポンス
  ヘッダーの `Content-Length` が数値として不正な値 (壊れた/非準拠な
  サーバー・中間プロキシ由来) だった場合、`int(content_length)` が
  `ValueError` を送出するが、`fetch_url` の except 節は
  `(URLError, OSError, http.client.HTTPException)` のみを捕捉しており
  対象外だった。その結果、期限切れキャッシュが存在していても stale-serve
  フォールバックへ進まず、生の Python traceback が利用者に露出していた。
  `int()` 変換を `try/except ValueError` で包み、変換失敗時は
  `Content-Length` が無いのと同じ扱い (検証をスキップ) にする方式で修正
  (読み取り自体は成功しているため、ヘッダーが壊れているという理由だけで
  正常に受信済みのボディを破棄する理由が無い)

### テスト

`test_fetch_and_cache.py` に回帰テストを 6 件追加 (86 tests, 従来比 +6)。
`DefaultCacheDirTest` に相対パス / 空文字 / `~` プレフィックス付き
`$XDG_CACHE_HOME` の 3 パターンと `$LLMS_DOCS_CACHE_DIR` の相対値受理を
確認する 1 件、`FetchUrlTest` に非整数 `Content-Length` で例外が飛ばない
ことを確認する 1 件と、`Content-Length` 不一致時の警告メッセージが
(`IncompleteRead` の `args` に載る) 受信済みボディ全体を stderr に
ダンプしていないことを確認する 1 件を追加 (`IncompleteRead.__str__` が
ペイロードでなく要約文字列を返すことは Python 3.11/3.12/3.14 で実機確認済み
だが、標準ライブラリの実装詳細であり将来変わり得るため回帰テストとして固定)。

## [0.16.0] - 2026-08-25

### キャッシュディレクトリの既定値を `/tmp` から XDG 準拠に変更

`/tmp` は再起動やOSの定期クリーンアップで消えるため `--max-age` によるキャッシュが
実質無効化されていたほか、マルチユーザー環境では world-writable な `/tmp` を
共有することによる `PermissionError` のリスクもあった。

- `_common.default_cache_dir()` を追加。解決順は `$LLMS_DOCS_CACHE_DIR` (完全上書き)
  > `$XDG_CACHE_HOME/llms-docs` > `~/.cache/llms-docs`
- `add_cache_dir_arg()` の既定値をこの関数の戻り値に変更。`--cache-dir` での
  個別指定は従来通り可能
- 3 script (`parse-claude-docs.py` / `parse-ai-sdk.py` / `parse-firebase.py`)
  の `--cache-dir` 既定値・ヘルプ文言を統一。firebase の `DEFAULT_CACHE_DIR =
  "/tmp"` 定数と ai-sdk のヘルプ文言に残っていたハードコードされた `/tmp` を除去
- README.md のキャッシュ表・前提条件、3 SKILL.md のキャッシュ表・失敗時対処表の
  `/tmp` 参照をすべて `~/.cache/llms-docs` ベースの記述に更新
- **利用者向け挙動変更**: 既存の `/tmp` 配下のキャッシュファイルは新しい既定
  ディレクトリからは見えなくなる (再取得が発生する)。旧 cache の自動移行は
  行わない (import 元が `/tmp` で消えている可能性が高く、移行の実利が薄いため)

### `fetch_url` の堅牢性向上: stale-serve / 例外拡張 / atomic write

- **stale-serve**: fetch に失敗しても既存キャッシュ (期限切れ含む) があれば
  `WARNING: fetch failed (...); using cached copy (<age> old)` を stderr に出し
  そのキャッシュを返して継続する (exit 0)。キャッシュが無い場合のみ
  `Error: ...` で exit 1。従来は期限切れキャッシュがあっても fetch 失敗で
  即 exit 1 しており、「ネットワーク不調時だけ検索が完全に使えなくなる」
  という壊れ方をしていた
- **例外の捕捉範囲拡張**: `except urllib.error.URLError` を `except
  (urllib.error.URLError, OSError, http.client.HTTPException)` に拡張。
  read timeout (`TimeoutError`、`OSError` のサブクラス) や途中切断
  (`http.client.IncompleteRead`) が生の Python traceback として利用者に
  露出していたのを解消
- **Content-Length 検証**: レスポンスに `Content-Length` があり受信バイト数と
  不一致なら `IncompleteRead` として扱い、不完全なデータをキャッシュに
  書き込まない
- **atomic write**: `_common._atomic_write()` を追加。同一ディレクトリ内の
  一時ファイルへ書いてから `os.replace` で原子的に差し替える (別ファイル
  システム間の rename は atomic でなくなるため、必ず対象と同じディレクトリに
  作成する)。並列 Skill fork や連続実行が同じキャッシュパスに競合しても、
  読み手が truncate 直後の空/途中のファイルを掴むことがなくなる
- **`load_lines` の decode を `errors="replace"` に変更**: 途中で打ち切られた
  多バイト文字が原因の `UnicodeDecodeError` で全コマンドが traceback して
  いたのを、置換文字で継続するよう緩和した
- 3 SKILL.md の失敗時対処表の「ネットワーク失敗」「キャッシュ破損」行を
  上記の新しい挙動 (stale-serve / `--max-age 0` 推奨) に合わせて書き換え。
  SKILL.md metadata version をそれぞれ patch bump:
  researching-claude-docs 3.4.1 → 3.4.2 / researching-ai-sdk 3.3.2 → 3.3.3 /
  researching-firebase 2.1.1 → 2.1.2

### テスト

`scripts/tests/test_fetch_and_cache.py` を新設 (19 tests)。`default_cache_dir`
の環境変数解決順、`_format_age`、`fetch_url` の atomic write / stale-serve /
widened exception handling / Content-Length 検証、`load_lines` の
`errors="replace"` をカバーする。ネットワーク失敗系のテストは
`unittest.mock.patch("urllib.request.urlopen")` を使用 (cache fixture 方式は
`fetch_url` の早期 return を突破できないため、この経路だけはモックが必要)。

## [0.15.0] - 2026-08-25

### 検索フォールバック: 本文にしか無い語で `search` が 0 件になる問題を修正

`search` (claude-docs / ai-sdk) は llms.txt の title/description で候補ページを
先に絞り込み (index ランキング)、その上位候補の本文だけを検索していたため、
オプション名やフィールド名のように**本文にしか出現しない語**で検索すると、
候補ページ自体が 0 件になり `search-content` なら hit するはずの語が
「No matching pages found」になっていた (例: `compact_summary` / `onFinish`)。

- `_common.py` に `full_corpus_body_search()` を追加。index 候補が 0 件、または
  絞り込んだ候補の本文ヒットが全て 0 件のときに、ロード済み全ドキュメントへ
  本文検索を実行し、上位 N 件を `[body-only]` 付きで**追加**表示する
- **index 候補を上書きしない**: title/description で正しくランクされたページは
  本文ヒットが 0 件でも従来通り「(no body hits — index match only)」として
  残る。フォールバックは index ランキングが拾えなかったページを**追加**するだけ
  (最初の実装では結果を丸ごと置き換えてしまい、正しく index マッチしたページが
  本文 0 件というだけで結果から消える回帰があったため、追加方式に修正)
- firebase は対象外 (本文検索の全文フォールバックは全ページ HTTP fetch が
  必要でコストが高すぎるため)。代わりに `search` / `search-index` の 0 件 Tip に
  `search-content "<query>"` (`--page-ref` 省略で全ページ横断) を明記
- `search-index` (3 script) の 0 件 Tip にも `search-content` を明記

### 上流フォーマット変化の検知: index/doc が 0 件でも無警告 exit 0 だった問題を修正

`_common.py` に `assert_parsed(label, count, path)` を追加。パース結果が
0 件のとき `Error: <source> format may have changed` を stderr に出し
**exit 2** で終了する。「クエリに対する正当な 0 件検索結果」(exit 0) と
「パーサ自体が壊れている 0 件」を区別する。claude-docs / ai-sdk の index
パース・llms-full.txt パースの全呼び出し箇所に適用した。

- firebase は元々 `die()` (exit 1) で 0 件を検知していたが、`assert_parsed`
  (exit 2) に統一した。**利用者向け挙動変更: exit code が 1 → 2 になる**
  (メッセージ文言も他 2 script と揃えた)
- `_common.py` に `check_join_rate(label, joinable, total)` を追加。
  claude-docs の index↔full-text URL join rate が 50% 未満のとき exit 2
  (50〜80% は従来通り WARNING のみで継続)
- `parse_llms_index` の箇条書き記法を `-` 限定から `-`/`*`/`+` に拡張
  (インデントは従来通り strip 済みなので無条件で許容)。上流が bullet
  記号を変えただけで index が silent に 0 件になる故障モードを縮小
- ai-sdk: パースした doc の 10% 超が frontmatter title 無しのとき
  `fetch-index` / `search-index` / `search` で WARNING を出す
  (body の `---` 水平線を新規 frontmatter と誤認する等の兆候)

### テスト基盤: scripts/tests/ を新設 (stdlib unittest, 61 tests)

`scripts/` 配下にテストが 0 件だったため fixture ベースの回帰テストを新設した。
`parse-*.py` はファイル名にハイフンを含み通常の `import` ができないため、
`tests/_loader.py` が `importlib.util.spec_from_file_location` でロードする
helper を提供する (CLI レベルのテストは `--cache-dir` に事前生成した cache
ファイルを置くことで、`fetch_url` のキャッシュ短絡経路を使いネットワーク
アクセス無しで実行できる)。CI (`.github/workflows/validate.yml`) は
`find plugins -type d -name tests` で自動検出するため追加設定は不要。

- `test_common.py`: `parse_llms_index` (3 記法 + `*`/`+`/インデント + 非
  ASCII)、`extract_sections` (レベル飛び)、`extract_content` (fence 延長 /
  table 保護)、`score_entry`、`search_content_in_body` (AND→partial /
  overflow)、`assert_parsed` / `check_join_rate` / `full_corpus_body_search`
- `test_parse_claude_docs.py`: `split_documents` (fence 内 H1 / platform
  重複 H1 merge / Source・URL 抽出)、検索フォールバックと format-change
  検知の CLI 統合テスト
- `test_parse_ai_sdk.py`: `split_documents` (先頭ゴミ / fence 内 `---`)、
  `parse_frontmatter`、検索フォールバック・format-change 検知・
  untitled-ratio 警告の CLI 統合テスト
- `test_parse_firebase.py`: cache fixture (`_url_to_cache_filename` を
  再利用してページキャッシュを命名) 経由の CLI 統合テスト
- **characterization test を 2 件追加** (既知の未修正の限界を「バグとして
  固定」ではなく「現状の挙動として明示的に pin」する目的、修正は別スコープ):
  - `_common.FenceTracker` が backtick fence (` ``` `) のみを認識し
    tilde fence (`~~~`) を認識しない (今回のテスト作成中に新規発見。
    3 script 共通の `split_documents`/`extract_sections`/`extract_content`
    全てに影響するため、実データでの before/after diff 無しに直すのは
    リスクが高いと判断し本 PR では見送り。内部バックログでの追跡を推奨)
  - ai-sdk `split_documents` が本文の `---` + `Note:` 等の散文で偽の
    doc 境界を作る (既知の限界。内部バックログで追跡中、P3)

### Python バージョン要件を明文化 (3.11+)

`parse-*.py` は PEP 604 記法 (`list[X]` / `X | None`) を素の型注釈として
使っており、Python 3.11 未満 (例: macOS 標準の `/usr/bin/python3` = 3.9 系)
では起動直後に `TypeError: unsupported operand type(s) for ...` で
クラッシュする。marketplace 横断方針により **3.11+ を宣言する** 対応を採用した
(3.9 互換化のための `from __future__ import annotations` 追加は不採用 —
`scripts/_common.py` の既存 shim はそのまま維持するが、3 script 側の型注釈を
3.9 互換にする目的の変更は行わない)。

- README.md の前提条件を「3.10+」→「3.11+ 必須」に書き換え、
  `TypeError` が起きる理由と対処 (`mise use python@3.11` 等) を明記
- 3 SKILL.md の「失敗時の対処」表に Python バージョン不足の行を追加
  (`TypeError` から即座に原因が分かるようにし、WebFetch フォールバックへの
  不要な迂回を減らす)。SKILL.md metadata version をそれぞれ patch bump:
  researching-claude-docs 3.4.0 → 3.4.1 / researching-ai-sdk 3.3.1 → 3.3.2 /
  researching-firebase 2.1.0 → 2.1.1
- CI (`.github/workflows/validate.yml`) は既に Python 3.12 (3.11+) を
  使用済みのため変更不要

## [0.14.0] - 2026-06-08

### researching-ai-sdk: ai-sdk.dev 上流構造変更に追従

ai-sdk.dev/llms.txt が ~2KB / 46 行のインデックス + 検索 API 案内 + llms-full.txt
リンクに分離され、本体は `llms-full.txt` (~5MB / 530 doc) に移動した。
旧構造 (1 ファイルに全 doc 連結) を前提にしていた `parse-ai-sdk.py` では
`search "streamText"` 等の基本 API 検索がゼロ件になっていたのを修正。

#### 変更内容

- `LLMS_TXT_URL` を `https://ai-sdk.dev/llms.txt` → `https://ai-sdk.dev/llms-full.txt`
  に切替
- `DEFAULT_CACHE_FILENAME` を `ai-sdk-llms.txt` → `ai-sdk-llms-full.txt` に変更
  (旧 cache との混在を避ける)
- fetch timeout を 60s → 120s (5MB ファイルのため余裕を確保)
- docstring / argparse description / 各種コメントを `llms-full.txt` 起点の
  説明に更新
- `llms-full.txt` 先頭は frontmatter ではなく contributing guide
  (TypeScript コード) で始まるが、`split_documents` は最初の `---` まで
  無視するため挙動影響なし
- SKILL.md / references/llms-txt-structure.md / README.md の cache filename
  と doc 数記述を更新

#### CLI I/O 仕様

既存サブコマンド (search / search-index / search-content / sections /
content / fetch-index) の I/O 仕様は維持。利用側のコマンド書き換えは不要。

#### 検証

- `search "streamText"` で複数 doc が hit (28 body hits in top doc)
- `search "useChat"` / `search "generateText"` も正常 hit
- `search "stream object"` (space 区切り) で structured data ガイドが top
- `fetch-index --compact` で 530 documents
- `sections 22` / `content 22 "<heading_path>"` で本文取得確認

#### SKILL.md metadata version

`3.3.0` → `3.3.1` (script 仕様変更に伴うキャッシュ filename 変更を反映。
skill 起動条件・トリガー語は不変なので patch bump)

## [0.13.0] - 2026-06-08

### researching-ai-sdk / researching-firebase: 使用感ベース改善の横展開

0.12.0 で `researching-claude-docs` に入れた改善パターンを ai-sdk /
firebase にも適用。3 SKILL の構造を揃え、初見ユーザーが同じ感覚で扱える
ようにした。

#### researching-ai-sdk: 3.2.0 → 3.3.0

- **Quick Start を冒頭に追加**: 2 コマンドのみの最小ブロック
- **`when_to_use` を独立フィールド化** + verb 拡張:
  `implementing, debugging, configuring, reviewing, or designing`
  + `especially before editing code that imports from ai / @ai-sdk/*`
- **Triggers に最新 API 名を追加**:
  `streamObject` / `generateObject` / `useObject` / `tool` / `tools` /
  `embed` / `embedMany` / `convertToModelMessages` / `provider`
- **出力フォーマット強制度を緩和** (claude-docs と同じ書き換え)
- `paths:` は追加しない (AI SDK は特徴的ファイル名がなく誤発火リスクが
  高いため、description ベースの auto-invoke に任せる)

#### researching-firebase: 2.0.0 → 2.1.0

- **Quick Start を冒頭に追加**: 2 コマンドのみの最小ブロック
- **`when_to_use` を独立フィールド化** + verb 拡張:
  `implementing, debugging, configuring, reviewing, or designing`
  + `especially before editing firebase.json / .firebaserc / *.rules /
  *.indexes.json`
- **Triggers に最新プロダクト名を追加**:
  `AI Logic` / `Genkit` / `App Hosting` / `Data Connect` /
  `security rules` / `firestore.rules` / `storage.rules`
- **`paths:` を新規追加** (Firebase 固有ファイル名で auto-trigger):
  `**/firebase.json` / `**/.firebaserc` / `**/firestore.rules` /
  `**/firestore.indexes.json` / `**/storage.rules` /
  `**/database.rules.json` / `**/remoteconfig.template.json` /
  `**/apphosting.yaml`
- **出力フォーマット強制度を緩和** (claude-docs と同じ書き換え)

#### 統一の効果

3 SKILL とも以下の構造で揃った:

```
---
description: |
  ... (各 source 固有の説明)
when_to_use: |
  Use when implementing, debugging, configuring, reviewing, or designing ...
  Use proactively before answering spec questions ...
  Triggers: ...
context: fork
model: sonnet
allowed-tools: [Read, Bash, WebFetch]
paths: [...]  # claude-docs / firebase のみ
metadata: { author, version }
---

# Title

## Quick Start
(2 コマンドの最小ブロック)

(以降は source 固有の詳細)

## 出力フォーマット (参考)
(緩和された最低限要素のみ)
```

## [0.12.0] - 2026-06-08

### researching-claude-docs: 使用感ベース改善 (AgentSkill 仕様調査の実体験から)

skill 自身を使って AgentSkill の最新仕様 (frontmatter フィールド一覧 /
ロード段階 / `context: fork` 挙動 / `paths:` 条件 / `hooks:` / Skill vs
Subagent) を verbatim 取得した実体験から、以下 3 点を改善。

#### 1. SKILL.md 冒頭に Quick Start を追加

9 セクション・183 行に対して「最初に何をすればいいか」が掴みづらかった。
冒頭に 2 コマンドのみの最小ブロックを置き、迷ったらここから始められるよう
にした。`--source both` / `--source platform` の指針も 1 行で示す。

```bash
# 1. キーワードで候補ページと本文ヒットを 1 コマンドで取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" search "<キーワード>"
# 2. 返ってきた [doc_idx] と heading_path を使って本文取得
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/parse-claude-docs.py" content <doc_idx> "<heading_path>"
```

#### 2. 最新 frontmatter フィールド名を Triggers に追加

`disable-model-invocation` / `user-invocable` / `argument-hint` / `effort` /
`arguments` / `context: fork` / `paths` / `SubagentStop` / `$ARGUMENTS` /
`$CLAUDE_SKILL_DIR` / `output style` を `Triggers:` に追加。新しいフィールド
名で質問されても description マッチで auto-invoke されるようにした。

合算 char 数: 689 → 約 980 chars (1,536 cap headroom 556)

#### 3. `paths:` glob に `**/.claude/skills/**` を追加

skill の `references/` や `scripts/` を編集する際にも自動 trigger されるよう
拡張。既存の `**/SKILL.md` は SKILL.md ファイル本体しかカバーしていなかった
ため、skill ディレクトリ全体作業時のロード抜けを補う。

#### 4. when_to_use の action verb 拡張

旧 `implementing, debugging, configuring, or reviewing`
新 `implementing, debugging, configuring, reviewing, or **designing**`
+ `especially before editing SKILL.md / agent / hook files` の Use-before
ガイダンスを追記。「Skill 自身を設計・編集している最中こそ skill が必要」
という観点を明示。

#### 5. 出力フォーマット強制度を緩和

「### 調査結果 / ### コード例 / ### 情報源 / ### 注意事項」の固定 4 セクション
を必須から「参考スケルトン」に変更。複数フィールドの仕様調査では表組み、
複数引用の比較では blockquote の方が読みやすい体験から、最低限満たすべき
要素 (発見事項 / 引用元 / コード例 / 注意事項) のみ示して、構成は柔軟に
できるようにした。

SKILL.md metadata version: `3.3.0` → `3.4.0` (Skill 自発 invoke 判定材料が
変わるため minor bump)

## [0.11.1] - 2026-06-06

### researching-claude-docs: paths あり skill 向け description ガイドラインに準拠

`paths` 指定がある skill では where (path 条件) が paths field に外出しできる
ため、`description` のトークンを「**何をする** (action) + **task 文脈での
トリガー語** (when)」に再配分するのが推奨。これに合わせて frontmatter を
ブラッシュアップ。

- `description` を**動詞先頭**に変更:
  `Fetch verbatim sections from Claude Code (code.claude.com) and Claude
   Developer Platform (platform.claude.com) 公式 docs — ...`
  (旧: 名詞列「Claude Code … 公式ドキュメントから … 取得する」)
- `when_to_use` の action verb を強化:
  旧 `implementing or debugging` → 新 `implementing, debugging,
  configuring, or reviewing`。`Skills/AgentSkill` を 1 トークンに統合
- Triggers から description で既出 / ユーザー prompt に出にくい host 系を削除:
  `"Claude Code ドキュメント"`, `"code.claude.com"`, `"platform.claude.com"`
- 合算 char 数: 774 → 673 chars (1,536 cap headroom 863)

SKILL.md metadata version: `3.2.0` → `3.3.0` (Skill 自発 invoke 判定材料が
変わるため minor bump)

## [0.11.0] - 2026-06-05

### researching-claude-docs: 3 つの機能追加

#### `paths:` auto-activation (SKILL.md frontmatter)

`SKILL.md` / plugin manifest / agent / command / hook / settings / MCP 設定
ファイル等を編集する直前に skill が自動でロードされる。Claude が WebFetch で
取りに行く事故を最小コストで減らす。

```yaml
paths:
  - "**/SKILL.md"
  - "**/.claude-plugin/**"
  - "**/.claude/agents/**.md"
  - "**/.claude/commands/**.md"
  - "**/.claude/hooks/**"
  - "**/.claude/settings*.json"
  - "**/.mcp.json"
  - "**/hooks.json"
```

#### `search --source both` 並列検索

`code` (Claude Code) と `platform` (Claude Developer Platform) を 1 コマンドで
横断検索する。Skill / hook / MCP のように両ソースに解説が散らばる topic で
切替コストが消える。

- 結果に `[code]` / `[platform]` プレフィックスを付けて区別
- `doc_idx` は **source 内ユニーク**なため follow-up 呼び出しに
  `--source <code|platform>` を明示するよう Note と Next hint で誘導
- `_search_one_source(args, source_key)` と `_print_search_results(results,
  label_source)` に分割してテスト・拡張容易性を確保

#### `content` 本文中の docs リンクに `→ [doc_idx N]` を付与

本文内の Markdown link (`[Text](/en/...)` / `[Text](/docs/en/...)` /
`[Text](https://code.claude.com/...)`) のうち**同 source 内の既知ページ**を
指すものに、自動でアノテーションが付く。follow-up の page 切替が 1 ステップ
減る。

- 絶対 URL と相対 path (`/en/...` ↔ `/docs/en/...` の alias 解決) の両方に対応
- self-link (現在ページへの link) は除外
- コードフェンス内 / Markdown テーブル行はスキップ (formatting 保護)
- `--no-link-annotations` で抑制可

#### SKILL.md metadata version

`3.1.0` → `3.2.0`

## [0.10.0] - 2026-06-05

### researching-claude-docs: 使用感ベースの改善

実際に AgentSkill 仕様を調査する過程で観察した摩擦点を 4 つ改善した。

#### `content` 出力にサブセクション hint を追加 (parse-claude-docs.py)

`content <doc_idx> "<heading_path>"` の本文末尾に、そのセクション直下の子
`heading_path` を一覧表示する。これまでは深掘り時に `sections` を再呼び出し
する必要があり 1 ステップ多かった。

- `heading_path` 指定時 → 直接の子 (level + 1) を表示
- `heading_path` 省略時 → トップレベル (L2) セクションを表示
- 出力例:

  ```
  --- Subsections of 'Configure skills/Frontmatter reference' (2) ---
    - Configure skills/Frontmatter reference/How a skill gets its command name
    - Configure skills/Frontmatter reference/Available string substitutions [code]

  Next: parse-claude-docs.py content 83 "<heading_path from above>"
  ```

- `--no-subsection-hints` で抑制可
- target.line_end は次の見出し開始でしかないため、target と同レベル以下の
  次セクションまでをブロック終端として再計算する

#### `search` / `search-content` の overflow セクション表示

`(60 body hits, showing 3)` 表示時、`--max-hits` (既定 3) で切り捨てた残り
セクションを `Other sections with hits (not shown):` として heading_path と
ヒット数を一覧表示する。`_common.py` の `search_content_in_body` 戻り値に
`overflow_sections` フィールドを追加 (既存呼び出し元は無視するだけなので
非破壊)。

#### SKILL.md frontmatter の再構成

公式 Skills ベストプラクティス (key use case first / 1,536 char cap) に
合わせて `description` と `when_to_use` を分離。

- `description`: 「公式ドキュメントから verbatim 取得 / WebFetch 回避」を
  冒頭に置き、Triggers/Use when を分離して説明性を上げた
- `when_to_use`: Triggers キーワード列 + Use when を集約。`"AgentSkill"`,
  `"Skill"` を Triggers に追加 (今回の調査で頻出キーワードだった)
- skill metadata version: `3.0.0` → `3.1.0`

#### SKILL.md 本文の重複削減

- `v3 互換性` セクションを削除 (時間依存表記の回避: `~/.claude/rules/claude/skills/principles.md`)
- 「推奨フロー」と「コマンドリファレンス」で重複していた `page_ref` /
  `heading_path` の説明を「リファレンス」セクションに集約
- 「サブフロー」をテーブル化して縦方向に圧縮
- サブセクション hint 仕様を `Step 2` 内に文書化

## [0.9.0] - 2026-06-05

### plugin rename: `doc-researcher` → `llms-docs` (BREAKING)

`context: fork` の skill が `<plugin>:<skill>` 完全修飾名 + description の
「独立コンテキストで実行され」表現により、**Skill ツールではなく Agent ツールの
`subagent_type` として誤呼び出し**される問題を解消した
(`Agent type 'doc-researcher:researching-claude-docs' not found`)。

- plugin 名 `doc-researcher` → `llms-docs` に変更。"doc-research**er**" の `-er`
  語尾が agent (researcher) を連想させていたのを除去
- ディレクトリ `plugins/doc-researcher/` → `plugins/llms-docs/` (`git mv`)
- skill 名 (`researching-claude-docs` / `researching-ai-sdk` /
  `researching-firebase`) は**動名詞命名のため維持** (Anthropic skill 命名推奨に従う。
  外部 rule の table 追従も不要)
- 3 SKILL.md の description「独立コンテキストで実行されメインセッションを消費しない」
  → 「Skill ツールで起動し、メインの会話コンテキストを消費しない」に変更
  (subagent 連想を排し、Skill ツール起動を明示)
- `marketplace.json` の entry name / source、SessionStart hook メッセージを追従
- `scripts/` は `${CLAUDE_PLUGIN_ROOT}` 経由参照のため動作影響なし
- `context: fork` + `model: sonnet` の設計 (subagent fork + Sonnet) は維持

#### 移行 (利用者向け)

旧名でインストール済みの場合は再インストールが必要:

```
/plugin uninstall doc-researcher@mao-worktools
/plugin install llms-docs@mao-worktools
```

## [0.8.0] - 2026-05-26

### SessionStart hook 追加

- doc-researcher plugin 有効時に SessionStart で「WebFetch より doc-researcher スキルを優先」リマインドを注入
- 生 stdout 形式で 1 行のみ (SessionStart では plain stdout が Claude に届く — 公式推奨。トークン最小化)
- plugin hook のため doc-researcher 未インストール環境では発火しない

### キャッシュ TTL 既定値統一

- 3 スクリプト全てに `--max-age` を統一実装 (既定: 604800 秒 = 7 日)
- `_common.py` に `DEFAULT_MAX_AGE_SECONDS` 定数 + `add_max_age_arg()` ヘルパー追加
- `parse-ai-sdk.py` / `parse-firebase.py` に `--max-age` CLI 引数を新規追加
- `parse-claude-docs.py` のローカル `_add_max_age_arg` を共通版に統一
- 強制 re-fetch: `--max-age 0`
- 3 SKILL.md に「キャッシュ期限切れ」行を追加

## [0.7.0] - 2026-05-23

### 3 script API 統一 (BREAKING)

claude-docs v3 (0.5.0) で実装した `search` 統合 + `--page-ref` + `--file` flag 化を
ai-sdk / firebase にも展開し、3 script で以下の 6 サブコマンドを共通化:

- `fetch-index` — 軽量 index 一覧
- `search-index` — title/description でランキング (候補だけ取得)
- `search-content` — 本文横断キーワード検索 (`--page-ref` で 1 ページに絞れる)
- `search` — 統合検索 (`search-index` + 本文 hits を 1 コマンドで返す、推奨入口)
- `sections` — 指定ページの見出し一覧
- `content` — ページ全体 / セクション本文

`<page_ref>` は 3 形式を受け付ける (ai-sdk は URL を持たないため int / title 部分一致のみ):

- 整数 index
- URL slug (last path component)
- 完全 URL

### `researching-ai-sdk` skill 3.2.0 (BREAKING)

- `<file>` positional 引数を全廃止 → `--file` flag 化 (省略時は cache を auto-fetch)
- `search-content` の `--doc-index` → `--page-ref` (int / title 部分一致)
- `sections <file> <doc_index>` → `sections <page_ref>`
- `content <file> <doc_index> "<heading_path>"` → `content <page_ref> "<heading_path>"`
- 旧 `search` (= `search-index` alias) を廃止し、**統合 search** に置き換え。
  top N (`--top-n`, default 5) 候補をスコアリングし、各 body を keyword 検索して
  heading_path + スニペットを返す
- SKILL.md を 2 段階フロー (`search` → `content`) に書き直し、v3.1.0 → v3.2.0 migration 例を追記

旧 → 新 置換例:

| v3.1.0 (旧) | v3.2.0 (新) |
|------------|------------|
| `search-index /tmp/ai-sdk-llms.txt "X"` | `search-index "X"` |
| `search-content /tmp/ai-sdk-llms.txt "X" --doc-index 42` | `search-content "X" --page-ref 42` |
| `sections /tmp/ai-sdk-llms.txt 42` | `sections 42` |
| `content /tmp/ai-sdk-llms.txt 42 "X"` | `content 42 "X"` |
| `search /tmp/ai-sdk-llms.txt "X"` (旧 alias) | `search "X"` (統合検索) |

### `researching-firebase` skill 2.0.0 (BREAKING)

- `sections <doc_index>` → `sections <page_ref>` (int / URL slug / 完全 URL)
- `content <doc_index>` → `content <page_ref>`
- `search-content --pages <idx,idx,...>` (REQUIRED 複数) → `--page-ref <ref>` (optional 単数)
- 新規 `search` 統合 (top N on-demand fetch + 本文 hits)。Firebase は llms-full.txt が
  ないため top N ページを順次 HTTP fetch するヒューリスティクス (初回のみ重い、cache hit 後は高速)
- 旧 `--pages` 廃止 → 複数ページ横断したいときは `search` 経由を使う
- SKILL.md を 2 段階フロー (`search` → `content`) に書き直し、v1.1.0 → v2.0.0 migration 例を追記

旧 → 新 置換例:

| v1.1.0 (旧) | v2.0.0 (新) |
|------------|------------|
| `search-content "X" --pages 42` | `search-content "X" --page-ref 42` |
| `search-content "X" --pages 42,43,44` | `search "X" --top-n 3` (search 経由が推奨) |
| (新規可能) | `sections vector-search` (URL slug で直接アクセス) |

### `researching-claude-docs` skill (変更なし)

claude-docs は 0.5.0 で既に新 API を実装済み。本 release で他 2 script が claude-docs と
揃ったため、3 script 共通の使い方が一貫した。

### scripts/_common.py

- 変更なし (既存の `score_entry` / `search_index_entries` / `search_content_in_body` /
  `normalize_doc_url` / `build_url_to_full_index` 等を ai-sdk / firebase の新規 `cmd_search`
  / `_resolve_page_ref` から再利用)

### 検証

- 3 script の `--help` および各サブコマンド の `--help` が argparse error なし
- `parse-ai-sdk.py search "streamText onFinish" --top-n 2` で top 2 候補 + 本文 hits が
  正常に取得できる (cache hit 時 ~1 秒)
- `parse-claude-docs.py search "hook" --index-limit 2` の既存挙動は維持 (regression なし)

### Fix

- `parse-firebase.py` の `_resolve_page_ref` で `page_ref` に完全 URL (e.g.
  `search-index` 出力の `URL:` 行をそのままコピペした `.md.txt` 付き URL)
  を渡したときに `No page found for URL` で fail するバグを修正。ユーザー
  入力側にも `_entry_url_for_match()` を適用して両側で `.md.txt` を剥がして
  から比較するようにした。SKILL.md / README で「完全 URL 受付」と謳って
  いるのと挙動を一致させる (Codex Review P2 feedback on PR #17)
- `parse-ai-sdk.py` の `_default_cache_path` / `parse-firebase.py` の
  `_index_cache_path` / `_pages_cache_dir` で `cache_dir.rstrip("/")` が
  `cache_dir="/"` を空文字列にしてしまい、`os.path.join("", filename)` が
  相対パスを返すバグを修正。`os.path.join` は trailing slash を自動処理する
  ため rstrip は元々不要 (Codex Review P3 feedback on PR #17)

### 設計判断記録 (subagent fork 維持)

3 SKILL の `context: fork + model: sonnet` 構成は **維持** する設計判断を明文化
(README.md の「設計判断」セクション参照)。spawn オーバーヘッドより context rot
回避と正確性を優先するため、軽量化方向 (fork 外し / 親 model 継承 / 軽量モード追加)
は採用しない。「軽い質問でも WebFetch に流れる」課題への対応は description 充実
(0.6.0) + doc-first rules (`~/.claude/rules/`) + `search` 統合 (本 release) の
3 系統で行う。

## [0.6.0] - 2026-05-23

### 3 SKILL description の統一: Triggers / Use proactively / WebFetch 優位文

- `researching-claude-docs` の frontmatter `description` に
  `Use proactively when ...` / `Use when implementing or debugging ... such as ...` /
  `Triggers: ...` 行を追加 (ai-sdk / firebase と同じパターンに揃える)。Triggers は
  "Claude Code", "hook schema", "subagent", "plugin manifest", "slash command",
  "settings.json", "permission", "MCP", "Anthropic API", "code.claude.com",
  "platform.claude.com", "Claude Code ドキュメント", "researching-claude-docs"
  の 13 個。これまで Triggers 列挙がなく ai-sdk / firebase と非対称だった状態を
  解消し、LLM 側の skill 候補マッチ率を底上げする
- 3 SKILL すべての `description` に「WebFetch ではなくこのスキルを使う（要約
  モデル経由ではないため field の抜け落とし・幻覚が起きない）」の優位文を
  統一文型で揃える。claude-docs に既存だったこの文型を ai-sdk / firebase の
  description にも追加し、3 スキル共通のパターンで LLM が「verbatim 取得が
  欲しい」「field 抜け落ちを避けたい」場面を引っかけられるようにする
- SKILL 本体 (markdown) / parse スクリプト I/F / キャッシュ動作の変更なし。
  description のみの非破壊変更

## [0.5.0] - 2026-05-14

### `researching-claude-docs` skill 3.0.0 (UX 改善 + 一部破壊的)

- **NEW** `search` subcommand: `llms.txt` のタイトル/説明ランキングと
  `llms-full.txt` の本文検索を **URL で join** して 1 コマンドで返す統合検索。
  返ってくる `doc_idx` は `content` / `sections` にそのまま渡せる
  (`search-index` / `search-content` 間の doc_idx 乖離問題を根本解決)
- **NEW** `--max-age` flag for `search` / `search-content` / `search-index` /
  `sections` / `content` / `fetch-index`: 既定では `/tmp/` キャッシュは無期限
  再利用、`--max-age N` (秒) を指定すれば期限切れで自動再 fetch
- **NEW** `--max-snippet-chars` flag for `search` / `search-content`:
  スニペット文字数上限 (既定 500 文字、`0` で無制限)
- **NEW** Changelog / Release notes ページの自動 deprioritize (`search` /
  `search-content`)。`--include-changelog-priority` で旧挙動に戻せる
- **BREAKING** `sections` / `content` / `search-content` の引数を再設計:
  - `file` positional 引数を廃止 → `--file` flag 化
  - `sections` / `content` の `doc_index` positional を `page_ref` に拡張、
    整数 / URL slug / 完全 URL を runtime で自動判別
  - `search-content` の `--doc-index` を `--page-ref` に改名 (slug/URL も受付)
  - 旧 `sections /tmp/claude-code-llms-full.txt 5` 形式は argparse error
  - 移行例: `sections 5` / `content hooks "Hook events/PreToolUse"` /
    `content https://code.claude.com/docs/en/hooks "..."`
- SKILL.md を全面書き直し: 推奨フローを 2 段階 (`search` → `content`) に簡略化、
  `page_ref` の 3 形式・slug 曖昧時の対処・v2 → v3 移行例を追記
- `next_hint` が `--source` を伝搬: `--source platform` で実行した時の follow-up
  ヒントが silently `code` (デフォルト) に落ちる事故を防止。デフォルトソース時
  はヒントを短く保つため省略
- `fetch-index` の `Next:` ヒントを v3 形式 (`sections <doc_index>`) に統一
  (旧 file positional 表記が残っていた点を修正)
- `--file` と `--source` の不整合検出: ユーザーが `--file /tmp/claude-platform-llms-full.txt`
  を渡したのに `--source` がデフォルト `code` だった場合などに、silent
  cross-population (platform 名のファイルに code docs を書き込む等) を防ぐため
  fetch 前に fail-fast。未知の `--file` (推測不能なパス) は従来通り通す
- `--include-changelog-priority` の挙動を `cmd_search` と `cmd_search_content`
  で揃える: フラグが ON のときペナルティ項だけを 0 にし、relevance ソート
  (`total_matches` 降順) は維持。以前は `search-content` 側でソート全体を
  skip していたため `--limit` が元の文書順で切られ、高 hit ページが落ちる
  リグレッションがあった
- `--file` 指定時のセマンティクスを **read-only** に変更: 既存 user ファイル
  を `--max-age` で silently 上書きする regression を排除。`--file` ありの時
  は (1) 既知 cache 名と `--source` の不整合を fetch 前に die (前出修正)、
  (2) ファイル不在も fetch せず die (`--file` を外して auto-fetch せよと案内)、
  (3) `--max-age` は無視。fetch-and-cache サイクルは `--file` を渡さない時
  にだけ走る。`--file` で渡したローカルスナップショットは絶対に上書きされない

### `_common.py` 共有ヘルパー強化

- `fetch_url` に `max_age` kwarg を追加 (既存呼び出しは backward compatible)
- `search_content_in_body` に `max_snippet_chars` kwarg を追加 (既存呼び出しは
  backward compatible)
- `normalize_doc_url` / `build_url_to_full_index` を追加 (`.md` suffix /
  trailing slash / query / fragment を剥がした正規化 URL で
  llms.txt ↔ llms-full.txt の 1:1 join を担保)

### 検証

- Claude Code llms.txt と llms-full.txt の **131 entries が 100% URL join**
  することを実機確認 (`.md` suffix strip で一致)
- `search "test"` は join 警告無しで動作

## [0.4.0] - 2026-04-15

- Add `search-index` subcommand to all three parse scripts. Replaces the
  Agent's previous habit of running `grep` over `llms.txt` to locate pages by
  keyword. Ranks pages against title / description (and tags / H1-H2 headings
  for AI SDK) with case-insensitive AND scoring. On `parse-ai-sdk.py`, the
  existing `search` subcommand is renamed and kept as an alias for backwards
  compatibility
- Add `search-content` subcommand to all three parse scripts. Performs
  section-level AND keyword search across `llms-full.txt` bodies (AI SDK /
  Claude) or lazily-fetched pages listed in `--pages` (Firebase, which has no
  `llms-full.txt`). Returns `heading_path`, a snippet with `→` markers on hit
  lines, matched keywords, per-section hit count, source URL, and a grand
  total so the Agent can jump straight to `content` without a follow-up grep
- Promote `search_index_entries` / `search_content_in_body` / `score_entry`
  to `_common.py` so all three sources share one search implementation.
  `parse-ai-sdk.py` drops its private `score_document` helper; the
  equivalent scoring weights (title 10/5, tags 4, description 2, headings 1,
  all-keyword bonus 10) now live in `score_entry`
- Section-level AND semantics: `search-content` requires every query keyword
  to appear somewhere within the same section before it's reported. Sections
  with 20+ hit lines are truncated to the first three with a trailing
  "… (N more hits in this section)" marker to keep output scannable
- Rewrite three SKILL.md files around the new entry points (search-index →
  sections/search-content → content), explicitly forbid the common
  grep/Read-lines fallbacks, and document the `llms.txt` / `llms-full.txt`
  `doc_index` divergence in Claude docs (search-content is the safe chain
  because its `doc_index` is the one sections/content use). Bump skill
  versions: ai-sdk 3.1.0 / claude-docs 2.1.0 / firebase 1.1.0
- Update README subcommand table, dev-test commands, and maintenance notes
  to reflect the new entry points and the AND semantics of `search-content`

## [0.3.0] - 2026-04-15

- Extract shared parser / fetch / output helpers into `scripts/_common.py`
  (`FenceTracker`, `extract_sections`, `extract_content`, `parse_llms_index`,
  `fetch_url`, `load_lines`, `die*` error helpers, `print_metadata_header`,
  `next_hint`, and argparse skeleton helpers). The three `parse-*.py` scripts
  are now thinner and consistent in behavior
- Fix `Next:` hint in `parse-ai-sdk.py` (3 call sites) and `parse-claude-docs.py`
  (2 call sites) which referenced the pre-rename script name
  (`parse-llms-txt.py`). Firebase was already correct; all three now derive the
  hint from `sys.argv[0]`
- No user-visible behavior change beyond the `Next:` hint fix; all other
  subcommand stdout/stderr is byte-identical to 0.2.0
- Unify 3 SKILL.md structure: add `context: fork` / `model: sonnet` frontmatter
  and "出力フォーマット" / "ルール" sections to `researching-claude-docs`;
  patch-bump SKILL versions (ai-sdk 3.0.1 / claude-docs 2.0.1 / firebase 1.0.1)
- Update README: Python requirement corrected to 3.10+ (parse-\*.py uses PEP 604
  syntax; only `_common.py` is 3.8+-compatible via `from __future__ import annotations`);
  add `scripts/_common.py` row to Components table and a paragraph on the shared
  helper layer to the maintenance notes
- Use `os.path.realpath(__file__)` (not `abspath`) when prepending the script
  directory to `sys.path` in the three `parse-*.py` scripts, so symlinked
  invocations cannot be shadowed by an unrelated `_common.py` sitting next
  to the symlink. Verified with an adversarial test (Codex Review P2 feedback
  on PR #3)
- Thread `min_level` through `_common.extract_content` (default 2) and have
  `parse-ai-sdk.py` pass `min_level=1` explicitly. The previous hardcoded
  `min_level=1` meant `cmd_sections` (H2+) and `cmd_content`'s internal
  heading lookup (H1+) disagreed in `parse-firebase.py`, which hands the raw
  page (H1 included) to `extract_content` — a Firebase page with an H1 and
  an H2 sharing the same title could have `content` match the H1 and return
  nearly the whole document instead of the intended H2 section. Claude docs
  and Firebase `content` output is now byte-identical to 0.2.0 again (Codex
  Review P2 feedback on PR #3 commit `e449a21`)

## [0.2.0] - 2026-04-15

- Add `researching-firebase` skill (Firebase docs progressive loader)
- Add `parse-firebase.py` script (per-page on-demand fetch; no llms-full.txt available)
- Use collision-resistant cache filenames (readable path + sha1 hash suffix) so
  Firebase URLs differing only by `/` vs `_` no longer share a cache file
- Update plugin description and keywords to include Firebase
- Update marketplace.json entry and root README

## [0.1.0] - 2026-04-14

- Initial release
- `researching-claude-docs` skill (Claude Code + Platform docs)
- `researching-ai-sdk` skill (Vercel AI SDK docs)
- Script paths updated to use `${CLAUDE_PLUGIN_ROOT}`
