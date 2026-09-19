# file-split-advisor (実装者向けガイド)

このファイルは **plugin の保守・拡張者向け**。利用者向け概要は
[../../README.md](../../README.md)。

## 目的と非目的

### 目的

1. `Write` / `Edit` 直後に、行数と構造シグナルを組み合わせてファイル分割の
   検討を促す advisory メモを `additionalContext` で返す
2. 言語ごとの記述密度・ファイルの役割 (ロジック/宣言的/型定義/生成コード/
   テスト) によって適正な長さが変わることを閾値調整で反映する
3. 同一セッション内で同一ファイル×同一 tier への再警告を避け、通知疲れを防ぐ

### 非目的

- **block/deny はしない**。判断材料の提示のみ (advisor であり guardrail では
  ない)
- **git 履歴ベースのシグナル (著者数・コミット頻度) は v1 に含まない**。静的
  解析のみ
- **`line_count` が `note` 閾値未満のファイルの責務混在検出は範囲外**。構造
  シグナルが何個点火していても、行数が小さければ emit しない
- **`config.local.json` 的な、tier ごと・言語ごとの個別閾値上書きは 0.3.0 でも
  含まない**。0.3.0 で `FILE_SPLIT_ADVISOR_IGNORE`/`ignore.local.txt`
  (path-ignore) と `FILE_SPLIT_ADVISOR_SCALE` (全閾値一律倍率) を追加した
  (詳細は「拡張ポイント」節参照) が、これは「対象を除外する」「感度を一律に
  調整する」の 2 手段であり、tier/言語単位の individual な閾値上書きではない

## ディレクトリ構成

```
file-split-advisor/
├── .claude-plugin/plugin.json
├── README.md                       利用者向け概要
├── CHANGELOG.md
└── hooks/
    ├── hooks.json                  PostToolUse, matcher: "Write|Edit"
    └── file-split-advisor/
        ├── __main__.py             エントリポイント、パイプライン統括、fail-open
        ├── CLAUDE.md                本ファイル
        ├── source.py                 I/O 境界: パス解決・早期 skip・安全な読み込み
        ├── language.py                純粋関数: 拡張子 allowlist・言語判定・test判定・generated判定・vague filename
        ├── metrics.py                  純粋関数: テキスト → 数値メトリクス
        ├── judge.py                     純粋関数: 閾値テーブル・tier/emit 判定
        ├── change.py                     純粋関数: tool_input → 編集がファイルを大きくしたか
        ├── state.py                       唯一の I/O 副作用: session_id ベース debounce store
        ├── message.py                      additionalContext 文面組み立て
        └── tests/
```

`source.py` を独立させている理由: `language.py`/`metrics.py`/`judge.py` を
純粋関数のまま保ちモックなしでテストできるようにするため
(`session-facts` の `core/fs.py` と `collectors/*.py` の分離、
`redact-sensitive-reads` の `core/safepath.py` と `redaction/*.py` の分離と
同じ発想)。

## 判定パイプライン

```mermaid
flowchart TD
    A[stdin JSON] --> B{tool_name が<br/>Write/Edit?}
    B -- no --> Z[return]
    B -- yes --> C{FILE_SPLIT_ADVISOR_DISABLED?}
    C -- yes --> Z
    C -- no --> D[source.resolve_path]
    D --> T{should_skip_temp_dir?<br/>一時領域配下 かつ cwd の外}
    T -- yes --> Z
    T -- no --> W{CWD_ONLY かつ<br/>is_outside_cwd?}
    W -- yes --> Z
    W -- no --> IG{IGNORE glob /<br/>ignore.local.txt に一致?}
    IG -- yes --> Z
    IG -- no --> E{should_skip_by_name?<br/>lockfile/minified/generated/第三者ディレクトリ}
    E -- yes --> Z
    E -- no --> N{language.is_code_path?<br/>拡張子 allowlist}
    N -- yes --> F[source.load_text<br/>symlink/2MB/20000行の安全弁]
    N -- no --> SB{拡張子なし かつ<br/>dotfile でない?}
    SB -- no --> Z
    SB -- yes --> F
    F -- None --> Z
    F -- LoadedFile --> G{先頭20行に<br/>generated marker?}
    G -- yes --> Z
    G -- no --> SH{拡張子なし かつ<br/>shebang が未知?}
    SH -- yes --> Z
    SH -- no --> H[language 判定<br/>detect_language / is_test_path]
    H --> I[metrics.compute<br/>line_count/def_count/import多様性/制御フロー密度/vague filename]
    I --> J[judge.judge<br/>effective_thresholds (SCALE 反映) → tier → signals → should_emit]
    J --> P[change.classify_growth<br/>Edit の行数差 → grew/not_grew/unknown]
    P --> K[state.try_reserve_emit<br/>行数記録 + 成長判定 + debounce + emit上限を単一ロック区間で]
    K -- False --> Z
    K -- True --> L[message.build]
    L --> M[additionalContext を stdout に JSON dump]
```

**tier が `ok` でも `try_reserve_emit` を呼ぶ**。judge の結果で打ち切ると、
縮んで `ok` に戻ったファイルの行数記録が古いまま残り、その後の再成長が
「記録より小さい」と誤判定されて抑制される (900 行を記録 → 100 行に縮む →
600 行に成長、で 600 < 900 とみなされる)。`emit_candidate=False` を渡せば
記録だけ行って False が返る。

## 判定ロジックの設計判断

### role=test の緩和を 1 本化した (0.6.0) — 唯一の判定変更

`ROLE_MULTIPLIER["test"]` を 1.6 → **2.5** に引き上げ、あわせて **role=test に
宣言的緩和を重ねない**ようにした。複数プロジェクトから集めた約 1 万ファイル
(10,097 件) のコーパスで前後比較した結果に基づく:

| 案 | test の emit | 全体の emit |
|---|---|---|
| 0.5.0 (role 1.6 × declarative 1.6) | 40 | 446 |
| 重ね掛け廃止のみ | 79 | 485 |
| 係数 2.5 のみ | 17 | 423 |
| **2.5 + 重ね掛け廃止 (採用)** | **28** | **434** |
| 3.0 + 重ね掛け廃止 (不採用) | 20 | 426 |

- **重ね掛け廃止を単独で入れると逆効果**。test の 67% は宣言的緩和も受けて
  おり、これを外すだけだと閾値が下がって emit が倍増する。係数の引き上げと
  セットでしか成立しない
- **係数 3.0 も測った**。2.5 と emit 集合が完全に一致するのは「係数のみ」
  同士の比較 (どちらも 423 件) で、**採用形 (重ね掛け廃止と併用) では 3.0 の
  ほうが 8 件少ない** (434 → 426 / test 28 → 20。差分は 1,262〜1,421 行の
  test ファイル)。効果が穏やかな 2.5 を採る
- 不採用にした案: `review` tier の抑制 (−1 件で効果なし) /
  `fixtures`・`__mocks__` 等のディレクトリ skip (±0 件、対象 9 件はすべて
  `ok` tier で元々 emit していない)
- 実装は `max(role, declarative)` ではなく「**test なら declarative を掛け
  ない**」と書く。現行の係数では全件で同値だが、将来 test 係数を
  `DECLARATIVE_RELAXATION` (1.6) 未満に下げたときに max だと意味が変わる

残る test の emit は概ね 1,250 行超 (python の warn = 500 × 2.5)。

### 構造シグナルの閾値は据え置いた (0.6.0) — 測ったうえで動かさない判断

`IMPORT_DIVERSITY_SIGNAL_THRESHOLD` / `HIGH_DENSITY_SIGNAL_THRESHOLD` /
`DEF_COUNT_SIGNAL_THRESHOLD` / `NOTE_PROMOTION_SIGNAL_COUNT` は**変えていない**。
同じ 10,097 件のコーパスでの実測:

| 指標 | 実測 |
|---|---|
| import カテゴリ多様性の点火 | 2 件 |
| 制御フロー密度の点火 | 96 件 |
| 定義数過多の点火 | 287 件 |
| 命名が抽象的の点火 | 176 件 |
| emit のうちシグナル 0 個 | 38% (0.5.0: 171/446) / 37% (0.6.0: 160/434) |
| `note` tier のうち emit | 2 件 (0.5.0: 1,304 件中 / 0.6.0: 1,257 件中) |

閾値を緩める案 (import 4→3 / 密度 0.25→0.20 / `note` 昇格を 1 個に / 全部) を
それぞれ測ったが、**どれもシグナル 0 個の emit を減らさず** (171→164 件)、
新規 emit 16 件を目視した内訳は 有用 4 / 部分的 3 / ノイズ寄り 9 だった。
「行数カウンタになっている」という問題は閾値調整では解けない。

**再検討するなら、閾値より先に抽出側を直す**:

1. 型宣言だけのファイル (TS の interface 列挙等) で `def_count` が点火する
2. `testing` / `logging` のような横断的カテゴリが import 多様性を水増しする
   (テストファイルが `testing` カテゴリを立てて +1 される等)
3. import 語の誤分類 (`http` のようにモジュール名でない字面での一致)

### 「行数だけで emit」は warn 以上に限定する (0.2.0 で変更)

0.1.0 は tier が `review`/`warn`/`strong` なら構造シグナルの有無を問わず emit
していた。ユーザー提示の参考資料が「300 行超はレビューを促す」「500-800 行は
分割候補」「800 行超は設計再確認」と大きさそのものを発火条件として扱っている
ことに整合させた設計判断だったが、**実測すると通知の大半 (63 件中 55 件) が
シグナル 0 件**で、この repo の tracked file の 20% が初回編集で発火していた。

0.2.0 では「大きさそのものが十分条件」を `warn` 以上に引き上げ、`review` は
シグナル 1 個以上を要求する。あわせて Python の言語係数を 0.7 → 1.0 に戻した
(0.7 では review 閾値が 210 行で、pylint の `too-many-lines` 既定 1000 や
ESLint の `max-lines` 既定 300 と比べて突出して厳しかった)。実測値の前後比較は
CHANGELOG 0.2.0 に記録している。

構造シグナルは依然として「行数判定を上書きする独立ゲート」ではなく、(1) 言語/
role 係数・宣言的緩和という形で `effective_thresholds` 自体に織り込み、(2)
`note` tier の昇格判定 (シグナル 2 個以上)、(3) `review` tier の昇格判定
(シグナル 1 個以上) という 3 箇所で行数評価の解像度を上げる役割に限定している。
透明性確保のため、`message.py` は signal_count==0 で emit された場合 (warn/strong
のみ) は「検出された構造シグナル: なし」と明示する。

### `EXTENSION_LANGUAGE` を判定対象の allowlist に兼用する (0.2.0)

`language.py::is_code_path()` は `path.suffix.lower() in EXTENSION_LANGUAGE` を
返すだけ。denylist (`.md` / `.json` / … を列挙して弾く) ではなく allowlist に
したのは、未知の拡張子が現れたときの失敗方向を「通知しない」に倒すため
(advisory hook の `fail-open` = 通知しない側、という設計原則に合わせている)。

新しい言語を追加するときは `EXTENSION_LANGUAGE` に拡張子を足せば判定対象に
入る。`judge.py::LANGUAGE_MULTIPLIER` への追加は任意で、未登録なら 1.0。

#### 拡張子を持たないファイルだけ、内容 (shebang) で判定に戻す (0.5.0)

0.2.0 の allowlist 化には「拡張子を持たない shebang スクリプトも判定対象から
外れる」という代償があった。`#!/usr/bin/env python3` で始まる 261 行の
`bin/deploy` は無出力なのに、同内容の `.py` なら emit するという食い違いで、
判定がファイルの中身ではなく名前で決まっていた (内部バックログ)。

`is_code_path` (名前だけの判定) は**そのまま残す**。allowlist に載らない理由は
「未登録の拡張子」と「拡張子なし」の 2 通りあり、**後者だけ**を
`language.is_shebang_candidate` で内容判定に回す:

- 判定の移動は最小限。`.md` / `.json` のように名前で非コードと判るものは
  従来どおり内容を読まずに落ちる (allowlist の失敗方向を保つ)
- `__main__.py` は `by_extension` を保持し、`load_text` の後に
  `detect_language(path, first_line)` が `generic` を返したら
  「拡張子なし かつ shebang 不明」として skip する
- 拡張子がある場合は**拡張子の判定を優先**する。`.py` に `#!/usr/bin/env node`
  と書かれている食い違いでは拡張子の方が実体を表していることが多い
- `SHEBANG_LANGUAGE` は実在の shebang として広く使われるインタプリタだけに
  絞る。`bash`/`sh`/`zsh` を含めるのは `.sh`/`.bash`/`.zsh` が既に allowlist に
  あるためで、これを外すと「同じスクリプトが名前次第で判定される/されない」
  という非対称が残る
- **dotfile (`.bashrc` / `.envrc` / `.env`) は対象外**。`Path(".bashrc").suffix`
  は `""` なので絞らないと候補に入ってしまうが、慣例として設定ファイルであり、
  内容を読む対象を無用に広げない

代償: 拡張子なしのファイルは shebang を見るために内容を読む (最大 2MB)。
`Makefile` / `LICENSE` のような非スクリプトでも 1 回読んでから落ちる。
名前で落とすより遅いが、読み込みには既存の安全弁 (symlink/2MB/20,000 行) が
そのまま効く。

### `Metrics` に `import_categories` (カテゴリ名のタプル) を追加した理由

計画時点の `Metrics` フィールド列挙は `import_category_count` (件数) のみだった
が、`message.py` が「import カテゴリ多様性 5種 (network, db, ui, logging,
auth)」とカテゴリ名を列挙するには件数だけでは足りない。`message.build` は
`path/language/role/verdict/metrics` の 5 引数のみで生テキストにはアクセス
しないため、`metrics.py` 側でカテゴリ名を保持する以外に経路がない。
`import_category_count` は `judge.py` のシグナル閾値判定 (`>= 4`) に使うため
両方のフィールドを残す。

### `def_count_exact` フィールドは実装しなかった

計画の `Metrics(...)` 列挙に一度だけ登場するが、`judge.py`/`message.py`/テスト
計画のいずれにも対応する消費者が見当たらない。「AST exact か regex fallback
か」を示す品質フラグの意図だった可能性はあるが未配線のため、未使用フィールド
として残すのは冗長と判断し実装していない。`count_defs_python()` は AST 解析に
失敗すると `None` を返し、呼び出し側が generic regex にフォールバックする制御
フローのみ実装している。

### `message.build` の `path` 引数は表示用パスを呼び出し側で解決

`message.build(path, language, role, verdict, metrics)` は cwd を受け取らない
(計画の 5 引数シグネチャそのまま)。相対パス表示にしたい場合は `__main__.py`
側で `path.relative_to(cwd)` を試み、失敗時 (cwd 外) は絶対パスにフォール
バックしてから `message.build` に渡す。`message.py` 自体は cwd を一切知らない。

### `role` 引数はテストファイルの閾値緩和を可視化する用途で使用

`message.build` の `role` 引数は計画のシグネチャに含まれているが、計画が示す
2 つの出力例 (いずれも role=="normal" 相当) では表示に現れない。未使用の死んだ
引数にしないため、`role == "test"` のときだけ見出し行に `(test: 閾値 N倍)`
を追記する形で使っている。`role=="normal"` の出力は計画の例と完全一致する。

**係数の値は `verdict.applied_multipliers["role"]` から取る** (0.6.0)。以前は
`1.6` を文字列に直書きしていたため、`ROLE_MULTIPLIER` を動かすと表示だけが旧値
のまま残り、「目安の数値が printed 係数から導出できない」(0.3.0 で一度直した
欠陥) に戻る。`tests/test_message.py` が judge を実際に通した Verdict で
「表示 = 適用値」を固定している。

0.3.0 で `judge.Verdict.applied_multipliers` (language/role/declarative の実際の
係数) を追加し、`message._multiplier_breakdown` が「言語 係数 (× 宣言的 係数)」
を見出し行に追記するようになったが、`role` はここに含めない。上記の role_note
と重複表示になるため、role 係数の可視化は role_note 側に残している。

### `Verdict.scale` は `applied_multipliers` と別枠で保持する (0.3.0 レビュー対応)

`FILE_SPLIT_ADVISOR_SCALE` (全閾値への一律倍率) は当初、実効閾値の計算にだけ
反映し表示には一切出していなかった。これは「宣言的 ×1.6 が理由不明のまま
表示される」(内部バックログ) を修正した同じリリースで、SCALE についても同じ欠陥
(倍率 1.0 以外のとき「目安」の数値が printed 係数から導出できない) を
自己再導入していたバグで、レビューで指摘された。`scale` は judge の設計判断
どおり `applied_multipliers` には含めない (グローバル config であり per-file
の推論シグナルではないため) が、`Verdict.scale` という別フィールドで保持し、
`message.build` が role_note と同じ形の専用 parenthetical
(`(全体 2.0倍)`) を breakdown の直後に追記する。judge 側の実効閾値計算・
tier/emit 判定ロジックは変更していない (表示だけの修正)。

### 目安の表示 tier は判定 tier + 隣接 tier (0.3.0)

`message.build` の「目安」表示は以前 review/warn 固定だったため、note/strong
判定時には無関係な review/warn の数値だけが表示され、実際の判定根拠になった
閾値が示されないことがあった (`message.py:14` 相当の不正確さ)。`_display_tiers`
が判定 tier に応じて動的に 2 tier を選ぶ (strong のときだけ「1 つ下 + 自身」、
それ以外は「自身 + 1 つ上」)。judge 側の tier/emit 判定ロジックは変更していない
(表示だけの修正)。

### `Verdict.applied_multipliers` に signal_count==0 の推測文言の条件を持たせた (0.3.0)

`message.build` は signal_count==0 (行数のみが emit 根拠) のとき、以前は常に
「宣言的なコードの可能性があります」と表示していたが、実際に宣言的緩和
(`control_flow_density < DECLARATIVE_THRESHOLD`) が適用されていないファイル
(分岐の多いハンドラ等) にも同じ文言が付く不正確さがあった。
`verdict.applied_multipliers["declarative"]` (1.0 なら未適用) で判定するように
修正した。judge 側は `_effective_thresholds` が内部で計算済みの
`is_declarative` を `applied_multipliers` として外部に公開するだけで、
判定ロジック自体 (`_collect_signals` の独自計算) は変更していない。

### メモに分割候補の境界を付記する (0.5.0) — 判定は変えず、表示だけを足す

0.4.0 までのメモは行数・tier・シグナル名・定型 footer だけで、`検出シグナル:
定義数 28` のように「多い」ことしか言えず、**どの定義群を切り出すか**の手掛かり
が無かった (内部バックログ)。追加した 2 行はどちらも既に計算済みの情報から
得られる:

| 付記行 | 出典 | 精度 |
|---|---|---|
| 大きい定義 上位 N | Python は `ast` のモジュール直下、他言語は定義行の正規表現 | Python は正確、他言語は「次の定義まで」の概算 |
| import クラスタ | `IMPORT_CATEGORY_KEYWORDS` に一致した字面 | モジュール名でない語 (`http`) も混じる |

守っている制約:

- **`judge.py` は一切触らない**。`Metrics` に足した 2 フィールド
  (`top_level_defs` / `import_modules`) は `message.py` 専用で、シグナル判定に
  使わない。カテゴリの集合と順序 (`import_category_count` /
  `import_categories`) が 0.4.0 と同一になるよう、`_collect_import_categories`
  は**カテゴリの有無と語の採取を独立に記録する** — 表示用の整形
  (`_clean_module_token`) の結果が emit 判定に漏れないようにするため
- **トップレベルだけを列挙する**。`count_defs_python` (シグナル用) は入れ子も
  再帰的に数えるが、付記に出すのは分割単位になりうる定義だけなので
  `tree.body` に限る。非 Python も行頭にインデントのない定義行だけ見る
- **上限を 3 段で持つ**: 候補の保持 (`TOP_LEVEL_DEF_CAP` / 
  `IMPORT_MODULE_SAMPLE_CAP`)、表示件数 (`MAX_DEF_HIGHLIGHTS`)、メモ全体
  (`MAX_MEMO_CHARS` = 10,000)。最後の 1 つを超えるときは**付記行から落とす** —
  判定根拠 (行数・tier・シグナル) はメモの本体なので常に残す
- **単一カテゴリでは import クラスタ行を出さない**。境界は 2 つ以上のクラスタの
  間にしか現れず、1 つだけ並べてもメモが長くなるだけ
- 非 Python の `span` が概算である点は README の既知の限界に開示する。パーサを
  持たない言語で「大きさ」を出す手段が他に無く、出さないと順位付けができない
  (順位付けをやめて出現順に並べる案は、「大きい定義」という見出しの意味が
  失われるため採らない)

### 解析層の精度改善 (0.6.0) — 誤 advisory を消す方向だけ

0.6.0 は「本来コードでない部分を数えていた」2 件を直した。どちらも **metrics が
返す値が小さくなる = 通知が減る方向**にしか動かない。

#### 三重引用符を言語別テーブルにした

`_TRIPLE_QUOTE_DELIMITERS_BY_LANGUAGE` が「言語 → その言語に実在する三重引用符」
を持つ。0.5.0 までは `python` / `elixir` の 2 言語固定で、Kotlin / C# (raw
string) / Swift / Julia / Groovy の `"""…"""` はマスクが最初の改行で止まり、
**文字列の中の行頭 `if` / `for` が制御フロー密度に数えられていた**。大きな
SQL / doc 文字列を持つファイルで宣言的緩和が外れ、誤った分割助言が出る。

`frozenset` + 共通の区切りタプルではなく dict にしたのは、Julia に `'''` が
無く単一引用符が文字リテラルであるように、**言語ごとに実在する区切りが違う**
ため。投機的に広げず、実在を確認したものだけ足す (Java の text block、Groovy の
`'''`、C# の `""""` 以上の多重引用符は未対応のまま — README の既知の限界)。

#### 括弧 import ブロック内の「コメントだけの行」を import 行にしない

`_iter_import_lines` は継続行に `_strip_trailing_comment` を掛け、残りが空なら
yield しない。`import (` ブロックに書かれる `// http clients` のような見出し
コメントを import 行として扱うと、その散文に含まれる語 (`http`) から
カテゴリが立ち `import_category_count` を水増しする。

**行末コメント付きの import 行は従来どおり丸ごと yield する** (`"net/http" //
redis に切り替え予定` ではコメント側の語からもカテゴリが立つ)。落とすのは
「コメントだけの行」に限る — yield する内容まで `code` に差し替えると
0.4.0 までの挙動が広く変わり、変更の切り分けができなくなる。

#### 検証は合成 fixture の前後差で行った (コーパス diff の代替)

対象言語のファイルがコーパスにほとんど無く (`"""` を含むのは Swift 1 件のみ、
C# / Julia / Groovy は 0 件)、10,097 + Go 113 件を流して得た「Verdict 変化 0」
は**安全性の証明にならない**。代わりに合成 fixture の前後差を
`tests/test_metrics.py` の `TestTripleQuoteLanguages` /
`TestImportBlockCommentOnlyLines` に固定した。修正前のコードで**assertion
failure として落ちること**を確認してから採用している (床テスト 4 件は前後で
変わらないことを固定する側なので、修正前でも通る)。

### 解析層の精度改善 (0.4.0) — 判定表は変えず、入力の抽出だけを直す

0.4.0 は test 判定 / 早期 skip / import 抽出 / 定義数 / 制御フロー密度の
**抽出精度**だけを変えた。`BASE_THRESHOLDS`・`LANGUAGE_MULTIPLIER`・
`ROLE_MULTIPLIER`・`DECLARATIVE_THRESHOLD`・各シグナル閾値・emit 判定行列は
一切触っていない。閾値を動かすと「精度が上がったのか閾値が緩んだのか」を
コーパス差分から切り分けられなくなるため。

#### ディレクトリ名判定は `cwd` からの相対部分だけを見る

`language.relevant_dir_parts` を新設し、`is_test_path` と
`source.should_skip_by_name` の両方が使う。全祖先を見ると、プロジェクトの
置き場所 (`~/work/test/myapp/`、`~/src/vendor/app/`) がそのまま判定に混入する。
相対化できないとき (cwd 外・cwd 未指定) は**従来どおり全 parts を返す** —
失敗方向を「従来と同じ」に固定し、macOS の `/tmp` symlink 等の表記揺れで
判定が静かに変わらないようにするため。`language.py` は純粋関数層なので
realpath 正規化は行わない (`source.py` 側の containment 判定とは別系統)。

**この絞り込みは `cwd` 自身がテストディレクトリのとき逆向きに働く**
(`cwd=/repo/tests` で配下を編集すると相対部分に `tests` が現れず role が
`normal` になる)。ファイル名パターンによる test 判定は独立に効くため影響は
「テストディレクトリ配下でファイル名が test 形式でないヘルパ」に限られる。
`cwd` を test ディレクトリ名の集合と突き合わせて補正する案もあるが、
「`cwd` の外のディレクトリ名を判定に混ぜない」という本修正の主旨と衝突する
ため採らず、README の制約として開示するに留めた (マージ前レビューの指摘)。

#### 制御フロー密度は分子だけをマスクする

コメント・文字列リテラルを潰したテキストで**ヒット行だけ**を数え、分母
(非空行数) は元のテキストのまま。分母からコメントを除くと全ファイルの密度が
一斉に動き、「1 行あたりどれだけ分岐が詰まっているか」という指標の意味自体が
変わる。誤検出の除去 (分子側) に限定した。

マスクは `mask_comments_and_strings` が全文に対する 1 回の `re.sub` で行い、
マッチ部分を同じ長さの空白 (改行はそのまま) に置換する。長さと改行位置が
保たれるので行数・行の対応が変わらず、複数行文字列やブロックコメントも
1 パスで潰せる。万一行数がずれたらマスクせず元の行で数える安全弁を置いた。

**改行を跨ぐ文字列**は三重引用符だけでなく、JS/TS のテンプレートリテラルと
Go の raw string (バッククォート) も対象にする (`_MULTILINE_STRING_DELIMITERS`)。
本体の交替は「エスケープされた 1 文字」を先に置き、``\` `` (テンプレート
リテラル内のエスケープされたバッククォート) を閉じ区切りと誤認しない
(マージ前レビューの指摘)。誤認すると**本当の閉じ記号が「新しい未終端文字列の
開始」になり、以降のコードがファイル末尾まで全部マスクされる**。

> **交替のもう一方からバックスラッシュを除くのは必須** (`[^\\]`)。両方の交替が
> 同じ位置 (`\`) で開始できると、閉じ記号を持たない長い文字列で組み合わせが
> 指数爆発し `re.sub` が事実上停止する。実装中に自己導入して実測した
> (minify 済み bundle 1 ファイルで CPU 100% のまま 3 分以上返らない)。hook は
> 毎回の Write/Edit で走るので、これはそのまま編集のハングになる。
> `tests/test_metrics.py::TestMaskCommentsAndStrings::
> test_unterminated_multiline_string_with_backslashes_is_linear` が別プロセス
> に時間予算を与えて固定している。

代償として、バックスラッシュをエスケープ記号として扱わない Go の raw string が
`\` で終わる (`` `\d+\` ``) と閉じ記号を見失う (fail-open 方向)。
行内で閉じる前提のパターンで扱うと、(1) 埋め込まれた SQL/HTML/散文の
`if …` / `for …` 行が制御フローとして残り、(2) 逆に閉じバッククォートの後ろに
ある実コードが行末まで潰される、という双方向の誤りが出る (マージ前レビューの
指摘)。実 JS/TS コーパス 8,376 ファイルでは密度が動いたのが 288 件 (減 147 /
増 141) で、増加側がこの (2) の解消にあたる。代償は三重引用符と同じで、対に
ならないバッククォートが 1 個あるとそこから先すべてを文字列とみなす
(fail-open 方向)。

**測定が正確になった副作用**: 文字列リテラルに英文を大量に持つファイル
(i18n の文言テーブル、フィクスチャ文字列の多い大きなテストファイル) は
実測密度が下がり、`DECLARATIVE_THRESHOLD` (0.02) を下回って宣言的緩和
(×1.6) が効くようになる。その結果 tier が 1 段下がって emit が消えるものが
ある。**これは閾値を動かした結果ではなく、従来はコメント・文字列の中の英単語で
密度が水増しされていたために緩和が効いていなかった**もの。

とくに**テストファイルは緩和が二重に効いていた** (role 1.6 × declarative 1.6)
ため、この影響を受ける件数が多かった (実測: 本 repo の 873 行/976 行のテスト
ファイル 2 件 + 別の Python プロジェクトで 10 件)。**この二重緩和は 0.6.0 で
廃止した** (上記「role=test の緩和を 1 本化した」節)。

#### 制御フローのキーワードは言語で絞る

`match` / `select` / `when` は他言語では普通のメソッド名・関数名として頻出
する (`str.match(...)`、`select(state)`)。汎用集合に入れると誤検出が増える
ため `detect_language` の結果で分ける。Python の `match` はさらに文頭限定
(soft keyword であり `re.match(...)` と綴りが同じ) で、**文頭でも直後が
`=` / `(` / `.` / `[` なら数えない** (`match = re.match(...)` は変数名)。
ただし `(` だけは例外があり、**行末が `:` なら数える** — `match (value):` /
`match (a, b):` は subject を括弧で囲んだ / tuple subject の正当な match 文で、
`(` を無条件に弾くと分子から漏れる (マージ前レビューの指摘)。行末コメントは
マスク後に空白になっているので `:` 終端の判定には影響しない。
Rust の `match` は `let x = match y {` のように行中に来るのが普通なので
行内一致のまま。Ruby は `case` だけでなく分岐節の `when` も数える
(マージ前レビューの指摘)。

#### 定義数はプロパティ名と「識別子としての使用」を除外する

`type` / `enum` / `class` は TypeScript のオブジェクトリテラル・インタフェース
のプロパティ名として頻出する。キーワード直後の `:` / `?:` を否定先読みで
弾く。実コーパスでこれを入れないと、宣言の少ないデータ定義ファイルで
def_count が 1 → 37 に膨らむ例を観測した。

同じ理由で**直後が `.` / `(` / `[` の行も弾く** — `object.keys(x)` /
`impl.run()` / `type(x)` / `fun(x)` は宣言ではない (マージ前レビューの指摘)。
Go の `interface{}` / `struct{}` は `{` が続くので影響しない。代償として
行頭の無名関数式 (`function(payload) {`) を数えなくなるが、実プロジェクト
8,426 ファイルで 2 行 (いずれも bundle 済みの生成物) であり、拾い漏れ方向
= 通知が減る方向なので許容する。

アロー関数は**矢印が右辺の最上位**であることを要求する (`=` と `=>` の間に
許すのは括弧 1 組の仮引数リストか識別子 1 個だけ)。緩めると
`arr.reduce((a, x) => a + x, 0)` のような「アロー関数を引数に取る呼び出し」
まで定義として数える。

ただし仮引数の括弧と `=>` の間には**戻り型注釈**を許す
(`const parse = (x: Input): Output => …`)。これを許さないと TypeScript で
戻り型を書いたアロー関数が 1 件も数えられない (マージ前レビューの指摘。実
プロジェクトの TS/TSX 222 ファイルで計 +334 件)。注釈に許すのは
`=` / 括弧 / 波括弧 / `;` を含まない字面だけ — `Promise<void>` や
`Record<string, number>` は通り、オブジェクト型・関数型を戻り型に書いた形は
通らない。曖昧な形は「数えない」側に倒し、`arr.reduce((a: number, x: number):
number => …)` のような呼び出しを巻き込まないことを床テストで固定している。
先頭のジェネリック仮引数 (`<T,>`) も入れ子なしの 1 組だけ許す。

#### `require(` は言語と構文位置で絞る

行頭とは限らない `require(` の検出は **JS/TS 系の言語に限定**し
(`_REQUIRE_CALL_LANGUAGES`)、メンバ呼び出し (`x.require(…)`) を否定後読みで
除外する。全言語・全位置で見ると、Python の `schema.require(requests)` や
コメント・文字列リテラルの中の `require(` まで import 行になり、その行に
含まれる語からカテゴリが立つ (マージ前レビューの指摘)。実 JS/TS コーパスで
消えた 9 件はすべて minify 済み bundle の `o.require(N)` / `C.require("util")`
だった。

代償は Lua の `local http = require("socket.http")` のような JS/TS 以外の
正当な `require(` を分類しなくなること。カテゴリが減る = シグナルが減る =
通知が減る方向 (fail-open) なので許容し、README の既知の限界に開示した。
Ruby / PHP の `require 'x'` / `require_once` は行頭形なので `_IMPORT_HINT_RE`
側で従来どおり拾える。

#### `using` は import 形だけを数える

同じ綴りで import ではない構文が 3 系統ある (いずれもマージ前レビューの指摘):

| 形 | 例 | import か |
|---|---|---|
| using 文 | `using (var conn = …)` | ✗ |
| using 宣言 (C# 8) | `using var conn = …` | ✗ |
| リソース宣言 (TS 5.2) | `using resource = getResource()` | ✗ |
| 名前空間 / static | `using System.IO;` / `using static …;` | ✓ |
| エイリアス指令 (C#) | `using Alias = Namespace.Type;` | ✓ |

エイリアス指令と TypeScript のリソース宣言は `using <識別子> = …` で字面が
完全に重なるため、**`=` の右辺に呼び出し (`(`) があるかどうか**で分ける
(エイリアスの右辺は名前空間修飾された型名で `(` を含まない)。したがって
`using x = y;` (呼び出しを含まない TS のリソース宣言) は依然として import と
数えうる — 残る曖昧さは開示するに留め、判定を増やさない。

#### import ブロックの括弧判定は開き・閉じの両方で行末コメントを無視する

`    last_name)  # noqa` のように閉じ括弧の後にコメントが続く継続行は
`endswith(")")` を満たさず、ブロックが閉じないまま後続の最大 100 行が import
行として分類されていた (マージ前レビューの指摘)。`_strip_trailing_comment` で
言語別の行コメント記号以降を落としてから判定する。引用符の中の `#` / `//` も
落としうるが、用途が括弧の検出に限られるため、誤る方向は「ブロックを早く
閉じる」= import 行を増やさない側に倒れる。

**同じ処理を開き括弧の判定にも適用する** — `from deps import (  # grouped` /
`import (  // grouped` は `endswith("(")` を満たさず、継続行がまったく走査
されないためブロック形式の import からモジュール名が 1 件も分類されなかった
(マージ前レビューの指摘)。閉じ側だけ直して開き側を残していた片手落ちで、
**「同じ判定を 2 か所でしている」ことに気付いた時点で両方直す**のが正しい。

## 一時ディレクトリ・cwd 外の skip (`source.py`, 0.3.0)

### `should_skip_temp_dir` は「cwd 自体が一時領域か」ではなく「path が cwd の内側か」で決める

一時領域 (scratchpad 等) 配下のファイルは、実測で `/private/tmp/…/scratchpad/`
配下に絶対パス付きの分割助言が emit されることを確認した (Claude が分析用ダンプ
や handoff メモを scratchpad に書く運用)。これを常時 skip する機構を追加する際、
「`cwd` 自体が一時領域配下なら丸ごと除外する」設計も検討したが、この場合
`cwd=/private/tmp/projA` で `path=/private/tmp/projB/foo.py` (cwd の外にある
**別の**一時ディレクトリ) まで免除されてしまい、対象外にしたい「本来プロジェクト
外のファイル」を見逃す。最終的に `is_under_temp_dir(path) and not
path.is_relative_to(cwd)` (path が cwd の内側なら skip しない) に絞った。

**この設計は既存テストスイートとの互換性の鍵でもある**: `tempfile.mkdtemp()`
は本 repo の開発機 (macOS) で `/var/folders/...` 配下を返すため、`tests/` 配下の
全フィクスチャ (`cwd == self.tmp`、`self.tmp` 配下にファイルを書く) は実質的に
「一時領域配下で cwd もそこにある」状態になる。`path.is_relative_to(cwd)` が
常に True になるこのケースを skip しない設計にしたことで、既存 141 テストへの
影響ゼロで機能追加できた (`cwd` 自体が一時領域かどうかだけで免除する設計だと
同じ結果になるが、上記の兄弟ディレクトリ誤判定を残したままになる)。

`FILE_SPLIT_ADVISOR_CWD_ONLY` (opt-in, 既定 off) 用のテストはこの常時 on の
temp-dir skip と条件が重なるため、`source._temp_dir_roots` を空にモックして
温存領域スキップ自体を無効化し、CWD_ONLY 単体の挙動を分離して確認している
(`tests/test_main.py::TestCwdOnlyOptIn`)。

## debounce (`state.py`)

MainClaude/Subagent が並行して複数ファイルを Write/Edit する運用を想定すると、
同一セッションの state ファイルへの read-modify-write はロックなしでは競合
する。「debounce 判定」と「emit 実行後の記録」を別々の呼び出しに分けると、2 つ
の呼び出しの間に別プロセスが割り込む TOCTOU (check-then-act) レースが生じうる。
この repo には同型の問題 (レビュー回数の並行予約) に対する実装済みの前例
`external-ai-assist/hooks/exitplan-review/__main__.py::reserve_slot`
(`fcntl.flock` で read→判定→write を単一ロック区間に収める) があり、これを
踏襲した `try_reserve_emit()` に「判定」と「予約」を統合している。

`reserve_slot` との差分:

- **session_id をハッシュ化してファイル名にする** (`hashlib.sha256(...)
  .hexdigest()[:16]`)。`/` や `..` を含む session_id が万一渡ってきても
  `TMPDIR` 外への書き込みや例外につながらない
- **`import fcntl` をモジュールトップレベルで無条件に行わない**。
  `exitplan-review` は無条件 import で、Windows では `main()` 到達前の
  モジュールロード時に未捕捉 `ImportError` で丸ごとクラッシュする潜在バグが
  ある。本 hook は advisory であり security-critical ではないため、「ロック
  なしで動作継続」に degrade する方が「ロックはあるが起動不能」より適切、と
  判断して `try/except ImportError` + `HAVE_FLOCK` フラグに変更した

### 書込不能時は沈黙する / 古い state を掃除する (0.5.0)

0.4.0 までの `try_reserve_emit` は、`OSError` を「state 無し」と同じ扱いにして
**通知する**方向に倒していた (docstring にも明記していた)。実測すると
`TMPDIR=/` のような書込不能環境では同一セッション・同一ファイルの Edit 2 回が
どちらも emit し、debounce が完全に失われる (内部バックログ)。巨大なファイルを
編集するたびに同じメモが注入され、ユーザーには原因が見えない。

方向は自分で決めずに済んだ — README の設計原則 2 が既に「判定不能・IO 失敗は
すべて『通知しない』側に倒す」と書いており、`state.py` だけがそれに反していた。
実装はコードを原則に合わせただけで、新しい方針決定ではない。

- 候補ディレクトリを `_state_dirs()` で順に試す (`$TMPDIR` → XDG キャッシュ)。
  XDG は「消えてもよいが書けることが多い」場所なので 2 番目に置く
- 全滅したら `False` (通知しない) を返し、**プロセス内で 1 回だけ** stderr に
  理由を出す。hook は編集ごとに新しいプロセスなので実質「その編集につき 1 行」
- **`session_id` が空のときは従来どおり通知する**。ここは「記録先が壊れている」
  のではなく「debounce がそもそも要求されていない」ケースで、抑制側に倒すと
  envelope の仕様変更で通知が黙って全滅する。非対称は意図的

古い state の掃除 (`sweep_stale_states`) は **自分の state ファイルを作る前**に、
**新規作成のときだけ**走らせる:

- 作る前に掃除するので自分のファイルは消えない
- 「新規作成のときだけ」にしないと、7 日を超えて生きているセッションが自分の
  記録を消してしまう。`open(..., "a+")` がファイルを作り直すため**存在を見ても
  気付けない** — 回帰テストは記録の中身 (`/repo/a.py` のエントリ) で確認する
- `/compact` 後の再通知は**行わない** (README の既知の限界に開示)。`PostCompact`
  で tier 記録をリセットする案は、通知疲れを防ぐという目的 (本ファイル冒頭の
  目的 3) と正面から衝突するため採らない

### 行数の記録と「tier を進めない抑制」 (0.2.0)

パスごとの記録が tier 文字列から `{"tier": ..., "lines": ...}` に変わった
(0.1.0 が書いた文字列形式も `_read_record` が引き続き読む)。`Write` は編集前の
内容が hook に渡らないため、直近に観測した行数を比較対象として使う。

**行数は通知の有無に関わらず記録するが、tier は通知したときだけ進める。**
これを取り違えると、typo 修正で抑制した編集が `tier=strong` を記録してしまい、
その後にそのファイルが本当に成長しても「同一 tier」とみなされてセッション内で
恒久的に抑制される。`state.py::try_reserve_emit` の `granted` と `record` の
分岐、および `tests/test_main.py::TestGrowthGate` の
`test_suppressed_edit_does_not_consume_the_tier_high_water_mark` で固定している。

成長方向が `UNKNOWN` (Write、または `old_string`/`new_string` が無い・型が違う
Edit) のときは、記録があれば行数比較で決め、記録が無ければ通知する。envelope の
形が将来変わったときに通知が黙って全滅するより、0.1.0 と同じ挙動に戻す方を選ぶ。

### `change.py`: 改行数の差 ≠ 行数の差 (ファイル末尾だけ)

行数は `splitlines()` で数えるため「改行で終わらない最終行」が 1 行として
数えられるが、改行の個数はこれを含まない。したがって **ファイル末尾の置換が
改行終端の有無を変える場合だけ**、改行数の差と行数の差が 1 ずれる:

| 末尾の置換 | 改行数の差 | 実際の行数の差 |
|---|---|---|
| `"foo\n"` → `"foo\nbar"` | 0 | **+1** |
| `"foo"` → `"foo\n"` | +1 | **0** |

末尾以外の置換では、置換前後のテキストの末尾が変わらないため差は一致する。

`classify_growth` は編集後の全文 (`loaded.text`) を受け取り、**置換が末尾で
起きたと確定できるときだけ**「置換前の末尾テキスト」を復元して補正する。
確定条件は `text.find(new) == tail_start == text.rfind(new)` — つまり
`new_string` が全文にちょうど 1 箇所しか現れず、それが末尾にあること。

`text.endswith(new)` だけでは不十分。**別の場所を置換した結果、たまたま同じ
文字列が末尾にも並ぶ**ことがあるため。例: `"X\nsomething\nfoo\n"` の先頭 `X` を
`"foo\n"` に置換すると 3 行 → 4 行に増えるが、結果 `"foo\n\nsomething\nfoo\n"`
の末尾にも元からの `"foo\n"` がある。ここで補正すると増加分が打ち消されて
`NOT_GREW` になり、通知が消える (`count()` は重複を数えないので
`find`/`rfind` の一致で判定する)。

確定できないときと全文を渡さない呼び方では、改行数の差による近似に戻る
(末尾で改行終端が変わる場合に 1 ずれうる)。`tests/test_change.py` で
補正あり・補正なし・確定できない場合の 3 通りを固定している。

## テスト実行

```bash
cd hooks/file-split-advisor
python3 -m unittest discover tests
```

`tests/_testutil.py` が plugin dir を sys.path に挿入する (unittest discover
経由)。`tests/conftest.py` は pytest 実行時の同型セーフティネット。

クラス単位・メソッド単位の単体指定 (`python3 -m unittest
tests.test_x.Class.method` / `pytest tests/test_x.py::Class::method`) では、
ディレクトリ探索と違って `tests/` ディレクトリ自体が sys.path に入らないため、
`_testutil` の解決に `tests/__init__.py` が自身のディレクトリを sys.path に
足す処理も必要になる (0.3.1、内部バックログ)。ディレクトリ探索/単体指定/pytest
の3経路とも `tests/__init__.py` 経由で解決されるため、`_testutil.py`/
`conftest.py` はどちらも変更していない。単体指定の実行例は
[../../README.md](../../README.md) のテスト節を参照。

## 手動スモークテスト

```bash
cd hooks/file-split-advisor
echo '{"session_id":"smoke","cwd":"'"$PWD"'","tool_name":"Write",
"tool_input":{"file_path":"/tmp/big.py","content":"..."}}' \
  | python3 .
```

事前に `/tmp/big.py` に 300 行超・import 多様性ありの Python ファイルを置いて
おくと `additionalContext` 付き JSON が stdout に出る。閾値未満なら無出力。

## 拡張ポイント

- **path-ignore リスト (0.3.0 で実装済み)**: `source.load_ignore_globs` /
  `source.matches_ignore_glob` が `FILE_SPLIT_ADVISOR_IGNORE` (カンマ区切り
  glob) と `~/.claude/file-split-advisor/ignore.local.txt` (gitignore 風、
  1 行 1 glob) を統合する。fnmatch ベースの素朴な glob のみで、否定 (`!`) 等の
  完全な .gitignore 構文は実装していない。`__main__.py` は
  `source.should_skip_by_name` より前でこの判定を行う
- **全閾値の一律倍率 (0.3.0 で実装済み)**: `FILE_SPLIT_ADVISOR_SCALE` を
  `judge.judge(..., scale=...)` に渡す。tier/言語ごとの個別上書きではなく
  グローバルな倍率のみ。`judge.Verdict.applied_multipliers` には含めない
  (per-file の推論シグナルではなくグローバル config のため、message.py の
  breakdown 表示対象外)
- **新しい import カテゴリ / キーワード**: `metrics.py::IMPORT_CATEGORY_KEYWORDS`
  に追記する。カテゴリ自体を増やす場合は `judge.py::IMPORT_DIVERSITY_SIGNAL_THRESHOLD`
  (現状 7 カテゴリ中 4 種) も見直す
- **新しい言語**: `language.py::EXTENSION_LANGUAGE` に拡張子を追加する
  (= 判定対象に入る)。`judge.py::LANGUAGE_MULTIPLIER` への係数追加は任意で、
  未登録なら 1.0
- **新しい shebang**: `language.py::SHEBANG_LANGUAGE` に
  `インタプリタ名 -> 言語名` を 1 行足す (値は `EXTENSION_LANGUAGE` と同じ
  言語名にする)。「あり得そう」なインタプリタを投機的に足すと、判定対象が
  測らないまま広がるので実在の使用を確認してから足す

## 発火率を変える変更をしたときの測定

閾値・シグナル・skip 条件を触ったら、**変更前後の実コードで同じコーパスを流して
通知件数を比較する**。「新しい判定が動くこと」は単体テストで確認できるが、
「旧版が拾えていた入力を落としたこと」は前後比較でしか分からない。

手順は「repo の全 tracked file に対して `should_skip_by_name` →
`is_code_path` → `load_text` → `is_generated_by_content` → `compute` →
`judge` を流し、1 ファイル 1 レコードで結果を出す」だけ。state (debounce) は
通さない (= 「そのファイルをセッション内で初めて編集したときに通知が出るか」を
測る)。差分は「意図した変更 / 改善 / 説明不能」に分類し、説明不能をゼロにする。

0.2.0 の測定結果は CHANGELOG に記録している。編集内容に依存する成長判定
(`change.py`) の効果はこの走査には現れないので、別途 `tests/test_main.py::
TestGrowthGate` で固定する。

## 依存関係

標準ライブラリのみ。`pip install` 不要。Python 3.11+ 想定。
