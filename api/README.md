# API Service

Small Express service that currently handles:

- browser search requests to Elasticsearch through Searchkit
- async post actions by spawning Python job runners

## Run locally

```bash
cd api
npm install
cp .env.example .env
npm start
```

The async ANKI job uses `../preprocessing/anki_job.py`, so make sure the
repository Python environment has `preprocessing/requirements.txt` installed and
the relevant LiteLLM provider credentials are exported in your shell.

ANKI prompt templates live under `../preprocessing/prompts/` and default to the
`anki_cards_{system,user}.txt` pair.

By default it serves:

- `POST /api/search`
- `POST /api/llm-jobs`
- `GET /api/llm-jobs/:jobId`
- `GET /health`
