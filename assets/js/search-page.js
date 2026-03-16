import instantsearch from "https://cdn.jsdelivr.net/npm/instantsearch.js@4/+esm";
import { configure, hits, pagination, searchBox } from "https://cdn.jsdelivr.net/npm/instantsearch.js@4/es/widgets/+esm";
import { createSearchkitProxyClient } from "./search-client.js";

function buildRouter(indexName) {
  return {
    router: instantsearch.routers.history({
      createURL({ location, routeState }) {
        const url = new URL(location.href);
        const query = routeState.q || "";
        if (query) {
          url.searchParams.set("q", query);
        } else {
          url.searchParams.delete("q");
        }
        return `${url.pathname}${url.search}${url.hash}`;
      },
      parseURL({ location }) {
        const url = new URL(location.href);
        return { q: url.searchParams.get("q") || "" };
      },
    }),
    stateMapping: {
      stateToRoute(uiState) {
        const query = uiState[indexName] && uiState[indexName].query ? uiState[indexName].query : "";
        return { q: query };
      },
      routeToState(routeState) {
        return {
          [indexName]: {
            query: routeState.q || "",
          },
        };
      },
    },
  };
}

function initSearchPage() {
  const root = document.querySelector("[data-search-page]");
  if (!root) return;

  const apiUrl = root.dataset.searchApiUrl;
  const indexName = root.dataset.searchIndex || "private_posts";
  const baseurl = root.dataset.siteBaseurl || "";

  const searchClient = createSearchkitProxyClient({ apiUrl });
  const routing = buildRouter(indexName);

  const search = instantsearch({
    indexName,
    searchClient,
    routing,
  });

  search.addWidgets([
    configure({
      hitsPerPage: 12,
      attributesToSnippet: ["content:28"],
      snippetEllipsisText: "...",
    }),
    searchBox({
      container: "#serp-searchbox",
      placeholder: "Search private posts",
      showReset: true,
      showSubmit: true,
    }),
    hits({
      container: "#serp-hits",
      templates: {
        empty(results, { html }) {
          const query = results.query ? ` for "${results.query}"` : "";
          return html`<p>No results${query}.</p>`;
        },
        item(hit, { html, components }) {
          const resultUrl = typeof hit.url === "string" && hit.url.startsWith("/") && baseurl
            ? `${baseurl.replace(/\/$/, "")}${hit.url}`
            : hit.url;
          return html`
            <article class="serp-hit-item">
              <h2 class="serp-hit-title">
                <a href="${resultUrl}">${components.Highlight({ hit, attribute: "title" })}</a>
              </h2>
              ${hit.date ? html`<p class="serp-hit-date">${hit.date}</p>` : ""}
              <p class="serp-hit-snippet">${components.Snippet({ hit, attribute: "content" })}</p>
            </article>
          `;
        },
      },
    }),
    pagination({ container: "#serp-pagination" }),
  ]);

  search.start();
}

initSearchPage();
