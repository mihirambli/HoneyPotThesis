# Thesis Notes

Explanations and reasoning captured during development for later use when writing the thesis. Entries are dated and kept in the order they were recorded.

---

## 2026-07-08 — Why a warm-up phase is needed in the benchmarks

**Cold start**, in this project, means the extra latency the very first requests hit before the runtime's internal caches and optimizations have kicked in. Several independent things are cold at container startup:

**1. JIT compilation (the dominant effect here)**
- OpenResty and Envoy+Lua both run on **LuaJIT**. LuaJIT doesn't compile Lua to machine code immediately — it starts by *interpreting* bytecode, and only after a loop/function executes enough times does its trace compiler kick in and emit native machine code for that hot path. The first N calls to `access_by_lua_block` / `envoy_on_request` run in the slow interpreted path; later calls run compiled and are dramatically faster.
- Envoy's WASM VM (used for the Rust filter) does something analogous — WASM runtimes typically use a fast baseline compiler for the first executions, then re-optimize hot functions with a higher-tier compiler.
- Apache's mod_lua is less JIT-dependent (often plain Lua 5.x, not LuaJIT), so it's less affected by this specific mechanism.

**2. Everything else that "warms up" under sustained traffic**
- TCP connection pool / keep-alive reuse between k6 and the edge — early requests pay full connection setup cost, later ones reuse open connections.
- OS/page cache for the proxy binaries and shared libraries.
- Memory allocator behavior (heap/arena growth stabilizes after initial allocations).
- Envoy's cluster connection pool to the upstream `backend` ramping up.

**Why it distorts the benchmark:** the VU=1 run is always the *first* traffic the edge has ever seen, so it pays 100% cold-start cost. The VU=500 run happens last, after thousands of prior requests, so it is fully warm. That means what looks like "latency improves under higher concurrency" is partly a real scaling effect and partly just "this run happened later." This was observed directly: Envoy+Lua's detection median dropped from 234µs (VU=1) to 47µs (VU=500) — a large chunk of that gap was warm-up, not concurrency.

**What the warm-up phase does:** before any *recorded* VU level runs, the benchmark orchestrators fire one throwaway burst of real traffic (VU=100 for 20s) at the edge and discard the results. That traffic exercises the exact same code paths (LuaJIT tracing, WASM tiering, connection reuse) so the JIT has already compiled the hot paths and the pools are already established by the time recording starts at VU=1. All four recorded levels then start from the same warm baseline, so differences between VU=1 and VU=500 reflect actual concurrency/scaling behavior rather than "which level happened to run first."
