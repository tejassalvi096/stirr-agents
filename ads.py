"""Ads agent — Meta Marketing API: plan, launch, monitor and optimise within a hard monthly cap.

Usage:
  python3 ads.py status                         account, spend this month vs cap, active campaigns + 7-day insights (JSON)
  python3 ads.py insights [days]                per-campaign/adset/ad insights for the last N days (default 7)
  python3 ads.py launch <spec.json>             create campaign → ad set → ad(s) from a JSON spec (see SPEC below)
  python3 ads.py pause <campaign_id> "<reason>"
  python3 ads.py resume <campaign_id> "<reason>"
  python3 ads.py budget <adset_id> <daily_inr> "<reason>"
  python3 ads.py leadforms                      lead forms on the page (for LEAD_GENERATION campaigns)
  python3 ads.py leads [form_id]                new leads from lead forms → pushed into the Leads table

Ownership guard: the agent only sees and touches campaigns whose name starts with "Stirr AI —"
(or that it recorded in ad_campaigns). Client campaigns in the same ad account are invisible to it.
Hard guard: total spend this calendar month + planned daily budgets × days left must stay
under agent_config.ads_monthly_cap_inr (₹10,000 by default). Any launch/budget call that
would breach it is refused and logged for Tejas.

SPEC (launch):
{
 "name": "Stirr — Cafés Bandra — Sept W2",
 "objective": "OUTCOME_LEADS" | "OUTCOME_TRAFFIC" | "OUTCOME_ENGAGEMENT",
 "daily_budget_inr": 300,
 "segment": "food",
 "targeting": {"geo": [{"key": "...", "radius": 5}] OR "cities": ["Mumbai"], "age_min": 25, "age_max": 50,
               "interests": ["Restaurant", "Small business"]},
 "page_id": "...", "instagram_actor_id": "...",   (optional; defaults from secrets META_PAGE_ID / META_IG_ID)
 "lead_form_id": "...",                            (for OUTCOME_LEADS)
 "link": "https://stirrstudio.in/...",             (for traffic)
 "ads": [{"name": "Hook A", "primary_text": "...", "headline": "...", "description": "...",
          "image_url": "https://..." or "image_hash": "...", "cta": "LEARN_MORE"}]
}
"""
import sys, json, datetime, calendar
import common as C

AGENT = "Ads"
PREFIX = "Stirr AI —"   # the agent may only touch campaigns it created (this prefix); client campaigns are off-limits


def mine():
    """IDs of campaigns/adsets/ads the agent owns (by name prefix or ad_campaigns table)."""
    known = {r["meta_campaign_id"] for r in C.select("ad_campaigns", "select=meta_campaign_id")}
    camps = C.ads_get(f"{C.ad_account()}/campaigns", {"fields": "id,name", "limit": 200}).get("data", [])
    ids = {c["id"] for c in camps if c["name"].startswith(PREFIX) or c["id"] in known}
    return ids


def assert_mine(obj_id):
    j = C.ads_get(obj_id, {"fields": "id,name,campaign_id"})
    cid = j.get("campaign_id") or j["id"]
    if cid not in mine():
        msg = f"REFUSED: {obj_id} ({j.get('name')}) belongs to a client campaign, not to the AI agent"
        C.log(AGENT, "refused", msg, needs_tejas=True)
        sys.exit(msg)


def cap():
    return float(C.config("ads_monthly_cap_inr", 10000))


def month_range():
    n = C.now_ist().date()
    first = n.replace(day=1)
    last = n.replace(day=calendar.monthrange(n.year, n.month)[1])
    return first, last, (last - n).days + 1


def spend_this_month():
    first, last, _ = month_range()
    ids = mine()
    if not ids:
        return 0.0
    j = C.ads_get(f"{C.ad_account()}/insights", {"fields": "campaign_id,spend", "level": "campaign", "limit": 200,
        "filtering": json.dumps([{"field": "campaign.id", "operator": "IN", "value": list(ids)}]),
        "time_range": json.dumps({"since": first.isoformat(), "until": C.now_ist().date().isoformat()})})
    return sum(float(r.get("spend", 0)) for r in j.get("data") or [])


def active_daily_budget_inr():
    j = C.ads_get(f"{C.ad_account()}/adsets", {"fields": "id,name,daily_budget,effective_status,campaign_id", "limit": 200})
    tot = 0.0
    ids = mine()
    for a in j.get("data", []):
        if a.get("campaign_id") in ids and a.get("effective_status") == "ACTIVE" and a.get("daily_budget"):
            tot += float(a["daily_budget"]) / 100.0
    return tot


def projected(extra_daily=0.0):
    spent = spend_this_month()
    _, _, days_left = month_range()
    return spent, spent + (active_daily_budget_inr() + extra_daily) * days_left


def guard(extra_daily, what):
    spent, proj = projected(extra_daily)
    if proj > cap():
        msg = f"REFUSED {what}: projected month spend ₹{proj:,.0f} > cap ₹{cap():,.0f} (spent ₹{spent:,.0f})"
        C.log(AGENT, "cap_refusal", msg, needs_tejas=True)
        sys.exit(msg)
    return spent, proj


def status():
    acc = C.ads_get(C.ad_account(), {"fields": "name,currency,account_status,amount_spent,balance"})
    ids = mine()
    camps = [c for c in C.ads_get(f"{C.ad_account()}/campaigns", {"fields": "id,name,objective,status,effective_status,created_time,daily_budget", "limit": 200}).get("data", []) if c["id"] in ids]
    spent, proj = projected()
    ins = insights_data(7)
    # mirror into ad_campaigns table
    for c in camps:
        row = {"meta_campaign_id": c["id"], "name": c["name"], "objective": c.get("objective"), "status": c.get("effective_status"),
               "updated_at": C.now_ist().isoformat()}
        i = next((x for x in ins.get("campaigns", []) if x.get("campaign_id") == c["id"]), None)
        if i:
            row["spend_to_date"] = float(i.get("spend", 0)); row["leads_count"] = int(i.get("leads", 0) or 0)
            row["cpl"] = float(i["spend"]) / row["leads_count"] if row["leads_count"] else None
        try:
            C.insert("ad_campaigns", [row], upsert_on="meta_campaign_id")
            C.update("ad_campaigns", f"meta_campaign_id=eq.{c['id']}", row)
        except Exception as e:
            print("mirror failed", e, file=sys.stderr)
    C.out({"account": acc, "cap_inr": cap(), "spent_this_month_inr": spent, "projected_month_inr": proj,
           "active_daily_budget_inr": active_daily_budget_inr(), "days_left": month_range()[2],
           "campaigns": camps, "last_7_days": ins})


def _leads_from_actions(row):
    for a in row.get("actions", []) or []:
        if a.get("action_type") in ("lead", "onsite_conversion.lead_grouped", "leadgen_grouped"):
            return int(float(a.get("value", 0)))
    return 0


def insights_data(days):
    until = C.now_ist().date(); since = until - datetime.timedelta(days=days)
    tr = json.dumps({"since": since.isoformat(), "until": until.isoformat()})
    out = {}
    ids = list(mine())
    if not ids:
        return {"campaigns": [], "adsets": [], "ads": [], "note": "no AI-owned campaigns yet"}
    for level in ("campaign", "adset", "ad"):
        j = C.ads_get(f"{C.ad_account()}/insights", {
            "level": level, "time_range": tr, "limit": 200,
            "filtering": json.dumps([{"field": "campaign.id", "operator": "IN", "value": ids}]),
            "fields": f"{level}_id,{level}_name,spend,impressions,reach,clicks,ctr,cpc,cpm,frequency,actions,cost_per_action_type"})
        rows = []
        for r in j.get("data", []):
            r["leads"] = _leads_from_actions(r)
            r["cpl"] = round(float(r["spend"]) / r["leads"], 2) if r["leads"] else None
            rows.append(r)
        out[level + "s"] = rows
    return out


def insights(days):
    C.out(insights_data(days))


def launch(spec_file):
    spec = json.load(open(spec_file))
    if not spec["name"].startswith(PREFIX):
        spec["name"] = PREFIX + " " + spec["name"]
    daily = float(spec["daily_budget_inr"])
    spent, proj = guard(daily, f"launch '{spec['name']}' at ₹{daily}/day")
    acct = C.ad_account()
    page = spec.get("page_id") or C.secret("META_PAGE_ID")
    ig = spec.get("instagram_actor_id") or C.secrets().get("META_IG_ID")
    obj = spec.get("objective", "OUTCOME_LEADS")
    camp = C.ads_post(f"{acct}/campaigns", {"name": spec["name"], "objective": obj, "status": "PAUSED",
                                            "special_ad_categories": "[]", "buying_type": "AUCTION",
                                            "is_adset_budget_sharing_enabled": "false"})
    cid = camp["id"]
    t = spec.get("targeting", {})
    geo = {"countries": ["IN"]}
    if t.get("cities"):
        # resolve city keys
        keys = []
        for cname in t["cities"]:
            s = C.ads_get("search", {"type": "adgeolocation", "location_types": '["city"]', "q": cname, "country_code": "IN", "limit": 1})
            if s.get("data"):
                k = {"key": s["data"][0]["key"]}
                if t.get("radius_km"):
                    k.update({"radius": max(17, int(t["radius_km"])), "distance_unit": "kilometer"})
                keys.append(k)
        if keys:
            geo = {"cities": keys}
    if t.get("geo"):
        geo = {"custom_locations": [{"latitude": g["lat"], "longitude": g["lng"], "radius": g.get("radius", 5), "distance_unit": "kilometer"} for g in t["geo"]]}
    targeting = {"geo_locations": geo, "age_min": t.get("age_min", 24), "age_max": t.get("age_max", 55),
                 "publisher_platforms": ["facebook", "instagram"], "facebook_positions": ["feed"], "instagram_positions": ["stream", "story", "reels"],
                 "targeting_automation": {"advantage_audience": 0}}
    if t.get("interests"):
        ints = []
        for name in t["interests"]:
            s = C.ads_get("search", {"type": "adinterest", "q": name, "limit": 1})
            if s.get("data"):
                ints.append({"id": s["data"][0]["id"], "name": s["data"][0]["name"]})
        if ints:
            targeting["flexible_spec"] = [{"interests": ints}]
    adset_params = {"name": spec["name"] + " — set", "campaign_id": cid, "daily_budget": int(daily * 100),
                    "billing_event": "IMPRESSIONS", "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                    "targeting": json.dumps(targeting), "status": "PAUSED", "start_time": C.now_ist().isoformat()}
    if obj == "OUTCOME_LEADS":
        adset_params.update({"optimization_goal": "LEAD_GENERATION", "promoted_object": json.dumps({"page_id": page}),
                             "destination_type": "ON_AD"})
    elif obj == "OUTCOME_TRAFFIC":
        adset_params.update({"optimization_goal": "LINK_CLICKS"})
    else:
        adset_params.update({"optimization_goal": "POST_ENGAGEMENT"})
    adset = C.ads_post(f"{acct}/adsets", adset_params)
    asid = adset["id"]
    ads_created = []
    for ad in spec["ads"]:
        link_data = {"message": ad["primary_text"], "name": ad.get("headline", ""), "description": ad.get("description", ""),
                     "call_to_action": {"type": ad.get("cta", "LEARN_MORE")}}
        if ad.get("image_hash"):
            link_data["image_hash"] = ad["image_hash"]
        elif ad.get("image_url"):
            link_data["picture"] = ad["image_url"]
        if obj == "OUTCOME_LEADS":
            link_data["link"] = "https://fb.me/"
            link_data["call_to_action"]["value"] = {"lead_gen_form_id": spec["lead_form_id"]}
        else:
            link_data["link"] = spec.get("link", "https://stirrstudio.in")
        story = {"page_id": page, "link_data": link_data}
        if ig:
            story["instagram_user_id"] = ig
        try:
            cr = C.ads_post(f"{acct}/adcreatives", {"name": ad["name"], "object_story_spec": json.dumps(story)})
        except RuntimeError as e:
            if "instagram" not in str(e).lower():
                raise
            story.pop("instagram_user_id", None)   # fall back to the page-backed Instagram identity
            cr = C.ads_post(f"{acct}/adcreatives", {"name": ad["name"], "object_story_spec": json.dumps(story)})
        a = C.ads_post(f"{acct}/ads", {"name": ad["name"], "adset_id": asid, "creative": json.dumps({"creative_id": cr["id"]}), "status": "PAUSED"})
        ads_created.append({"ad_id": a["id"], "creative_id": cr["id"], "name": ad["name"]})
    # go live
    C.ads_post(f"{asid}", {"status": "ACTIVE"})
    for a in ads_created:
        C.ads_post(f"{a['ad_id']}", {"status": "ACTIVE"})
    C.ads_post(f"{cid}", {"status": "ACTIVE"})
    C.insert("ad_campaigns", [{"meta_campaign_id": cid, "name": spec["name"], "objective": obj, "segment": spec.get("segment"),
                               "status": "ACTIVE", "daily_budget": daily}], upsert_on="meta_campaign_id")
    C.log(AGENT, "launch", f"Launched '{spec['name']}' ₹{daily}/day ({obj}); projected month ₹{proj:,.0f} of ₹{cap():,.0f}",
          detail={"campaign_id": cid, "adset_id": asid, "ads": ads_created}, needs_tejas=True)
    C.out({"campaign_id": cid, "adset_id": asid, "ads": ads_created, "projected_month_inr": proj})


def set_status(cid, st, reason):
    assert_mine(cid)
    C.ads_post(cid, {"status": st})
    try:
        C.update("ad_campaigns", f"meta_campaign_id=eq.{cid}", {"status": st, "last_analysis": reason, "updated_at": C.now_ist().isoformat()})
    except Exception:
        pass
    C.log(AGENT, st.lower(), f"Campaign {cid} → {st}: {reason}", needs_tejas=True)
    C.out({"ok": True})


def budget(asid, daily, reason):
    daily = float(daily)
    assert_mine(asid)
    cur = C.ads_get(asid, {"fields": "daily_budget,campaign_id"})
    delta = daily - float(cur.get("daily_budget", 0)) / 100
    guard(max(0, delta), f"budget change on {asid} to ₹{daily}/day")
    C.ads_post(asid, {"daily_budget": int(daily * 100)})
    C.log(AGENT, "budget", f"Ad set {asid} daily budget → ₹{daily}: {reason}", needs_tejas=True)
    C.out({"ok": True})


def leadforms():
    page = C.secret("META_PAGE_ID")
    C.out(C.page_get(f"{page}/leadgen_forms", {"fields": "id,name,status,questions,leads_count", "limit": 50}))


def pull_leads(form_id=None):
    page = C.secret("META_PAGE_ID")
    forms = [form_id] if form_id else [f["id"] for f in C.page_get(f"{page}/leadgen_forms", {"fields": "id", "limit": 50}).get("data", [])]
    existing = {r["dedupe_key"] for r in C.select("leads", "select=dedupe_key&dedupe_key=like.metalead:*")}
    new = []
    for fid in forms:
        for l in C.page_get(f"{fid}/leads", {"fields": "id,created_time,field_data,campaign_name,ad_name", "limit": 100}).get("data", []):
            key = "metalead:" + l["id"]
            if key in existing:
                continue
            f = {x["name"]: (x.get("values") or [""])[0] for x in l.get("field_data", [])}
            name = f.get("company_name") or f.get("full_name") or f.get("first_name") or "Meta lead"
            row = {"dedupe_key": key, "name": name, "contact_name": f.get("full_name", ""), "email": f.get("email", ""),
                   "contact_phone": C.norm_phone(f.get("phone_number", "")), "source": f"Meta Ads: {l.get('campaign_name', '')}",
                   "segment": "inbound", "stage": "New", "score": 85, "why": "Inbound from ad — responded to our offer",
                   "owner_name": "Tejas", "kind": "retainer", "est_value": 45000, "goal_value": 45000 * max(1, 13 - C.now_ist().month),
                   "next_date": C.now_ist().date().isoformat(), "next_action": "AI: respond within the hour",
                   "agent_notes": json.dumps(f, ensure_ascii=False), "notes": ""}
            new.append(row)
    ins = C.insert("leads", new) if new else []
    if ins:
        C.log(AGENT, "inbound", f"{len(ins)} new inbound lead(s) from Meta lead forms", needs_tejas=True)
    C.out({"inserted": len(ins), "leads": [{"id": r["id"], "name": r["name"], "phone": r.get("contact_phone"), "email": r.get("email")} for r in ins]})


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    if cmd == "status": status()
    elif cmd == "insights": insights(int(a[1]) if len(a) > 1 else 7)
    elif cmd == "launch": launch(a[1])
    elif cmd == "pause": set_status(a[1], "PAUSED", a[2] if len(a) > 2 else "")
    elif cmd == "resume": set_status(a[1], "ACTIVE", a[2] if len(a) > 2 else "")
    elif cmd == "budget": budget(a[1], a[2], a[3] if len(a) > 3 else "")
    elif cmd == "leadforms": leadforms()
    elif cmd == "leads": pull_leads(a[1] if len(a) > 1 else None)
    else: print(__doc__)
