# Final_Report_Workspace

Self-contained LaTeX workspace for writing the final thesis report, seeded from
`../structure_proposals/template_final.tex`.

## Contents
- `template_final.tex` — main document (edit this).
- `proposal_common.tex` — shared preamble `\input` by the main document.
- `sources.bib` — bibliography (BibTeX, `ieeetr` style).
- `rptuma.cls` — RPTU department thesis class.
- `img/` — logos and figures; the class loads `img/RPTUlogo`.
- `build.sh` — runs pdflatex → bibtex → pdflatex ×2 and cleans intermediates.

## Data flow
`template_final.tex` + `proposal_common.tex` + `rptuma.cls` + `img/` + `sources.bib`
→ `./build.sh` → `template_final.pdf`.
