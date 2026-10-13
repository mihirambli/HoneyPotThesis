#!/bin/sh
# Builds the final report against the local rptuma.cls, then removes intermediate files.
set -e
cd "$(dirname "$0")"

classdir="."
shim=""
# rptuma.cls loads babel's German, which minimal TeX Live installs lack. Building against a copy
# with that option dropped renders identically as long as the German declaration is not printed.
if ! kpsewhich german.ldf >/dev/null 2>&1; then
    shim="$(mktemp -d)"
    sed 's/\[german,english\]{babel}/[english]{babel}/' rptuma.cls > "$shim/rptuma.cls"
    classdir="$shim"
    echo "note: babel German not installed; building with a temporary English-only copy of rptuma.cls"
fi
export TEXINPUTS="$classdir:.:"

name=template_final
pdflatex -interaction=nonstopmode -halt-on-error "$name" >/dev/null
bibtex "$name" >/dev/null
pdflatex -interaction=nonstopmode -halt-on-error "$name" >/dev/null
pdflatex -interaction=nonstopmode -halt-on-error "$name" >/dev/null
echo "built $name.pdf"

rm -f -- *.aux *.bbl *.blg *.log *.out *.toc *.lof *.lot *.idx *.ilg *.ind
[ -n "$shim" ] && rm -rf "$shim"
exit 0
