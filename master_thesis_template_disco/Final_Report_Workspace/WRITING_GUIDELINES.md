# Writing guidelines for the thesis report

Derived on 2026-10-26 from a full read of `template_final.tex` (Abstract to Chapter 7). They
describe how the finished chapters are written, so that the remaining chapters (Discussion,
Conclusion, appendices) and later revisions read as the same text.

## 1. Story and structure

- **The report is one story for a newcomer.** Each chapter starts where the previous one ended
  ("Chapter 5 made the four variants do the same work. This chapter describes how the cost of that
  work was measured."). Concepts are built up in order: Background introduces a term, Design uses
  it, Implementation realises it, Methodology measures it, and Results reports it.
- **Every chapter opens with a short roadmap paragraph.** It says what the chapter does and lists
  its sections with `\secref{...}` or `\secrefp{...}`.
- **Every section opens with its purpose.** First state what the section is about, then the
  problem, then the solution. The reader always knows why a section is there before reading the
  details (§5.7 is the model: purpose, why a check is needed, the check, its rules, the result).
- **Subsections fit their whole section.** Background holds general knowledge only: no
  implementation, method, results or directive names.
- **Results follow the research questions.** One section per RQ, subsections per finding, and a
  closing "Answer to RQx" subsection that answers each sub-question and tags it, e.g. "(RQ2.1)".
- **Link back instead of repeating.** When a later chapter relies on an earlier explanation, point
  to it with `(\secrefp{label})` and do not repeat the explanation; a one-clause reminder is enough.
- **No history.** Describe the current design, implementation and results in the present tense.
  Do not write about earlier versions, errors that were fixed or the order in which things were
  done. (The one exception is the existing paragraph on how the RQs arose in §1.3.)

## 2. Paragraphs and sentences

- **A paragraph begins with a topic sentence** that states its claim ("The reverse proxy alone adds
  more than HDM does."), followed by the evidence and, where needed, its interpretation.
- **Sentences are plain and precise.** One idea per sentence; no filler, no promotional words, no
  "it is important to note". Prefer concrete nouns: "the server running HDM", not "it".
- **No first person.** Use "this thesis", the variant names or the passive voice.
- **Tense.** Present tense for what the design and the code do and for what a figure shows; past
  tense for what was measured or observed ("crashed", "returned 99.78 % of its responses intact").
- **Hedge exactly as far as the evidence goes.** When a cause is not measured, say so: "these
  measurements do not show why", "was not measured for this variant". Never offer an explanation
  as fact without a source or a measurement behind it.
- **Use British spelling:** behaviour, utilisation, organisation, levelling, recognise.
- **Lists:** use `itemize` with an `\emph{label}` and a full sentence when items are parallel
  concepts; use `enumerate` for steps or rules that are referred to by number.

## 3. Terminology (fixed; do not vary)

- **HDM**, the HTTP Deception Module. Never write "WADM", which is a code name only.
- The two **deployments:** the *proxy deployment* and the *in-server deployment*.
- The four **variants**, always in this order: **Envoy Lua, Envoy WebAssembly, OpenResty, Apache**.
  The two Envoy variants are also called the *proxy variants*. Do not write "in-server variants" or
  "OpenResty in-server"; write "OpenResty and Apache".
- **Servers:**
  - the server whose module or filter runs HDM is *the server running HDM* or *the server under
    test*;
  - *server software* refers to Envoy, OpenResty or Apache httpd as a dimension of comparison;
  - *origin server* is nginx behind Envoy;
  - *reverse proxy* is used where a statement holds only in the proxy deployment.

  Never write "host server".
- **Apache:** Apache httpd is the server software, Apache is the variant, and "Apache httpd's Lua
  module" is the module, not `mod_lua`.
- **The SQL-injection trap:** always "SQL-injection trap", its log line the *SQL-injection trap
  line*, and the honeytoken log line the *alert line*. Never a bare "trap" or "trap line".
- **Honeytokens:** honeytoken *types*, not kinds. The detection signals are *alteration*,
  *resource request* and *data use*.
- **Tiers:** *origin*, *bare*, *silent* and *instrumented*. A cost is written "silent minus bare",
  never "silent − bare".
- **Results vocabulary:**
  - *replicate*, *cell*, *probe*, *iteration*, *timing line*, *timers inside HDM*;
  - *resolved* (all five differences have the same sign);
  - *marginal CPU time*;
  - *CPU utilisation* (in per cent, with additions in percentage points).
- **Injection:** HDM *injects* honeytokens (*injection*, not "insertion"). Extension code is
  *attached* to hooks, never "injected", which would read as an attack.
- **Experiments:** the *main benchmark* (not "paired benchmark"), the *functional matrix*, and the
  *request-type and page-size runs*. "Paired" describes the method only: paired differences,
  paired replicates.
- **Functional-matrix categories:** *preserved*, *injected*, *changed*, *broken*, *not exercised*.
- **Abbreviations** are spelled out at first use in the main text (and again in each abstract).

## 4. Numbers and claims

- **No fabrication.** Every number comes from a result file in `benchmarks/results/`, mostly via
  `plots/report/report_values.md`, and is copied, never estimated. Every external claim carries a
  `\cite{}` to a source that was checked to say it. Web sources need a Wayback Machine URL.
- **Format a result as the median of the five paired differences with its range:** "45 (39 to
  50)\us" in text, "120 to 177\us" for a range across load levels. Write `\us`, `\,\%` and
  "percentage points".
- **Order variants only when their ranges do not overlap** (Hoefler and Belli, §2.8). When ranges
  overlap, say that the variants are "not ordered". When a range includes zero, call the result
  unresolved and do not argue from its median.
- **Every cost is taken against the bare tier.** A comparison between variants is a comparison of
  paired differences from separate runs, never of raw latencies.
- **State the scope:** values describe this testbed. The comparison between the variants is the
  claim; absolute values do not transfer to other hardware.
- **Method chapters contain no result numbers**, except the worked examples the author asked for
  (§6.4).

## 5. Figures, tables and LaTeX

- **Captions are one sentence** that names what is shown and the load level.
- **Tables:**
  - use `booktabs` (`\toprule`, `\midrule`, `\bottomrule`) and `\addlinespace` between rows;
  - write ranges as "1.22 to 1.30";
  - put units in the column heading.
- **Each figure or table sits in the subsection that discusses it.** Use `[!htbp]`, and a
  `\FloatBarrier` before the next subsection if it would otherwise drift. The text refers to it
  before it appears.
- **Figures** come from `benchmarks/plot_report.py`, use the fixed variant colours and markers, and
  are copied to `figures/`.
- **Cross-references** use `\secref{}`, `\secrefp{}`, `\tabref{}`, `\figref{}` (the whole phrase is the link) and
  `Chapter~\ref{}`.
- **Build** with pdflatex → bibtex → pdflatex → pdflatex. Keep the `.aux` and `.bbl` files.
  Afterwards there must be 0 undefined references and 0 overfull boxes, and the changed pages are
  rendered and looked at.

## 6. What the remaining chapters must do

- **Discussion** interprets; it does not report new measurements. It may combine results from
  different RQs, compare them with Related Work (Chapter 3) and state what they imply for an
  operator. Each claim points back to the result it rests on.
- **Threats to validity** names each threat, says what it could distort and what the design does
  against it. **Limitations** states what the thesis did not study.
- **Conclusion** answers RQ1–RQ4 in one or two sentences each, with the key numbers, then states
  the contribution. **Future work** follows from concrete findings and limitations.
- **Appendices** are reference material: configuration fields, the parity-check requests,
  reproduction steps and the numbers behind the figures. They use the same terminology, with
  repository paths in `\texttt{}`.
