#!/usr/bin/env python3
"""Download cloud-native / cloud-native architecture papers with a
solution-architect framing. Saved into the same solution_architecture folder."""
from __future__ import annotations

import argparse, csv, json, os, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
API_BASE = "https://api.openalex.org"
UA = "cloudnative-sa-downloader/1.0"

QUERIES = [
    '"cloud-native" "solution architect"',
    '"cloud native architecture"',
    '"cloud-native application architecture"',
    '"cloud native application design"',
    '"container orchestration architecture"',
    '"kubernetes architecture"',
]

TARGET = 150
CSV_PATH = os.path.join(OUT_DIR, "papers_cloudnative.csv")
JSONL_PATH = os.path.join(OUT_DIR, "papers_cloudnative.jsonl")
UNAVAILABLE_PATH = os.path.join(OUT_DIR, "unavailable_cloudnative.json")
FAILED_PATH = os.path.join(OUT_DIR, "failed_cloudnative.csv")


def norm_title(t):
    if not t: return ""
    s = t.lower()
    s = re.sub(r"\s+", " ", s)
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


def openalex_get(url, params=None, retries=4):
    if params:
        url = url + "?" + urllib.parse.urlencode(params, safe='"')
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", errors="ignore"))
        except Exception as e:
            print(f"GET failed ({i}/{retries}): {e}")
            time.sleep(i * 2)
    raise RuntimeError(url)


def fetch_oa_pdf(w):
    loc = w.get("best_oa_location") or {}
    if loc.get("pdf_url"):
        return loc["pdf_url"]
    for l in w.get("locations", []):
        if l.get("pdf_url"):
            return l["pdf_url"]
    return None


def main(target=TARGET, year_min=2015, year_max=2026):
    seen_id, seen_title, collected, unavailable = set(), set(), [], []
    existing = {f[:-4] for f in os.listdir(OUT_DIR) if f.endswith(".pdf")}

    for q in QUERIES:
        print(f"Querying: {q}")
        params = {
            "filter": f"from_publication_date:{year_min}-01-01,to_publication_date:{year_max}-12-31,language:en,fulltext.search:{q}",
            "per-page": 200,
            "sort": "cited_by_count:desc",
            "select": "id,display_name,publication_year,doi,cited_by_count,best_oa_location,locations,open_access",
        }
        try:
            data = openalex_get(f"{API_BASE}/works", params)
        except Exception as e:
            print(f"  skip query: {e}")
            continue
        for w in data.get("results", []):
            wid = w.get("id", "")
            nt = norm_title(w.get("display_name"))
            if wid in seen_id or (nt and nt in seen_title):
                continue
            seen_id.add(wid)
            seen_title.add(nt)
            pdf = fetch_oa_pdf(w)
            if pdf:
                w["_pdf_url"] = pdf
                collected.append(w)
            else:
                unavailable.append({"id": wid, "title": w.get("display_name")})
        time.sleep(0.5)

    collected.sort(key=lambda x: x.get("cited_by_count", 0) or 0, reverse=True)
    collected = [w for w in collected if w.get("id", "").rsplit("/", 1)[-1] not in existing][:target]

    print(f"Collected OA candidates: {len(collected)}")
    with open(UNAVAILABLE_PATH, "w") as f:
        json.dump(unavailable, f, indent=2)

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as cf, open(JSONL_PATH, "w", encoding="utf-8") as jf:
        wr = csv.DictWriter(cf, fieldnames=["rank", "id", "doi", "title", "year", "citations", "pdf_url"])
        wr.writeheader()
        for i, w in enumerate(collected, 1):
            wr.writerow({"rank": i, "id": w.get("id"), "doi": w.get("doi"), "title": w.get("display_name"),
                         "year": w.get("publication_year"), "citations": w.get("cited_by_count"), "pdf_url": w.get("_pdf_url")})
            jf.write(json.dumps(w) + "\n")

    failed, ok = [], 0

    def dl(item):
        _, w = item
        oid = w.get("id", "").rsplit("/", 1)[-1]
        out = os.path.join(OUT_DIR, f"{oid}.pdf")
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            return ("skip", oid)
        try:
            req = urllib.request.Request(w["_pdf_url"], headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r, open(out, "wb") as f:
                f.write(r.read())
            return ("ok", oid)
        except Exception as e:
            return ("fail", f"{oid}:{e}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        for res in as_completed(ex.submit(dl, x) for x in enumerate(collected)):
            st, info = res.result()
            if st == "ok":
                ok += 1; print(f"  [{ok}] {info}.pdf")
            elif st == "fail":
                failed.append(info); print(f"  FAIL {info}")

    with open(FAILED_PATH, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f); wr.writerow(["id"])
        for x in failed:
            wr.writerow([x])
    print(f"Done. {ok} downloaded, {len(failed)} failed")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=TARGET)
    main(ap.parse_args().target)
