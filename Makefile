# 全 plugin の validate/lint/test/clean をローカルから 1 コマンドで実行するための
# 開発者向けショートカット。test は CI (.github/workflows/validate.yml) と
# 同じ scripts/test-all.sh を呼ぶため、列挙ロジックの二重管理を避けている。
.PHONY: validate lint test clean

# ruff の呼び出し方の差し替え口。既定は PATH 上の ruff (CI は
# `pip install 'ruff==0.16.8'` で入れる)。version manager 経由で使う場合は
# コマンド前置きをまとめて渡す。例:
#   make lint RUFF="mise exec ruff@0.16.8 -- ruff"
# ruff は版ごとに規則が追加・強化されるため、CI は version を固定している。
RUFF ?= ruff

validate:
	claude plugin validate .
	for dir in plugins/*/; do [ -d "$$dir" ] || continue; echo "== Validating $$dir =="; claude plugin validate "$$dir" || exit 1; done
	python3 scripts/check_codex_manifest_version.py
	python3 scripts/check_marketplace_entry_sync.py

lint:
	$(RUFF) check .

test:
	scripts/test-all.sh

clean:
	find . -type d \( -name "__pycache__" -o -name ".pytest_cache" -o -name ".ruff_cache" \) -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
