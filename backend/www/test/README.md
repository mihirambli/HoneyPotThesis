# backend/www/test (generated test pages)

Pages the origin serves only for the benchmark's response-size sweep and the functional matrix.
Everything here except this README is **generated** by `backend/generate_test_pages.py` and
gitignored: the content is fully determined by that script (megabytes of filler and seeded random
bytes), so it is regenerated rather than committed.

## Data flow

    backend/generate_test_pages.py  -> backend/www/test/*          (run by the benchmark runners)
    backend container               serves /usr/share/nginx/html/test/ (read-only mount)
    edge (bare / silent / wadm)     proxies /test/... like any other page
    benchmarks/test.js  (WADM_MIX=size)   size-1k/16k/256k/768k.html  — injection cost vs. body size
    benchmarks/functional_matrix.py       every file below            — does WADM break it?

## Files

| File | Purpose |
|---|---|
| `size-1k.html` … `size-768k.html` | Exact-size HTML pages for the size sweep. All below Envoy's default 1 MiB buffer limit. |
| `size-2m.html` | Above that limit: a functional case, not a latency one. |
| `no-body-close.html` | HTML with no `</body>`, so the comment token has no anchor. |
| `gz/page.html` | Served gzip-encoded (`location /test/gz/` in `backend/nginx.conf`). |
| `data.json` | Non-HTML text; no edge should modify it. |
| `blob.bin` | 32 KiB of seeded random bytes; no edge should modify it. |

`/test/empty` is not a file: `backend/nginx.conf` answers it with a bodiless 204.
