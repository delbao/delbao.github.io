const express = require("express");
const cors = require("cors");
const dotenv = require("dotenv");
const Searchkit = require("@searchkit/api").default;

dotenv.config();

const port = Number(process.env.PORT || 3001);
const indexName = process.env.SEARCHKIT_INDEX || "private_posts";

const auth = (() => {
  if (process.env.ELASTICSEARCH_API_KEY) {
    return { apiKey: process.env.ELASTICSEARCH_API_KEY };
  }
  if (process.env.ELASTICSEARCH_USERNAME && process.env.ELASTICSEARCH_PASSWORD) {
    return {
      username: process.env.ELASTICSEARCH_USERNAME,
      password: process.env.ELASTICSEARCH_PASSWORD,
    };
  }
  return undefined;
})();

const client = Searchkit(
  {
    connection: {
      host: process.env.ELASTICSEARCH_URL || "http://localhost:9200",
      auth,
    },
    search_settings: {
      search_attributes: [{ field: "title", weight: 5 }, { field: "content", weight: 1 }, "tags", "categories"],
      result_attributes: ["id", "title", "url", "date", "content", "tags", "categories"],
      highlight_attributes: ["title", "content"],
    },
  },
  {
    debug: process.env.SEARCHKIT_DEBUG === "1",
  }
);

const app = express();
const allowedOrigins = (process.env.SEARCH_ALLOWED_ORIGINS || "")
  .split(",")
  .map((origin) => origin.trim())
  .filter(Boolean);

app.use(express.json({ limit: "1mb" }));
app.use(
  cors(
    allowedOrigins.length > 0
      ? {
          origin(origin, callback) {
            if (!origin || allowedOrigins.includes(origin)) {
              callback(null, true);
              return;
            }
            callback(new Error(`Origin not allowed: ${origin}`));
          },
        }
      : undefined
  )
);

app.get("/health", (_req, res) => {
  res.json({ ok: true, indexName });
});

app.post("/api/search", async (req, res) => {
  try {
    const requestBody = Array.isArray(req.body) ? req.body : req.body && req.body.requests;
    if (!Array.isArray(requestBody)) {
      res.status(400).json({ error: "Expected request body to contain an InstantSearch request array" });
      return;
    }
    const results = await client.handleRequest(requestBody);
    res.json(results);
  } catch (error) {
    console.error("Search request failed", error);
    res.status(500).json({ error: "Search request failed" });
  }
});

app.listen(port, () => {
  console.log(`Search API listening on http://localhost:${port} (index: ${indexName})`);
});
