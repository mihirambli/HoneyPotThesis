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

// sql_injection probes. The trap is POST-gated, so these are the only non-GET requests.
//
// Two factors crossed, because a measured 2x2 showed they pull in opposite directions and a
// single hit/miss pair confounds them:
//
//   outcome   hit vs. clean login. `admin'` matches signature #11 of 22 and stops the scan
//             there; a clean login walks all 22 against `username` then all 22 against
//             `password`. That 33-comparison difference turned out to cost nothing measurable,
//             because most signatures are longer than a real field value and are rejected on
//             length before a character is compared.
//   encoding  whether the body needs percent-decoding. This is what actually costs: url_decode's
//             substitution path runs over every body pair and again inside sqli_normalize.
//
// The `%27` form of each payload decodes to exactly the plain form, so the encoded and plain arms
// of the same outcome scan identical bytes and differ only in decoding work. Re-check the hit
// payloads if the `signatures` list in config.json is ever reordered.
const SQLI_PATH = __ENV.SQLI_PATH || '/api/login';
const SQLI_HIT_BODY = __ENV.SQLI_HIT_BODY || "username=admin'&password=x";
const SQLI_HIT_ENC_BODY = __ENV.SQLI_HIT_ENC_BODY || 'username=admin%27&password=x';
const SQLI_MISS_BODY = __ENV.SQLI_MISS_BODY || 'username=alice&password=secret';
const SQLI_MISS_ENC_BODY = __ENV.SQLI_MISS_ENC_BODY || 'username=alice%27&password=secret';

// Per-phase Trends keep WADM injection vs. detection latency distinguishable in k6's summary;
// the built-in http_req_duration would aggregate all calls into one distribution.
const injectGetDuration = new Trend('inject_get_duration', true);
const detectQueryDuration = new Trend('detect_query_duration', true);
const tokenTamperDuration = new Trend('token_tamper_duration', true);
const tokenDecoyDuration = new Trend('token_decoy_duration', true);
const sqliHitDuration = new Trend('sqli_hit_duration', true);
const sqliHitEncDuration = new Trend('sqli_hit_enc_duration', true);
const sqliMissDuration = new Trend('sqli_miss_duration', true);
const sqliMissEncDuration = new Trend('sqli_miss_enc_duration', true);

// k6's default summary carries only min/avg/med/max/p(90)/p(95). The quartiles are what let the
// baseline-vs-WADM plots draw a *true* box (Q1/median/Q3) from the summary alone; the alternative,
// `--out json`, would emit hundreds of megabytes at 500 VUs.
const TREND_STATS = [
  'count', 'min', 'p(5)', 'p(25)', 'med', 'p(75)', 'p(90)', 'p(95)', 'max', 'avg',
];

// startTime gives slower-starting edges (Envoy, WASM) time to finish initialising
// before the first request fires, preventing connection-refused errors that skew min/avg.
export const options = {
  summaryTrendStats: TREND_STATS,
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

// The trap answers 500 on a signature hit and 401 on a clean login, while the baseline tiers have
// no trap at all and fall through to the origin's 404. All three are expected outcomes, so all
// three must count as non-failures or http_req_failed would read as a broken run.
const SQLI_REQUEST = {
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  responseCallback: http.expectedStatuses(200, 401, 404, 500),
};

export default function () {
  // 1. Baseline HTML response: html_comments body injection, plus the header/cookie baits and
  //    the decoy link that are planted on every page ("/*").
  const getRes = http.get(`${TARGET}/`);
  injectGetDuration.add(getRes.timings.duration);

  // 2. html_comments detection. Keyword in the query string is the one surface ALL four edges
  //    inspect, and they parse it identically (ordered segments, %XX/+ decoding, raw-segment
  //    strip — see docs/EDGE_LEVELING.md), so this measurement is cross-edge comparable.
  //    POST-body inspection in OpenResty/Envoy+Lua is gated behind the config.json
  //    `post_body_inspection` flag (default off) so all four edges do equal detection work.
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

  // 5-8. sql_injection, all four arms. The trap is detection-only — it plants nothing, so it has
  //      no injection phase. The edge labels each sample by outcome and by whether the body
  //      needed decoding, which is what separates scan depth from normalisation cost.
  const sqliHitRes = http.post(`${TARGET}${SQLI_PATH}`, SQLI_HIT_BODY, SQLI_REQUEST);
  sqliHitDuration.add(sqliHitRes.timings.duration);

  const sqliHitEncRes = http.post(`${TARGET}${SQLI_PATH}`, SQLI_HIT_ENC_BODY, SQLI_REQUEST);
  sqliHitEncDuration.add(sqliHitEncRes.timings.duration);

  const sqliMissRes = http.post(`${TARGET}${SQLI_PATH}`, SQLI_MISS_BODY, SQLI_REQUEST);
  sqliMissDuration.add(sqliMissRes.timings.duration);

  const sqliMissEncRes = http.post(`${TARGET}${SQLI_PATH}`, SQLI_MISS_ENC_BODY, SQLI_REQUEST);
  sqliMissEncDuration.add(sqliMissEncRes.timings.duration);

  sleep(1);
}

// http_req_duration is the pooled headline (all eight requests); the custom Trends keep the
// per-request-type breakdown. The per-type ones are what the overhead decomposition sums, because
// the four POSTs are short-circuited by the trap on three of four edges and are therefore *faster*
// than their baseline counterparts — scaling the pooled median by the request count would let
// that saving cancel out the overhead the GETs add.
const REPORTED_TRENDS = [
  'http_req_duration',
  'inject_get_duration',
  'detect_query_duration',
  'token_tamper_duration',
  'token_decoy_duration',
  'sqli_hit_duration',
  'sqli_hit_enc_duration',
  'sqli_miss_duration',
  'sqli_miss_enc_duration',
];

// k6 stat key -> JSON key. Parentheses are stripped so the result files stay easy to index.
const STAT_KEYS = {
  count: 'count',
  min: 'min',
  'p(5)': 'p5',
  'p(25)': 'p25',
  med: 'med',
  'p(75)': 'p75',
  'p(90)': 'p90',
  'p(95)': 'p95',
  max: 'max',
  avg: 'avg',
};

function metricValue(metric, key) {
  if (!metric || !metric.values || metric.values[key] === undefined) return null;
  return metric.values[key];
}

function trendStats(metric) {
  if (!metric) return null;
  const out = {};
  for (const k6Key of Object.keys(STAT_KEYS)) {
    out[STAT_KEYS[k6Key]] = metricValue(metric, k6Key);
  }
  return out;
}

// The edges already publish their internal microsecond timings through `docker compose logs`;
// this reuses that transport for k6's end-to-end numbers rather than adding a writable mount
// (which would also mean the container writing into the repo as a different uid).
//
// The sentinel and its JSON must stay on ONE line: `docker compose logs` prefixes every line
// with `load-tester-1  | `, so anchoring the scraper on the sentinel and taking the rest of the
// line is what makes that prefix harmless. The payload is ~1 KB, well under the log driver's
// 16 KB line-split threshold.
export function handleSummary(data) {
  const trends = {};
  for (const name of REPORTED_TRENDS) {
    trends[name] = trendStats(data.metrics[name]);
  }

  const payload = {
    vus: parseInt(__ENV.K6_VUS || '5', 10),
    duration: __ENV.K6_DURATION || '30s',
    iterations: metricValue(data.metrics.iterations, 'count'),
    http_reqs: metricValue(data.metrics.http_reqs, 'count'),
    http_req_failed_rate: metricValue(data.metrics.http_req_failed, 'rate'),
    trends: trends,
  };

  // Defining handleSummary replaces k6's built-in end-of-test table, so a short human digest is
  // reprinted here to keep a manual `docker compose up --abort-on-container-exit` readable.
  const digest = REPORTED_TRENDS.map(function (name) {
    const med = trends[name] && trends[name].med !== null ? trends[name].med.toFixed(2) : 'n/a';
    return `  ${name}: med=${med}ms`;
  }).join('\n');

  const failedPct = ((payload.http_req_failed_rate || 0) * 100).toFixed(2);

  return {
    stdout:
      `\nk6 done: ${payload.iterations} iterations, ${payload.http_reqs} requests, `
      + `${failedPct}% failed\n${digest}\n`
      + `WADM K6 SUMMARY ${JSON.stringify(payload)}\n`,
  };
}
