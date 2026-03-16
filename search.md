---
layout: default
title: Search private posts
permalink: /search/
comments: false
avatar: false
---

<section
  class="search-page"
  data-search-page
  data-search-api-url="{{ site.search_api_url | default: 'http://localhost:3001/api/search' }}"
  data-search-index="{{ site.search_index_name | default: 'private_posts' }}"
  data-site-baseurl="{{ site.baseurl }}"
>
  <h1>Search private posts</h1>
  <div id="serp-searchbox"></div>
  <div id="serp-hits"></div>
  <div id="serp-pagination"></div>
</section>

<script type="module" src="{{ site.baseurl }}/assets/js/search-page.js"></script>
