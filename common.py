"""Shared plumbing for Stirr Studio AI agents.

Every agent script imports this. It talks to the Stirr Control Room database
(Supabase) as the `agents@stirrstudio.in` login and pulls third-party
credentials from the RLS-locked `agent_secrets` table.

Required environment variables (the scheduled-task prompt sets them):
  SB_URL, SB_ANON, AGENT_EMAIL, AGENT_PASSWORD
"""
import json, os, sys, time, datetime, urllib.request, urllib.parse, urllib.error

SB_URL = os.environ.get("SB_URL", "https://dlayvjfdlthbuvwuglwg.supabase.co").rstrip("/")
SB_ANON = os.environ.get("SB_ANON", "")
AGENT_EMAIL = os.environ.get("AGENT_EMAIL", "agents@stirrstudio.in")
AGENT_PASSWORD = os.environ.get("AGENT_PASSWORD", "")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

_token = None
_secrets = None


def now_ist():
    return datetime.datetime.now(IST)


def http(method, url, body=None, headers=None, timeout=60, raw=False):
    data = None
    h = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = body
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {url} -> {e.code}: {txt[:800]}")
    if raw:
        return txt
    try:
        return json.loads(txt) if txt else None
    except json.JSONDecodeError:
        return txt


# ---------------------------------------------------------------- Supabase
def login():
    global _token
    if _token:
        return _token
    if not SB_ANON or not AGENT_PASSWORD:
        sys.exit("SB_ANON / AGENT_PASSWORD not set")
    j = http("POST", f"{SB_URL}/auth/v1/token?grant_type=password",
             {"email": AGENT_EMAIL, "password": AGENT_PASSWORD},
             {"apikey": SB_ANON})
    _token = j["access_token"]
    return _token


def _h(extra=None):
    h = {"apikey": SB_ANON, "Authorization": f"Bearer {login()}"}
    if extra:
        h.update(extra)
    return h


def select(table, query=""):
    """query: PostgREST query string, e.g. 'stage=eq.New&order=score.desc&limit=20'"""
    return http("GET", f"{SB_URL}/rest/v1/{table}?{query}", headers=_h()) or []


def insert(table, rows, upsert_on=None):
    prefer = "return=representation"
    q = ""
    if upsert_on:
        prefer += ",resolution=ignore-duplicates"
        q = f"?on_conflict={upsert_on}"
    return http("POST", f"{SB_URL}/rest/v1/{table}{q}", rows, _h({"Prefer": prefer}))


def update(table, match_query, patch):
    return http("PATCH", f"{SB_URL}/rest/v1/{table}?{match_query}", patch,
                _h({"Prefer": "return=representation"}))


def rpc(fn, args=None):
    return http("POST", f"{SB_URL}/rest/v1/rpc/{fn}", args or {}, _h())


def config(key, default=None):
    rows = select("agent_config", f"key=eq.{key}&select=value")
    return rows[0]["value"] if rows else default


def secrets():
    global _secrets
    if _secrets is None:
        _secrets = {r["key"]: r["value"] for r in select("agent_secrets", "select=key,value")}
    return _secrets


def secret(key, required=True):
    v = secrets().get(key)
    if required and not v:
        sys.exit(f"Missing secret '{key}' in agent_secrets — ask Tejas to add it")
    return v


def set_secret(key, value):
    return insert("agent_secrets", [{"key": key, "value": value,
                                     "updated_at": now_ist().isoformat()}], upsert_on="key") or \
        update("agent_secrets", f"key=eq.{key}", {"value": value, "updated_at": now_ist().isoformat()})


def log(agent, action, summary, lead_id=None, client_id=None, detail=None, needs_tejas=False):
    row = {"agent": agent, "action": action, "summary": summary[:1000],
           "lead_id": lead_id, "client_id": client_id, "detail": detail,
           "needs_tejas": bool(needs_tejas)}
    try:
        insert("agent_activity", [row])
    except Exception as e:  # logging must never break an agent
        print("log failed:", e, file=sys.stderr)


# ---------------------------------------------------------------- Apify
def apify_run(actor, run_input, wait=240, max_usd=None):
    tok = secret("APIFY_TOKEN")
    actor_path = actor.replace("/", "~")
    q = {"token": tok, "waitForFinish": str(min(wait, 300))}
    if max_usd:
        q["maxTotalChargeUsd"] = str(max_usd)
    url = f"https://api.apify.com/v2/acts/{actor_path}/runs?" + urllib.parse.urlencode(q)
    run = http("POST", url, run_input)["data"]
    rid = run["id"]
    t0 = time.time()
    while run["status"] in ("READY", "RUNNING") and time.time() - t0 < wait:
        time.sleep(10)
        run = http("GET", f"https://api.apify.com/v2/actor-runs/{rid}?token={tok}")["data"]
    if run["status"] not in ("SUCCEEDED", "RUNNING", "READY"):
        raise RuntimeError(f"Apify run {rid} ended with {run['status']}")
    ds = run["defaultDatasetId"]
    items = http("GET", f"https://api.apify.com/v2/datasets/{ds}/items?token={tok}&clean=true&limit=1000")
    return items or []


# ---------------------------------------------------------------- Zoho Mail
def zoho_access_token():
    s = secrets()
    j = http("POST", "https://accounts.zoho.in/oauth/v2/token",
             urllib.parse.urlencode({"refresh_token": secret("ZOHO_REFRESH_TOKEN"),
                                     "client_id": secret("ZOHO_CLIENT_ID"),
                                     "client_secret": secret("ZOHO_CLIENT_SECRET"),
                                     "grant_type": "refresh_token"}),
             {"Content-Type": "application/x-www-form-urlencoded"})
    if "access_token" not in j:
        raise RuntimeError(f"Zoho token refresh failed: {j}")
    return j["access_token"]


def zoho_account_id(tok):
    s = secrets()
    if s.get("ZOHO_ACCOUNT_ID"):
        return s["ZOHO_ACCOUNT_ID"]
    j = http("GET", "https://mail.zoho.in/api/accounts", headers={"Authorization": f"Zoho-oauthtoken {tok}"})
    acc = j["data"][0]["accountId"]
    set_secret("ZOHO_ACCOUNT_ID", acc)
    return acc


def send_email(to, subject, html_body, from_addr="tejas@ourdreamwork.com", reply_to=None):
    tok = zoho_access_token()
    acc = zoho_account_id(tok)
    body = {"fromAddress": from_addr, "toAddress": to, "subject": subject,
            "content": html_body, "mailFormat": "html", "askReceipt": "no"}
    if reply_to:
        body["replyTo"] = reply_to
    j = http("POST", f"https://mail.zoho.in/api/accounts/{acc}/messages", body,
             {"Authorization": f"Zoho-oauthtoken {tok}"})
    return j


def list_inbox(limit=50, since_days=3):
    """Recent messages in the Zoho inbox (for reply detection)."""
    tok = zoho_access_token()
    acc = zoho_account_id(tok)
    j = http("GET", f"https://mail.zoho.in/api/accounts/{acc}/messages/view?limit={limit}&sortorder=false",
             headers={"Authorization": f"Zoho-oauthtoken {tok}"})
    cutoff = (now_ist() - datetime.timedelta(days=since_days)).timestamp() * 1000
    return [m for m in j.get("data", []) if float(m.get("receivedTime", 0)) >= cutoff]


# ---------------------------------------------------------------- Meta (WhatsApp + Ads)
GRAPH = "https://graph.facebook.com/v21.0"


def meta_headers():
    return {"Authorization": f"Bearer {secret('META_TOKEN')}"}


def wa_send_text(to_phone, text):
    pid = secret("WA_PHONE_ID")
    return http("POST", f"{GRAPH}/{pid}/messages",
                {"messaging_product": "whatsapp", "to": to_phone.lstrip("+").replace(" ", ""),
                 "type": "text", "text": {"preview_url": False, "body": text}}, meta_headers())


def wa_send_template(to_phone, template, params=None, lang="en"):
    pid = secret("WA_PHONE_ID")
    comp = []
    if params:
        comp = [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}]
    return http("POST", f"{GRAPH}/{pid}/messages",
                {"messaging_product": "whatsapp", "to": to_phone.lstrip("+").replace(" ", ""),
                 "type": "template",
                 "template": {"name": template, "language": {"code": lang}, "components": comp}},
                meta_headers())


def wa_templates():
    wid = secret("WA_BUSINESS_ID")
    return http("GET", f"{GRAPH}/{wid}/message_templates?fields=name,status,language,components&limit=100",
                headers=meta_headers()).get("data", [])


def ads_get(path, params=None):
    p = dict(params or {})
    p["access_token"] = secret("META_TOKEN")
    return http("GET", f"{GRAPH}/{path}?" + urllib.parse.urlencode(p))


_PAGE_TOKEN = None
def page_token():
    """Page access token for the business page (lead forms / leads need one, not the system-user token)."""
    global _PAGE_TOKEN
    if not _PAGE_TOKEN:
        _PAGE_TOKEN = ads_get(secret("META_PAGE_ID"), {"fields": "access_token"})["access_token"]
    return _PAGE_TOKEN


def page_get(path, params=None):
    p = dict(params or {})
    p["access_token"] = page_token()
    return http("GET", f"{GRAPH}/{path}?" + urllib.parse.urlencode(p))


def ads_post(path, params):
    p = dict(params)
    p["access_token"] = secret("META_TOKEN")
    return http("POST", f"{GRAPH}/{path}", urllib.parse.urlencode(p).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})


def ad_account():
    a = secret("AD_ACCOUNT_ID")
    return a if a.startswith("act_") else f"act_{a}"


# ---------------------------------------------------------------- helpers
def norm_phone(p):
    if not p:
        return ""
    d = "".join(ch for ch in p if ch.isdigit())
    if len(d) == 11 and d.startswith("0"):
        d = d[1:]
    if len(d) == 10:
        d = "91" + d
    if len(d) == 12 and d.startswith("91"):
        return d
    if len(d) == 12 and d.startswith("22"):      # Mumbai landline without country code
        return "91" + d[:10]
    return ""                                     # anything else is not a number we can message


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))

# ------------------------------------------------------------------ corporate email (Tejas, 13 Oct 2026): plain service offer — no ideas, no client names
CORP_SUBJECT = "{company}: leadership podcasts and video content"


def corp_email(company, contact_name=""):
    first = (contact_name or "").strip().split(" ")[0] if contact_name else ""
    hi = f"Hi {first}," if first else "Hello,"
    body = (f"<p>{hi}</p>"
            "<p>I'm Tejas, founder of Stirr Studio, a content and video production studio in Mumbai.</p>"
            "<p>We work with companies on:</p><ul>"
            "<li><b>Leadership podcast recording</b> — planned, recorded, edited and published end to end</li>"
            "<li><b>Video content for leadership</b> — leader messages, LinkedIn videos, town-hall and event videos</li>"
            "<li><b>Video content for employees</b> — culture films, employee stories, onboarding and employer-branding videos</li></ul>"
            f"<p>Is {company} looking for any of these, or something similar? We'd be glad to help.</p>"
            "<p>Tejas<br>Founder — Stirr Studio<br>stirrstudio.in</p>")
    return f"<!-- SUBJECT: {CORP_SUBJECT.format(company=company)} -->\n" + body


def corp_wa(company, contact_name=""):
    return (f"Hi, Tejas here from Stirr Studio. We help companies with leadership podcast recording and video content "
            f"for leaders and employees. Is {company} looking for anything like this? Happy to help.")


# ------------------------------------------------------------------ international agency email (Tejas, 13 Oct 2026): white-label offer, no ideas, no client names
AGENCY_REGIONS = {"uk": "UK & Ireland", "uae": "UAE", "au": "Australia & NZ", "us": "USA & Canada"}
AGENCY_SUBJECT = "{company}: white-label editing and design support"


def agency_email(company, contact_name=""):
    first = (contact_name or "").strip().split(" ")[0] if contact_name else ""
    hi = f"Hi {first}," if first else "Hello,"
    body = (f"<p>{hi}</p>"
            "<p>I'm Tejas, founder of Stirr Studio, a production studio in Mumbai. We work as a white-label team for agencies, "
            "under your brand, in your timezone's working hours.</p>"
            "<p>What we handle:</p><ul>"
            "<li><b>Video editing</b> — reels and short-form, ad cuts, long-form, podcasts, motion graphics</li>"
            "<li><b>Graphic design</b> — social creatives, carousels, decks, brand collateral</li>"
            "<li><b>Social media support</b> — scheduling, posting, community management</li></ul>"
            f"<p>Is {company} looking for extra production capacity, or something similar? Happy to share samples.</p>"
            "<p>Tejas<br>Founder — Stirr Studio<br>stirrstudio.in</p>")
    return f"<!-- SUBJECT: {AGENCY_SUBJECT.format(company=company)} -->\n" + body
