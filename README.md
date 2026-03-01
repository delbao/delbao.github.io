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

## 5. Transcript preprocessing

Python transcript-processing code lives under `preprocessing/`.

This repository uses `litellm` as the client layer for GPT/Gemini-style model
calls. The current summarizer reads diarized transcript `.jsonl` files from
paths like `/Users/dbao/Movies/*.jsonl`, reduces them to merged
`[HH:MM:SS-HH:MM:SS] NAME_OR_SPEAKER: text` transcript lines, asks the LLM to
guess speaker names when the transcript provides enough evidence, and writes a
final markdown summary next to the source transcript by default.

Setup:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r preprocessing/requirements.txt
```

Usage:

```bash
python preprocessing/summarize_transcript.py \
  "/Users/dbao/Movies/AI Platform - Sprint Planning - 2026-02-23 at 10.32.29 AM.webm.smart.diarization.jsonl" \
  --model "openai/gpt-4o"
```

Or through `make`:

```bash
make summarize-transcript \
  TRANSCRIPT="/Users/dbao/Movies/AI Platform - Sprint Planning - 2026-02-23 at 10.32.29 AM.webm.smart.diarization.jsonl" \
  MODEL="gemini/gemini-2.0-flash"
```

The summarizer uses LiteLLM's Python SDK `completion(...)` interface rather
than a custom provider client so the same script can switch models by changing
the model string and environment variables. See the LiteLLM repo and docs for
provider-specific model names and required API keys:
https://github.com/BerriAI/litellm

Prompt templates live under `preprocessing/prompts/` and are loaded from disk
at runtime. Speaker-name correction rules for local post-processing live in
`preprocessing/speaker_name_corrections.json`.

The transcript is sent to the model in one pass after preprocessing rather than
using chunked map-reduce summarization. The preprocessing step strips the
original JSONL structure down to merged time-range and speaker-label text
before the LLM call. That single LLM response returns both:

- a `speaker_map` JSON object
- the final `summary_markdown`

The code then applies the returned speaker map locally to produce the saved
processed transcript input. If the transcript does not support a confident
real-name guess, the speaker map falls back to the original speaker IDs.

The tool writes:

- `*.summary.md` for the final summary
- `*.llm-input.txt` for the processed transcript sent to the model

After the LLM response, the code also:

- compares guessed speaker names against the misspelled-key side of
  `speaker_name_corrections.json` with a fuzzy matching threshold, plus a
  phonetic guard for close misspellings, and rewrites them only when the
  similarity is high enough
- replaces remaining `SPEAKER_xx` references in the summary using the returned
  speaker map
- removes obvious duplicate-name artifacts such as `Del (Del)`
