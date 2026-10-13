"""Performance agent — pulls public Instagram numbers for every client account into post_metrics.

Usage:
  python3 perf.py pull [days]     scrape each client's Instagram profile (followers) + recent posts (views/likes/comments),
                                  keep posts from the last <days> days (default 40), upsert into post_metrics, match to calendar posts by URL
  python3 perf.py followers           daily follower snapshot for every client → follower_history (Graph business_discovery → public web endpoint → Apify)
  python3 perf.py summary [YYYY-MM]   per-client month summary (planned/live/on-time/viral/reach multiple) — same maths as the dashboard
  python3 perf.py scorecard <Bhavana|Shubham> [YYYY-MM]   the person's KPI table as the dashboard computes it

Source: Apify apify/instagram-profile-scraper (followers) + apify/instagram-scraper (posts). Costs ~$0.30 per weekly run.
Falls back gracefully: if Apify credits are out, records the failure to agent_activity and exits 0 so the Monday run still reports.
"""
import sys, json, re, datetime, calendar
import common as C

AGENT = "Performance"
VIRAL_X = 5


def clients_with_ig():
    return [c for c in C.select("clients", "select=id,name,instagram,ig_followers,status&status=neq.Ended") if (c.get("instagram") or "").strip()]


def social_clients(owner=None):
    """Clients whose social media KPIs count (service_type != video); optionally only those owned by one SMM."""
    rows = C.select("clients", "select=id,name,instagram,ig_followers,posts_per_month,follower_target_pct,smm_owner,service_type,status&status=neq.Ended")
    rows = [c for c in rows if (c.get("service_type") or "social") != "video"]
    if owner:
        rows = [c for c in rows if (c.get("smm_owner") or "Bhavana") == owner]
    return rows


def pull(days=40):
    cls = clients_with_ig()
    if not cls:
        C.log(AGENT, "pull", "no client Instagram handles set — nothing to pull", needs_tejas=True)
        C.out({"ok": False, "reason": "no handles"}); return
    handles = [c["instagram"].strip().lstrip("@") for c in cls]
    byhandle = {c["instagram"].strip().lstrip("@").lower(): c for c in cls}
    since = (C.now_ist() - datetime.timedelta(days=days)).date().isoformat()
    try:
        profiles = C.apify_run("apify/instagram-profile-scraper", {"usernames": handles}, wait=240, max_usd=0.3)
    except Exception as e:
        C.log(AGENT, "pull_failed", f"Instagram profile pull failed: {str(e)[:200]}", needs_tejas=True)
        C.out({"ok": False, "error": str(e)[:300]}); return
    followers = {}
    for p in profiles:
        u = (p.get("username") or "").lower()
        if u in byhandle:
            followers[u] = p.get("followersCount")
            if p.get("followersCount") is not None:
                snapshot(byhandle[u], p["followersCount"], p.get("postsCount"), "apify")
    try:
        posts = C.apify_run("apify/instagram-scraper", {"directUrls": [f"https://www.instagram.com/{h}/" for h in handles],
                                                        "resultsType": "posts", "resultsLimit": 30, "onlyPostsNewerThan": since,
                                                        "addParentData": False}, wait=280, max_usd=0.6)
    except Exception as e:
        C.log(AGENT, "pull_failed", f"Instagram posts pull failed: {str(e)[:200]}", needs_tejas=True)
        C.out({"ok": False, "followers": followers, "error": str(e)[:300]}); return
    # calendar posts with an Instagram link → match by shortcode
    cal = C.select("posts", "select=id,client_id,content_link&content_link=ilike.*instagram.com*")
    def code(u):
        m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", u or "")
        return m.group(1) if m else None
    calmap = {code(p["content_link"]): p["id"] for p in cal if code(p["content_link"])}
    rows, matched = [], 0
    for it in posts:
        u = (it.get("ownerUsername") or "").lower()
        c = byhandle.get(u)
        sc = it.get("shortCode") or code(it.get("url"))
        if not c or not sc:
            continue
        url = it.get("url") or f"https://www.instagram.com/p/{sc}/"
        t = it.get("type") or ("Reel" if it.get("videoViewCount") is not None else "Image")
        if it.get("productType") == "clips" or it.get("isVideo"):
            t = "Reel"
        pid = calmap.get(sc)
        if pid: matched += 1
        rows.append({"client_id": c["id"], "post_id": pid, "ig_url": url, "ig_shortcode": sc, "ig_type": t,
                     "posted_at": it.get("timestamp"), "caption": (it.get("caption") or "")[:300],
                     "views": it.get("videoPlayCount") or it.get("videoViewCount"), "likes": it.get("likesCount"),
                     "comments": it.get("commentsCount"), "followers_at": followers.get(u) or c.get("ig_followers"),
                     "fetched_at": C.now_ist().isoformat(), "source": "apify"})
    n = 0
    for i in range(0, len(rows), 50):
        batch = rows[i:i + 50]
        # upsert on ig_url: PostgREST merge-duplicates so refreshed counts overwrite
        r = C.http("POST", f"{C.SB_URL}/rest/v1/post_metrics?on_conflict=ig_url", batch,
                   C._h({"Prefer": "return=representation,resolution=merge-duplicates"}))
        n += len(r or [])
    viral = [r for r in rows if r["followers_at"] and (r["views"] or 0) >= VIRAL_X * r["followers_at"]]
    C.log(AGENT, "pull", f"Instagram pull: {len(cls)} accounts, {len(rows)} posts since {since}, {matched} matched to the calendar, {len(viral)} viral (≥{VIRAL_X}× followers)",
          detail={"followers": followers, "viral": [{"url": v["ig_url"], "views": v["views"], "followers": v["followers_at"]} for v in viral]})
    C.out({"ok": True, "accounts": len(cls), "followers": followers, "posts": len(rows), "matched_to_calendar": matched,
           "viral": [{"url": v["ig_url"], "views": v["views"], "followers": v["followers_at"]} for v in viral]})


def snapshot(c, followers, posts=None, source="web"):
    today = C.now_ist().date().isoformat()
    C.update("clients", f"id=eq.{c['id']}", {"ig_followers": followers, "ig_followers_at": C.now_ist().isoformat()})
    C.http("POST", f"{C.SB_URL}/rest/v1/follower_history?on_conflict=client_id,date",
           [{"client_id": c["id"], "date": today, "followers": followers, "posts": posts, "source": source}],
           C._h({"Prefer": "return=minimal,resolution=merge-duplicates"}))


def followers():
    """Daily follower counts. Tries the official Graph API (business_discovery — needs instagram_basic on the token),
    then Instagram's public web endpoint (paced; shared IPs get rate-limited), then Apify."""
    import time, urllib.request
    cls = clients_with_ig(); got = {}; errs = []
    ig = C.secrets().get("META_IG_ID")
    for c in cls:
        h = c["instagram"].strip().lstrip("@")
        # 1) Graph API
        if ig and h not in got:
            try:
                r = C.ads_get(ig, {"fields": f"business_discovery.username({h}){{followers_count,media_count}}"})
                bd = r.get("business_discovery") or {}
                if bd.get("followers_count") is not None:
                    snapshot(c, bd["followers_count"], bd.get("media_count"), "graph"); got[h] = bd["followers_count"]; continue
            except Exception as e:
                errs.append(f"graph {h}: {str(e)[-80:]}")
        # 2) public web endpoint
        for attempt in range(2):
            try:
                req = urllib.request.Request(f"https://i.instagram.com/api/v1/users/web_profile_info/?username={h}",
                                             headers={"x-ig-app-id": "936619743392459", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
                u = json.load(urllib.request.urlopen(req, timeout=20))["data"]["user"]
                snapshot(c, u["edge_followed_by"]["count"], u["edge_owner_to_timeline_media"]["count"], "web"); got[h] = u["edge_followed_by"]["count"]; break
            except Exception as e:
                errs.append(f"web {h}: {str(e)[:60]}"); time.sleep(8)
        time.sleep(6)
    missing = [c["instagram"] for c in cls if c["instagram"].strip().lstrip("@") not in got]
    if missing:
        # 3) Apify for whatever is left
        try:
            for p in C.apify_run("apify/instagram-profile-scraper", {"usernames": missing}, wait=200, max_usd=0.2):
                for c in cls:
                    if (p.get("username") or "").lower() == c["instagram"].strip().lstrip("@").lower() and p.get("followersCount") is not None:
                        snapshot(c, p["followersCount"], p.get("postsCount"), "apify"); got[c["instagram"]] = p["followersCount"]
        except Exception as e:
            errs.append("apify: " + str(e)[-100:])
    missing = [c["name"] for c in cls if c["instagram"].strip().lstrip("@") not in got and c["instagram"] not in got]
    C.log(AGENT, "followers", f"Follower snapshot: {len(got)} of {len(cls)} accounts" + (f"; missing {', '.join(missing)}" if missing else ""),
          detail={"followers": got, "errors": errs[:10]}, needs_tejas=bool(missing))
    C.out({"ok": not missing, "followers": got, "missing": missing, "errors": errs[:10]})


def month_bounds(m):
    y, mo = int(m[:4]), int(m[5:7])
    return f"{m}-01", f"{m}-{calendar.monthrange(y, mo)[1]:02d}"


def summary(m=None):
    m = m or C.now_ist().strftime("%Y-%m")
    first, last = month_bounds(m); today = C.now_ist().date().isoformat(); upto = min(today, last)
    out = []
    for c in C.select("clients", "select=id,name,instagram,ig_followers,posts_per_month,follower_target_pct,smm_owner,service_type,status&status=neq.Ended"):
        ps = C.select("posts", f"select=id,date,status,live_at,type&client_id=eq.{c['id']}&date=gte.{first}&date=lte.{last}")
        due = [p for p in ps if p["date"] <= upto]
        live = [p for p in due if p["status"] == "Live"]
        on = [p for p in live if p.get("live_at") and p["live_at"][:10] <= p["date"]]
        met = C.select("post_metrics", f"select=views,likes,comments,followers_at,ig_type,ig_url&client_id=eq.{c['id']}&posted_at=gte.{first}&posted_at=lte.{last}T23:59:59")
        f = c.get("ig_followers") or 0
        hist = C.select("follower_history", f"select=date,followers&client_id=eq.{c['id']}&date=lte.{first}&order=date.desc&limit=1") or \
               C.select("follower_history", f"select=date,followers&client_id=eq.{c['id']}&order=date.asc&limit=1")
        f0 = hist[0]["followers"] if hist else None
        growth = round((f - f0) / f0 * 100, 1) if f0 and f else None
        viral = [x for x in met if (x["followers_at"] or f) and (x["views"] or 0) >= VIRAL_X * (x["followers_at"] or f)]
        reels = [x for x in met if x.get("views") is not None and (x["followers_at"] or f) and "reel" in (x.get("ig_type") or "").lower()]
        rm = round(sum(x["views"] / (x["followers_at"] or f) for x in reels) / len(reels), 2) if reels else None
        out.append({"client": c["name"], "smm_owner": c.get("smm_owner") or "Bhavana", "service_type": c.get("service_type") or "social", "instagram": c.get("instagram"), "followers": f, "planned": len(ps), "quota": c.get("posts_per_month") or 12,
                    "due_so_far": len(due), "live": len(live), "on_time_pct": round(len(on) / len(due) * 100) if due else None,
                    "ig_posts_seen": len(met), "viral": len(viral), "avg_reel_x": rm,
                    "followers_month_start": f0, "follower_growth_pct": growth, "follower_target_pct": c.get("follower_target_pct")})
    C.out({"month": m, "clients": out})


def scorecard(person, m=None):
    m = m or C.now_ist().strftime("%Y-%m")
    first, last = month_bounds(m); today = C.now_ist().date().isoformat(); upto = min(today, last)
    targets = C.select("kpi_targets", f"select=*&person_name=eq.{person}&order=weight.desc")
    posts = C.select("posts", f"select=id,client_id,date,type,status,live_at,designed_at,created_at&date=gte.{first}&date=lte.{last}")
    if person == "Shubham":
        live_clients = [c for c in C.select("clients", "select=id,posts_per_month,status&status=neq.Ended")]   # design counts for every client
    else:
        live_clients = social_clients(person)                                                                    # social KPIs only for the clients this SMM owns
    scope = {c["id"] for c in live_clients}
    posts = [p for p in posts if p["client_id"] in scope]
    due = [p for p in posts if p["date"] <= upto]
    y, mo = int(m[:4]), int(m[5:7]); pm = f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}"
    cutoff = pm + "-25T23:59:59"
    met = [x for x in C.select("post_metrics", f"select=views,likes,comments,followers_at,ig_type,client_id&posted_at=gte.{first}&posted_at=lte.{last}T23:59:59") if x["client_id"] in scope]
    fol = {c["id"]: c.get("ig_followers") or 0 for c in C.select("clients", "select=id,ig_followers")}
    def F(x): return x["followers_at"] or fol.get(x["client_id"]) or 0
    def pct(a, b): return round(a / b * 100) if b else None
    calc = {}
    ready = sum(1 for c in live_clients if sum(1 for p in posts if p["client_id"] == c["id"] and (p.get("created_at") or "") <= cutoff) >= (c.get("posts_per_month") or 12))
    calc["calendar_ready"] = pct(ready, len(live_clients))
    calc["posts_on_time"] = pct(sum(1 for p in due if p["status"] == "Live" and p.get("live_at") and p["live_at"][:10] <= p["date"]), len(due))
    calc["posts_delivered"] = pct(sum(1 for p in due if p["status"] == "Live"), len(due))
    hist = C.select("follower_history", "select=client_id,date,followers&order=date.asc")
    on_track = tot = 0
    frac = (datetime.date.fromisoformat(upto) - datetime.date.fromisoformat(first)).days / max(1, (datetime.date.fromisoformat(last) - datetime.date.fromisoformat(first)).days)
    for c in live_clients:
        if not c.get("instagram"): continue
        h = [x for x in hist if x["client_id"] == c["id"]]
        base_ = [x for x in h if x["date"] <= first] or h[:1]
        if not base_ or not c.get("ig_followers"): continue
        f0 = base_[-1]["followers"]; g = (c["ig_followers"] - f0) / f0 * 100 if f0 else 0
        tot += 1; on_track += g >= float(c.get("follower_target_pct") or 5) * frac
    calc["follower_growth"] = pct(on_track, tot)
    calc["viral_posts"] = sum(1 for x in met if F(x) and (x["views"] or 0) >= VIRAL_X * F(x)) if met else None
    reels = [x for x in met if x.get("views") is not None and F(x) and "reel" in (x.get("ig_type") or "").lower()]
    calc["reach_multiple"] = round(sum(x["views"] / F(x) for x in reels) / len(reels), 2) if reels else None
    eng = [x for x in met if F(x) and (x.get("likes") is not None or x.get("comments") is not None)]
    calc["engagement_rate"] = round(sum(((x["likes"] or 0) + (x["comments"] or 0)) / F(x) * 100 for x in eng) / len(eng), 1) if eng else None
    dz = [p for p in due if (p.get("type") or "").lower() in ("static", "carousel", "creative")]
    def minus2(d): return (datetime.date.fromisoformat(d) - datetime.timedelta(days=2)).isoformat()
    calc["design_on_time"] = pct(sum(1 for p in dz if p.get("designed_at") and p["designed_at"][:10] <= minus2(p["date"])), len(dz))
    calc["design_delivered"] = pct(sum(1 for p in dz if p.get("designed_at")), len(dz))
    ppl = C.select("people", f"select=id&name=eq.{person}"); pid = ppl[0]["id"] if ppl else None
    tk = C.select("tasks", f"select=due,status,done_at&owner_id=eq.{pid}&due=gte.{first}&due=lte.{upto}") if pid else []
    calc["tasks_on_time"] = pct(sum(1 for t in tk if t["status"] == "Done" and t.get("done_at") and t["done_at"][:10] <= t["due"]), len(tk))
    rec = [r for r in C.select("recurring", "select=*") if r.get("owner_name") in (person, "Everyone")]
    done = C.select("recur_done", f"select=recurring_id,date&by_name=eq.{person}&date=gte.{first}&date=lte.{upto}")
    dset = {(d["recurring_id"], d["date"]) for d in done}
    dueN = doneN = 0
    d = datetime.date.fromisoformat(first)
    while d.isoformat() <= upto:
        if d.weekday() != 6:
            for r in rec:
                ok = (r["freq"] == "daily") or (r["freq"] == "weekly" and (d.weekday() + 1) % 7 == int(r.get("day") or 1)) or \
                     (r["freq"] == "monthly" and d.day == min(int(r.get("day") or 1), calendar.monthrange(d.year, d.month)[1]))
                if ok:
                    dueN += 1; doneN += (r["id"], d.isoformat()) in dset
        d += datetime.timedelta(days=1)
    calc["routine_done"] = pct(doneN, dueN)
    rows, got, wsum = [], 0, 0
    for t in targets:
        v = calc.get(t["kpi"]); ach = None if v is None else min(100, round(v / float(t["target"]) * 100))
        if ach is not None: got += ach * (t.get("weight") or 1); wsum += (t.get("weight") or 1)
        rows.append({"kpi": t["kpi"], "label": t["label"], "value": v, "target": t["target"], "unit": t.get("unit"), "achieved_pct": ach, "weight": t.get("weight")})
    C.out({"person": person, "month": m, "score": round(got / wsum) if wsum else None, "kpis": rows})


def setcounts(path):
    """Save follower counts read elsewhere (e.g. from instagram.com in a browser).
    File: JSON {"handle": "1,835 Followers, 24 Following, 277 Posts ..." or 1835, ...}."""
    import re
    def num(t):
        t = str(t).replace(",", "").strip()
        m = re.match(r"([\d.]+)\s*([KkMm]?)", t)
        if not m: return None
        v = float(m.group(1)); u = m.group(2).upper()
        return int(round(v * (1000 if u == "K" else 1000000 if u == "M" else 1)))
    data = json.load(open(path)); got = {}
    for c in clients_with_ig():
        h = c["instagram"].strip().lstrip("@")
        if h not in data: continue
        raw = data[h]; f = num(raw); posts = None
        pm = re.search(r"([\d,]+)\s+Posts", str(raw))
        if pm: posts = int(pm.group(1).replace(",", ""))
        if f is not None: snapshot(c, f, posts, "browser"); got[c["name"]] = f
    missing = [c["name"] for c in clients_with_ig() if c["name"] not in got]
    C.log(AGENT, "followers", f"Follower snapshot: {len(got)} of {len(got)+len(missing)} accounts (browser)" + (f"; missing {', '.join(missing)}" if missing else ""),
          detail={"followers": got}, needs_tejas=bool(missing))
    C.out({"ok": not missing, "followers": got, "missing": missing})


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "pull": pull(int(a[1]) if len(a) > 1 else 40)
    elif a[0] == "followers": followers()
    elif a[0] == "setcounts": setcounts(a[1])
    elif a[0] == "summary": summary(a[1] if len(a) > 1 else None)
    elif a[0] == "scorecard": scorecard(a[1], a[2] if len(a) > 2 else None)
    else: print(__doc__)
