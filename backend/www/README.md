<!-- backend/www/README.md: static document root for the origin container. -->
# backend/www (origin document root)

Static HTML for the dummy "victim" application. The `backend` Compose service (`nginx:alpine`)
mounts this **whole directory** read-only at `/usr/share/nginx/html`, so any `.html` file dropped
here is served automatically — no Compose change needed to add a page.

## Data flow

These pages carry **no** honeytoken logic. A client request reaches the edge proxy first
(OpenResty/Envoy/Apache), which runs WADM detection on the way in and injects honeytokens into
the HTML on the way back out, then proxies to `backend:80`, which simply returns the file here.

## Pages

| Path | Purpose | Has `<form>`? |
|------|---------|---------------|
| `/` (`index.html`) | Landing page | no |
| `/login.html` | Login form | **yes** |
| `/dashboard.html` | User dashboard tiles | no |
| `/admin.html` | Admin settings form | **yes** |
| `/about.html` | Static info | no |

## Deploying honeytokens on a page

Choose where a token is planted with the `paths` (or `advertise_on_paths`) field in the repo-root
`config.json`. Matching is against the request URI: use `"/*"` for every page or the **exact**
path, e.g. `"/admin.html"`. Note the homepage is requested as `"/"`, not `"/index.html"`.
The `form_fields` kind only injects on pages that contain a `</form>` (currently `/login.html`
and `/admin.html`).
