# Changelog

## 0.3.1

内部バックログの精査で見つかったテスト不足 2 件を修正。挙動変更は最小限
(非dict payload の防御ガード 1 箇所のみ)。

### `message.py` / `__main__.py` のテストを追加

- `message._format_signal` の 4 分岐 (import カテゴリ多様性・命名が抽象的・
  定義数・制御フロー密度) と、未知シグナルキーに対するフォールバック文言を
  直接固定するテストを追加
- `role` 引数が `"normal"` のとき role 注記 (`(test: 閾値 1.6倍)`) が
  出ないことを直接確認するテストを追加
- `FILE_SPLIT_ADVISOR_MAX_EMITS` の不正値 (`abc`)・`0`・負値・有効値超過時の
  抑制挙動を確認するテストを追加
- cwd 外ファイルのメモ見出しが絶対パス表示にフォールバックすることを
  `__main__` 経由の e2e で確認するテストを追加 (単体レベルでは
  `source.relative_to_cwd` を既にカバー済みだった)
- payload や `tool_input` が dict でない (list/str/number/null) ときに
  クラッシュしないことを確認するテストを追加。従来は `payload.get(...)` が
  `AttributeError` になり、fail-open ラッパー経由で exit 0 にはなるものの
  stderr に `fatal: 'list' object has no attribute 'get'` 等のログが出ていた。
  `__main__.py` に `isinstance(payload, dict)` の早期 return ガードを追加し、
  他の想定外 envelope と同じ「無出力で skip」に揃えた
- CRLF 改行・非UTF-8 (latin-1) バイト列を含むソースでも `source.load_text`
  がクラッシュせず行数が正しく数えられることを確認するテストを追加
- `subprocess.run([sys.executable, <pkg_dir>])` (README の手動スモークテスト
  手順と同じ起動形) で実プロセスとして起動し、正常系が exit 0・stdout に
  JSON・stderr 空になることと、非dict payload でも fatal ログが出ないことを
  確認する e2e テストを追加

### テストの単体指定ができない DX 課題を修正

`tests/` ディレクトリ配下の各テストファイルが行うトップレベル `import
_testutil` は、ディレクトリ探索 (`python3 -m unittest discover tests`) では
探索先自体が検索パスに入るため解決できていたが、クラス単位・メソッド単位の
単体指定 (`python3 -m unittest tests.test_x.Class.method` /
`pytest tests/test_x.py::Class::method`) では `tests/` ディレクトリ自体が
sys.path に入らず `ModuleNotFoundError` になっていた (pytest 経由では
スイート全体の collection が失敗する形でも顕在化していた)。`tests/__init__.py`
(既存は空ファイル) に `tests/` を自身の sys.path に足す処理を追加し、
呼び出し形式によらず解決されるようにした (呼び出し側の起動形は不変。同型の
課題を先に解決した `verify-cloud-account` と同じ解法)。README にクラス
単位・メソッド単位・pytest node id 指定の実行例を追記した。

## 0.3.0

内部バックログの精査で見つかった 6 件の不具合・改善をまとめてリリース。

### 設定手段を追加 (合わないプロジェクトで plugin 無効化しか選べない問題への対応)

- `FILE_SPLIT_ADVISOR_IGNORE` (カンマ区切り glob、fnmatch) と、永続設定として
  `~/.claude/file-split-advisor/ignore.local.txt` (1 行 1 glob、`#` コメント、
  空行無視のシンプルな gitignore 風フォーマット) を追加。両方を統合してファイル
  名・フルパスの両方に対して判定し、一致したファイルは判定対象から除外する
- `FILE_SPLIT_ADVISOR_SCALE` (全閾値への一律倍率、既定 `1.0`) を追加。0 以下・
  数値に変換できない値・`nan`/`inf` 等の非有限値は既定にフォールバックする

### 一時ディレクトリ配下・cwd 外ファイルの誤発火を修正

Claude が分析用ダンプや handoff メモ等の一時ファイルを scratchpad
(`$TMPDIR` 配下) に書く運用で、プロジェクト外のこれらのファイルにまで絶対パス
付きの分割助言 memo が emit されることを確認した。`$TMPDIR` / `/tmp` /
`/private/tmp` / `/var/folders` 配下のファイルを、`cwd` の内側にない限り常時
skip するようにした (opt-out 機構なし)。あわせて `FILE_SPLIT_ADVISOR_CWD_ONLY`
(既定 off) を追加し、有効化すると `cwd` 外のファイルを全般 skip できる
(`--add-dir` 運用を壊さないよう既定は off)。containment 判定は
`os.path.realpath()` で正規化した上で行う (macOS では `/tmp`/`/var` が
`/private/tmp`/`/private/var` への symlink であり、正規化しないと表記揺れで
「一時ファイルが skip されない」「cwd 内側のファイルが誤って skip される」の
両方向に壊れていた)。

### メモの根拠表示を正確にする

- シグナル 0 件のときに常に「宣言的なコードの可能性があります」と表示していた
  のを、実際に宣言的緩和 (`control_flow_density < 0.02`) が適用されたときだけ
  表示するよう修正 (制御フロー密度の高いファイルにも誤表示されていた)
- 実効閾値の根拠になった係数 (言語・宣言的緩和) を「言語 係数 (× 宣言的 係数)」
  の形でメモに明示するようにした (`judge.Verdict.applied_multipliers` を追加)
- メモの「目安」行数表示が review/warn 固定で、note/strong 判定時には無関係な
  数値だけが出ていたのを、判定 tier + 隣接 tier を動的に表示するよう修正
- `FILE_SPLIT_ADVISOR_SCALE` != 1.0 のとき、目安の倍率が printed 係数から
  導出できなかったのを修正。role_note と同じ形の専用表示
  (`(全体 2.0倍)`) を breakdown の直後に追記する (`judge.Verdict.scale` を追加。
  `applied_multipliers` には含めない設計判断は維持)

### stderr 汚染を修正

`count_defs_python` の `ast.parse` が、無効なエスケープシーケンス
(`"\d"` 等) を含む Python ソースに対して Python 3.12+ で `SyntaxWarning` を
毎回 stderr に出していた (実測: 260 行中 236 行に該当パターンを含むファイルで
約 76KB)。`warnings.catch_warnings()` で抑制した。

### レビューで見つかった追加の不具合を修正

上記の設定手段追加・temp-dir skip・メモ表示修正の初版に対するレビューで
見つかった不具合。

- **非UTF-8の `ignore.local.txt` で plugin が全プロジェクトで無言停止する
  不具合を修正**。`UnicodeDecodeError` は `OSError` のサブクラスではないため
  `except OSError` だけでは捕まらず、この設定ファイルが読めないと
  (`~/.claude/` 配下のユーザーグローバル設定のため) 全プロジェクトの
  全 Write/Edit で plugin が無言で死んでいた。`except (OSError,
  UnicodeDecodeError)` に修正
- **`FILE_SPLIT_ADVISOR_SCALE` に `nan`/`inf`/`1e400` を渡すと plugin が
  無言で無効化される不具合を修正**。これらは `float()` 変換に成功し
  `value <= 0` も素通りするため、非有限な倍率が実効閾値に乗算されて
  全ファイルが判定不能になっていた。`math.isfinite()` によるチェックを追加
- **`__main__.py` の表示パス相対化が macOS の `/tmp`/`/var` symlink による
  表記揺れで絶対パス表示にフォールバックする問題を修正** (実害は表示のみ)。
  temp-dir skip と同じ realpath 正規化 (`source.relative_to_cwd`) を使うよう
  に統一した
- **テストが `FILE_SPLIT_ADVISOR_*` の環境変数 5 つを遮断していなかったのを
  修正**。これらを export した端末/CI ではテストの green の意味が変わって
  いた
- メモの「検出された構造シグナル: なし」表示から、将来 `judge` 側の
  thresholds が部分的になったときの防御ガードが抜けていたのを復元
  (現状は到達不能だが fail-open で表示ごと消えるのを防ぐ)
- 除外 glob (`FILE_SPLIT_ADVISOR_IGNORE` / `ignore.local.txt`) の fnmatch が
  完全一致 (anchored) であり、相対パス形のパターン (`migrations/*`) が
  絶対パスに決してマッチしないことを README に明記 (`*/migrations/*` の形が
  必要)
- `$TMPDIR` を深さ・cwd との関係を検証せず root として無条件採用する既知の
  限界を README に追記 (稀な設定ミス時にのみ顕在化するため、既定の skip
  判定の入力自体を変える対応は見送った)

### 2巡目のレビューで見つかった追加の不具合を修正

- **`FILE_SPLIT_ADVISOR_SCALE` に `1e308` のような巨大だが有限な値を渡すと
  plugin が無言で無効化される不具合を修正**。前項の `math.isfinite()` に
  よる非有限値チェックは通過するが、言語/role/宣言的緩和の係数と掛け合わ
  せた実効閾値が `float` の表現範囲を超えて `inf` に飽和し、同じ「無言の
  無効化」が起きていた。既知の最悪ケース係数で判定する
  `judge.is_scale_safe()` を追加し、同じフォールバック経路 (既定 1.0) に
  載せた
- **倍率の表示が固定小数点2桁への丸めで実設定を誤って伝える不具合を修正**。
  `0.004` は `0.0倍`、`1.004` は中立値と同じ `1.0倍` と表示されており、
  「表示された係数から実効閾値を導出できる」という主張が壊れていた。
  `str()`/`repr()` の最短往復表現を使うよう修正し、丸めによる情報損失が
  原理的に起きないようにした
- **メモの「目安」行数表示が `round()` の偶数丸めで実際の境界と1ずれる
  不具合を修正**。判定は `line_count >= threshold` の半開区間で行数は
  整数なので、最初に到達する行数は常に `ceil(threshold)`。`round()` は
  閾値がちょうど `.5` のとき (例: 基準 150 に javascriptreact の言語係数
  1.15 を掛けた 172.5) 偶数丸めで 1 小さい値を表示していた。倍率機能に
  限らず、role 係数や言語係数の掛け算が二進浮動小数点の丸め誤差で整数から
  わずかにずれる既定経路 (SCALE 未設定) でも同じ不正確さがあったため、
  あわせて修正した

### ドキュメント修正

- README に Python 3.11+ の互換性表記を追加
- README に test 判定パターン (ディレクトリ名/ファイル名) の一覧を追加
- 「Kotlin はメソッド宣言にキーワードを伴わないため `def_count` が機能しない」
  という誤った記述を訂正。Kotlin は `fun` キーワードを伴うが、判定の正規表現が
  認識するのは Go 想定の `func` のみで一致しないだけであり、Rust の `fn` も
  同様に拾えないことを明記した

## 0.2.2

0.2.1 で入れたファイル末尾の補正が、**末尾以外を置換した編集にも誤ってかかって
いた**のを修正。`new_string` と同じ文字列が結果の末尾にも並ぶ場合、置換箇所が
末尾だと誤認して行数の増加を打ち消し、通知が消えていた。

例: `"X\nsomething\nfoo\n"` の先頭 `X` を `"foo\n"` に置換すると 3 行 → 4 行に
増えるが、結果 `"foo\n\nsomething\nfoo\n"` の末尾にも元からの `"foo\n"` がある。

補正は「置換が末尾で起きたと確定できるとき」— `new_string` が編集後の全文に
ちょうど 1 箇所しか現れず、それが末尾にあるとき — に限定した。確定できない
場合は改行数の差による近似に戻る。

## 0.2.1

0.2.0 のレビューで見つかった成長判定の取りこぼし 2 件を修正。

- **縮んで `ok` tier まで戻ったファイルの行数記録が更新されず、その後の再成長を
  誤って抑制していた**。900 行を記録 → 100 行に縮む → 600 行に成長、という順で
  600 が 900 と比較され通知されなかった。tier が `ok` でも行数だけは記録する
- **ファイル末尾の置換で、改行数の差と実際の行数の差が 1 ずれていた**。行数は
  `splitlines()` で数えるため「改行で終わらない最終行」が 1 行として数えられる
  が、改行の個数はこれを含まない。末尾に改行なしで 1 行足す編集
  (`"foo\n"` → `"foo\nbar"`) が「増えていない」と判定されて通知が消え、末尾に
  改行だけを足す整形 (`"foo"` → `"foo\n"`) が「増えた」と判定されて不要な通知が
  出ていた。編集後の全文を使って末尾の置換だけ補正する

## 0.2.0

通知の発火率を下げる 3 つの変更。0.1.0 は「触っただけの巨大ファイル」「Markdown
や JSON」「通常サイズの実装ファイル」にまで通知が出て、無効化したくなる水準の
ノイズになっていた。

### 判定対象を実コードの拡張子に限定した

`language.py::EXTENSION_LANGUAGE` を判定対象の allowlist として使うようにし、
未登録の拡張子と拡張子なしのファイルは読み込む前に skip する。Markdown / JSON /
YAML / TOML / CSV / SQL / テキスト等が行数だけで分割検討を促されることがなくなる。

allowlist 化で実コードが判定対象から外れないよう、これまで `generic` (係数 1.0)
として判定していた言語を明示登録した: Swift / C / C++ / Objective-C / Scala /
Elixir / Shell / Lua / Perl / R / Vue / Svelte / Groovy / PowerShell / Clojure /
Haskell / Erlang / Julia / Zig / Nim。このうち Java / C# / Kotlin と同じく宣言・
ヘッダのボイラープレートで行数が伸びるもの (Swift 1.2 / C 1.2 / C++ 1.3 /
Objective-C 1.4 / PowerShell 1.2) と、JSX/TSX と同型のコンポーネントファイル
(Vue / Svelte 1.15) にだけ係数を設定し、残りは従来と同じ 1.0 に据え置いた。

**拡張子を持たない shebang スクリプトも判定対象外になる** (従来は `generic` と
して判定していた)。内容を読む前の名前だけの判定に閉じているための制約で、
shebang を見る改善は別途の課題として残している。

### review tier に構造シグナルを 1 個以上要求するようにした

0.1.0 は `review` 以上を構造シグナルの有無によらず emit していた。「大きさその
ものをレビュー発火の十分条件として扱う」設計は `warn` 以上に引き上げ、`review`
は「大きく、かつ責務混在の兆候が 1 つ以上ある」ときに限定する。`note` の
「シグナル 2 個以上」は従来どおり。

あわせて **Python の言語係数を 0.7 → 1.0** に見直した。0.7 では review 閾値が
210 行となり、pylint の `too-many-lines` (既定 1000) や ESLint の `max-lines`
(既定 300) と比べて突出して厳しく、通常の実装ファイルが初回編集で発火する主因に
なっていた。

### ファイルを大きくしない編集では通知しなくなった

`Edit` の `old_string` / `new_string` の行数差が 0 以下 (typo 修正・同じ行数の
リファクタ・行を削る編集) なら通知しない。`replace_all` は考慮不要 — 置換 1 箇所
あたりの差分が同符号で N 箇所に適用されるため、合計の符号は変わらない。

`Write` は編集前の内容が hook に渡らないため、同一セッション内に直近の行数記録が
あればそれと比較し、縮んだ/変わらなければ抑制する。記録が無い初回は従来どおり
通知する。

このため state (`$TMPDIR/file-split-advisor/<hash>.json`) のパスごとの記録が
tier 文字列から `{"tier": ..., "lines": ...}` に変わった。0.1.0 が書いた形式も
引き続き読める。**行数は通知の有無に関わらず記録するが、tier は通知したときだけ
進める** — 通知せずに tier を進めると、その後に本当に成長したときも同一 tier と
みなされて恒久的に抑制されてしまうため。

### 実測 (この repo の全 tracked file, 312 件)

セッション内で各ファイルを初めて編集したときに通知が出るかを、変更前後の実コード
で判定した結果:

| | 判定対象 | 通知 | うちシグナル 0 個 |
|---|---|---|---|
| 0.1.0 | 312 | **63** (20.2%) | 55 |
| 0.2.0 | 243 (69 件は非コードで skip) | **18** (7.4%) | 10 |

通知が消えたファイル 45 件、増えたファイル 0 件。残る 18 件はいずれも 368〜2836
行で、うち 6 件は大きなテストファイル (テスト向けの閾値緩和をさらに見直すかは
別途の課題)。

この測定は上記 3 変更のうち前 2 つの効果のみを表す。3 つ目 (成長判定) は編集内容に
依存するためファイル単位の走査には現れない。

## 0.1.0

初版リリース。

`PostToolUse(Write|Edit)` で発火し、行数 tier (言語係数 × role 係数 × 宣言的
緩和で調整) と構造シグナル (import カテゴリ多様性・命名の抽象度・定義数過多・
制御フロー密度) を組み合わせて、ファイル分割検討を促す advisory メモを
`additionalContext` で返す。block/deny は一切しない。

- 静的解析のみ (v1 スコープ、git 履歴ベースのシグナルは含まない)
- セッション内で「1 ファイル × 1 tier につき 1 回」の debounce (tier 悪化時のみ
  再警告、ハイウォーターマーク方式)
- `FILE_SPLIT_ADVISOR_DISABLED` で無効化、`FILE_SPLIT_ADVISOR_MAX_EMITS`
  (既定 20) でセッション内 emit 数の安全弁
- lockfile / minified / generated ファイルは早期 skip
