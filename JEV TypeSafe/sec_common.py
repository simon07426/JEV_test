"""
Shared helpers for working with filings from fetch_latest_filings.py:
downloading from sec.gov politely, recognising Forms 3/4/5, listing a filing's
documents and turning HTML into plain text.

Used by classify_filings.py and extract_insider_numbers.py.
"""

# --- Imports (standard library only) ---
import html                          # turns "&amp;" etc. back into normal characters
import json                          # reads the input JSON file
import os                            # reads the SEC_USER_AGENT environment variable
import re                            # regular expressions for parsing the index page
import sys                           # stderr printing and exiting with an error
import time                          # throttling and retry waits
import urllib.error                  # HTTP error types
import urllib.request                # HTTP requests
from datetime import datetime        # timestamps for output file names
from html.parser import HTMLParser   # simple HTML parser used to strip tags
from pathlib import Path             # building folder and file paths
from urllib.parse import urljoin     # turns "/Archives/..." links into full URLs

# --- Output folders ---
# All output goes next to these scripts, sorted into one folder per kind and one sub-folder per day:
#   filings/2026-10-14/2026-10-14_13-30_8-K_100.json               <- fetch_latest_filings.py
#   classified/2026-10-14/2026-10-14_13-46_8-K_30_from-13-30.json  <- classify_filings.py
#   insider/2026-10-14/2026-10-14_13-50_84_from-13-30.json         <- extract_insider_numbers.py
PROJECT_DIR = Path(__file__).resolve().parent

# --- Recognising insider filings (Forms 3, 4, 5 and their amendments) ---
# Form 3 = initial holdings of a new insider, Form 4 = a change (buy/sell/grant...),
# Form 5 = annual catch-up of transactions not reported on Form 4.
INSIDER_FORMS = {"3", "4", "5", "3/A", "4/A", "5/A"}

# Minimum pause between two requests to sec.gov (SEC limit is 10 per second)
MIN_REQUEST_INTERVAL = 0.15
_last_request_at = 0.0  # time of the previous request, used for throttling


def is_insider_form(form_type):
    """True if the filing is a Form 3, 4 or 5 (including amendments like 4/A)."""
    return (form_type or "").strip().upper() in INSIDER_FORMS


def get_user_agent():
    """Read the User-Agent the SEC requires (name + email) or stop with a clear message."""
    user_agent = os.environ.get("SEC_USER_AGENT")
    if not user_agent:
        sys.exit('Set SEC_USER_AGENT, e.g. SEC_USER_AGENT="Your Name your@email.com" (required by the SEC)')
    return user_agent


def sec_get(url, retries=3):
    """Download a URL from sec.gov and return the raw bytes (throttled, with retries)."""
    global _last_request_at
    req = urllib.request.Request(url, headers={"User-Agent": get_user_agent()})
    for attempt in range(retries):
        # Wait if the previous request was too recent, to respect the SEC rate limit
        wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
            # Retry only temporary problems (throttling, server errors, network); give up otherwise
            status = getattr(err, "code", None)
            if status not in (None, 429, 500, 502, 503, 504) or attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)  # back off: 1s, 2s, ...


def load_filings(path):
    """Read a JSON file produced by fetch_latest_filings.py and return (whole file, list of filings)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data, data["filings"]


def list_documents(index_url):
    """
    Read a filing's "-index.htm" page and return its documents in order, e.g.
    [{"seq": "1", "description": "...", "type": "8-K", "url": "https://..."}, ...].
    The document with seq "1" is the main document of the filing.
    """
    page = sec_get(index_url).decode("utf-8", errors="replace")
    # Only look at the first table ("Document Format Files"); the second one holds XBRL data files
    table = re.search(r'<table class="tableFile" summary="Document Format Files">(.*?)</table>', page, re.S)
    if not table:
        return []
    documents = []
    # Each <tr> row is one document: Seq | Description | Document (link) | Type | Size.
    # Rows can have attributes (e.g. <tr class="evenRow">), so match <tr ...> too.
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        link = re.search(r'href="([^"]+)"', row)
        # Skip the header row and rows without a document link
        if len(cells) < 4 or not link:
            continue
        # Inline-XBRL documents are linked through a viewer ("/ix?doc=/Archives/..."), which returns
        # an almost empty page. Strip the viewer part to get the actual document.
        href = re.sub(r"^/ix\?doc=", "", link.group(1))
        documents.append({
            "seq": strip_tags(cells[0]),
            "description": strip_tags(cells[1]),
            "type": strip_tags(cells[3]),
            "url": urljoin("https://www.sec.gov", href),  # make the link absolute
        })
    return documents


def raw_xml_url(url):
    """
    The index page links XML forms through a viewer folder like ".../xslF345X06/ownership.xml",
    which returns formatted HTML. Removing that folder gives the raw XML file.
    """
    return re.sub(r"/xsl[^/]+/", "/", url)


def strip_tags(fragment):
    """Remove HTML tags from a small piece of HTML and tidy the whitespace."""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


class _TextExtractor(HTMLParser):
    """Collects the visible text of an HTML page, skipping <script> and <style>."""

    # Tags after which we start a new line, so paragraphs and table rows stay readable
    BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table"}

    def __init__(self):
        super().__init__()
        self.parts = []     # pieces of text collected so far
        self.skip = 0       # > 0 while we are inside <script> or <style>

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        elif tag in ("td", "th"):
            self.parts.append(" | ")  # separate table cells so numbers don't run together

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(raw):
    """Convert an HTML (or plain text) document into clean plain text."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    # Plain-text filings (.txt) have no tags; only run the parser when there is HTML
    if "<" in text and ">" in text:
        parser = _TextExtractor()
        parser.feed(text)
        text = "".join(parser.parts)
    # Collapse runs of spaces and blank lines
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


def save_json(path, data):
    """Write data to a pretty-printed JSON file."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _safe(part):
    """Make a piece of a file name safe, e.g. "SCHEDULE 13D/A" -> "SCHEDULE-13D-A"."""
    return re.sub(r"[^A-Za-z0-9.]+", "-", str(part)).strip("-")


def output_path(kind, *parts, when=None):
    """
    Build the path for a new output file and create its folder, e.g.
    output_path("classified", "8-K", 30, "from-13-30")
      -> <project>/classified/2026-10-14/2026-10-14_13-46_8-K_30_from-13-30.json
    `when` is the time used in the name (default: now, local time).
    """
    when = when or datetime.now()
    folder = PROJECT_DIR / kind / when.strftime("%Y-%m-%d")   # one sub-folder per day
    folder.mkdir(parents=True, exist_ok=True)
    name = "_".join([when.strftime("%Y-%m-%d_%H-%M")] + [_safe(p) for p in parts if p not in (None, "")])
    path = folder / f"{name}.json"
    # Two runs in the same minute: add _2, _3, ... instead of overwriting
    counter = 2
    while path.exists():
        path = folder / f"{name}_{counter}.json"
        counter += 1
    return path


def latest_file(kind):
    """Return the newest .json file in a folder like "filings" (searching all day sub-folders), or None."""
    files = list((PROJECT_DIR / kind).glob("**/*.json"))
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def source_label(input_path):
    """
    Short tag saying which input a result came from, used in output names:
    "2026-10-14_13-30_8-K_100.json" -> "from-13-30" (same day) or "from-10-13_13-30" (other day).
    """
    stem = Path(input_path).stem
    match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2})", stem)
    if not match:
        return "from-" + _safe(stem)            # older/other file names: use the whole name
    day, time_ = match.groups()
    today = datetime.now().strftime("%Y-%m-%d")
    return f"from-{time_}" if day == today else f"from-{day[5:]}_{time_}"


def resolve_input(path_arg):
    """
    Pick the input file: the path given on the command line, or else the newest file in filings/.
    Stops with a clear message if there is nothing to use.
    """
    if path_arg:
        return Path(path_arg)
    newest = latest_file("filings")
    if not newest:
        sys.exit("No input file given and filings/ is empty. Run fetch_latest_filings.py first.")
    print(f"Using newest fetched file: {display_path(newest)}", file=sys.stderr)
    return newest


def display_path(path):
    """Show a path relative to the project folder (e.g. "filings/2026-10-14/...json"), or in full if outside it."""
    path = Path(path).resolve()
    try:
        return str(path.relative_to(PROJECT_DIR))
    except ValueError:
        return str(path)
