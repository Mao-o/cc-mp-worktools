# tests/fixtures/keys/

PEM / armored 鍵形状の fixture。**実鍵は置かない** (公開 repo に鍵形状の実データを
置くと secret scanner が反応し、レビュー時のノイズになるため)。

## なぜ固定 fixture が要るか

`redaction/keyonly_scan.py` の `_KEY_RE` は PEM 最終行の base64 パディング `=` を
`KEY=` と誤認し、base64 本体を「鍵名」として reason に出していた
(詳細は `redaction/pem.py` の冒頭)。

この発火は **最終行に `+` `/` が含まれないときだけ**起きる。`_KEY_RE` の
`[\w.\-]` が `+` `/` を含まないためで、実鍵を生成してテストすると
**約 90% の確率で pass する flaky テスト**になる。したがって

- **発火する形 (最終行に `+` `/` を含まない)** を固定で置く
- 実鍵ではなく、同じ構造トリガを持つ合成データにする

## ファイル

| ファイル | 性質 |
|---|---|
| `synthetic_rsa.pem.txt` | 最終行が `...EFghIJkl=` で `+` `/` を含まない = **修正前は漏洩した形**。本文は `NOTAREALKEY` を含む明示的な合成データ |

`+` `/` を含む形 (漏洩しなかった側) は、上記が pass すれば自動的に pass するため
fixture としては置かない。

## 拡張子が `.pem.txt` である理由

`.pem` のままだと **本 plugin 自身の Stop hook が毎セッション block する**。
除外レシピは `~/.claude/` 配下のユーザー単位ファイルにしか書けず、repo に
commit して貢献者・CI と共有する手段が無いため、fixture 側の拡張子で回避する。

PEM 判定は 0.23.0 で **内容の sniff** (`redaction/pem.py::looks_pem`) に
なったため、拡張子を変えてもテストの検証力は落ちない
(`_detect_format` は `.txt` を `opaque` に落とし、そこから content sniff で
pem 経路に入る)。32KB 超の bundle 経路と `.env` 埋め込み経路は
テスト側で動的に生成する (`test_pem.py`)。
