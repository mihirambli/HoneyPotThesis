import http from 'k6/http';
import { sleep } from 'k6';
import { Trend } from 'k6/metrics';

const TARGET = __ENV.TARGET || 'http://openresty:80';
const TRIGGER_KEYWORD = __ENV.TRIGGER_KEYWORD || 'internal-admin.example.com';

// Surfaces for the four non-html_comments honeytoken kinds. Defaults mirror config.json;
// override per-run if the config's token definitions change.
const HEADER_KEYWORD = __ENV.HEADER_KEYWORD || 'app-07.internal.example.com';
const COOKIE_NAME = __ENV.COOKIE_NAME || 'admin_ui';
const COOKIE_TAMPER_VALUE = __ENV.COOKIE_TAMPER_VALUE || '1';
const DECOY_PATH = __ENV.DECOY_PATH || '/api/v1/debug';
const FORM_PAGE = __ENV.FORM_PAGE || '/login.html';
const FORM_FIELD = __ENV.FORM_FIELD || 'is_admin';
const FORM_TAMPER_VALUE = __ENV.FORM_TAMPER_VALUE || '1';

// Per-phase Trends keep WADM injection vs. detection latency distinguishable in k6's summary;
// the built-in http_req_duration would aggregate all calls into one distribution.
const injectGetDuration = new Trend('inject_get_duration', true);
const detectQueryDuration = new Trend('detect_query_duration', true);
const tokenTamperDuration = new Trend('token_tamper_duration', true);
const tokenDecoyDuration = new Trend('token_decoy_duration', true);

// startTime gives slower-starting edges (Envoy, WASM) time to finish initialising
// before the first request fires, preventing connection-refused errors that skew min/avg.
export const options = {
  scenarios: {
    default: {
      executor: 'constant-vus',
      vus: parseInt(__ENV.K6_VUS || '5', 10),
      duration: __ENV.K6_DURATION || '30s',
      startTime: __ENV.K6_START_DELAY || '5s',
    },
  },
};

// 404 is expected for the two edge-only paths (/api/login and the decoy trap have no backend
// route); marking it acceptable keeps http_req_failed meaningful.
const ALLOW_404 = { responseCallback: http.expectedStatuses(200, 404) };

export default function () {
  // 1. Baseline HTML response: html_comments body injection, plus the header/cookie baits and
  //    the decoy link that are planted on every page ("/*").
  const getRes = http.get(`${TARGET}/`);
  injectGetDuration.add(getRes.timings.duration);

  // 2. html_comments detection. Keyword in the query string is the one surface ALL four edges
  //    already inspect (OpenResty get_uri_args, Envoy+Lua parse_query_string, Apache r.args,
  //    WASM :path substring), so this measurement is cross-edge comparable. POST-body inspection
  //    in OpenResty/Envoy+Lua is gated behind the config.json `post_body_inspection` flag
  //    (default off) so all four edges do equal detection work.
  const detectRes = http.get(
    `${TARGET}/api/login?password=${encodeURIComponent(TRIGGER_KEYWORD)}`,
    ALLOW_404,
  );
  detectQueryDuration.add(detectRes.timings.duration);

  // 3. Three kinds fire on one request, each timed in its own region by the edge:
  //    form_fields (hidden input returned with a changed value), http_headers (the planted
  //    backend-server value replayed in the query), cookies (bait cookie returned tampered).
  //    /login.html also carries a </form>, so it is the page where form_fields injection runs.
  const tamperRes = http.get(
    `${TARGET}${FORM_PAGE}?${FORM_FIELD}=${encodeURIComponent(FORM_TAMPER_VALUE)}`
      + `&probe=${encodeURIComponent(HEADER_KEYWORD)}`,
    { headers: { Cookie: `${COOKIE_NAME}=${COOKIE_TAMPER_VALUE}` } },
  );
  tokenTamperDuration.add(tamperRes.timings.duration);

  // 4. decoy_paths detection: any request to the advertised trap path is a hit, no keyword needed.
  const decoyRes = http.get(`${TARGET}${DECOY_PATH}`, ALLOW_404);
  tokenDecoyDuration.add(decoyRes.timings.duration);

  sleep(1);
}
