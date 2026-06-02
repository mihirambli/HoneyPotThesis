import http from 'k6/http';
import { sleep } from 'k6';
import { Trend } from 'k6/metrics';

const TARGET = __ENV.TARGET || 'http://openresty:80';
const TRIGGER_KEYWORD = __ENV.TRIGGER_KEYWORD || 'internal-admin.example.com';

// Per-phase Trends keep WADM injection vs. detection latency distinguishable in k6's summary;
// the built-in http_req_duration would aggregate both calls into one distribution.
const injectGetDuration = new Trend('inject_get_duration', true);
const detectQueryDuration = new Trend('detect_query_duration', true);

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

export default function () {
  const getRes = http.get(`${TARGET}/`);
  injectGetDuration.add(getRes.timings.duration);

  // Keyword in the query string is the one surface ALL four edges already inspect
  // (OpenResty get_uri_args, Envoy+Lua parse_query_string, Apache r.args, WASM :path
  // substring), so this measurement is cross-edge comparable. POST-body inspection
  // in OpenResty/Envoy+Lua is intentionally left in place for a future iteration
  // that raises Apache + WASM to the same level and re-introduces a body scenario.
  // /api/login has no backend route so the backend returns 404 — that is expected and
  // does not affect the proxy-side detection timing. 404 is marked acceptable so k6
  // does not count it as a failure and inflate http_req_failed.
  const detectRes = http.get(
    `${TARGET}/api/login?password=${encodeURIComponent(TRIGGER_KEYWORD)}`,
    { responseCallback: http.expectedStatuses(200, 404) },
  );
  detectQueryDuration.add(detectRes.timings.duration);

  sleep(1);
}
