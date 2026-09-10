# sanduk - run an agent in a disposable container.
#
# Two families of targets: Python packaging (uv, ruff, mypy, pytest) and
# container operations (image, network, cleanup). Override any variable on the
# command line, e.g.
#   make run TASK='Find the slowest test' WORK=../myrepo ARGS='--effort max'

PKG     ?= sanduk
AGENT   ?= claude
RUNTIME ?= apple
IMAGE   ?=
NETWORK ?= sanduk-net
WORK    ?= ./work
TASK    ?= Summarise every Python file here.
ARGS    ?=
UV      ?= uv
RUN     ?= $(UV) run

# The integration suite reads these to pick what it boots. IMAGE is exported
# only when set, so the suite falls back to the handler's own image rather than
# a name this file would have to keep in step with the registry.
export AGENT RUNTIME NETWORK
ifneq ($(IMAGE),)
export IMAGE
endif

.DEFAULT_GOAL := help
.PHONY: help sync build wheel sdist dist check publish-test publish upgrade \
        release test test-container test-all coverage coverage-html \
        lint lint-check format format-check typecheck qa docs \
        image image-rebuild run run-proxy shell ps logs \
        stop clean distclean destroy system-start system-stop system-status

help:  ## Show this help
	@echo "sanduk targets:"
	@grep -hE '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) \
	  | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'
	@echo
	@echo "Variables: AGENT=$(AGENT) RUNTIME=$(RUNTIME) NETWORK=$(NETWORK) WORK=$(WORK)"

# --- python environment -----------------------------------------------------

sync:  ## Resolve and install the environment, including the editable package
	@$(UV) sync

build: sync  ## Alias for sync (pure Python: edits take effect immediately)

upgrade:  ## Re-resolve every dependency to its newest allowed version
	@$(UV) lock --upgrade
	@$(UV) sync

# --- quality ----------------------------------------------------------------

test:  ## Fast suite: no containers, no API calls, no key needed
	@$(RUN) pytest -q

test-container:  ## Integration suite: boots real containers; needs `make image`
	@$(RUN) pytest -q -m container

test-live:  ## Live provider suite: needs LLAMA_SERVER, or a provider key
	@$(RUN) pytest -q -m provider_live

test-all:  ## Every suite
	@$(RUN) pytest -q -m ""

coverage:  ## Fast suite with a terminal coverage report
	@$(RUN) pytest -q --cov=src/$(PKG) --cov-report=term-missing

coverage-html:  ## Fast suite with an HTML coverage report
	@$(RUN) pytest -q --cov=src/$(PKG) --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

lint:  ## Lint with ruff, applying fixes
	@$(RUN) ruff check --fix src/ tests/ scripts/

lint-check:  ## Lint with ruff, reporting only
	@$(RUN) ruff check src/ tests/ scripts/

format:  ## Format with ruff
	@$(RUN) ruff format src/ tests/ scripts/

format-check:  ## Check formatting without modifying files
	@$(RUN) ruff format --check src/ tests/ scripts/

typecheck:  ## Type check with mypy
	@$(RUN) mypy src/$(PKG)

qa: lint-check format-check typecheck test  ## Non-mutating quality gate; mirrors CI

# --- distribution -----------------------------------------------------------

wheel:  ## Build a wheel
	@$(UV) build --wheel

sdist:  ## Build a source distribution
	@$(UV) build --sdist

check:  ## Validate the built distributions with twine
	@$(RUN) twine check dist/*

dist: wheel sdist check  ## Build and validate both distributions

publish-test: check  ## Upload to TestPyPI
	@$(RUN) twine upload --repository testpypi dist/*

publish: check  ## Upload to PyPI
	@$(RUN) twine upload dist/*

release:  ## Bump the version, commit, and tag
	@echo "Current version: $$(grep '^version' pyproject.toml | head -1)"
	@read -p "New version: " version; \
	sed "s/^version = .*/version = \"$$version\"/" pyproject.toml > pyproject.toml.tmp \
	  && mv pyproject.toml.tmp pyproject.toml; \
	git add pyproject.toml; \
	git commit -m "Bump version to $$version"; \
	git tag -a "v$$version" -m "Release $$version"; \
	echo "Tagged v$$version. Run 'git push && git push --tags' to publish."

docs:  ## Build documentation (sphinx is fetched on demand)
	@$(UV) run --with sphinx sphinx-build -b html docs/ docs/_build/html

# --- image ------------------------------------------------------------------

image:  ## Build the agent image if it is missing
	@$(RUN) sanduk build --agent $(AGENT) --runtime $(RUNTIME)

image-rebuild:  ## Rebuild the agent image unconditionally
	@$(RUN) sanduk build --agent $(AGENT) --force --runtime $(RUNTIME)

# --- running ----------------------------------------------------------------

run:  ## Run the agent. TASK='...' WORK=./dir AGENT=hax ARGS='--effort max'
	@$(RUN) sanduk run "$(TASK)" -w $(WORK) --agent $(AGENT) $(ARGS) --runtime $(RUNTIME)

run-proxy:  ## Run sealed: no egress, and the key held on the host
	@$(RUN) sanduk run "$(TASK)" -w $(WORK) --agent $(AGENT) --mode sealed $(ARGS) --runtime $(RUNTIME)

shell:  ## Interactive shell in the agent image (no network, nothing mounted)
	@$(RUN) sanduk shell --agent $(AGENT) --runtime $(RUNTIME)

# --- inspection -------------------------------------------------------------

ps:  ## List sanduk containers
	@$(RUN) sanduk ps --runtime $(RUNTIME)

logs:  ## Show recorded request bodies from --log-bodies runs
	@ls -R sanduk-logs 2>/dev/null || echo "no logs (run with --log-bodies)"

# --- teardown ---------------------------------------------------------------

stop:  ## Stop running sanduk containers, leaving them on disk
	@$(RUN) sanduk stop --runtime $(RUNTIME)

clean:  ## Delete sanduk containers and build scratch. Keeps work/ and logs
	@$(RUN) sanduk clean --runtime $(RUNTIME)
	@rm -rf build/ dist/ htmlcov/ .coverage .pytest_cache/
	@rm -rf src/*.egg-info/ *.egg-info/
	@find . -name "__pycache__" -type d -prune -exec rm -rf {} +
	@echo "cleaned"

distclean: clean  ## clean, plus the resolved environment and tool caches
	@rm -rf .venv/ .mypy_cache/ .ruff_cache/

destroy:  ## clean, plus the image, the network, and recorded request bodies
	@$(RUN) sanduk destroy --agent $(AGENT) --proxy-network $(NETWORK) --runtime $(RUNTIME)
	@rm -rf build/ dist/ htmlcov/ .coverage .pytest_cache/
	@if [ -d sanduk-logs ]; then \
	  n=$$(find sanduk-logs -name '*.json' | wc -l | tr -d ' '); \
	  rm -rf sanduk-logs; echo "deleted sanduk-logs ($$n files)"; \
	else echo "no sanduk-logs"; fi

# --- container service ------------------------------------------------------

system-status:  ## Show whether the container engine is ready
	@$(RUN) sanduk system status --runtime $(RUNTIME)

system-start:  ## Start the engine's service, where it has one
	@$(RUN) sanduk system start --runtime $(RUNTIME)

system-stop:  ## Stop it. NOTE: system-wide, not just sanduk
	@$(RUN) sanduk system stop --runtime $(RUNTIME)
