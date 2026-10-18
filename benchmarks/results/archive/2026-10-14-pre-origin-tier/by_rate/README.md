<!-- benchmarks/results/plots/by_rate/README.md: the paired results split into one-measure, one-rate figures. -->
# by_rate — one measure per figure, one folder per offered rate

Written by `benchmarks/plot_by_rate.py` from `results/paired_<edge>.json` (via `analyze_paired.py`).
Regenerate with:

```bash
python3 benchmarks/plot_by_rate.py
```

Each subfolder is one offered load, `<rate>_per_s/` = `<rate>` k6 iterations started per second. One
iteration is one pass through `test.js`: an unrecorded priming `GET /`, then the four GET probes and the
four SQLi POSTs in a rotated order, so 9 requests. 1, 10 and 100 iterations/s are therefore about
9, 90 and 900 requests/s at the edge.

Every folder holds the same seven figures, each comparing the four edges on a single measure:

| File | Measure | Plane |
|---|---|---|
| `0_inject_vs_detect.png` | inject vs detect per edge, averaged over the five token kinds, one µs scale shared by all rates | in-edge timer, µs |
| `1_injection_timers.png` | inject region per token kind | in-edge timer, µs |
| `2_detection_timers.png` | detect region per token kind (no SQLi) | in-edge timer, µs |
| `3_sqli_scan_timers.png` | SQLi signature scan: hit/miss × plain/%-encoded | in-edge timer, µs |
| `4_cpu_per_request.png` | edge CPU per request, bare vs WADM as deployed | cgroup CPU, µs |
| `5_latency_added_get.png` | latency WADM adds to each GET probe | client wall clock, µs |
| `6_latency_added_sqli.png` | latency WADM adds to each SQLi POST probe | client wall clock, µs |

SQLi is kept apart from detection because its scan is several times longer than any other detect
region and, end to end, its sign is reversed (the trap answers without the origin round-trip).

They replace nothing: `paired_internal_timers.png` and `paired_latency_added.png` in the parent folder
still show every rate side by side. Use those to see a trend across rates, these to compare edges.

## How `0_inject_vs_detect.png` merges the token kinds

Each kind's samples are reduced to a 5%-trimmed mean first, then the five kinds are averaged with
equal weight, so a bar reads as "one token operation on this edge". Equal weight rather than pooling
the raw samples because `test.js` injects form_fields on one GET in five but the other kinds on every
GET; a pooled mean would mostly describe whichever kinds the probe mix repeats. SQLi is excluded and
has its own figure.

Why the trimmed mean, and why no box plot: the edges log whole microseconds and most regions take
1-3 µs, so a region's samples sit on two or three integer values. Quartiles then coincide (Q1 = median
= Q3 for several kinds), boxes collapse to a line, edges with different true costs share the same
median, and the 1.5 IQR rule turns every host-preemption sample (up to ~2 ms) into one of thousands of
fliers. Averaging integer readings recovers sub-µs resolution (unbiased on the edges that subtract two
truncated timestamps; WASM truncates the difference and reads ≈0.5 µs low), and trimming drops the
preemption tail that pulls a plain mean up.
