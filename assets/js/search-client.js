function normalizeResult(hit) {
  const content = typeof hit.content === "string" ? hit.content : "";
  const collapsed = content.replace(/\s+/g, " ").trim();
  const excerpt = collapsed.length > 180 ? `${collapsed.slice(0, 177)}...` : collapsed;

  return {
    id: hit.id || hit.objectID || "",
    title: hit.title || "Untitled",
    url: hit.url || "#",
    date: hit.date || "",
    excerpt,
    raw: hit,
  };
}

function buildSearchParams(params) {
  const output = {};
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    output[key] = value;
  });
  return output;
}

export function createSearchkitProxyClient({ apiUrl }) {
  async function postSearch(body, signal) {
    const response = await fetch(apiUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });

    if (!response.ok) {
      throw new Error(`Search API request failed with status ${response.status}`);
    }

    return response.json();
  }

  return {
    search(requests, signal) {
      return postSearch({ requests }, signal);
    },
    searchForFacetValues(requests, signal) {
      return postSearch({ requests }, signal);
    },
  };
}

export async function runSingleQuery({ apiUrl, indexName, query, hitsPerPage = 6, signal }) {
  const client = createSearchkitProxyClient({ apiUrl });
  const params = buildSearchParams({
    query,
    hitsPerPage,
    attributesToSnippet: ["content:20"],
  });

  const payload = await client.search([{ indexName, params }], signal);
  const firstResult = payload && Array.isArray(payload.results) ? payload.results[0] : null;
  const hits = firstResult && Array.isArray(firstResult.hits) ? firstResult.hits : [];

  return hits.map(normalizeResult);
}
