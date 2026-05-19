use log::warn;
use proxy_wasm::traits::*;
use proxy_wasm::types::*;
use serde::Deserialize;
use std::rc::Rc;

// Registers the WASM plugin with Envoy: sets log level and the root context factory (entry point for all streams).
proxy_wasm::main! {{
    proxy_wasm::set_log_level(LogLevel::Warn);
    proxy_wasm::set_root_context(|_| -> Box<dyn RootContext> {
        Box::new(HoneypotRoot { config: None })
    });
}}

// Top-level JSON from Envoy plugin `configuration` (same shape as repo `config.json`).
#[derive(Deserialize, Clone)]
struct Config {
    honeytokens: Option<Honeytokens>,
}

// Groups injectable HTML comment tokens under one key so the schema can grow later.
#[derive(Deserialize, Clone)]
struct Honeytokens {
    html_comments: Option<Vec<HtmlComment>>,
}

// One honeytoken row: where to inject, what HTML comment to add, optional secret substring to watch for in requests.
#[derive(Deserialize, Clone)]
struct HtmlComment {
    paths: Vec<String>,
    comment_value: String,
    trigger_keyword: Option<String>,
}

// Root context: created once per WASM VM; holds parsed config shared by all HTTP streams on this worker.
struct HoneypotRoot {
    config: Option<Rc<Config>>,
}

// Per-request state: cheap clone of config Rc, URI path for injection matching, and HTML flag for body buffering.
struct HoneypotHttp {
    config: Option<Rc<Config>>,
    request_path: String,
    is_html_response: bool,
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
            request_path: String::new(),
            is_html_response: false,
        }))
    }

    // Declares that this root context produces HTTP-level child contexts (required for HTTP filter chain).
    fn get_type(&self) -> Option<ContextType> {
        Some(ContextType::HttpContext)
    }
}

impl Context for HoneypotHttp {}

impl HttpContext for HoneypotHttp {
    // Strips trigger keywords from `:path` (including query) so secrets do not reach upstream; stores path-only for injection rules.
    fn on_http_request_headers(&mut self, _num_headers: usize, _end_of_stream: bool) -> Action {
        let path = self.get_http_request_header(":path").unwrap_or_default();

        let uri = path.split('?').next().unwrap_or(&path);
        self.request_path = uri.to_string();

        let tokens = match self.html_comments() {
            Some(t) => t,
            None => return Action::Continue,
        };

        let mut cleaned = path.clone();
        for token in tokens {
            if let Some(ref kw) = token.trigger_keyword {
                if !kw.is_empty() && cleaned.contains(kw.as_str()) {
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

        Action::Continue
    }

    // For HTML responses, drops Content-Length (body will grow) and sets flag for body phase. Always continues so
    // headers flow to the client immediately -- Pause here would freeze the entire stream with no way to resume.
    fn on_http_response_headers(&mut self, _num_headers: usize, _end_of_stream: bool) -> Action {
        let ct = self
            .get_http_response_header("content-type")
            .unwrap_or_default();

        if ct.contains("text/html") {
            self.is_html_response = true;
            self.set_http_response_header("content-length", None);
        }

        Action::Continue
    }

    // Buffers all body chunks until end_of_stream (Pause per chunk), then injects comments and releases.
    // Non-HTML streams skip injection entirely via the flag set in on_http_response_headers.
    fn on_http_response_body(&mut self, body_size: usize, end_of_stream: bool) -> Action {
        if !self.is_html_response {
            return Action::Continue;
        }

        if !end_of_stream {
            return Action::Pause;
        }

        let body_bytes = match self.get_http_response_body(0, body_size) {
            Some(b) => b,
            None => return Action::Continue,
        };

        let body_str = match String::from_utf8(body_bytes) {
            Ok(s) => s,
            Err(_) => return Action::Continue,
        };

        let tokens = match self.html_comments() {
            Some(t) => t,
            None => return Action::Continue,
        };

        let mut to_inject = Vec::new();
        for token in tokens {
            for pattern in &token.paths {
                if pattern == "/*" || pattern == self.request_path.as_str() {
                    to_inject.push(token.comment_value.as_str());
                    break;
                }
            }
        }

        if to_inject.is_empty() {
            return Action::Continue;
        }

        let injection = to_inject.join("\n");
        let new_body = if let Some(pos) = body_str.find("</body>") {
            let mut buf = String::with_capacity(body_str.len() + injection.len() + 1);
            buf.push_str(&body_str[..pos]);
            buf.push_str(&injection);
            buf.push('\n');
            buf.push_str(&body_str[pos..]);
            buf
        } else {
            let mut buf = body_str;
            buf.push_str(&injection);
            buf
        };

        self.set_http_response_body(0, body_size, new_body.as_bytes());
        Action::Continue
    }
}

impl HoneypotHttp {
    // Small helper to avoid repeating optional chaining when reading html_comments from nested config.
    fn html_comments(&self) -> Option<&Vec<HtmlComment>> {
        self.config
            .as_ref()
            .and_then(|c| c.honeytokens.as_ref())
            .and_then(|h| h.html_comments.as_ref())
    }
}
