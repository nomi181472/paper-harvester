#!/usr/bin/env python3
"""Download top-400 most-cited arXiv papers matching "software architecture"
in title/abstract, published 2000-01-01 through 2026-10-15.

Citation counts come from OpenAlex; PDFs come from arXiv.
Outputs: papers.csv, papers.jsonl, <arxiv_id>.pdf, failed.csv
"""

import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(OUT_DIR, "papers.csv")
JSONL_PATH = os.path.join(OUT_DIR, "papers.jsonl")
FAILED_PATH = os.path.join(OUT_DIR, "failed.csv")

OPENALEX = "https://api.openalex.org/works"
ARXIV_SOURCE = "S4306400194"  # arXiv (Cornell University)
TOP_N = 400
PAGE_SIZE = 100
ARXIV_DELAY = 3.5  # seconds between PDF requests, per arXiv rate guidance
UA = "sa-papers-downloader/1.0 (research; polite bulk download)"

FILTER = (
    "from_publication_date:2000-01-01,"
    "to_publication_date:2026-10-15,"
    f"primary_location.source.id:{ARXIV_SOURCE},"
    'title_and_abstract.search:"software architecture"'
)
SELECT = "id,display_name,publication_year,cited_by_count,doi,ids,authorships,primary_location,abstract_inverted_index"


def http_get(url, timeout=60, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last = e
            wait = 2 ** attempt * 2
            print(f"  retry {attempt + 1}/{retries} in {wait}s: {e}", flush=True)
            time.sleep(wait)
    raise last


def fetch_metadata():
    records = []
    page = 1
    total = None
    while True:
        url = (
            f"{OPENALEX}?filter={urllib.parse.quote(FILTER)}"
            f"&sort=cited_by_count:desc&per-page={PAGE_SIZE}&page={page}"
            f"&select={SELECT}"
        )
        data = json.loads(http_get(url))
        if total is None:
            total = data["meta"]["count"]
            print(f"OpenAlex reports {total} matching works", flush=True)
        results = data["results"]
        if not results:
            break
        for r in results:
            rec = parse_record(r)
            if rec:
                records.append(rec)
        print(f"  fetched page {page} ({len(records)} so far)", flush=True)
        if len(records) >= min(TOP_N, total) or len(results) < PAGE_SIZE:
            break
        page += 1
        time.sleep(0.5)
    # OpenAlex may return more/less than requested when ties exist; keep top N
    records = records[:TOP_N]
    for i, rec in enumerate(records, 1):
        rec["rank"] = i
    return records, total


def parse_record(r):
    loc = r.get("primary_location") or {}
    landing = loc.get("landing_page_url") or ""
    m = re.search(r"arxiv\.org/abs/([a-z0-9./\-]+)", landing, re.I)
    arxiv_id = m.group(1) if m else None
    if not arxiv_id:
        # fall back to ids dict
        m = re.search(r"arxiv\.org/abs/([a-z0-9./\-]+)", (r.get("ids") or {}).get("doi", "") or "", re.I)
        arxiv_id = m.group(1) if m else None
    if not arxiv_id:
        return None
    arxiv_id = arxiv_id.split("v")[0] if re.search(r"v\d+$", arxiv_id) else arxiv_id
    authors = [a["author"]["display_name"] for a in r.get("authorships", []) if a.get("author")]
    return {
        "arxiv_id": arxiv_id,
        "title": (r.get("display_name") or "").strip(),
        "year": r.get("publication_year"),
        "citations": r.get("cited_by_count"),
        "doi": r.get("doi"),
        "openalex_id": r.get("id"),
        "authors": authors,
        "first_author": authors[0] if authors else "",
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        "pdf_file": f"{arxiv_id.replace('/', '_')}.pdf",
    }


def write_metadata(records):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "arxiv_id", "title", "first_author", "authors_count",
                    "year", "citations", "doi", "pdf_file", "abs_url"])
        for r in records:
            w.writerow([r["rank"], r["arxiv_id"], r["title"], r["first_author"],
                        len(r["authors"]), r["year"], r["citations"], r["doi"],
                        r["pdf_file"], r["abs_url"]])
    with open(JSONL_PATH, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def download_pdfs(records):
    failures = []
    ok = 0
    skipped = 0
    total_bytes = 0
    for i, r in enumerate(records, 1):
        dest = os.path.join(OUT_DIR, r["pdf_file"])
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            skipped += 1
            print(f"[{i}/{len(records)}] skip {r['pdf_file']} (exists)", flush=True)
            continue
        url = r["pdf_url"]
        success = False
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    ctype = resp.headers.get("Content-Type", "")
                    data = resp.read()
                if b"<html" in data[:200].lower():
                    raise ValueError("got HTML instead of PDF")
                if len(data) < 1024:
                    raise ValueError(f"suspiciously small ({len(data)} bytes)")
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
                total_bytes += len(data)
                ok += 1
                success = True
                print(f"[{i}/{len(records)}] OK {r['pdf_file']} {len(data)//1024}KB ({ctype})", flush=True)
                break
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    print(f"[{i}/{len(records)}] FAIL {r['arxiv_id']} HTTP {e.code}", flush=True)
                    break
                print(f"[{i}/{len(records)}] HTTP {e.code}, retrying...", flush=True)
                time.sleep(3 * (attempt + 1))
            except Exception as e:  # noqa: BLE001
                print(f"[{i}/{len(records)}] error {e}, retrying...", flush=True)
                time.sleep(3 * (attempt + 1))
        if not success:
            failures.append(r)
        time.sleep(ARXIV_DELAY)

    if failures:
        with open(FAILED_PATH, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["rank", "arxiv_id", "title", "pdf_url"])
            for r in failures:
                w.writerow([r["rank"], r["arxiv_id"], r["title"], r["pdf_url"]])
    return ok, skipped, failures, total_bytes


def main():
    print("Fetching metadata from OpenAlex...", flush=True)
    records, total = fetch_metadata()
    print(f"Selected top {len(records)} of {total} candidates", flush=True)
    write_metadata(records)
    print(f"Wrote {CSV_PATH} and {JSONL_PATH}", flush=True)

    print(f"Downloading {len(records)} PDFs (delay {ARXIV_DELAY}s)...", flush=True)
    ok, skipped, failures, nbytes = download_pdfs(records)

    print("\n=== SUMMARY ===", flush=True)
    print(f"candidates: {total}", flush=True)
    print(f"selected:   {len(records)}", flush=True)
    print(f"downloaded: {ok}  skipped(existing): {skipped}  failed: {len(failures)}", flush=True)
    print(f"bytes new:  {nbytes / 1e6:.1f} MB", flush=True)
    if records:
        print(f"citations: top={records[0]['citations']}  rank{len(records)}={records[-1]['citations']}", flush=True)
    if failures:
        print(f"failures listed in {FAILED_PATH}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
