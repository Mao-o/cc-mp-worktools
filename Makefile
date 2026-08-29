# 全 plugin の validate/test/clean をローカルから 1 コマンドで実行するための
# 開発者向けショートカット。test は CI (.github/workflows/validate.yml) と
# 同じ scripts/test-all.sh を呼ぶため、列挙ロジックの二重管理を避けている。
.PHONY: validate test clean

validate:
	claude plugin validate .
	for dir in plugins/*/; do [ -d "$$dir" ] || continue; echo "== Validating $$dir =="; claude plugin validate "$$dir"; done
	python3 scripts/check_codex_manifest_version.py

test:
	scripts/test-all.sh

clean:
	find . -type d \( -name "__pycache__" -o -name ".pytest_cache" \) -prune -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
