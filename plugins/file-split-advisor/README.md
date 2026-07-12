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

1. **言語係数** — Python は密度が高いので閾値を下げ、Java/C#/Kotlin はボイラー
   プレートで長くなりやすいので閾値を上げる
2. **role 係数** — テストファイルは記述が単調に伸びやすいため閾値を 1.6 倍緩和
3. **宣言的コード緩和** — 制御フロー密度が低い (ルーティング定義・型定義・DTO
   等) ファイルは閾値をさらに 1.6 倍緩和
4. **構造シグナル** — import カテゴリの多様性・命名の抽象度・定義数過多・制御
   フロー密度の高さを検出し、行数が中庸 (note tier) でも責務混在が疑われる
   ファイルを拾い上げる

## 判定ロジックサマリ

### 行数 tier (半開区間、`ok < note <= review <= warn <= strong`)

基準値 (係数 1.0 の場合): `note=150 review=300 warn=500 strong=800`。

実効閾値 = 基準値 × 言語係数 × role 係数 × (宣言的なら 1.6 倍)。

| 言語 | 係数 | 言語 | 係数 |
|---|---|---|---|
| python | 0.7 | go | 1.0 |
| javascript / typescript | 1.0 | rust | 1.1 |
| javascriptreact / typescriptreact | 1.15 | ruby | 1.0 |
| java / csharp | 1.5 | php | 1.1 |
| kotlin | 1.4 | generic (未知拡張子) | 1.0 |
| dart | 1.3 | | |

role 係数: `test=1.6` / `normal=1.0`。宣言的緩和は `control_flow_density < 0.02`
のときに 1.6 倍。

### 構造シグナル

| シグナル | 条件 |
|---|---|
| import カテゴリ多様性 | network/db/ui/logging/testing/auth/filesystem の 7 カテゴリのうち 4 種以上を import |
| 命名が抽象的 | ファイル名の全トークンが `util/common/helper/service/manager` 等の総称語のみ |
| 定義数過多 | 関数/クラス定義が 20 以上 (テストファイルは評価しない) |
| 制御フロー密度高 | `if/for/while/switch/case/catch/except` を含む行が 25% 以上 (宣言的緩和が不適用の場合のみ) |

### emit するかどうか

- tier が `review` 以上 → 常に emit (シグナル数によらない。行数の大きさ自体を
  レビュー発火の十分条件として扱う設計)
- tier が `note` → 構造シグナルが 2 個以上のときのみ emit (行数は中庸だが責務
  混在が疑われるファイルを拾う)
- tier が `ok` → emit しない

## 通知の抑制 (debounce)

- **1 セッション内で 1 ファイル × 1 tier につき 1 回のみ** 通知 (ハイウォーター
  マーク方式。tier が悪化したときのみ再警告し、shrink→regrow で同一 tier に
  戻っても再警告しない)
- `FILE_SPLIT_ADVISOR_MAX_EMITS` (既定 20) — セッション内 emit 数の安全弁

## 環境変数

| 変数 | 既定値 | 意味 |
|---|---|---|
| `FILE_SPLIT_ADVISOR_DISABLED` | (未設定) | `1`/`true`/`yes`/`on` で hook を無効化 |
| `FILE_SPLIT_ADVISOR_MAX_EMITS` | `20` | セッション内の最大 emit 回数 |

## 早期 skip 対象

- lockfile (`package-lock.json` / `yarn.lock` / `pnpm-lock.yaml` / `Cargo.lock` /
  `Pipfile.lock` / `poetry.lock` / `go.sum` / `composer.lock`)
- minified (`*.min.js` / `*.min.css` / `*.map`)
- generated ファイル名パターン (`*.pb.go` / `*_pb2.py` / `*_pb2_grpc.py` /
  `*.g.dart` / `*.freezed.dart` / `*_generated.*`)
- ファイル先頭 5 行に `@generated` / `do not edit` 等の generated マーカーを
  含むファイル

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
- Java/C#/Kotlin は `def_count` シグナルがほぼ機能しない (メソッド宣言に
  `def`/`function`/`func` 等のキーワードを伴わないため)。これらの言語では行数
  (1.5x 係数) と import カテゴリ多様性・制御フロー密度が主戦力になる
- import カテゴリ分類はキーワード辞書によるヒューリスティックで、精密な import
  resolver ではない
- 閾値のローカル上書き機構 (`config.local.json` 等) は v1 に含まない

詳細な設計判断の経緯は [hooks/file-split-advisor/CLAUDE.md](./hooks/file-split-advisor/CLAUDE.md) 参照。

## テスト

```bash
cd hooks/file-split-advisor
python3 -m unittest discover tests
```

## ライセンス

MIT
