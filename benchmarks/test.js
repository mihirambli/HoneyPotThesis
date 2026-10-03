import http from 'k6/http';
import exec from 'k6/execution';
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

// ── Load shape ───────────────────────────────────────────────────────────────────────────────
//
// WADM_-prefixed, NOT K6_-prefixed. Anything named K6_<option> is read by k6 itself as a CLI
// option: setting K6_VUS/K6_DURATION makes k6 synthesise its own default scenario and DISCARD the
// `scenarios` block below entirely. The previous harness did exactly that, which silently voided
// both the executor choice and the startTime warm-up guard.
const RATE = parseInt(__ENV.WADM_RATE || '10', 10);
const DURATION = __ENV.WADM_DURATION || '30s';
const START_DELAY = __ENV.WADM_START_DELAY || '5s';

// Headroom for k6 to absorb a latency rise without reducing the offered rate. Once maxVUs is
// exhausted k6 records dropped_iterations instead of quietly slowing down, which is the signal
// that separates "the edge got slower" from "we stopped asking for as much".
const PREALLOC_VUS = parseInt(__ENV.WADM_PREALLOC_VUS || String(Math.max(20, RATE * 2)), 10);
const MAX_VUS = parseInt(__ENV.WADM_MAX_VUS || String(Math.max(60, RATE * 10)), 10);

// `rotate` (default) walks the probe list by one slot per iteration, so over any multiple of
// PROBES.length every probe has occupied every position an equal number of times. `fixed`
// reproduces the original always-same-order behaviour and exists to quantify the bias it causes;
// `shuffle` is a seeded permutation, for checking that rotation itself introduces no pattern.
const ORDER = __ENV.WADM_ORDER || 'rotate';
const SEED = parseInt(__ENV.WADM_SEED || '1', 10);

// One unrecorded request at the head of each iteration. Under an arrival-rate executor a VU sits
// idle between iterations, so its first request pays connection state the other seven do not: the
// measured slot-1 median ran 1.79x slot-8 with its *minimum* also elevated, which is setup cost
// rather than queueing. Absorbing it into a discarded request leaves all eight measured probes on
// an equally warm connection. Set WADM_PRIME=0 to measure without it.
const PRIME = (__ENV.WADM_PRIME || '1') !== '0';

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

// One Trend per position in the iteration, independent of which probe landed there. With
// `rotate` these must come out equal; any residual spread is the positional artifact itself,
// measured rather than assumed, and is what makes the per-probe figures auditable.
const slotDurations = [];
for (let i = 0; i < 8; i += 1) {
  slotDurations.push(new Trend(`slot_${i + 1}_duration`, true));
}

// k6's default summary carries only min/avg/med/max/p(90)/p(95). The quartiles are what let the
// baseline-vs-WADM plots draw a *true* box (Q1/median/Q3) from the summary alone; the alternative,
// `--out json`, would emit hundreds of megabytes at high rates. p(99) is included because the
// preemption tail, not the body, is what distinguishes a contaminated run from a clean one.
const TREND_STATS = [
  'count', 'min', 'p(5)', 'p(25)', 'med', 'p(75)', 'p(90)', 'p(95)', 'p(99)', 'max', 'avg',
];

// constant-arrival-rate, not constant-vus: the offered load must be a fixed input, not an
// outcome. Under constant-vus plus a sleep the achieved rate falls as latency rises, so a slower
// tier is also measured under LIGHTER load — the two tiers being compared were not offered the
// same traffic. An arrival-rate executor holds the rate fixed and reports the shortfall as
// dropped_iterations instead.
//
// startTime gives slower-starting edges (Envoy, WASM) time to finish initialising before the
// first request fires, preventing connection-refused errors that skew min/avg.
export const options = {
  summaryTrendStats: TREND_STATS,
  scenarios: {
    default: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PREALLOC_VUS,
      maxVUs: MAX_VUS,
      startTime: START_DELAY,
      gracefulStop: '10s',
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

// The eight probes of an iteration, each with the Trend that records it. Order in this array is
// the *reference* order only — the order they actually execute in is decided per iteration by
// `probeOrder`, so no probe is permanently attached to a queue position.
//
// 1-4 are GETs: injection fires on all of them (every response is text/html and the `/*` tokens
// are planted on every page), while only the last three carry a trigger and take the detection
// hit path. 5-8 are the SQLi trap's 2x2, which plants nothing.
const PROBES = [
  {
    trend: injectGetDuration,
    run: () => http.get(`${TARGET}/`),
  },
  {
    trend: detectQueryDuration,
    run: () => http.get(
      `${TARGET}/api/login?password=${encodeURIComponent(TRIGGER_KEYWORD)}`,
      ALLOW_404,
    ),
  },
  {
    // /login.html also carries a </form>, so it is the page where form_fields injection runs.
    // Three kinds fire on this one request, each timed in its own region by the edge:
    // form_fields (hidden input returned changed), http_headers (planted value replayed in the
    // query), cookies (bait cookie returned tampered).
    trend: tokenTamperDuration,
    run: () => http.get(
      `${TARGET}${FORM_PAGE}?${FORM_FIELD}=${encodeURIComponent(FORM_TAMPER_VALUE)}`
        + `&probe=${encodeURIComponent(HEADER_KEYWORD)}`,
      { headers: { Cookie: `${COOKIE_NAME}=${COOKIE_TAMPER_VALUE}` } },
    ),
  },
  {
    // decoy_paths detection: any request to the advertised trap path is a hit, no keyword needed.
    trend: tokenDecoyDuration,
    run: () => http.get(`${TARGET}${DECOY_PATH}`, ALLOW_404),
  },
  {
    trend: sqliHitDuration,
    run: () => http.post(`${TARGET}${SQLI_PATH}`, SQLI_HIT_BODY, SQLI_REQUEST),
  },
  {
    trend: sqliHitEncDuration,
    run: () => http.post(`${TARGET}${SQLI_PATH}`, SQLI_HIT_ENC_BODY, SQLI_REQUEST),
  },
  {
    trend: sqliMissDuration,
    run: () => http.post(`${TARGET}${SQLI_PATH}`, SQLI_MISS_BODY, SQLI_REQUEST),
  },
  {
    trend: sqliMissEncDuration,
    run: () => http.post(`${TARGET}${SQLI_PATH}`, SQLI_MISS_ENC_BODY, SQLI_REQUEST),
  },
];

// Mulberry32. A named generator with an explicit seed, rather than Math.random, so a run can be
// replayed request-for-request when a result needs re-checking.
function mulberry32(state) {
  let a = state >>> 0;
  return function next() {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function probeOrder(iteration) {
  const n = PROBES.length;
  if (ORDER === 'fixed') {
    return PROBES.map((_, i) => i);
  }
  if (ORDER === 'shuffle') {
    const rand = mulberry32(SEED + iteration);
    const idx = PROBES.map((_, i) => i);
    for (let i = n - 1; i > 0; i -= 1) {
      const j = Math.floor(rand() * (i + 1));
      const tmp = idx[i];
      idx[i] = idx[j];
      idx[j] = tmp;
    }
    return idx;
  }
  const offset = iteration % n;
  return PROBES.map((_, i) => (i + offset) % n);
}

export default function () {
  // Discarded: recorded in no Trend, so it cannot enter any reported distribution. It still
  // reaches the edge, which means it contributes injection samples of exactly the same kind the
  // measured GETs produce — more samples of the same operation, not a different population.
  if (PRIME) {
    http.get(`${TARGET}/`);
  }

  // iterationInTest is global and monotonic across VUs, so the rotation advances once per
  // iteration overall rather than once per VU — which is what keeps slot occupancy balanced when
  // many VUs run concurrently.
  const order = probeOrder(exec.scenario.iterationInTest);

  for (let slot = 0; slot < order.length; slot += 1) {
    const probe = PROBES[order[slot]];
    const res = probe.run();
    probe.trend.add(res.timings.duration);
    slotDurations[slot].add(res.timings.duration);
  }
  // No sleep: the arrival-rate executor paces iterations itself. A sleep here would make every
  // VU wake on the same instant, producing a synchronised burst in which the first request of an
  // iteration queues behind every peer and the last finds a drained queue — a 1.6x-4.9x spread
  // across slots that is pure arrival artefact, and was previously read as per-phase WADM cost.
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
  'slot_1_duration',
  'slot_2_duration',
  'slot_3_duration',
  'slot_4_duration',
  'slot_5_duration',
  'slot_6_duration',
  'slot_7_duration',
  'slot_8_duration',
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
  'p(99)': 'p99',
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
// line is what makes that prefix harmless. The payload is ~2 KB, well under the log driver's
// 16 KB line-split threshold.
export function handleSummary(data) {
  const trends = {};
  for (const name of REPORTED_TRENDS) {
    trends[name] = trendStats(data.metrics[name]);
  }

  const payload = {
    rate: RATE,
    duration: DURATION,
    order: ORDER,
    seed: SEED,
    prime: PRIME,
    max_vus: MAX_VUS,
    // Retained under the old key so result files stay one schema across the change; it now
    // means "offered iterations per second", which is what the old vus+sleep(1) pair produced.
    vus: RATE,
    iterations: metricValue(data.metrics.iterations, 'count'),
    http_reqs: metricValue(data.metrics.http_reqs, 'count'),
    http_req_failed_rate: metricValue(data.metrics.http_req_failed, 'rate'),
    // Non-zero means k6 could not keep the offered rate with maxVUs — the run is saturated and
    // its latencies are queueing, not service time.
    dropped_iterations: metricValue(data.metrics.dropped_iterations, 'count') || 0,
    vus_max_used: metricValue(data.metrics.vus_max, 'max'),
    trends: trends,
  };

  // Defining handleSummary replaces k6's built-in end-of-test table, so a short human digest is
  // reprinted here to keep a manual `docker compose up --abort-on-container-exit` readable.
  const digest = REPORTED_TRENDS.filter(function (n) { return !n.startsWith('slot_'); })
    .map(function (name) {
      const med = trends[name] && trends[name].med !== null ? trends[name].med.toFixed(2) : 'n/a';
      return `  ${name}: med=${med}ms`;
    }).join('\n');

  const failedPct = ((payload.http_req_failed_rate || 0) * 100).toFixed(2);

  return {
    stdout:
      `\nk6 done: ${payload.iterations} iterations, ${payload.http_reqs} requests, `
      + `${failedPct}% failed, ${payload.dropped_iterations} dropped\n${digest}\n`
      + `WADM K6 SUMMARY ${JSON.stringify(payload)}\n`,
  };
}
