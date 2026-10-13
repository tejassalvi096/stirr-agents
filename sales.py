"""Sales agent — works the Leads pipeline: first outreach, follow-ups, replies, escalation.

Usage:
  python3 sales.py due                      leads that need an action now (JSON)
  python3 sales.py templates                approved WhatsApp templates
  python3 sales.py replies                  recent inbox emails matched to leads (JSON)
  python3 sales.py send-email <lead_id> <subject> <html_file>   (Zoho API — NOT used; email goes via the Microsoft 365 tools)
  python3 sales.py sent <lead_id> email "<subject>" <body_file>  record an email you sent with the Microsoft 365 outlook_send_mail tool (advances stage/follow-up)
  python3 sales.py reply-in <lead_id> "<subject>" "<summary>"    record an inbound reply you found with outlook_email_search
  python3 sales.py send-wa <lead_id> <text_file>           (free-form; only inside 24h window)
  python3 sales.py send-wa-template <lead_id> <template> [param1] [param2] ...
  python3 sales.py move <lead_id> <stage> "<note>" [next_date YYYY-MM-DD]
  python3 sales.py escalate <lead_id> "<why Tejas is needed>"

Rules baked in: never contact a lead more than 4 times without a reply, never
outside working hours, never a lead in Won/Lost, and always log every message.
"""
import sys, json, json, datetime, re
import common as C

AGENT = "Sales"
MAX_TOUCHES = 4


def working_hours():
    wh = C.config("working_hours", {"start": "11:00", "end": "19:00"})
    t = C.now_ist().strftime("%H:%M")
    return wh["start"] <= t <= wh["end"]


def cadence():
    return C.config("followup_cadence_days", [2, 4, 7, 14])


def lead(lead_id):
    r = C.select("leads", f"id=eq.{lead_id}&select=*")
    if not r:
        sys.exit(f"lead {lead_id} not found")
    return r[0]


def due(segment=None, region=None):
    today = C.now_ist().date().isoformat()
    rows = C.select("leads", "select=id,name,contact_name,segment,stage,score,email,contact_phone,instagram,website,city_area,"
                             "email_draft,whatsapp_draft,last_contacted_at,followups_sent,needs_tejas,next_date,"
                             "next_action,notes,agent_notes,why,owner_name,kind,est_value"
                             f"&stage=in.(New,Contacted)&needs_tejas=eq.false&or=(next_date.is.null,next_date.lte.{today})"
                             + (f"&segment=eq.{segment}" if segment else "&segment=neq.agency")
                             + "&order=score.desc.nullslast&limit=60")
    if region:
        def _reg(r):
            try: return (json.loads(r.get("agent_notes") or "{}").get("region") or "").lower() == region.lower()
            except Exception: return False
        rows = [r for r in rows if _reg(r)]
    msgs = C.select("outreach_messages", "select=lead_id,channel,direction,created_at,subject&order=created_at.desc&limit=500")
    by = {}
    for m in msgs:
        by.setdefault(m["lead_id"], []).append(m)
    out = []
    for r in rows:
        hist = by.get(r["id"], [])
        touches = sum(1 for m in hist if m["direction"] == "out")
        replied = any(m["direction"] == "in" for m in hist)
        if touches >= MAX_TOUCHES and not replied:
            continue
        r["touches"] = touches
        r["replied"] = replied
        r["history"] = hist[:6]
        r["suggested"] = ("first_outreach" if touches == 0 else
                          "reply_handling" if replied else f"followup_{touches}")
        if r.get("segment") == "agency":
            r["email_draft"] = C.agency_email(r["name"], r.get("contact_name"))   # always the standard white-label email
            r["copy_hint"] = ("INTERNATIONAL AGENCY lead — EMAIL ONLY. First email: send email_draft exactly as given. "
                              "Follow-ups, short, sign 'Tejas': "
                              "#2 'Hi <first name>, following up on my note — we take on video editing, graphic design and social "
                              "media support for agencies as a white-label team in Mumbai. Is extra capacity something you need "
                              "this quarter?' "
                              "#3 'Hi <first name>, who looks after production or resourcing at <Agency>? Happy to send samples "
                              "to the right person.' "
                              "#4 'Hi <first name>, I'll assume the timing isn't right and stop here. If you need an extra pair "
                              "of hands later, one line is enough.' "
                              "REPLIES: samples → say samples will follow and `sales.py escalate` (Tejas sends them). PRICING → "
                              "you may state: video editing from USD 70 per edit; monthly retainers and a dedicated editor are "
                              "available — quote USD 900/month (retainer) and USD 1,400/month (dedicated editor) ONLY as "
                              "indicative and escalate to Tejas to confirm. A trial edit is a good first step — offer one paid "
                              "trial edit. No ideas, no client names, no claims about results.")
        if r.get("segment") == "corporate":
            r["email_draft"] = C.corp_email(r["name"], r.get("contact_name"))   # always the standard service email
            r["copy_hint"] = ("CORPORATE lead — EMAIL ONLY, never WhatsApp. Send email_draft exactly as given for the first email. "
                              "Do NOT pitch ideas, do NOT mention any client names (no Great Place To Work), no pricing. "
                              "Follow-ups, plain and short (sign 'Tejas'): "
                              "#2 'Hi <first name>, following up on my note — we help companies with leadership podcasts and "
                              "video content for leaders and employees. Is this something your team is planning this quarter?' "
                              "#3 'Hi <first name>, who looks after leadership and internal communication at <Company>? "
                              "Happy to share details with the right person.' "
                              "#4 'Hi <first name>, I'll assume the timing isn't right and stop here. If you need podcast or "
                              "video support later, one line is enough.'")
        out.append(r)
    C.out({"working_hours": working_hours(), "now_ist": C.now_ist().isoformat(), "cadence_days": cadence(), "due": out})


def _after_send(l, channel, subject, body, ext_id=None):
    C.insert("outreach_messages", [{"lead_id": l["id"], "channel": channel, "direction": "out",
                                    "subject": subject, "body": body, "external_id": ext_id}])
    touches = int(l.get("followups_sent") or 0) + 1
    cad = cadence()
    nxt = (C.now_ist() + datetime.timedelta(days=cad[min(touches - 1, len(cad) - 1)])).date().isoformat()
    C.update("leads", f"id=eq.{l['id']}", {
        "stage": "Contacted" if l["stage"] == "New" else l["stage"],
        "last_contacted_at": C.now_ist().isoformat(), "followups_sent": touches,
        "next_date": nxt, "next_action": f"AI: follow-up {touches + 1} via {channel}" if touches < MAX_TOUCHES else "AI: final — waiting for reply",
        "updated_at": C.now_ist().isoformat()})
    C.log(AGENT, f"send_{channel}", f"{channel} #{touches} → {l['name']}: {subject or body[:80]}", lead_id=l["id"])


def guard(l):
    if l["stage"] in ("Won", "Lost"):
        sys.exit("lead is closed")
    if l.get("needs_tejas"):
        sys.exit("lead is escalated to Tejas — do not contact")
    if not working_hours():
        sys.exit("outside working hours — try later")


def send_email(lead_id, subject, html_file):
    l = lead(lead_id)
    guard(l)
    if not l.get("email"):
        sys.exit("lead has no email")
    html = open(html_file, encoding="utf-8").read()
    r = C.send_email(l["email"], subject, html)
    _after_send(l, "email", subject, html, str(r.get("data", {}).get("messageId", "")) if isinstance(r, dict) else None)
    C.out({"ok": True, "to": l["email"], "zoho": r if isinstance(r, dict) else str(r)[:200]})


def sent(lead_id, channel, subject, body_file):
    l = lead(lead_id)
    body = open(body_file, encoding="utf-8").read()
    _after_send(l, channel, subject, body)
    C.out({"ok": True, "lead": l["name"]})


def reply_in(lead_id, subject, summary):
    l = lead(lead_id)
    C.insert("outreach_messages", [{"lead_id": lead_id, "channel": "email", "direction": "in",
                                    "subject": subject, "body": summary}])
    C.update("leads", f"id=eq.{lead_id}", {"next_date": C.now_ist().date().isoformat(),
                                          "next_action": "AI: reply received — respond", "updated_at": C.now_ist().isoformat()})
    C.log(AGENT, "reply_in", f"Reply from {l['name']}: {subject}", lead_id=lead_id, needs_tejas=True)
    C.out({"ok": True})


def send_wa(lead_id, text_file):
    l = lead(lead_id)
    guard(l)
    if not l.get("contact_phone"):
        sys.exit("lead has no phone")
    text = open(text_file, encoding="utf-8").read().strip()
    r = C.wa_send_text(l["contact_phone"], text)
    _after_send(l, "whatsapp", None, text, (r.get("messages") or [{}])[0].get("id"))
    C.out({"ok": True, "to": l["contact_phone"], "meta": r})


def send_wa_template(lead_id, template, params):
    l = lead(lead_id)
    guard(l)
    if not l.get("contact_phone"):
        sys.exit("lead has no phone")
    r = C.wa_send_template(l["contact_phone"], template, params)
    _after_send(l, "whatsapp", f"template:{template}", " | ".join(params), (r.get("messages") or [{}])[0].get("id"))
    C.out({"ok": True, "to": l["contact_phone"], "meta": r})


def move(lead_id, stage, note, next_date=None):
    l = lead(lead_id)
    patch = {"stage": stage, "notes": ((l.get("notes") or "") + f"\n[{C.now_ist():%d %b}] AI: {note}").strip(),
             "updated_at": C.now_ist().isoformat()}
    if next_date:
        patch["next_date"] = next_date
    C.update("leads", f"id=eq.{lead_id}", patch)
    C.log(AGENT, "stage", f"{l['name']}: {l['stage']} → {stage} — {note}", lead_id=lead_id,
          needs_tejas=stage in ("Meeting", "Proposal", "Negotiation"))
    C.out({"ok": True})


def escalate(lead_id, why):
    l = lead(lead_id)
    C.update("leads", f"id=eq.{lead_id}", {"needs_tejas": True, "next_action": f"TEJAS: {why}"[:200],
                                          "updated_at": C.now_ist().isoformat()})
    C.log(AGENT, "escalate", f"{l['name']}: {why}", lead_id=lead_id, needs_tejas=True)
    C.out({"ok": True})


def replies():
    """Match recent inbox mail to leads by sender address; record inbound + surface for handling."""
    leads = C.select("leads", "select=id,name,email,stage&email=not.is.null")
    by_email = {(l["email"] or "").lower(): l for l in leads if l.get("email")}
    seen = {m["external_id"] for m in C.select("outreach_messages", "select=external_id&direction=eq.in&limit=1000")}
    found = []
    for m in C.list_inbox(limit=80, since_days=5):
        frm = (m.get("fromAddress") or "").lower()
        l = by_email.get(frm)
        if not l or str(m.get("messageId")) in seen:
            continue
        C.insert("outreach_messages", [{"lead_id": l["id"], "channel": "email", "direction": "in",
                                        "subject": m.get("subject"), "body": m.get("summary"),
                                        "external_id": str(m.get("messageId"))}])
        C.update("leads", f"id=eq.{l['id']}", {"next_date": C.now_ist().date().isoformat(),
                                               "next_action": "AI: reply received — respond"})
        C.log(AGENT, "reply_in", f"Reply from {l['name']}: {m.get('subject')}", lead_id=l["id"], needs_tejas=True)
        found.append({"lead_id": l["id"], "name": l["name"], "subject": m.get("subject"), "summary": m.get("summary")})
    C.out(found)


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "due"
    if cmd == "due": due(a[1] if len(a) > 1 else None, a[2] if len(a) > 2 else None)
    elif cmd == "templates": C.out(C.wa_templates())
    elif cmd == "replies": replies()
    elif cmd == "send-email": send_email(a[1], a[2], a[3])
    elif cmd == "sent": sent(a[1], a[2], a[3], a[4])
    elif cmd == "reply-in": reply_in(a[1], a[2], a[3])
    elif cmd == "send-wa": send_wa(a[1], a[2])
    elif cmd == "send-wa-template": send_wa_template(a[1], a[2], a[3:])
    elif cmd == "move": move(a[1], a[2], a[3], a[4] if len(a) > 4 else None)
    elif cmd == "escalate": escalate(a[1], a[2])
    else: print(__doc__)
