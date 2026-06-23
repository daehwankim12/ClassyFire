.PHONY: test lint format typecheck seed-download seed-verify seed-import seed-audit seed-activate

test:
	python -m pytest

lint:
	python -m ruff check .

format:
	python -m ruff format .

typecheck:
	python -m mypy apps config

seed-download:
	python manage.py seed_download

seed-verify:
	python manage.py seed_verify

seed-import:
	python manage.py seed_import --resume

seed-audit:
	python manage.py seed_audit

seed-activate:
	python manage.py seed_activate
