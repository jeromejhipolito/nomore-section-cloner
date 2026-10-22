URL ?= https://try-nomore.com/products/nomorecolds-red-light-therapy-colds-sinusitis

install:
	uv sync
	uv run playwright install chromium

run:
	uv run python -m sectioncloner $(URL)

test:
	uv run pytest -q

.PHONY: install run test
