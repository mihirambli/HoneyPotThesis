use log::warn;
use proxy_wasm::traits::*;
use proxy_wasm::types::*;
use serde::Deserialize;
use std::borrow::Cow;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::time::SystemTime;

// Registers the WASM plugin with Envoy: sets log level and the root context factory (entry point for all streams).
proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Warn);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> {
        Box::new(HoneypotRoot {
            compiled: None,
            detected_ips: Rc::new(RefCell::new(HashSet::new())),
        })
    });
}}

// ── Config schema (same shape as the repo's config.json) ─────────────────────────────────────
// Every token field defaults when absent, matching the Lua edges, which treat a missing field as
// nil rather than rejecting the whole config.

// Per-token on/off switch. JSON allows number (1/0), bool, or string (on/off/…); an untagged
// enum accepts all of them so the shared config.json parses identically to the Lua edges.
#[derive(Deserialize)]
#[serde(untagged)]
enum Enabled {
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
}

// Active unless explicitly disabled; absent (None) defaults to on (backward compatible).
fn is_on(e: &Option<Enabled>) -> bool {
    match e {
        None => true,
        Some(Enabled::Bool(b)) => *b,
        Some(Enabled::Int(i)) => *i == 1,
        Some(Enabled::Float(f)) => *f == 1.0,
        Some(Enabled::Str(s)) => matches!(s.to_ascii_lowercase().as_str(), "1" | "on" | "true" | "yes"),
    }
}

// `post_body_inspection` is deliberately absent: WASM has no POST-body inspection (serde ignores
// the unknown key), matching every edge's behaviour while that flag is off (the default).
#[derive(Deserialize)]
struct Config {
    #[serde(default)]
    honeytokens: Option<Honeytokens>,
    #[serde(default)]
    sql_injection: Option<SqlInjection>,
}

// Fake SQL-injection trap: the edge terminates the configured login endpoint and plays a
// vulnerable MySQL app instead of proxying. Not a honeytoken kind — it plants nothing and
// is a single response policy rather than an array of baits.
#[derive(Deserialize)]
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

#[derive(Deserialize, Default)]
struct Honeytokens {
    #[serde(default)]
    html_comments: Vec<HtmlComment>,
    #[serde(default)]
    http_headers: Vec<HeaderToken>,
    #[serde(default)]
    cookies: Vec<CookieToken>,
    #[serde(default)]
    decoy_paths: Vec<DecoyPathToken>,
    #[serde(default)]
    form_fields: Vec<FormFieldToken>,
}

#[derive(Deserialize)]
struct HtmlComment {
    #[serde(default)]
    enabled: Option<Enabled>,
    #[serde(default)]
    paths: Vec<String>,
    #[serde(default)]
    comment_value: Option<String>,
    #[serde(default)]
    trigger_keyword: Option<String>,
}

#[derive(Deserialize)]
struct HeaderToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    #[serde(default)]
    paths: Vec<String>,
    #[serde(default)]
    header_name: String,
    #[serde(default)]
    header_value: String,
    #[serde(default)]
    trigger_keyword: Option<String>,
}

#[derive(Deserialize)]
struct CookieToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    #[serde(default)]
    paths: Vec<String>,
    #[serde(default)]
    cookie_name: String,
    #[serde(default)]
    cookie_value: String,
    #[serde(default)]
    attributes: String,
    #[serde(default)]
    trigger_keyword: Option<String>,
}

#[derive(Deserialize)]
struct DecoyPathToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    #[serde(default)]
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

#[derive(Deserialize)]
struct FormFieldToken {
    #[serde(default)]
    enabled: Option<Enabled>,
    #[serde(default)]
    paths: Vec<String>,
    #[serde(default)]
    field_name: String,
    #[serde(default)]
    field_value: String,
    #[serde(default)]
    trigger_keyword: Option<String>,
}

// ── Precompiled config ───────────────────────────────────────────────────────────────────────
// Built once in on_configure. Paths are only ever "/*" or exact, so every request resolves to
// one of a handful of plans; resolving them here replaces the per-request token loops, enabled
// checks and format! calls. Config order is kept within each list because it decides the
// joined comment order and the splice order.

struct Extra {
    kind: &'static str,
    anchor: &'static [u8],
    markup: Vec<u8>,
}

struct Plan {
    comments: Option<Vec<u8>>,
    headers: Vec<(String, String)>,
    cookies: Vec<String>,
    extras: Vec<Extra>,
}

impl Plan {
    fn has_body(&self) -> bool {
        self.comments.is_some() || !self.extras.is_empty()
    }
}

struct CookieDetect {
    name: Option<String>,
    value: String,
    kw: Option<String>,
}

struct DecoyDetect {
    trap: String,
    prefix: String,
    exact: bool,
}

struct FormDetect {
    name: Option<String>,
    value: String,
    kw: Option<String>,
}

struct Sqli {
    cfg: SqlInjection,
    paths: Vec<String>,
    any_path: bool,
}

impl Sqli {
    fn owns_path(&self, path: &str) -> bool {
        self.any_path || self.paths.iter().any(|p| p == path)
    }
}

struct Compiled {
    plans: HashMap<String, Plan>,
    default_plan: Plan,
    triggers: Vec<String>,
    header_detect: Vec<String>,
    cookie_detect: Vec<CookieDetect>,
    decoy_detect: Vec<DecoyDetect>,
    form_detect: Vec<FormDetect>,
    sqli: Option<Sqli>,
}

impl Compiled {
    fn plan_for(&self, path: &str) -> &Plan {
        self.plans.get(path).unwrap_or(&self.default_plan)
    }
}

fn nonempty(s: &Option<String>) -> Option<String> {
    s.as_ref().filter(|v| !v.is_empty()).cloned()
}

fn nonempty_str(s: &str) -> Option<String> {
    if s.is_empty() {
        None
    } else {
        Some(s.to_string())
    }
}

// `path` None builds the default plan: only "/*" tokens apply.
fn covers(paths: &[String], path: Option<&str>) -> bool {
    paths.iter().any(|p| p == "/*" || Some(p.as_str()) == path)
}

fn compile(cfg: Config) -> Compiled {
    let ht = cfg.honeytokens.unwrap_or_default();
    let comments: Vec<&HtmlComment> = ht.html_comments.iter().filter(|t| is_on(&t.enabled)).collect();
    let headers: Vec<&HeaderToken> = ht.http_headers.iter().filter(|t| is_on(&t.enabled)).collect();
    let cookies: Vec<&CookieToken> = ht.cookies.iter().filter(|t| is_on(&t.enabled)).collect();
    let decoys: Vec<&DecoyPathToken> = ht.decoy_paths.iter().filter(|t| is_on(&t.enabled)).collect();
    let forms: Vec<&FormFieldToken> = ht.form_fields.iter().filter(|t| is_on(&t.enabled)).collect();

    let build = |path: Option<&str>| -> Plan {
        let joined: Vec<&str> = comments
            .iter()
            .filter(|t| covers(&t.paths, path))
            .filter_map(|t| t.comment_value.as_deref())
            .collect();
        let mut extras = Vec::new();
        for t in &decoys {
            if t.advertise_via.as_deref().unwrap_or("link") == "link" && covers(&t.advertise_on_paths, path) {
                let markup = format!(
                    "<a href=\"{}\" style=\"display:none\">{}</a>",
                    t.trap_path,
                    t.link_text.as_deref().unwrap_or("")
                );
                extras.push(Extra { kind: "decoy_paths", anchor: b"</body>", markup: markup.into_bytes() });
            }
        }
        for t in &forms {
            if !t.field_name.is_empty() && covers(&t.paths, path) {
                let markup = format!("<input type=\"hidden\" name=\"{}\" value=\"{}\">", t.field_name, t.field_value);
                extras.push(Extra { kind: "form_fields", anchor: b"</form>", markup: markup.into_bytes() });
            }
        }
        Plan {
            comments: if joined.is_empty() { None } else { Some(joined.join("\n").into_bytes()) },
            headers: headers
                .iter()
                .filter(|t| !t.header_name.is_empty() && covers(&t.paths, path))
                .map(|t| (t.header_name.clone(), t.header_value.clone()))
                .collect(),
            cookies: cookies
                .iter()
                .filter(|t| !t.cookie_name.is_empty() && covers(&t.paths, path))
                .map(|t| {
                    let mut c = format!("{}={}", t.cookie_name, t.cookie_value);
                    if !t.attributes.is_empty() {
                        c.push_str("; ");
                        c.push_str(&t.attributes);
                    }
                    c
                })
                .collect(),
            extras,
        }
    };

    let mut exact: Vec<&String> = Vec::new();
    for paths in comments
        .iter()
        .map(|t| &t.paths)
        .chain(headers.iter().map(|t| &t.paths))
        .chain(cookies.iter().map(|t| &t.paths))
        .chain(forms.iter().map(|t| &t.paths))
        .chain(decoys.iter().map(|t| &t.advertise_on_paths))
    {
        exact.extend(paths.iter().filter(|p| *p != "/*"));
    }
    let mut plans = HashMap::new();
    for p in exact {
        if !plans.contains_key(p.as_str()) {
            plans.insert(p.clone(), build(Some(p.as_str())));
        }
    }
    let default_plan = build(None);

    Compiled {
        plans,
        default_plan,
        triggers: comments.iter().filter_map(|t| nonempty(&t.trigger_keyword)).collect(),
        header_detect: headers.iter().filter_map(|t| nonempty(&t.trigger_keyword)).collect(),
        cookie_detect: cookies
            .iter()
            .map(|t| CookieDetect {
                name: nonempty_str(&t.cookie_name),
                value: t.cookie_value.clone(),
                kw: nonempty(&t.trigger_keyword),
            })
            .filter(|d| d.name.is_some() || d.kw.is_some())
            .collect(),
        decoy_detect: decoys
            .iter()
            .filter(|t| !t.trap_path.is_empty())
            .map(|t| DecoyDetect {
                trap: t.trap_path.clone(),
                prefix: format!("{}/", t.trap_path),
                exact: t.match_type.as_deref() == Some("exact"),
            })
            .collect(),
        form_detect: forms
            .iter()
            .map(|t| FormDetect {
                name: nonempty_str(&t.field_name),
                value: t.field_value.clone(),
                kw: nonempty(&t.trigger_keyword),
            })
            .filter(|d| d.name.is_some() || d.kw.is_some())
            .collect(),
        sqli: cfg.sql_injection.filter(|s| is_on(&s.enabled)).map(|s| Sqli {
            any_path: s.paths.iter().any(|p| p == "/*"),
            paths: s.paths.iter().filter(|p| *p != "/*").cloned().collect(),
            cfg: s,
        }),
    }
}

// ── Contexts ─────────────────────────────────────────────────────────────────────────────────

// Root context: created once per WASM VM; holds the compiled config shared by all HTTP streams on this worker.
struct HoneypotRoot {
    compiled: Option<Rc<Compiled>>,
    // In-memory attacker store shared across all HTTP contexts on this worker's VM,
    // mirroring OpenResty's ngx.shared.wadm_state.
    detected_ips: Rc<RefCell<HashSet<String>>>,
}

struct HoneypotHttp {
    compiled: Option<Rc<Compiled>>,
    detected_ips: Rc<RefCell<HashSet<String>>>,
    // Raw request path, kept for the response phase, which can no longer read :path.
    request_path: String,
    // HTML response whose plan has body work, so the body is buffered and spliced.
    inject: bool,
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
                    self.compiled = Some(Rc::new(compile(cfg)));
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
            compiled: self.compiled.clone(),
            detected_ips: self.detected_ips.clone(),
            request_path: String::new(),
            inject: false,
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
    // Detection: SQLi trap first (it owns the whole request), then the additional kinds, then
    // the benchmarked html_comments scan. Every header read is a host call into Envoy, so only
    // the headers a request actually needs are fetched.
    fn on_http_request_headers(&mut self, _num_headers: usize, end_of_stream: bool) -> Action {
        let c = match self.compiled.clone() {
            Some(c) => c,
            None => return Action::Continue,
        };
        let target = self.get_http_request_header(":path").unwrap_or_default();
        let (path, query) = target.split_once('?').unwrap_or((target.as_str(), ""));
        self.request_path = path.to_string();
        let ip = self.peer_ip();

        // Fake SQL-injection trap. Placed first and returning unconditionally: an owned request
        // never reaches any other detector, keeping all four edges observably identical. POST-only,
        // so the k6 GET population (including GET /api/login?password=…) is unaffected.
        // Unlike the other three edges this cannot short-circuit into a local reply. In Envoy's
        // proxy-wasm host, Action::Pause on request headers stops the stream without ever
        // delivering on_http_request_body, and once headers have continued, send_http_response
        // returns BadArgument (which the SDK unwraps into a VM panic). So the trap detects on
        // the request body and swaps the upstream reply for its page on the response side. The
        // attacker-visible result is byte-identical; only the origin round-trip differs.
        if let Some(s) = &c.sqli {
            if s.owns_path(path) {
                let method = self.get_http_request_header(":method").unwrap_or_default();
                if s.cfg.methods.iter().any(|m| *m == method) {
                    self.sqli_method = method;
                    self.sqli_ip = ip;
                    if end_of_stream {
                        self.sqli_prepare(&c, "");
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
            }
        }

        let host = self
            .get_http_request_header(":authority")
            .or_else(|| self.get_http_request_header("host"))
            .unwrap_or_default();
        let cookie = if c.cookie_detect.is_empty() {
            String::new()
        } else {
            self.get_http_request_header("cookie").unwrap_or_default()
        };
        let ctx = DetectCtx {
            ip: &ip,
            path,
            host: &host,
            cookie: &cookie,
            segs: parse_query(query),
        };

        self.detect_kinds(&c, &ctx);
        self.detect_comments(&c, &ctx);
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

        let c = match self.compiled.clone() {
            Some(c) => c,
            None => return Action::Continue,
        };
        let raw = self
            .get_http_request_body(0, body_size)
            .map(|b| String::from_utf8_lossy(&b).into_owned())
            .unwrap_or_default();
        self.sqli_prepare(&c, &raw);
        Action::Continue
    }

    // Drops Content-Length for HTML that will be injected (the body grows), then writes the
    // response-header honeytokens (any content type). Always continues so headers flow.
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

        let c = match self.compiled.clone() {
            Some(c) => c,
            None => return Action::Continue,
        };
        let plan = c.plan_for(&self.request_path);

        if plan.has_body() {
            let ct = self.get_http_response_header("content-type").unwrap_or_default();
            if ct.contains("text/html") {
                self.inject = true;
                self.set_http_response_header("content-length", None);
            }
        }

        // Token selection is precompiled; the timed region is the header write itself.
        if !plan.headers.is_empty() {
            let start = self.get_current_time();
            for (name, value) in &plan.headers {
                self.set_http_response_header(name, Some(value));
            }
            let elapsed = self.elapsed_us(start);
            warn!("WADM TOKEN http_headers inject (us): {}", elapsed);
        }
        if !plan.cookies.is_empty() {
            let start = self.get_current_time();
            for cookie in &plan.cookies {
                self.add_http_response_header("set-cookie", cookie);
            }
            let elapsed = self.elapsed_us(start);
            warn!("WADM TOKEN cookies inject (us): {}", elapsed);
        }
        Action::Continue
    }

    // Canonical injection contract (shared with OpenResty / Envoy+Lua / Apache):
    //   * Pause per chunk until end_of_stream so the whole body is buffered before any timer.
    //   * Content-type guard + plan lookup are setup → OUTSIDE the timers.
    //   * Per-kind timers wrap locate-anchor → splice; the html_comments timer wraps
    //     splice → write-back.
    // The body is handled as bytes: decoding it as UTF-8 first cost a full validation pass and
    // skipped injection entirely for non-UTF-8 pages, which the Lua edges inject into.
    fn on_http_response_body(&mut self, body_size: usize, end_of_stream: bool) -> Action {
        // Trap request: discard whatever the origin said and emit the page instead.
        if let Some(page) = self.sqli_page.as_deref() {
            if !end_of_stream {
                return Action::Pause;
            }
            self.set_http_response_body(0, body_size, page.as_bytes());
            return Action::Continue;
        }
        if !self.inject {
            return Action::Continue;
        }
        if !end_of_stream {
            return Action::Pause;
        }

        let c = match self.compiled.clone() {
            Some(c) => c,
            None => return Action::Continue,
        };
        let plan = c.plan_for(&self.request_path);
        let mut body = match self.get_http_response_body(0, body_size) {
            Some(b) => b,
            None => return Action::Continue,
        };

        for x in &plan.extras {
            let start = self.get_current_time();
            let spliced = splice_before(&body, x.anchor, &x.markup);
            let elapsed = self.elapsed_us(start);
            if let Some(nb) = spliced {
                body = nb;
            }
            warn!("WADM TOKEN {} inject (us): {}", x.kind, elapsed);
        }

        match &plan.comments {
            Some(injection) => {
                let start = self.get_current_time();
                let out = splice_or_append(body, injection);
                self.set_http_response_body(0, body_size, &out);
                let elapsed = self.elapsed_us(start);
                warn!("WASM Injection execution time (us): {}", elapsed);
            }
            None => self.set_http_response_body(0, body_size, &body),
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

    // The socket peer, as OpenResty ($remote_addr) and Apache (useragent_ip) record it. Reading
    // X-Forwarded-For here before meant every attacker was recorded as "unknown", since k6 never
    // sends it; one property read also replaces two header lookups that always missed.
    fn peer_ip(&self) -> String {
        self.get_property(vec!["source", "address"])
            .and_then(|b| String::from_utf8(b).ok())
            .map(|a| strip_port(&a).to_string())
            .unwrap_or_else(|| "unknown".to_string())
    }

    // Match and render the trap page, stashing it for the response phase. Timed region covers
    // parse → match → render only, mirroring the detection-timer contract.
    fn sqli_prepare(&mut self, c: &Compiled, raw: &str) {
        let cfg = match &c.sqli {
            Some(s) => &s.cfg,
            None => return,
        };

        let start = self.get_current_time();
        let hit = sqli_match(cfg, raw);
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
            self.detected_ips.borrow_mut().insert(self.sqli_ip.clone());
            // Label deliberately shares no substring with the benchmark scrapers'
            // "Detection/Injection execution time (us):" patterns.
            warn!("WASM WADM SQLI trap build (us): {}", elapsed);
        } else {
            // These requests are answered by the edge, so this is the only record that the trap
            // endpoint was hit.
            warn!(
                "WADM TRAP: {} {} {} answered locally with {} (no signature matched)",
                self.sqli_ip, self.sqli_method, self.request_path, status
            );
        }

        self.sqli_page = Some(page);
        self.sqli_status = status;
    }

    // Detection for the additional honeytoken kinds, one timed region per kind. The timed region
    // covers the kind's own scan plus the in-memory IP record — the same unit the html_comments
    // detection timer measures — while ctx construction stays outside as setup and alert
    // rendering/logging happens after the timer closes.
    fn detect_kinds(&self, c: &Compiled, ctx: &DetectCtx<'_>) {
        let kinds: [(&str, bool, DetectFn); 4] = [
            ("http_headers", c.header_detect.is_empty(), detect_http_headers),
            ("cookies", c.cookie_detect.is_empty(), detect_cookies),
            ("decoy_paths", c.decoy_detect.is_empty(), detect_decoy_paths),
            ("form_fields", c.form_detect.is_empty(), detect_form_fields),
        ];
        for (name, empty, detect) in kinds {
            if empty {
                continue;
            }
            let mut hits: Vec<Alert> = Vec::new();
            let start = self.get_current_time();
            detect(c, ctx, &mut hits);
            if !hits.is_empty() {
                self.detected_ips.borrow_mut().insert(ctx.ip.to_string());
            }
            let elapsed = self.elapsed_us(start);

            if !hits.is_empty() {
                for hit in &hits {
                    warn!("{}", format_alert(ctx.ip, hit));
                }
                // Timed only on a hit, so every edge samples the same population.
                warn!("WADM TOKEN {} detect (us): {}", name, elapsed);
            }
        }
    }

    // html_comments detection (the benchmarked reference). Query parsing happens in setup, so
    // the timed region is scan → strip → write back → record, the same on all edges.
    fn detect_comments(&self, c: &Compiled, ctx: &DetectCtx<'_>) {
        if c.triggers.is_empty() {
            return;
        }
        let mut hits: Vec<Alert> = Vec::new();
        let start = self.get_current_time();

        let mut dropped: Option<Vec<bool>> = None;
        for (i, s) in ctx.segs.iter().enumerate() {
            for kw in &c.triggers {
                if s.key.contains(kw.as_str()) || s.value.contains(kw.as_str()) {
                    hits.push(Alert::QueryKeyword(kw.clone(), s.key.to_string(), s.value.to_string()));
                    dropped.get_or_insert_with(|| vec![false; ctx.segs.len()])[i] = true;
                }
            }
        }
        if let Some(dropped) = &dropped {
            let kept: Vec<&str> = ctx
                .segs
                .iter()
                .zip(dropped)
                .filter(|(_, d)| !**d)
                .map(|(s, _)| s.raw)
                .collect();
            let new_path = if kept.is_empty() {
                ctx.path.to_string()
            } else {
                format!("{}?{}", ctx.path, kept.join("&"))
            };
            self.set_http_request_header(":path", Some(&new_path));
        }
        if !hits.is_empty() {
            self.detected_ips.borrow_mut().insert(ctx.ip.to_string());
        }
        let elapsed = self.elapsed_us(start);

        if !hits.is_empty() {
            for hit in &hits {
                warn!("{}", format_alert(ctx.ip, hit));
            }
            // Only trigger-bearing requests are timed so all edges sample the same population.
            warn!("WASM Detection execution time (us): {}", elapsed);
        }
    }
}

// ── Detection ────────────────────────────────────────────────────────────────────────────────

// One query-string segment. `raw` is kept so the strip can rebuild the query from the segments
// it keeps without re-encoding them; key/value borrow from it unless decoding was needed.
struct Segment<'a> {
    raw: &'a str,
    key: Cow<'a, str>,
    value: Cow<'a, str>,
}

// Per-request surfaces the detectors read, gathered once outside every timed region so the
// timers measure matching work only (mirrors the Lua edges' ctx tables).
struct DetectCtx<'a> {
    ip: &'a str,
    path: &'a str,
    host: &'a str,
    cookie: &'a str,
    segs: Vec<Segment<'a>>,
}

type DetectFn = fn(&Compiled, &DetectCtx<'_>, &mut Vec<Alert>);

// Decoding is skipped for the common segment that has nothing to decode.
fn parse_query(q: &str) -> Vec<Segment<'_>> {
    q.split('&')
        .filter(|s| !s.is_empty())
        .map(|raw| {
            let (k, v) = raw.split_once('=').unwrap_or((raw, ""));
            if raw.contains(['%', '+']) {
                Segment { raw, key: Cow::Owned(url_decode(k)), value: Cow::Owned(url_decode(v)) }
            } else {
                Segment { raw, key: Cow::Borrowed(k), value: Cow::Borrowed(v) }
            }
        })
        .collect()
}

// Substring scan of a request's target surface (path, Host, query) for a replayed value.
fn keyword_in_request(kw: &str, ctx: &DetectCtx<'_>) -> bool {
    ctx.path.contains(kw)
        || ctx.host.contains(kw)
        || ctx.segs.iter().any(|s| s.key.contains(kw) || s.value.contains(kw))
}

fn detect_http_headers(c: &Compiled, ctx: &DetectCtx<'_>, hits: &mut Vec<Alert>) {
    for kw in &c.header_detect {
        if keyword_in_request(kw, ctx) {
            hits.push(Alert::HeaderReplay(kw.clone()));
        }
    }
}

// A real browser replays the planted value unchanged, so a value that returns DIFFERENT is an
// attacker tampering with it (near-zero false positives).
fn detect_cookies(c: &Compiled, ctx: &DetectCtx<'_>, hits: &mut Vec<Alert>) {
    for t in &c.cookie_detect {
        if let Some(name) = &t.name {
            if let Some(v) = cookie_value(ctx.cookie, name) {
                if v != t.value {
                    hits.push(Alert::CookieTamper(name.clone(), v.to_string(), t.value.clone()));
                }
            }
        }
        if let Some(kw) = &t.kw {
            if keyword_in_request(kw, ctx) {
                hits.push(Alert::CookieReplay(kw.clone()));
            }
        }
    }
}

fn detect_decoy_paths(c: &Compiled, ctx: &DetectCtx<'_>, hits: &mut Vec<Alert>) {
    for t in &c.decoy_detect {
        if ctx.path == t.trap || (!t.exact && ctx.path.starts_with(&t.prefix)) {
            hits.push(Alert::DecoyHit(t.trap.clone(), ctx.path.to_string()));
        }
    }
}

// The first mismatching submission is the tamper evidence; later duplicates add nothing.
fn detect_form_fields(c: &Compiled, ctx: &DetectCtx<'_>, hits: &mut Vec<Alert>) {
    for t in &c.form_detect {
        if let Some(name) = &t.name {
            if let Some(s) = ctx.segs.iter().find(|s| s.key == name.as_str() && s.value != t.value.as_str()) {
                hits.push(Alert::FormTamper(name.clone(), s.value.to_string(), t.value.clone()));
            }
        }
        if let Some(kw) = &t.kw {
            if keyword_in_request(kw, ctx) {
                hits.push(Alert::FormKeyword(kw.clone()));
            }
        }
    }
}

// A recorded hit, kept as data so that alert *formatting and writing* both sit outside the
// detection timer. Log I/O otherwise dominated the measurement (see docs/EDGE_LEVELING.md).
// The wire format is identical on all edges.
enum Alert {
    HeaderReplay(String),
    CookieTamper(String, String, String),
    CookieReplay(String),
    DecoyHit(String, String),
    FormTamper(String, String, String),
    FormKeyword(String),
    QueryKeyword(String, String, String),
}

fn format_alert(ip: &str, hit: &Alert) -> String {
    let body = match hit {
        Alert::HeaderReplay(kw) => format!("http_header value '{}' replayed in request", kw),
        Alert::CookieTamper(name, got, expected) => format!(
            "cookie '{}' tampered (got '{}', expected '{}')",
            name,
            log_safe(got),
            expected
        ),
        Alert::CookieReplay(kw) => format!("cookie value '{}' replayed in request", kw),
        Alert::DecoyHit(trap, path) => format!("decoy path '{}' requested ({})", trap, log_safe(path)),
        Alert::FormTamper(name, got, expected) => format!(
            "form field '{}' tampered (got '{}', expected '{}')",
            name,
            log_safe(got),
            expected
        ),
        Alert::FormKeyword(kw) => format!("form field keyword '{}' seen in request", kw),
        Alert::QueryKeyword(kw, key, value) => format!(
            "keyword '{}' found in query param '{}={}'",
            kw,
            log_safe(key),
            log_safe(value)
        ),
    };
    format!("WADM ALERT: honeytoken triggered by {} — {}", ip, body)
}

// Read one cookie's value from the raw Cookie header (None if absent).
fn cookie_value<'a>(cookie_header: &'a str, name: &str) -> Option<&'a str> {
    for part in cookie_header.split(';') {
        if let Some((k, v)) = part.split_once('=') {
            if k.trim() == name {
                return Some(v.trim());
            }
        }
    }
    None
}

// Envoy reports "ip:port" (or "[v6]:port"); the other edges record the bare address.
fn strip_port(addr: &str) -> &str {
    if let Some(rest) = addr.strip_prefix('[') {
        return match rest.find(']') {
            Some(end) => &rest[..end],
            None => addr,
        };
    }
    match addr.split_once(':') {
        Some((ip, port)) if !port.contains(':') => ip,
        _ => addr,
    }
}

// ── SQLi trap helpers ────────────────────────────────────────────────────────────────────────

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
// making the reflected payload differ between edges on a multi-field hit. Every pair is decoded
// (not only those containing % or +) because the signatures must see the normalised text.
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
    s.replace(['\r', '\n'], " ")
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

// ── Body splice ──────────────────────────────────────────────────────────────────────────────

// First occurrence of `needle`, scanning for its first byte and comparing only at candidates.
fn find_bytes(hay: &[u8], needle: &[u8]) -> Option<usize> {
    let n = needle.len();
    if n == 0 || hay.len() < n {
        return None;
    }
    let last_start = hay.len() - n;
    let mut i = 0;
    while i <= last_start {
        let p = i + hay[i..=last_start].iter().position(|&b| b == needle[0])?;
        if &hay[p..p + n] == needle {
            return Some(p);
        }
        i = p + 1;
    }
    None
}

// Splice `insert` before the first `anchor`; None when the anchor is absent, so each caller
// picks its own fallback (additional kinds skip, html_comments appends).
fn splice_before(body: &[u8], anchor: &[u8], insert: &[u8]) -> Option<Vec<u8>> {
    let pos = find_bytes(body, anchor)?;
    let mut out = Vec::with_capacity(body.len() + insert.len() + 1);
    out.extend_from_slice(&body[..pos]);
    out.extend_from_slice(insert);
    out.push(b'\n');
    out.extend_from_slice(&body[pos..]);
    Some(out)
}

fn splice_or_append(body: Vec<u8>, insert: &[u8]) -> Vec<u8> {
    match splice_before(&body, b"</body>", insert) {
        Some(out) => out,
        None => {
            let mut out = body;
            out.extend_from_slice(insert);
            out
        }
    }
}
