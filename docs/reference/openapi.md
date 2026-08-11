---
hide:
  - navigation
  - toc
---

# OpenAPI explorer

Every route, request body and response, rendered from
[`openapi.json`](openapi.json) — the document CI regenerates from the running
application and refuses to let drift. [API endpoints](api.md) is the curated
version of the same surface, with the judgement a specification cannot carry.

<!--
Scalar is pinned to an exact version and to the exact bytes of that version.
An unpinned CDN URL on a documentation site is a third-party script that can
change under you, on a page about a service that holds money.

`integrity` makes the browser refuse anything but these bytes, so the pin is
enforced by the reader's browser rather than by trust in a CDN. To move to a
newer Scalar: change both the version in the URL and the hash, which is

    curl -sL <the new URL> | openssl dgst -sha384 -binary | openssl base64 -A

`withDefaultFonts: false` stops the bundle fetching webfonts from
fonts.scalar.com, so loading the page makes no third-party request beyond the
script itself. No proxy is configured, so "Test Request" talks to the server in
the specification directly or not at all.

Scalar's own toolbar has an "Ask AI" button this version offers no flag to
remove. Clicking it uploads the OpenAPI document to Scalar's servers, after an
explicit terms prompt. That document is public and committed, so there is
nothing there to leak — but it is a reader's deliberate choice, not something
the page does.

The style block is scoped to this page and is the minimum the embed needs:
Material's content column is sized for prose and an API explorer inside it is
unusable. It is the only custom CSS on the site.
-->

<style>
  .md-content__inner { padding-right: 0; padding-left: 0; }
  .md-content__inner > h1,
  .md-content__inner > p { padding-right: .8rem; padding-left: .8rem; }
  .md-main__inner { max-width: none; }
  #scalar-api-reference { min-height: 60vh; }
</style>

<div id="scalar-api-reference"></div>

<script
  src="https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.64.1/dist/browser/standalone.js"
  integrity="sha384-yNQdqLDpE2fst+aUqSHXcquVibo90vCkT+zBMLgYfCejLv85GXAR3tFg9lXDUJAd"
  crossorigin="anonymous"
></script>
<script>
  // Two things that are easy to get wrong here.
  //
  // The mount element must NOT be called `openapi-explorer`: the heading above
  // is `# OpenAPI explorer`, and toc gives it exactly that slug as its id.
  // Scalar resolves the selector, finds the heading first, and mounts the whole
  // explorer inside the page title.
  //
  // `../` because use_directory_urls puts this page at /reference/openapi/
  // while the document it renders is at /reference/openapi.json.
  window.Scalar.createApiReference('#scalar-api-reference', {
    url: '../openapi.json',
    withDefaultFonts: false,
    hideDarkModeToggle: true,
    darkMode: document.body.getAttribute('data-md-color-scheme') === 'slate',
  });
</script>

!!! note "If this page is blank"

    The explorer is client-side JavaScript. With scripts blocked, read
    [`openapi.json`](openapi.json) directly — it is the same content, and it is
    what the generated clients are built from.
