import http from 'k6/http';
import crypto from 'k6/crypto';
import exec from 'k6/execution';
import { Rate, Trend } from 'k6/metrics';

const TARGET = __ENV.TARGET || 'http://openresty:80';
// The origin, addressed directly. setup() asks it the same questions as the edge, so the result
// file records whether the edge returns what the origin would have — the "does it break existing
// functionality" question, answered per probe rather than assumed.
const ORIGIN = __ENV.ORIGIN || 'http://backend:80';
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
// The `/*` html_comments token every proxied HTML page should carry under WADM.
const COMMENT_MARKER = __ENV.COMMENT_MARKER || 'DEV-PORTAL: internal-admin.example.com';

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

// `williams` (default) walks a Williams Latin square: over any 8 consecutive iterations every
// probe occupies every position once AND follows every other probe once. `rotate`, the previous
// default, balanced position only — probe i was always preceded by probe i-1, so whatever a
// request leaves behind (Apache closes the connection after a 500) was charged to the same
// successor every time. That confound put +4.8 ms on exactly the two probes that follow the SQLi
// trap's 500s under Apache `LuaScope conn`. `rotate` and `fixed` remain to reproduce earlier
// results; `shuffle` is a seeded permutation that balances both only in expectation.
const ORDER = __ENV.WADM_ORDER || 'williams';
const SEED = parseInt(__ENV.WADM_SEED || '1', 10);

// One unrecorded request at the head of each iteration. Under an arrival-rate executor a VU sits
// idle between iterations, so its first request pays connection state the other seven do not: the
// measured slot-1 median ran 1.79x slot-8 with its *minimum* also elevated, which is setup cost
// rather than queueing. Absorbing it into a discarded request leaves all eight measured probes on
// an equally warm connection. Set WADM_PRIME=0 to measure without it.
const PRIME = (__ENV.WADM_PRIME || '1') !== '0';

// Which probes an iteration issues. `all` is the reportable mix. `get` drops the SQLi POSTs, so
// that CPU per iteration of `all` minus `get` isolates what the trap's requests cost: the cgroup
// counter cannot tell request types apart, and on three edges the trap answers its POSTs itself,
// saving proxy work, so a single mixed figure nets WADM's cost against that saving. `post` is the
// complement. `size` replaces the probe set with one GET per page size, for the injection-vs-body
// size sweep. `sqli-hit` and `sqli-miss` send only the trap's signature hits (plain and
// %-encoded) or only its clean logins, so the CPU one hit or one miss costs can be read on its own;
// run them with WADM_PRIME=0, so the iteration holds nothing but those two requests.
const MIX = __ENV.WADM_MIX || 'all';

// End-to-end integrity checks against the reference responses setup() captures. Costs a hash per
// response on the load generator's cores, never the edge's. WADM_CHECKS=0 disables them.
const CHECKS = (__ENV.WADM_CHECKS || '1') !== '0';

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
// first request fires, preventing connection-refused errors that skew min/avg. The edge sits idle
// during it, which the harness uses as a free measurement of the edge's idle CPU.
export const options = {
  summaryTrendStats: TREND_STATS,
  setupTimeout: '60s',
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
// route); marking it acceptable keeps http_req_failed meaningful. Whether the status is the RIGHT
// one for this tier is decided separately, against setup()'s reference response.
const ALLOW_404 = { responseCallback: http.expectedStatuses(200, 404) };

// The trap answers 500 on a signature hit and 401 on a clean login, while the baseline tiers have
// no trap at all and fall through to the origin's 404. All three are expected outcomes, so all
// three must count as non-failures or http_req_failed would read as a broken run.
const SQLI_REQUEST = {
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  responseCallback: http.expectedStatuses(200, 401, 404, 500),
};

// Each probe is a request description, so the same request can be sent to the edge under load
// and to the origin in setup(). `baited` marks the probes whose response the edge proxies from
// the origin and should therefore carry injected tokens; the trap's POSTs are answered locally
// (or, on WASM, replaced) and carry none.
//
// 1-4 are GETs: injection fires on all of them (every response is text/html and the `/*` tokens
// are planted on every page), while only the last three carry a trigger and take the detection
// hit path. 5-8 are the SQLi trap's 2x2, which plants nothing.
const MAIN_PROBES = [
  { name: 'inject_get', method: 'GET', path: '/', baited: true },
  {
    name: 'detect_query',
    method: 'GET',
    path: `/api/login?password=${encodeURIComponent(TRIGGER_KEYWORD)}`,
    params: ALLOW_404,
    baited: true,
  },
  {
    // /login.html also carries a </form>, so it is the page where form_fields injection runs.
    // Three kinds fire on this one request, each timed in its own region by the edge:
    // form_fields (hidden input returned changed), http_headers (planted value replayed in the
    // query), cookies (bait cookie returned tampered).
    name: 'token_tamper',
    method: 'GET',
    path: `${FORM_PAGE}?${FORM_FIELD}=${encodeURIComponent(FORM_TAMPER_VALUE)}`
      + `&probe=${encodeURIComponent(HEADER_KEYWORD)}`,
    params: { headers: { Cookie: `${COOKIE_NAME}=${COOKIE_TAMPER_VALUE}` } },
    baited: true,
  },
  {
    // decoy_paths detection: any request to the advertised trap path is a hit, no keyword needed.
    name: 'token_decoy', method: 'GET', path: DECOY_PATH, params: ALLOW_404, baited: true,
  },
  { name: 'sqli_hit', method: 'POST', path: SQLI_PATH, body: SQLI_HIT_BODY, params: SQLI_REQUEST },
  {
    name: 'sqli_hit_enc', method: 'POST', path: SQLI_PATH, body: SQLI_HIT_ENC_BODY, params: SQLI_REQUEST,
  },
  { name: 'sqli_miss', method: 'POST', path: SQLI_PATH, body: SQLI_MISS_BODY, params: SQLI_REQUEST },
  {
    name: 'sqli_miss_enc', method: 'POST', path: SQLI_PATH, body: SQLI_MISS_ENC_BODY, params: SQLI_REQUEST,
  },
];

// Pages generated by backend/generate_test_pages.py. All stay below Envoy's default 1 MiB buffer
// limit, which a body-buffering filter cannot exceed; larger pages are a functional question, and
// functional_matrix.py asks it.
const SIZE_PROBES = ['1k', '16k', '256k', '768k'].map((label) => ({
  name: `size_${label}`, method: 'GET', path: `/test/size-${label}.html`, baited: true,
}));

const MIXES = {
  all: MAIN_PROBES,
  get: MAIN_PROBES.slice(0, 4),
  post: MAIN_PROBES.slice(4),
  'sqli-hit': MAIN_PROBES.filter((p) => p.name === 'sqli_hit' || p.name === 'sqli_hit_enc'),
  'sqli-miss': MAIN_PROBES.filter((p) => p.name === 'sqli_miss' || p.name === 'sqli_miss_enc'),
  size: SIZE_PROBES,
};
if (!MIXES[MIX]) {
  throw new Error(`WADM_MIX must be one of ${Object.keys(MIXES).join(', ')}; got ${MIX}`);
}
const PROBES = MIXES[MIX];
const ALL_PROBES = MAIN_PROBES.concat(SIZE_PROBES);

// Metrics are declared for every probe, active or not, because k6 requires metrics to be created
// in the init context; an unused one simply records nothing and is reported as null.
//
// <name>_duration keeps per-request-type latency distinguishable in k6's summary (the built-in
// http_req_duration aggregates every call). new_conn_<name> is the share of that probe's requests
// that opened a TCP connection instead of reusing one, which exposes connection teardown — a
// keep-alive change WADM causes is a functional side-effect in its own right. intact_<name> is the
// share whose status and body matched the reference the edge returned in setup(), so a response
// corrupted, truncated or left un-injected under load shows up as a rate below 1.
const METRICS = {};
for (const probe of ALL_PROBES) {
  METRICS[probe.name] = {
    duration: new Trend(`${probe.name}_duration`, true),
    newConn: new Rate(`new_conn_${probe.name}`),
    intact: new Rate(`intact_${probe.name}`),
  };
}

// One Trend per position in the iteration, independent of which probe landed there. With a
// balanced order these must come out equal; any residual spread is the positional artifact
// itself, measured rather than assumed, and is what makes the per-probe figures auditable.
const MAX_SLOTS = 8;
const slotDurations = [];
for (let i = 0; i < MAX_SLOTS; i += 1) {
  slotDurations.push(new Trend(`slot_${i + 1}_duration`, true));
}

function send(base, probe) {
  const url = `${base}${probe.path}`;
  if (probe.method === 'POST') {
    return http.post(url, probe.body, probe.params);
  }
  return http.get(url, probe.params);
}

function digest(res) {
  return {
    status: res.status,
    sha256: res.body ? crypto.sha256(res.body, 'hex') : null,
    bytes: res.body ? res.body.length : 0,
  };
}

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

// Williams' base sequence for an even n: 0, 1, n-1, 2, n-2, ... Adding the row number to every
// element (mod n) yields n rows in which each element is in each position once and each ordered
// pair of elements is adjacent exactly once.
function williamsBase(n) {
  const base = [0];
  let low = 1;
  let high = n - 1;
  while (base.length < n) {
    base.push(low);
    low += 1;
    if (base.length < n) {
      base.push(high);
      high -= 1;
    }
  }
  return base;
}
const WILLIAMS_BASE = williamsBase(PROBES.length);

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
  if (ORDER === 'rotate') {
    return PROBES.map((_, i) => (i + offset) % n);
  }
  return WILLIAMS_BASE.map((v) => (v + offset) % n);
}

function headerValue(res, name) {
  const key = Object.keys(res.headers).find((k) => k.toLowerCase() === name.toLowerCase());
  return key ? res.headers[key] : undefined;
}

// The reference each measured response is compared against, captured once per run before the
// load starts. Two comparisons are recorded per probe:
//   edge vs origin  whether this tier returns what the origin itself returns. Must hold on the
//                   bare tier; on WADM tiers it must NOT hold for baited probes, by design.
//   bait present    whether the injected header, cookie and comment are actually there.
// setup()'s requests are counted in http_reqs, which is why the payload also reports
// edge_requests — the requests the measured iterations sent — for anything divided per request.
export function setup() {
  if (!CHECKS) {
    return { probes: {} };
  }
  const probes = {};
  for (const probe of PROBES) {
    const edge = send(TARGET, probe);
    const origin = send(ORIGIN, probe);
    const setCookie = headerValue(edge, 'Set-Cookie') || '';
    probes[probe.name] = {
      edge: digest(edge),
      origin: digest(origin),
      matches_origin: edge.status === origin.status && digest(edge).sha256 === digest(origin).sha256,
      baited: Boolean(probe.baited),
      bait: {
        header: headerValue(edge, 'X-Backend-Server') === HEADER_KEYWORD,
        cookie: setCookie.indexOf(`${COOKIE_NAME}=`) !== -1,
        comment: (edge.body || '').indexOf(COMMENT_MARKER) !== -1,
      },
    };
  }
  return { probes };
}

export default function (reference) {
  // Marks where the measured window begins, so the harness can integrate the edge's CPU counter
  // over exactly the measured seconds rather than over the whole container lifecycle.
  if (exec.scenario.iterationInTest === 0) {
    console.log(`WADM K6 WINDOW START ${Date.now()}`);
  }

  // Discarded: recorded in no Trend, so it cannot enter any reported distribution. It still
  // reaches the edge, which means it contributes injection samples of exactly the same kind the
  // measured GETs produce — more samples of the same operation, not a different population.
  if (PRIME) {
    http.get(`${TARGET}/`);
  }

  // iterationInTest is global and monotonic across VUs, so the order advances once per iteration
  // overall rather than once per VU — which is what keeps slot occupancy balanced when many VUs
  // run concurrently.
  const order = probeOrder(exec.scenario.iterationInTest);
  const golden = (reference && reference.probes) || {};

  for (let slot = 0; slot < order.length; slot += 1) {
    const probe = PROBES[order[slot]];
    const metrics = METRICS[probe.name];
    const res = send(TARGET, probe);
    metrics.duration.add(res.timings.duration);
    slotDurations[slot].add(res.timings.duration);
    metrics.newConn.add(res.timings.connecting > 0);
    const expected = golden[probe.name];
    if (CHECKS && expected) {
      const got = digest(res);
      metrics.intact.add(got.status === expected.edge.status && got.sha256 === expected.edge.sha256);
    }
  }
  // No sleep: the arrival-rate executor paces iterations itself. A sleep here would make every
  // VU wake on the same instant, producing a synchronised burst in which the first request of an
  // iteration queues behind every peer and the last finds a drained queue — a 1.6x-4.9x spread
  // across slots that is pure arrival artefact, and was previously read as per-phase WADM cost.
}

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

function rateStats(metric) {
  if (!metric || !metric.values) return null;
  return {
    rate: metricValue(metric, 'rate'),
    passes: metricValue(metric, 'passes'),
    fails: metricValue(metric, 'fails'),
  };
}

// The edges already publish their internal microsecond timings through `docker compose logs`;
// this reuses that transport for k6's end-to-end numbers rather than adding a writable mount
// (which would also mean the container writing into the repo as a different uid).
//
// The sentinel and its JSON must stay on ONE line: `docker compose logs` prefixes every line
// with `load-tester-1  | `, so anchoring the scraper on the sentinel and taking the rest of the
// line is what makes that prefix harmless. The payload stays well under the log driver's 16 KB
// line-split threshold.
export function handleSummary(data) {
  // http_req_duration is the pooled headline (all requests); the per-probe Trends keep the
  // per-request-type breakdown. The per-type ones are what the overhead decomposition sums,
  // because the four POSTs are short-circuited by the trap on three of four edges and are
  // therefore *faster* than their baseline counterparts — scaling the pooled median by the request
  // count would let that saving cancel out the overhead the GETs add.
  const trends = { http_req_duration: trendStats(data.metrics.http_req_duration) };
  const newConn = {};
  const intact = {};
  for (const probe of PROBES) {
    trends[`${probe.name}_duration`] = trendStats(data.metrics[`${probe.name}_duration`]);
    newConn[probe.name] = rateStats(data.metrics[`new_conn_${probe.name}`]);
    intact[probe.name] = rateStats(data.metrics[`intact_${probe.name}`]);
  }
  for (let i = 0; i < PROBES.length; i += 1) {
    trends[`slot_${i + 1}_duration`] = trendStats(data.metrics[`slot_${i + 1}_duration`]);
  }

  const iterations = metricValue(data.metrics.iterations, 'count');
  const payload = {
    rate: RATE,
    duration: DURATION,
    order: ORDER,
    seed: SEED,
    prime: PRIME,
    mix: MIX,
    probes: PROBES.map((p) => p.name),
    max_vus: MAX_VUS,
    // Retained under the old key so result files stay one schema across the change; it now
    // means "offered iterations per second", which is what the old vus+sleep(1) pair produced.
    vus: RATE,
    iterations: iterations,
    http_reqs: metricValue(data.metrics.http_reqs, 'count'),
    // Requests the measured iterations sent to the edge: excludes setup()'s reference requests,
    // half of which went to the origin directly. This is the denominator for CPU per request.
    edge_requests: iterations === null ? null : iterations * (PROBES.length + (PRIME ? 1 : 0)),
    data_received: metricValue(data.metrics.data_received, 'count'),
    http_req_failed_rate: metricValue(data.metrics.http_req_failed, 'rate'),
    // Non-zero means k6 could not keep the offered rate with maxVUs — the run is saturated and
    // its latencies are queueing, not service time.
    dropped_iterations: metricValue(data.metrics.dropped_iterations, 'count') || 0,
    vus_max_used: metricValue(data.metrics.vus_max, 'max'),
    trends: trends,
    new_conn: newConn,
    intact: intact,
    reference: (data.setup_data && data.setup_data.probes) || {},
  };

  // Defining handleSummary replaces k6's built-in end-of-test table, so a short human digest is
  // reprinted here to keep a manual `docker compose up --abort-on-container-exit` readable.
  const digestLines = PROBES.map((p) => {
    const t = trends[`${p.name}_duration`];
    const med = t && t.med !== null ? t.med.toFixed(2) : 'n/a';
    const ok = intact[p.name] && intact[p.name].rate !== null
      ? `${(intact[p.name].rate * 100).toFixed(1)}% intact` : 'unchecked';
    return `  ${p.name}: med=${med}ms ${ok}`;
  }).join('\n');

  const failedPct = ((payload.http_req_failed_rate || 0) * 100).toFixed(2);

  return {
    stdout:
      `\nk6 done: ${payload.iterations} iterations, ${payload.http_reqs} requests, `
      + `${failedPct}% failed, ${payload.dropped_iterations} dropped\n${digestLines}\n`
      + `WADM K6 SUMMARY ${JSON.stringify(payload)}\n`,
  };
}
