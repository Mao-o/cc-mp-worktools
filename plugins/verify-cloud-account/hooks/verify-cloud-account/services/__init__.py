"""登録済みサービス一覧。

新しいサービスを追加するには:
  1. services/<name>.py を作成し、以下を定義する:
     - PATTERNS: list[str]          コマンドマッチ用の正規表現
     - READONLY: list[str]          検証をスキップする読み取り専用コマンド
     - ACCOUNT_KEY: str             accounts.local.json 上のキー名
     - SETUP_HINT: str              accounts.local.json 未設定時の案内文
     - verify(expected, project_dir) -> str | None  検証関数（None=成功, 文字列=エラー理由）
  2. 下記 import と ALL リストに追加する。
"""
from . import aws, firebase, github, gcloud

ALL = [github, firebase, aws, gcloud]
