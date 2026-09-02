PYTHON ?= python3
LITEX_REPOS := m-labs/migen enjoy-digital/litex enjoy-digital/liteeth \
               enjoy-digital/litedram litex-hub/litex-boards \
               litex-hub/pythondata-cpu-vexriscv

.PHONY: lint test docs check update-litex

check: docs lint test

docs:
	$(PYTHON) tools/check_docs.py

lint:
	black --check .
	# --ignore on the command line would replace the pyproject list, not add to it.
	pylint --recursive=y .

# numpy/OpenBLAS spawns a thread pool per xdist worker; on a many-core host that
# exhausts pthread limits and crashes workers.
export OPENBLAS_NUM_THREADS := 1
export OMP_NUM_THREADS := 1

test:
	pytest -n auto --cov-fail-under=85

update-litex:
	@for r in $(LITEX_REPOS); do \
	  sha=$$(git ls-remote https://github.com/$$r HEAD | cut -f1); \
	  name=$$(basename $$r); \
	  sed -i "s|github.com/$$r@[0-9a-f]\{40\}|github.com/$$r@$$sha|" requirements-gateware.txt; \
	done
	@git diff --stat requirements-gateware.txt
