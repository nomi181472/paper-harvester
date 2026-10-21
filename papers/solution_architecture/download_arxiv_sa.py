#!/usr/bin/env python3
import os, re, time, argparse, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT = os.path.dirname(os.path.abspath(__file__))
UA = 'sa-arxiv/1.0'

def fetch(query, max_results=30):
    url = 'https://export.arxiv.org/api/query?' + urllib.parse.urlencode({
        'search_query': query,
        'start':0,
        'max_results':max_results,
        'sortBy':'submittedDate',
        'sortOrder':'descending'
    })
    req = urllib.request.Request(url, headers={'User-Agent':UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode('utf-8')

def main(target=30):
    os.makedirs(OUT, exist_ok=True)
    txt = fetch('all:"solution architect" OR all:"solution architecture"', target)
    pdfs = re.findall(r'href="(https://arxiv.org/pdf/([^"]+))"', txt)  # (full, idv)
    # dedup by id
    seen=set()
    items=[]
    for full, idv in pdfs:
        aid = idv.split('v')[0]
        if aid in seen: continue
        seen.add(aid)
        items.append((aid, full))
    print(f'Found {len(items)} arXiv papers')
    ok=0
    for aid, full in items:
        out = os.path.join(OUT, f'{aid}.pdf')
        if os.path.exists(out) and os.path.getsize(out)>1000:
            print(f'skip {aid}'); ok+=1; continue
        try:
            req = urllib.request.Request(full, headers={'User-Agent':UA})
            with urllib.request.urlopen(req, timeout=120) as r, open(out,'wb') as f:
                f.write(r.read())
            print(f'dl {aid}'); ok+=1
        except Exception as e:
            print(f'fail {aid}:{e}')
    print(f'Done {ok}/{len(items)}')

if __name__=='__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', type=int, default=30)
    args = ap.parse_args()
    main(args.target)
