# structure_proposals

Alternative chapter structures for the thesis, each written as a skeleton in the department's
own `rptuma` class. Every PDF's table of contents *is* the proposed structure. Under each heading is
a short note on what to write there, and a grey **Material** line pointing to where the repository
already holds that content.

| File | Organising idea | Choose it if |
|---|---|---|
| `template_A_classic_systems.pdf` | Design → Implementation → Evaluation (the standard CS thesis) | WADM itself is the main contribution |
| `template_B_research_questions.pdf` | One chapter per research question (RQ1 latency, RQ2 CPU/capacity, RQ3 functionality, RQ4 runtime), with categorised threats to validity | the measured answers are the main contribution |
| `template_C_design_science.pdf` | *(rejected)* Design science with two artefacts: WADM, and the fair cross-edge evaluation method (levelling, paired harness, gates) | the levelling and methodology work should count as a contribution |
| `template_AB_combined.pdf` | Template A's chapters, with B's research questions as the evaluation's backbone: RQs stated in the introduction, metrics mapped to them, Results split into one section per RQ ending in its answer | A's structure with B's per-RQ results |
| `template_final.pdf` | **Current working structure.** The A+B template with sections trimmed (2.4, 2.5, 3.5, 4.3, 4.7, 6.5, 6.7, 7.2.5, 7.3.3 of A+B removed) | the version to write the thesis in |

All of them keep the department template's front matter (title page, Eigenständigkeitserklärung,
Abstract, Zusammenfassung, ToC/LoF/LoT) and its IEEE bibliography style.

## Data flow

```
proposal_common.tex ─┐   (shared preamble: macros, title-page fields, front-matter note)
sources.bib ─────────┼─> build.sh ─> pdflatex/bibtex against ../rptuma.cls ─> template_*.pdf
template_*.tex ──────┘
```

`build.sh` compiles every `template_*.tex` and then deletes the intermediate files.

## Building

```sh
./build.sh
```

`rptuma.cls` loads babel's German, which a minimal TeX Live install lacks (`texlive-lang-german`).
When it is missing, `build.sh` builds against a temporary copy of the class with only that babel
option dropped. The proposals never switch to German or print the declaration, so the output is
unchanged. The real thesis should be built with the unmodified class, e.g. on Overleaf.

## Caveats

- Numbers quoted in the notes come from the 2026-10-04/05 paired suite and the 2026-10-07 SQLi run
  (`docs/BENCHMARK_METHODOLOGY.md` §8). Update them if the results are re-run.
- `sources.bib` lists the structural models and a starter set of related-work references. In the
  department template's own example, websites are cited through a `web.archive.org` copy; do the
  same for any web entry before citing it in the thesis.
- Page budgets in each proposal are rough estimates, not department rules.
