# Changelog

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
