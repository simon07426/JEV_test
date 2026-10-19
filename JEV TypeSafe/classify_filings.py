"""
Classify filings from fetch_latest_filings.py with Jev (TypeSafe AI).
For each filing it downloads the text and asks Jev three questions:

  1. type             - event / commitment / insider / results / financing / ownership / routine
  2. new_or_expected  - new (not public before) or expected (scheduled / already known)
  4. deeper_analysis  - yes / borderline / no

(Field 3, the amount, is not something Jev can extract. Forms 3/4/5 are skipped by
default because extract_insider_numbers.py handles them without AI.)

Usage:
    export SEC_USER_AGENT="Your Name your@email.com"
    export TYPESAFE_API_KEY="..."            # from https://console.typesafe.ai/
    python3 classify_filings.py                      # uses the newest file in filings/
    python3 classify_filings.py --limit 5            # small test
    python3 classify_filings.py --dry-run            # no API key needed, nothing sent to Jev
    python3 classify_filings.py filings/2026-10-14/2026-10-14_13-30_8-K_100.json   # a specific input file

Output is saved automatically to classified/<day>/<day>_<time>_<form>_<count>_from-<input time>.json,
e.g. classified/2026-10-14/2026-10-14_13-46_8-K_30_from-13-30.json
"""

# --- Imports ---
import argparse                      # command-line options
import json                          # building the request / reading the response
import os                            # API key and settings from environment variables
import re                            # reads 8-K item numbers out of older input files
import sys                           # progress messages and exiting with an error
import time                          # retry waits
import urllib.error                  # HTTP error types
import urllib.request                # sending the request to Jev
from collections import Counter      # counting 424B2 filings per filer
from datetime import datetime, timezone  # timestamp for the output file

# Shared helpers (see sec_common.py)
from sec_common import (display_path, html_to_text, is_insider_form, list_documents, load_filings, output_path,
                        resolve_input, save_json, sec_get, source_label)

# --- Jev settings (can be changed with environment variables) ---
# Official TypeSafe endpoint (docs.typesafe.ai). Set TYPESAFE_API_URL to use a gateway instead.
JEV_API_URL = os.environ.get("TYPESAFE_API_URL", "https://api.typesafe.ai/v1/systemone")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-latest")  # or pin a version, e.g. "jev-1.13.0"

# Max characters of filing text sent per filing. Number-heavy filings use ~3 characters per token,
# so 80k chars ~ 27k tokens, under Jev's per-question limit. Long filings get cut here; the output
# marks them "truncated": true. If Jev still says the text is too long, it is halved and retried.
DEFAULT_MAX_CHARS = 80_000

# Answers with a confidence below this are flagged as uncertain ("Jev couldn't really decide").
# E.g. probabilities 56% vs 44% give a confidence of ~0.12, which is close to a coin flip.
CONFIDENCE_THRESHOLD = 0.3

# A "yes" for deeper_analysis with confidence below this is downgraded to "borderline".
# In testing, all correct "yes" answers had confidence >= 0.94, while doubtful ones were below 0.5.
WEAK_YES_THRESHOLD = 0.5

# --- The questions sent to Jev ---
# Definitions from section 5 of the spec ("What it is" + examples).
# Jev decides based on these texts, so clear descriptions = better answers.
TYPE_CRITERIA = {
    # Deals and structural changes
    "event": (
        "an acquisition, merger, sale of a business, major contract or change of company structure; "
        "e.g. a company buys another company for billions (paid in cash or shares), "
        "takes over a competitor, or reorganises its business segments (e.g. from 3 to 2 segments)"
    ),
    # Guarantees and obligations that create risk for the company
    "commitment": (
        "a guarantee, large obligation or hidden risk the company takes on; "
        "e.g. the company guarantees residual values or debts of another party (such as data centers "
        "leased to a tenant), capped at a large amount"
    ),
    # Insider trades (Form 4 and similar)
    "insider": (
        "directors or officers trading shares of their own company (Form 4 and similar, incl. Form 144 "
        "notices of planned sales); e.g. a director sells millions of shares and their holding falls sharply"
    ),
    # Earnings and guidance
    "results": (
        "quarterly or annual financial results and outlook/guidance; "
        "e.g. revenue and growth versus last year, or the company raises or lowers its annual outlook"
    ),
    # Raising money through debt
    "financing": (
        "bond issues, notes, credit lines and other borrowing; "
        "e.g. a company issues billions of bonds in several tranches, issues notes, or exchanges bonds"
    ),
    # Large shareholders' stakes
    "ownership": (
        "a large shareholder reports or changes its stake: Schedule 13G (passive investor, annual) "
        "or Schedule 13D (active stake); e.g. founders reduce their stakes slightly, or a fund "
        "goes from 9.41% to 8.97% of a company"
    ),
    # No news
    "routine": (
        "nothing new: administrative or procedural filing without a news item"
    ),
}

QUESTIONS = {
    # Field 1: what kind of filing is this?
    "type": {
        "type": "choice",
        "instructions": "Which category best describes this SEC filing?",
        "criteria": TYPE_CRITERIA,
    },
    # Field 2: is this news, or was it already known/scheduled?
    "new_or_expected": {
        "type": "choice",
        "instructions": (
            "Does this filing disclose a new transaction, decision or change, or does it confirm something "
            "already announced, scheduled or automatic?"
        ),
        "criteria": {
            "new": (
                "discloses a transaction, decision or change that was not announced before; e.g. a large "
                "shareholder or insider decides to buy or sell shares, a stake changes, a deal or contract "
                "is signed, guidance changes unexpectedly"
            ),
            "expected": (
                "confirms something already announced or scheduled, or reports an automatic event; e.g. regular "
                "quarterly results, details of an offering that was already announced, shares sold automatically "
                "to cover taxes when stock awards vest, trades under a pre-set trading plan, routine periodic reports"
            ),
        },
    },
    # Field 4: should someone (or a bigger AI) read this filing more closely?
    "deeper_analysis": {
        "type": "choice",
        "instructions": (
            "Should this filing be analysed in more depth because it may matter to investors? "
            "Judge by what happens and how large it is relative to the company, not by how the press release sounds."
        ),
        "criteria": {
            "yes": (
                "a completed or announced acquisition, merger or large asset sale; financing or an obligation that is "
                "large relative to the company (e.g. over $10 million); a CEO or CFO change; results with a guidance "
                "change or a big surprise; a change of control; an activist stake (Schedule 13D) or a shareholder "
                "crossing 5% or 10% of the company"
            ),
            "borderline": (
                "smaller deals and partnerships, clinical trial updates, executive changes other than CEO or CFO, "
                "reverse stock splits"
            ),
            "no": (
                "regular dividends, conference presentations, product announcements, small loans (under $1 million), "
                "compensation or incentive plan changes, administrative filings, routine holding updates by passive "
                "investors (Schedule 13G or 13G/A) that do not cross 5% or 10%"
            ),
        },
    },
}

# --- Structured notes (424B2) are classified by a rule instead of Jev ---
# Banks file hundreds of 424B2 "pricing supplements" a day for structured notes. They are almost never
# relevant, are long (expensive for Jev) and Jev's new/expected answer is a coin flip for them.
# Other companies also use 424B2 for real bond issues (e.g. PepsiCo), so those still go to Jev.
STRUCTURED_NOTE_ISSUERS = [
    "JPMORGAN", "MORGAN STANLEY", "CITIGROUP", "UBS AG", "GS FINANCE", "GOLDMAN SACHS", "BARCLAYS",
    "BANK OF NOVA SCOTIA", "BOFA FINANCE", "BANK OF MONTREAL", "HSBC", "TORONTO DOMINION", "ROYAL BANK OF CANADA",
    "WELLS FARGO FINANCE", "NOMURA", "CANADIAN IMPERIAL BANK", "JEFFERIES", "MAREX", "SOCIETE GENERALE",
    "BNP PARIBAS", "DEUTSCHE BANK", "MIZUHO", "CREDIT SUISSE",
]
# Any filer with at least this many 424B2s in the same input file is also treated as a structured-note issuer
STRUCTURED_NOTE_MIN_FILINGS = 5

# The fixed answers given to structured notes (same fields as Jev's answers; no confidence since no AI was used)
RULE_RESULT_STRUCTURED_NOTE = {
    "type": "financing", "type_confidence": None, "type_uncertain": False,
    "new_or_expected": "expected", "new_or_expected_confidence": None, "new_or_expected_uncertain": False,
    "deeper_analysis": "no", "deeper_analysis_confidence": None, "deeper_analysis_uncertain": False,
    "uncertain_fields": [], "deeper_analysis_jev_answer": None, "deeper_analysis_downgraded": False,
}


def is_structured_note(filing, frequent_424b2_filers):
    """True if the filing is a 424B2 from a bank / frequent structured-note issuer."""
    if filing["form_type"].upper() != "424B2":
        return False
    names = [e["name"] for e in filing["entities"]]
    known_bank = any(issuer in name.upper() for name in names for issuer in STRUCTURED_NOTE_ISSUERS)
    frequent = any(name in frequent_424b2_filers for name in names)
    return known_bank or frequent


def build_state(filing, max_chars):
    """
    Download a filing and build the text Jev will read: a short header (form, date, companies)
    followed by the main document and any press-release exhibits (EX-99), cut at max_chars.
    """
    # Header with the metadata we already have; helps Jev with context
    names = "; ".join(f"{e['name']} ({e['role']})" for e in filing["entities"])
    # 8-K items say what the filing is about (e.g. "2.02: Results of Operations"). Newer input files
    # have them in "items"; older ones had them mixed into the "size" text, so read them from there too.
    items = filing.get("items") or re.findall(r"Item\s+(\d+\.\d+:\s*[^<\n]+)", filing.get("size", ""))
    header = f"SEC form type: {filing['form_type']}\nFiled: {filing['filed_date']}\nCompanies/persons: {names}\n"
    if items:
        header += "Reported items: " + "; ".join(i.strip() for i in items) + "\n"
    header += "\n"

    # Pick the documents to read: the main document (seq 1) plus EX-99 exhibits (usually press releases)
    docs = list_documents(filing["index_urls"][0])
    wanted = [d for d in docs if d["seq"] == "1" or d["type"].upper().startswith("EX-99")]
    # Only text-like files (skip images, PDFs, zip files)
    wanted = [d for d in wanted if d["url"].lower().endswith((".htm", ".html", ".txt", ".xml"))]

    # Download each document and add its text until we reach the character budget
    parts, total = [header], len(header)
    for doc in wanted:
        if total >= max_chars:
            break
        body = html_to_text(sec_get(doc["url"]))
        chunk = f"=== {doc['type']} {doc['description']} ===\n{body}\n\n"
        parts.append(chunk)
        total += len(chunk)
    state = "".join(parts)
    # Cut to the budget and report whether anything was dropped
    return state[:max_chars], len(state) > max_chars


def ask_jev(state, api_key, retries=3):
    """Send one filing's text + the questions to Jev and return the "answers" part of the response."""
    body = json.dumps({"model": JEV_MODEL, "state": state, "questions": QUESTIONS}).encode("utf-8")
    req = urllib.request.Request(
        JEV_API_URL,
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as err:
            # Retry rate limits and server errors; for other errors show Jev's message and stop
            if err.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            detail = err.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"Jev API error {err.code}: {detail}") from None


def flatten(answers, min_confidence):
    """
    Turn Jev's answers into simple fields, e.g. {"type": "results", "type_confidence": 0.93,
    "type_uncertain": false, ...} plus "uncertain_fields": a list of answers below min_confidence.
    """
    result, uncertain = {}, []
    for name in QUESTIONS:
        answer = answers.get(name, {})
        confidence = answer.get("confidence")
        result[name] = answer.get("choice")                     # the chosen label
        result[f"{name}_confidence"] = confidence               # how sure Jev is (0-1)
        # Flag the answer as uncertain if confidence is low (or missing)
        is_uncertain = confidence is None or confidence < min_confidence
        result[f"{name}_uncertain"] = is_uncertain
        if is_uncertain:
            uncertain.append(name)
    # One list per filing, so you can filter "filings that need a human look" easily
    result["uncertain_fields"] = uncertain

    # Weak "yes" rule: a deeper_analysis "yes" with low confidence becomes "borderline".
    # Jev's original answer is kept in deeper_analysis_jev_answer (and in jev_raw).
    jev_answer = result["deeper_analysis"]
    confidence = result["deeper_analysis_confidence"]
    downgraded = jev_answer == "yes" and (confidence is None or confidence < WEAK_YES_THRESHOLD)
    result["deeper_analysis_jev_answer"] = jev_answer
    result["deeper_analysis_downgraded"] = downgraded
    if downgraded:
        result["deeper_analysis"] = "borderline"
    return result


def main():
    # --- Command-line options ---
    parser = argparse.ArgumentParser(description="Classify SEC filings with Jev (fields 1, 2 and 4).")
    parser.add_argument("input", nargs="?", default=None,
                        help="JSON file from fetch_latest_filings.py (default: the newest file in filings/)")
    parser.add_argument("--out", default=None, help="Output file (default: classified/<day>/<day>_<time>_...json)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N filings (for testing)")
    parser.add_argument("--form", default=None, help="Only process one form type, e.g. 8-K")
    parser.add_argument("--include-insider", action="store_true", help="Also classify Forms 3/4/5 (skipped by default)")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Max characters of text sent per filing")
    parser.add_argument("--dry-run", action="store_true", help="Download and prepare everything but don't call Jev")
    parser.add_argument("--min-confidence", type=float, default=CONFIDENCE_THRESHOLD,
                        help=f"Flag answers below this confidence as uncertain (default: {CONFIDENCE_THRESHOLD})")
    args = parser.parse_args()

    # --- API key (not needed for a dry run) ---
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key and not args.dry_run:
        sys.exit("Set TYPESAFE_API_KEY (get one at https://console.typesafe.ai/), or use --dry-run to test without it")

    # --- Pick which filings to classify ---
    input_path = resolve_input(args.input)   # given file, or the newest one in filings/
    source, filings = load_filings(input_path)
    selected = [f for f in filings if args.include_insider or not is_insider_form(f["form_type"])]
    if args.form:
        selected = [f for f in selected if f["form_type"].upper() == args.form.upper()]
    skipped_insider = sum(1 for f in filings if is_insider_form(f["form_type"])) if not args.include_insider else 0
    if args.limit:
        selected = selected[: args.limit]
    print(f"Classifying {len(selected)} filings (skipped {skipped_insider} Forms 3/4/5)"
          f"{' [DRY RUN]' if args.dry_run else ''}", file=sys.stderr)

    # Filers with many 424B2s in this input are structured-note issuers (counted over the whole input file)
    filer_counts = Counter(e["name"] for f in filings if f["form_type"].upper() == "424B2" for e in f["entities"])
    frequent_424b2_filers = {name for name, n in filer_counts.items() if n >= STRUCTURED_NOTE_MIN_FILINGS}

    # --- Process each filing ---
    results, errors, rule_count = [], 0, 0
    for i, filing in enumerate(selected, 1):
        try:
            # 0) Structured notes: fixed answer, no download and no Jev call
            if is_structured_note(filing, frequent_424b2_filers):
                results.append({**filing, "classified_by": "rule", "text_chars": 0, "truncated": False,
                                "jev": dict(RULE_RESULT_STRUCTURED_NOTE)})
                rule_count += 1
                print(f"  [{i}/{len(selected)}] {filing['form_type']:<14} -> structured note (rule, no Jev)",
                      file=sys.stderr)
                continue
            # 1) Download the filing's text
            state, truncated = build_state(filing, args.max_chars)
            record = {**filing, "classified_by": "jev", "text_chars": len(state), "truncated": truncated}
            if args.dry_run:
                # Dry run: keep a preview of what would be sent instead of calling Jev
                record["state_preview"] = state[:500]
                print(f"  [{i}/{len(selected)}] {filing['form_type']:<14} {len(state):>7} chars"
                      f"{' (truncated)' if truncated else ''}", file=sys.stderr)
            else:
                # 2) Ask Jev. If the text is too long for Jev, halve it and try again (up to 2 times)
                for shrink in range(3):
                    try:
                        response = ask_jev(state, api_key)
                        break
                    except RuntimeError as err:
                        if "max_tokens_exceeded" not in str(err) or shrink == 2:
                            raise
                        state = state[: len(state) // 2]
                        record.update(text_chars=len(state), truncated=True)
                # 3) Keep both the simple fields and the full answer (with probabilities)
                record["jev"] = flatten(response.get("answers", {}), args.min_confidence)
                record["jev_raw"] = response
                j = record["jev"]
                # Show a "?" after each answer Jev was unsure about
                mark = lambda name: f"{j[name]}{'?' if j[f'{name}_uncertain'] else ''}"
                print(f"  [{i}/{len(selected)}] {filing['form_type']:<14} -> {mark('type')}, {mark('new_or_expected')}, "
                      f"deeper: {mark('deeper_analysis')}", file=sys.stderr)
            results.append(record)
        except Exception as err:
            # Don't stop the whole run for one bad filing; record the error instead
            errors += 1
            results.append({**filing, "error": str(err)})
            print(f"  [{i}/{len(selected)}] ERROR {filing['accession_number']}: {err}", file=sys.stderr)

    # --- Save ---
    # Count filings with at least one uncertain answer
    uncertain_count = sum(1 for r in results if r.get("jev", {}).get("uncertain_fields"))
    now = datetime.now(timezone.utc)
    # Default: classified/<day>/<day>_<time>_<form or "all">_<count>_from-<input time>.json
    tag = "dryrun" if args.dry_run else None
    form_label = args.form or source.get("form_filter") or "all"   # e.g. "8-K" if the input only had 8-Ks
    out = args.out or output_path("classified", form_label, len(results), source_label(input_path), tag)
    save_json(out, {
        "created_at": now.isoformat(),
        "input_file": display_path(input_path),
        "input_fetched_at": source.get("fetched_at"),
        "model": JEV_MODEL,
        "dry_run": args.dry_run,
        "questions": QUESTIONS,              # saved so you know exactly what Jev was asked
        "min_confidence": args.min_confidence,   # threshold used for the uncertain flags
        "count": len(results),
        "errors": errors,
        "uncertain_filings": uncertain_count,    # filings with at least one low-confidence answer
        "rule_classified": rule_count,           # structured notes answered by rule (not sent to Jev)
        "skipped_insider_forms": skipped_insider,
        "filings": results,
    })
    print(f"Saved {len(results)} filings ({errors} errors, {uncertain_count} with uncertain answers, "
          f"{rule_count} structured notes by rule) to {display_path(out)}", file=sys.stderr)


# Run main() only when this file is executed directly
if __name__ == "__main__":
    main()
