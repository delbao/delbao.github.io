.PHONY: prepare-media build serve

PYTHON ?= python3
TRANSCRIPT ?=
MODEL ?= gpt-4.1-mini

prepare-media:
	mkdir -p assets/posts_media
	rsync -av --delete --prune-empty-dirs "_posts/media/" "assets/posts_media/" || true

build: prepare-media
	bundle exec jekyll build

serve: prepare-media
	bundle exec jekyll serve

summarize-transcript:
	test -n "$(TRANSCRIPT)"
	$(PYTHON) preprocessing/summarize_transcript.py "$(TRANSCRIPT)" --model "$(MODEL)"
