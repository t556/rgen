PYTHON ?= .venv/bin/python

.PHONY: test serve generate-dev preview-dev
test:
	$(PYTHON) -m pytest -q
serve:
	$(PYTHON) -m resultsgen serve
generate-dev:
	$(PYTHON) -m resultsgen generate resultsgen/presets/dev.json --out ./out --force
preview-dev:
	$(PYTHON) -m resultsgen preview resultsgen/presets/dev.json
