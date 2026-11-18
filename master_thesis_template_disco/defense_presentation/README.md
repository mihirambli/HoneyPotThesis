# defense_presentation

Material for the 20-minute thesis defence, meant to be copied to another machine and turned into a
slide deck there.

## Contents
- `DEFENSE_PRESENTATION_PLAN.md`: slide-by-slide plan (on-slide content, visual, speaker notes,
  timing), backup slides, Q&A preparation and the key numbers. Part 0 is the prompt for the tool that
  builds the deck.
- `figures/`: PNG images for the slides.
  - Result figures, rendered at 220 dpi from `../Final_Report_Workspace/figures/*.pdf`.
  - `diagram_*.png`: the report's TikZ diagrams (Figures 4.1, 4.2, 5.1, 6.1 and 6.2), cropped from
    `../Final_Report_Workspace/template_final.pdf` at 250 dpi.

## Data flow
`benchmarks/plot_report.py` → `Final_Report_Workspace/figures/*.pdf` → `mutool draw` →
`figures/*.png`; `template_final.pdf` → `mutool draw` + ImageMagick crop → `figures/diagram_*.png`.
The numbers in the plan are copied from `template_final.tex`. If the report changes, re-render the
figures and update the plan.
