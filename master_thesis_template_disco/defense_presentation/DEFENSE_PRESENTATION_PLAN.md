# Thesis Defence: Slide Plan, Speaker Notes and Q&A Preparation

**Thesis:** *Active Defence via Multi-Type Honeytoken Injection: Evaluating Performance across Lua and
WebAssembly Server Extensions*
**Author:** Mihir Mahesh Ambli, University of Kaiserslautern-Landau (RPTU), Department of Computer
Science, Distributed Systems Lab
**Examiner:** Prof. Dr.-Ing. Jens B. Schmitt **Second supervisor:** M. Sc. Daniel Reti
**Talk length:** 20 minutes, followed by questions. The plan below runs about **18:30**, which leaves
about 1.5 minutes of slack.
**Goal of the defence:** the examiners check that you understand every part of the work: why it was
done, how it was built, how it was measured, and what the numbers do and do not show.

Every number in this file is copied from the final report (`template_final.tex`). Do not round,
change or "improve" any of them on the slides.

---

## Part 0: Instructions for building the deck (paste this part into Claude on Windows)

> Build a 16:9 PowerPoint deck from the slide plan below. Use one slide per section "Slide N".
> Put the "On the slide" content on the slide, the "Speaker notes" text into the notes field of the
> slide, and insert the image named under "Visual" from the `figures/` folder I attach. Where the
> visual says "draw", build the diagram with native PowerPoint shapes. Slides marked "Backup" go
> after the "Questions" slide.
>
> Design rules:
> - Clean academic style: white background, dark text (#0b0b0b), one accent colour for headings,
>   a small slide number and the short title "HDM: Honeytokens in Lua and WebAssembly" in the footer.
> - Use these fixed colours whenever a slide refers to a variant, matching the figures:
>   Envoy Lua **#eda100** (orange), Envoy WebAssembly **#e34948** (red), OpenResty **#2a78d6**
>   (blue), Apache **#1baf7a** (green). Always list the variants in this order:
>   Envoy Lua, Envoy WebAssembly, OpenResty, Apache.
> - At most about 6 short bullet lines per slide; a figure should take at least half the slide when
>   there is one. Numbers in bold where a slide states a key result.
> - Use the exact numbers and the exact terms from this file. British spelling (behaviour,
>   utilisation, levelling).
> - Write "µs" for microseconds.

### Fixed terms (examiners will notice inconsistencies)

| Use | Never use |
|---|---|
| **HDM** (HTTP Deception Module) | "WADM" (that is only the code name) |
| **proxy deployment** / **in-server deployment** | "host server" |
| variant names **Envoy Lua, Envoy WebAssembly, OpenResty, Apache** | "OpenResty in-server", "the in-server variants" |
| **SQL-injection trap**, its log line the **SQL-injection trap line** | a bare "trap" or "trap line" |
| honeytoken **types** | "kinds" of honeytoken |
| detection signals **alteration**, **resource request**, **data use** | |
| tiers **origin, bare, silent, instrumented**; cost = "**silent minus bare**" | "overhead" without saying against what |
| HDM **injects** honeytokens; code is **attached** to hooks | "injecting" code (sounds like an attack) |
| **main benchmark**, **functional matrix**, **request-type and page-size runs** | "paired benchmark" |
| **origin server** = nginx behind Envoy | |
| Apache httpd = the server software; **Apache** = the variant | |

---

## Part 1: Timing overview

| # | Slide | Time | Cumulative |
|---|---|---|---|
| 1 | Title | 0:20 | 0:20 |
| 2 | Motivation: deception for web applications | 1:00 | 1:20 |
| 3 | Problem and research gap | 1:00 | 2:20 |
| 4 | HDM: five honeytoken types and the SQL-injection trap | 1:20 | 3:40 |
| 5 | Two deployments, four variants | 1:15 | 4:55 |
| 6 | Research questions | 0:45 | 5:40 |
| 7 | Per-request processing | 0:50 | 6:30 |
| 8 | Making the variants comparable | 1:10 | 7:40 |
| 9 | Testbed and workload | 1:10 | 8:50 |
| 10 | Tiers and pairing | 0:55 | 9:45 |
| 11 | Worked example: from requests to a comparison | 0:55 | 10:40 |
| 12 | Measurement planes and validity | 0:45 | 11:25 |
| 13 | RQ1: latency | 1:20 | 12:45 |
| 14 | RQ2: CPU time and where it arises | 1:20 | 14:05 |
| 15 | RQ3: functional correctness | 1:15 | 15:20 |
| 16 | RQ4: runtime and deployment | 1:05 | 16:25 |
| 17 | Synthesis: choosing a deployment and a variant | 0:50 | 17:15 |
| 18 | Validity and limitations | 0:35 | 17:50 |
| 19 | Conclusion and future work | 0:35 | 18:25 |
| 20 | Questions | n/a | |
| B1 to B8 | Backup slides | only if asked | |

Weighting: about 7 minutes on the problem and what you built (slides 2 to 8), about 4 minutes on the
method (slides 9 to 12), about 6 minutes on the results (slides 13 to 17), and about 1 minute to
close. The method gets a large share on purpose: it is where the examiners can best see whether you
understand your own numbers.

---

## Part 2: The slides

### Slide 1: Title (0:20)

**On the slide**
- Title: *Active Defence via Multi-Type Honeytoken Injection: Evaluating Performance across Lua and
  WebAssembly Server Extensions*
- Mihir Mahesh Ambli, Master Thesis Defence
- Examiner: Prof. Dr.-Ing. Jens B. Schmitt; second supervisor: M. Sc. Daniel Reti
- University of Kaiserslautern-Landau, Distributed Systems Lab, date of the defence
- RPTU logo (`Final_Report_Workspace/img/RPTUlogo.svg`)

**Speaker notes**
"Good morning. My thesis asks what it costs to plant honeytokens into the traffic of a web
application, and whether that cost depends on where the deception code runs and which runtime
executes it."

---

### Slide 2: Motivation: deception for web applications (1:00)

**On the slide**
- Exploiting a software vulnerability: **31 %** of breaches, the most common way in (Verizon DBIR 2026)
- Firewalls and IDS: weak against unknown (zero-day) attacks; **false alarms** limit them (Axelsson)
- **Deception**: fake but plausible elements that no legitimate user touches, so a touch is an alert
- **Honeytoken** = fake data, e.g. a fake credential in an HTML comment, a hidden link
- Han et al.: deceptive elements in a production web app for 7 months, **no false alarm**

**Visual:** draw a small browser window showing page source with a highlighted line
`<!-- DEV-PORTAL: internal-admin.example.com -->` and a hidden link `debug console`, with an arrow
"attacker reads source → uses the value → alert".

**Speaker notes**
"Web applications are reachable by anyone, and in the 2026 Verizon report exploiting a vulnerability
was the most common way into an organisation, 31 per cent of breaches. Classic defences try to keep
attackers out or spot them, but they struggle with unknown vulnerabilities, and intrusion detection
drowns in false alarms. Deception adds a different signal: elements that look real but that no
legitimate user has any reason to use. In a web application these are honeytokens: a fake hostname in
an HTML comment, a hidden link, a cookie. An attacker reconnoitring the application reads exactly these
artefacts. OWASP's testing guide even tells testers to read comments and robots.txt. Whoever uses a
honeytoken reveals the attack, with very few false alarms. Han et al. ran this in production for seven
months without a single false alarm, but they also found it does not catch every attacker, so it is a
complement to other defences, not a replacement."

---

### Slide 3: Problem and research gap (1:00)

**On the slide**
- Deploying deception without changing the app: a **reverse proxy** is the most common method in
  research (Kahlhofer and Rass); web servers can run the same code **in-server**
- Cost largely unknown: proxies "often add significant overhead"; deception systems report little
  about their own cost
- Existing measurements (Zhu et al.): Envoy filters that **do nothing**; no OpenResty, no Apache httpd
- Rewriting responses can **break HTTP** (length, compression, partial content)
- **Gap:** no measured basis for choosing the deployment, the server and the runtime, and no
  estimate of the cost in latency, CPU time and correctness

**Visual:** none, or a small two-column "known / unknown" layout.

**Speaker notes**
"An operator usually has the built application, not its source code. So deception has to be added from
outside: in a reverse proxy in front of the application, which is the most common approach in research,
or, as I add in this thesis, inside the web server that already serves the pages. Both mean running code
on every request and rewriting response bodies. What does that cost? Koney and Kahlhofer's survey say
proxies often add significant overhead but do not measure the deception itself. The closest measurement,
Zhu et al., used Envoy filters that do nothing. That shows the cost of the extension mechanism, but not
of searching requests and rewriting bodies, and it covers neither nginx nor Apache. Rewriting can also
break responses. So the problem is that an operator has no measured basis for choosing where and in what
to run the deception, or what it will cost."

---

### Slide 4: HDM: five honeytoken types and the SQL-injection trap (1:20)

**On the slide**

| Type | Planted as | Detected when | Signal |
|---|---|---|---|
| HTML comment | comment before `</body>` | its keyword appears in the query string | data use |
| HTTP header | decoy response header | its value reappears in path, Host or query | data use |
| Cookie | `Set-Cookie admin_ui=0` | comes back with another value | alteration |
| Decoy path | hidden link to `/api/v1/debug` | that path (or below) is requested | resource request |
| Form field | hidden `is_admin=0` before `</form>` | submitted with another value | alteration |

- **SQL-injection trap:** fake `POST /api/login`, answered by HDM itself; decode, lower-case,
  collapse whitespace, search **22 literal signatures**
  - signature found → **500** fake MySQL error (input HTML-escaped, max 200 chars) + alert
  - no signature → **401** "sign in failed" + SQL-injection trap line
- One configuration file for all variants

**Visual:** the table above (native table).

**Speaker notes**
"I built the HTTP Deception Module, HDM. It plants five honeytoken types. Each maps to one of the three
OWASP AppSensor signals. Data use: the attacker reuses a value it could only have read in the source,
like the hostname in the comment or the decoy header value. Alteration: the attacker changes a value the
server planted, like the cookie or the hidden is_admin field. A browser returns these unchanged, so a
change means tampering. Resource request: the attacker requests a path that is only advertised in a
hidden link. When a honeytoken triggers, HDM writes an alert line and records the client address, taken
from the connection, not from a header the client could forge. A triggered comment keyword is also
removed from the query before the request goes on.

Second, HDM has an SQL-injection trap: a fake login endpoint that exists only in HDM. It searches the
username and password fields for 22 literal signatures like `' or` or `union select`. On a hit it
returns a convincing MySQL error page, which invites the attacker to continue. Without a hit it behaves
like a normal failed login. Two security details: line breaks in attacker input are replaced before
logging, so alert lines cannot be forged, and reflected input is HTML-escaped, so the trap itself is not
an XSS hole."

---

### Slide 5: Two deployments, four variants (1:15)

**On the slide**
- Left: diagram of the two deployments
- Right: table

| Variant | Server software | Deployment | Runtime |
|---|---|---|---|
| Envoy Lua | Envoy 1.30 (dedicated proxy), Lua HTTP filter | proxy | LuaJIT |
| Envoy WebAssembly | Envoy 1.30, Wasm filter (Rust, Proxy-Wasm) | proxy | WebAssembly on V8 |
| OpenResty | nginx + lua-nginx-module | in-server | LuaJIT |
| Apache | Apache httpd 2.4, Lua module | in-server | PUC Lua 5.1 |

- Same two phases everywhere: **request phase: detect**, **response phase: inject**

**Visual:** `figures/diagram_deployments.png` (left half of slide).

**Speaker notes**
"HDM runs in two deployments. In the proxy deployment, a reverse proxy runs HDM in front of an
unmodified origin server; I use Envoy, a dedicated proxy, extended through HTTP filters. In the in-server
deployment, HDM runs inside the web server that reads the pages from its own disk, with no proxy; there
HDM is Lua code run by a server module. The design is the same in both, because both have a request
phase where HDM detects and a response phase where it injects. Only the source of the response differs.

The four variants are chosen so that comparisons isolate factors. The two Envoy variants share server
and deployment and differ only in the runtime: LuaJIT against WebAssembly on V8. Envoy Lua and OpenResty
share LuaJIT but differ in deployment and server. Apache is the only variant whose code is always
interpreted, by PUC Lua. Envoy appears only as a proxy because it cannot serve files: each Envoy route
can only forward, redirect or return a fixed response."

---

### Slide 6: Research questions (0:45)

**On the slide**
- **RQ1 Latency:** how much does HDM add (median, p95), compared with what the reverse proxy itself
  adds, and how with load?
- **RQ2 CPU time:** how much CPU time per request, and where does it arise (inside HDM's code or
  around it; GET vs SQL-injection trap; page size)?
- **RQ3 Correctness:** apart from the honeytokens, are responses unchanged, intact under load, and is
  every expected alert raised?
- **RQ4 Runtime and deployment:** how much do RQ1 to RQ3 depend on the runtime (LuaJIT, PUC Lua,
  Wasm/V8) and the deployment (Envoy Lua vs OpenResty, both LuaJIT)?

**Speaker notes**
"Four questions. RQ1 is latency, what a client feels, and in the proxy deployment I compare it with the
latency of the proxy itself. RQ2 is CPU time, the work done, which latency cannot show because latency
includes waiting. RQ3 is correctness: does rewriting break responses, and does detection work. RQ4 asks
which differences come from the runtime and which from the deployment."

---

### Slide 7: Per-request processing (0:50)

**On the slide**
- Configuration compiled once at load into a **per-path plan**
- Steps 1 to 4 in the request phase, step 5 in the response phase
- Injection needs the whole body: remove `Content-Length`, wait for the last piece, insert at
  `</body>` / `</form>`
- HDM does **not** check status code or content coding before injecting (matters for RQ3)

**Visual:** `figures/diagram_request_flow.png` (right two-thirds of slide).

**Speaker notes**
"Everything that depends only on the configuration is computed once, at load time, so the per-request
work is a lookup plus the checks. For every request: setup takes the client address from the connection
and splits the query once. A POST to /api/login goes to the SQL-injection trap and ends there. Otherwise
the detectors run in a fixed order, and a matched comment keyword is removed while keeping the order and
encoding of the other parameters. If anything triggered, HDM writes the alert lines. In the response
phase it sets the decoy header and cookie, and for HTML it removes Content-Length, because the body gets
longer, waits for the whole body, and injects at the anchors. Note the last bullet: HDM does not check
compression or status before injecting. That design choice explains several results in RQ3."

---

### Slide 8: Making the variants comparable (1:10)

**On the slide**
- **Levelling:** identical parsing, literal keyword search, same alert lines; server defaults
  aligned (access logs off in OpenResty and Apache, Apache loads scripts once); baseline configuration
  = configuration with HDM minus the HDM parts
- **Parity check:** 13 requests to every variant, compared with OpenResty as reference by 4 rules
  (status/header/cookies; body hash of 200 pages; identical alert lines; requests reaching the origin)
- **Result:** every variant wrote the same **11 alert lines and 1 SQL-injection trap line**; both
  Envoy variants forwarded the same 11 requests, none carrying a keyword
- **Documented differences remain**, e.g.:
  - Envoy WebAssembly: SQL-injection trap requests still reach the origin (interface constraint)
  - Apache: no honeytokens in its own error pages; at most one cookie honeytoken per path

**Visual:** small inset of `figures/diagram_wasm_interface.png` in a corner, labelled "Envoy
WebAssembly: module ↔ Envoy calls".

**Speaker notes**
"Four implementations, three in Lua against different server interfaces and one in Rust, can easily
differ in small details. And if one variant does more work, a cost difference measures the work, not
the platform. So I levelled them: same behaviour step by step, and I removed default differences that
would otherwise be charged to HDM. For example OpenResty and Apache write an access log line per request
by default and Envoy does not, so those logs are off. Then an automated parity check sends 13 requests,
including encoded keywords, duplicated fields and parameter order after removal, and compares every
variant with OpenResty. All wrote the same 11 alert lines and one SQL-injection trap line.

Some differences cannot be removed and are documented. The most interesting one is Envoy WebAssembly. In
Envoy 1.30 the Wasm module can answer a request itself only when the headers arrive, but the
SQL-injection trap needs the body, which comes later. So that variant forwards the request, the origin
answers 404, and the module replaces that 404 with the trap's page. The client sees the same response,
at the cost of one extra round trip."

---

### Slide 9: Testbed and workload (1:10)

**On the slide**
- One VirtualBox VM: **8 vCPUs, 7.8 GB**, host Intel Core i7-13650HX; Docker containers on one bridge
- Pinning: server under test **2 vCPUs**, origin nginx **1 vCPU**, load generator k6 **4 vCPUs**,
  OS 1 vCPU
- Non-blocking logs (64 MB buffer); transparent huge pages and swap disabled
- **Iteration** = 9 requests: 1 unrecorded `GET /` + **8 probes**: 4 GET (injection only; comment
  keyword; tampered form field + header value + cookie; decoy path) + 4 POST to the SQL-injection trap
  (signature yes/no × encoded yes/no)
- Expected per iteration: **7 alert lines + 2 SQL-injection trap lines**
- Probe order: **Williams design**; load: **open model** (k6 constant arrival rate)
- Rates: **1, 10, 100 iterations/s** (9, 90, 900 requests/s)

**Visual:** draw a simple box diagram: [k6, vCPU 4-7] → [server under test, vCPU 1-2] → [nginx origin,
vCPU 3], all inside a box "VM, Docker bridge".

**Speaker notes**
"Everything runs on one VM on my laptop, each component in its own container, pinned to its own vCPUs
so that, for example, the load generator's scheduling delay does not end up in the latency it measures.
Logs are delivered without blocking: otherwise a full log pipe would stall a worker in the middle of a
timed operation and that stall would be charged to HDM.

The workload is built from iterations of nine requests. The first GET is sent but not recorded: the
first request on an idle connection pays a set-up cost, and this request absorbs it. The eight probes
each exercise one part of HDM. The four SQL-injection trap probes form a two-by-two design, because
decoding and finding a signature affect the cost in opposite directions. Probe order follows a Williams
design, so that every probe follows every other probe exactly once over eight iterations. This matters
because, for example, Apache closes the connection after a 500, so the next request pays for a new one.
The load is open: iterations start at a fixed rate regardless of response time. With a closed model, the
slower configuration would receive less load and the two would not be compared under the same load."

---

### Slide 10: Tiers and pairing (0:55)

**On the slide**

| Tier | Proxy variants | OpenResty, Apache | HDM | Timing lines |
|---|---|---|---|---|
| origin | nginx alone | not reported | no | n/a |
| bare | Envoy → nginx | the web server | no | n/a |
| silent | Envoy → nginx | the web server | yes | off |
| instrumented | Envoy → nginx | the web server | yes | on |

- **Cost of HDM = silent minus bare**; reverse proxy's own cost = bare minus origin; timing lines =
  instrumented minus silent
- 5 replicates per rate; the 4 tiers of a replicate run **minutes apart**, order rotated
- Warm-up before each cell (≥ 8 s) so LuaJIT has compiled the hot code
- Reported: **median of the 5 paired differences**, uncertainty = their **range**;
  **resolved** = all 5 have the same sign

**Visual:** the table above plus `figures/diagram_run_schedule.png` (smaller, below or right).

**Speaker notes**
"This is the core of the method. HDM's operations take microseconds, a request takes under a
millisecond, and the VM drifts by hundreds of microseconds between runs. A difference between two
separate runs would measure the drift as much as HDM. So every cost is a difference between two
configurations measured minutes apart in the same replicate. The bare tier is the same server without
HDM, so silent minus bare is HDM alone. If I measured against the origin instead, the cost of the proxy
itself would be counted as HDM's, and I will show that it is larger than HDM's. The silent tier exists
because the timing lines themselves cost time, which a deployed HDM would not spend; the instrumented
tier exists because only there can I time individual operations. For OpenResty and Apache the origin
tier is not reported: there is no proxy to measure, and the comparison would just compare two different
web servers."

---

### Slide 11: Worked example: from requests to a comparison (0:55)

**On the slide**
- Envoy Lua, 10 iterations/s, replicate 1: bare **927 µs**, silent **1,100 µs** → **173 µs**
- Five replicates: 173, 177, 135, 170, 120 → **median 170 µs, range 120 to 177 µs**, all positive:
  resolved
- OpenResty (own run): 68, 89, 74, 81, 83 → **81 µs (68 to 89)**
- 89 < 120: ranges **do not overlap** → OpenResty adds less (rule of Hoefler and Belli)
- Envoy WebAssembly 106 to 128 µs overlaps Envoy Lua → **not ordered**, even though its median is
  lower

**Visual:** `figures/diagram_worked_example.png` (large).

**Speaker notes**
"Here is one result end to end. In one cell, k6 records about 450 durations per GET probe. I take the
median of each probe, average the four medians: 927 microseconds without HDM, 1,100 with HDM, so 173 in
this replicate. Five replicates give five differences. The median, 170, is reported, the range, 120 to
177, is the uncertainty, and because all five are positive the effect is resolved. Variants are measured
in separate runs, so I never compare their raw latencies. I compare their ranges: OpenResty's highest
value, 89, is below Envoy Lua's lowest, 120, so OpenResty is cheaper. If ranges overlap, like Envoy
WebAssembly and Envoy Lua here, I do not order them, even if the medians differ. This is the rule of
Hoefler and Belli for noisy measurements."

---

### Slide 12: Measurement planes and validity (0:45)

**On the slide**
- **Latency** (k6, per probe): what the client experiences
- **CPU time per request** (cgroup `cpu.stat`, sampled 4×/s): the work done; **marginal CPU time** =
  per-request slope of a fit "idle cost + cost per request" over the three rates
- **Timers inside HDM** (instrumented tier only): duration of each operation, in whole µs → reported
  as the **mean**
- Also checked per cell: integrity (status + SHA-256 of body vs reference), new connections, alert
  lines
- Cell rejected if: < 95 % iterations placed or > 1 % dropped; > 0.5 % failed requests; > 1 % of an
  operation's timer samples (and ≥ 3) above 1 ms; origin CPU > 1,000 µs/request; soft lockup or clock jump
- **240 of 240 cells valid** (4 variants × 3 rates × 5 replicates × 4 tiers)

**Speaker notes**
"Each cell is measured on three planes because none is reliable alone on a VM. Latency is what a user
feels. CPU time is read from each container's control group, from outside the server, and excludes
waiting. The marginal fit removes the idle CPU that would otherwise inflate the per-request value at low
load. The timers measure only HDM's own operations, from inside. They write whole microseconds while
most operations take one to five, so I report their mean, not their median. Every cell passes explicit
validity gates, and none of the gates refers to the quantity being measured, so they cannot favour a
result. Worker crashes are recorded but do not reject a cell by themselves, because the requests they
destroy are already caught by the failed-request gates. All 240 cells of the main benchmark were
valid."

---

### Slide 13: RQ1: latency (1:20)

**On the slide**
- Median latency added to the GET probes (silent minus bare):
  - OpenResty **69 to 81 µs** (13 to 20 %)
  - Envoy WebAssembly **110 to 150 µs** (12 to 18 %)
  - Envoy Lua **147 to 203 µs** (18 to 22 %)
  - Apache **183 to 244 µs** (32 to 48 %)
- All **60 of 60** differences resolved; OpenResty lowest at every load; the two Envoy variants **not
  ordered**
- p95: Apache **880 / 622 / 1,491 µs** (range up to 4,132 µs at 100/s)
- The **reverse proxy alone adds 309 to 396 µs**, more than HDM; HDM = 23 to 34 % of the stack's
  addition
- Apache with HDM: **12 to 25 %** of requests opened a new connection (≤ 0.9 % without)

**Visual:** `figures/latency_added.png` (main) and `figures/latency_levels.png` (smaller, below or
on a click).

**Speaker notes**
"HDM adds tens to a couple of hundred microseconds to the median, and every one of the 60 differences is
resolved: in every variant, at every load, all five replicates agree that HDM makes requests slower.
OpenResty adds the least at every load level, with non-overlapping ranges. The Envoy medians favour
WebAssembly, but their ranges overlap, so I do not order them. At the 95th percentile the cost grows,
and Apache goes into the milliseconds at the highest load.

The right figure is for the proxy deployment. The grey circle is direct access to nginx, the hollow
marker Envoy without HDM, the filled marker Envoy with HDM. The proxy alone adds 309 to 396
microseconds, more than HDM does. Part of that is one extra pass over the Docker bridge. If I had
measured HDM against the origin, it would look three to four times as expensive. With load, the absolute
cost stays the same or falls slightly, while its share grows, because the servers themselves answer
faster at 100 iterations per second. Apache also closes the connection after every 500 response of the
SQL-injection trap, so 12 to 25 per cent of its requests had to open a new connection."

---

### Slide 14: RQ2: CPU time and where it arises (1:20)

**On the slide**
- Marginal CPU time added per request: OpenResty **32**, Envoy Lua **45**, Envoy WebAssembly **108**,
  Apache **261 µs**; ranges **do not overlap**
- At 100 iterations/s, utilisation of the 2 vCPUs: +1.4, +2.0, +4.9, +11.7 percentage points;
  every server ≤ 20 % busy
- **Only 5 to 35 % of the added CPU time is inside HDM's timed detection and injection code**
  (Envoy WebAssembly and Apache: 5 to 7 %)
- Order by CPU ≠ order by latency (Envoy WebAssembly: lower latency median, > 2× CPU of Envoy Lua)
- Per GET request: 77 / 162 / 165 / 238 µs; per SQL-injection trap request OpenResty **−28 µs**
  (saves work), Apache **+276 µs**
- From 256 KB on, cost grows roughly in proportion to page size

**Visual:** `figures/cpu_added.png` (top) and `figures/cost_scopes.png` (bottom). If space is tight,
use `cost_scopes.png` only and move `cpu_added.png` to backup.

**Speaker notes**
"CPU time ranks the variants clearly: OpenResty 32 microseconds per request, Envoy Lua 45, Envoy
WebAssembly 108, Apache 261, with non-overlapping ranges. Note the order differs from latency: Envoy
WebAssembly does not add more latency than Envoy Lua, but more than twice the CPU time. Latency includes
waiting, CPU time does not, so an evaluation that reported only one of them would give a different
order.

The most important finding is on the bottom figure. Left: time inside HDM's timed operations. Middle:
CPU time HDM adds. In Envoy WebAssembly and OpenResty the timed operations take the same 54 microseconds
per iteration, but WebAssembly adds 973 microseconds of CPU against 286. Only 5 to 35 per cent of HDM's
cost is in its own detection and injection code. The rest is around it: entering the runtime,
collecting the body, writing log lines. The measurements show how large that part is, but not how it
divides among these causes.

Two more details: on a request to the SQL-injection trap, OpenResty spends less CPU with HDM than
without, because the trap answers in the request phase and saves the 404 handling. Only Apache clearly
pays for the trap. And for large pages the cost grows with size, as expected of code that buffers and
searches the whole body."

---

### Slide 15: RQ3: functional correctness (1:15)

**On the slide**
- Functional matrix: 13 kinds of response, each compared with the same variant without HDM
- **Broken:** Envoy Lua **4**, Envoy WebAssembly **3**, OpenResty **1**, Apache **0** of 13
  - Both Envoy variants: **gzip page** (injected into compressed bytes), **range request**, **2 MB
    page** (> 1 MB buffer); Envoy Lua also **HEAD** length
  - OpenResty: answers a range request with the whole page and status 200
- **ETag kept** (Envoy, OpenResty): a cached page without honeytokens is still answered with 304
- Under load: three variants **100 % intact**, every alert raised; **Apache ≥ 99.97 %** intact,
  ≥ 99.95 % of lines written, worker processes **crashed** (10 to 12 per run at 100/s, never
  without HDM)
- No variant raised an alert without HDM

**Visual:** `figures/functional_matrix.png` (large, left); bullets on the right.

**Speaker notes**
"The functional matrix sends responses the benchmark does not contain and compares each variant with
itself without HDM. In the proxy deployment three things break. The origin compresses the page, and
Envoy injects text into the gzip bytes, so the client cannot decode it. A range request returns a
modified part, which corrupts the reassembled file. A 2 megabyte page exceeds Envoy's 1 megabyte buffer
and is not delivered. Envoy Lua also answers HEAD with the old length. The web servers deliver the
compressed and the large page correctly; OpenResty only turns a range request into a full 200, which
HTTP allows but which the matrix counts as broken. Apache breaks nothing, partly because it injects
nothing into its own error pages.

There is also a subtle issue: the entity tag stays the same, so a cache holding the page without
honeytokens is told its copy is current.

Under load, Envoy Lua, Envoy WebAssembly and OpenResty kept every response intact and wrote every
expected line. Apache's worker processes crashed with HDM, 10 to 12 times per run at the highest load,
never without HDM. It still kept at least 99.97 per cent of responses intact. I recorded the crashes;
their cause was not measured."

---

### Slide 16: RQ4: runtime and deployment (1:05)

**On the slide**
- Every operation takes **0.35 to 5.3 µs**; PUC Lua (Apache) slowest in every detection
- Envoy Lua vs Envoy WebAssembly (same server, different runtime): within **1.4 µs** in every operation
- SQL-injection trap: 2.7 µs (WebAssembly), about 7 µs (LuaJIT), 16 µs (PUC Lua); decoding ≈ 1.3 µs
  in Lua, ≈ 0 in WebAssembly; only PUC Lua pays noticeably for scanning further (2.8 µs)
- **The runtime sets the speed of the operations, but the work around them separates the variants**
- RQ4.3, Envoy Lua vs OpenResty (both LuaJIT): detections within 1 µs, but latency **147 to 203 vs 69
  to 81 µs**, marginal CPU **45 vs 32 µs**, and OpenResty handles gzip and 2 MB
- Caveat: they also differ in server software, so this **cannot be attributed to the deployment
  alone**

**Visual:** `figures/same_runtime.png` (main); `figures/hdm_timers.png` in backup.

**Speaker notes**
"The timers show that the runtime does matter for the operations themselves: PUC Lua, the plain
interpreter, is slowest everywhere, and only there does the length of the signature list matter. But
all operations take a few microseconds, while the variants differ by tens to hundreds. The runtime
mostly matters through the work around the code, for example WebAssembly crossing into Envoy for every
header and body piece.

For RQ4.3 I compare the two LuaJIT variants. Their operations take almost the same time, yet OpenResty
adds about half the latency and less CPU, and it delivers more responses correctly. I have to be careful
here: they differ in the server software as well as in the deployment, so I cannot say the in-server
deployment is cheaper as such. Separating these would need the same server measured in both
deployments, which is future work."

---

### Slide 17: Synthesis: choosing a deployment and a variant (0:50)

**On the slide**

| Variant | Latency added (µs) | Marginal CPU (µs) | Utilisation added (pp, 100/s) | Broken (of 13) | Intact under load |
|---|---|---|---|---|---|
| Envoy Lua | 147 to 203 | 45 (39 to 50) | 2.0 | 4 | all |
| Envoy WebAssembly | 110 to 150 | 108 (99 to 121) | 4.9 | 3 | all |
| OpenResty | 69 to 81 | 32 (25 to 36) | 1.4 | 1 | all |
| Apache | 183 to 244 | 261 (229 to 334) | 11.7 | 0 | ≥ 99.97 % |

- Pages already served by nginx/OpenResty → **OpenResty**: cheapest, no crashes, no extra server
- Need a proxy (any origin, no extension mechanism) → **Envoy Lua**: half the CPU of WebAssembly; but
  skip injection for gzip, ranges and large pages
- Apache: crashes must be resolved first
- On this testbed: the **comparison** is the claim, not the absolute values

**Speaker notes**
"Putting it together: OpenResty is ahead or level in every column except the matrix, where Apache
breaks nothing. For an operator whose pages are already served by nginx, running HDM in the server is
the cheapest option and needs no extra server. A reverse proxy is still the choice when the origin has
no extension mechanism, and it leaves the origin untouched; then Envoy Lua costs less than half the CPU
of WebAssembly, whose filter is also marked experimental. In both cases the operator must stop HDM from
injecting into compressed bodies, partial content and large pages. These are values from one testbed:
the order of the variants is what I claim, not that HDM costs exactly 32 microseconds elsewhere."

---

### Slide 18: Validity and limitations (0:35)

**On the slide**
- **Threats handled:** drift (pairing, 5 replicates, gates); separate runs (only paired differences,
  order only without overlap); timing lines (silent tier as basis)
- **Remaining threats:** single VM; supplementary runs with 3 replicates (Apache page size: 2);
  5 of 8 tier orders; remaining variant differences (8 OpenResty workers, Apache injects into 3 of 5
  HTML responses); confounded comparisons
- **Not studied:** effectiveness against real attackers; TLS and HTTP/2; dynamic applications;
  capacity (2 vs 3 vCPUs); cause of Apache's crashes

**Speaker notes**
"Briefly, the limits. The design protects against drift, but all values come from one VM. Comparisons
between variants come from separate runs, so I order variants only when ranges do not overlap. Some
differences are part of a variant's cost, for example Apache injects into three of the five HTML
responses per iteration because its own 404 pages bypass the filter, so its cost covers less injection
work. And I did not test real attackers, TLS, HTTP/2, dynamic applications or capacity. I did not
compare capacity because the proxy deployment uses three vCPUs and the in-server deployment two."

---

### Slide 19: Conclusion and future work (0:35)

**On the slide**
- **Contributions:** one deception module in four server/runtime combinations, driven by one
  configuration; a measurement method that separates HDM's cost from testbed drift; measurements of an
  extension doing **real deception work**
- **Take-aways:**
  1. HDM costs tens to a few hundred µs; OpenResty cheapest
  2. In the proxy deployment, the **proxy costs more latency than the deception**
  3. Most of the cost lies **around** the detection and injection code
  4. Rewriting breaks HTTP mechanisms in **server-dependent** ways
- **Future work:** respect gzip/range/HEAD/size; profile the untimed cost; same server in both
  deployments; fix Apache crashes; sqlmap traffic and a field study; TLS, HTTP/2, equal-vCPU capacity

**Speaker notes**
"To conclude: I built one deception module in four variants, a method that separates its cost from the
drift of a virtual machine, and measurements of an extension that does real deception work, unlike the
earlier studies of filters that do nothing. Four take-aways: the cost is small in absolute terms and
lowest in OpenResty; in a proxy deployment the proxy itself is the larger cost; most of the cost lies
around the code, so optimising the detection logic would gain little; and rewriting responses needs care
with HTTP mechanisms, in different ways on different servers. Thank you."

---

### Slide 20: Questions

**On the slide:** "Thank you. Questions?" plus the four take-aways in small text, and the synthesis
table from slide 17 as a small reference, so the examiners can point at it.

---

## Part 3: Backup slides (only shown if asked)

**B1. Envoy WebAssembly and the SQL-injection trap.** Visual: `figures/diagram_wasm_interface.png`.
Points: Envoy calls the module at request headers, request body, response headers, response body; the
module calls Envoy to read a header, read or replace a body, answer a request, write a log line; in
Envoy 1.30 a local reply is possible only at the request-headers step; the trap needs the body, so the
request is forwarded, the origin answers 404, and the module replaces the 404 with the trap's page.

**B2. CPU per load level and utilisation.** Visuals: `figures/cpu_added.png`, `figures/util_added.png`.
Utilisation table, in per cent of the 2 vCPUs, without and with HDM: at 10/s Envoy Lua 1.8 → 2.1,
Envoy WebAssembly 1.8 → 2.3, OpenResty 1.0 → 1.1, Apache 1.5 → 2.5; at 100/s 12.4 → 14.4, 12.7 → 17.6,
6.6 → 8.0, 8.0 → 20.0. With Envoy Lua, nginx's utilisation falls by 3.6 points at 100/s because the
SQL-injection trap answers the POSTs itself.

**B3. GET requests and the SQL-injection trap (100/s).**

| Variant | per GET (µs) | per SQL-injection trap request (µs) |
|---|---|---|
| Envoy Lua | +165 (+66 to +175) | −109 (−118 to +16), unresolved |
| Envoy WebAssembly | +162 (+128 to +387) | +50 (−213 to +82), unresolved |
| OpenResty | +77 (+66 to +101) | −28 (−48 to −3), resolved |
| Apache | +238 (+213 to +247) | +276 (+254 to +455) |

**B4. Page size.** Visual: `figures/page_size.png`. 768 KB: Envoy WebAssembly 889, Envoy Lua 1,372,
OpenResty 1,628, Apache 2,167 µs. OpenResty 16 KB costs more than 1 KB (199 to 252 vs 43 to 58 µs);
the measurements do not show why.

**B5. SQL-injection trap: decoding and scanning.** Visual: `figures/sqli_trap.png`. Plain hit: 7.4 /
2.7 / 6.9 / 16.0 µs; decoding 1.26 / 0.09 / 1.42 / 1.43 µs; scanning further 0.73 / 0.51 / 0.82 /
2.84 µs (Envoy Lua / Envoy WebAssembly / OpenResty / Apache).

**B6. Operations per honeytoken type.** Visual: `figures/hdm_timers.png`. Setting a decoy header or
cookie: 1.6 to 3.3 µs in Envoy, 0.35 to 1.5 µs in the web servers.

**B7. Remaining differences after levelling** (report Table 5.1): framing (OpenResty and Envoy
WebAssembly send injected pages in chunks; Envoy Lua and Apache send a matching Content-Length); Apache
at most one cookie honeytoken per path; Envoy WebAssembly trap requests reach the origin; error-page
bodies rendered by different servers; Apache injects nothing into its own error pages; OpenResty and
Apache send nothing to an origin; Apache no decoy header on the 204 test response.

**B8. Proxy vs in-server: pros and cons** (report Tables 8.2 and 8.3). Proxy: in front of any origin,
origin unchanged, one proxy can cover several applications; but +309 to 396 µs, an extra process,
breaks gzip/range/large pages in Envoy, and a filter interface may not allow answering after reading
the body. In-server: no extra server or network pass, OpenResty cheapest, gzip and 2 MB correct; but it
needs a web server with an extension module and a changed configuration, a fault in HDM hits page
delivery (Apache crashes), and some responses may be outside HDM's reach (Apache error pages).

---

## Part 4: What you must be able to explain (the examiners' likely questions)

The examiner's group works on the performance evaluation of distributed systems, so expect questions
on method and statistics. The second supervisor, Daniel Reti, is first author of *Honey Infiltrator*
(Netfilter-based honeytoken injection), which the thesis cites in Related Work, so expect a comparison
question. Answer with what you measured; when something was not measured, say so plainly. That is
better than guessing.

### Concept and design

1. **Why are honeytokens a low-false-alarm signal?** No legitimate workflow uses them. A browser returns
   cookies and hidden fields unchanged, and nobody visits a path advertised only in a hidden link. Han et
   al.: 7 months in production, no false alarm. (Caveat: crawlers that follow hidden links could trigger
   the decoy path; effectiveness and false alarms in the wild were not studied.)
2. **Why three signals, and which type uses which?** Alteration (cookie, form field) needs the planted
   value; resource request (decoy path) needs only the path; data use (comment, header) needs a search
   of incoming requests for the planted value.
3. **Why does HDM strip the comment keyword (R2)?** So that planted secrets never reach the application.
   In OpenResty and Apache nothing reads the cleaned query after the page is chosen, so there R2 holds
   trivially and is checked only through the alert lines.
4. **Why take the client address from the connection?** A header such as X-Forwarded-For can be set by
   the attacker.
5. **What does R3 protect against?** Log forging (line breaks replaced before an alert is written) and
   cross-site scripting through the trap page (reflected input HTML-escaped, truncated to 200 chars).
6. **Why literal substrings instead of regular expressions for the 22 signatures?** Regular expressions
   are not available in the same form in every variant. Literal search keeps the four variants
   identical (R5).
7. **Does HDM block attackers?** No. It detects and records; the recorded addresses do not change any
   response.
8. **Why is the fake endpoint believable?** A clean login gets 401 "sign in failed" like a real login;
   only SQL syntax gets the MySQL error, which, according to OWASP, testers use to refine payloads.
9. **Compared with Honey Infiltrator?** It injects honeytokens with a network bridge using Netfilter,
   an alternative to a proxy, which the thesis mentions in Related Work. HDM instead runs in the HTTP
   server's extension mechanism, where requests and responses are already parsed and where HDM can
   answer a request itself (the SQL-injection trap). The thesis did not measure Honey Infiltrator, so
   do not claim it is cheaper or more expensive. If asked about TLS: the thesis measured unencrypted
   HTTP/1.1 only; a server that terminates TLS sees the plaintext, but that was not measured.

### Implementation

10. **Why Envoy only as a proxy, and nginx/Apache only in-server?** Envoy serves no files: every route
    forwards, redirects or returns a fixed response. The web servers could also be proxies, but the
    thesis keeps one deployment per server family; this is also why deployment and server are confounded.
11. **Why does the OpenResty configuration map `/` directly to the index page?** nginx's `index` does an
    internal redirect, and the redirected request passes the access phase, and so HDM's detection, a
    second time, which would raise two alerts for one request.
12. **Why does Apache inject into only 3 of 5 HTML responses?** Apache removes resource-level output
    filters before rendering its own error pages (the two 404 probes). The decoy header and cookie are
    still sent, because they are prepared in the request phase.
13. **Why at most one cookie honeytoken in Apache?** Apache httpd's Lua module can only replace a
    Set-Cookie header, not add a second one.
14. **Why does Envoy WebAssembly read each header at most once and lazily?** Every read is a call
    across the module boundary into Envoy, which costs time.
15. **What is LuaJIT's warm-up issue?** LuaJIT first interprets and compiles a loop or function only
    once it is hot (by default after 56 iterations). The warm-up (40 % of the window, at least 8 s)
    makes the measurement run the compiled code.

### Method and statistics (prepare these best)

16. **Why silent minus bare, not silent minus origin?** Against the origin, the cost of the proxy itself
    (309 to 396 µs) would be charged to HDM; HDM would look 3 to 4 times as expensive.
17. **Why a silent tier at all?** Timing lines cost 21 to 144 µs per request, close to HDM's own cost
    in OpenResty and Envoy Lua; a deployed HDM would not write them.
18. **Why median of five paired differences, and why the range instead of a confidence interval?** The
    pairing cancels drift within a replicate. With five values, the range is a simple, assumption-free
    uncertainty; the comparison rule (no overlap) follows Hoefler and Belli. "Resolved" = all five
    differences have the same sign. If asked "is n = 5 enough?": five replicates agreeing in sign has
    probability 2 × (1/2)^5 ≈ 6 % under a symmetric null of no effect, so "resolved" is a strong but
    not absolute indication; and all 60 latency differences were resolved. (This calculation is not in
    the report. Present it as your own reasoning if you use it.)
19. **Why never compare raw latencies between variants?** Each variant was measured in its own run,
    and the testbed may have drifted between runs.
20. **Why mean of the four GET-probe medians?** Each probe takes a different path through HDM; averaging
    medians gives each probe equal weight. The value describes this mix, not one request type.
21. **Why exclude the POST probes from latency?** With and without HDM they get different responses
    (404 vs 500/401), and in Envoy Lua the request does not even reach nginx, so silent minus bare would
    not compare the same work. Their cost is measured with CPU time and timers.
22. **Why an open model?** In a closed model the slower configuration gets less load (Schroeder et al.),
    so two configurations would not be compared under the same load. Dropped iterations count as a
    failed run, not as lower load.
23. **Why a Williams design?** Requests leave state behind (Apache closes the connection after a 500).
    In a fixed order this cost would always hit the same probe; Williams spreads it evenly.
24. **Why the unrecorded first GET?** The first request on an idle connection pays a set-up cost.
25. **Why the marginal CPU fit?** Idle CPU divided by few requests inflates per-request CPU at 1/s.
    The fit is idle cost per second + cost per request; fits needing a negative term are rejected.
26. **Why timers summarised by mean?** They write whole microseconds while most operations take 1 to
    5 µs, so a median would only ever be a whole number.
27. **Why CPU pinning?** Otherwise k6's own scheduling delay ends up in the latency it records.
28. **Why non-blocking logs, and what is the risk?** Blocking delivery stalls a worker mid-request when
    the pipe is full, and that stall would be charged to HDM. The risk is lost lines under an extreme
    burst, which would show up as a missing sample, not as a wrong one. For a real deployment, the log
    path must be lossless.
29. **Why is a worker crash not a rejection gate?** The damage (lost requests) is measured directly by
    the failed-request and placement gates; rejecting crashing cells would discard exactly the
    measurements in which the crashes are the finding. Same rules for all cells.
30. **Why not compare capacity?** The proxy deployment uses 2 + 1 vCPUs and the in-server deployment
    2; it would compare 2 vCPUs with 3. All servers stayed ≤ 20 % busy, far from saturation.
31. **Why does OpenResty run 8 workers on 2 vCPUs?** `worker_processes auto` uses the processor count
    that the C library reports, and the OpenResty image's C library ignores the pinning. It is the same
    with and without HDM, so it cancels in HDM's cost, but it is a difference between variants.
32. **Why only 5 of 8 tier orders?** Five replicates cover the first five rows of the balanced 8-row
    cycle. No tier is fixed to one position, but the balance is partial (listed as a threat).

### Results: interpretation

33. **Why do latency and CPU rank Envoy WebAssembly vs Envoy Lua differently?** CPU time counts work
    only, while latency also contains waiting. The report does not decompose why WebAssembly's extra
    CPU does not show up as extra latency; say that it was not measured.
34. **Where does the untimed 65 to 95 % of the cost go?** Candidates: entering and leaving the runtime,
    collecting the response body, writing alert lines. Supporting evidence: WebAssembly, which crosses
    the module boundary for every header and body piece, has one of the largest untimed shares; log
    lines are not free (timing lines cost 21 to 144 µs/request). How it divides among the candidates was
    **not measured** (future work: profiling).
35. **Why can HDM's CPU cost on a SQL-injection trap request be negative?** Without HDM, the server
    handles the POST as usual (Envoy forwards to nginx; OpenResty looks for a file and renders a 404).
    With HDM, the trap answers in the request phase and skips that work. If the saved work exceeds the
    added work, the difference is negative: resolved in OpenResty (−28 µs).
36. **Why does Envoy break gzip but the web servers do not?** In the proxy deployment the origin
    compresses the page, and Envoy injects into the compressed bytes (HDM does not check the content
    coding). In OpenResty and Apache the compressed page arrived decodable and with honeytokens, which
    shows the injection happened before compression in those servers. Describe what was observed;
    the report does not go into the servers' filter order.
37. **Why is the 2 MB page broken in Envoy?** Both Envoy filters wait for the whole body, and the body
    exceeds Envoy's default 1 MB buffer limit. Fix: exclude large pages or raise the limit.
38. **What is the ETag problem?** The injected page keeps the original entity tag (Envoy, OpenResty), so
    a conditional request from a cache holding the page without honeytokens gets 304, and that client
    never sees the honeytokens.
39. **Why did Apache crash?** Recorded, not resolved. The workers crashed only with HDM (10 to 12 times
    per silent run at 100/s), never without, and no other variant crashed. Apache keeps one Lua state per
    thread; whether another lifetime of the Lua state avoids the crashes was not measured. Do not offer
    a cause as fact.
40. **Why does relative latency cost grow with load although absolute cost falls?** The servers without
    HDM answer faster at 100 iterations/s, so the same or a slightly lower absolute cost is a larger share.
41. **What exactly does RQ4.3 show, and what not?** Envoy Lua and OpenResty share LuaJIT and have almost
    equal operation times, but OpenResty adds less latency and CPU and breaks fewer responses. Because
    server software also differs, the difference cannot be attributed to the deployment alone. The fix is
    the same web server in both deployments (future work).
42. **Can the absolute numbers be transferred?** No. One VM, static pages of 864 to 1,396 bytes, one
    honeytoken configuration. The claim is the comparison between the variants.

### Positioning

43. **What is new compared with Zhu et al.?** They measured Envoy filters that do nothing (Lua about 3×
    the latency of WebAssembly). This thesis measures an extension doing deception work, across three
    servers, three runtimes and two deployments, and checks correctness as well.
44. **What would you change if you started again?** (Honest options that follow from the limitations:)
    measure one web server in both deployments to remove the confound; record image identifiers for
    every run; give the supplementary runs five replicates; run all eight tier orders.
45. **How did the research questions arise?** The proposal asked how server extension modules can
    implement deception and inject without unacceptable latency or breaking the application (→ design,
    RQ1, RQ3). CPU time (RQ2) was added because latency includes waiting; runtime (RQ4.1/4.2) once three
    runtimes existed; the origin comparison (RQ1.2) to see the proxy's own cost; the deployment (RQ4.3)
    last.

---

## Part 5: Numbers to know by heart

| Quantity | Envoy Lua | Envoy WebAssembly | OpenResty | Apache |
|---|---|---|---|---|
| Median latency added (µs, 3 loads) | 147 to 203 | 110 to 150 | 69 to 81 | 183 to 244 |
| … relative to without HDM | 18 to 22 % | 12 to 18 % | 13 to 20 % | 32 to 48 % |
| Marginal CPU per request (µs) | 45 (39 to 50) | 108 (99 to 121) | 32 (25 to 36) | 261 (229 to 334) |
| Utilisation added at 100/s (pp) | 2.0 | 4.9 | 1.4 | 11.7 |
| Share of added CPU inside timed code | 17 to 22 % | 5 to 7 % | 19 to 35 % | 5 to 7 % |
| Broken responses (of 13) | 4 | 3 | 1 | 0 |
| Intact under load | all | all | all | ≥ 99.97 % |

- Reverse proxy alone (bare minus origin): **309 to 396 µs**
- HDM's operations: **0.35 to 5.3 µs** each
- Main benchmark: **240/240 cells valid**; 5 replicates × 4 tiers × 3 rates × 4 variants
- Rates: **1 / 10 / 100 iterations/s = 9 / 90 / 900 requests/s**; windows 90 / 45 / 30 s
- Per iteration: **9 requests, 8 probes, 7 alert lines, 2 SQL-injection trap lines**
- Parity check: **13 requests, 11 alert lines + 1 SQL-injection trap line**
- Signatures: **22**; trap responses **500** (hit) / **401** (no hit); reflection ≤ **200** chars
- Envoy buffer: **1 MB**; test pages 768 KB and 2 MB; benchmark pages **864 to 1,396 bytes**
- Testbed: **8 vCPUs, 7.8 GB**, i7-13650HX; Envoy 1.30.11, OpenResty 1.27.1, Apache httpd 2.4.67,
  PUC Lua 5.1.5, nginx 1.29, k6 2.0

---

## Part 6: Delivery tips

- Rehearse with a timer twice; at 18:30 you have room for one slow slide. If you run late, shorten
  slides 7 and 18 first; never cut slides 10 and 11 (the method).
- On each results slide, say what the axes and markers mean before you state the finding.
- Say "not ordered" when ranges overlap and "not measured" when a cause is unknown. Examiners notice
  over-claiming more than they notice a missing explanation.
- Keep the report PDF open on a second screen to answer page-specific questions; backup slides B1 to B8
  cover the most likely detail questions.
