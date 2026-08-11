use log::warn;
use proxy_wasm::traits::*;
use proxy_wasm::types::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::collections::HashSet;
use std::rc::Rc;
use std::time::SystemTime;

// Registers the WASM plugin with Envoy: sets log level and the root context factory (entry point for all streams).
proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Warn);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> {
        Box::new(HoneypotRoot {
            config: None,
            detected_ips: Rc::new(RefCell::new(HashSet::new())),
        })
    });
}}

// Per-token on/off switch. JSON allows number (1/0), bool, or string (on/off/…); an untagged
// enum accepts all three so the shared config.json parses identically to the Lua edges.
#[derive(Deserialize, Clone)]
#[serde(untagged)]
enum Enabled {
    Bool(bool),
    Int(i64),
    Str(String),
}

// Active unless explicitly disabled; absent (None) defaults to on (backward compatible).
fn is_on(e: &Option<Enabled>) -> bool {
    match e {
        None => true,
        Some(Enabled::Bool(b)) => *b,
        Some(Enabled::Int(i)) => *i == 1,
        Some(Enabled::Str(s)) => matches!(s.to_ascii_lowercase().as_str(), "1" | "on" | "true" | "yes"),
    }
}

// Top-level JSON from Envoy plugin `configuration` (same shape as repo `config.json`).
#[derive(Deserialize, Clone)]
struct Config {
    // Future feature flag: parsed for forward-compatibility but not yet acted on — WASM has
    // no POST-body inspection, so form-field tamper is a query-string check only (matching
    // every edge's default behaviour while this is off).
    #[serde(default)]
    #[allow(dead_code)]
    post_body_inspection: Option<bool>,
    honeytokens: Option<Honeytokens>,
    #[serde(default)]
    sql_injection: Option<SqlInjection>,
}

// Fake SQL-injection trap: the edge terminates the configured login endpoint and plays a
// vulnerable MySQL app instead of proxying. Not a honeytoken kind — it plants nothing and
// is a single response policy rather than an array of baits.
#[derive(Deserialize, Clone)]
struct SqlInjection {
    #[serde(default)]
    enabled: Option<Enabled>,
    #[serde(default)]
    paths: Vec<String>,
    #[serde(default)]
    methods: Vec<String>,
    #[serde(default)]
    watch_fields: Vec<String>,
    #[serde(default)]
    reflect_max_len: Option<usize>,
    #[serde(default)]
    signatures: Vec<String>,
    #[serde(default)]
    status_code: Option<u32>,
    #[serde(default)]
    error_template: String,
    #[serde(default)]
    deny_status_code: Option<u32>,
    #[serde(default)]
    deny_template: String,
}

// Groups the honeytoken kinds under one key. Each is optional so a config.json omitting a kind still parses.
#[derive(Deserialize, Clone)]
struct Honeytokens {
    #[serde(default)]
    html_comments: Option<Vec<HtmlComment>>,
    #[serde(default)]
    http_headers: Option<Vec<HeaderToken>>,
    #[serde(default)]
    cookies: Option<Vec<CookieToken>>,
    #[serde(default)]
    decoy_paths: Option<Vec<DecoyPathToken>>,
    #[serde(default)]
    form_fields: Option<Vec<FormFieldToken>>,
}

// One HTML-comment token: where to inject, what comment to add, optional secret to watch for.
#[derive(Deserialize, Clone)]
struct HtmlComment {
    #[serde(default)]
    enabled: Option<Enabled>,
    paths: Vec<String>,
    comment_value: String,
    trigger_keyword: Option<String>,
}

// Decoy response header planted on matching paths; flagged when its value is replayed.
#[derive(Deserialize, Clone)]
struct HeaderToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    paths: Vec<String>,
    header_name: String,
    header_value: String,
    #[serde(default)]
    trigger_keyword: Option<String>,
}

// Bait cookie set on matching paths; flagged when it returns with a changed value (tamper).
#[derive(Deserialize, Clone)]
struct CookieToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    paths: Vec<String>,
    cookie_name: String,
    #[serde(default)]
    cookie_value: String,
    #[serde(default)]
    attributes: String,
    #[serde(default)]
    trigger_keyword: Option<String>,
}

// Fake sensitive path advertised as a hidden link; flagged on any request whose URI matches it.
#[derive(Deserialize, Clone)]
struct DecoyPathToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    trap_path: String,
    #[serde(default)]
    match_type: Option<String>,
    #[serde(default)]
    advertise_via: Option<String>,
    #[serde(default)]
    advertise_on_paths: Vec<String>,
    #[serde(default)]
    link_text: Option<String>,
}

// Hidden form field injected before </form>; flagged when a submission carries a changed value.
#[derive(Deserialize, Clone)]
struct FormFieldToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    paths: Vec<String>,
    field_name: String,
    #[serde(default)]
    field_value: String,
    #[serde(default)]
    trigger_keyword: Option<String>,
}

// Root context: created once per WASM VM; holds parsed config shared by all HTTP streams on this worker.
struct HoneypotRoot {
    config: Option<Rc<Config>>,
    // In-memory attacker store shared across all HTTP contexts on this worker's VM,
    // mirroring OpenResty's ngx.shared.wadm_state.
    detected_ips: Rc<RefCell<HashSet<String>>>,
}

// Per-request state: cheap clone of config Rc, URI path for injection matching, and HTML flag for body buffering.
struct HoneypotHttp {
    config: Option<Rc<Config>>,
    detected_ips: Rc<RefCell<HashSet<String>>>,
    request_path: String,
    is_html_response: bool,
    // Headers matched the SQLi trap, so the request body is still being awaited for it.
    sqli_pending: bool,
    // Declared Content-Length of that body, used to detect completeness without pausing.
    sqli_expected_len: usize,
    // Client IP and method captured on the request side, where the trap page is built; the
    // response phase that emits it can no longer read request headers.
    sqli_ip: String,
    sqli_method: String,
    // Page and status the response phase substitutes for whatever the origin returned.
    sqli_page: Option<String>,
    sqli_status: u32,
}

impl Context for HoneypotRoot {}

impl RootContext for HoneypotRoot {
    // Loads and validates plugin JSON from Envoy; failure prevents the filter from running correctly (returns false).
    fn on_configure(&mut self, _plugin_configuration_size: usize) -> bool {
        match self.get_plugin_configuration() {
            Some(bytes) => match serde_json::from_slice::<Config>(&bytes) {
                Ok(cfg) => {
                    self.config = Some(Rc::new(cfg));
                    true
                }
                Err(e) => {
                    warn!("WADM: failed to parse plugin config: {}", e);
                    false
                }
            },
            None => {
                warn!("WADM: no plugin configuration provided");
                false
            }
        }
    }

    // Envoy calls this for each new HTTP stream so request/response callbacks have isolated state.
    fn create_http_context(&self, _context_id: u32) -> Option<Box<dyn HttpContext>> {
        Some(Box::new(HoneypotHttp {
            config: self.config.clone(),
            detected_ips: self.detected_ips.clone(),
            request_path: String::new(),
            is_html_response: false,
            sqli_pending: false,
            sqli_expected_len: 0,
            sqli_ip: String::new(),
            sqli_method: String::new(),
            sqli_page: None,
            sqli_status: 0,
        }))
    }

    // Declares that this root context produces HTTP-level child contexts (required for HTTP filter chain).
    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }
}

impl Context for HoneypotHttp {}

impl HttpContext for HoneypotHttp {
    // Detection: SQLi trap first (it owns the whole request), then additional kinds (untimed),
    // then the benchmarked html_comments path scan.
    fn on_http_request_headers(&mut self, _num_headers: usize, end_of_stream: bool) -> Action {
        let ip = self
            .get_http_request_header("x-forwarded-for")
            .or_else(|| self.get_http_request_header("x-real-ip"))
            .unwrap_or_else(|| "unknown".to_string());

        // Read once, outside the timer, and stash the path-only portion for injection matching.
        let full_path = self.get_http_request_header(":path").unwrap_or_default();
        let host = self
            .get_http_request_header(":authority")
            .or_else(|| self.get_http_request_header("host"))
            .unwrap_or_default();
        let uri = full_path.split('?').next().unwrap_or(&full_path).to_string();
        self.request_path = uri.clone();

        // Fake SQL-injection trap. Placed first and returning unconditionally: an owned request
        // never reaches any other detector, keeping all four edges observably identical. POST-only,
        // so the k6 GET population (including GET /api/login?password=…) is unaffected.
        // Unlike the other three edges this cannot short-circuit into a local reply. In Envoy's
        // proxy-wasm host, Action::Pause on request headers stops the stream without ever
        // delivering on_http_request_body, and once headers have continued, send_http_response
        // returns BadArgument (which the SDK unwraps into a VM panic). So the trap detects on
        // the request body and swaps the upstream reply for its page on the response side. The
        // attacker-visible result is byte-identical; only the origin round-trip differs.
        let method = self.get_http_request_header(":method").unwrap_or_default();
        if self.sqli_owns(&method, &uri) {
            self.sqli_method = method;
            self.sqli_ip = ip;
            if end_of_stream {
                self.sqli_prepare("");
            } else {
                self.sqli_pending = true;
                // Tells the body callback when it has everything without returning Pause.
                self.sqli_expected_len = self
                    .get_http_request_header("content-length")
                    .and_then(|v| v.parse::<usize>().ok())
                    .unwrap_or(0);
            }
            return Action::Continue;
        }

        // Additional honeytoken kinds: cookie/form tamper, decoy-path hits, header/cookie value
        // replays. Same sink (WARN log + in-memory IP). Each kind is timed separately and logged
        // under `WADM TOKEN <kind> detect (us):`, a label sharing no substring with the
        // html_comments scraper pattern, so the benchmarked timer below is untouched.
        self.detect_additional(&DetectCtx {
            ip: ip.clone(),
            full_path: full_path.clone(),
            host,
            uri: uri.clone(),
            cookie: self.get_http_request_header("cookie").unwrap_or_default(),
        });

        // html_comments detection (benchmarked): timer wraps path scan + strip + record.
        let start = self.get_current_time();
        let path = self.get_http_request_header(":path").unwrap_or_default();

        let tokens = match self.html_comments() {
            Some(t) => t,
            None => return Action::Continue,
        };

        let mut cleaned = path.clone();
        let mut matched = false;
        for token in tokens {
            if !is_on(&token.enabled) {
                continue;
            }
            if let Some(ref kw) = token.trigger_keyword {
                if !kw.is_empty() && cleaned.contains(kw.as_str()) {
                    matched = true;
                    warn!(
                        "WADM ALERT: attacker detected -- trigger_keyword '{}' found in path '{}'",
                        kw, path
                    );
                    cleaned = cleaned.replace(kw.as_str(), "");
                }
            }
        }

        if cleaned != path {
            self.set_http_request_header(":path", Some(&cleaned));
        }

        if matched {
            self.detected_ips.borrow_mut().insert(ip);
            warn!(
                "WASM Detection execution time (us): {}",
                self.elapsed_us(start)
            );
        }
        Action::Continue
    }

    // Runs the SQLi match once the request body is complete. Completeness is decided by
    // end_of_stream or by having reached Content-Length, never by returning Pause — pausing the
    // data path here is what makes Envoy reject the later response mutation.
    fn on_http_request_body(&mut self, body_size: usize, end_of_stream: bool) -> Action {
        if !self.sqli_pending {
            return Action::Continue;
        }
        if !end_of_stream && body_size < self.sqli_expected_len {
            return Action::Continue;
        }
        self.sqli_pending = false;

        let raw = self
            .get_http_request_body(0, body_size)
            .map(|b| String::from_utf8_lossy(&b).into_owned())
            .unwrap_or_default();
        self.sqli_prepare(&raw);
        Action::Continue
    }

    // For HTML responses, drops Content-Length (body will grow) and sets flag for body phase, then
    // stages response-header honeytokens (any content type). Always continues so headers flow.
    fn on_http_response_headers(&mut self, _num_headers: usize, _end_of_stream: bool) -> Action {
        // Trap request: replace the upstream status and framing, and skip honeytoken injection
        // so the page ships exactly as built — the other three edges bypass their response
        // filters on a local reply, and this is how that parity is reproduced here.
        if self.sqli_page.is_some() {
            self.set_http_response_header(":status", Some(&self.sqli_status.to_string()));
            self.set_http_response_header("content-type", Some("text/html; charset=UTF-8"));
            self.set_http_response_header("content-length", None);
            return Action::Continue;
        }

        let ct = self
            .get_http_response_header("content-type")
            .unwrap_or_default();

        if ct.contains("text/html") {
            self.is_html_response = true;
            self.set_http_response_header("content-length", None);
        }

        self.inject_response_headers();
        Action::Continue
    }

    // Canonical injection contract (shared with OpenResty / Envoy+Lua / Apache):
    //   * Pause per chunk until end_of_stream so the whole body is buffered before the timer.
    //   * Content-type guard + path matching + token join are setup → OUTSIDE the timer.
    //   * Additional-kind body payloads are spliced untimed; the html_comments splice is timed.
    //   * Content-Length is dropped in on_http_response_headers (external to the timer).
    fn on_http_response_body(&mut self, body_size: usize, end_of_stream: bool) -> Action {
        // Trap request: discard whatever the origin said and emit the page instead.
        if let Some(page) = self.sqli_page.clone() {
            if !end_of_stream {
                return Action::Pause;
            }
            self.set_http_response_body(0, body_size, page.as_bytes());
            return Action::Continue;
        }
        if !self.is_html_response {
            return Action::Continue;
        }
        if !end_of_stream {
            return Action::Pause;
        }

        // Setup (outside timer): html_comments payloads for this path + additional-kind payloads.
        let uri = self.request_path.clone();
        let mut to_inject: Vec<String> = Vec::new();
        if let Some(tokens) = self.html_comments() {
            for token in tokens {
                if !is_on(&token.enabled) {
                    continue;
                }
                for pattern in &token.paths {
                    if pattern == "/*" || pattern == uri.as_str() {
                        to_inject.push(token.comment_value.clone());
                        break;
                    }
                }
            }
        }
        let extra = self.extra_body_payloads(&uri);

        if to_inject.is_empty() && extra.is_empty() {
            return Action::Continue;
        }

        if extra.is_empty() {
            // Unchanged benchmarked path: timer wraps read body → find </body> → splice → write.
            let injection = to_inject.join("\n");
            let start = self.get_current_time();
            let body_str = match self
                .get_http_response_body(0, body_size)
                .and_then(|b| String::from_utf8(b).ok())
            {
                Some(s) => s,
                None => return Action::Continue,
            };
            let new_body = splice_before(&body_str, "</body>", &injection);
            self.set_http_response_body(0, body_size, new_body.as_bytes());
            warn!(
                "WASM Injection execution time (us): {}",
                self.elapsed_us(start)
            );
        } else {
            // Additional-kind payloads present: splice each under its own per-kind timer, then
            // run the html_comments splice under its (unchanged) timer on the already-assembled
            // body. The per-kind timed region is locate-anchor → splice only; the single
            // set_http_response_body write-back happens once, outside.
            let mut body_str = match self
                .get_http_response_body(0, body_size)
                .and_then(|b| String::from_utf8(b).ok())
            {
                Some(s) => s,
                None => return Action::Continue,
            };
            for (kind, markup, anchor) in &extra {
                let start = self.get_current_time();
                body_str = splice_before_if_present(&body_str, anchor, markup);
                warn!(
                    "WADM TOKEN {} inject (us): {}",
                    kind,
                    self.elapsed_us(start)
                );
            }
            if !to_inject.is_empty() {
                let injection = to_inject.join("\n");
                let start = self.get_current_time();
                let new_body = splice_before(&body_str, "</body>", &injection);
                self.set_http_response_body(0, body_size, new_body.as_bytes());
                warn!(
                    "WASM Injection execution time (us): {}",
                    self.elapsed_us(start)
                );
            } else {
                self.set_http_response_body(0, body_size, body_str.as_bytes());
            }
        }
        Action::Continue
    }
}

impl HoneypotHttp {
    fn elapsed_us(&self, start: SystemTime) -> u128 {
        self.get_current_time()
            .duration_since(start)
            .map(|d| d.as_micros())
            .unwrap_or(0)
    }

    fn sqli_config(&self) -> Option<&SqlInjection> {
        self.config.as_ref().and_then(|c| c.sql_injection.as_ref())
    }

    // Owned = the request the trap answers itself. Scoped by method AND path so the
    // benchmarked GET population (including GET /api/login?password=…) is untouched.
    fn sqli_owns(&self, method: &str, uri: &str) -> bool {
        match self.sqli_config() {
            Some(cfg) => {
                is_on(&cfg.enabled)
                    && cfg.methods.iter().any(|m| m == method)
                    && path_matches(&cfg.paths, uri)
            }
            None => false,
        }
    }

    // Match and render the trap page, stashing it for the response phase. Timed region covers
    // parse → match → render only, mirroring the detection-timer contract.
    fn sqli_prepare(&mut self, raw: &str) {
        let cfg = match self.sqli_config() {
            Some(c) => c.clone(),
            None => return,
        };

        let start = self.get_current_time();
        let hit = sqli_match(&cfg, raw);
        let (page, status) = match hit {
            Some(ref h) => (
                sqli_render(&cfg.error_template, &h.value, cfg.reflect_max_len.unwrap_or(200)),
                cfg.status_code.unwrap_or(500),
            ),
            None => (cfg.deny_template.clone(), cfg.deny_status_code.unwrap_or(401)),
        };
        let elapsed = self.elapsed_us(start);

        if let Some(ref h) = hit {
            warn!(
                "WADM ALERT: honeytoken triggered by {} — sql_injection signature '{}' in field '{}' on {} {} (payload '{}')",
                self.sqli_ip, h.signature, h.field, self.sqli_method, self.request_path, log_safe(&h.value)
            );
            let ip = self.sqli_ip.clone();
            self.detected_ips.borrow_mut().insert(ip);
            // Label deliberately shares no substring with the benchmark scrapers'
            // "Detection/Injection execution time (us):" patterns.
            warn!("WASM WADM SQLI trap build (us): {}", elapsed);
        }

        self.sqli_page = Some(page);
        self.sqli_status = status;
    }

    fn html_comments(&self) -> Option<&Vec<HtmlComment>> {
        self.config
            .as_ref()
            .and_then(|c| c.honeytokens.as_ref())
            .and_then(|h| h.html_comments.as_ref())
    }

    fn honeytokens(&self) -> Option<&Honeytokens> {
        self.config.as_ref().and_then(|c| c.honeytokens.as_ref())
    }

    // Detection for the additional honeytoken kinds (parity with OpenResty nginx/nginx.conf),
    // one timed region per kind. The timed region covers the kind's own scan plus the in-memory
    // IP record — the same unit the html_comments detection timer measures — while ctx
    // construction (headers, path, cookie) stays outside as setup.
    fn detect_additional(&self, ctx: &DetectCtx) {
        for kind in KIND_ORDER {
            let start = self.get_current_time();
            let hit = match kind {
                "http_headers" => self.detect_http_headers(ctx),
                "cookies" => self.detect_cookies(ctx),
                "decoy_paths" => self.detect_decoy_paths(ctx),
                _ => self.detect_form_fields(ctx),
            };
            if hit {
                self.detected_ips.borrow_mut().insert(ctx.ip.clone());
            }
            let elapsed = self.elapsed_us(start);
            // Timed only on a hit, so every edge samples the same population.
            if hit {
                warn!("WADM TOKEN {} detect (us): {}", kind, elapsed);
            }
        }
    }

    fn detect_http_headers(&self, ctx: &DetectCtx) -> bool {
        let mut detected = false;
        if let Some(tokens) = self.honeytokens().and_then(|h| h.http_headers.as_ref()) {
            for t in tokens {
                if is_on(&t.enabled) {
                    if let Some(ref kw) = t.trigger_keyword {
                        if keyword_in_request(kw, &ctx.full_path, &ctx.host) {
                            warn!("WADM ALERT: honeytoken triggered by {} — http_header value '{}' replayed in request", ctx.ip, kw);
                            detected = true;
                        }
                    }
                }
            }
        }
        detected
    }

    fn detect_cookies(&self, ctx: &DetectCtx) -> bool {
        let mut detected = false;
        if let Some(tokens) = self.honeytokens().and_then(|h| h.cookies.as_ref()) {
            for t in tokens {
                if !is_on(&t.enabled) {
                    continue;
                }
                if !t.cookie_name.is_empty() {
                    if let Some(v) = cookie_value(&ctx.cookie, &t.cookie_name) {
                        if v != t.cookie_value {
                            warn!("WADM ALERT: honeytoken triggered by {} — cookie '{}' tampered (got '{}', expected '{}')", ctx.ip, t.cookie_name, v, t.cookie_value);
                            detected = true;
                        }
                    }
                }
                if let Some(ref kw) = t.trigger_keyword {
                    if keyword_in_request(kw, &ctx.full_path, &ctx.host) {
                        warn!("WADM ALERT: honeytoken triggered by {} — cookie value '{}' replayed in request", ctx.ip, kw);
                        detected = true;
                    }
                }
            }
        }
        detected
    }

    fn detect_decoy_paths(&self, ctx: &DetectCtx) -> bool {
        let mut detected = false;
        if let Some(tokens) = self.honeytokens().and_then(|h| h.decoy_paths.as_ref()) {
            for t in tokens {
                if !is_on(&t.enabled) || t.trap_path.is_empty() {
                    continue;
                }
                let trap = &t.trap_path;
                let hit = if t.match_type.as_deref() == Some("exact") {
                    &ctx.uri == trap
                } else {
                    &ctx.uri == trap || ctx.uri.starts_with(&format!("{}/", trap))
                };
                if hit {
                    warn!("WADM ALERT: honeytoken triggered by {} — decoy path '{}' requested ({})", ctx.ip, trap, ctx.uri);
                    detected = true;
                }
            }
        }
        detected
    }

    fn detect_form_fields(&self, ctx: &DetectCtx) -> bool {
        let mut detected = false;
        if let Some(tokens) = self.honeytokens().and_then(|h| h.form_fields.as_ref()) {
            let pairs = query_pairs(&ctx.full_path);
            for t in tokens {
                if !is_on(&t.enabled) {
                    continue;
                }
                if !t.field_name.is_empty() {
                    for (k, v) in &pairs {
                        if k == &t.field_name && v != &t.field_value {
                            warn!("WADM ALERT: honeytoken triggered by {} — form field '{}' tampered (got '{}', expected '{}')", ctx.ip, t.field_name, v, t.field_value);
                            detected = true;
                        }
                    }
                }
                if let Some(ref kw) = t.trigger_keyword {
                    if keyword_in_request(kw, &ctx.full_path, &ctx.host) {
                        warn!("WADM ALERT: honeytoken triggered by {} — form field keyword '{}' seen in request", ctx.ip, kw);
                        detected = true;
                    }
                }
            }
        }
        detected
    }

    // Response-header injection: decoy response headers + Set-Cookie baits on matching paths.
    // Token selection is setup and stays outside; the timed region is the header write itself.
    fn inject_response_headers(&self) {
        let ht = match self.honeytokens() {
            Some(h) => h,
            None => return,
        };
        let uri = self.request_path.as_str();

        let headers: Vec<&HeaderToken> = ht
            .http_headers
            .iter()
            .flatten()
            .filter(|t| is_on(&t.enabled) && path_matches(&t.paths, uri) && !t.header_name.is_empty())
            .collect();
        if !headers.is_empty() {
            let start = self.get_current_time();
            for t in &headers {
                self.set_http_response_header(&t.header_name, Some(&t.header_value));
            }
            warn!(
                "WADM TOKEN http_headers inject (us): {}",
                self.elapsed_us(start)
            );
        }

        let cookies: Vec<&CookieToken> = ht
            .cookies
            .iter()
            .flatten()
            .filter(|t| is_on(&t.enabled) && path_matches(&t.paths, uri) && !t.cookie_name.is_empty())
            .collect();
        if !cookies.is_empty() {
            let start = self.get_current_time();
            for t in &cookies {
                let mut c = format!("{}={}", t.cookie_name, t.cookie_value);
                if !t.attributes.is_empty() {
                    c.push_str(&format!("; {}", t.attributes));
                }
                self.add_http_response_header("set-cookie", &c);
            }
            warn!("WADM TOKEN cookies inject (us): {}", self.elapsed_us(start));
        }
    }

    // Additional-kind body payloads: hidden form fields (before </form>) + decoy links (before
    // </body>). Each entry is (kind, markup, anchor); building the markup is setup and happens
    // here, outside the per-kind splice timers in on_http_response_body.
    fn extra_body_payloads(&self, uri: &str) -> Vec<(&'static str, String, String)> {
        let mut out = Vec::new();
        let ht = match self.honeytokens() {
            Some(h) => h,
            None => return out,
        };
        if let Some(tokens) = ht.decoy_paths.as_ref() {
            for t in tokens {
                if is_on(&t.enabled)
                    && t.advertise_via.as_deref().unwrap_or("link") == "link"
                    && path_matches(&t.advertise_on_paths, uri)
                {
                    let markup = format!(
                        "<a href=\"{}\" style=\"display:none\">{}</a>",
                        t.trap_path,
                        t.link_text.as_deref().unwrap_or("")
                    );
                    out.push(("decoy_paths", markup, "</body>".to_string()));
                }
            }
        }
        if let Some(tokens) = ht.form_fields.as_ref() {
            for t in tokens {
                if is_on(&t.enabled) && path_matches(&t.paths, uri) && !t.field_name.is_empty() {
                    let markup = format!(
                        "<input type=\"hidden\" name=\"{}\" value=\"{}\">",
                        t.field_name, t.field_value
                    );
                    out.push(("form_fields", markup, "</form>".to_string()));
                }
            }
        }
        out
    }
}

// Fixed order so the per-kind timing regions run in the same sequence on every edge.
const KIND_ORDER: [&str; 4] = ["http_headers", "cookies", "decoy_paths", "form_fields"];

// Per-request surfaces the additional-kind detectors read, gathered once outside every
// timed region so the timers measure matching work only (mirrors the Lua edges' ctx tables).
struct DetectCtx {
    ip: String,
    full_path: String,
    host: String,
    uri: String,
    cookie: String,
}

struct SqliHit {
    field: String,
    value: String,
    signature: String,
}

// Signatures are stored pre-lowercased and space-normalised, so the same folding must be applied
// to the input. to_ascii_lowercase (not to_lowercase) and an explicit whitespace set including
// \x0b keep this byte-identical to Lua's string.lower and %s on the other three edges.
fn sqli_normalize(v: &str) -> String {
    let decoded = url_decode(v).to_ascii_lowercase();
    let mut out = String::with_capacity(decoded.len());
    let mut in_ws = false;
    for c in decoded.chars() {
        if matches!(c, ' ' | '\t' | '\n' | '\x0b' | '\x0c' | '\r') {
            if !in_ws {
                out.push(' ');
                in_ws = true;
            }
        } else {
            out.push(c);
            in_ws = false;
        }
    }
    out
}

// Ordered pair list keeping duplicates: a map would be unordered and would drop repeated keys,
// making the reflected payload differ between edges on a multi-field hit.
fn body_pairs(raw: &str) -> Vec<(String, String)> {
    raw.split('&')
        .filter(|s| !s.is_empty())
        .map(|pair| match pair.split_once('=') {
            Some((k, v)) => (url_decode(k), url_decode(v)),
            None => (url_decode(pair), String::new()),
        })
        .collect()
}

// Loop order (watch_fields → body pairs → signatures) is part of the cross-edge contract:
// it makes "first match wins" resolve identically on all four edges.
fn sqli_match(cfg: &SqlInjection, raw: &str) -> Option<SqliHit> {
    let pairs = body_pairs(raw);
    for field in &cfg.watch_fields {
        for (k, v) in &pairs {
            if k == field {
                let norm = sqli_normalize(v);
                for sig in &cfg.signatures {
                    if norm.contains(sig.as_str()) {
                        return Some(SqliHit {
                            field: field.clone(),
                            value: v.clone(),
                            signature: sig.clone(),
                        });
                    }
                }
            }
        }
    }
    None
}

// The trap reflects attacker-controlled input; without escaping the honeypot would itself be
// a live reflected-XSS vector against anyone who views the page.
fn html_escape(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 16);
    for c in s.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(c),
        }
    }
    out
}

fn sqli_render(template: &str, payload: &str, max_len: usize) -> String {
    // Largest char boundary at or below max_len: the Lua edges cut at exactly that byte, and
    // this is the closest Rust can get without panicking on a mid-codepoint split. Identical
    // for ASCII, i.e. for every canonical SQLi payload.
    let end = payload
        .char_indices()
        .map(|(i, _)| i)
        .chain(std::iter::once(payload.len()))
        .take_while(|&i| i <= max_len)
        .last()
        .unwrap_or(0);
    template.replace("{PAYLOAD}", &html_escape(&payload[..end]))
}

// CR/LF would let an attacker forge whole log lines that the benchmark scrapers read.
fn log_safe(s: &str) -> String {
    s.replace('\r', " ").replace('\n', " ")
}

// "/*" matches every path, otherwise exact match against the request URI.
fn path_matches(paths: &[String], uri: &str) -> bool {
    paths.iter().any(|p| p == "/*" || p == uri)
}

// Substring scan of a request's target surface (raw path incl. query, and Host) for a replayed value.
fn keyword_in_request(kw: &str, full_path: &str, host: &str) -> bool {
    !kw.is_empty() && (full_path.contains(kw) || host.contains(kw))
}

// Read one cookie's value from the raw Cookie header (None if absent).
fn cookie_value(cookie_header: &str, name: &str) -> Option<String> {
    for part in cookie_header.split(';') {
        if let Some((k, v)) = part.split_once('=') {
            if k.trim() == name {
                return Some(v.trim().to_string());
            }
        }
    }
    None
}

// Parse the query string of a :path into decoded (key, value) pairs.
fn query_pairs(full_path: &str) -> Vec<(String, String)> {
    let query = match full_path.split_once('?') {
        Some((_, q)) => q,
        None => return Vec::new(),
    };
    query
        .split('&')
        .filter(|s| !s.is_empty())
        .map(|pair| match pair.split_once('=') {
            Some((k, v)) => (url_decode(k), url_decode(v)),
            None => (url_decode(pair), String::new()),
        })
        .collect()
}

// Minimal application/x-www-form-urlencoded decode (+ and %XX), matching the Lua edges' url_decode.
fn url_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b'%' if i + 2 < bytes.len() => match (hex_val(bytes[i + 1]), hex_val(bytes[i + 2])) {
                (Some(h), Some(l)) => {
                    out.push(h * 16 + l);
                    i += 3;
                }
                _ => {
                    out.push(bytes[i]);
                    i += 1;
                }
            },
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn hex_val(b: u8) -> Option<u8> {
    match b {
        b'0'..=b'9' => Some(b - b'0'),
        b'a'..=b'f' => Some(b - b'a' + 10),
        b'A'..=b'F' => Some(b - b'A' + 10),
        _ => None,
    }
}

// Splice `insert` before the first `anchor`; if the anchor is absent, append (html_comments fallback).
fn splice_before(body: &str, anchor: &str, insert: &str) -> String {
    match body.find(anchor) {
        Some(pos) => {
            let mut buf = String::with_capacity(body.len() + insert.len() + 1);
            buf.push_str(&body[..pos]);
            buf.push_str(insert);
            buf.push('\n');
            buf.push_str(&body[pos..]);
            buf
        }
        None => {
            let mut buf = body.to_string();
            buf.push_str(insert);
            buf
        }
    }
}

// Splice `insert` before the first `anchor`; if the anchor is absent, leave the body unchanged
// (additional-kind payloads must not land outside their anchor, e.g. a form field with no </form>).
fn splice_before_if_present(body: &str, anchor: &str, insert: &str) -> String {
    match body.find(anchor) {
        Some(pos) => {
            let mut buf = String::with_capacity(body.len() + insert.len() + 1);
            buf.push_str(&body[..pos]);
            buf.push_str(insert);
            buf.push('\n');
            buf.push_str(&body[pos..]);
            buf
        }
        None => body.to_string(),
    }
}
