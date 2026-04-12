# Blog

My blog. Jekyll-based.

# Workflow

## 1. Write up your post
```
$ ~/my/
$ git clone git@github.com:delbao/delbao.github.io.git
$ cd delbao.github.io
$ git submodule update --init _posts
```

```
cd \_posts

$ git commit ...

# run the script to commit / push at the parent level
$ commit_submodule.sh

```

## 2. Diagram
ChatGPT -> Mermaid -> Exclidraw

Add media files to \_posts/media/. At build time, Github Actions copy them to
/assets/posts_media/

Local builds use the same explicit copy step. This repository keeps that logic
outside Jekyll as a workflow step rather than a custom plugin so local behavior
matches GitHub Actions and GitHub Pages remains on the standard Pages build
path.

## 3. Formatting

## 4. Private posts

Private posts live in the `_private_posts/` submodule.

The site uses one shared Jekyll build:

```bash
bundle exec jekyll build
```

When `_private_posts/` is available, Jekyll renders it as the `private_posts`
collection. Those documents are output under `/private/...`.

GitHub Actions and private environments use the same Jekyll config, but they do
not use the same checkout step.

GitHub Actions builds the public site with this flow:

```bash
git submodule update --init _posts
make build
```

That workflow intentionally does not initialize `_private_posts`.
This matters because `_private_posts` is a private submodule, and GitHub Pages
deployments should not require private repository credentials.

As a result, the GitHub Actions build behaves like this:

- `_posts` is present and contributes normal public posts.
- `_posts/media` is copied into `assets/posts_media/` before the build.
- `_private_posts` is absent, so the `private_posts` collection contributes no
  documents.
- Jekyll still uses the same `_config.yml`; the difference is only which
  submodules were checked out before the build.

Local or VPS environments that have access to the private repository can
initialize both submodules before building:

```bash
git submodule update --init _posts _private_posts
make build
```

That private build path behaves like this:

- `_posts` contributes the public site content.
- `_private_posts` is available, so Jekyll renders the `private_posts`
  collection as `/private/...`.
- The build command stays the same; only the checkout step changes.

In short: same build, different checkout.

For local development, use:

```bash
make serve
```

That target mirrors the GitHub Actions media preparation step before starting
Jekyll.

## 4.1 Private posts search (Searchkit + Elasticsearch)

Private-post search now uses a small API service:

- Homepage live search is wired in `private.md` and
  `assets/js/home-search.js`.
- The dedicated SERP page is `search.md` (served at `/search/`) and uses
  `assets/js/search-page.js` with URL-synced `?q=` state.
- Shared browser search transport lives in `assets/js/search-client.js`.
- Search and job backend lives in `api/server.js`.
- Reindexing private posts into Elasticsearch is done by
  `preprocessing/index_private_posts.py`.

### Local run commands

1. Start Elasticsearch:

```bash
docker run --name private-posts-es \
  -p 9200:9200 \
  -e discovery.type=single-node \
  -e xpack.security.enabled=false \
  docker.elastic.co/elasticsearch/elasticsearch:8.12.2
```

2. Reindex private posts:

```bash
source .venv/bin/activate
pip install -r preprocessing/requirements.txt
make reindex-private-posts
```

3. Start the API service:

```bash
cd api
npm install
cp .env.example .env
npm start
```

4. Start Jekyll site:

```bash
cd ..
make serve
```

Default local wiring expects:

- Jekyll at `http://localhost:4000`
- API service at `http://localhost:3001`
- Elasticsearch at `http://localhost:9200`

### Async post actions with LLM jobs

Post pages now include a small "Study Tools" panel that can submit an async LLM
job using the current post as context. The HTTP layer lives in `api/server.js`,
and the ANKI generation job itself runs in Python via
`preprocessing/anki_job.py`.

To enable it locally:

1. Create the Python env and install preprocessing dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r preprocessing/requirements.txt
```

2. Copy the API env file:

```bash
cd api
cp .env.example .env
```

3. Set your model credentials in the shell environment for LiteLLM, for
example:

```bash
OPENAI_API_KEY=...
```

4. Start the API and Jekyll:

```bash
npm install
npm start
cd ..
make serve
```

Then open a post page and use the "Generate ANKI CSV" button. The browser will
submit a background job to `http://localhost:3001/api/llm-jobs`, poll for
completion, and expose the CSV as a downloadable file when ready. The API
spawns the Python job runner, which calls the model through LiteLLM and returns
CSV output back to the UI through the API.

### One-command local stack (with no-op detection)

Use:

```bash
make serve-private-stack
```

This ensures all three services are up:

- Elasticsearch on `:9200`
- API service on `:3001`
- Jekyll on `:4000`

If any service is already running, the target no-ops for that service.

Check status anytime:

```bash
make status-private-stack
```

### Manual restart commands

Restart everything:

```bash
make restart-private-stack
```

Restart only one service:

```bash
make restart-private-stack SERVICE=es
make restart-private-stack SERVICE=api
make restart-private-stack SERVICE=jekyll
```

Stop everything:

```bash
make stop-private-stack
```
