#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen-thumbs: build cover thumbnails for visible books that have no thumb.webp anywhere, and put them into the
R2 hot layer (bucket guyaofang-assets, key thumbs/{book_id}.webp) that /api/reader/{id}/cover reads first.

Why (2026-09-10 22:xx, founder: front page covers still missing): 12,285 visible overseas books have no
thumbnail, so every cover request pulls a ~1 MB full page through 123 (8-17 s, timeouts -> placeholder).
This job fetches that full page ONCE through our own cover endpoint (direct-link path, no 123 API quota),
resizes it to a 360 px WebP and stores it in R2. From then on the cover is a millisecond R2 read.

Discipline: serial inside a shard, sleep between calls, skip when R2 already has the key (one head_object per
book per lifetime via a ledger file), never LIST, never touch D1 rows, never delete anything.
"""
import io, json, os, sys, time, urllib.request
import boto3
from PIL import Image

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
B = "https://gufangai.com"
ACC = os.environ["CF_ACCOUNT_ID"]; DB = os.environ["D1_DATABASE_ID"]; TOK = os.environ["D1_API_TOKEN"]
SHARD = int(os.environ.get("SHARD") or "0"); SHARDS = int(os.environ.get("SHARDS") or "1")
CAP = int(os.environ.get("CAP") or "0")
SOFT_MIN = int(os.environ.get("SOFT_MIN") or "105")
BUCKET = os.environ.get("R2_BUCKET") or "guyaofang-assets"
WIDTH = 360
s3 = boto3.client("s3", endpoint_url=os.environ["S_EP"], aws_access_key_id=os.environ["S_AK"],
                  aws_secret_access_key=os.environ["S_SK"], region_name="auto")
LEDGER = "ledger_%d.json" % SHARD
try: done = set(json.load(open(LEDGER)))
except Exception: done = set()

def d1(sql):
    url = "https://api.cloudflare.com/client/v4/accounts/%s/d1/database/%s/query" % (ACC, DB)
    req = urllib.request.Request(url, data=json.dumps({"sql": sql}).encode(), method="POST",
                                 headers={"Authorization": "Bearer " + TOK, "Content-Type": "application/json"})
    j = json.loads(urllib.request.urlopen(req, timeout=60).read())
    if not j.get("success"): raise RuntimeError(str(j.get("errors"))[:200])
    return j["result"][0]["results"]

def r2_has(key):
    try: s3.head_object(Bucket=BUCKET, Key=key); return True
    except Exception: return False

def fetch_cover(bid):
    req = urllib.request.Request("%s/api/reader/%s/cover" % (B, bid), headers={"User-Agent": "gen-thumbs/1 (+github actions)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.headers.get("Content-Type") or "", r.headers.get("X-Source") or "", r.read()

def to_thumb(data):
    im = Image.open(io.BytesIO(data)); im.load()
    if im.mode not in ("RGB", "L"): im = im.convert("RGB")
    w, h = im.size
    if w > WIDTH: im = im.resize((WIDTH, max(1, int(h * WIDTH / w))), Image.LANCZOS)
    out = io.BytesIO(); im.save(out, "WEBP", quality=80, method=4); return out.getvalue()

def main():
    t0 = time.time()
    # 2026-09-11 00:4x: candidates = EVERY visible book, not only the ones without a 123 thumbnail. Random
    # sample of 40 books that DO have a 123 thumb: only 3 were in the R2 hot layer (7.5%) -- the cover endpoint's
    # natural backfill and warm_thumbs (counts every "dlink" hit as ineligible) never filled it, so those 34K
    # books still pay a 123 round trip on every cover request. Books already in R2 cost one head_object each
    # (recorded in the ledger, so once per lifetime); books with a 123 thumb come back small and fast via dlink.
    rows = d1("SELECT book_id FROM books_assets_v2 WHERE frontend_visible=1 "
              "AND collection IN ('overseas','overseas_guji') ORDER BY created_at DESC")
    # created_at DESC = the default order of the public shelves (agg=1 pages), so the books people actually see
    # on page 1..N get their thumbnails first (2026-09-10 22:5x, founder: front page still had placeholders).
    ids = [r["book_id"] for r in rows][SHARD::SHARDS]
    print("shard %d/%d candidates=%d ledger=%d" % (SHARD, SHARDS, len(ids), len(done)), flush=True)
    n = made = skip = fail = svg = 0
    for bid in ids:
        if bid in done: skip += 1; continue
        if CAP and n >= CAP: break
        if (time.time() - t0) / 60 > SOFT_MIN: print("soft deadline reached", flush=True); break
        n += 1
        key = "thumbs/%s.webp" % bid
        if r2_has(key): done.add(bid); skip += 1; continue
        try:
            # the cover endpoint gives up after ~6 s and answers a placeholder SVG when 123 is slow; pilot run:
            # 42/100 placeholders, 2/100 HTTP 500. Retry up to 3 times with growing gaps before giving up on a book.
            data = None
            for attempt in range(3):
                try:
                    ctype, src, data = fetch_cover(bid)
                except urllib.error.HTTPError as he:
                    if he.code >= 500 and attempt < 2: time.sleep(6 * (attempt + 1)); continue
                    raise
                if ctype.startswith("image/") and not ctype.startswith("image/svg"): break
                data = None; time.sleep(6 * (attempt + 1))
            if data is None:
                svg += 1; continue                            # still a placeholder; next run retries
            thumb = to_thumb(data) if len(data) > 60 * 1024 else data
            s3.put_object(Bucket=BUCKET, Key=key, Body=thumb, ContentType="image/webp")
            done.add(bid); made += 1
        except Exception as e:                                # noqa: BLE001
            fail += 1; print("  fail %s %s: %s" % (bid, type(e).__name__, str(e)[:60]), flush=True); time.sleep(3)
        if n % 50 == 0:
            print("  %d tried made=%d skip=%d svg=%d fail=%d elapsed=%ds" % (n, made, skip, svg, fail, time.time() - t0), flush=True)
            json.dump(sorted(done), open(LEDGER, "w"))
        time.sleep(1.0)
    json.dump(sorted(done), open(LEDGER, "w"))
    line = "gen-thumbs shard %d: candidates=%d tried=%d made=%d skip=%d svg=%d fail=%d elapsed=%ds" % (SHARD, len(ids), n, made, skip, svg, fail, time.time() - t0)
    print(line)
    open(os.environ.get("GITHUB_STEP_SUMMARY", "summary.md"), "a", encoding="utf-8").write("```\n" + line + "\n```\n")

if __name__ == "__main__":
    main()
