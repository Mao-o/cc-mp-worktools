"""external-ai-assist の hook 間で共有するヘルパー群。

## 参照のしかた (sys.path ブートストラップ)

各 hook は `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/<hook>` で **ディレクトリを直接実行**
されるため、`sys.path[0]` は各 hook ディレクトリになり、隣の `_common/` は見えない。
そこで各 hook の `__main__.py` (とテストの `_testutil.py`) は、hook 内モジュールを
import する前に

    _HOOKS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _HOOKS_DIR not in sys.path:
        sys.path.insert(0, _HOOKS_DIR)

を実行し、`from _common import ...` を解決できるようにしている。`hooks/` は plugin root
内の相対配置なので、`${CLAUDE_PLUGIN_ROOT}` が `~/.claude/plugins/cache/` 配下のコピーを
指していてもそのまま解決する (plugin root の外は一切参照しない)。`hooks/` 直下に
`.py` は置かないため、sys.path に載せて見えるようになる名前は `_common` だけ。

## モジュール

| module | 役割 |
|---|---|
| `sentinel` | REVIEW_CLEAN sentinel の判定 (フェンス・装飾・前置き 1 文の扱い) |
| `subproc` | 外部 CLI の起動。process group + timeout + 残出力の読み捨て |
| `cursorcli` | cursor agent の存在確認と review 用 argv |
| `flock` | flock 下での read-modify-write |
| `hooklog` | `[<hook>] msg` 形式の stderr ログ |

## 共通化しないもの

hook 固有の状態機械 (post-implementation-review の pending / in-flight、exitplan-review の
マーカー、explore-parallel の pid / result ファイル) と、review 実行中ずっと保持する
非ブロッキングの `cursor_lock` (ロックファイルを開けない環境で直列化を諦める fail-open
分岐が固有) は各 hook に残している。
"""
