.PHONY: prepare-media build serve

prepare-media:
	mkdir -p assets/posts_media
	rsync -av --delete --prune-empty-dirs "_posts/media/" "assets/posts_media/" || true

build: prepare-media
	bundle exec jekyll build

serve: prepare-media
	bundle exec jekyll serve
