.PHONY: prepare-media build serve reindex-private-posts \
	serve-private-stack stop-private-stack restart-private-stack status-private-stack \
	serve-private-search stop-private-search restart-private-search status-private-search

PYTHON ?= python3
SERVICE ?= all

prepare-media:
	mkdir -p assets/posts_media
	rsync -av --delete --prune-empty-dirs "_posts/media/" "assets/posts_media/" || true

build: prepare-media
	PAGES_DISABLE_GITHUB_METADATA=true bundle exec jekyll build

serve: prepare-media
	PAGES_DISABLE_GITHUB_METADATA=true bundle exec jekyll serve

reindex-private-posts:
	$(PYTHON) preprocessing/index_private_posts.py

serve-private-stack: prepare-media
	@set -e; \
	wait_port() { \
		port="$$1"; name="$$2"; max_tries="$${3:-12}"; i=1; \
		while [ $$i -le $$max_tries ]; do \
			if lsof -ti tcp:$$port >/dev/null 2>&1; then \
				echo "$$name ready on :$$port"; \
				return 0; \
			fi; \
			sleep 1; \
			i=$$((i + 1)); \
		done; \
		echo "$$name failed to come up on :$$port"; \
		return 1; \
	}; \
	if lsof -ti tcp:9200 >/dev/null 2>&1; then \
		echo "Elasticsearch already running on :9200 (noop)"; \
	elif [ -x /usr/local/opt/elasticsearch-full/bin/elasticsearch ]; then \
		echo "Starting Elasticsearch on :9200"; \
		nohup /usr/local/opt/elasticsearch-full/bin/elasticsearch -Ediscovery.type=single-node -Ehttp.port=9200 >/tmp/delbao-es.log 2>&1 & echo $$! >/tmp/delbao-es.pid; \
		wait_port 9200 "Elasticsearch" 30; \
	else \
		echo "Elasticsearch binary not found at /usr/local/opt/elasticsearch-full/bin/elasticsearch"; \
		echo "Install with: brew tap elastic/tap && brew install elastic/tap/elasticsearch-full"; \
		exit 1; \
	fi; \
	if lsof -ti tcp:3001 >/dev/null 2>&1; then \
		echo "API service already running on :3001 (noop)"; \
	else \
		echo "Starting API service on :3001"; \
		cd api && nohup node server.js >/tmp/delbao-api.log 2>&1 & echo $$! >/tmp/delbao-api.pid; \
		wait_port 3001 "API service"; \
	fi; \
	if lsof -ti tcp:4000 >/dev/null 2>&1; then \
		echo "Jekyll already running on :4000 (noop)"; \
	else \
		echo "Starting Jekyll on :4000"; \
		nohup env PAGES_DISABLE_GITHUB_METADATA=true bundle exec jekyll serve >/tmp/delbao-jekyll.log 2>&1 & echo $$! >/tmp/delbao-jekyll.pid; \
		wait_port 4000 "Jekyll"; \
	fi
	@$(MAKE) status-private-stack

stop-private-stack:
	@if lsof -ti tcp:9200 >/dev/null 2>&1; then echo "Stopping Elasticsearch on :9200"; kill $$(lsof -ti tcp:9200) >/dev/null 2>&1 || true; else echo "Elasticsearch is not running (noop)"; fi
	@if lsof -ti tcp:3001 >/dev/null 2>&1; then echo "Stopping API service on :3001"; kill $$(lsof -ti tcp:3001) >/dev/null 2>&1 || true; else echo "API service is not running (noop)"; fi
	@if lsof -ti tcp:4000 >/dev/null 2>&1; then echo "Stopping Jekyll on :4000"; kill $$(lsof -ti tcp:4000) >/dev/null 2>&1 || true; else echo "Jekyll is not running (noop)"; fi

restart-private-stack:
	@if [ "$(SERVICE)" = "all" ]; then \
		$(MAKE) stop-private-stack; \
		$(MAKE) serve-private-stack; \
	elif [ "$(SERVICE)" = "es" ]; then \
		if lsof -ti tcp:9200 >/dev/null 2>&1; then kill $$(lsof -ti tcp:9200) >/dev/null 2>&1 || true; fi; \
		$(MAKE) serve-private-stack; \
	elif [ "$(SERVICE)" = "api" ]; then \
		if lsof -ti tcp:3001 >/dev/null 2>&1; then kill $$(lsof -ti tcp:3001) >/dev/null 2>&1 || true; fi; \
		$(MAKE) serve-private-stack; \
	elif [ "$(SERVICE)" = "jekyll" ]; then \
		if lsof -ti tcp:4000 >/dev/null 2>&1; then kill $$(lsof -ti tcp:4000) >/dev/null 2>&1 || true; fi; \
		$(MAKE) serve-private-stack; \
	else \
		echo "Unsupported SERVICE='$(SERVICE)'. Use: all | es | api | jekyll"; \
		exit 1; \
	fi

status-private-stack:
	@echo "Service status:"
	@if lsof -ti tcp:9200 >/dev/null 2>&1; then echo "  Elasticsearch: running (:9200)"; else echo "  Elasticsearch: stopped"; fi
	@if lsof -ti tcp:3001 >/dev/null 2>&1; then echo "  API service:   running (:3001)"; else echo "  API service:   stopped"; fi
	@if lsof -ti tcp:4000 >/dev/null 2>&1; then echo "  Jekyll:        running (:4000)"; else echo "  Jekyll:        stopped"; fi

serve-private-search: serve-private-stack

stop-private-search: stop-private-stack

restart-private-search:
	@$(MAKE) restart-private-stack SERVICE="$(if $(filter search,$(SERVICE)),api,$(SERVICE))"

status-private-search: status-private-stack
