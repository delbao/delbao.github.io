import { runSingleQuery } from "./search-client.js";

const SEARCH_DEBOUNCE_MS = 220;

function withBaseUrl(url, baseurl) {
  if (!url.startsWith("/")) {
    return url;
  }
  if (!baseurl) {
    return url;
  }
  return `${baseurl.replace(/\/$/, "")}${url}`;
}

function renderResults(container, results, query, searchPagePath, baseurl) {
  const list = container.querySelector("[data-role='results-list']");
  const footer = container.querySelector("[data-role='results-footer']");
  const empty = container.querySelector("[data-role='results-empty']");

  list.innerHTML = "";
  empty.hidden = results.length > 0;

  results.forEach((result) => {
    const li = document.createElement("li");
    li.className = "home-search-result-item";

    const link = document.createElement("a");
    link.className = "home-search-result-link";
    link.href = withBaseUrl(result.url, baseurl);

    const title = document.createElement("span");
    title.className = "home-search-result-title";
    title.textContent = result.title;

    const meta = document.createElement("span");
    meta.className = "home-search-result-meta";
    meta.textContent = [result.date, result.excerpt].filter(Boolean).join(" - ");

    link.appendChild(title);
    if (meta.textContent) {
      link.appendChild(meta);
    }
    li.appendChild(link);
    list.appendChild(li);
  });

  const encodedQuery = encodeURIComponent(query);
  footer.href = `${searchPagePath}?q=${encodedQuery}`;
  footer.textContent = `See all results for "${query}"`;
}

function initHomeSearch() {
  const root = document.querySelector("[data-private-home-search]");
  if (!root) return;

  const apiUrl = root.dataset.searchApiUrl;
  const indexName = root.dataset.searchIndex || "private_posts";
  const searchPagePath = root.dataset.searchPagePath || "/search/";
  const baseurl = root.dataset.siteBaseurl || "";

  const input = root.querySelector("[data-role='search-input']");
  const panel = root.querySelector("[data-role='results-panel']");
  const status = root.querySelector("[data-role='results-status']");

  let debounceTimer = null;
  let currentController = null;

  const hidePanel = () => {
    panel.hidden = true;
    status.textContent = "";
  };

  input.addEventListener("input", () => {
    const query = input.value.trim();

    if (debounceTimer) {
      window.clearTimeout(debounceTimer);
    }

    if (currentController) {
      currentController.abort();
      currentController = null;
    }

    if (!query) {
      hidePanel();
      return;
    }

    panel.hidden = false;
    status.textContent = "Searching...";

    debounceTimer = window.setTimeout(async () => {
      currentController = new AbortController();
      try {
        const results = await runSingleQuery({
          apiUrl,
          indexName,
          query,
          hitsPerPage: 6,
          signal: currentController.signal,
        });
        renderResults(root, results, query, searchPagePath, baseurl);
        status.textContent = "";
      } catch (error) {
        if (error.name === "AbortError") {
          return;
        }
        status.textContent = "Search is temporarily unavailable.";
      } finally {
        currentController = null;
      }
    }, SEARCH_DEBOUNCE_MS);
  });

  document.addEventListener("click", (event) => {
    if (!root.contains(event.target)) {
      hidePanel();
    }
  });

  input.addEventListener("focus", () => {
    if (input.value.trim()) {
      panel.hidden = false;
    }
  });
}

initHomeSearch();
