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
| 1 | `sudo ./benchmarks/tune_guest.sh apply` | seconds | Disables THP and swap, widens the accept queue and ephemeral port range, stops the apt/man-db timers. Fully reversible. | Once per session, before 2–7 |
| 2 | `run_paired_benchmark.py --preset smoke --all --results <scratch>` | **~40 min** | The whole pipeline at 1 replicate and short windows. Proves the harness works before you commit hours to it. Its numbers are **not reportable** — one replicate cannot produce an interval — and `--results` keeps it away from the dataset. | Once, the first time |
| 3 | `functional_matrix.py` | ~10 min | WADM vs bare across 13 response shapes on all four edges (§7.3). | Yes |
| 4 | `run_paired_suite.sh` (or `run_paired_benchmark.py --preset full --all`) | **~6.5 h** | The reportable dataset: 5 replicates × 3 rates × 4 tiers (origin, bare, silent, wadm) × 4 edges, paired, order-balanced, gated and functionally checked. The script also archives the previous dataset first and regenerates every table and figure drawn from it (§9). | Yes — this is the dataset |
| 5 | `run_paired_benchmark.py --all --mix get --rates 100 --replicates 3` and `--mix size --rates 10 --replicates 3` | ~1 h | The GET-only run that separates the trap's CPU from the GET path's, and the page-size sweep (§7.5). | For per-mechanism and size claims |
| 5b | `run_sqli_suite.sh` | **~1.5 h** | The SQLi trap's cost per hit and per miss, interleaved, 5 replicates of 20 s (§7.5). | For any SQLi hit/miss claim |
| 6 | `run_capacity_benchmark.py --all` | **~2.5 h** | The saturation ladder with per-container utilisation, bisection and 3 replicates per step (§7.4). | Yes, for any capacity claim |
| 7 | `run_paired_benchmark.py --edge apache_lua_conn --tiers bare,wadm` | ~45 min | The Apache remediation variant, re-measured under the same order and checks (§4c). | For the remediation figure |
| 8 | `analyze_paired.py`, then `plot_paired_overhead.py`, `plot_by_rate.py`, `plot_cost_attribution.py`, `plot_mechanisms.py`, `plot_remediation.py`, `plot_origin_floor.py`, `report_capacity.py` | ~5 min total | The console report, every figure and every table (§6.6, §7.6). | Yes |
| 9 | `sudo ./benchmarks/tune_guest.sh revert` | seconds | Puts the guest back. | When you are done |

Steps 2–3 are a rehearsal and a one-shot check. Steps 4–7 are the long ones and can be left
unattended — but nothing else should run on the VM *or on the host* while they do. Before 4,
copy `results/` into a new dated folder under `results/archive/` if it holds anything you want to
keep; the runners overwrite their files.

**If a run is interrupted:** `paired_<edge>.json` is written once, at the end of that edge, so an
interrupted edge leaves no file and the completed ones are untouched. Re-run just that one with
`--edge <name>`.

**Splitting step 4 across sessions** is fine — run `--edge openresty`, later `--edge wasm`, and so
on. Each edge writes its own file and `analyze_paired.py` reads whatever is present. Pairing is
*within* an edge, so splitting across days costs nothing.


```bash
# Once per session, before measuring (guest side).
sudo ./benchmarks/tune_guest.sh apply

# Prove the harness works end to end. ~40 minutes, not reportable — one replicate
# cannot produce an interval — and written to a scratch directory so it cannot overwrite results/.
python3 benchmarks/run_paired_benchmark.py --preset smoke --all --results /tmp/wadm-smoke
python3 benchmarks/analyze_paired.py --results /tmp/wadm-smoke

# Does WADM break what the server did without it? ~10 minutes.
python3 benchmarks/functional_matrix.py

# The reportable run: four tiers (origin, bare, silent, wadm), five replicates. ~6.5 hours for four
# edges. run_paired_suite.sh does the same, archives the previous dataset first, and redraws its figures.
./benchmarks/run_paired_suite.sh

# Per-mechanism CPU (GET path vs. trap) and the page-size sweep. ~1 hour.
python3 benchmarks/run_paired_benchmark.py --all --mix get --rates 100 --replicates 3
python3 benchmarks/run_paired_benchmark.py --all --mix size --rates 10 --replicates 3

# Capacity, with the resource that limits it. ~2.5 hours.
python3 benchmarks/run_capacity_benchmark.py --all

# The Apache remediation variant, same order and checks. ~45 minutes.
python3 benchmarks/run_paired_benchmark.py --edge apache_lua_conn --tiers bare,wadm

# Report, figures and tables.
python3 benchmarks/analyze_paired.py
python3 benchmarks/plot_paired_overhead.py    # latency added, shift function, timers, tables
python3 benchmarks/plot_by_rate.py            # one measure per figure, per rate
python3 benchmarks/plot_cost_attribution.py   # timers vs CPU vs latency, and the probe effect
python3 benchmarks/plot_mechanisms.py         # CPU per mechanism, latency vs page size
python3 benchmarks/plot_remediation.py        # Apache LuaScope thread vs conn (section 4c)
python3 benchmarks/plot_origin_floor.py       # bare and WADM against the no-proxy floor (section 9)
python3 benchmarks/report_capacity.py         # capacity with its binding resource

sudo ./benchmarks/tune_guest.sh revert
```

`run_paired_benchmark.py` writes `paired_<edge>.json` as the record of truth — every cell, every
tier, all replicates, each with its validity verdict. `analyze_paired.py` derives
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
once per 8 iterations) plus a per-probe reconnect metric — both now implemented (§7.2).

### 6.3 What the CPU figure does and does not contain

- **It is net of the trap's savings.** CPU per request divides all container CPU by all 9 requests
  of an iteration. On OpenResty, Envoy+Lua and Apache the trap answers 4 of those 9 itself instead
  of proxying them, saving proxy work, so the delta is "GET-path WADM cost − proxy work saved +
  trap rendering". cgroup counters cannot separate request types; a per-mechanism figure needs
  separate GET-only and POST-only runs — now `--mix get` (§7.5).
- **It includes the instrumentation.** Only the WADM tier writes timing lines — about 39 per
  iteration (4 on `GET /`, 11 on the tamper probe). Each is a `write()` charged to the edge and
  sitting in the request path; dockerd/containerd-shim then process them outside the cgroup on
  unpinned cores. Kernel time is 30–45% of WADM's CPU delta on OpenResty, Envoy and WASM (e.g.
  OpenResty 100/s: user +32, system +24 µs/req), which is not what Lua string work looks like. Part
  of the "instrumented blind spot" (§2) may therefore be the instrumentation itself. The lines were
  kept deliberately (THESIS_NOTES, "timing log lines were deliberately left alone"); a WADM-silent
  tier that turns them off without changing their format now measures this directly (§7.1).
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
`elapsed_us` was changed (§7.1; `docs/EDGE_LEVELING.md`).

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
until the ladder records per-container utilisation** — it now does (§7.4).

### 6.6 Functional correctness, and the figure set

Goal three — does WADM break existing functionality — is the least measured. Under load the only
checks are k6 status whitelists, which accept 404/401/500 on both tiers, so a status change caused
by WADM passes. `parity_check.py` diffs each edge against OpenResty rather than against bare, once,
without load. The origin serves five HTML pages of 864–1,396 B, the best case for body injection.
Reading the edge code predicts untested breakage: no `Content-Encoding` guard (a gzip body gets a
plain-text comment appended); header and cookie baits on every response including non-HTML and
304; full-body buffering everywhere, with the Envoy edges bounded by Envoy's default 1 MiB buffer.
A WADM-vs-bare functional matrix and in-run integrity checks now exist (§7.3).

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

---

## 7. What the harness measures since 2026-10-03

The review in §6 listed what the paired harness could not see. This section records what was built
to see it. Results are in §8.

### 7.1 Three tiers: bare, silent, instrumented

Every paired cell now runs three tiers, not two:

| Tier | Server config | `config.json` | What it is |
|---|---|---|---|
| `bare` | WADM-free | — | the server before WADM |
| `silent` | WADM | generated `config-silent.json`, `emit_timings: false` | WADM as a deployment would run it |
| `wadm` | WADM | `config.json` | the instrumented build; the only tier with in-edge timers |

**silent − bare is the headline cost of WADM**; wadm − silent is the instrumentation's own cost;
wadm − bare is what the two-tier harness reported. `config-silent.json` is regenerated from
`config.json` at every run, so the two can only differ in the one flag. The tier order is balanced
across replicates (`tier_order`): with three tiers it cycles through all six orders, so every tier
occupies every position twice in six replicates. The edge-side switch, and the WASM timer
correction made at the same time, are recorded in `docs/EDGE_LEVELING.md`.

Since 2026-10-13 the reportable `all` mix runs a fourth tier, `origin`, in the same replicates: the
same probes sent to the origin directly, with no proxy in the path. It is described in §9.

### 7.2 A probe order that balances the preceding request

`test.js` defaults to `WADM_ORDER=williams`: rotating Williams' base sequence `0, 1, 7, 2, 6, 3, 5,
4` instead of the identity. Over any eight consecutive iterations every probe occupies every
position once **and** follows every other probe once, so whatever one request leaves behind — such
as Apache closing the connection after a 500 — is spread over all probes instead of landing on the
same successor every time (§6.2). `rotate` and `fixed` remain for reproducing older results. Every
probe also records `new_conn_<probe>`: the share of its requests that opened a TCP connection
instead of reusing one.

### 7.3 Functional checks

**In every paired cell.** Before the load starts, `test.js`'s `setup()` sends each probe once to the
edge and once to the origin directly, and records both responses' status and body hash. That gives
two checks per probe and tier: whether the edge returns what the origin returns (must hold on bare),
and whether the bait header, cookie and comment are present (must hold on WADM tiers for every
proxied probe). Every measured response is then compared with the edge's own reference:
`intact_<probe>` is the share that came back with the same status and body hash, so a response
corrupted, truncated or left un-injected under load shows up as a rate below 1. The runner also
counts the `WADM ALERT` and `WADM TRAP` lines the edge logged in the measured window: one
iteration should produce 7 and 2, so alert recall is counted ÷ expected, and on bare the count must
be 0. Alerts are WADM's only output; a detection that leaves no line was, operationally, not made.

**Once, across response shapes.** `functional_matrix.py` brings up all four edges, bare then WADM,
and sends thirteen requests the probe set never exercises: an HTML page, a gzip-encoded page, HEAD,
a conditional GET, a range request, a 204, a 404, JSON, binary, a page without `</body>`, 768 KiB
and 2 MiB pages, and an HTTP/1.0 client. The origin serves the extra cases from generated pages
(`backend/generate_test_pages.py`, `backend/www/test/`) and two locations in `backend/nginx.conf`
(gzip, 204) that the latency probes never touch. Each WADM response is judged against the same
edge's bare response — `preserved`, `injected` (HTML that differs only by inserted text, checked
with a line-then-character diff), `changed`, `BROKEN` or `not exercised` (the bare edge itself
refuses the case) — and a benign request that raises an alert is a false positive.

### 7.4 Capacity with the binding resource named

`run_capacity_benchmark.py` (schema `capacity-v2`) now:

* samples the edge, the origin and the load generator from the host and records each one's cores
  in use as a share of its cpuset, plus the host-wide busy and softirq shares, and names the busiest
  component at the first failing rate as the binding resource;
* bisects geometrically between the last passing and first failing rate (two steps by default), so
  the capacity is resolved to about 20% rather than 2×;
* runs every rate `--replicates` times (3 by default) and calls it sustained on a majority;
* judges the SLO on the worst GET probe's p95, not the pooled p95 the trap's fast local answers
  would flatter;
* reports, beside the measured capacity, the edge-CPU-bound capacity: edge cores ÷ edge CPU per
  request at the highest sustained rate. That figure is continuous and does not depend on which
  other component saturates first, so it can be compared across tiers when the measured
  capacities cannot.

`report_capacity.py` prints the binding resource next to every capacity and refuses to call a
throughput ratio comparable when the two tiers were limited by different components.

### 7.5 Per-mechanism CPU and the page-size sweep

`--mix get` runs the paired harness with only the priming GET and the four GET probes; the full mix
minus this run isolates what the trap's four POSTs cost the edge, which the mixed figure nets away
(§6.3). `--mix size` replaces the probes with one GET each for 1, 16, 256 and 768 KiB pages, all
under Envoy's 1 MiB buffer. Both write `<mix>_<edge>.json`, never `paired_<edge>.json`, and
`plot_mechanisms.py` draws them. CPU per iteration there is net of idle: the edge's idle rate,
measured in the k6 `startTime` delay just before each window, times the window length is subtracted.

**SQLi hit vs. miss.** The main mix measures every SQLi request's latency and the scan timer, but
not what one hit or one miss costs in CPU. `run_sqli_suite.sh` runs, per edge,
`run_paired_benchmark.py --mixes sqli-hit,sqli-miss --no-prime`: inside every replicate and tier a
hit-only cell and a miss-only cell run minutes apart (order alternating by replicate), and every
iteration of a cell is exactly two requests of its outcome (plain and %-encoded body, no priming
GET), so CPU per iteration net of idle, halved, is the cost of one request of that outcome, and
hit − miss is paired in time like every other difference. 100 iterations/s (200 req/s), 20 s
windows (~4,000 requests a cell) and 5 replicates, ~1.5 h — deliberately shorter than the main runs,
since this comparison is secondary (a 60 s × 7 configuration was tried first and abandoned, §8.8). It writes `sqli-hit_<edge>.json` and
`sqli-miss_<edge>.json`; `plot_sqli.py` draws `sqli_hit_miss.png` — one row per edge, hit (circle)
and miss (diamond) side by side in three panels: latency added, CPU added, and time inside the
signature scan, with the hit − miss gap printed — and writes `sqli.md`, which adds absolute
latencies, each encoding's scan time, reconnects, and the ALERT/TRAP lines per request (1/0 for a
hit, 0/1 for a miss, so a missed detection or a false positive is visible).

### 7.6 The measurement window, and what each cell now records

CPU is no longer read with `docker compose exec cat cpu.stat` around the whole load-tester
lifecycle. A host-side sampler reads each container's `cpu.stat` and `memory.current` four times a
second, `test.js` logs the instant the first measured iteration starts, and the CPU is integrated
over exactly the measured window. The same timestamp filters the edge's log lines, so timing
samples and alert counts exclude `setup()`'s reference requests and the warm-up's tail; the coverage
ratio (§6.6) is exact rather than off by a sample or two. The denominator for CPU per request is
`edge_requests` — the requests the measured iterations sent — rather than k6's `http_reqs`, which
also counts `setup()`. The exec-based reading is kept as `cpu.edge_lifecycle` for comparison.

| Cell field | Content |
|---|---|
| `window` | k6's first measured iteration and the end of the measured duration (epoch s) |
| `cpu.edge`, `cpu.origin` | CPU over the window: per request (total, user, system), cores in use, memory max/mean |
| `cpu.edge.idle_cpu_us_per_s` | the edge's CPU while idle, just before the window |
| `cpu.loadgen` | the load generator's cores in use |
| `k6.intact`, `k6.new_conn` | per probe: share intact, share that reconnected |
| `k6.reference` | per probe: the edge's and origin's reference responses, bait presence |
| `alerts` | `WADM ALERT` / `WADM TRAP` lines in the window |

Outputs added: `paired_functional.md/.csv`, `functional_matrix.md/.json`, `mechanisms.md`,
`capacity_summary.md`, and the figures `mechanism_cpu.png`, `size_sweep.png`, `capacity.png`.

---

## 8. Results of the 2026-10-04/05 suite

Run `suite_20261004T203641Z` (log and status file in `benchmarks/results/`), guest tuned, all steps
OK. Tables: `paired_headline.md`, `paired_functional.md`, `functional_matrix.md`, `mechanisms.md`,
`capacity_summary.md`; figures in `results/plots/`.

**Run incidents.** The host slept for 2.8 h mid-run (VirtualBox logged a 10,200 s clock jump at
04:29 UTC); the one step it overlapped, Apache's GET-only run, was re-run and its first version
archived (`archive/2026-10-05-host-sleep/`). 15 cells were rejected, all explained: Apache at
100/s (overloaded and segfaulting, 6), the `LuaScope conn` variant at 100/s (cannot sustain it, 5),
and 4 instrumented cells of the size sweep, where the 1 ms "preemption" gate fires on legitimately
slow injection into a 768 KiB page — a known false positive of that gate for large bodies; the size
figure uses the silent tier and is unaffected.

### 8.1 Latency added (silent − bare, median per GET)

| Edge | 1/s | 10/s | 100/s | Δ p95 per GET |
|---|---|---|---|---|
| OpenResty | +166 µs (+19%) | +114 (+13%) | +109 (+20%) | +192 … +591 |
| WASM | +149 (+13%) | +144 (+14%) | +134 (+20%) | +242 … +1,067 |
| Envoy+Lua | +208 (+20%) | +163 (+16%) | +136 (+21%) | +351 … +478 |
| Apache+Lua | +218 (+23%) | +162 (+17%) | **+2,174 (+334%)** | +320 … **+158,377** |

Every GET cell is resolved (5/5 replicates agree in sign) except two `GET /` cells. The tail grows
more than the median everywhere; Apache at 900 req/s is overloaded. The SQLi POSTs come back
*faster* with WADM on three edges (the trap skips the origin) and slower on WASM, which still
proxies them.

### 8.2 The measurement's own cost

The timing lines (wadm − silent) add +56 … +178 µs per GET of latency and up to +63 µs of CPU per
request — on OpenResty and Envoy+Lua as much as WADM itself. Every two-tier figure before this run
over-stated WADM's cost by that amount.

### 8.3 CPU

Marginal CPU per request, silent − bare: OpenResty +33 µs, Envoy+Lua +41, WASM +123, Apache +200
(Apache's includes crash recovery; per-rate it is +113 … +139 at 1–10/s). The in-edge timers
enclose 5–22% of it. By mechanism at 100/s (`mechanisms.md`): the GET path costs +62 (OpenResty),
+102 (WASM, Envoy+Lua) and +236 µs (Apache) per GET; the trap costs about nothing per POST on
OpenResty, *saves* 65 µs on Envoy+Lua, costs +146 on WASM (it still proxies) and +1,815 on Apache
(crash recovery at that rate).

### 8.4 Page size

Injection cost grows roughly linearly with the body: at 768 KiB it adds +0.9 ms (WASM), +1.4
(Envoy+Lua), +1.5 (OpenResty) and +3.0 ms (Apache) per request, against ~0.1 ms at 1 KiB.

### 8.5 Functionality

* **Under load:** OpenResty, WASM and Envoy+Lua return 100% intact responses at every rate, with
  100% alert and trap recall and no alerts on bare. Apache segfaults under WADM in both WADM tiers;
  at 100/s it corrupts or fails ~1% of responses (worst intact 98.9%) and loses 0.2% of alerts and
  0.8% of trap lines. Apache also closes the connection after the trap's 500, so 25% of all WADM
  requests reconnect (1% on bare).
* **Response shapes** (`functional_matrix.md`): gzip-encoded HTML and range requests are corrupted
  on all four edges; HEAD advertises a wrong Content-Length on Apache (47 bytes) and Envoy+Lua
  (the origin's); pages above 1 MiB fail on both Envoy edges (500, or a truncated body). Bait
  headers are added to every response including JSON, binary, 204 and 304; three edges keep the
  origin's ETag on a modified body. No benign request raised an alert.

### 8.6 Capacity

| Edge | bare | silent WADM | instrumented |
|---|---|---|---|
| OpenResty | 6,057 req/s | 4,284 (71%) | 4,284 |
| WASM | 6,057 | 4,285 (71%) | 3,024 |
| Envoy+Lua | 6,057 | 5,095 (84%) | 3,600 |
| Apache+Lua | 3,024 | 900 (30%) | 901 |

No single container was saturated at any failing step (all below 85% of their cores, the host 65–86%
busy), so the measured limits are partly the VM's network path and the ratios are indicative. The
edge-CPU-bound capacity, which does not depend on that, keeps 55–64% for silent WADM.

### 8.7 Apache remediation (`LuaScope conn`)

Zero segfaults (against 0.98–5.4 per 10,000 requests), at ten times the CPU (+1,847 against +178 µs
per request at 1/s) and +3.2 against +1.3 ms per iteration of latency; neither configuration
sustains 100/s. With the Williams order the reconnect cost after each 500 is now spread over every
probe rather than charged to two, which is why the variant's latency overhead is higher than the
rotate-order figure in §4c.1.

### 8.8 SQLi hit/miss: first configuration abandoned (2026-10-06)

*Superseded:* the long configuration below was abandoned the same day. Its files, both passes and
their logs are in `archive/2026-10-06-sqli-long-runs/`, and all four edges were re-run with 20 s
windows and 5 replicates. The account is kept for the record.

`run_sqli_suite.sh` (`sqli_suite_20261005T203935Z`) completed, but the host repeatedly froze the VM
(27+ kernel soft lockups of 10-335 s, one 14.7 h pause), and the `host_stalls` gate rejected every
cell that overlapped a freeze. Usable silent/bare pairs per edge: OpenResty 4 hit / 5 miss, WASM 5 / 5,
**Envoy+Lua 5 / 3, Apache 1 / 1**. `sqli_hit_miss.png` and `sqli.md` currently show this state;
Apache's row is not reportable.

A second pass of Apache and Envoy+Lua was started and stopped on request before any edge
finished. Still to do, on a quiet host (plugged in, sleep off, VirtualBox in the foreground):

```bash
sudo ./benchmarks/tune_guest.sh apply
for e in apache_lua envoy_lua; do
  python3 benchmarks/run_paired_benchmark.py --edge $e --mixes sqli-hit,sqli-miss --no-prime \
      --rates 100 --duration 60s --replicates 7 --results /tmp/sqli_pass2
done
```

then append the second pass's cells to `results/sqli-{hit,miss}_{apache_lua,envoy_lua}.json` as
replicates 8-14 (renumber `replicate` += 7, so each pair stays within one replicate) and re-run
`plot_sqli.py`. The first-pass files are kept in `archive/2026-10-06-sqli-first-pass/`.

### 8.9 SQLi hit vs. miss (20 s × 5, 2026-10-07)

`sqli_suite_20261007T003347Z`, guest tuned, plus one Apache top-up pass with the same settings
(merged as replicates 6-10; pre-merge files in `archive/2026-10-07-sqli-apache-before-topup/`).
Host freezes continued (11 in two hours) and rejected the cells they touched; usable silent/bare
pairs: OpenResty 4 hit / 3 miss, WASM 4 / 4, Apache 7 / 7, Envoy+Lua 4 / 3. `sqli.md`,
`plots/sqli_hit_miss.png`.

* **Latency vs. bare** (median request): the trap answers 0.39-0.45 ms *faster* than the origin
  round-trip on OpenResty and Envoy+Lua for both outcomes, and on Apache −0.16 ms for a hit and
  −0.31 ms for a miss. Envoy+WASM, which still forwards the request, adds +0.21 ms for either.
* **CPU vs. bare:** answering locally is cheaper than proxying on OpenResty (−41/−43 µs per request)
  and Envoy+Lua (−56/−76); WASM adds ~+195 µs for either outcome; Apache adds +75 µs for a miss and
  +257 µs for a hit.
* **Hit − miss:** indistinguishable from zero on OpenResty, WASM and Envoy+Lua (within ±40 µs, ranges
  span zero). On Apache a hit costs +178 µs of latency and +172 µs of CPU more than a miss, because
  Apache closes the connection after the trap's 500: 100% of requests after a hit reconnect (5% on
  bare). The signature scan itself is never the difference — a hit's scan is 0.6-4.2 µs *shorter*
  than a miss's, as it stops at the first matching signature.
* **Detection:** exactly one ALERT per hit and one TRAP per miss on every edge, no ALERT on a miss.

## 9. The origin floor (2026-10-13)

Every latency figure up to §8 is a difference between two configurations of the same edge. It says
what WADM costs on top of a proxy, and nothing about what the proxy itself costs. The pre-paired
harness did measure a no-proxy tier (`run_baseline_benchmark.py --edge origin`), but in a session of
its own, so any difference against it carried the drift §1.3 describes; it was retired with that
harness and its files are in `archive/2026-10-05-legacy-unused/`. The reportable `all` mix now
measures the same floor again, inside the paired design.

### 9.1 How the origin tier runs

| | Origin tier | Proxied tiers (bare, silent, wadm) |
|---|---|---|
| k6 sends to | `http://backend:80` | the edge, which forwards to `http://backend:80` |
| Iteration | identical: priming GET, four GETs, four SQLi POSTs, Williams order | same |
| Rate, window, warm-up, start delay, seed | identical | same |
| Load generator, cores, network | identical (`load-tester`, cores 4-7, `honeypot` bridge) | same |
| Edge container | left running from the previous tier, idle, out of the path | serves every request |
| Recorded | k6 trends, reconnects, integrity, origin CPU | additionally edge CPU, alert and timing-line counts |

* It is a fourth tier in every replicate of every edge's run, ordered with the other three by
  `tier_order`. Over eight replicates every tier holds every position twice; the five replicates of
  the full preset cover five rows of that cycle, so position is spread but not perfectly balanced.
* `bare − origin` and `silent − origin` are therefore paired exactly as `silent − bare` is: within a
  replicate, minutes apart, summarised as the median of the per-replicate differences with their
  range and sign agreement.
* There are four floors, one per edge run. They should agree; the spread between them is a direct
  reading of how far the floor moved between the four edges' sessions, which is what pairing removes
  from the differences.
* The edge is neither started nor stopped for the origin tier. Recreating it would cost the next
  proxied tier the warm state that a targeted `up -d` preserves. The idle edge sits on cores 1-2,
  disjoint from the origin (3) and the load generator (4-7).
* Edge CPU, alert counts and timing-line counts are not recorded for the origin tier, because each
  would be a vacuous zero or an idle reading divided by requests the edge never served. The origin
  CPU control (§1.6c) and every other validity gate apply unchanged.
* The tier is the default for `--mix all` only. The GET-only and page-size runs keep three tiers.
  The SQLi mixes refuse it: the origin has no trap, so it has nothing to compare the trap's answer
  against.

### 9.2 What the differences mean, and what they do not

* **bare − origin** is what putting the proxy, without WADM, in the request path adds on this host.
  It includes the proxy's own request handling, forwarding and upstream connection pool, *and* one
  extra pass across the Docker bridge in each direction. It is a property of this topology (three
  containers on one bridge), not of the proxy software alone. A proxy on another machine would
  replace the bridge pass with a real network hop.
* **silent − origin** is the deployed stack: proxy plus WADM, without the benchmark's timing lines.
* **silent − bare** is the unchanged headline, shown beside the other two as "of which WADM". Each of
  the three is its own median of paired values. Within one replicate
  `silent − origin = (bare − origin) + (silent − bare)` holds exactly, but the medians across
  replicates need not add up.
* **The relative figures depend on the origin.** The origin is static nginx serving HTML files of
  864–1,396 B, so its own latency is a fraction of a millisecond, and any fixed cost is a large
  percentage of it. The same absolute cost in front of an application that takes tens of
  milliseconds would be a few percent. Quote the absolute µs as the result and the percentage as
  "relative to this origin".
* **Client connections terminate somewhere different.** In the origin tier, k6's keep-alive
  connections end at the origin's nginx, which runs its default policy (`keepalive_timeout 75s`,
  `keepalive_requests 1000`; `backend/nginx.conf` sets neither). In the proxied tiers they end at the
  edge, under that edge's client-side keep-alive settings. Any difference in reconnects is part of
  bare − origin. `new_conn_<probe>` is recorded for every tier, so it can be checked rather than
  assumed.
* **The SQLi POSTs are sent but not reported.** They stay in the origin tier's iteration so that
  every tier offers the same load, but against the origin they are 404s for a route that does not
  exist. `paired_summary.json` holds their `* − origin` differences (the proxy's cost of forwarding a
  404), and no figure or table draws them.

### 9.3 What has to be re-measured

**The main paired dataset, all four edges, all four tiers: yes.** A difference is trustworthy here
because both members of a pair are measured minutes apart in the same replicate. Origin cells run now
cannot be paired with the bare and silent cells of the 2026-10-04 suite. A cross-session difference
is exactly the failure of §1.3: session drift of 100–500 µs, set against a proxy cost that the
retired unpaired runs put at roughly 0.3–0.7 ms at 1 and 10 VUs. The drift would be a large, unknown
fraction of the result. `merge_replicates.py` does not help either: it appends whole replicates, and never
forms a pair across sessions.

The three-tier data is not *wrong*. Its silent − bare is a valid paired measurement. But once the
four-tier run exists, the thesis should quote one dataset throughout, and every number drawn from
`paired_<edge>.json` will be re-derived from the new one: §8.1–8.3, the timer and cost-attribution
figures, the by-rate figures, and the "all" side of the per-mechanism CPU split. The new values will
differ from the old by about the spread of their replicates. `run_paired_suite.sh` (~6.5 h) copies the
three-tier files and figures to `archive/<date>-pre-origin-tier/` first, so the old numbers stay
traceable.

**Everything else: no.**

| Dataset | Re-measure? | Why |
|---|---|---|
| `functional_matrix.json` | No | A functional check, WADM vs bare per response shape. No timing and no origin tier. |
| `sqli-hit_` / `sqli-miss_` | No | Excluded by design. A self-contained paired run whose question (the trap against the same edge proxying the POST) has no origin-side counterpart. |
| `get_` / `size_` mixes | No | Self-contained paired runs with no origin tier. `plot_mechanisms.py` subtracts the GET-only run from the main one, but each side is a within-replicate difference before they are combined, and the two were already measured hours apart in the 2026-10-04 suite. Re-run the GET-only mix (~45 min) only if the per-mechanism CPU split is quoted and both sides should come from one session. |
| `paired_apache_lua_conn` | No (optional, ~45 min) | Its own paired run. `plot_remediation.py` sets its paired delta beside `apache_lua`'s, two within-replicate differences from different runs, as before. |
| `capacity_*` | No | A separate runner with no origin tier, and its own caveats (§6.5). |

### 9.4 Where the results appear

* `analyze_paired.py` adds two comparisons per rate, `bare_minus_origin` and `silent_minus_origin`,
  with the same per-probe, per-GET, quantile and relative statistics as every other comparison, but
  no CPU block, since the origin tier has no edge. It also adds `latency_levels_ms`, each tier's
  absolute median latency per GET. The console report prints an "Above the origin floor" line per
  rate.
* `plot_origin_floor.py` writes `plots/origin_floor/<rate>_per_s.png` (per edge, the latency added per
  GET above the origin by the bare proxy and by proxy + WADM, with "of which WADM"),
  `plots/origin_floor/tier_comparison.png` (origin vs bare proxy vs proxy + WADM as absolute
  latency per GET, grouped by edge, one panel per rate), `plots/origin_floor/latency_levels.png`
  (the same levels as a dot plot, descriptive), and
  `origin_floor.md` / `.csv` (per GET and per GET probe). The table also gives WADM's share of the
  latency the deployed stack adds above the origin, (silent − bare) ÷ (silent − origin), taken within
  each replicate and then summarised. A ratio of two medians would mix replicates.
* "Per GET" is the mean of the four GET probes' medians, differenced within each replicate, the same
  definition as `paired_headline.md`'s "Δ p50 per GET".

### 9.5 Verification (smoke run, 2026-10-13)

`run_paired_benchmark.py --preset smoke --all` with four tiers, into a scratch directory: one
replicate, 20 s windows, rates 1/10/100, all four edges, on an untuned guest. It took 40 minutes.

* **47 of 48 cells valid; all 12 origin cells valid.** Every origin cell met its offered rate with
  every response intact. The one rejection was Apache's instrumented tier at 100/s (0.54 % failed
  requests and a preemption tail), the mod_lua crash of §4b, unrelated to the origin tier.
* **The four floors agree.** Origin latency per GET was 508–561 µs at 1/s, 522–560 µs at 10/s and
  341–350 µs at 100/s across the four edges' runs. That is the cross-session consistency the paired
  design relies on.
* **The whole pipeline runs on four-tier data:** `analyze_paired.py`, `plot_paired_overhead.py`
  (the origin tier's functional row shows no bait or alert check, as intended), `plot_by_rate.py`,
  `plot_cost_attribution.py` and `plot_origin_floor.py`.
* **The three-tier analysis is unchanged.** Re-running `analyze_paired.py` on the committed
  2026-10-04 files reproduces the committed `paired_summary.json` exactly, apart from the two keys
  added (`latency_delta_pct_per_get`, `latency_levels_ms`).

A single replicate gives point estimates without an interval. Its bare − origin values
(+0.21 to +0.58 ms per GET) only show that the effect is in the range §9.3 expected, and are not
results.

### 9.6 Results of the four-tier run (2026-10-14/15)

`paired_suite_20261014T203839Z` (`run_paired_suite.sh`), guest tuned, 20:38–02:51 UTC, every step
OK. The kernel log shows no soft lockup or clock jump inside the window. The three-tier dataset it
replaced is in `archive/2026-10-14-pre-origin-tier/`.

**Validity.** 236 of 240 cells were valid: OpenResty, WASM and Envoy+Lua 60/60, Apache 56/60. The four
rejections are all Apache's instrumented tier at 100/s, on the preemption-tail gate (one also dropped
1.7 % of iterations). Every origin, bare and silent cell is valid, so every origin-floor and headline
figure rests on 5 paired replicates. Apache's in-edge timers at 100/s rest on a single replicate.

**The origin floor** (`origin_floor.md`, median per GET, 5/5 replicates agree in sign everywhere):

| Edge | Bare proxy − origin (1 / 10 / 100 per s) | Proxy + WADM − origin | of which WADM | WADM share |
|---|---|---|---|---|
| OpenResty | +260 / +255 / +184 µs | +409 / +388 / +294 µs | +145 / +114 / +111 µs | 35 / 31 / 37 % |
| WASM | +372 / +369 / +319 | +500 / +484 / +442 | +150 / +110 / +121 | 29 / 23 / 28 % |
| Apache+Lua | +324 / +372 / +279 | +545 / +553 / +580 | +233 / +189 / +295 | 42 / 33 / 51 % |
| Envoy+Lua | +396 / +389 / +309 | +571 / +557 / +455 | +203 / +170 / +147 | 34 / 30 / 32 % |

The origin itself answered a GET in 516–552 µs at 1 and 10/s and 346–350 µs at 100/s, and the four
edges' floors agree within 36 µs at every rate. The proxy without WADM adds 184–396 µs per GET;
WADM adds 110–295 µs on top. Outside Apache at 100/s, WADM is 23–42 % of what the deployed stack adds
above the origin, and the proxy is the rest.

**Reproducibility against the three-tier run.** The silent − bare headline moved by less than the
replicate spread in most cells (for example OpenResty +114/+111 µs at 10/100 per s in both runs,
Envoy+Lua +208→+203 and +163→+170). Two quantities did not reproduce, and the thesis must not quote
them as point values:

* **Apache's median at 100/s.** It was +2,174 µs in the three-tier run (per-replicate +605, +26,332,
  +2,658, +1,691) and is +295 µs now (+235, +295, +544, +509, +291), with the same crash count
  (12–16 segfaults per silent run in both). Crash-recovery stalls sit in the tail of the
  distribution, and whether they reach the median depends on the run. The tail did reproduce: the
  p90 added per GET is +1.5 to +78 ms now and was +89 to +229 ms before. The reportable statement is
  "the latency tail grows by tens of milliseconds and the median by 0.2–26 ms depending on the run",
  not a single median.
* **Apache's marginal CPU.** The fit of CPU against requests is rejected as unphysical for both WADM
  tiers in all five replicates (the unconstrained fit needs an idle cost of −6,600 µs/s). WADM-tier
  CPU per request *rises* at 100/s (722 µs, against 430 µs at 10/s) because respawning crashed workers
  costs CPU, so a straight line through the three rates does not describe it. The bare tier fits
  cleanly at 226 µs/request in every replicate. The earlier "+200 µs" came from a run in which the
  same model happened to stay physical. Quote Apache's CPU added per rate (+295, +129, +495 µs/request
  at 1, 10 and 100/s) instead. Marginal CPU for the other three: OpenResty +31 (was +33), WASM +108
  (+123), Envoy+Lua +45 (+41) µs/request.

## 10. The in-server deployment (2026-10-18)

Every variant up to §9 runs the module in a **reverse proxy** in front of the `backend` origin.
OpenResty (nginx) and Apache httpd are web servers that act as reverse proxies only through their
proxy modules, and their Lua hooks (lua-nginx-module's phase handlers; mod_lua's hooks and output
filters) run on the responses the server produces itself, files served from disk included. So the
same module can run **inside the server that serves the pages**, with no proxy hop. Two variants
measure that deployment:

| Key | Service, port | Serves | Module code |
|---|---|---|---|
| `openresty_inserver` | `openresty-inserver`, 8084 | `backend/www` at `/srv/www` | `nginx/nginx-inserver.conf`: `nginx.conf`'s Lua block verbatim, machine-checked |
| `apache_inserver` | `apache-inserver`, 8085 | `backend/www` at `/srv/www` | `httpd-inserver.conf` + the shared `apache_scripts/` |

Envoy has no in-server variant: it is a dedicated proxy, with no file-serving handler to run the
module over. The four proxy variants, their configs and the 2026-10-15 dataset are unchanged; the
in-server keys sit outside `EDGE_KEYS`, so `--all` and the four-way figures do not include them.
`docs/EDGE_LEVELING.md` ("In-server deployment") records how the variants were levelled.

### 10.1 What the tiers mean in-server

| Tier | Proxy deployment | In-server deployment |
|---|---|---|
| `origin` | k6 → `backend` (nginx:alpine), edge idle | identical: k6 → the shared `backend`, in-server container idle |
| `bare` | the proxy without the module, forwarding to `backend` | OpenResty / Apache serving `backend/www` itself, without the module |
| `silent` / `wadm` | proxy + module | server + module, serving its own pages |

* **silent − bare** is the module's cost in both deployments: the same server, with and without the
  same module code.
* **bare − origin** changes meaning. In the proxy deployment it is the proxy hop (§9.2). In-server
  there is no hop: it is **OpenResty or Apache serving files, against nginx:alpine serving the same
  files** (the "server swap"). `analyze_paired.py` labels it `bare server - origin` for in-server
  keys.
* **silent − origin** is the deployed stack in both deployments: proxy + module + origin, or server
  + module. In-server it contains the server swap, so a difference between the deployments in
  silent − origin is partly a difference between the file servers. That confound is largest for
  Apache, whose file serving is not nginx's. `compare_deployments.py` prints bare − origin beside
  silent − origin so the two can be read together.
* `backend` stays up through `depends_on` on every tier, because the origin tier and the cgroup
  sampler use it. On the in-server edge tiers it serves nothing, and its CPU per request should be
  about zero. `compare_deployments.py` prints that as a sanity row: it shows that nothing was
  proxied.

### 10.2 Comparing the deployments: paired differences from separate sessions

The proxy edge and its in-server counterpart are measured in **different sessions**: the proxy
edges in `paired_suite_20261014T203839Z`, the in-server variants in `run_inserver_suite.sh`. Each
deployment's differences are paired inside its own session. A comparison of the deployments is
therefore a comparison of **two paired differences**, each the median of its replicates with its
replicate range. It is never a subtraction of raw latencies taken in different sessions, which §1.3
showed is dominated by session drift (100–500 µs). `compare_deployments.py` reports whether the two
replicate ranges overlap. Non-overlapping ranges mean the deployments differ by more than either
session's replicate spread. Overlapping ranges mean this harness cannot order them. It does not
print a difference of the two medians.

### 10.3 CPU allocation, and why capacity is not compared

Both deployments use the same cpusets (`EDGE_CPUS` 1-2, `BACKEND_CPUS` 3, `LOADGEN_CPUS` 4-7). In the
proxy deployment the request's server-side work is spread over **three** cores: the proxy on 1-2,
the origin on 3. In-server, all of it (request handling, the module, and reading and sending the
file) runs on the **two** `EDGE_CPUS` cores, while the origin's core sits idle. At the measured rates
neither deployment is near saturation, so latency and CPU per request are comparable. **Capacity is
not**: an in-server ladder would compare two cores against three. No in-server capacity run was made.

The worker counts are inherited unchanged, because the images are the same: OpenResty (glibc)
still starts 8 workers on its 2 pinned cores (§4.4b), and Apache (musl) uses its compiled event-MPM
defaults. Both cancel within a deployment's paired difference, as they do for the proxy variants.

### 10.4 Stack CPU

For silent − origin and bare − origin, the CPU of a single container no longer means the same thing
in the two deployments: the proxy deployment splits the work between the proxy and the origin, while
in-server it is all in one container. `compare_deployments.py` therefore reports **stack CPU**: host
container + origin container, each integrated over the measured window and divided by k6's
`edge_requests`. On the origin tier only the origin serves, and the idle host container is not
counted, as in `analyze_paired.py`. silent − bare keeps the host container's CPU alone, because there
the origin is the same in both tiers.

### 10.5 Measured functional differences in the in-server deployment

From `functional_matrix.md` (2026-10-18; each variant against its own bare tier) and manual probes.
The four proxy columns are identical, verdicts and reasons, to the 2026-10-04 matrix in
`archive/2026-10-18-pre-inserver/`.

| Case | OpenResty proxy | OpenResty in-server | Apache proxy | Apache in-server |
|---|---|---|---|---|
| HTML, gzip-encoded | BROKEN (tokens spliced into compressed bytes) | injected (decodes) | BROKEN | injected (decodes) |
| HEAD | changed (Content-Length dropped) | changed (same) | BROKEN (HEAD length ≠ GET length) | preserved (HEAD advertises the injected length) |
| Range `bytes=0-99` | BROKEN (206, modified partial body) | BROKEN by the matrix's rule: 206 → 200, full injected page | BROKEN | preserved (206 of the injected page; `Content-Range: bytes 0-99/975`) |
| 204 | changed (bait header) | changed (bait header) | changed | preserved (answered before the access hook) |
| 404 page | injected | injected | injected | preserved: bait header and cookie, **no body tokens** |

Two of these need care in the write-up:

* **Apache does not inject into its own error pages.** `ap_send_error_response`
  (`modules/http/http_protocol.c`) resets `r->output_filters` to the protocol filters, "to get rid of
  any RESOURCE filters", and mod_lua registers `LuaOutputFilter` as `AP_FTYPE_RESOURCE`
  (`modules/lua/mod_lua.c`). Headers staged in `err_headers_out` survive. In the proxy deployment
  the 404 came from the origin as an ordinary proxied response and was injected. Two of the four
  benchmark GETs (`detect_query`, `token_decoy`) are 404s, so Apache in-server injects into **three**
  of the five HTML responses per iteration rather than five. Its wadm-tier html_comments and
  decoy_paths inject coverage is therefore 3/5 by construction, its `BAIT MISSING` lines for those
  two probes are expected, and its silent − bare covers less body-injection work than the proxy
  variant's. `compare_deployments.py` prints the coverage beside the costs for this reason.
* **OpenResty's range answer is valid HTTP.** The matrix classifies any status change as BROKEN, but
  a server may ignore `Range` and send the full representation with 200 (RFC 9110 §14.2). What
  changed is that the client gets the whole injected page instead of the requested part.

### 10.6 Verification (2026-10-18)

* Config syntax: `openresty -t` and `httpd -t` pass for both tiers of both variants. nginx warns
  `duplicate MIME type "text/html"` for the gz location's `gzip_types text/html`, which is
  harmless (text/html is always included) and mirrors `backend/nginx.conf`.
* Manual probes, WADM tier: `/` returns 200 with the comment token, `X-Backend-Server` and the
  `admin_ui` cookie. One trigger request to `/` or `/about.html` raises **exactly one** `WADM ALERT`,
  so the `/` mapping does not re-run the access phase. The decoy path returns 404 with one ALERT.
  The SQLi hit returns 500 with the fake MySQL error and an ALERT, and a clean login returns 401
  with a TRAP. The gzip page decodes. `/test/empty` returns 204. Bare tier: same pages, no tokens,
  no WADM lines; `/` is byte-identical to `index.html`.
* `parity_check.py` (`results/parity_check_2026-10-18.txt`): the Lua-sync check passes (754
  lines); Envoy+Lua, Envoy+WASM and Apache are identical to OpenResty as before; both in-server
  variants are identical under the in-server rules, with the same 12 alert/trap lines and **no**
  origin requests.
* No file the proxy variants depend on changed (`git diff` on `nginx/nginx.conf`,
  `nginx/nginx-baseline.conf`, `httpd*.conf`, `envoy*`, `wasm-filter/`, `apache_scripts/`,
  `config.json`, `benchmarks/test.js`, `backend/` is empty). `analyze_paired.py`, after the
  in-server changes, reproduces the archived `paired_summary.json` byte for byte from the unchanged
  `paired_<edge>.json` files (the bootstrap is seeded).
* Images (not re-pulled; the same local images as the proxy variants):
  `openresty/openresty:latest` `sha256:12c49073…` (2026-02-13), `httpd:2.4-alpine` `sha256:c9e51ef5…`
  (Apache 2.4.67, 2026-05-05), `nginx:alpine` `sha256:d0c78077…`, `grafana/k6:latest` `sha256:9adaede0…`.
* The `/` mapping, tested both ways. With an `index`-based copy of the OpenResty in-server config,
  one trigger to `/` raised **two** alerts (the internal redirect re-runs the access phase). With
  mod_dir resolving `/` in a copy of the Apache in-server config, it raised **one**, and
  `detect.lua` ran once. The shipped mappings raise one on both; Apache's `AliasMatch` is kept for
  symmetry, not because Apache needs it.
* Harness smoke (`--preset smoke`, scratch results, `openresty_inserver`, `apache_inserver`,
  `openresty`) on an **untuned, busy guest** (THP `[always]`, swap on, load 7.5–10; the origin
  control burned 280–1,520 µs/request against 153–287 in the tuned 2026-10-15 run, so every smoke
  CPU figure is inflated 2–6×). 8/12, 7/12 and 9/12 cells valid; every rejection is at 100/s or a
  host-contention gate. Self-checks hold: 7 ALERT + 2 TRAP per iteration on silent/wadm, 0 on bare;
  timing lines only on wadm; origin CPU 0.0 on every in-server edge tier; no crash indicator;
  coverage complete except Apache in-server's html_comments/decoy_paths inject at 0.600 (§10.5).
  **Apache in-server's WADM tiers collapsed at 100/s** (19–20 % of the offered rate, 500 VUs, bare
  at 100 %). This was one contended replicate. **The full run on the tuned guest sustained 100/s on
  every Apache in-server tier (3,000 iterations, 100 % of offered; §10.7), so the collapse was host
  contention**, not a property of the variant.

### 10.7 Results of the in-server run (2026-10-18/19)

`inserver_suite_20261018T214050Z` (`run_inserver_suite.sh`), guest tuned (THP never, swap off,
load ≈ 1 at start), 21:40–00:47 UTC, every step OK. No soft lockup or clock jump in the kernel log
and no host stall in any cell. **120 of 120 cells valid**: 5/5 replicates in every tier and rate of
both variants. Apache in-server's workers segfaulted 9–12 times per WADM run at 100/s and 2–3 times
in three WADM runs at 10/s, never on bare; the gate kept those cells with a warning, as it does for
proxy Apache. `analyze_paired.py` reproduced all five earlier entries of `paired_summary.json` byte
for byte. On every in-server edge tier the origin's CPU was 0.00–0.61 µs/request: nothing was
proxied.

From `deployment_comparison.md` (median per GET of the paired differences, replicate range in
brackets; proxy figures are the 2026-10-15 run):

| | 1 / 10 / 100 per s | proxy | in-server | ranges overlap? |
|---|---|---|---|---|
| OpenResty | silent − bare, latency | +145 / +114 / +111 µs | +69 / +81 / +72 µs | no / no / no |
| | silent − bare, CPU | +61 / +31 / +31 µs/req | +26 / +19 / +32 µs/req | yes / yes / yes |
| | silent − origin, latency | +409 / +388 / +294 µs | +12 (spans 0) / +29 / +82 µs | no / no / no |
| | bare − origin, latency | +260 / +255 / +184 (proxy hop) | −58 / −49 / +10 (server swap) | no / no / no |
| | silent − origin, stack CPU | +203 / +113 / +114 µs/req | −0.6 (spans 0) / −28 / +21 µs/req | no / no / no |
| Apache | silent − bare, latency | +233 / +189 / +295 µs | +244 / +198 / +183 µs | yes / yes / no |
| | silent − bare, p95 at 100/s | +83,946 µs | +1,491 µs | no |
| | silent − bare, CPU | +295 / +129 / +495 µs/req | +279 / +183 / +260 µs/req | yes / yes / yes |
| | silent − origin, latency | +545 / +553 / +580 µs | +286 / +271 / +224 µs | no / no / no |
| | bare − origin, latency | +324 / +372 / +279 (proxy hop) | +46 / +71 / +35 (server swap) | no / no / no |
| | silent − origin, stack CPU | +484 / +294 / +812 µs/req | +360 / +276 / +290 µs/req | yes / yes / no |

Read with these caveats:

* Apache proxy's 100/s **median** is not stable (+2,174 µs and +295 µs in two runs, §9.6), so the
  100/s comparison for Apache rests on the tail.
* Apache in-server's wadm-tier html_comments/decoy_paths inject coverage is 0.60 at every rate
  (§10.5), so its silent − bare covers less body-injection work than the proxy variant's.
  OpenResty's coverage is complete in both deployments.
* In-server silent − origin includes the server swap (bare − origin in-server), which for Apache
  is +35–71 µs at the median. That is Apache serving files rather than nginx, not a cost of HDM.
* The two sessions' origin floors agree: at 10/s the origin answered a GET in 0.551 ms in both
  in-server sessions and in 0.543/0.545 ms in the OpenResty/Apache proxy sessions. The comparison
  still uses only the paired differences.
* The measurements do not explain why the same OpenResty code adds less latency in-server (the
  ranges do not overlap; their medians are not subtracted across sessions). No mechanism is claimed.

Functional checks under load (paired cells): OpenResty in-server is 100 % intact with recall 1.0 at
every rate. Apache in-server's worst intact share is 99.78 % (10/s) and 99.63 % (100/s), with alert
recall ≥ 0.9978 and trap recall ≥ 0.998 (proxy Apache: 99.36–99.78 %, 0.9979–0.9997). Its
`BAIT MISSING` on `detect_query` and `token_decoy` is the error-page residual.

### 10.8 The thesis report's scope, and the parity reference (2026-10-19)

The thesis report covers four variants: **Envoy+Lua and Envoy+WASM (proxy deployment), OpenResty
in-server and Apache in-server**. The OpenResty and Apache proxy variants stay in the repository,
with their data, but the report no longer includes them. Two consequences:

* `parity_check.py` now uses **OpenResty in-server** as its reference, so the report needs no proxy
  variant of OpenResty. Responses and alerts are compared against it (body hash only where it
  answered 200). Origin traffic cannot be compared against an in-server reference, so the in-server
  variants must send nothing, and the proxy variants must forward the same requests as each other
  with no `html_comments` keyword left in any forwarded query. Result
  (`results/parity_check_2026-10-19.txt`): all four identical; 11 ALERT + 1 TRAP lines each; both
  Envoy variants forwarded the same 11 requests, none carrying a keyword; the in-server variants
  sent none. `--all` adds the two proxy variants.
* The capacity experiment (§7.4) is not part of the report: in-server variants run on two cores,
  Envoy on two plus the origin's one (§10.3). The GET-only and page-size runs (§7.5) exist for the
  Envoy variants from 2026-10-05 and are re-run for the in-server variants. The SQLi hit/miss
  experiment (§7.5) covers only proxy variants; the report answers its question from the main
  run's per-arm scan timers, which exist for all four variants.

