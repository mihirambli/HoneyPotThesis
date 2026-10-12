#!/bin/sh
# Builds every proposal against the department class one directory up, then removes the
# intermediate files so only the .tex sources and PDFs remain.
set -e
cd "$(dirname "$0")"

classdir=".."
shim=""
# rptuma.cls loads babel's German, which minimal TeX Live installs lack. The proposals never
# switch to German or print the declaration (the only German text), so building against a copy
# with that one option dropped renders identically. The real thesis should build unpatched.
if ! kpsewhich german.ldf >/dev/null 2>&1; then
    shim="$(mktemp -d)"
    sed 's/\[german,english\]{babel}/[english]{babel}/' ../rptuma.cls > "$shim/rptuma.cls"
    classdir="$shim"
    echo "note: babel German not installed; building with a temporary English-only copy of rptuma.cls"
fi
export TEXINPUTS=".:$classdir:"

for doc in template_*.tex; do
    name="${doc%.tex}"
    pdflatex -interaction=nonstopmode -halt-on-error "$name" >/dev/null
    bibtex "$name" >/dev/null
    pdflatex -interaction=nonstopmode -halt-on-error "$name" >/dev/null
    pdflatex -interaction=nonstopmode -halt-on-error "$name" >/dev/null
    echo "built $name.pdf"
done

rm -f -- *.aux *.bbl *.blg *.log *.out *.toc *.lof *.lot *.idx *.ilg *.ind
[ -n "$shim" ] && rm -rf "$shim"
exit 0
