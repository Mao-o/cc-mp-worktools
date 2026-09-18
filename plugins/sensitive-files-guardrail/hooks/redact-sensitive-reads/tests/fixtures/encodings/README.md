# tests/fixtures/encodings/

テキストエンコーディング形状の fixture。**バイト列そのものは commit しない。**

## なぜこのディレクトリが (ファイル無しで) あるか

0.30.0 まで BOM / UTF-16 を扱うテストが 1 件も無く、次の 2 件が 1,200 件超の
テストを通過していた (内部バックログ):

- **BOM 付き UTF-8** (`utf-8-sig`): 残った U+FEFF に行頭 regex (`^\s*` /
  `^`) が一致せず、**先頭 1 鍵が黙って消える** (`entries` も 1 件少なくなる)
- **UTF-16**: 無条件 `utf-8` デコードで全バイトが U+FFFD になり、
  `entries: 0` / `(no entries)` = **生きた鍵を含むファイルを「空」と報告**

「テスト件数」では見えない **入力形状の空白**だったので、形状カバレッジとして
独立に固定した。

## 実体は `tests/_testutil.py`

`fixtures/keys/README.md` と同じ方針で、**クレデンシャル形状のバイナリを公開
repo に置かない**ため、1 本の UTF-8 ソース文字列から**テスト実行時に再
エンコード**する:

| シンボル (`tests/_testutil.py`) | 内容 |
|---|---|
| `ENCODING_SAMPLE_DOTENV` | 3 鍵の `.env` (値はすべて明示的なダミー) |
| `ENCODING_SAMPLE_YAML` | 同じ鍵名の YAML (yaml パーサ経路) |
| `ENCODING_SAMPLE_OPAQUE` | 構造不明形式として流す入力 (keys-only scan 経路) |
| `ENCODINGS_WITH_DETECTION` | 推定できるエンコーディング一覧 |
| `encode_sample(text, encoding)` | 再エンコード |

BOM の有無はコーデック任せ (`utf-16` は Python が BOM を付け、`utf-16-le` /
`utf-16-be` は付けない = BOM 無し検出経路を通る)。

同じソースを使うので **「同じ内容なのにエンコーディングだけ違う」** という
マトリクスが崩れない (バイト列を個別に commit すると片方だけ更新される)。

## 使っているテスト

`tests/test_decoding.py` — 3 パーサ (dotenv / yaml / keys-only) ×
エンコーディングのマトリクスと、`redaction/decoding.py` の推定単体テスト。
`latin-1` は「推定できない」側の代表で、**取りこぼしを開示すること**だけを
期待する (鍵名・長さは不正確なままでよい)。
