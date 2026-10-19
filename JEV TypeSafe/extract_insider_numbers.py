"""
Recognise insider filings (Forms 3, 4, 5 and amendments) in the JSON from
fetch_latest_filings.py and extract their numbers from the filing's XML:
who the insider is, what they bought/sold/hold, how many shares, at what price,
and the total value in USD. No AI is needed: these forms are structured data.

Usage:
    SEC_USER_AGENT="Your Name your@email.com" python3 extract_insider_numbers.py   # newest file in filings/
    python3 extract_insider_numbers.py --limit 10                                   # small test
    python3 extract_insider_numbers.py filings/2026-10-14/2026-10-14_09-45_all_98.json   # a specific input file

Output is saved automatically to insider/<day>/<day>_<time>_<count>_from-<input time>.json,
e.g. insider/2026-10-14/2026-10-14_13-50_84_from-09-45.json
"""

# --- Imports ---
import argparse                      # command-line options
import sys                           # progress messages on stderr
import xml.etree.ElementTree as ET   # parses the Form 3/4/5 XML
from datetime import datetime, timezone  # timestamp for the output file

# Shared helpers (see sec_common.py)
from sec_common import (display_path, is_insider_form, list_documents, load_filings, output_path, raw_xml_url,
                        resolve_input, save_json, sec_get, source_label)

# What each SEC transaction code means (the most common ones)
TRANSACTION_CODES = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant or award from the company",
    "M": "exercise or conversion of a derivative (e.g. options)",
    "X": "exercise of an in-the-money derivative",
    "C": "conversion of a derivative",
    "F": "shares withheld to pay exercise price or tax",
    "G": "gift",
    "D": "sale or transfer back to the company",
    "J": "other acquisition or disposition",
    "W": "acquired or disposed by will or inheritance",
    "I": "discretionary transaction",
    "K": "equity swap or similar",
    "L": "small acquisition",
    "V": "transaction voluntarily reported earlier than required",
    "E": "expiration of a short derivative position",
    "H": "expiration or cancellation of a long derivative position",
    "O": "exercise of an out-of-the-money derivative",
    "U": "shares tendered in a change of control",
    "Z": "deposit into or withdrawal from a voting trust",
}


# --- Small helpers for reading the XML ---

def text(el, path):
    """Return the text at a path like "issuer/issuerName", or None if missing/empty."""
    found = el.find(path) if el is not None else None
    return found.text.strip() if found is not None and found.text and found.text.strip() else None


def value(el, path):
    """Most numbers in these forms are wrapped like <transactionShares><value>100</value>. Read the <value>."""
    return text(el, f"{path}/value")


def number(raw):
    """Convert "1,234.50" to 1234.5 (or 1234 for whole numbers); None if it isn't a number."""
    if raw is None:
        return None
    try:
        n = float(raw.replace(",", ""))
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


def flag(el, path):
    """Read a yes/no field. The forms use "true"/"false" or "1"/"0"."""
    return (text(el, path) or "").lower() in ("true", "1")


def optional_flag(el, path):
    """Like flag(), but None if the field is missing (older forms don't have every field)."""
    raw = text(el, path)
    return None if raw is None else raw.lower() in ("true", "1")


# --- Parsing the parts of the form ---

def parse_owner(owner):
    """One <reportingOwner>: the insider's name, CIK and relationship to the company."""
    rel = owner.find("reportingOwnerRelationship")
    return {
        "name": text(owner, "reportingOwnerId/rptOwnerName"),
        "cik": text(owner, "reportingOwnerId/rptOwnerCik"),
        "is_director": flag(rel, "isDirector"),
        "is_officer": flag(rel, "isOfficer"),
        "officer_title": text(rel, "officerTitle"),        # e.g. "Chief Financial Officer"
        "is_ten_percent_owner": flag(rel, "isTenPercentOwner"),
        "is_other": flag(rel, "isOther"),
        "other_text": text(rel, "otherText"),
    }


def parse_transaction(tx, table):
    """One buy/sell/grant/... row. `table` says if it's a plain share ("non_derivative") or an option etc."""
    code = text(tx, "transactionCoding/transactionCode")
    shares = number(value(tx, "transactionAmounts/transactionShares"))
    price = number(value(tx, "transactionAmounts/transactionPricePerShare"))
    record = {
        "table": table,
        "security": value(tx, "securityTitle"),                     # e.g. "Common Stock"
        "date": value(tx, "transactionDate"),                       # YYYY-MM-DD
        "code": code,                                               # e.g. "S"
        "code_meaning": TRANSACTION_CODES.get(code, "other"),       # e.g. "open-market sale"
        "acquired_or_disposed": value(tx, "transactionAmounts/transactionAcquiredDisposedCode"),  # A or D
        "shares": shares,
        "price_per_share": price,
        # Total value in USD = shares x price (only when both are given; grants often have no price)
        "value_usd": round(shares * price, 2) if shares is not None and price is not None else None,
        "shares_owned_after": number(value(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
        "direct_or_indirect": value(tx, "ownershipNature/directOrIndirectOwnership"),  # D = direct, I = indirect
    }
    # Options, warrants etc. have extra fields: exercise price and the shares they convert into
    if table == "derivative":
        record["exercise_price"] = number(value(tx, "conversionOrExercisePrice"))
        record["underlying_security"] = value(tx, "underlyingSecurity/underlyingSecurityTitle")
        record["underlying_shares"] = number(value(tx, "underlyingSecurity/underlyingSecurityShares"))
    return record


def parse_holding(h, table):
    """One holding row: shares owned without a transaction (typical for Form 3)."""
    record = {
        "table": table,
        "security": value(h, "securityTitle"),
        "shares_owned": number(value(h, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
        "direct_or_indirect": value(h, "ownershipNature/directOrIndirectOwnership"),
    }
    if table == "derivative":
        record["exercise_price"] = number(value(h, "conversionOrExercisePrice"))
        record["underlying_security"] = value(h, "underlyingSecurity/underlyingSecurityTitle")
        record["underlying_shares"] = number(value(h, "underlyingSecurity/underlyingSecurityShares"))
    return record


def summarize(transactions):
    """Totals for the two codes that matter most: open-market buys (P) and sells (S)."""
    totals = {}
    for label, code in (("purchases", "P"), ("sales", "S")):
        rows = [t for t in transactions if t["code"] == code]
        totals[label] = {
            "count": len(rows),
            "shares": sum(t["shares"] or 0 for t in rows),
            "value_usd": round(sum(t["value_usd"] or 0 for t in rows), 2),
        }
    return totals


def parse_ownership_xml(xml_bytes):
    """Turn a Form 3/4/5 XML document into a clean dict."""
    root = ET.fromstring(xml_bytes)
    transactions, holdings = [], []
    # Plain shares ("non-derivative") and options/warrants ("derivative") live in two separate tables
    for table, tag in (("non_derivative", "nonDerivativeTable"), ("derivative", "derivativeTable")):
        section = root.find(tag)
        if section is None:
            continue
        prefix = "nonDerivative" if table == "non_derivative" else "derivative"
        transactions += [parse_transaction(tx, table) for tx in section.findall(f"{prefix}Transaction")]
        holdings += [parse_holding(h, table) for h in section.findall(f"{prefix}Holding")]
    return {
        "document_type": text(root, "documentType"),          # "3", "4", "5", "4/A", ...
        "period_of_report": text(root, "periodOfReport"),     # date of the earliest transaction
        "issuer": {                                           # the company whose shares these are
            "name": text(root, "issuer/issuerName"),
            "cik": text(root, "issuer/issuerCik"),
            "ticker": text(root, "issuer/issuerTradingSymbol"),
        },
        "reporting_owners": [parse_owner(o) for o in root.findall("reportingOwner")],
        # True when the insider declares they own nothing (common for new SPAC directors on Form 3),
        # so an empty filing is clearly intentional and not a parsing problem
        "no_securities_owned": flag(root, "noSecuritiesOwned"),
        # True if the trades were made under a pre-set trading plan (Rule 10b5-1), i.e. planned in advance;
        # None if the form doesn't include this field
        "trading_plan_10b5_1": optional_flag(root, "aff10b5One"),
        # Free-text remarks from the filer, e.g. "This Form 4 constitutes an exit filing ..."
        "remarks": text(root, "remarks"),
        "currency": "USD",                                    # prices on these forms are always in USD
        "transactions": transactions,
        "holdings": holdings,
        "totals": summarize(transactions),
        # Footnotes often explain prices (e.g. "weighted average of sales between $92.10 and $93.05")
        "footnotes": {fn.get("id"): (fn.text or "").strip() for fn in root.findall("footnotes/footnote")},
    }


def find_ownership_xml(filing):
    """Find the URL of the raw XML document inside an insider filing."""
    docs = list_documents(filing["index_urls"][0])
    # Prefer the document whose type is the form itself (e.g. "4") and is XML; otherwise any XML
    for doc in docs:
        if is_insider_form(doc["type"]) and doc["url"].endswith(".xml"):
            return raw_xml_url(doc["url"])
    for doc in docs:
        if doc["url"].endswith(".xml"):
            return raw_xml_url(doc["url"])
    return None


def main():
    # --- Command-line options ---
    parser = argparse.ArgumentParser(description="Extract numbers from Form 3/4/5 filings.")
    parser.add_argument("input", nargs="?", default=None,
                        help="JSON file from fetch_latest_filings.py (default: the newest file in filings/)")
    parser.add_argument("--out", default=None, help="Output file (default: insider/<day>/<day>_<time>_...json)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N insider filings (for testing)")
    args = parser.parse_args()

    # --- Recognise the insider filings in the input ---
    input_path = resolve_input(args.input)   # given file, or the newest one in filings/
    source, filings = load_filings(input_path)
    insider = [f for f in filings if is_insider_form(f["form_type"])]
    print(f"{len(insider)} of {len(filings)} filings are Forms 3/4/5", file=sys.stderr)
    if args.limit:
        insider = insider[: args.limit]

    # --- Download and parse each one ---
    results, errors = [], 0
    for i, filing in enumerate(insider, 1):
        try:
            xml_url = find_ownership_xml(filing)
            if not xml_url:
                raise ValueError("no XML document found in filing")
            data = parse_ownership_xml(sec_get(xml_url))
            results.append({**filing, "xml_url": xml_url, "insider_data": data})
            # Short progress line: form, company, insider, buy/sell totals
            t = data["totals"]
            owner = data["reporting_owners"][0]["name"] if data["reporting_owners"] else "?"
            print(f"  [{i}/{len(insider)}] {filing['form_type']:<4} {data['issuer']['ticker'] or '-':<6} {owner}: "
                  f"bought ${t['purchases']['value_usd']:,.0f}, sold ${t['sales']['value_usd']:,.0f}", file=sys.stderr)
        except Exception as err:
            # Don't stop the whole run for one bad filing; record the error instead
            errors += 1
            results.append({**filing, "error": str(err)})
            print(f"  [{i}/{len(insider)}] ERROR {filing['accession_number']}: {err}", file=sys.stderr)

    # --- Save ---
    now = datetime.now(timezone.utc)
    # Default: insider/<day>/<day>_<time>_<count>_from-<input time>.json
    out = args.out or output_path("insider", len(results), source_label(input_path))
    save_json(out, {
        "created_at": now.isoformat(),
        "input_file": display_path(input_path),
        "input_fetched_at": source.get("fetched_at"),
        "count": len(results),
        "errors": errors,
        "filings": results,
    })
    print(f"Saved {len(results)} insider filings ({errors} errors) to {display_path(out)}", file=sys.stderr)


# Run main() only when this file is executed directly
if __name__ == "__main__":
    main()
