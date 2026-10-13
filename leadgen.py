"""Lead Gen agent — finds prospects, scores them, writes them into the Leads pipeline.

Usage:
  python3 leadgen.py apollo-hospitality [n]            Apollo: Mumbai restaurant/hospitality group decision-makers → food leads (draft after)
  python3 leadgen.py apollo-agencies <uk|uae|au|us> [n]   Apollo: verified agency decision-makers → leads
  python3 leadgen.py apollo-corporates [n]              Apollo: Mumbai comms/marketing/HR heads → leads
  python3 leadgen.py addagency lead.json  agency abroad (website + email + region uk|uae|au|us required)
  python3 leadgen.py addcorp lead.json   corporate prospect from web research (website + email required)
  python3 leadgen.py corptoday           corporate leads added today
  python3 leadgen.py run [segment]      segment = food | d2c | corporate | auto (weekday rotation)
  python3 leadgen.py pending            leads inserted by the agent that still need drafts
  python3 leadgen.py draft <lead_id> <email_draft_file> <whatsapp_draft_file>
  python3 leadgen.py pool                      how many pre-scraped candidates are waiting per segment
  python3 leadgen.py pool-load <items.json> <segment> <area>   score an Apify Google-Maps export into the pool
  python3 leadgen.py osm [food|corporate]      free fallback: candidate business names from OpenStreetMap for today's area
                                               (no reviews/followers — the Claude session enriches them with web search)
  python3 leadgen.py add <lead.json>           insert one enriched lead (fields: name, segment, city_area, email,
                                               contact_phone, instagram, website, rating, reviews, ig_followers, category, why)

Sources, in order: Google Places API (if GOOGLE_PLACES_KEY secret exists) → Apify Google Maps → lead_pool
(pre-scraped candidates) when scraping fails or credits are out. D2C uses Apify Instagram; falls back to pool.

`run` prints a JSON summary of the leads it inserted (with the facts a human or
Claude needs to write a personalised opener). Drafting is done by the Claude
session that runs this task, then stored with `draft`.
"""
import sys, json, re, datetime, urllib.parse
import common as C

AGENT = "Lead Gen"

# Andheri-East-centric rotation of areas; one area per run so each day's batch is coherent
FOOD_AREAS = ["Bandra West, Mumbai", "Andheri West, Mumbai", "Juhu, Mumbai", "Powai, Mumbai",
              "Lower Parel, Mumbai", "Khar West, Mumbai", "Versova, Mumbai", "Vile Parle West, Mumbai"]
FOOD_TERMS = ["cafe", "restaurant", "bakery", "bar", "brunch"]
CORP_AREAS = ["Andheri East, Mumbai", "Powai, Mumbai", "Bandra Kurla Complex, Mumbai", "Goregaon East, Mumbai",
              "Lower Parel, Mumbai", "Vikhroli, Mumbai"]
CORP_TERMS = ["corporate office", "software company", "manufacturing company", "real estate developer",
              "pharmaceutical company", "logistics company"]
D2C_HASHTAGS = ["mumbaibrand", "homegrownbrand", "madeinindia", "d2cindia", "indianbrand",
                "smallbusinessindia", "mumbaientrepreneur", "indianskincare", "indianfashionbrand"]

CHAINS = {"starbucks", "mcdonald", "domino", "kfc", "subway", "burger king", "pizza hut", "chaayos",
          "third wave", "blue tokai", "social", "barista", "cafe coffee day", "ccd", "theobroma",
          "haldiram", "wow momo", "faasos", "behrouz", "swiggy", "zomato", "justdial", "dunkin"}
AGG_DOMAINS = ("zomato", "swiggy", "justdial", "jsdl.in", "dotpe", "linktr.ee", "facebook.com", "instagram.com",
               "magicpin", "dineout", "eazydiner")


def months_left():
    return max(1, 12 - C.now_ist().month + 1)


def goal_value(kind, est):
    return est * months_left() if kind == "retainer" else est


def is_chain(name):
    n = name.lower()
    return any(c in n for c in CHAINS)


def pick(lst, salt=0):
    d = C.now_ist().timetuple().tm_yday + salt
    return lst[d % len(lst)]


MIN_FOOD_REVIEWS = 300          # below this a restaurant rarely has ₹45k/month for marketing
PREMIUM_CATS = ("bar", "pub", "brewery", "lounge", "fine dining", "bistro", "steak", "sushi", "japanese", "italian",
                "european", "rooftop", "cocktail", "wine", "bakery chain", "patisserie")

# ------------------------------------------------------------------ food + corporate (Google Maps)
def gmaps(segment):
    area = pick(FOOD_AREAS if segment == "food" else CORP_AREAS)
    terms = FOOD_TERMS if segment == "food" else CORP_TERMS
    items = C.apify_run("compass/crawler-google-places", {
        "searchStringsArray": terms, "locationQuery": area,
        "maxCrawledPlacesPerSearch": 30 if segment == "food" else 20, "language": "en",
        "skipClosedPlaces": True, "placeMinimumStars": "threeAndHalf" if segment == "food" else "",
        "website": "allPlaces" if segment == "food" else "withWebsite",
        "scrapeContacts": True,
        "scrapeSocialMediaProfiles": {"instagrams": True, "facebooks": False, "youtubes": False,
                                      "tiktoks": False, "twitters": False},
    }, wait=280, max_usd=1.5)
    return score_gmaps(items, segment, area), area


def score_gmaps(items, segment, area):
    """Filter + score Google-Maps-shaped items (Apify format). Returns candidate dicts."""
    out = []
    for it in items:
        name = (it.get("title") or "").strip()
        if not name or is_chain(name):
            continue
        reviews = it.get("reviewsCount") or 0
        rating = it.get("totalScore") or 0
        website = it.get("website") or ""
        if any(a in website for a in AGG_DOMAINS):
            website = ""
        igs = [u for u in (it.get("instagrams") or []) if not any(a in u for a in ("swiggy", "zomato", "justdial"))]
        ig_profiles = it.get("instagramProfiles") or []
        ig = igs[0] if igs else ""
        ig_user = re.sub(r".*instagram\.com/", "", ig).strip("/").split("?")[0] if ig else ""
        followers = posts = None
        for p in ig_profiles:
            if p.get("username") and (not ig_user or p["username"].lower() == ig_user.lower()):
                followers, posts = p.get("followersCount"), p.get("postsCount")
                ig_user = p["username"]
                break
        emails = [e for e in (it.get("emails") or []) if not any(a in e for a in ("swiggy", "zomato", "justdial"))]
        phone = C.norm_phone(it.get("phone") or "")

        # ---- hard filters
        if segment == "food":
            # ICP (Tejas, 24 Sep): must be able to afford a ₹45k/month retainer — tiny single-counter shops are out
            if reviews < MIN_FOOD_REVIEWS:
                continue
            if not (website or ig_user or emails):
                continue
        elif segment == "hospitality":
            # Konkan luxury (Tejas, 13 Oct 2026): resorts, boutique/4-5 star hotels, villas, fine dine — must be able to pay
            if reviews < 150 or not (website or ig_user or emails):
                continue
            if not (emails or phone or ig_user):
                continue
        else:
            if reviews < 5 or not website:
                continue
            if not (emails or phone):
                continue

        # ---- score 0-100: reachability + size + social gap
        score = 30
        why = []
        if emails: score += 15; why.append("email found")
        if phone: score += 10
        if ig_user: score += 10; why.append(f"IG @{ig_user}")
        if website: score += 5
        if segment == "food":
            # budget fit first: footfall (review volume) is the best public proxy for revenue
            if reviews >= 3000: score += 25; why.append(f"{reviews} reviews — high footfall, budget fit")
            elif reviews >= 1500: score += 20; why.append(f"{reviews} reviews — strong footfall")
            elif reviews >= 800: score += 14; why.append(f"{reviews} reviews — established")
            else: score += 6; why.append(f"{reviews} reviews")
            cat = (it.get("categoryName") or "").lower()
            if any(k in cat for k in PREMIUM_CATS): score += 6; why.append(f"premium format ({it.get('categoryName')})")
            if rating and rating >= 4.2: score += 4
            # the social gap still matters, but less than the ability to pay
            if followers is not None:
                if followers < 3000: score += 8; why.append(f"only {followers} IG followers — growth gap")
                elif followers < 15000: score += 5; why.append(f"{followers} IG followers")
                elif followers > 60000: score -= 8; why.append("large following — likely has an agency")
            elif not ig_user:
                score += 6; why.append("no Instagram found — clear gap")
            if rating and rating < 4.0: why.append(f"rating {rating} — reputation content angle")
        elif segment == "hospitality":
            cat = (it.get("categoryName") or "").lower() + " " + name.lower()
            if any(k in cat for k in ("resort", "villa", "boutique", "5-star", "5 star", "4-star", "4 star", "luxury", "beach", "spa", "fine din", "wellness")):
                score += 12; why.append(f"premium format ({it.get('categoryName') or 'resort/villa'})")
            if reviews >= 2000: score += 20; why.append(f"{reviews} reviews — high occupancy, budget fit")
            elif reviews >= 800: score += 14; why.append(f"{reviews} reviews — established")
            else: score += 6; why.append(f"{reviews} reviews")
            if rating and rating >= 4.3: score += 5
            if it.get("tariff"): why.append(f"tariff {it['tariff']}")
            if followers is not None:
                if followers < 5000: score += 8; why.append(f"only {followers} IG followers — growth gap")
                elif followers > 80000: score -= 8; why.append("large following — likely has an agency")
            elif not ig_user:
                score += 6; why.append("no Instagram found — clear gap")
        else:
            if not ig_user: score += 12; why.append("no Instagram presence — video/brand gap")
            elif followers is not None and followers < 2000: score += 10; why.append(f"only {followers} IG followers")
            score += 5
            why.append(f"category: {it.get('categoryName')}")
        score = max(0, min(100, score))

        out.append({
            "dedupe_key": "gmaps:" + it.get("placeId", name.lower()),
            "name": name, "segment": segment, "source": "AI Lead Gen (Google Maps)",
            "city_area": (it.get("neighborhood") or area).split(",")[-1].strip() if segment == "food" else (it.get("neighborhood") or area.split(",")[0]) if segment == "hospitality" else area.split(",")[0],
            "email": emails[0] if emails else "", "contact_phone": phone, "instagram": ig_user, "website": website,
            "score": score, "why": "; ".join(why),
            "agent_notes": json.dumps({"category": it.get("categoryName"), "rating": rating, "reviews": reviews,
                                       "ig_followers": followers, "ig_posts": posts, "address": it.get("address"),
                                       "maps_url": it.get("url")}, ensure_ascii=False),
        })
    return out


# ------------------------------------------------------------------ Google Places (New) provider — used when GOOGLE_PLACES_KEY exists
def google_places(segment):
    """Text Search via Google Places API (New); enriches email/Instagram by reading the website."""
    key = C.secret("GOOGLE_PLACES_KEY", required=False)
    if not key:
        raise RuntimeError("no GOOGLE_PLACES_KEY")
    area = pick(FOOD_AREAS if segment == "food" else CORP_AREAS)
    terms = FOOD_TERMS if segment == "food" else CORP_TERMS
    items = []
    for t in terms:
        body = {"textQuery": f"{t} in {area}", "pageSize": 20, "languageCode": "en"}
        j = C.http("POST", "https://places.googleapis.com/v1/places:searchText", body,
                   {"X-Goog-Api-Key": key,
                    "X-Goog-FieldMask": "places.id,places.displayName,places.rating,places.userRatingCount,places.websiteUri,"
                                        "places.nationalPhoneNumber,places.formattedAddress,places.primaryTypeDisplayName,places.googleMapsUri,places.businessStatus"})
        for p in j.get("places", []):
            if p.get("businessStatus") not in (None, "OPERATIONAL"):
                continue
            items.append({"placeId": p["id"], "title": p.get("displayName", {}).get("text"), "totalScore": p.get("rating"),
                          "reviewsCount": p.get("userRatingCount"), "website": p.get("websiteUri"), "phone": p.get("nationalPhoneNumber"),
                          "address": p.get("formattedAddress"), "categoryName": p.get("primaryTypeDisplayName", {}).get("text"),
                          "url": p.get("googleMapsUri"), "neighborhood": area, "emails": [], "instagrams": [], "instagramProfiles": []})
    # enrich from website (free): find emails + instagram links
    seen = set(); uniq = []
    for it in items:
        if it["placeId"] in seen: continue
        seen.add(it["placeId"]); uniq.append(it)
    for it in uniq:
        w = it.get("website")
        if not w or any(a in w for a in AGG_DOMAINS):
            continue
        try:
            html = C.http("GET", w, headers={"User-Agent": "Mozilla/5.0"}, timeout=12, raw=True)[:400000]
        except Exception:
            continue
        it["emails"] = list(dict.fromkeys(e for e in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", html)
                                          if not e.lower().endswith((".png", ".jpg", ".svg", ".gif")) and "example" not in e))[:3]
        it["instagrams"] = list(dict.fromkeys(re.findall(r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.]+", html)))[:2]
    return score_gmaps(uniq, segment, area), area


# ------------------------------------------------------------------ OpenStreetMap (free, no key) — names only
AREA_LL = {"Bandra West, Mumbai": (19.0596, 72.8295), "Andheri West, Mumbai": (19.1360, 72.8296), "Juhu, Mumbai": (19.1075, 72.8263),
           "Powai, Mumbai": (19.1176, 72.9060), "Lower Parel, Mumbai": (18.9960, 72.8300), "Khar West, Mumbai": (19.0700, 72.8340),
           "Versova, Mumbai": (19.1310, 72.8140), "Vile Parle West, Mumbai": (19.0990, 72.8380), "Andheri East, Mumbai": (19.1136, 72.8697),
           "Bandra Kurla Complex, Mumbai": (19.0653, 72.8687), "Goregaon East, Mumbai": (19.1650, 72.8590), "Vikhroli, Mumbai": (19.1100, 72.9270)}
OVERPASS = ["https://maps.mail.ru/osm/tools/overpass/api/interpreter", "https://overpass.private.coffee/api/interpreter",
            "https://overpass-api.de/api/interpreter"]


def osm(segment, area=None):
    """Candidate businesses from OpenStreetMap around today's area. Free; returns names + whatever contact tags exist.
    The calling Claude session must enrich (Google rating/reviews, Instagram + followers, email, phone) via web search."""
    area = area or pick(FOOD_AREAS if segment == "food" else CORP_AREAS)
    lat, lng = AREA_LL.get(area, (19.1136, 72.8697))
    if segment == "food":
        q = f'[out:json][timeout:40];(nwr["amenity"~"^(restaurant|cafe|bar|pub|ice_cream)$"]["name"](around:1800,{lat},{lng});' \
            f'nwr["shop"~"^(bakery|pastry|confectionery|chocolate)$"]["name"](around:1800,{lat},{lng}););out center tags;'
    else:
        q = f'[out:json][timeout:40];(nwr["office"]["name"](around:2000,{lat},{lng});' \
            f'nwr["industrial"]["name"](around:2000,{lat},{lng});nwr["craft"]["name"](around:2000,{lat},{lng}););out center tags;'
    data = None
    for url in OVERPASS:
        try:
            data = C.http("POST", url, "data=" + urllib.parse.quote(q), {"Content-Type": "application/x-www-form-urlencoded"}, timeout=50)
            if isinstance(data, dict) and "elements" in data:
                break
            data = None
        except Exception as e:
            print("overpass", url.split("/")[2], "failed:", str(e)[:80], file=sys.stderr)
    if not data:
        raise RuntimeError("all Overpass mirrors failed")
    known = {(r["name"] or "").lower() for r in C.select("leads", "select=name")}
    known |= {(r["name"] or "").lower() for r in C.select("clients", "select=name")}
    seen, out = set(), []
    for e in data.get("elements", []):
        t = e.get("tags", {})
        name = t.get("name", "").strip()
        if not name or name.lower() in seen or name.lower() in known or is_chain(name):
            continue
        seen.add(name.lower())
        if segment != "food" and not (t.get("office") or t.get("industrial") or t.get("craft")):
            continue
        out.append({"name": name, "segment": segment, "city_area": area.split(",")[0],
                    "category": t.get("cuisine") or t.get("amenity") or t.get("shop") or t.get("office") or t.get("industrial") or t.get("craft"),
                    "phone": C.norm_phone((t.get("phone") or t.get("contact:phone") or "").split(";")[0]),
                    "website": t.get("website") or t.get("contact:website") or "",
                    "instagram": (t.get("contact:instagram") or "").rstrip("/").split("/")[-1].lstrip("@"),
                    "email": t.get("email") or t.get("contact:email") or "",
                    "address": ", ".join(x for x in (t.get("addr:housename"), t.get("addr:street"), t.get("addr:suburb")) if x)})
    # things with a contact channel first, then everything else
    out.sort(key=lambda c: -(bool(c["instagram"]) * 3 + bool(c["website"]) * 2 + bool(c["email"]) * 2 + bool(c["phone"])))
    return out, area


def add(json_file):
    """Insert one lead the Claude session enriched by hand (web search). Scores it like the Google-Maps path."""
    d = json.load(open(json_file))
    seg = d.get("segment", "food")
    item = {"placeId": "osm:" + re.sub(r"[^a-z0-9]+", "-", d["name"].lower()).strip("-"), "title": d["name"],
            "totalScore": d.get("rating") or 0, "reviewsCount": d.get("reviews") or 0, "website": d.get("website") or "",
            "phone": d.get("contact_phone") or "", "address": d.get("address") or "", "categoryName": d.get("category") or "",
            "url": d.get("maps_url") or "", "neighborhood": d.get("city_area") or "Mumbai", "tariff": d.get("tariff"),
            "emails": [d["email"]] if d.get("email") else [],
            "instagrams": [f"https://instagram.com/{d['instagram']}"] if d.get("instagram") else [],
            "instagramProfiles": [{"username": d["instagram"], "followersCount": d.get("ig_followers"), "postsCount": d.get("ig_posts")}]
                                 if d.get("instagram") else []}
    cands = score_gmaps([item], seg, (d.get("city_area") or "Mumbai") + (", Konkan" if seg == "hospitality" else ", Mumbai"))
    if not cands:
        sys.exit("rejected by filters (food: ≥300 reviews + website/Instagram/email; hospitality: ≥150 reviews + a contact channel; corporate: website + email/phone)")
    c = cands[0]
    c["source"] = "AI Lead Gen (OSM + web)"
    if d.get("why"):
        c["why"] = (c["why"] + "; " + d["why"]).strip("; ")
    existing = {r["dedupe_key"] for r in C.select("leads", "select=dedupe_key&dedupe_key=not.is.null")}
    names = {(r["name"] or "").lower() for r in C.select("leads", "select=name")}
    if c["dedupe_key"] in existing or c["name"].lower() in names:
        sys.exit("duplicate — already in the pipeline")
    est = EST[seg]
    row = {**c, "contact_name": d.get("contact_name", ""), "service": "Social media management + content" if seg != "corporate" else "Video production (corporate)",
           "kind": "retainer", "est_value": est, "goal_value": goal_value("retainer", est), "stage": "New", "owner_name": "Tejas",
           "next_date": C.now_ist().date().isoformat(), "next_action": "AI: draft + send first outreach", "notes": ""}
    ins = C.insert("leads", [row])
    C.log(AGENT, "scrape", f"{seg} @ {c['city_area']}: added {c['name']} (score {c['score']}) from OSM + web enrichment")
    C.out({k: ins[0].get(k) for k in ("id", "name", "score", "why", "email", "contact_phone", "instagram", "website", "city_area", "agent_notes")})


# ------------------------------------------------------------------ lead pool (pre-scraped candidates; used when scraping is unavailable)
def pool_load(json_file, segment, area):
    items = json.load(open(json_file))
    cands = score_gmaps(items, segment, area)
    rows = [{**{k: v for k, v in c.items() if k != "source"}, "area": area} for c in cands]
    n = 0
    for i in range(0, len(rows), 50):
        r = C.insert("lead_pool", rows[i:i + 50], upsert_on="dedupe_key")
        n += len(r or [])
    C.out({"scored": len(cands), "added_to_pool": n})


def pool_status():
    rows = C.select("lead_pool", "select=segment,used")
    st = {}
    for r in rows:
        k = f"{r['segment']}:{'used' if r['used'] else 'available'}"
        st[k] = st.get(k, 0) + 1
    C.out(st)


def from_pool(segment, target):
    rows = C.select("lead_pool", f"select=*&segment=eq.{segment}&used=eq.false&order=score.desc&limit={target * 3}")
    where = "lead pool (" + ", ".join(sorted({r.get('area') or '' for r in rows})) + ")"
    cands = [{k: r.get(k) for k in ("dedupe_key", "name", "segment", "email", "contact_phone", "instagram", "website",
                                    "city_area", "score", "why", "agent_notes")} for r in rows]
    for c in cands:
        c["source"] = "AI Lead Gen (Google Maps)"
    return cands, where


# ------------------------------------------------------------------ D2C (Instagram)
def d2c():
    tags = [pick(D2C_HASHTAGS), pick(D2C_HASHTAGS, 3)]
    posts = C.apify_run("apify/instagram-hashtag-scraper",
                        {"hashtags": tags, "resultsType": "posts", "resultsLimit": 60}, wait=240, max_usd=0.6)
    users = {}
    for p in posts:
        u = p.get("ownerUsername")
        if u and u not in users:
            users[u] = p
    users = dict(list(users.items())[:40])
    if not users:
        return [], ",".join(tags)
    profiles = C.apify_run("apify/instagram-profile-scraper", {"usernames": list(users)}, wait=240, max_usd=0.6)
    out = []
    for pr in profiles:
        u = pr.get("username")
        if not u:
            continue
        bio = (pr.get("biography") or "")
        followers = pr.get("followersCount") or 0
        posts_n = pr.get("postsCount") or 0
        is_biz = pr.get("isBusinessAccount") or bool(pr.get("businessCategoryName"))
        ext = pr.get("externalUrl") or ""
        text = (bio + " " + (pr.get("fullName") or "") + " " + (pr.get("businessCategoryName") or "")).lower()
        brandish = any(k in text for k in ("shop", "order", "ship", "store", "brand", "handmade", "skincare",
                                            "apparel", "clothing", "jewel", "cod", "dm to order", "website",
                                            "founded", "since", "delivery", "pan india"))
        if not (is_biz or brandish):
            continue
        if followers < 800 or followers > 150000:
            continue
        mumbai = "mumbai" in text or "bombay" in text
        score = 35 + (15 if ext else 0) + (10 if mumbai else 0) + (10 if is_biz else 0)
        why = []
        if ext: why.append("has website/shop link")
        if mumbai: why.append("Mumbai-based")
        if followers < 5000: score += 15; why.append(f"{followers} followers — early stage, needs content")
        elif followers < 30000: score += 10; why.append(f"{followers} followers — scaling")
        else: why.append(f"{followers} followers")
        if posts_n < 100: score += 5; why.append("thin content library")
        email = ""
        m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", bio)
        if m: email = m.group(0); score += 15; why.append("email in bio")
        out.append({
            "dedupe_key": "ig:" + u.lower(), "name": pr.get("fullName") or u, "segment": "d2c",
            "source": "AI Lead Gen (Instagram)", "city_area": "Mumbai" if mumbai else "India",
            "email": email, "contact_phone": "", "instagram": u, "website": ext,
            "score": max(0, min(100, score)), "why": "; ".join(why),
            "agent_notes": json.dumps({"bio": bio[:400], "followers": followers, "posts": posts_n,
                                       "category": pr.get("businessCategoryName"), "hashtags": tags}, ensure_ascii=False),
        })
    return out, ",".join(tags)


# ------------------------------------------------------------------ main
EST = {"food": 45000, "d2c": 45000, "corporate": 45000, "hospitality": 45000}   # minimum ticket size per client (Tejas, Sept 2026)


def run(segment):
    if segment == "auto":
        segment = ["food", "d2c", "food", "d2c", "food", "food", "food"][C.now_ist().weekday()]  # corporate runs as its own daily task (addcorp)
    target = int(C.config("lead_gen_daily_target", 15))
    cands, where, used_pool = [], "", False
    try:
        if segment == "d2c":
            cands, where = d2c()
        elif C.secrets().get("GOOGLE_PLACES_KEY"):
            cands, where = google_places(segment)
        else:
            cands, where = gmaps(segment)
    except Exception as e:
        print("scrape unavailable:", str(e)[:200], "— falling back to lead pool", file=sys.stderr)
        seg = "food" if segment == "d2c" else segment
        cands, where = from_pool(seg, target)
        segment, used_pool = seg, True
        if not cands and seg != "corporate":
            cands, where = from_pool("food", target)
            segment = "food"
            used_pool = True
    existing = {r["dedupe_key"] for r in C.select("leads", "select=dedupe_key&dedupe_key=not.is.null")}
    names = {(r["name"] or "").lower() for r in C.select("leads", "select=name")}
    names |= {(r["name"] or "").lower() for r in C.select("clients", "select=name")}
    fresh = [c for c in cands if c["dedupe_key"] not in existing and c["name"].lower() not in names]
    if used_pool and not fresh:
        # pool is dry and no scraper: hand candidate names from OpenStreetMap to the Claude session for web enrichment
        seg = "food" if segment == "d2c" else segment
        try:
            names_out, area = osm(seg)
        except Exception as e:
            names_out, area = [], str(e)
        C.log(AGENT, "pool", f"{seg}: pool empty, scraping unavailable — {len(names_out)} OSM candidates @ {area} handed over for web enrichment")
        C.out({"segment": seg, "where": f"OpenStreetMap ({area})", "candidates": len(names_out), "inserted": 0, "leads": [],
               "needs_enrichment": names_out[:40],
               "instruction": "No scraper and the pool is empty. For up to 15 of these names (contactable ones first), use web search to find: "
                              "Google rating + review count, Instagram handle + follower count, website, email, phone. Then write each as "
                              "JSON and run `python3 leadgen.py add lead.json`. Skip anything you cannot verify."})
        return
    fresh.sort(key=lambda c: -c["score"])
    chosen = fresh[:target]
    rows = []
    for c in chosen:
        est = EST[segment]
        rows.append({**c, "contact_name": "", "service": "Social media management + content"
                     if segment != "corporate" else "Video production (corporate)",
                     "kind": "retainer", "est_value": est, "goal_value": goal_value("retainer", est),
                     "stage": "New", "owner_name": "Tejas", "next_date": C.now_ist().date().isoformat(),
                     "next_action": "AI: draft + send first outreach", "notes": ""})
    inserted = C.insert("leads", rows) if rows else []
    if used_pool and inserted:
        for r in inserted:
            C.update("lead_pool", f"dedupe_key=eq.{r['dedupe_key']}", {"used": True})
    C.log(AGENT, "scrape" if not used_pool else "pool", f"{segment} @ {where}: {len(cands)} candidates, {len(fresh)} new, {len(inserted)} inserted",
          detail={"segment": segment, "where": where, "candidates": len(cands), "inserted": len(inserted),
                  "skipped_duplicates": len(cands) - len(fresh)})
    C.out({"segment": segment, "where": where, "candidates": len(cands), "inserted": len(inserted),
           "leads": [{k: r.get(k) for k in ("id", "name", "segment", "score", "why", "email", "contact_phone",
                                             "instagram", "website", "city_area", "agent_notes")} for r in inserted]})


def pending():
    rows = C.select("leads", "select=id,name,segment,score,why,email,contact_phone,instagram,website,city_area,agent_notes"
                             "&source=like.AI%20Lead%20Gen*&email_draft=is.null&stage=eq.New&order=score.desc&limit=30")
    C.out(rows)


def draft(lead_id, email_file, wa_file):
    e = open(email_file, encoding="utf-8").read().strip()
    w = open(wa_file, encoding="utf-8").read().strip()
    cur = C.select("leads", f"select=name,segment,contact_name&id=eq.{lead_id}")
    if cur and cur[0].get("segment") == "corporate":   # corporate = fixed service email (Tejas, 13 Oct)
        e, w = C.corp_email(cur[0]["name"], cur[0].get("contact_name")), C.corp_wa(cur[0]["name"], cur[0].get("contact_name"))
    r = C.update("leads", f"id=eq.{lead_id}", {"email_draft": e, "whatsapp_draft": w})
    C.log(AGENT, "draft", f"Drafts written for {r[0]['name'] if r else lead_id}", lead_id=lead_id)
    C.out({"ok": True, "lead": r[0]["name"] if r else None})


# ------------------------------------------------------------------ corporate track (Tejas, Oct 2026): leadership video, employee video, leadership podcasts
CORP_SERVICE = "Leadership video + podcast (corporate)"
CORP_ROLES = ("marketing", "brand", "communication", "corporate comm", "hr", "human resource", "people", "talent",
              "employer brand", "culture", "ceo", "founder", "managing director", "director", "head", "chief", "cmo", "chro")


def addcorp(json_file):
    """Insert one corporate prospect the Claude session researched on the web.
    JSON: name, website, email, contact_name, contact_role, contact_phone, linkedin, employees, industry, city_area,
          has_podcast (bool), has_youtube (bool), why, hook (the specific video/podcast idea for them)."""
    d = json.load(open(json_file))
    name = (d.get("name") or "").strip()
    web = (d.get("website") or "").strip()
    email = (d.get("email") or "").strip()
    if not name or not web or not email or "@" not in email:
        sys.exit("rejected: corporate leads need a company website AND an email address")
    if is_chain(name):
        sys.exit("rejected: chain/aggregator")
    role = (d.get("contact_role") or "").lower()
    local = email.split("@")[0].lower()
    generic = local in ("info", "contact", "hello", "enquiry", "enquiries", "sales", "support", "admin", "office", "mail", "careers", "jobs", "hr")
    score, why = 40, []
    if not generic: score += 20; why.append(f"named contact email ({d.get('contact_name') or local})")
    else: score += 6; why.append(f"generic email ({local}@)")
    if any(k in role for k in CORP_ROLES): score += 10; why.append(f"decision-maker role: {d.get('contact_role')}")
    try: emp = int(str(d.get("employees") or "0").replace(",", "").split("-")[-1].strip("+ ") or 0)
    except ValueError: emp = 0
    if emp >= 1000: score += 15; why.append(f"~{d.get('employees')} employees")
    elif emp >= 200: score += 12; why.append(f"~{d.get('employees')} employees")
    elif emp >= 50: score += 6; why.append(f"~{d.get('employees')} employees")
    if d.get("has_podcast") is False: score += 5; why.append("no leadership podcast yet")
    if d.get("has_youtube") is False: score += 4; why.append("little/no video on YouTube")
    if d.get("industry"): why.append(f"industry: {d['industry']}")
    if d.get("why"): why.append(d["why"])
    score = max(0, min(100, score))
    key = "corp:" + re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://(www\.)?", "", web.lower()).split("/")[0]).strip("-")
    existing = {r["dedupe_key"] for r in C.select("leads", "select=dedupe_key&dedupe_key=not.is.null")}
    names = {(r["name"] or "").lower() for r in C.select("leads", "select=name")}
    names |= {(r["name"] or "").lower() for r in C.select("clients", "select=name")}
    if key in existing or name.lower() in names:
        sys.exit("duplicate — already in the pipeline")
    est = EST["corporate"]
    notes = {k: d.get(k) for k in ("contact_role", "linkedin", "employees", "industry", "has_podcast", "has_youtube", "hook", "address")}
    row = {"dedupe_key": key, "name": name, "segment": "corporate", "source": "AI Lead Gen (corporate web research)",
           "city_area": d.get("city_area") or "Mumbai", "email": email, "contact_phone": C.norm_phone(d.get("contact_phone") or ""),
           "instagram": d.get("instagram") or "", "website": web, "score": score, "why": "; ".join(why),
           "agent_notes": json.dumps(notes, ensure_ascii=False), "contact_name": d.get("contact_name") or "",
           "service": CORP_SERVICE, "kind": "retainer", "est_value": est, "goal_value": goal_value("retainer", est),
           "stage": "New", "owner_name": "Tejas", "next_date": C.now_ist().date().isoformat(),
           "next_action": "AI: draft + send first outreach", "notes": ""}
    row["email_draft"] = C.corp_email(name, row["contact_name"])
    row["whatsapp_draft"] = C.corp_wa(name, row["contact_name"])
    ins = C.insert("leads", [row])
    C.log(AGENT, "scrape", f"corporate @ {row['city_area']}: added {name} (score {score}) from web research — standard corporate email drafted")
    C.out({k: ins[0].get(k) for k in ("id", "name", "score", "why", "email", "contact_name", "website", "city_area", "agent_notes")})


def corp_count_today():
    today = C.now_ist().date().isoformat()
    rows = C.select("leads", f"select=id,name&segment=eq.corporate&created_at=gte.{today}T00:00:00%2B05:30")
    C.out({"corporate_added_today": len(rows), "names": [r["name"] for r in rows]})


# ------------------------------------------------------------------ international agencies (Tejas, 13 Oct 2026): white-label editing / design / social support
AGENCY_SERVICE = "White-label production (international agency)"


def addagency(json_file):
    """Insert one agency abroad researched on the web.
    JSON: name, website, email, contact_name, contact_role, region (uk|uae|au|us), city, country, size (team size or
          employee range), services (what the agency does), linkedin, why."""
    d = json.load(open(json_file))
    name = (d.get("name") or "").strip()
    web = (d.get("website") or "").strip()
    email = (d.get("email") or "").strip()
    region = (d.get("region") or "").strip().lower()
    if not name or not web or "@" not in email:
        sys.exit("rejected: agency leads need a website AND a published email")
    if region not in C.AGENCY_REGIONS:
        sys.exit("rejected: region must be one of uk, uae, au, us")
    local = email.split("@")[0].lower()
    generic = local in ("info", "contact", "hello", "hi", "enquiry", "enquiries", "sales", "support", "admin", "office", "mail", "team", "studio", "careers", "jobs")
    role = (d.get("contact_role") or "").lower()
    score, why = 40, [C.AGENCY_REGIONS[region]]
    if not generic: score += 20; why.append(f"named contact email ({d.get('contact_name') or local})")
    else: score += 8; why.append(f"generic email ({local}@)")
    if any(k in role for k in ("founder", "owner", "director", "ceo", "managing", "head", "partner", "principal", "producer", "operations")):
        score += 10; why.append(f"decision-maker: {d.get('contact_role')}")
    try: size = int(str(d.get("size") or "0").replace(",", "").split("-")[-1].strip("+ ") or 0)
    except ValueError: size = 0
    if 5 <= size <= 50: score += 15; why.append(f"team ~{d.get('size')} — right size to outsource")
    elif size > 50: score += 8; why.append(f"team ~{d.get('size')}")
    elif size: score += 3; why.append(f"team ~{d.get('size')}")
    svc = (d.get("services") or "").lower()
    if any(k in svc for k in ("video", "content", "social", "creative", "production")): score += 7; why.append("video/social/creative agency")
    if d.get("why"): why.append(d["why"])
    score = max(0, min(100, score))
    key = "agency:" + re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://(www\.)?", "", web.lower()).split("/")[0]).strip("-")
    existing = {r["dedupe_key"] for r in C.select("leads", "select=dedupe_key&dedupe_key=not.is.null")}
    names = {(r["name"] or "").lower() for r in C.select("leads", "select=name")}
    if key in existing or name.lower() in names:
        sys.exit("duplicate — already in the pipeline")
    est = EST["corporate"]
    notes = {"region": region, "country": d.get("country"), "contact_role": d.get("contact_role"), "linkedin": d.get("linkedin"),
             "size": d.get("size"), "services": d.get("services")}
    row = {"dedupe_key": key, "name": name, "segment": "agency", "source": "AI Lead Gen (agency web research)",
           "city_area": ", ".join(x for x in (d.get("city"), d.get("country")) if x) or C.AGENCY_REGIONS[region],
           "email": email, "contact_phone": "", "instagram": d.get("instagram") or "", "website": web,
           "score": score, "why": "; ".join(why), "agent_notes": json.dumps(notes, ensure_ascii=False),
           "contact_name": d.get("contact_name") or "", "service": AGENCY_SERVICE, "kind": "retainer",
           "est_value": est, "goal_value": goal_value("retainer", est), "stage": "New", "owner_name": "Tejas",
           "next_date": C.now_ist().date().isoformat(), "next_action": "AI: send first outreach (international)", "notes": "",
           "email_draft": C.agency_email(name, d.get("contact_name") or ""), "whatsapp_draft": ""}
    ins = C.insert("leads", [row])
    C.log(AGENT, "scrape", f"agency ({C.AGENCY_REGIONS[region]}) @ {row['city_area']}: added {name} (score {score}) — white-label email drafted")
    C.out({k: ins[0].get(k) for k in ("id", "name", "score", "why", "email", "contact_name", "website", "city_area")})


def agency_today():
    today = C.now_ist().date().isoformat()
    rows = C.select("leads", f"select=name,city_area&segment=eq.agency&created_at=gte.{today}T00:00:00%2B05:30")
    C.out({"agencies_added_today": len(rows), "names": [f"{r['name']} ({r['city_area']})" for r in rows]})


# ------------------------------------------------------------------ Apollo (Tejas, 13 Oct 2026): verified decision-maker emails for agencies abroad + Mumbai corporates
APOLLO_REGION_LOCS = {
    "uk": ["London, United Kingdom", "Manchester, United Kingdom", "Birmingham, United Kingdom", "Leeds, United Kingdom",
           "Bristol, United Kingdom", "Edinburgh, United Kingdom", "Dublin, Ireland"],
    "uae": ["Dubai, United Arab Emirates", "Abu Dhabi, United Arab Emirates"],
    "au": ["Sydney, Australia", "Melbourne, Australia", "Brisbane, Australia", "Perth, Australia", "Auckland, New Zealand"],
    "us": ["New York, United States", "Toronto, Canada", "Miami, United States", "Austin, United States",
           "Chicago, United States", "Los Angeles, United States", "Vancouver, Canada"],
}
APOLLO_AGENCY_KW = ["social media agency", "creative agency", "video production", "digital marketing agency", "content agency", "branding agency"]
APOLLO_AGENCY_TITLES = ["founder", "co-founder", "managing director", "owner", "head of production", "creative director", "operations director"]
APOLLO_CORP_TITLES = ["head of corporate communications", "corporate communications", "head of marketing", "marketing head",
                      "chief marketing officer", "chief human resources officer", "head of human resources", "employer branding",
                      "internal communications", "head of brand"]
APOLLO_CORP_INDUSTRIES = ["information technology & services", "computer software", "financial services", "banking", "insurance",
                          "pharmaceuticals", "hospital & health care", "real estate", "logistics & supply chain", "manufacturing",
                          "management consulting", "media production", "education management", "hospitality"]


def _apollo(path, body):
    key = C.secrets().get("APOLLO_KEY")
    if not key:
        raise RuntimeError("APOLLO_KEY secret missing")
    return C.http("POST", "https://api.apollo.io/api/v1/" + path, body,
                  {"Content-Type": "application/json", "Cache-Control": "no-cache", "x-api-key": key}, timeout=60)


def _apollo_reveal(pid):
    """1 credit per reveal. Returns (email, email_status, person dict)."""
    r = _apollo("people/match", {"id": pid, "reveal_personal_emails": False, "reveal_phone_number": False})
    p = (r or {}).get("person") or {}
    return p.get("email") or "", p.get("email_status") or "", p


def apollo_agencies(region, want=12, page=None):
    """Find agency decision-makers in a region via Apollo and insert them as 'agency' leads (standard white-label email drafted).
    Only reveals contacts whose email Apollo marks verified. Stops at `want` inserted or 3× want reveals."""
    region = region.lower()
    locs = APOLLO_REGION_LOCS[region]
    page = page or (int(C.config("apollo_page_" + region, 0)) + 1)
    r = _apollo("mixed_people/api_search", {
        "person_titles": APOLLO_AGENCY_TITLES, "person_locations": locs,
        "q_organization_keyword_tags": APOLLO_AGENCY_KW,
        "organization_num_employees_ranges": ["1,10", "11,20", "21,50"],
        "contact_email_status": ["verified"], "page": page, "per_page": 25})
    people = (r or {}).get("people") or []
    existing = {x["dedupe_key"] for x in C.select("leads", "select=dedupe_key&dedupe_key=not.is.null")}
    names = {(x["name"] or "").lower() for x in C.select("leads", "select=name")}
    inserted, reveals, skipped = [], 0, []
    for p in people:
        if len(inserted) >= want or reveals >= want * 3:
            break
        org = p.get("organization") or {}
        oname = (org.get("name") or "").strip()
        if not oname or is_chain(oname) or oname.lower() in names or not p.get("has_email"):
            skipped.append((oname or "?") + (" (dup)" if oname.lower() in names else "")); continue
        reveals += 1
        email, status, full = _apollo_reveal(p["id"])
        org = full.get("organization") or org
        web = (org.get("website_url") or "").strip()
        if not email or status != "verified" or not web:
            skipped.append(oname + (" (no verified email)" if not email or status != "verified" else " (no website)")); continue
        key = "agency:" + re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://(www\.)?", "", web.lower()).split("/")[0]).strip("-")
        if key in existing:
            skipped.append(oname + " (dup)"); continue
        d = {"name": oname, "website": web, "email": email, "contact_name": full.get("name") or p.get("name") or "",
             "contact_role": full.get("title") or p.get("title") or "", "region": region,
             "city": full.get("city") or p.get("city") or "", "country": full.get("country") or p.get("country") or "",
             "size": org.get("estimated_num_employees") or "", "services": ", ".join(org.get("keywords", [])[:6]) if org.get("keywords") else "agency",
             "linkedin": full.get("linkedin_url") or "", "why": "Apollo verified contact"}
        json.dump(d, open("_ap.json", "w"))
        try:
            _addagency_quiet(d)
            inserted.append(f"{oname} — {d['contact_name']} ({d['contact_role']}) <{email}>")
            existing.add(key); names.add(oname.lower())
        except SystemExit as e:
            skipped.append(f"{oname} ({e})")
    C.update("agent_config", "key=eq.apollo_page_" + region, {"value": page}) if C.select("agent_config", "key=eq.apollo_page_" + region) \
        else C.insert("agent_config", [{"key": "apollo_page_" + region, "value": page}])
    C.log(AGENT, "apollo", f"agency {region}: page {page}, {len(people)} candidates, {reveals} reveals, {len(inserted)} inserted")
    C.out({"region": region, "page": page, "candidates": len(people), "reveals_used": reveals, "inserted": inserted, "skipped": skipped[:20]})


def _addagency_quiet(d):
    json.dump(d, open("_ap.json", "w"))
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        addagency("_ap.json")


def apollo_corporates(want=8, page=None):
    """Mumbai corporates: comms / marketing / HR heads at 100+ employee companies via Apollo, inserted as 'corporate' leads."""
    page = page or (int(C.config("apollo_page_corp", 0)) + 1)
    r = _apollo("mixed_people/api_search", {
        "person_titles": APOLLO_CORP_TITLES, "person_locations": ["Mumbai, India", "Navi Mumbai, India", "Thane, India"],
        "organization_num_employees_ranges": ["101,200", "201,500", "501,1000", "1001,2000", "2001,5000", "5001,10000"],
        "contact_email_status": ["verified"], "page": page, "per_page": 25})
    people = (r or {}).get("people") or []
    existing = {x["dedupe_key"] for x in C.select("leads", "select=dedupe_key&dedupe_key=not.is.null")}
    names = {(x["name"] or "").lower() for x in C.select("leads", "select=name")}
    inserted, reveals, skipped = [], 0, []
    for p in people:
        if len(inserted) >= want or reveals >= want * 3:
            break
        org = p.get("organization") or {}
        oname = (org.get("name") or "").strip()
        if not oname or oname.lower() in names or not p.get("has_email"):
            skipped.append((oname or "?") + (" (dup)" if oname.lower() in names else "")); continue
        reveals += 1
        email, status, full = _apollo_reveal(p["id"])
        org = full.get("organization") or org
        web = (org.get("website_url") or "").strip()
        if not email or status != "verified" or not web:
            skipped.append(oname + (" (no verified email)" if not email or status != "verified" else " (no website)")); continue
        key = "corp:" + re.sub(r"[^a-z0-9]+", "-", re.sub(r"^https?://(www\.)?", "", web.lower()).split("/")[0]).strip("-")
        if key in existing:
            skipped.append(oname + " (dup)"); continue
        d = {"name": oname, "website": web, "email": email, "contact_name": full.get("name") or "",
             "contact_role": full.get("title") or p.get("title") or "", "linkedin": full.get("linkedin_url") or "",
             "employees": org.get("estimated_num_employees") or "", "industry": org.get("industry") or "",
             "city_area": full.get("city") or "Mumbai", "why": "Apollo verified contact"}
        json.dump(d, open("_ap.json", "w"))
        import io, contextlib
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                addcorp("_ap.json")
            inserted.append(f"{oname} — {d['contact_name']} ({d['contact_role']}) <{email}>")
            existing.add(key); names.add(oname.lower())
        except SystemExit as e:
            skipped.append(f"{oname} ({e})")
    C.update("agent_config", "key=eq.apollo_page_corp", {"value": page}) if C.select("agent_config", "key=eq.apollo_page_corp") \
        else C.insert("agent_config", [{"key": "apollo_page_corp", "value": page}])
    C.log(AGENT, "apollo", f"corporate: page {page}, {len(people)} candidates, {reveals} reveals, {len(inserted)} inserted")
    C.out({"page": page, "candidates": len(people), "reveals_used": reveals, "inserted": inserted, "skipped": skipped[:20]})


# ------------------------------------------------------------------ Apollo: premium restaurant / hospitality GROUPS in Mumbai (Tejas, 13 Oct 2026 — "like Mirah Hospitality / Bayroute")
APOLLO_HOSP_TITLES = ["founder", "co-founder", "managing director", "director", "chief executive officer", "chief operating officer",
                      "head of marketing", "marketing head", "brand head", "chief marketing officer", "general manager marketing"]
APOLLO_HOSP_KW = ["hospitality", "restaurants", "restaurant group", "fine dining", "food and beverage", "hotels", "cafe chain", "bar and lounge"]


def apollo_hospitality(want=8, page=None):
    """Decision-makers at Mumbai restaurant / hospitality groups (multi-outlet, premium) via Apollo → 'food' leads,
    source 'AI Lead Gen (Apollo hospitality)'. No draft yet — the Lead Gen task writes the content-led email for each (python3 leadgen.py pending)."""
    page = page or (int(C.config("apollo_page_hosp", 0)) + 1)
    r = _apollo("mixed_people/api_search", {
        "person_titles": APOLLO_HOSP_TITLES, "person_locations": ["Mumbai, India", "Navi Mumbai, India", "Thane, India"],
        "q_organization_keyword_tags": APOLLO_HOSP_KW,
        "organization_num_employees_ranges": ["21,50", "51,100", "101,200", "201,500", "501,1000", "1001,2000", "2001,5000"],
        "contact_email_status": ["verified"], "page": page, "per_page": 25})
    people = (r or {}).get("people") or []
    existing = {x["dedupe_key"] for x in C.select("leads", "select=dedupe_key&dedupe_key=not.is.null")}
    names = {(x["name"] or "").lower() for x in C.select("leads", "select=name")}
    names |= {(x["name"] or "").lower() for x in C.select("clients", "select=name")}
    seen_orgs, inserted, reveals, skipped = set(), [], 0, []
    for p in people:
        if len(inserted) >= want or reveals >= want * 3:
            break
        org = p.get("organization") or {}
        oname = (org.get("name") or "").strip()
        if not oname or is_chain(oname) or oname.lower() in names or oname.lower() in seen_orgs or not p.get("has_email"):
            skipped.append((oname or "?") + (" (dup)" if oname.lower() in names or oname.lower() in seen_orgs else "")); continue
        seen_orgs.add(oname.lower())
        reveals += 1
        email, status, full = _apollo_reveal(p["id"])
        org = full.get("organization") or org
        web = (org.get("website_url") or "").strip()
        if not email or status != "verified":
            skipped.append(oname + " (no verified email)"); continue
        key = "apollo:" + re.sub(r"[^a-z0-9]+", "-", (re.sub(r"^https?://(www\.)?", "", web.lower()).split("/")[0] if web else oname.lower())).strip("-")
        if key in existing:
            skipped.append(oname + " (dup)"); continue
        est = EST["food"]
        kw = ", ".join((org.get("keywords") or [])[:8])
        notes = {"category": org.get("industry") or "hospitality group", "employees": org.get("estimated_num_employees"),
                 "keywords": kw, "contact_role": full.get("title"), "linkedin": full.get("linkedin_url"), "source": "apollo"}
        why = f"hospitality group, ~{org.get('estimated_num_employees') or '?'} employees; decision-maker: {full.get('title') or ''}; verified email"
        row = {"dedupe_key": key, "name": oname, "segment": "food", "source": "AI Lead Gen (Apollo hospitality)",
               "city_area": full.get("city") or "Mumbai", "email": email, "contact_phone": "", "instagram": "", "website": web,
               "score": 85, "why": why, "agent_notes": json.dumps(notes, ensure_ascii=False),
               "contact_name": full.get("name") or "", "service": "Social media management + content", "kind": "retainer",
               "est_value": est, "goal_value": goal_value("retainer", est), "stage": "New", "owner_name": "Tejas",
               "next_date": C.now_ist().date().isoformat(), "next_action": "AI: draft + send first outreach", "notes": ""}
        ins = C.insert("leads", [row])
        inserted.append({"id": ins[0]["id"], "name": oname, "contact": f"{row['contact_name']} ({full.get('title') or ''})", "email": email,
                         "website": web, "employees": org.get("estimated_num_employees"), "keywords": kw})
        existing.add(key); names.add(oname.lower())
    C.update("agent_config", "key=eq.apollo_page_hosp", {"value": page}) if C.select("agent_config", "key=eq.apollo_page_hosp") \
        else C.insert("agent_config", [{"key": "apollo_page_hosp", "value": page}])
    C.log(AGENT, "apollo", f"hospitality groups: page {page}, {len(people)} candidates, {reveals} reveals, {len(inserted)} inserted")
    C.out({"page": page, "candidates": len(people), "reveals_used": reveals, "inserted": inserted, "skipped": skipped[:20],
           "next": "write a content-led email + WhatsApp for each inserted lead and save with `python3 leadgen.py draft <id> e.html w.txt`"})


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] == "run":
        run(a[1] if len(a) > 1 else "auto")
    elif a[0] == "apollo-hospitality":
        apollo_hospitality(int(a[1]) if len(a) > 1 else 8)
    elif a[0] == "apollo-agencies":
        apollo_agencies(a[1], int(a[2]) if len(a) > 2 else 12)
    elif a[0] == "apollo-corporates":
        apollo_corporates(int(a[1]) if len(a) > 1 else 8)
    elif a[0] == "addagency":
        addagency(a[1])
    elif a[0] == "agencytoday":
        agency_today()
    elif a[0] == "addcorp":
        addcorp(a[1])
    elif a[0] == "corptoday":
        corp_count_today()
    elif a[0] == "pending":
        pending()
    elif a[0] == "draft":
        draft(a[1], a[2], a[3])
    elif a[0] == "pool-load":
        pool_load(a[1], a[2], a[3])
    elif a[0] == "pool":
        pool_status()
    elif a[0] == "osm":
        cands, area = osm(a[1] if len(a) > 1 else "food")
        C.out({"area": area, "candidates": cands})
    elif a[0] == "add":
        add(a[1])
    else:
        print(__doc__)
