# Changelog

All notable changes to this plugin will be documented here.

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
