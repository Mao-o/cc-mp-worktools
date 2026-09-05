# file-split-advisor

`Write` / `Edit` の直後に、行数と責務混在の構造シグナルを組み合わせてファイル
分割の検討を促す advisory メモを返す hook。**block/deny は一切しない** —
`sensitive-files-guardrail` の "guardrail" (block する) と対比した "advisor"
(判断材料を提示するだけ) という命名。

## 同梱 hook

| Hook | 発火イベント | 役割 |
|---|---|---|
| `file-split-advisor` | `PostToolUse(Write\|Edit)` | 行数 tier + 構造シグナルを判定し、閾値超過時に `additionalContext` で分割検討メモを注入 |

## インストール

```bash
/plugin marketplace add Mao-o/cc-mp-worktools
/plugin install file-split-advisor@mao-worktools
```

## なぜ行数だけで判定しないか

言語ごとの記述密度やファイルの役割 (ロジック/宣言的/型定義/生成コード/テスト)
によって「適正な長さ」は変わる。単純な行数一律基準は誤検出が多いため、以下を
組み合わせて判定する:

1. **言語係数** — Java/C#/Kotlin/Swift/C++ 等はボイラープレートで長くなりやすい
   ので閾値を上げる
2. **role 係数** — テストファイルは記述が単調に伸びやすいため閾値を 1.6 倍緩和
3. **宣言的コード緩和** — 制御フロー密度が低い (ルーティング定義・型定義・DTO
   等) ファイルは閾値をさらに 1.6 倍緩和
4. **構造シグナル** — import カテゴリの多様性・命名の抽象度・定義数過多・制御
   フロー密度の高さを検出し、行数だけでは判断できない責務混在を拾い上げる

さらに、**判定対象は実コードの拡張子に限定**し、**ファイルを大きくしない編集
(typo 修正など) では通知しない**。

## 判定ロジックサマリ

### 行数 tier (半開区間、`ok < note <= review <= warn <= strong`)

基準値 (係数 1.0 の場合): `note=150 review=300 warn=500 strong=800`。

実効閾値 = 基準値 × 言語係数 × role 係数 × (宣言的なら 1.6 倍) × 全体倍率
(`FILE_SPLIT_ADVISOR_SCALE`、既定 1.0)。

| 言語 | 係数 | 言語 | 係数 |
|---|---|---|---|
| java / csharp | 1.5 | dart | 1.3 |
| objectivec | 1.4 | cpp | 1.3 |
| kotlin | 1.4 | swift / c / powershell | 1.2 |
| javascriptreact / typescriptreact | 1.15 | rust / php | 1.1 |
| vue / svelte | 1.15 | 上記以外 | 1.0 |

係数 1.0 の言語: python / javascript / typescript / go / ruby / scala / elixir /
shell / lua / perl / r / groovy / clojure / haskell / erlang / julia / zig / nim。

role 係数: `test=1.6` / `normal=1.0`。宣言的緩和は `control_flow_density < 0.02`
のときに 1.6 倍。

### test 判定

以下のいずれかに一致すると role が `test` になり、role 係数 (1.6倍) が適用され
`def_count` シグナルの評価対象からも除外される。

- **ディレクトリ名** (パス中のいずれかの階層、大文字小文字を無視):
  `test` / `tests` / `__tests__` / `spec` / `specs` / `e2e`
- **ファイル名パターン**:

  | パターン | 例 |
  |---|---|
  | `test_*.py` | `test_foo.py` |
  | `*_test.py` | `foo_test.py` |
  | `*.test.ts` / `*.test.tsx` | `foo.test.tsx` |
  | `*.spec.ts` / `*.spec.tsx` | `foo.spec.tsx` |
  | `*Test.java` | `FooTest.java` |
  | `*_test.go` | `foo_test.go` |

### 構造シグナル

| シグナル | 条件 |
|---|---|
| import カテゴリ多様性 | network/db/ui/logging/testing/auth/filesystem の 7 カテゴリのうち 4 種以上を import |
| 命名が抽象的 | ファイル名の全トークンが `util/common/helper/service/manager` 等の総称語のみ |
| 定義数過多 | 関数/クラス定義が 20 以上 (テストファイルは評価しない) |
| 制御フロー密度高 | `if/for/while/switch/case/catch/except` を含む行が 25% 以上 (宣言的緩和が不適用の場合のみ) |

### emit するかどうか

- tier が `warn` 以上 → 常に emit (シグナル数によらない。この大きさになれば
  行数そのものをレビュー発火の十分条件として扱う)
- tier が `review` → 構造シグナルが 1 個以上のときのみ emit
- tier が `note` → 構造シグナルが 2 個以上のときのみ emit (行数は中庸だが責務
  混在が疑われるファイルを拾う)
- tier が `ok` → emit しない

## 通知の抑制 (debounce)

- **ファイルを大きくしない編集では通知しない**
  - `Edit` は `old_string` / `new_string` の行数差で判定する。差が 0 以下
    (typo 修正・同じ行数のリファクタ・行を削る編集) なら通知しない。
    `replace_all` でも 1 箇所あたりの差分の符号は変わらないため考慮不要
  - `Write` は編集前の内容が hook に渡らないため、同一セッション内に直近の
    行数記録があればそれと比較する。記録が無い初回は通知する
- **1 セッション内で 1 ファイル × 1 tier につき 1 回のみ** 通知 (ハイウォーター
  マーク方式。tier が悪化したときのみ再警告し、shrink→regrow で同一 tier に
  戻っても再警告しない)
- `FILE_SPLIT_ADVISOR_MAX_EMITS` (既定 20) — セッション内 emit 数の安全弁

## 環境変数

| 変数 | 既定値 | 意味 |
|---|---|---|
| `FILE_SPLIT_ADVISOR_DISABLED` | (未設定) | `1`/`true`/`yes`/`on` で hook を無効化 |
| `FILE_SPLIT_ADVISOR_MAX_EMITS` | `20` | セッション内の最大 emit 回数 |
| `FILE_SPLIT_ADVISOR_CWD_ONLY` | (未設定) | `1`/`true`/`yes`/`on` で `cwd` 外のファイルを skip する。既定 off — `--add-dir` で cwd 外を正当に編集する運用を壊さないための opt-in |
| `FILE_SPLIT_ADVISOR_IGNORE` | (未設定) | 判定対象から除外する glob (カンマ区切り、fnmatch)。ファイル名・フルパスの両方に対して判定する |
| `FILE_SPLIT_ADVISOR_SCALE` | `1.0` | 全閾値 (note/review/warn/strong) に一律で掛ける倍率。0 以下・数値に変換できない値・`nan`/`inf` 等の非有限値に加え、他の係数 (言語/role/宣言的緩和) と組み合わせた結果が非有限になるほど巨大な値 (例: `1e308`) も既定 (1.0) にフォールバックする。メモの目安に表示される倍率 (`(全体 N倍)`) は設定値をそのまま反映し、固定桁数への丸めは行わない (`0.004` は `0.004倍`、`1.004` は `1.004倍` と表示され、いずれも中立値 `1.0倍` と区別できる) |

`FILE_SPLIT_ADVISOR_IGNORE` に加え、`~/.claude/file-split-advisor/ignore.local.txt`
(1 行 1 glob、`#` 始まりはコメント、空行は無視) があれば読み込んで併用する
(gitignore の否定 `!` 等の完全な構文には対応しない、素朴な fnmatch のみ)。
ファイルが存在しない/読めない場合 (非UTF-8 を含む) は無視する (fail-open)。

**glob は fnmatch ベースの完全一致 (anchored) で、ファイル名または
フルパス (絶対パス) のいずれかに対して判定する。** gitignore 風に見えても
相対パス形のパターン (例: `migrations/*`) は絶対パスの途中にしか現れない
文字列には決してマッチしない。ディレクトリを対象にしたい場合は
`*/migrations/*` のように先頭に `*` を置く。

## 早期 skip 対象

- lockfile (`package-lock.json` / `yarn.lock` / `pnpm-lock.yaml` / `Cargo.lock` /
  `Pipfile.lock` / `poetry.lock` / `go.sum` / `composer.lock`)
- minified (`*.min.js` / `*.min.css` / `*.map`)
- generated ファイル名パターン (`*.pb.go` / `*_pb2.py` / `*_pb2_grpc.py` /
  `*.g.dart` / `*.freezed.dart` / `*_generated.*`)
- **上の言語係数表に載っていない拡張子のファイル全般** — Markdown / JSON / YAML /
  TOML / CSV / XML / SVG / HTML / SQL / notebook / プレーンテキスト等。行数だけで
  分割検討を促しても有用でないため判定対象にしない
- **拡張子を持たないファイル** — shebang 付きスクリプトを含む。内容を読む前の
  名前だけの判定に閉じているための制約
- ファイル先頭 5 行に `@generated` / `do not edit` 等の generated マーカーを
  含むファイル
- symlink / FIFO 等の非通常ファイル、2MB 超、20,000 行超のファイル
- **一時ディレクトリ配下のファイル** (`$TMPDIR` / `/tmp` / `/private/tmp` /
  `/var/folders` 配下)。ただし対象ファイルが `cwd` の内側にあるとき (session
  全体がその場限りの一時プロジェクトである場合を含む) は対象にする。Claude が
  分析用ダンプ・handoff メモ等の一時ファイルを scratchpad に書く運用があり、
  プロジェクト外のファイルにまで分割助言が注入されるのを防ぐ (opt-out 機構なし)
- `FILE_SPLIT_ADVISOR_IGNORE` / `ignore.local.txt` に一致するファイル (上記
  「環境変数」参照)

## 設計原則

1. **block しない** — advisory メモのみ。判断はモデル/ユーザーに委ねる
2. **fail-open** — 何が起きても exit 0。判定不能・IO 失敗はすべて「通知しない」
   側に倒す (advisory hook に fail-closed は不要)
3. **透明性** — 行数のみが emit 根拠 (構造シグナルなし) のときは、その旨をメモに
   明記する
4. **純粋関数と I/O の分離** — `language.py` / `metrics.py` / `judge.py` は
   ファイルシステムアクセスを持たない。I/O は `source.py` (読み込み) と
   `state.py` (debounce) に閉じ込める

## 既知の限界 (v1)

- **静的解析のみ**。著者数・コミット頻度などの git 履歴ベースのシグナルは
  含まない
- `line_count` が `note` 閾値未満のファイルは、構造シグナルが何個点火していても
  emit しない (小さいが責務混在したファイルの検出は範囲外)
- 拡張子を持たないスクリプトは shebang を見ずに skip する
- `Write` が既存ファイルを縮めたかどうかは、同一セッション内に直近の行数記録が
  あるときしか分からない (記録が無い初回は通知する)
- `def_count` は行頭キーワード正規表現 (`def`/`class`/`function`/`func`/
  `interface`/`struct`/`enum`) にマッチする行を数える (Python のみ AST で厳密に
  カウントし、構文エラー時だけこの正規表現にフォールバックする)。関数/メソッド
  宣言がこれらのキーワードで始まらない言語では機能しない: Java/C# はアクセス
  修飾子や戻り値型から始まるためほぼ機能しない。**Kotlin はメソッド宣言に
  `fun` キーワードを伴うが、正規表現が認識するのは Go 想定の `func` のみで
  `fun` とは一致しないため、同様にほぼ機能しない** (「キーワードを伴わない」の
  ではなく「未対応のキーワード」)。Rust の関数宣言 (`fn`) も同じ理由で拾えない
  (Rust の `struct`/`enum` 宣言は対応するキーワードに一致するため数えられる)。
  これらの言語では行数 (Java/C# 1.5x, Kotlin 1.4x 係数) と import カテゴリ
  多様性・制御フロー密度が主戦力になる
- import カテゴリ分類はキーワード辞書によるヒューリスティックで、精密な import
  resolver ではない
- 閾値の詳細な上書き (tier ごと・言語ごとの個別設定) はできない。
  `FILE_SPLIT_ADVISOR_SCALE` は全閾値に一律の倍率をかけるのみで、
  `config.local.json` 的なきめ細かい上書き機構ではない
- **`$TMPDIR` は無検証で一時ディレクトリの root として取り込む**。極端に浅い値
  (`/` や `/Users` 等) や、実際にはプロジェクトの祖先ディレクトリにあたる値
  (`/Users/you/dev` 等) が設定されていると、無関係な兄弟ディレクトリのファイル
  まで「一時領域」扱いになり、`cwd` の外にある限り skip されてしまう
  (通常 `$TMPDIR` は OS が管理する専用パスであり、この状況は稀)

詳細な設計判断の経緯は [hooks/file-split-advisor/CLAUDE.md](./hooks/file-split-advisor/CLAUDE.md) 参照。

## 依存関係

標準ライブラリのみ。`pip install` 不要。Python 3.11+ を想定する。

## テスト

```bash
cd hooks/file-split-advisor
python3 -m unittest discover tests
```

クラス単位・メソッド単位で 1 件だけ指定して実行することもできる:

```bash
python3 -m unittest tests.test_message.TestFormatMultiplier.test_integral_value_gets_one_decimal -v
python3 -m unittest tests.test_judge.TestTierBoundaries -v
# pytest がインストールされていれば node id 指定も可能
pytest tests/test_message.py::TestFormatMultiplier::test_integral_value_gets_one_decimal -q
```

## ライセンス

MIT
