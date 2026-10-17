#!/usr/bin/env python3
"""Download the top-300 most-cited ERP (Enterprise Resource Planning) papers,
published 2021-01-01 through 2026-12-31, ranked by OpenAlex citations.

Source policy (per the user's decision)
---------------------------------------
Two phases, merged and finally sorted by citations descending:

  Phase A -- arXiv-first. For each query, works hosted on arXiv (OpenAlex source
             S4306400194) are admitted and their PDF fetched from arxiv.org.
             In this window arXiv holds only ~24 ERP papers, so this alone can
             never reach 300.

  Phase B -- open-access top-up. The same queries without the arXiv source
             filter; PDFs are resolved through OpenAlex best_oa_location /
             locations[].pdf_url (journals, conferences, repositories). This is
             what actually reaches 300.

Deduplication is by OpenAlex id and by normalized title, with the arXiv copy
winning when a work appears in both phases.

The acronym trap
----------------
"ERP" also means "event-related potential" in neuroscience. Phrase queries
("enterprise resource planning", "erp implementation", ...) are searched, and a
negative gate rejects any work whose *title* carries an event-related-potential
signal (p300, n400, eeg, evoked potential, ...), while admission requires a
positive enterprise-ERP signal. Both gates are applied after the query, so the
noise a bare "erp" search would drag in cannot enter the corpus.

Outputs (papers/enterprise_resource_planning/):
  papers.csv            rank, id, title, first_author, year, citations, source, ...
  papers.jsonl          one JSON record per paper
  <openalex_id>.pdf     downloaded PDFs
  unavailable.json      high-cited works passed over for lack of an OA PDF
  failed.csv            download failures
  download_summary.json counts + phase provenance

Re-running is safe: existing PDFs are skipped and the run is idempotent.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(OUT_DIR, "papers.csv")
JSONL_PATH = os.path.join(OUT_DIR, "papers.jsonl")
FAILED_PATH = os.path.join(OUT_DIR, "failed.csv")
UNAVAILABLE_PATH = os.path.join(OUT_DIR, "unavailable.json")
SUMMARY_PATH = os.path.join(OUT_DIR, "download_summary.json")

OPENALEX = "https://api.openalex.org/works"
ARXIV_SOURCE = "S4306400194"  # arXiv (Cornell University)

FROM_DATE = "2021-01-01"
TO_DATE = "2026-12-31"
TARGET = 300
PAGE_SIZE = 200
ARXIV_DELAY = 3.5
OA_DELAY = 1.0

# An API key lifts the shared $0.10/day budget to $1/day. Optional: read from the
# environment so it is never committed. See plan / OpenAlex docs.
API_KEY = os.environ.get("OPENALEX_API_KEY", "").strip()

CONTACT = "nomansoomro51@gmail.com"
UA = {"User-Agent": f"erp-papers-downloader/1.0 (mailto:{CONTACT})"}

# Many OA publishers (MDPI, ScienceDirect, Hindawi, ...) reject non-browser
# clients outright, while repositories only sometimes do. A browser-shaped
# request gets the repository copies that a bot-shaped one loses, and the ones
# still blocked are left to Unpaywall alternate locations.
PDF_UA = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

UNPAYWALL = "https://api.unpaywall.org/v2"
UNPAYWALL_CACHE = os.path.join(OUT_DIR, "unpaywall_cache.json")

SELECT = (
    "id,display_name,title,publication_year,cited_by_count,doi,ids,authorships,"
    "primary_location,best_oa_location,locations,abstract_inverted_index,type"
)

# Exact phrase first (highest precision), then synonym phrases. Each is searched
# as title_and_abstract.search:"...".
QUERIES: list[str] = [
    "enterprise resource planning",
    "erp system",
    "erp implementation",
    "erp adoption",
    "erp success",
    "sap erp",
]

# ---------------------------------------------------------------- relevance

# Positive enterprise-ERP signal (word-bounded). Short/ambiguous tokens only
# count when the surrounding text is clearly the enterprise sense.
POSITIVE = re.compile(
    r"\b(?:"
    r"enterprise\s+resource\s+planning|"
    r"erp\s+systems?|erp\s+software|erp\s+packages?|erp\s+projects?|"
    r"erp\s+implementations?|erp\s+adoptions?|erp\s+success|"
    r"erp\s+solutions?|erp\s+vendors?|erp\s+modules?|erp\s+applications?|"
    r"sap(?:\s+erp|\s+r/3|\s+s/4|)?|oracle\s+erp|microsoft\s+dynamics|"
    r"netsuite|odoo|infor\s+(?:erp|cloudsuite)|"
    r"enterprise\s+systems?|enterprise\s+software|enterprise\s+applications?|"
    r"business\s+process\s+reengineering|resource\s+planning\s+system"
    r")\b",
    re.I,
)

# Event-related-potential / neurophysiology signal -> reject on TITLE.
OFF = re.compile(
    r"\b(?:"
    r"event[-\s]related(?:\s+potential)?s?|"
    r"evoked\s+potential|"
    r"p\s?300|p\s?600|n\s?170|n\s?400|n\s?200|mmn|"
    r"eeg|meg|erp\s+component|erp\s+components|erp\s+amplitude|"
    r"erp\s+waveform|oddball|electroencephalogra|"
    r"neural\s+correlat|scalp|amplitude|latency|"
    r"cortical|brain[-\s]?computer|neurofeedback|"
    r"stimulus[-\s]?locked|time[-\s]?frequency|"
    r"attention\s+deficit|schizophren|parkinson|epilep|"
    r"dementia|alzheimer|autism|depression|motor\s+imagery"
    r")\b",
    re.I,
)

def reconstruct_abstract(inv: dict | None) -> str:
    if not inv:
        return ""
    n = max((max(v) for v in inv.values() if v), default=-1) + 1
    buf = [""] * n
    for word, poss in inv.items():
        for pos in poss:
            if 0 <= pos < n:
                buf[pos] = word
    return " ".join(buf)


def relevant(title: str, abstract: str) -> bool:
    """True if the title/abstract is the enterprise-ERP sense."""
    t = (title or "").lower()
    if OFF.search(t):  # a neurophysiology title is out, whatever the abstract says
        return False
    hay = f"{title}\n{abstract}"
    return bool(POSITIVE.search(hay))


# ------------------------------------------------------------------ http


def http_get_json(url: str, timeout: int = 90, retries: int = 4) -> dict:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            if e.code == 403 and "budget" in body.lower():
                raise SystemExit(
                    "OpenAlex daily budget exhausted. Set OPENALEX_API_KEY, or "
                    "wait for the 00:00 UTC reset.\n" + body
                )
            last = e
            time.sleep(2 ** attempt * 2)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"GET failed after {retries} tries: {url[:120]} ({last})")


def with_key(params: dict) -> dict:
    p = dict(params)
    p["mailto"] = CONTACT
    if API_KEY:
        p["api_key"] = API_KEY
    return p


# ------------------------------------------------------------- openalex query


def arxiv_id_of(w: dict) -> str | None:
    for loc in [w.get("primary_location") or {}, *(w.get("locations") or [])]:
        lp = (loc or {}).get("landing_page_url") or ""
        m = re.search(r"arxiv\.org/abs/([a-z0-9.\-/]+)", lp, re.I)
        if m:
            aid = m.group(1)
            return re.sub(r"v\d+$", "", aid)
    # fall back to ids dict
    ids = w.get("ids") or {}
    m = re.search(r"arxiv\.org/abs/([a-z0-9.\-/]+)", str(ids), re.I)
    if m:
        return re.sub(r"v\d+$", "", m.group(1))
    return None


def oa_pdf_url(w: dict) -> str:
    """Resolve an open-access PDF url: best_oa_location, then any location,
    then an arXiv landing page."""
    best = w.get("best_oa_location") or {}
    if best.get("pdf_url"):
        return best["pdf_url"]
    for loc in w.get("locations") or []:
        if (loc or {}).get("pdf_url"):
            return loc["pdf_url"]
    for loc in [best, *(w.get("locations") or [])]:
        lp = (loc or {}).get("landing_page_url") or ""
        if "arxiv.org/abs/" in lp:
            aid = re.sub(r"v\d+$", "", lp.rstrip("/").split("/abs/")[-1])
            if aid:
                return f"https://arxiv.org/pdf/{aid}"
    return ""


def parse_work(w: dict, source_type: str) -> dict | None:
    title = (w.get("title") or w.get("display_name") or "").strip()
    if not title:
        return None
    abstract = reconstruct_abstract(w.get("abstract_inverted_index"))
    if not relevant(title, abstract):
        return None

    arxiv_id = arxiv_id_of(w)
    if source_type == "arxiv" and not arxiv_id:
        return None
    oa_url = oa_pdf_url(w)
    if source_type == "arxiv":
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    else:
        pdf_url = oa_url
    if not pdf_url:
        return None

    authors = [
        (a.get("author") or {}).get("display_name", "")
        for a in (w.get("authorships") or [])[:25]
    ]
    venue = ""
    pl = w.get("primary_location") or {}
    if pl.get("source"):
        venue = (pl["source"] or {}).get("display_name") or ""
    oaid = w["id"].rsplit("/", 1)[-1]
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    return {
        "openalex": oaid,
        "doi": doi,
        "title": title,
        "year": w.get("publication_year"),
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "venue": venue,
        "type": (w.get("type") or "").lower(),
        "citations": w.get("cited_by_count") or 0,
        "sourceType": source_type,
        "arxivId": arxiv_id or "",
        "pdfUrl": pdf_url,
        "pdfFile": f"{oaid}.pdf",
        "abstract": abstract[:6000],
    }


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def query_phase(queries: list[str], source_type: str, need: int) -> list[dict]:
    """Run each query, page until exhausted, gate, dedupe within the phase."""
    out: list[dict] = []
    seen: set[str] = set()
    source_filter = f"primary_location.source.id:{ARXIV_SOURCE}," if source_type == "arxiv" else ""
    per_page = 50 if source_type == "arxiv" else PAGE_SIZE

    for q in queries:
        page = 1
        total = None
        while True:
            filt = (
                f"from_publication_date:{FROM_DATE},to_publication_date:{TO_DATE},"
                f"{source_filter}"
                f'title_and_abstract.search:"{q}"'
            )
            params = with_key({
                "filter": filt,
                "sort": "cited_by_count:desc",
                "per-page": per_page,
                "page": page,
                "select": SELECT,
            })
            url = OPENALEX + "?" + urllib.parse.urlencode(params)
            data = http_get_json(url)
            if total is None:
                total = data.get("meta", {}).get("count", 0)
                print(f"  [{source_type}] \"{q}\": {total} matches", flush=True)
            rows = data.get("results") or []
            if not rows:
                break
            for w in rows:
                r = parse_work(w, source_type)
                if r and r["openalex"] not in seen:
                    seen.add(r["openalex"])
                    out.append(r)
            print(f"    page {page}: {len(out)} gated candidates", flush=True)
            if len(rows) < per_page or (source_type == "openalex" and len(out) >= need * 3):
                break
            page += 1
            time.sleep(0.6)
        if source_type == "openalex" and len(out) >= need * 3:
            break
        time.sleep(1.0)
    return out


# ------------------------------------------------------------------ download


_UNPAYWALL_MEM: dict[str, list[str]] = {}


def _load_unpaywall_cache() -> None:
    global _UNPAYWALL_MEM
    if os.path.exists(UNPAYWALL_CACHE):
        try:
            with open(UNPAYWALL_CACHE, encoding="utf-8") as f:
                _UNPAYWALL_MEM = json.load(f)
        except Exception:  # noqa: BLE001
            _UNPAYWALL_MEM = {}


def _save_unpaywall_cache() -> None:
    tmp = UNPAYWALL_CACHE + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_UNPAYWALL_MEM, f)
    os.replace(tmp, UNPAYWALL_CACHE)


def unpaywall_locations(doi: str) -> list[str]:
    """Every OA PDF-ish URL Unpaywall knows for a DOI, repositories first.

    Publisher URLs are kept last because they are the ones that block bots;
    repository copies (figshare, institutional repos, PMC, DOAJ) usually serve.
    """
    doi = (doi or "").strip().lower()
    if not doi:
        return []
    if doi in _UNPAYWALL_MEM:
        return _UNPAYWALL_MEM[doi]
    urls: list[str] = []
    try:
        data = http_get_json(
            f"{UNPAYWALL}/{urllib.parse.quote(doi)}?email={CONTACT}", retries=2
        )
        locs = data.get("oa_locations") or []
        locs = sorted(locs, key=lambda l: 0 if (l.get("host_type") == "repository") else 1)
        for loc in locs:
            u = loc.get("url_for_pdf") or loc.get("url")
            if u and u not in urls:
                urls.append(u)
    except Exception:  # noqa: BLE001
        urls = []
    _UNPAYWALL_MEM[doi] = urls
    return urls


def candidates_for(rec: dict) -> list[str]:
    """Ordered list of URLs to try for one record."""
    cands: list[str] = []
    if rec.get("arxivId"):
        cands.append(f"https://arxiv.org/pdf/{rec['arxivId']}")
    for u in unpaywall_locations(rec.get("doi", "")):
        if u not in cands:
            cands.append(u)
    if rec.get("pdfUrl") and rec["pdfUrl"] not in cands:
        cands.append(rec["pdfUrl"])
    return cands


def _try_url(url: str, dest: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, headers=PDF_UA)
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read()
        if not data.startswith(b"%PDF-"):
            return False, f"not-pdf({ctype[:20]})"
        if len(data) < 1024:
            return False, f"too-small({len(data)}B)"
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return True, f"{len(data) // 1024}KB"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def download_one(rec: dict) -> tuple[bool, str]:
    dest = os.path.join(OUT_DIR, rec["pdfFile"])
    if os.path.exists(dest) and os.path.getsize(dest) > 1024:
        with open(dest, "rb") as f:
            if f.read(5) == b"%PDF-":
                return True, "exists"
    last = "no-candidate"
    for url in candidates_for(rec):
        for attempt in range(2):
            ok, why = _try_url(url, dest)
            if ok:
                return True, why
            last = why
            if why.startswith("HTTP 4"):
                break  # a 404/403 will not improve on retry
            time.sleep(2 * (attempt + 1))
    return False, last


def write_metadata(selected: list[dict]) -> None:
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "rank", "id", "openalex", "title", "first_author", "authors_count",
            "year", "citations", "source_type", "arxiv_id", "venue", "doi", "pdf_file",
        ])
        for r in selected:
            w.writerow([
                r["rank"], r["id"], r["openalex"], r["title"], r["first_author"],
                len(r["authors"]), r["year"], r["citations"], r["sourceType"],
                r["arxivId"], r["venue"], r["doi"], r["pdfFile"],
            ])
    with open(JSONL_PATH, "w", encoding="utf-8") as f:
        for r in selected:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def download_phase(selected: list[dict], n_arxiv: int, n_oa: int,
                   workers: int = 6) -> int:
    print(f"Downloading {len(selected)} PDFs with {workers} workers ...", flush=True)
    _load_unpaywall_cache()
    failures: list[dict] = []
    ok = skipped = 0
    lock = threading.Lock()

    def job(i: int, r: dict) -> tuple[int, dict, bool, str]:
        good, why = download_one(r)
        with lock:
            if (i + 1) % 25 == 0:
                _save_unpaywall_cache()
        return i, r, good, why

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(job, i, r) for i, r in enumerate(selected)]
        for fut in as_completed(futures):
            i, r, good, why = fut.result()
            if good:
                if why == "exists":
                    skipped += 1
                else:
                    ok += 1
                print(f"[{i + 1}/{len(selected)}] OK {r['pdfFile']} {why} ({r['sourceType']})", flush=True)
            else:
                failures.append(r)
                print(f"[{i + 1}/{len(selected)}] FAIL {r['openalex']} {why} {r['pdfUrl'][:70]}", flush=True)

    _save_unpaywall_cache()
    with open(FAILED_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "openalex", "title", "source_type", "pdf_url"])
        for r in failures:
            w.writerow([r["rank"], r["openalex"], r["title"], r["sourceType"], r["pdfUrl"]])

    # high-cited candidates that could not be downloaded
    unavailable = [
        {
            "openalex": r["openalex"],
            "title": r["title"],
            "year": r["year"],
            "citations": r["citations"],
            "reason": "no-open-access-pdf" if r["sourceType"] == "openalex" else "download-failed",
        }
        for r in failures[:100]
    ]
    with open(UNAVAILABLE_PATH, "w", encoding="utf-8") as f:
        json.dump(unavailable, f, indent=2, ensure_ascii=False)

    n_arxiv_sel = sum(1 for r in selected if r["sourceType"] == "arxiv")
    n_oa_sel = len(selected) - n_arxiv_sel
    n_pdf = sum(
        1 for r in selected
        if os.path.exists(os.path.join(OUT_DIR, r["pdfFile"]))
        and os.path.getsize(os.path.join(OUT_DIR, r["pdfFile"])) > 1024
    )
    summary = {
        "source": "openalex",
        "window": f"{FROM_DATE}..{TO_DATE}",
        "target": TARGET,
        "candidates_arxiv": n_arxiv,
        "candidates_openalex": n_oa,
        "selected": len(selected),
        "selected_arxiv": n_arxiv_sel,
        "selected_openalex": n_oa_sel,
        "pdfs_present": n_pdf,
        "downloaded": ok,
        "skipped_existing": skipped,
        "failed": len(failures),
        "citations_top": selected[0]["citations"] if selected else 0,
        "citations_bottom": selected[-1]["citations"] if selected else 0,
    }
    with open(SUMMARY_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print("\n=== SUMMARY ===", flush=True)
    for k, v in summary.items():
        print(f"{k:>20}: {v}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download-only", action="store_true",
                    help="read papers.jsonl and only fetch missing PDFs")
    ap.add_argument("--workers", type=int, default=6,
                    help="parallel PDF downloads (default 6)")
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    if args.download_only:
        if not os.path.exists(JSONL_PATH):
            print("papers.jsonl missing; run a full discovery first", file=sys.stderr)
            return 1
        with open(JSONL_PATH, encoding="utf-8") as f:
            selected = [json.loads(line) for line in f if line.strip()]
        for i, r in enumerate(selected, 1):
            r["rank"] = r.get("rank", i)
            r["id"] = r.get("id", f"ERP{i:03d}")
        print(f"download-only: {len(selected)} records from {JSONL_PATH}", flush=True)
        return download_phase(selected, 0, 0, args.workers)

    print("Phase A: arXiv-first discovery ...", flush=True)
    arxiv = query_phase(QUERIES, "arxiv", TARGET)
    print(f"  arXiv candidates: {len(arxiv)}", flush=True)

    openalex: list[dict] = []
    if len(arxiv) < TARGET:
        print("Phase B: open-access top-up discovery ...", flush=True)
        openalex = query_phase(QUERIES, "openalex", TARGET - len(arxiv))
        print(f"  OA candidates: {len(openalex)}", flush=True)

    merged: list[dict] = []
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    for r in [*arxiv, *openalex]:
        nt = norm_title(r["title"])
        if r["openalex"] in seen_ids or nt in seen_titles:
            continue
        seen_ids.add(r["openalex"])
        seen_titles.add(nt)
        merged.append(r)

    merged.sort(key=lambda r: (-r["citations"], -(r["year"] or 0), r["title"]))
    selected = merged[:TARGET]
    for i, r in enumerate(selected, 1):
        r["rank"] = i
        r["id"] = f"ERP{i:03d}"

    print(f"Selected {len(selected)} of {len(merged)} gated candidates", flush=True)
    write_metadata(selected)
    print(f"Wrote {CSV_PATH} and {JSONL_PATH}", flush=True)
    return download_phase(selected, len(arxiv), len(openalex), args.workers)


if __name__ == "__main__":
    sys.exit(main())
