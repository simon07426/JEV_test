"""
Fetch the latest SEC EDGAR filings (all form types) from the "Latest Filings"
Atom feed and save them as JSON.

The feed covers roughly the most recent business day (~5-6k entries) and is
updated every few minutes. It only contains filing metadata, not the documents.

Output is saved automatically to filings/<day>/<day>_<time>_<form>_<count>.json,
e.g. filings/2026-10-14/2026-10-14_13-30_8-K_100.json

Usage:
    SEC_USER_AGENT="Your Name your@email.com" python3 fetch_latest_filings.py
    python3 fetch_latest_filings.py --form 8-K
"""

# --- Imports: all from Python's standard library, nothing to install ---
import argparse                      # reads command-line options like --form and --out
import json                          # writes the result as a JSON file
import os                            # reads environment variables (SEC_USER_AGENT)
import re                            # regular expressions, used to pull fields out of text
import sys                           # stderr printing and exiting with an error message
import time                          # sleep() between requests so we don't overload the SEC
import urllib.error                  # HTTP error types (e.g. 503) so we can catch them
import urllib.request                # makes the HTTP requests to sec.gov
import xml.etree.ElementTree as ET   # parses the XML (Atom) feed
from datetime import datetime, timezone  # timestamp for when the data was fetched

# Shared helpers for output folders and file names (see sec_common.py)
from sec_common import display_path, output_path

# --- Settings ---
# Feed URL template: {form} = form type filter (empty = all), {start} = paging offset
FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&count=100&start={start}&output=atom"
# The Atom XML namespace; ElementTree needs it to find tags like <entry>
ATOM = {"a": "http://www.w3.org/2005/Atom"}
# The feed returns at most 100 entries per request
PAGE_SIZE = 100
# Pause between requests (seconds). SEC limit is 10 req/s, but this endpoint throttles earlier
REQUEST_DELAY = 0.5

# Pattern for an entry title like "4 - TELECOM ARGENTINA SA (0000932470) (Issuer)"
# -> captures form="4", name="TELECOM ARGENTINA SA", cik="0000932470", role="Issuer"
TITLE_RE = re.compile(r"^(?P<form>.+?) - (?P<name>.*) \((?P<cik>\d{10})\) \((?P<role>[^)]*)\)$")
# Pattern for the summary text "<b>Filed:</b> 2026-10-13 <b>AccNo:</b> 0001193125-26-420859 <b>Size:</b> 25 KB"
# -> captures filed date, accession number and size
SUMMARY_RE = re.compile(r"Filed:</b>\s*(?P<filed>[\d-]+).*?AccNo:</b>\s*(?P<acc>[\d-]+).*?Size:</b>\s*(?P<size>[^<\n]+)", re.S)
# 8-K summaries also list the reported items, e.g. "<br>Item 2.02: Results of Operations and Financial Condition"
ITEM_RE = re.compile(r"Item\s+(\d+\.\d+):\s*([^<\n]+)")


def fetch_page(form, start, user_agent, retries=3):
    """Download one page (up to 100 entries) of the feed and return the raw XML bytes."""
    # Fill in the URL template for this page
    url = FEED_URL.format(form=form, start=start)
    # Build the request with the User-Agent header the SEC requires
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    # Try a few times, because the SEC sometimes returns temporary errors
    for attempt in range(retries):
        try:
            # Send the request and return the response body
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
            # Get the HTTP status code (None for network errors like a timeout)
            status = getattr(err, "code", None)
            # Only retry "temporary" errors (429/5xx/network); give up on others or on the last attempt
            if status not in (None, 429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            # Wait longer after each failure: 1s, 2s, 4s...
            wait = 2 ** attempt
            print(f"  {err} at start={start}, retrying in {wait}s...", file=sys.stderr)
            time.sleep(wait)


def parse_entries(xml_bytes):
    """Turn one page of Atom XML into a list of Python dicts, one per <entry>."""
    # Parse the XML text into a tree we can search
    root = ET.fromstring(xml_bytes)
    # Loop over every <entry> element (one entry = one filing + one company/person)
    for entry in root.findall("a:entry", ATOM):
        # Read the parts of the entry we need
        title = entry.findtext("a:title", "", ATOM).strip()  # "4 - NAME (CIK) (Role)"
        summary = entry.findtext("a:summary", "", ATOM)      # contains Filed / AccNo / Size
        link = entry.find("a:link", ATOM)                    # link to the filing's index page
        category = entry.find("a:category", ATOM)            # holds the form type, e.g. "8-K"

        # Apply the regex patterns to extract the individual fields
        t = TITLE_RE.match(title)
        s = SUMMARY_RE.search(summary)
        # Without an accession number we can't use the entry, so skip it with a warning
        if not s:
            print(f"warning: could not parse entry: {title}", file=sys.stderr)
            continue

        # Return ("yield") one dict per entry; fall back gracefully if the title didn't match
        yield {
            "accession_number": s["acc"],  # unique ID of the filing
            "form_type": category.get("term") if category is not None else (t["form"] if t else None),
            "filed_date": s["filed"],      # date the filing was filed (YYYY-MM-DD)
            "accepted_at": entry.findtext("a:updated", "", ATOM),  # exact time the SEC accepted it
            "size": s["size"].strip(),     # size of the filing, e.g. "25 KB"
            # 8-K items, e.g. ["2.02: Results of Operations and Financial Condition"] (empty for other forms)
            "items": [f"{num}: {title.strip()}" for num, title in ITEM_RE.findall(summary)],
            "index_url": link.get("href") if link is not None else None,  # page listing the filing's documents
            "entity": {                    # the company or person this entry is about
                "name": t["name"] if t else title,
                "cik": t["cik"] if t else None,
                "role": t["role"] if t else None,  # e.g. Filer, Issuer, Reporting, Subject
            },
        }


def fetch_all(form, user_agent, max_entries=None):
    """Page through the feed until it runs out. Returns raw (per-entity) entries."""
    entries = []  # collects entries from all pages
    start = 0     # offset of the current page (0, 100, 200, ...)
    while True:
        try:
            # Download the current page
            xml_bytes = fetch_page(form, start, user_agent)
        except urllib.error.HTTPError as err:
            # Past the last page the SEC answers 503 instead of an empty feed,
            # so a 503 after the first page means "no more data"
            if err.code == 503 and start > 0:
                print(f"  got 503 at start={start}, treating as end of feed", file=sys.stderr)
                break
            # Any other error is a real problem: stop the script
            raise
        # Parse the page into dicts
        page = list(parse_entries(xml_bytes))
        # An empty page also means we've reached the end
        if not page:
            break
        # Add this page's entries to the full list and show progress
        entries.extend(page)
        print(f"  fetched {len(entries)} entries (oldest: {page[-1]['accepted_at']})", file=sys.stderr)
        # Stop early if the user set a --max limit and we've reached it
        if max_entries and len(entries) >= max_entries:
            break
        # Move to the next page and wait a bit before requesting it
        start += PAGE_SIZE
        time.sleep(REQUEST_DELAY)
    return entries


def merge_by_accession(entries):
    """One filing can appear several times (e.g. Form 4: Reporting + Issuer). Merge into one record."""
    filings = {}  # accession number -> merged filing record
    for e in entries:
        acc = e["accession_number"]
        # First time we see this filing: copy the shared fields and start empty lists
        if acc not in filings:
            filings[acc] = {k: v for k, v in e.items() if k not in ("entity", "index_url")}
            filings[acc]["entities"] = []
            filings[acc]["index_urls"] = []
        f = filings[acc]
        # Add this entry's company/person if not already listed
        if e["entity"] not in f["entities"]:
            f["entities"].append(e["entity"])
        # Add this entry's index URL if not already listed
        if e["index_url"] and e["index_url"] not in f["index_urls"]:
            f["index_urls"].append(e["index_url"])
    # Return just the merged records as a list (newest first, same order as the feed)
    return list(filings.values())


def main():
    # --- Command-line options ---
    parser = argparse.ArgumentParser(description="Fetch latest SEC EDGAR filings as JSON.")
    parser.add_argument("--form", default="", help="Form type filter, e.g. 10-K, 8-K, 4 (default: all)")
    parser.add_argument("--max", type=int, default=None, help="Stop after roughly this many feed entries")
    parser.add_argument("--out", default=None, help="Output file (default: filings/<day>/<day>_<time>_<form>_<count>.json)")
    args = parser.parse_args()

    # --- User-Agent: the SEC blocks requests without a name + email ---
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        sys.exit('Set SEC_USER_AGENT, e.g. SEC_USER_AGENT="Your Name your@email.com" (required by the SEC)')

    # --- Fetch and merge ---
    fetched_at = datetime.now(timezone.utc)  # remember when we fetched, saved into the output
    print(f"Fetching latest filings (form={args.form or 'ALL'})...", file=sys.stderr)
    entries = fetch_all(args.form, user_agent, args.max)  # download all pages
    filings = merge_by_accession(entries)                 # combine duplicates into one record per filing

    # --- Save to a JSON file ---
    # Use the --out name, or a timestamped name like filings_20261014_073500.json
    # Default: filings/<day>/<day>_<time>_<form or "all">_<count>.json, named after the fetch time (local)
    out = args.out or output_path("filings", args.form or "all", len(filings), when=fetched_at.astimezone())
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "fetched_at": fetched_at.isoformat(),          # when this pull ran (UTC)
                "source": "SEC EDGAR Latest Filings Atom feed",
                "form_filter": args.form or None,              # which form type was requested, if any
                "count": len(filings),                         # number of filings in the file
                "filings": filings,                            # the actual data
            },
            fh,
            indent=2,            # pretty-print so it's readable
            ensure_ascii=False,  # keep non-English characters as-is
        )
    print(f"Saved {len(filings)} filings ({len(entries)} feed entries) to {display_path(out)}", file=sys.stderr)


# Run main() only when this file is executed directly (not when imported)
if __name__ == "__main__":
    main()
