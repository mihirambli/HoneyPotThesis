<!-- BENCHMARK_METHODOLOGY.md: why the measurement is built the way it is, what it can and cannot
     resolve, and how to configure the machine it runs on. Read before trusting any number in
     benchmarks/results/. -->
# Benchmark methodology

This document exists because an earlier version of the suite produced results that could not be
right: "WADM overhead" bars that were negative for most of the SQLi arms and for whole edges, and a
mean per-operation cost that rose a hundredfold with load while the median did not move. None of
that was a WADM property. Each item below names the artefact, the evidence for it, and the change
that removes it.

The short version: **the original harness resolved somewhere between 300 µs and 400 ms depending on
load, against an effect it took to be 5–30 µs per request.** Every negative overhead bar was that
gap, not a speed-up. (5–30 µs is what the in-edge timers enclose. The paired harness has since
measured the end-to-end effect directly: **+160–360 µs per GET at the median**, resolved in every
GET cell — see §6.)

---

## 1. What was wrong

### 1.1 The per-phase figure measured request *position*, not WADM phase

`test.js` issued eight requests per iteration in a fixed order and then called `sleep(1)`. Under a
`constant-vus` executor every VU therefore woke on the same instant, so request #1 of each iteration
queued behind every peer while request #8 found a drained queue.

The control proves it. The **origin tier runs no proxy and no WADM code at all** — k6 straight to
static nginx — and its eight slots still came out monotonically decreasing:

| VUs | slot 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | spread |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 0.97 | 0.68 | 0.62 | 0.55 | 0.59 | 0.57 | 0.51 | 0.60 | **1.6×** |
| 500 | 3.63 | 2.94 | 2.44 | 1.73 | 1.28 | 1.08 | 0.90 | 0.74 | **4.9×** |

11 of 36 recorded runs were perfectly monotone across all eight slots; under random ordering that
is a 1-in-40,320 event per run. `inject_get_duration` was always slot 1, so the figure's
"injection only, no detection hit" bar carried 0.4–2.9 ms of pure arrival artefact against a real
injection cost of roughly 2 µs.

**Fixed by:** a `constant-arrival-rate` executor (no `sleep`, so no synchronised wake-up), a
one-slot-per-iteration **rotation** of the probe order so every probe occupies every position
equally often, an unrecorded priming request that absorbs the idle-connection cost the first
measured request used to pay, and eight `slot_N_duration` Trends that make the residual visible
instead of assumed.

Measured effect on OpenResty at 20 iterations/s: slot spread 1.79× → **1.33×**, and the four GET
probes — which previously spread 1.66× — now agree to within 1.13×, which is about what their
differing response sizes alone should produce.

### 1.2 The reported statistic was the mean, and a sub-1% tail owned it

`internal_apache_lua_raw.json`, `html_comments` detection:

| offered rate | p50 | p90 | p99 | max | **mean** | samples > 1 ms |
|---|---|---|---|---|---|---|
| 1 | 5 | 9 | 18 | 18 | 5.6 | 0% |
| 10 | 4 | 10 | 98 | 1,002 | 14.6 | 0.33% |
| 100 | 5 | 12 | 345 | 35,242 | 50.7 | 0.32% |
| 500 | 5 | 13 | 10,112 | **196,082** | **552.2** | 2.06% |

The median is **5, 4, 5, 5 µs** across a 500× load range, and p90 moves only 9 → 13 µs. The
measurement was already good. A 196 **millisecond** substring scan is not CPU work: all four edges
time their regions with a wall clock (`gettimeofday`, `apr_time_now`, proxy-wasm
`get_current_time`), so a worker
descheduled inside a region has that time billed to WADM.

**Fixed by:** `summarize()` now reports `p50_us`, `p90_us`, `p99_us`, a 5%-trimmed mean and the
median absolute deviation alongside the retained `avg_us`, plus `tail_over_threshold` and
`tail_pct` — the count and share of samples above 1 ms. A run whose tail share exceeds 1% is
**rejected**, because at that point the mean is reporting on host scheduling.

### 1.3 The two tiers were measured 15–30 minutes apart, unpaired

`e2e_openresty_wadm.json` was written at 08:57 and `e2e_openresty_bare.json` at 09:15. The figures
subtracted one file's median from the other's. Everything that drifts across twenty minutes — CPU
turbo and thermal state, host background work, page cache, which physical core a vCPU landed on —
went straight into the difference, and with a 5–30 µs effect against 100–500 µs of drift the drift
set the sign.

**Fixed by:** `run_paired_benchmark.py` runs both tiers for one rate inside one process, minutes
apart, with the origin container untouched between them; repeats the pair `--replicates` times;
swaps which tier goes first on alternate replicates; and reports the **median of the per-replicate
differences with a percentile bootstrap interval**. A difference whose interval spans zero is
labelled *below noise floor* rather than drawn as a speed-up.

### 1.4 The two tiers were offered *different* load

`constant-vus` plus `sleep(1)` is closed-loop: when latency rises the achieved rate falls. At 500
VUs OpenResty ran at 0.554× the ceiling bare and 0.665× with WADM — so the slower tier was also
tested under **lighter** traffic. A difference between two different offered loads is not an
overhead.

**Fixed by:** `constant-arrival-rate` holds the offered rate fixed as an input. A shortfall appears
as `dropped_iterations`, which is a gate rather than a silent change of experiment.

### 1.5 `K6_VUS` and `K6_DURATION` silently voided the scenario block

k6 owns the `K6_*` prefix for its own CLI options. Exporting `K6_VUS`/`K6_DURATION` makes k6
synthesise its own default scenario and **discard `options.scenarios` entirely**. The old harness
exported both, so every recorded run was a plain constant-VUs run with no start delay, whatever
`test.js` declared — including the 5 s `startTime` that was supposed to keep Envoy and WASM
start-up errors out of `min`/`avg`.

**Fixed by:** the load shape moved to a `WADM_*` namespace, and `strip_k6_option_env()` scrubs any
inherited `K6_*` option so a stale `export` in the operator's shell cannot quietly revert the
executor.

### 1.6 Broken runs stayed in the figures

`e2e_wasm_wadm.json` at 100 VUs completed **15.6%** of its offered iterations with a 75th-percentile
latency of 1,777 ms, and contributed a "+110 ms WADM overhead" bar. `e2e_apache_lua_bare.json` at
500 VUs ran at 23.4% with 2.7% of requests failing. The old `throughput_check` printed a warning
and wrote the data anyway.

**Fixed by:** `assess_run()` gates every run on achieved rate, dropped iterations, HTTP failure
rate, edge crash indicators scraped from the logs, and preemption-tail share. A failing run is kept
in the result file **with its reasons** and excluded from every statistic.

Crash scanning matters independently: Apache children have been observed to segfault and corrupt
their Lua state at high rates, after which that child's timings look entirely plausible in
isolation.

### 1.6b Gates must scale with the damage, not fire on its presence

Two gate rules were tightened after the first full run showed them rejecting sound data. Both are
recorded here because they change which runs enter a figure, and because the reasoning generalises.

**Crashes warn; damage rejects.** The first rule was "any crash indicator in the edge's logs
invalidates the run". Apache's mod_lua segfaults under WADM, so that rule discarded whole rate
levels — including, in the capacity ladder, a step that had placed 100% of its offered load at an
11 ms p95. It also discarded precisely the runs where *"this edge crashes under load"* is the
result. Inspection showed the damage is proportional and small: nine crashes killed about eight of
24,000 requests, 0.03%, and the surviving medians were identical to the crash-free levels
(4 µs detect at every rate). A crash is now a recorded warning; whether the DATA is usable is left
to the failure-rate and achieved-rate gates, which measure the damage directly. The same rule now
applies in `run_capacity_benchmark.py`, which had kept the older, stricter version and so produced
no WADM capacity figure for Apache at all.

**A percentage needs a denominator.** The preemption-tail gate rejected a run when more than 1% of
internal samples exceeded 1 ms. At the lowest rate a kind collects about 90 samples, so ONE
preempted sample is 1.1% and trips it alone. That rejected two otherwise pristine replicates whose
medians were 3–4 µs with a 0.0% tail on every major kind. The gate now also requires at least three
such samples before it fires. Re-assessing the existing data under the corrected rules took the
first full run from 118/120 to **120/120** valid cells, with no re-measurement.

`analyze_paired.py --reassess` exists for exactly this: every input the gate reads is persisted per
cell, so a rule change is applied to stored data rather than costing hours of re-running.

### 1.6c A negative control catches what the other gates cannot see

The `apache_lua_conn` run exposed a gap that had been open the whole time. One cell — bare tier,
rate 1, replicate 5 — recorded a median latency of 364 ms where its four siblings recorded 1.0 ms,
and an edge CPU cost of 42,395 µs/request where they recorded ~350 µs. Every existing gate passed
it. At rate 1 the constant-arrival-rate executor absorbs slow responses by spending more VUs
(20 instead of 1), so it still placed 100% of the offered iterations, dropped nothing and failed
nothing. The preemption-tail gate reads internal WADM timings, and a *bare* cell has none at all,
so on that tier the gate is structurally blind.

The contamination reached the figures as a paired difference of **−1344 ms**, which dragged the
bootstrap lower bound so far negative that matplotlib's autoscale rendered every real bar in the
latency panel as invisible against a 0–80 ms axis.

The fix is a negative control that was already being recorded but not used. The **backend container
is identical in every cell, of every tier, of every edge** — same origin, same pages, nothing under
test — so its CPU per request should barely move. Across the first 150 paired cells it stayed
between 86 and 311 µs/request. The contaminated cell read **39,948 µs/request**, 130× the worst
legitimate value, and the edge in the same cell was elevated by a similar factor. Two unrelated
containers burning kernel time together is the host being taken away, not edge behaviour. The
runner's own timeline agrees: the gap before that cell was 4 m 14 s and the gap after it 5 m 43 s,
against ~2 m 20 s everywhere else.

`MAX_ORIGIN_CPU_US_PER_REQUEST = 1000` sits about 3× above the worst clean observation. It is
edge-agnostic and tier-agnostic by construction, which is what makes it a control rather than a
post-hoc outlier rule: it does not reference the quantity being measured, so it cannot be tuned to
produce a preferred answer. Re-assessing all 150 stored cells under it rejected **exactly one**, the
cell above; the four-edge dataset was unchanged at 120/120. The same gate now runs in
`run_capacity_benchmark.py`, which also records the origin control per step.

`plot_remediation.py` additionally frames its y-axis from the bars and their upper uncertainty
rather than from autoscale, and prints a line whenever a confidence bound falls below the axis
floor and is therefore clipped. The gate is the fix; the axis rule is so that the next such cell
cannot quietly flatten a panel before anyone notices.

### 1.7 Log writes were in the measured path

Each measured request emits six to twelve `WADM TOKEN … (us): N` lines. Apache serialises its error
log across all children. Under blocking log delivery a full pipe stalls the worker mid-request, and
that stall lands inside the *next* timed region where a wall clock charges it to WADM.

**Fixed by:** `mode: non-blocking` with a 64 MB buffer on every measured container. The trade is
that a line may be dropped under extreme burst, which shows up honestly as a lower sample count
rather than as a corrupted sample.

### 1.8 The load generator competed with the edge it was measuring

k6, the edge and the origin all shared the same eight vCPUs, and at high rates k6 is the hungriest
process present. Its own scheduling delay was inside every latency it reported.

**Fixed by:** disjoint `cpuset` per service — origin on CPU 3, edge on 1–2, load generator on 4–7,
CPU 0 left for the system. Override with `EDGE_CPUS` / `BACKEND_CPUS` / `LOADGEN_CPUS`; set all
three to `0-7` to restore the old unpinned behaviour.

### 1.9 The SQLi "overhead" was never an overhead

This one is not noise. The trap answers at the edge and never contacts the origin; the bare tier has
no trap, so it proxies to the backend and 404s. WADM genuinely **removes a round-trip** worth
0.3–0.5 ms, which dwarfs the ~13 µs of scanning it adds. Subtracting the two and calling the result
"overhead" compares two different code paths.

**Handled by:** reporting the trap's end-to-end effect as what it is — a saved round-trip minus a
scan cost — and taking the scan cost itself from the internal microsecond timers, which measure only
the scan. The sign is the finding, not a defect. A like-for-like end-to-end comparison would need
the bare configs to answer `POST /api/login` locally too; that is deliberately **not** done here,
because it would mean the bare tier no longer represents "this edge without WADM".

### 1.10 The stack got warmer as the ladder climbed

One 100-VU warm-up preceded a ladder that then ran 1 → 10 → 100 → 500 on a stack that was never
restarted. The lowest level ran on the coldest edge and the highest on the warmest, which is an
ordering bias inside a single file.

**Fixed by:** every cell gets its own stack start and its own warm-up **at its own rate**, so the
code paths that are hot are the ones about to be measured, and no level is advantaged over another.

---

## 2. What the suite now measures

Three planes, deliberately, because no single one survives a noisy VM:

| Plane | Unit | Source | Robust to | Use for |
|---|---|---|---|---|
| Internal timers | µs | edge logs | nothing — wall clock; read at the 5%-trimmed mean (§6.4), gate on tail share | per-operation mechanism cost |
| End-to-end latency | ms | k6 summary | pairing + replication; reported as the range of paired replicates | what a client experiences |
| CPU per request | µs CPU | cgroup `cpu.stat` | queueing and scheduling delay — **not** hypervisor preemption on this guest (§6.3) | **the cost figure that survives load** |

CPU per request is the addition worth the most. `cpu.stat`'s `usage_usec` is a monotonic counter of
CPU time the container's processes actually consumed. Queueing adds wall-clock latency without
adding CPU time, so `Δusage_usec / requests_served` isolates the work WADM does from most of the
noise the environment adds — and a bare-vs-WADM difference there stays measurable at rates where
the latency difference is buried. Two qualifications, both in §6: VirtualBox does not account steal
time, so host preemption of a vCPU IS billed as CPU; and the figure is an average over a mixed
workload that includes the cost of writing the timing logs.

### The instrumented blind spot

Because all three planes are now measured on the same paired runs, they can be compared, and they
disagree by an order of magnitude. On OpenResty at 10 iterations/s: the detect/inject timers
attribute ~5-10 µs per request, the cgroup counter attributes ~55 µs of CPU, and the paired
end-to-end delta is ~100-190 µs.

The timers are not wrong — they measure exactly the regions they wrap. But those regions are
roughly **5-10% of what WADM costs**. The remainder is Lua VM entry and exit per phase hook, the
response body filter walking the body, the extra header table writes, and alert rendering and log
writes, none of which any timer encloses. The end-to-end figure additionally includes time spent
waiting rather than computing.

`plot_cost_attribution.py` draws this per edge. It is likely the most interesting thing the
rebuilt harness produces, and it was not measurable before: it needs the CPU plane, which did not
exist, and a bare-vs-WADM difference that is not swamped by drift, which pairing provides.

Saturation is a **separate experiment**. `run_capacity_benchmark.py` climbs a rate ladder and
reports the highest rate each edge sustains (≥95% of offered iterations placed, ≤1% dropped, ≤0.5%
failed, p95 under the SLO), the served request rate there, which limit bound first, and CPU per
request at each step. That is what the old 500-VU level was really asking, and latency at
saturation cannot answer it.

---

## 3. Running it

### What each command does, and how long it takes

Run them **one at a time, in this order**, and never two at once — every one drives the same
Docker stack and competes for the same pinned cores. The suite's whole premise is a quiet host;
two concurrent runs measure each other.

| # | Command | Time | What it does | Needed? |
|---|---|---|---|---|
| 1 | `sudo ./benchmarks/tune_guest.sh apply` | seconds | Disables THP and swap, widens the accept queue and ephemeral port range, stops the apt/man-db timers. Fully reversible. | Once per session, before 2–5 |
| 2 | `run_paired_benchmark.py --preset smoke --all` | **~20 min** | The whole pipeline at 1 replicate and short windows. Proves the harness works before you commit hours to it. Its numbers are **not reportable** — one replicate cannot produce an interval. | Once, the first time |
| 3 | `analyze_paired.py` | seconds | Prints deltas, bootstrap intervals, validity verdicts, the slot self-check and the internal timings. Reads `paired_*.json`, writes `paired_summary.json`. Read-only w.r.t. the plotters' inputs, so safe to re-run any time. | After 2, and after 4 |
| 4 | `run_paired_benchmark.py --preset full --all` | **~2.5–3 h** | The reportable dataset: 5 replicates × 3 rates × 2 tiers × 4 edges, paired, order-alternated and gated. | Yes — this is the dataset |
| 5 | `run_capacity_benchmark.py --all` | **~30–45 min** | The saturation ladder: max sustained throughput per edge, which limit binds first, and CPU per request at each step. Answers what the old 500-VU level was really asking. | Yes, for any capacity claim |
| 6 | `analyze_paired.py --write-legacy` | seconds | Optional. Regenerates `internal_*` / `e2e_*` in the schema the retired legacy plotters read. Copies whatever it replaces to `results/archive/<date>-write-legacy/` first. | Only for a legacy plotter |
| 7 | `plot_paired_overhead.py`, `plot_cost_attribution.py`, `plot_remediation.py` | ~1 min total | Draws the figures and the headline table from `paired_*.json` (§6.6). | Yes |
| 8 | `sudo ./benchmarks/tune_guest.sh revert` | seconds | Puts the guest back. | When you are done |

Steps 2–3 are a rehearsal. If they look sane, 4–8 are the real thing. Steps 4 and 5 are the only
long ones and can be left unattended — but nothing else should run on the VM *or on the host*
while they do.

**If a run is interrupted:** `paired_<edge>.json` is written once, at the end of that edge, so an
interrupted edge leaves no file and the completed ones are untouched. Re-run just that one with
`--edge <name>`.

**Splitting step 4 across sessions** is fine — run `--edge openresty`, later `--edge wasm`, and so
on. Each edge writes its own file and `analyze_paired.py` reads whatever is present. Pairing is
*within* an edge, so splitting across days costs nothing.


```bash
# Once per session, before measuring (guest side).
sudo ./benchmarks/tune_guest.sh apply

# Prove the harness works end to end. ~20 minutes, not reportable — one replicate
# cannot produce an interval.
python3 benchmarks/run_paired_benchmark.py --preset smoke --all
python3 benchmarks/analyze_paired.py

# The reportable run. ~2.5-3 hours for four edges.
python3 benchmarks/run_paired_benchmark.py --preset full --all
python3 benchmarks/analyze_paired.py

# Capacity and cost per request under load. ~30 minutes.
python3 benchmarks/run_capacity_benchmark.py --all

# Draw the figures and the headline table. All three read paired_<edge>.json directly.
python3 benchmarks/plot_paired_overhead.py    # latency added, shift function, timers, table
python3 benchmarks/plot_cost_attribution.py   # what the timers miss (see section 2)
python3 benchmarks/plot_remediation.py        # Apache LuaScope thread vs conn (section 4c)

sudo ./benchmarks/tune_guest.sh revert
```

`run_paired_benchmark.py` writes `paired_<edge>.json` as the record of truth — every cell, both
tiers, all replicates, each with its validity verdict. `analyze_paired.py` derives
`paired_summary.json` (the deltas with intervals), and with `--write-legacy` also rewrites the
`internal_*` / `e2e_*` files in the legacy schema. The plotters that read that schema
(`plot_baseline_comparison.py`, `plot_edge_comparison.py`, `plot_token_comparison.py`,
`plot_sqli_comparison.py`) are retired (§6.6); the flag is kept only for anyone who still needs
them. Whatever it replaces is copied to `results/archive/<date>-write-legacy/` first.

### Reading the output honestly

- Intervals are printed as `range a..b, k/n agree in sign`. With five replicates a percentile
  bootstrap of the median returns exactly the min and max of the five paired differences, which is
  the distribution-free 93.75% interval for a median; describe it as that range, not as a "95%
  bootstrap CI". `significant` at n=5 is the same statement as "5/5 agree in sign" (sign test,
  p = 0.0625).
- `below noise floor` means the range spans zero. The measurement cannot tell the direction of the
  effect, let alone its size. Report it as such; do not report the point estimate.
- Internal timings: read `trimmed_mean_us` (§6.4). `p50_us` can only take whole microseconds;
  `avg_us` is retained only for continuity with older files.
- `tail_pct` is the share of samples above 1 ms. Above ~1% the run was measuring host scheduling.
- `slot spread` is a harness self-check. Around 1.3× is expected and is common-mode under rotation.
  Above 2×, or with `rotation_active: false`, the per-probe figures are confounded.
- A rejected cell is evidence, not a gap. Its `reasons` say what went wrong.

### Reproducing the original artefact

`WADM_ORDER=fixed` restores the always-same-order behaviour. Running one rate both ways quantifies
the positional bias directly, which is a better defence of the fix than asserting it.

---

## 4. Machine configuration

### 4.1 The constraint that matters most

This guest is **VirtualBox** (`innotek GmbH`, 8 vCPUs, 7.8 GiB) on an **Intel i7-13650HX** — 6
performance cores (12 threads) plus 8 efficiency cores. E-cores run the same work roughly 40%
slower.

VirtualBox gives the guest no control over which physical core a vCPU runs on, and exposes all
eight vCPUs as identical single-threaded cores. A vCPU that migrates from a P-core to an E-core
mid-measurement produces a step change in per-operation cost that no guest-side setting can
suppress, and nothing in the guest can even observe it. **This must be fixed from the host.**

### 4.2 Host side — pin the VM to P-cores, with the VM powered off

First identify which logical CPUs are P-cores. On a Linux host, E-cores report a lower
`cpu_capacity`:

```bash
lscpu -e=CPU,CORE,MAXMHZ
cat /sys/devices/system/cpu/cpu*/cpu_capacity   # E-cores show the smaller number
```

On a 13650HX the P-core threads are normally CPUs 0–11 and the E-cores 12–19. Then:

```bash
# Linux host — pin the running VM process to P-core threads only.
taskset -acp 0-11 "$(pgrep -f 'VirtualBoxVM.*<vm-name>')"

# Disable turbo so a long run does not drift as the package heats up.
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
```

```powershell
# Windows host — 0xFFF is the low 12 logical CPUs.
$vm = Get-Process VirtualBoxVM | Where-Object { $_.MainWindowTitle -like '*<vm-name>*' }
$vm.ProcessorAffinity = 0xFFF
# Then set the power plan's maximum processor state to 99% to disable turbo.
```

Pinning must be reapplied whenever the VM restarts, so it belongs in whatever script starts it.

### 4.3 Host side — VM settings

```bash
VM="<vm-name>"
VBoxManage controlvm "$VM" poweroff 2>/dev/null

# 8 vCPUs is right for the cpuset layout (system 0, edge 1-2, origin 3, load generator 4-7) and is
# well within 12 P-core threads. Keep it at 8; raising it only adds vCPUs competing for the same
# physical cores.
VBoxManage modifyvm "$VM" --cpus 8

# 12 GiB if the host has 32 GiB or more. The containers need under 1 GiB between them, so this is
# not about capacity: it is so swap can stay off with room to spare, since a page faulted back in
# mid-request adds tens of milliseconds to whichever request pays for it.
VBoxManage modifyvm "$VM" --memory 12288

# Paravirtualised timekeeping and interrupt handling. kvm-clock is already in use; this makes it
# explicit rather than autodetected.
VBoxManage modifyvm "$VM" --paravirtprovider kvm

# Hardware nested paging and large pages: fewer EPT walks, so less variance per memory access.
VBoxManage modifyvm "$VM" --nestedpaging on --largepages on --vtxvpid on --vtxux on

# virtio-net instead of the emulated Intel NIC. Every measured request crosses this interface, and
# the emulated device costs microseconds per packet with jitter to match.
VBoxManage modifyvm "$VM" --nictype1 virtio

# Devices that generate host interrupts during a run and are not used by the benchmark.
VBoxManage modifyvm "$VM" --audio-driver none --usb off --usbehci off --usbxhci off
VBoxManage modifyvm "$VM" --clipboard-mode disabled --draganddrop disabled
VBoxManage modifyvm "$VM" --accelerate3d off
```

Then leave the host otherwise idle: no browser, no sync client, no antivirus scan, no host-side
indexer. The pairing design tolerates slow drift; it cannot tolerate a scan starting halfway
through one member of a pair.

### 4.4 Guest side

`benchmarks/tune_guest.sh apply` handles this and `revert` undoes it. It disables transparent huge
pages (currently `[always]`, and a direct contributor to the millisecond-range samples), turns off
swap, raises the accept queue and ephemeral port range so the kernel is not the bottleneck at the
top of the capacity ladder, and stops the periodic apt/man-db timers that would otherwise wake up
mid-run.

### 4.4b Recorded, not fixed: worker counts vs. the pinned cpuset

Pinning the edge to two CPUs introduced a mismatch that did not exist when everything shared all
eight, and the mismatch is **not the same on every edge**. Measured directly:

| Container | cpuset | `nproc` (affinity) | `_SC_NPROCESSORS_ONLN` | workers | oversubscribed |
|---|---|---|---|---|---|
| openresty (glibc) | 1-2 | 2 | **8** | **8** | yes, 4x |
| backend (musl/alpine) | 3 | 1 | 1 | 1 | no |

The cause is a libc difference, not a configuration one. nginx's `worker_processes auto` calls
`sysconf(_SC_NPROCESSORS_ONLN)`. glibc answers from `/sys` and **ignores the cpuset**; musl answers
from `sched_getaffinity` and respects it. So the glibc-based images (openresty, the Envoy pair)
oversubscribe their pinned cores while the musl-based ones (`nginx:alpine`, `httpd:2.4-alpine`) do
not. `nproc` is not a proxy for this — it always uses the affinity mask and so reports the
non-oversubscribed number even when the server has spawned eight workers.

This does **not** invalidate a paired bare-vs-WADM difference: both tiers run the same image and
the same config, so it is common to both halves and cancels. It does affect **cross-edge**
comparison and the capacity ladder, where the edges are being compared against each other.

Left as-is by choice, and **recorded** instead: `run_paired_benchmark.py` writes
`metadata.provenance` into every result file with `cpuset`, `affinity_cpus`, `online_cpus`,
`worker_processes` and an `oversubscribed` flag per container, so any figure can be traced to the
configuration that produced it and the write-up can state it plainly.

To change it later, two coherent options:

1. **Pin the worker counts explicitly** — `worker_processes 2` in both nginx configs, Envoy
   `--concurrency 2`, Apache `ServerLimit`/`ThreadsPerChild` to match. Every edge then gets the
   same concurrency and the cross-edge comparison is like-for-like. This is the better experiment,
   but `worker_processes` is a levelling parameter governed by `docs/EDGE_LEVELING.md`, so
   changing it is a decision about what is being compared.
2. **Drop the pinning** — `EDGE_CPUS=0-7 BACKEND_CPUS=0-7 LOADGEN_CPUS=0-7` restores the previous
   behaviour, at the cost of letting k6 compete with the edge it is measuring again.

### 4.5 If this is not enough

VirtualBox is the weakest part of the setup for microsecond-scale timing: no vCPU pinning, and a
timer implementation that is not built for it. If per-operation results still show step changes
after the above, KVM/QEMU on the same host supports real vCPU pinning
(`virsh vcpupin`), CPU-model passthrough, and `isolcpus` on the guest side, which together remove
the entire P-core/E-core migration problem rather than working around it. Migrating the disk image
is straightforward (`VBoxManage clonemedium --format qcow2`); the benchmark suite needs no changes,
since everything it depends on is inside Docker.

---

## 4b. Apache+mod_lua segfaults: what was measured

Apache is the only edge that crashes under WADM, and the effect is reproducible enough to
characterise. It matters for two reasons: it is a production-readiness result about mod_lua, and it
shapes how Apache's own numbers must be read.

**It is the response body filter.** Same load, same duration, the only difference being whether
`LuaOutputFilter`/`SetOutputFilter` are enabled:

| Configuration | Segfaults in 20 s at 900 req/s |
|---|---|
| Full WADM | **10** |
| WADM minus the body filter (detection and the trap still active) | **0** |

Detection (`detect.lua`) and the SQLi trap (`login.lua`) are not implicated. `inject.lua` is.

**It is driven by cumulative request volume, not by rate or concurrency.** At a fixed 90 req/s,
909 requests are clean and 8,100 crash four times. Nothing crashes below roughly 4,000 requests,
and above that the count grows with volume:

| Source | Requests served | Segfaults |
|---|---|---|
| standalone, 90 req/s | 819 – 4,050 | 0 |
| standalone, 90 req/s | 5,409 | 1 |
| standalone, 90 req/s | 8,100 | 4 |
| standalone, 900 req/s | 18,000 | 10 |
| paired suite, rate 1 (mean of 5) | 1,134 | **0.0** |
| paired suite, rate 10 (mean of 5) | 5,670 | 2.0 |
| paired suite, rate 100 (mean of 5) | 37,800 | 17.4 |

Two observations matter more than any fitted curve. First, **the onset is volume-dependent and
rate-independent**: 909 requests at 90 req/s are clean while 8,100 at the same 90 req/s crash four
times, and the paired suite's rate-1 cells — 1,134 requests including warm-up — never crashed in
any of five replicates. Second, **the rate is not constant**. A linear `(requests − 4000) / 1000`
describes the 4k–8k range exactly (predicting 0, 1 and 4 against 0, 1 and 4 observed) but
over-predicts beyond it: 14 against 10 observed at 18,000 requests, and 34 against 17.4 at 37,800.
Growth is sub-linear at high volume, plausibly because Apache replaces dead children and the
per-child request count that matters then accumulates more slowly across more children. The linear
form should be quoted only as an onset threshold, not extrapolated.

*(Counting note: one dead child produces one log line — `AH00052: child pid N exit signal
Segmentation fault (11)` — which matches both the "child pid … exit signal" and the "segmentation
fault" detectors. `count_crashes` originally summed per-pattern matches and so reported exactly
double; it now counts distinct lines, and the stored results were corrected.)*

A threshold followed by a constant rate per request, independent of arrival rate, is the signature
of something accumulating per request rather than a race between concurrent requests. `inject.lua`
runs as a Lua coroutine over Apache's bucket brigade and buffers the whole body; with
`LuaScope thread` and `LuaCodeCache forever` that Lua state is reused for every request on the
thread, and the `bucket` global still references the last chunk after the filter returns.
Identifying the exact allocation would need a core dump and is out of scope here — what is
established is the component, the driver, and the rate.

**The fix, measured.** Four configurations under identical load (900 req/s for 20 s):

| Configuration | Segfaults | Requests placed |
|---|---|---|
| baseline (`LuaScope thread`, `LuaCodeCache forever`) | 7 | 16,974 |
| `LuaScope request` | 0 | 10,550 — could not keep up |
| **`LuaScope conn`** | **0** | 16,560 |
| `LuaCodeCache stat` | 9 | 15,660 |

The code cache is not involved; narrowing the Lua state's lifetime is what fixes it, which
localises the leak to state reused across connections on a worker thread. `LuaScope request` works
but costs too much to be usable — a fresh Lua state per request left a third of the offered load
unplaced. `LuaScope conn` achieves the same zero crashes at baseline throughput.

Re-measured away from saturation (90 req/s, 90 s, after a warm-up, so the latencies are meaningful):

| Configuration | Requests | Segfaults | median | p95 |
|---|---|---|---|---|
| baseline | 8,109 | **4** | 1.02 ms | 3.39 ms |
| `LuaScope conn` | 8,109 | **0** | 1.32 ms | 6.43 ms |

The crash is eliminated for **+0.30 ms median (+29%) and +3.04 ms p95 (+90%)**, with no throughput
loss at this rate. One run per arm, so treat the latency figures as directional; the crash counts
are the robust part.

**The dominant cost is CPU, not latency.** A one-replicate validation run of the variant through
the full paired harness (so bare and WADM measured as a pair, same protocol as every edge):

| Offered rate | `LuaScope thread` CPU | `LuaScope conn` CPU | Latency (thread → conn) |
|---|---|---|---|
| 1/s | +143 µs/req | **+1,940 µs/req** | 1.29 → 1.57 ms/iter |
| 10/s | +218 µs/req | **+1,591 µs/req** | 1.04 → 0.98 ms/iter |
| 100/s | +547 µs/req | **could not sustain the rate** | — |

End-to-end latency barely moves at low rates, because there is CPU headroom to absorb the extra
work — which is exactly why latency alone would have made this fix look nearly free. The CPU plane
shows it costs roughly **7–9× more CPU per request**, and at 900 req/s the variant failed the run
outright (86.6% of offered iterations placed, 1.85% of requests failed). Rebuilding a Lua state per
connection is not cheap, and Apache's capacity under the remediation is materially lower.

Two further consequences, both found by the harness rather than assumed:

* **The measured mechanism cost moves too.** The expectation was that `LuaScope` changes only when a
  state is built, leaving the detect/inject medians alone. It does not: under `conn` scope the first
  request on each connection pays module-level setup *inside* the timed regions, and
  `form_fields.inject` rose from 3 µs to 20 µs. `plot_remediation.py` prints this comparison rather
  than drawing it, which is what caught it.
* **A rate the variant cannot sustain is a result, not a zero.** Its rate-100 cell is rejected, so
  there is no paired difference to report there. The figure labels that bar "did not sustain"; a
  zero-height bar would have read as "costs nothing".

These come from one replicate and are directional. The five-replicate run gives the reportable
figures.

**The trade-off is behavioural, not only performance.** `detect.lua` keeps its attacker store in a
module-scope table, which the comments describe as mirroring OpenResty's `ngx.shared.wadm_state`.
Under `LuaScope conn` that table is rebuilt per connection, so recorded attacker IPs are forgotten
as soon as the connection closes and Apache no longer mirrors OpenResty's shared dictionary — an
`EDGE_LEVELING.md` parity invariant. The timed region still performs an equivalent table write, so
the cost measurements stay comparable; it is the deception feature that degrades.

**Recommendation for the write-up:** keep the shipped configuration for the measured results, where
the crash is characterised rather than configured away, and report `LuaScope conn` as a validated
remediation with its cost stated. `MaxConnectionsPerChild 1000` would also recycle children before
the threshold, but that masks the leak rather than removing it.

**How to read Apache's numbers.** Its CPU cost per request climbs with load (+200 µs at rate 1,
+217 at rate 10, +547 at rate 100) while the other edges stay flat, and its marginal-CPU fit is
rejected as unphysical. Process teardown and respawn are a plausible contributor to that
non-linearity, so Apache's high-rate CPU figure should be reported as "WADM plus crash recovery",
not as the cost of the deception logic. Its internal per-operation timings are unaffected: the
medians are stable at 4 µs across all three rates, because a crashed child contributes no samples
rather than wrong ones.

## 4c. The remediation variant as a separate edge

`LuaScope conn` is shipped as its own edge key, `apache_lua_conn`, backed by `httpd-conn.conf`.
That file is byte-identical to `httpd.conf` apart from the single `LuaScope` directive, so a
difference measured between the two is attributable to the Lua state's lifetime and nothing else.

**It is deliberately excluded from `--all`.** The four-edge comparison rests on every edge running
the same mechanism; an edge whose runtime semantics differ would make that figure compare different
things. `EDGE_KEYS` therefore stays at four, `VARIANT_KEYS` holds the variant, and `--edge` accepts
either. `analyze_paired.py` discovers result files rather than iterating a fixed list, so a variant
is reported automatically once its file exists, after the main edges.

Both arms share one bare tier: `httpd-baseline.conf` carries no mod_lua at all, so `LuaScope` is
meaningless there and the same floor serves both. Each arm's WADM overhead is therefore measured
above the same baseline and the two are directly comparable.

```bash
# The variant, same protocol as every other edge (~48 min).
python3 benchmarks/run_paired_benchmark.py --edge apache_lua_conn --preset full
python3 benchmarks/run_capacity_benchmark.py --edge apache_lua_conn

# Its own figure and console report: what it fixes, what it costs.
python3 benchmarks/plot_remediation.py
```

`plot_remediation.py` draws three panels — segfaults per 10,000 requests, WADM latency overhead,
WADM CPU cost — with the shipped configuration solid and the remediation hatched in the same Apache
hue, because these are two settings of one edge rather than two edges. It also prints whether the
internal microsecond medians moved. The expectation was that they would not — `LuaScope` changes
when a Lua state is built, not what the detection and injection code does — and the check is
printed rather than drawn precisely so it could contradict that. It did (§4c.1), which is worth
more than a figure asserting that two bars are the same height.

### 4c.1 Measured result (5 replicates, full preset)

| Offered rate | Segfaults / 10k req | Latency overhead (ms/iter) | CPU cost (µs/req) |
|---|---|---|---|
| 1/s  | 0.0 → **0.0** | +1.29 → +1.09 | +143 → **+1946** |
| 10/s | 4.9 → **0.0** | +1.05 → +0.80 | +218 → **+1399** |
| 100/s| 6.4 → **0.0** | +1.45 → *not sustained* | +547 → *not sustained* |

*(`LuaScope thread` → `LuaScope conn`; both above the same bare tier.)*

**The remediation works.** Segfaults go to zero at every rate, across all five replicates. That is
the whole reason the variant exists and it is unambiguous.

**It is not free, and latency alone would have said it was.** End-to-end overhead is flat or lower
under `conn`, which is what a first look at latency reports. The CPU plane says something else
entirely: **7–9× the marginal CPU per request**. At low rates the machine has enough headroom to
absorb the extra work without it reaching the response time, so latency simply cannot see it. This
is the clearest case in the dataset for measuring cost in work as well as in time.

**Capacity roughly halves.** At an offered 100 iterations/s (≈900 req/s) all five WADM replicates
were rejected — 0.83–1.60% failed requests, 48–292 dropped iterations of 3,000, and 90–95% of the
offered rate placed. The capacity ladder agrees independently: `thread` reached 897 req/s at a
408 ms p95, `conn` 829 req/s at a 1,095 ms p95, and both fail the 100 ms SLO at that step. The
figure labels this *"did not sustain"* rather than drawing a zero-height bar, because a zero would
read as "costs nothing" and invert the finding.

**It also inflates the measured mechanism cost.** The expectation was that `LuaScope` changes only
*when* a Lua state is built, leaving the detection and injection medians alone. It does not: under
`conn` scope the first request on each connection pays module-level setup *inside* the timed
regions, and `form_fields.inject` moved from 3 µs to 21 µs at rate 1 and 14 µs at rate 10. Only
5–6 of 14 medians stayed identical. This is a second, independent reason to keep the variant out of
the cross-edge comparison: under `conn` the instrumented numbers no longer mean the same thing.

**How to present it.** The shipped `LuaScope thread` configuration is the benchmarked baseline and
belongs in the cross-edge comparison, because it runs the same algorithm as the other three edges.
The variant is reported separately as a fix that was identified, validated and costed — including
the behavioural cost, which is that `detect.lua`'s attacker store no longer survives a connection.

## 5. What this still cannot measure

Stated so the thesis does not over-claim:

- **A 5–30 µs end-to-end difference at low rates.** Even paired and replicated, the end-to-end plane
  resolves tens of microseconds at best. In practice this has not bitten: the measured GET effect
  is +160–360 µs and every GET cell at every rate resolved (5/5 replicates agree in sign). It would
  bite for a mechanism costing only what its timers enclose.
- **Anything about a real deployment's hardware.** Every number is from a VirtualBox guest sharing a
  laptop CPU. Relative comparisons between the four edges are the defensible claim; absolute
  microseconds are not portable.
- **Whether WADM's tail behaviour is acceptable in production.** The tail this suite measures is
  dominated by the measurement environment, and the gating removes those samples rather than
  explaining them.

---

## 6. Measurement and representation review (2026-10-03)

A review of whether the collection techniques and the figures answer the thesis's three
questions: how WADM affects the server's performance, how much latency it adds, and whether it
breaks existing functionality. The paired design (§1.3), the gates (§1.6) and the cgroup CPU
plane (§2) held up. The points below did not, or needed qualifying. Every number is from the
2026-09-30 paired dataset.

### 6.1 The median understates what WADM adds

`analyze_paired.py` previously differenced only k6's `med`, although every cell already stores
p5–p99. The paired difference at several quantiles (latency added per GET, averaged over the four
GET probes within each replicate, median across replicates):

| edge, rate | Δ p50 | Δ p75 | Δ p90 | Δ p95 |
|---|---|---|---|---|
| OpenResty 10/s | +203 µs | +273 | +456 | +618 |
| Envoy+Lua 10/s | +240 | +309 | +413 | +607 |
| WASM 10/s | +202 | +288 | +478 | +651 |
| Apache 10/s | +261 | +343 | +660 | +1,004 |
| **Apache 100/s** | **+364** | +1,320 | **+26,799** | **+49,960** |

WADM stretches the tail more than it shifts the middle: the p90 delta is 1.3–2.6× the median
delta on every edge, and on Apache at 900 req/s the median says "+0.36 ms" while one GET in ten is
~27 ms slower. The median stays the headline (it is robust on this VM); the shift function is
reported beside it. p99 is reported only pooled over the four GETs and only from 10/s upward: at
1/s each probe has ~90 samples per cell, so a per-probe p99 is effectively the maximum.

Relative overhead is reported too: +200 µs per GET is +22–36% of the bare latency here.

### 6.2 Rotation balances position, not the preceding request

`probeOrder()` shifts the probe list by one slot per iteration, so probe *i* is always preceded by
probe *i−1*. Position is balanced; carry-over from the previous request is confounded with probe
identity. Evidence: on `apache_lua_conn`, exactly the two probes that follow one of the trap's 500
responses (`sqli_hit_enc` after `sqli_hit`, `sqli_miss` after `sqli_hit_enc`) are +4.5–4.8 ms
slower, while their siblings are not. httpd closes keep-alive after a 500, so the next request
reconnects and, under `LuaScope conn`, builds a fresh Lua state. The same asymmetry, smaller,
appears on shipped Apache and contradicts the SQLi 2×2's premise (encoding costs µs, outcome
nothing). **Apache's end-to-end SQLi arms are therefore confounded; read the internal timers for
the 2×2.** Not yet fixed: the remedy is a Williams 8×8 order (each probe after every other probe
once per 8 iterations) plus a per-probe reconnect metric. Pending approval.

### 6.3 What the CPU figure does and does not contain

- **It is net of the trap's savings.** CPU per request divides all container CPU by all 9 requests
  of an iteration. On OpenResty, Envoy+Lua and Apache the trap answers 4 of those 9 itself instead
  of proxying them, saving proxy work, so the delta is "GET-path WADM cost − proxy work saved +
  trap rendering". cgroup counters cannot separate request types; a per-mechanism figure needs
  separate GET-only and POST-only runs (pending).
- **It includes the instrumentation.** Only the WADM tier writes timing lines — about 39 per
  iteration (4 on `GET /`, 11 on the tamper probe). Each is a `write()` charged to the edge and
  sitting in the request path; dockerd/containerd-shim then process them outside the cgroup on
  unpinned cores. Kernel time is 30–45% of WADM's CPU delta on OpenResty, Envoy and WASM (e.g.
  OpenResty 100/s: user +32, system +24 µs/req), which is not what Lua string work looks like. Part
  of the "instrumented blind spot" (§2) may therefore be the instrumentation itself. The lines were
  kept deliberately (THESIS_NOTES, "timing log lines were deliberately left alone"); a WADM-silent
  tier that turns them off without changing their format would measure this directly (pending).
- **Its window is wider than the measurement.** `cpu.stat` is read before and after the whole
  load-tester lifecycle (container start, the 5 s `startTime`, `gracefulStop`, teardown). Idle CPU
  in those ~10 s is billed to requests — Envoy idles at ~6.5 ms/s, ~80 µs/req at 1/s. It mostly
  cancels in a paired delta; the marginal fit (which regresses on the nominal duration) is the
  better per-request figure, and the cost-attribution figure now uses it.
- **It is not immune to hypervisor preemption on this guest.** `/proc/stat` steal is always 0:
  VirtualBox does not account it, so a vCPU the host takes away is billed to whichever task was
  running. Pairing handles this statistically; the counter does not. Instruction counts would be
  immune but need a vPMU (KVM, §4.5).

`analyze_paired.py` now reports the user/system split, which was recorded but unused.

### 6.4 Internal timers: read the trimmed mean

The edges log whole microseconds and most regions cost 1–5 µs, so a median of these samples can
only be an integer and cannot resolve sub-µs differences between edges. The quantisation phase
varies from sample to sample, so a mean over many samples recovers sub-tick resolution; trimming 5%
from each end removes the preemption tail the gate already bounds at 1%. `trimmed_mean_us` is now
the headline.

Clock semantics differ in one way that matters at this scale. OpenResty and Envoy+Lua (FFI
`gettimeofday`) and Apache (`r:clock()`) subtract two whole-µs timestamps, which is unbiased on
average. Envoy+WASM reads a nanosecond host clock and **truncates the difference** to whole µs
(`wasm-filter/src/lib.rs`, `elapsed_us`), so it reads ≈0.5 µs low against the other three —
15–50% of a 1–3 µs region. Cross-edge timer comparisons involving WASM carry that bias until
`elapsed_us` is changed (pending; needs a rebuild and re-run).

### 6.5 The capacity ladder does not support "throughput retained"

Cores in use at each step (CPU per request × served req/s; the edge is pinned to 2):

| edge | bare at failing step | WADM at failing step |
|---|---|---|
| OpenResty | 7,018 req/s, edge 0.89/2 | 4,737 req/s, edge 1.97/2 |
| Envoy+Lua | 7,162 req/s, edge 1.05/2 | 6,861 req/s, edge 1.52/2 |
| WASM | 9,581 req/s, edge 1.79/2 (7,200 passed at 1.04) | 6,188 req/s, edge 1.74/2 |

Every bare edge stalls near 7,000–7,200 req/s with its own cores half idle — a shared ceiling. In
the bare tier all 9 requests reach the origin (one core, ~130 µs CPU each ≈ 0.9 cores at that
rate); with WADM the trap keeps 4 of 9 away. The bare tier is most likely origin-bound and the WADM
tier edge-bound, so "100% throughput retained" (OpenResty, Envoy+Lua) compares two different
bottlenecks. The doubling ladder also cannot resolve differences under 2×, each step is a single
20 s run, and the capacity files predate origin-CPU recording. **Do not quote the capacity ratio
until the ladder records per-container utilisation** (pending). The headline table omits it.

### 6.6 Functional correctness, and the figure set

Goal three — does WADM break existing functionality — is the least measured. Under load the only
checks are k6 status whitelists, which accept 404/401/500 on both tiers, so a status change caused
by WADM passes. `parity_check.py` diffs each edge against OpenResty rather than against bare, once,
without load. The origin serves five HTML pages of 864–1,396 B, the best case for body injection.
Reading the edge code predicts untested breakage: no `Content-Encoding` guard (a gzip body gets a
plain-text comment appended); header and cookie baits on every response including non-HTML and
304; full-body buffering everywhere, with the Envoy edges bounded by Envoy's default 1 MiB buffer.
A WADM-vs-bare functional matrix and in-run integrity checks are proposed (pending).

One signal is available from existing data: timing samples per iteration against what the probes
should produce (`coverage` in `paired_summary.json`). OpenResty, Envoy and WASM record exactly the
expected count at every rate. Apache records 0.996–0.999 of it at 100/s on several kinds and
0.998 at 10/s on the SQLi arms: some requests that should have taken the WADM path left no record
(a crashed child or a dropped log line).

**Figures.** Every figure is now drawn from `paired_<edge>.json`:

| Figure / table | Script | Shows |
|---|---|---|
| `paired_latency_added.png` | `plot_paired_overhead.py` | Forest plot: median latency added per probe, edge and rate, with the replicate range; hollow = unresolved |
| `paired_latency_shift.png` | `plot_paired_overhead.py` | Shift function: latency added per GET at p50–p99 |
| `paired_internal_timers.png` | `plot_paired_overhead.py` | Heatmap of the in-edge timers, trimmed mean and p90 |
| `paired_headline.md` / `.csv` | `plot_paired_overhead.py` | One row per edge and rate: latency (abs, %), CPU (total, user, system, marginal), coverage |
| `cost_attribution.png` | `plot_cost_attribution.py` | Timers vs marginal CPU vs end-to-end, at the highest common rate |
| `instrumented_coverage.png` | `plot_cost_attribution.py` | Timers' share of the CPU WADM adds, with replicate range |
| `apache_remediation.png` | `plot_remediation.py` | LuaScope thread vs conn, now noting the post-500 reconnect cost |

The figures from `plot_baseline_comparison.py`, `plot_edge_comparison.py`,
`plot_token_comparison.py` and `plot_sqli_comparison.py` were drawn from the 2026-09-21/24 unpaired
constant-VU files and are retired; the right-hand panel of `instrumented_coverage` (work ÷ wall
clock) was dropped as uninterpretable. Everything the review replaced is in
`benchmarks/results/archive/2026-10-03-pre-critique/`, and any later regeneration copies what it
replaces into a new dated folder under `results/archive/` first.
