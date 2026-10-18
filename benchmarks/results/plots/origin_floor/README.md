<!-- benchmarks/results/plots/origin_floor/README.md: latency the bare proxy and the proxy with WADM add above the no-proxy origin. -->
# origin_floor — the proxy and WADM against the no-proxy floor

Written by `benchmarks/plot_origin_floor.py` from `results/paired_<edge>.json`, using
`analyze_paired.py`'s statistics. The table version is `results/origin_floor.md` / `.csv`, which
also gives WADM's share of the latency the deployed stack adds above the origin, computed within
each replicate.
Regenerate with:

```bash
python3 benchmarks/plot_origin_floor.py
```

The figures need a dataset with the `origin` tier, which `run_paired_suite.sh` (or
`run_paired_benchmark.py --preset full`) records for the reportable `all` mix. An edge whose file
predates the origin tier is skipped with a message.

| File | Measure | Paired? |
|---|---|---|
| `<rate>_per_s.png` | per edge: latency added per GET above the origin, by the bare proxy (grey) and by the proxy with WADM as deployed (edge colour), plus the paired WADM − bare difference ("of which WADM") | yes, within each replicate |
| `tier_comparison.png` | the three-way comparison: per rate and edge, absolute median latency per GET with the origin answering directly, through the bare proxy (WADM off), and through the proxy with WADM, as grouped bars on one axis | no, descriptive (the paired gaps are in `<rate>_per_s.png`) |
| `latency_levels.png` | absolute median latency per GET of each tier (origin, bare, WADM), one panel per rate | no, descriptive |

## Data flow

    run_paired_benchmark.py (all mix: origin, bare, silent, wadm tiers in every replicate)
      -> results/paired_<edge>.json
      -> plot_origin_floor.py  (analyze_paired: paired_get_mean, paired_get_relative, latency_levels)
           -> plots/origin_floor/*.png
           -> results/origin_floor.md, results/origin_floor.csv

## Reading them

- **Per GET** is the mean of the four GET probes' median latencies, differenced within each replicate:
  the same definition as `paired_headline.md`'s "Δ p50 per GET". Bar = median of the paired
  differences over replicates; whisker = their range; a hollow bar means the replicates disagree in
  sign, or that there are fewer than 3 of them (a smoke run).
- **"Of which WADM"** is its own paired median (WADM − bare), so it need not equal the gap between
  the two bars, although within any single replicate the three differences add up exactly.
- **WADM** is the deployed build (the `silent` tier, no benchmark timing lines).
- **Only the GETs are drawn.** The SQLi POSTs are sent in the origin tier too, so every tier offers
  the same load, but against the origin they are 404s, not anything WADM does.
- **Percentages are relative to this origin**, a static nginx answering in a fraction of a
  millisecond. Quote the µs.
- In `latency_levels.png` the four origin dots (one per edge run) should coincide. How far apart they
  are is how far the floor moved between the edges' sessions, which is why the comparison is made
  only within a replicate.

Design, caveats and the re-measurement decision: `docs/BENCHMARK_METHODOLOGY.md` §9.
