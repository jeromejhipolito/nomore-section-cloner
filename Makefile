URL ?= https://try-nomore.com/products/nomorecolds-red-light-therapy-colds-sinusitis

install:
	uv sync
	uv run playwright install chromium

run:
	uv run python -m sectioncloner $(URL)

show:
	uv run python -m sectioncloner.show

demo: run show

record:
	uv run python scripts/record_run.py $(URL) -o demo.mp4

test:
	uv run pytest -q

.PHONY: install run show demo record test
