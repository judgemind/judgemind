# Santa Clara batch-FCA clusters — investigation (2026-05)

Investigation #4339. Closes the post-#4331 question of whether the 21 SC
`all_same_case_title_cluster` rulings sharing `case_title="Plaintiff v. FCA"`
represent a real "batch FCA" data shape that the splitter cannot improve, or
something else.

**Spoiler:** the issue's stated hypothesis ("batch FCA cases where the
document-level header uses one generic title across all defendants") is
**falsified**. The affected PDF is a normal Department 6 multi-case calendar
with 12 distinct cases, distinct titles, and distinct case numbers — but in a
**compact summary-table format** that the SC splitter (`_split_rulings`)
cannot parse. The string `"Plaintiff v. FCA"` does not appear anywhere in the
PDF text — it is **synthesized by the LLM extraction step** as a generic
title, then stamped onto multiple rulings extracted from the same PDF.

The 21-row "cluster" is also exaggerated by an S3 mislabeling artefact:
15 distinct `derived.documents` S3 keys all point to the **same single PDF
blob** (`dept-6-tues_1.pdf`, content-hash `483f1da9e800…`, captured
2026-03-24).

## Symptom

Source: `scripts/audit_llm_carry_forward.py --county "Santa Clara" --json`
returns 21 SC `all_same_case_title_cluster` rows with `scraper_id =
"rebuild-ca-santa_clara"` and `case_title = "Plaintiff v. FCA"`. After #4331
landed and the SC splitter resolved correctly under the
`rebuild-ca-santa_clara` alias, `scripts/drain_splitter_carry_forward_clusters.py
--county "Santa Clara"` invoked the splitter on each affected PDF, but it
returned 2 entries per PDF with the same generic `case_title`, so
`plan_cluster_drain` returned `skip_single_title` for every cluster and the
drain refused to write.

Sample row from #4331's verification evidence:

```
"all_same_case_title_cluster": {
  "count": 21,
  "examples": [
    {"s3_key": "ca/santa_clara/superior_court/raw/0ef88ef1eff22ffa04e95debd065afe61cf90b91606b8b3d6d65c1be9d4cd3cc.pdf",
     "case_title": "Plaintiff v. FCA", "ruling_count": 2, "scraper_id": "rebuild-ca-santa_clara"}
  ]
}
```

## Investigation

### Step 1 — Verify the hypothesis: does the PDF body contain a per-defendant title the splitter is missing?

Downloaded the cluster's first sample S3 key
(`ca/santa_clara/superior_court/raw/0ef88ef1eff22ffa04e95debd065afe61cf90b91606b8b3d6d65c1be9d4cd3cc.pdf`)
and ran the same code path the scraper uses (`extract_pdf_text` →
`_split_rulings`). Probe script: `tmp/probe_sc_pdf.py` (this worktree).

**The PDF is not a batch-FCA PDF at all.** It is the **Department 6 Tuesday
calendar from 2026-03-24** (source URL
`https://santaclara.courts.ca.gov/system/files/tentative-ruling/dept-6-tues_1.pdf`),
listing **12 distinct cases** with distinct titles and case numbers in a
compact summary-table format:

```
LINE CASE NO. CASE TITLE                                  TENTATIVE RULING
9:00 22CV402462 Jessuca Shinneman vs. Kaiser Foundation   See Line Item 1 below for ruling.
9:00 25CV422369 Gabriella Schuhe vs Zuy Phan, et. al.     See Line Item 2 below for ruling
9:00 25CV465110 US National Bank vs. Susan Anne Edgar     Plaintiff moves this court to grant summary judgment...
9:00 24CV44932  Feng Wang et. al. vs Jiang Lewis et. al.  Defendant moves this court to compel...
9:00 25CV467190 Benjamin Cruz Solorio et.al. vs Ping Cao  Defendant petitions this court to compel...
9:00 23CV410483 Rudolph Silverberg vs Raul Castro         MCK Services... motion for leave...
9:00 25CV410483 Justin Kirkwood vs. Robert Bishop         Case is off calendar.
9:00 26CV475312 Re: Petition of Red Horse Freight, inc.   Petitioner requests order...
9:00 25CV435775 Jennifer Khu vs. Saul Flores, et.al.      See Line Item 9 below
10   25CV458975 Yin Labson vs. FCA US LLC                 See Line Item 10 below
11   24CV 439334 Lovette Mitchell vs. Guard Force, Inc.   Plaintiff moves this court to grant leave...
12   23CV428303 John Doe RVR vs Robert Crose              See Line Item 12 below
```

Then per-case ruling bodies follow, marked by section headings `Line 1`,
`LINE 10`, `LINE 12` (the only three with long-form rulings — the others
have short rulings inline in the summary table).

**The string "Plaintiff v. FCA" does not appear anywhere in the PDF text.**
A literal grep for `Plaintiff v. FCA` (and `Plaintiff vs. FCA`) returns zero
hits. The PDF's actual case title for the FCA case is `"Yin Labson vs. FCA US
LLC"` (line 10 in the summary table).

The "FCA" string itself appears only in (a) the `Yin Labson vs. FCA US LLC`
title in the summary table and (b) ruling-body prose for that one case
(`On August 20, 2016, Plaintiff entered a warranty contract with FCA US, LLC
("FCA")…`).

### Step 2 — Trace `_split_rulings` behaviour on the PDF

The splitter regex `_SC_RULING_ENTRY_RE = re.compile(r"^(?P<num>Line\s+\d{1,3})\s*$", re.MULTILINE | re.IGNORECASE)`
finds **3 boundaries** in this PDF — the section headings `Line 1`, `LINE 10`,
`LINE 12`. There are no `Line 2`, `Line 3`, … `Line 9`, `Line 11` body
sections because those are short rulings printed inline in the summary
table.

For each of the 3 entries, the per-entry `Case No.:` / `Case Name:` headers
the splitter expects (`_SC_CASE_NO_HEADER_RE`, `_SC_CASE_NAME_HEADER_RE`)
**do not exist** in this format. The splitter therefore returns:

```
ruling_index=1   case_number=None   case_title=None   body=64607 chars
ruling_index=10  case_number=None   case_title=None   body=18598 chars
ruling_index=12  case_number=None   case_title=None   body=30570 chars
```

This is **exactly what the SC scraper docstring at lines 451-456 explicitly
documents** as the limit of the splitter:

> B) Compact summary-table format (e.g. dept 6):
>    A single ``LINE CASE NO. CASE TITLE TENTATIVE RULING`` header,
>    then per-row entries on shared rows.  This format does NOT have
>    per-case bare ``Line N`` boundaries, so the splitter falls through
>    to the LLM path and the existing per-county prompt's anti-carry-
>    forward rule (5b) is the only protection.

`plan_cluster_drain` correctly refuses to drain because the splitter's
output (3 entries with no titles, all `None`) cannot improve on the existing
2 stored rulings.

### Step 3 — Where does `"Plaintiff v. FCA"` come from?

It must come from the LLM extraction step. The audit script is named
`audit_llm_carry_forward.py` because it specifically detects this failure
class — the LLM seeing a chunk of text containing "Plaintiff" and "FCA"
substrings and synthesising a generic title rather than extracting the
actual `"Yin Labson vs. FCA US LLC"` from the summary table.

The PDF-level `parse_case_title()` does NOT produce `"Plaintiff v. FCA"` —
its strategy-4 fallback returns `"Court will not consider. (See Nazir v.
United Airlines (2009)…"` (a citation), and the splitter's per-entry
`parse_case_title()` produces other unrelated citation strings. So the
hallucinated title is not coming from the regex layer.

### Step 4 — Check whether the cluster size is real

Aggregating the affected rows:

```
SELECT d.s3_key, COUNT(*) AS ruling_count, MAX(r.department) AS dept
FROM derived.documents d
JOIN derived.rulings r ON r.document_id = d.id
JOIN derived.cases    c ON c.id = r.case_id
WHERE d.scraper_id = 'rebuild-ca-santa_clara'
  AND c.case_title = 'Plaintiff v. FCA'
GROUP BY d.s3_key
ORDER BY d.s3_key;
```

Returns **15 distinct S3 keys × 2 rulings = 30 rulings** (the audit's "21"
appears to count cluster representatives, not all rows). All 15 PDFs are
**department 6**.

`aws s3api head-object` on each of the 15 S3 keys returns:

| key prefix         | content-hash       | size       | source URL                                      |
|--------------------|--------------------|------------|-------------------------------------------------|
| 0ef88ef1eff2…      | 483f1da9e800…      | 1,647,601  | …/system/files/tentative-ruling/dept-6-tues_1.pdf |
| 1631febda0ee…      | 483f1da9e800…      | 1,647,601  | …/system/files/tentative-ruling/dept-6-tues_1.pdf |
| 341904443dea…      | 483f1da9e800…      | 1,647,601  | …/system/files/tentative-ruling/dept-6-tues_1.pdf |
| … (12 more)        | 483f1da9e800…      | 1,647,601  | …/system/files/tentative-ruling/dept-6-tues_1.pdf |
| 483f1da9e800…      | 483f1da9e800…      | 1,647,601  | …/system/files/tentative-ruling/dept-6-tues_1.pdf |

**Every one of the 15 S3 keys references the same single PDF blob** (content
hash `483f1da9e800…`, the dept-6 Tuesday calendar from 2026-03-24). The S3
keys' embedded "hashes" do **not** match the content they store — only one
of them (`483f1da9…`) is correctly content-addressed; the other 14 are
mislabeled. This is consistent with the pre-existing S3 mislabeling
investigation (`docs/investigations/mislabeled-s3-writes-2026-04.md`) but is
not the focus of this issue.

The user-visible effect is that the "21 SC clusters" reported by the audit
is dominated by **one underlying PDF** counted under 15 different keys.

## Root cause

The 21-row cluster is the product of **two distinct upstream defects**, not
the single "batch FCA disposition" pattern the issue hypothesised:

1. **`_split_rulings` cannot handle dept-6 format-B compact summary
   tables.** The splitter regex (`^Line\s+\d+\s*$` boundary +
   `Case No.:`/`Case Name:` per-entry headers) is anchored on the format-A
   layout. Format B has no per-case `Line N` body section heading and no
   `Case No.:` headers. When the splitter falls through, the LLM
   extraction path is the only thing that produces structured output — and
   for these PDFs it hallucinates a generic `"Plaintiff v. FCA"` title for
   chunks that contain the FCA US LLC ruling alongside other cases'
   ruling text.
2. **S3 mislabeling inflates the apparent cluster size.** 15 distinct
   `derived.documents` rows reference the same single 1.6 MB blob under 15
   different (wrong) S3 keys. After de-duplicating by content hash, the
   cluster collapses from "15 PDFs × 2 rulings = 30" to "1 PDF, 12 real
   cases". This S3 mislabeling is tracked separately and is not in scope
   here.

The splitter's behaviour on this PDF is **correct as documented** — the
docstring explicitly calls out format B as the LLM-only path. The actual
gap is that no code path currently parses the format-B compact summary
table, so the LLM is the only chance to get titles right and it produces
hallucinated titles instead of extracting the real ones from the summary
table.

## Resolution path

**The right structural fix is option 1** — extend `_split_rulings` to
handle format B by parsing the compact summary table itself. This:

- Eliminates the LLM dependency for dept-6-style PDFs (12+ cases, all in a
  single summary table).
- Yields deterministic `case_number` AND `case_title` for every case in
  the calendar.
- Matches the same "splitter is the only way to avoid LLM carry-forward"
  pattern that #3534 (Fresno) and #3649 (Riverside) established.
- Drains the cluster reported by `audit_llm_carry_forward.py` (along with
  any future format-B clusters in other SC departments).

**The format-B summary table is highly regular** — across all 15
mislabeled copies of dept-6-tues, the layout is the same:

```
LINE CASE NO. CASE TITLE TENTATIVE RULING
<HH:MM> <CASENO> <Title spread across N lines> <inline ruling spread across M lines>
<line-num> <continuation of Title>
…
```

Lines start with either `<HH:MM> <CASENO>` (the per-row preamble) or a
small integer line number followed by continuation text. A parser that:

1. Anchors on the literal "LINE CASE NO. CASE TITLE TENTATIVE RULING" header
   to find the start of the summary table.
2. Iterates rows until the first body section heading (`Line 1` /
   `LINE 1`).
3. Splits each row on the leading `\d{1,2}:?\d{0,2}\s+\d{2}(?:CV|PR)\d{6}\s+`
   prefix to extract `<case_number>` and `<rest>`.
4. Greedily concatenates continuation lines into `<rest>` until the next
   row preamble.
5. Splits `<rest>` on `\s{2,}` (or whitespace alignment heuristics) to
   separate `case_title` from `inline ruling text`.

… would produce 12 deterministic SplitRuling entries for this PDF. The
ruling text for each entry can pull from (a) the inline summary-table cell
when present, or (b) the corresponding `Line N` / `LINE N` body section
when the cell says "See Line Item N below".

Alternative options that are NOT preferred:

- **Strengthen the LLM anti-carry-forward prompt rule (rule 5b).** Less
  reliable than deterministic regex parsing; even a stricter rule cannot
  invent the right title from chunks that don't include the summary table
  header.
- **Switch dept-6 PDFs to use a different LLM split strategy.** The same
  regex+structure problem moves from carry-forward to chunk boundary;
  doesn't fix the underlying "LLM doesn't know which row in the summary
  table to attribute the title from."

A follow-up implementation issue is filed (see "Follow-ups" below) with
concrete acceptance criteria, fixture text, and a reproducer test.

## Stale documentation

None found. The SC scraper docstring at lines 437-462 of
`packages/scraper-framework/src/courts/ca/sc_tentatives.py` already
correctly documents format B as falling through to the LLM path. The
follow-up implementation issue should update those lines to describe
format B as **also** handled by the splitter once the new path lands.

## Follow-ups

- **#TBD — feat(scraping): add SC format-B compact summary-table parser to
  `_split_rulings`.** Extend the splitter so it falls back to a
  summary-table path when no per-entry `Line N` body section is found.
  Acceptance includes a fixture from `dept-6-tues_1.pdf` and a regression
  test asserting 12 entries with distinct case_numbers and case_titles.
  Drains the audit's SC `all_same_case_title_cluster` rows (after
  reingest).

The S3 mislabeling artefact (15 keys × 1 blob) is **not** filed here —
it's already covered by the pre-existing
`docs/investigations/mislabeled-s3-writes-2026-04.md` investigation.
