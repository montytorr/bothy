# Bothy. Standard library only, so there is nothing to install.

.PHONY: test check clean

test:
	@python3 -m unittest discover -s tests -v

check:
	@python3 -m unittest discover -s tests -q && echo "ok"

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .bothy-test
