# Changelog

## 0.1.0

初版。手元のグローバル Skill として使っていた `iterating-pr-codex-reviews` を plugin に移した。

- Skill `iterating-pr-codex-reviews` と、状態確認 / 再 trigger / 安全 commit の 3 スクリプトを同梱
- 移植にあわせて直した点:
  - `pr-codex-status.sh`: Codex bot の issue comment を表示し、最新の `@codex review` 以降に
    エラー (利用上限・`Something went wrong` 等) が返っていれば `Codex verdict: ERROR` と出す。
    これまでは reaction しか見ておらず「NO REACTION」のまま待ち続けていた
  - `pr-codex-status.sh`: verdict を最新の `@codex review` 以降の Codex の応答だけで決める。
    PR 本文に残った前のサイクルの 👍 で、再レビュー中に PASSED と出ていた。trigger コメントの
    👀 / 👍 と、最新サイクルの review (`REVIEWED`) も見る。判定は Codex connector の応答に
    絞り (他の bot のコメントで誤判定しない)、`Unknown error` / 環境未設定もエラーとして扱う
    👍 / 👀 は PR body・issue comment・review comment のどれに付いても最新サイクルのものを数える
  - Skill: マージ前のチェックを「対象 repo のテスト・lint・検証コマンド」に一般化した
    (`unittest discover` 固定をやめた)
  - `pr-codex-status.sh`: review comments のリアクション取得と最新 `@codex review` の特定に
    `--paginate` を付けた (30 件を超えると最新が欠けていた)
  - Skill: スクリプトの参照を `${CLAUDE_PLUGIN_ROOT}/scripts` にした。`since` の付け方、
    エラー時の対処、収束しないときの打ち切り方を追記した
- スクリプトのテストを追加 (`gh` を偽物に差し替えて、コメントの投稿順などを確認する)
