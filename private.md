---
layout: default
title: Private posts
permalink: /private/
comments: false
avatar: true
---

<section
  class="private-home-search"
  data-private-home-search
  data-search-api-url="{{ site.search_api_url | default: 'http://localhost:3001/api/search' }}"
  data-search-index="{{ site.search_index_name | default: 'private_posts' }}"
  data-search-page-path="{{ site.baseurl }}/search/"
  data-site-baseurl="{{ site.baseurl }}"
>
  <label class="private-home-search-label" for="private-home-search-input">Search private posts</label>
  <input
    id="private-home-search-input"
    class="private-home-search-input"
    type="search"
    placeholder="Search by title or content"
    autocomplete="off"
    data-role="search-input"
  />

  <div class="private-home-search-panel" data-role="results-panel" hidden>
    <p class="private-home-search-status" data-role="results-status"></p>
    <ul class="private-home-search-results" data-role="results-list"></ul>
    <p class="private-home-search-empty" data-role="results-empty" hidden>No matching posts yet.</p>
    <a class="private-home-search-footer" data-role="results-footer" href="{{ site.baseurl }}/search/"></a>
  </div>
</section>

{% assign private_posts = site.private_posts | sort: "date" | reverse %}
{% include post-list.html posts=private_posts %}

<script type="module" src="{{ site.baseurl }}/assets/js/home-search.js"></script>
