# Search API

Small Express service that proxies browser search requests to Elasticsearch through Searchkit.

## Run locally

```bash
cd search-api
npm install
cp .env.example .env
npm start
```

By default it serves:

- `POST /api/search`
- `GET /health`
