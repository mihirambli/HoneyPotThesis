# `Final_Report_Workspace/figures/`

The result figures of the thesis report (Chapter 7), as vector PDFs included with
`\includegraphics`. They are copies: the source of truth is `benchmarks/plot_report.py`, which
draws them from `benchmarks/results/` into `benchmarks/results/plots/report/`.

Data flow: `benchmarks/results/*.json` → `plot_report.py` → `results/plots/report/*.pdf` → copied
here → `template_final.tex`. After re-running the benchmarks, regenerate and copy again:

    python3 benchmarks/plot_report.py
    cp benchmarks/results/plots/report/*.pdf master_thesis_template_disco/Final_Report_Workspace/figures/
