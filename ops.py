"""Ops agent — client health, deliverable tracking, freelancer briefs, month-end invoice prep.

Usage:
  python3 ops.py health            per-client: posts planned/done this month, overdue tasks, renewal soon, days since a post
  python3 ops.py invoices          month-end: which invoices are not raised, who is unpaid > 7 days (finance-locked; agents see only counts unless allowed)
  python3 ops.py routine           yesterday's daily routine + task completion per person
  python3 ops.py note <to_name> "<text>"     leave a dashboard note for a teammate (from "AI Ops")
  python3 ops.py task <owner_name> "<title>" [due YYYY-MM-DD] [client_name]   create a task on the dashboard
"""
import sys, json, datetime
import common as C

AGENT = "Ops"


def month_key():
    return C.now_ist().strftime("%Y-%m")


def health():
    today = C.now_ist().date()
    clients = C.select("clients", "select=id,name,status,stage,package,start_date,reporting_day,owner_id,smm_owner,service_type&order=name")
    posts = C.select("posts", f"select=client_id,date,status,owner_name,type&date=gte.{today.replace(day=1).isoformat()}")
    people = {p["id"]: p["name"] for p in C.select("people", "select=id,name")}
    tasks = C.select("tasks", "select=id,title,client_id,owner_id,status,due&status=neq.Done")
    for t in tasks:
        t["owner_name"] = people.get(t.get("owner_id"), "")
    fin = []
    try:
        fin = C.select("client_finance", "select=client_id,renewal_date")
    except Exception:
        pass
    renew = {f["client_id"]: f.get("renewal_date") for f in fin}
    rep = []
    for c in clients:
        if (c.get("status") or "active").lower() not in ("active", "onboarding", ""):
            continue
        cp = [p for p in posts if p["client_id"] == c["id"]]
        done = [p for p in cp if (p.get("status") or "").lower() in ("posted", "published", "done", "live")]
        pending_past = [p for p in cp if p["date"] < today.isoformat() and p not in done]
        ct = [t for t in tasks if t.get("client_id") == c["id"]]
        overdue = [t for t in ct if t.get("due") and t["due"] < today.isoformat()]
        last_post = max([p["date"] for p in done], default=None)
        gap = (today - datetime.date.fromisoformat(last_post)).days if last_post else None
        flags = []
        social = (c.get("service_type") or "social") != "video"   # video-only clients (e.g. Great Place To Work) have no calendar KPIs
        if social and not cp: flags.append("no content planned this month")
        if social and pending_past: flags.append(f"{len(pending_past)} scheduled post(s) past their date, not marked posted")
        if overdue: flags.append(f"{len(overdue)} overdue task(s)")
        if social and gap is not None and gap > 5: flags.append(f"{gap} days since last post")
        r = renew.get(c["id"])
        if r and 0 <= (datetime.date.fromisoformat(r) - today).days <= 14: flags.append(f"renewal on {r}")
        rep.append({"client": c["name"], "service_type": c.get("service_type") or "social", "smm_owner": c.get("smm_owner") or "Bhavana", "planned": len(cp), "posted": len(done), "past_due_posts": len(pending_past),
                    "open_tasks": len(ct), "overdue_tasks": [t["title"] for t in overdue][:5], "last_post": last_post,
                    "risk": "HIGH" if len(flags) >= 2 else "MEDIUM" if flags else "OK", "flags": flags})
    at_risk = [r for r in rep if r["risk"] != "OK"]
    C.log(AGENT, "health", f"{len(rep)} clients checked, {len(at_risk)} flagged: " + ", ".join(r["client"] for r in at_risk),
          detail={"at_risk": at_risk}, needs_tejas=bool([r for r in rep if r["risk"] == "HIGH"]))
    C.out({"month": month_key(), "clients": rep})


def invoices():
    clients = {c["id"]: c["name"] for c in C.select("clients", "select=id,name,invoice_day,status")}
    try:
        inv = C.select("invoices", f"select=client_id,month,amount,raised,paid_on&month=eq.{month_key()}")
    except Exception as e:
        C.out({"note": "invoices table is finance-locked for the agent login", "error": str(e)[:200]}); return
    not_raised = [clients.get(i["client_id"]) for i in inv if not i.get("raised")]
    unpaid = [clients.get(i["client_id"]) for i in inv if i.get("raised") and not i.get("paid_on")]
    missing = [n for cid, n in clients.items() if cid not in {i["client_id"] for i in inv}]
    C.log(AGENT, "invoices", f"{month_key()}: {len(not_raised)} not raised, {len(unpaid)} unpaid, {len(missing)} with no invoice row",
          needs_tejas=bool(not_raised or missing))
    C.out({"month": month_key(), "not_raised": not_raised, "unpaid": unpaid, "no_invoice_row": missing})


def due_on(r, d):
    """Mirror of the dashboard's dueOn(): daily = Mon–Sat, weekly = its weekday (0=Sun), monthly = its day."""
    import calendar
    dow = (d.weekday() + 1) % 7
    if r.get("freq") == "daily": return dow != 0
    if r.get("freq") == "weekly": return dow == int(r.get("day") or 1)
    if r.get("freq") == "monthly":
        return d.day == min(int(r.get("day") or 1), calendar.monthrange(d.year, d.month)[1]) and dow != 0
    return False


def routine():
    yd = C.now_ist().date() - datetime.timedelta(days=1)
    if yd.weekday() == 6:
        C.out({"date": yd.isoformat(), "skipped": "Sunday — no routine expected"}); return
    y = yd.isoformat()
    rec = [r for r in C.select("recurring", "select=id,title,owner_name,freq,day") if due_on(r, yd)]
    done = C.select("recur_done", f"select=recurring_id,by_name&date=eq.{y}")
    ppl = sorted({r.get("owner_name") for r in rec if r.get("owner_name")} | {"Tejas", "Snehal", "Bhavana", "Shubham"})
    d = {(x["recurring_id"], x["by_name"]) for x in done}
    per = {}
    for p in ppl:
        mine = [r for r in rec if (r.get("owner_name") in (p, "Everyone", "All", None))]
        did = [r for r in mine if (r["id"], p) in d]
        per[p] = {"routine_total": len(mine), "routine_done": len(did), "missed": [r["title"] for r in mine if r not in did][:5]}
    tasks_done = C.select("tasks", f"select=title,owner_id&status=eq.Done&done_at=eq.{y}")
    C.out({"date": y, "per_person": per, "tasks_completed_yesterday": len(tasks_done)})


TEAM = ["Bhavana", "Shubham", "Snehal", "Tejas"]


def brief(kind="morning"):
    """Per-person facts for the 10 AM start-of-day and 7 PM end-of-day messages. Nothing is sent here."""
    now = C.now_ist(); today = now.date(); t = today.isoformat()
    people = {p["name"]: p["id"] for p in C.select("people", "select=id,name")}
    clients = {c["id"]: c for c in C.select("clients", "select=id,name,status,smm_owner,service_type,posts_per_month,ig_followers,follower_target_pct")}
    tasks = C.select("tasks", "select=id,title,owner_id,client_id,due,priority,status,done_at")
    posts = C.select("posts", f"select=id,client_id,date,type,title,status,designed_at,live_at&date=gte.{(today - datetime.timedelta(days=21)).isoformat()}&date=lte.{(today + datetime.timedelta(days=3)).isoformat()}")
    rec = [r for r in C.select("recurring", "select=id,title,owner_name,freq,day,time_text") if due_on(r, today)]
    rdone = {(x["recurring_id"], x["by_name"]) for x in C.select("recur_done", f"select=recurring_id,by_name&date=eq.{t}")}
    scores = {}
    try:
        import subprocess
        for p in ("Bhavana", "Shubham"):
            j = json.loads(subprocess.run([sys.executable, "perf.py", "scorecard", p], capture_output=True, text=True, timeout=120).stdout)
            scores[p] = {"score": j.get("score"), "weakest": min([k for k in j["kpis"] if k["achieved_pct"] is not None], key=lambda k: k["achieved_pct"], default=None)}
    except Exception as e:
        scores["error"] = str(e)[:120]
    # KPI deadlines
    last_day = (today.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    d25 = today.replace(day=25) if today.day <= 25 else (last_day + datetime.timedelta(days=25))
    nm = (last_day + datetime.timedelta(days=1)).strftime("%Y-%m")
    deadlines = {"calendar_due": d25.isoformat(), "days_to_calendar_deadline": (d25 - today).days, "month_ends_in_days": (last_day - today).days, "next_month": nm}
    out = {"kind": kind, "date": t, "weekday": today.strftime("%A"), "deadlines": deadlines, "scores": scores, "people": {}}
    for name in TEAM:
        pid = people.get(name)
        mine = [x for x in tasks if x["owner_id"] == pid and x["status"] != "Done"]
        due_today = [x for x in mine if x.get("due") == t]
        overdue = [x for x in mine if x.get("due") and x["due"] < t]
        high = [x for x in mine if (x.get("priority") or "").lower() in ("high", "urgent") and (not x.get("due") or x["due"] <= (today + datetime.timedelta(days=2)).isoformat())]
        done_today = [x for x in tasks if x["owner_id"] == pid and x["status"] == "Done" and (x.get("done_at") or "")[:10] == t]
        my_rec = [r for r in rec if r.get("owner_name") in (name, "Everyone", "All")]
        rec_left = [r for r in my_rec if (r["id"], name) not in rdone]
        def tk(x): return {"title": x["title"], "client": clients.get(x.get("client_id"), {}).get("name"), "due": x.get("due"), "priority": x.get("priority")}
        person = {"tasks_due_today": [tk(x) for x in due_today], "overdue": [tk(x) for x in overdue][:8], "high_priority": [tk(x) for x in high][:6],
                  "done_today": len(done_today), "routine_total": len(my_rec), "routine_left": [r["title"] for r in rec_left][:8]}
        # content responsibilities
        my_clients = [c for c in clients.values() if c.get("status") != "Ended" and (c.get("service_type") or "social") != "video" and (c.get("smm_owner") or "Bhavana") == name]
        if my_clients:
            ids = {c["id"] for c in my_clients}
            todays = [p for p in posts if p["client_id"] in ids and p["date"] == t]
            past = [p for p in posts if p["client_id"] in ids and p["date"] < t and p["status"] != "Live"]
            person["posts_today"] = [{"client": clients[p["client_id"]]["name"], "title": p.get("title"), "type": p.get("type"), "status": p["status"]} for p in todays]
            person["posts_today_not_live"] = [x for x in person["posts_today"] if x["status"] != "Live"]
            person["past_date_not_live"] = len(past)
            nmp = {c["id"]: 0 for c in my_clients}
            for p in C.select("posts", f"select=client_id&date=gte.{nm}-01&date=lte.{nm}-31"):
                if p["client_id"] in nmp: nmp[p["client_id"]] += 1
            person["next_month_calendar"] = [{"client": clients[k]["name"], "planned": v, "quota": clients[k].get("posts_per_month") or 12} for k, v in nmp.items() if v < (clients[k].get("posts_per_month") or 12)]
        if name == "Shubham":
            soon = [p for p in posts if (p.get("type") or "").lower() in ("static", "carousel", "creative") and not p.get("designed_at")
                    and t <= p["date"] <= (today + datetime.timedelta(days=3)).isoformat()]
            person["creatives_due_soon"] = [{"client": clients.get(p["client_id"], {}).get("name"), "title": p.get("title"), "post_date": p["date"]} for p in soon][:8]
            person["creatives_past_date"] = len([p for p in posts if (p.get("type") or "").lower() in ("static", "carousel", "creative") and not p.get("designed_at") and p["date"] < t])
        out["people"][name] = person
    C.out(out)


def note(to, text, kind="note"):
    """Dashboard note + (if the person's number is on file and the template is approved) a WhatsApp copy.
    The note row keeps the WhatsApp message id so replies can be matched back."""
    row = {"to_name": to, "from_name": "AI Ops", "text": text, "read": False, "kind": kind, "created_at": C.now_ist().isoformat()}
    ins = C.insert("notes", [row]); nid = ins[0]["id"] if ins else None
    wa = None
    phones = C.config("team_phones", {}) or {}
    ph = phones.get(to) or phones.get(to.lower())
    if ph:
        try:
            r = C.wa_send_template(ph, "stirr_team_note", [to, text[:900]])
            wa = r.get("messages", [{}])[0].get("id") or "sent"
            if nid and wa: C.update("notes", f"id=eq.{nid}", {"wa_message_id": wa})
        except Exception as e:
            wa = "failed: " + str(e)[-160:]
    C.log(AGENT, "note", f"Note → {to}: {text[:120]}" + (f" (WhatsApp {wa})" if wa else ""))
    C.out({"ok": True, "id": nid, "whatsapp": wa})


def replies(hours=36):
    """Notes the AI sent recently: who replied (dashboard reply or WhatsApp), who only ticked 'got it', who did nothing.
    WhatsApp replies arrive through the webhook as outreach_messages rows (direction 'in') from a team number."""
    since = (datetime.datetime.utcnow() - datetime.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = C.select("notes", f"select=id,to_name,text,created_at,read,ack_at,reply,wa_message_id&from_name=eq.AI%20Ops&created_at=gte.{since}&order=created_at.desc")
    phones = {str(v)[-10:]: k for k, v in (C.config("team_phones", {}) or {}).items()}
    wa_in = []
    try:
        for m in C.select("outreach_messages", f"select=created_at,subject,body,external_id&direction=eq.in&channel=eq.whatsapp&created_at=gte.{since}&order=created_at.desc"):
            who = phones.get(str(m.get("subject") or "")[-10:])
            if who: wa_in.append({"from": who, "at": m["created_at"], "text": (m.get("body") or "")[:200]})
    except Exception:
        pass
    out = {}
    for n in notes:
        p = out.setdefault(n["to_name"], {"sent": 0, "replied": 0, "acknowledged": 0, "silent": [], "replies": []})
        p["sent"] += 1
        wa_reply = next((w for w in wa_in if w["from"] == n["to_name"] and w["at"] > n["created_at"]), None)
        if n.get("reply") or wa_reply:
            p["replied"] += 1; p["replies"].append({"note": n["text"][:80], "reply": n.get("reply") or wa_reply["text"], "via": "dashboard" if n.get("reply") else "whatsapp"})
        elif n.get("ack_at") or n.get("read"):
            p["acknowledged"] += 1
        else:
            p["silent"].append({"id": n["id"], "sent_at": n["created_at"], "note": n["text"][:120]})
    C.out({"since": since, "per_person": out, "whatsapp_inbound": wa_in[:20]})


def task(owner, title, due=None, client_name=None):
    # Tejas (23 Sep 2026): AI agents must not create tasks for team members. Only Tejas assigns work.
    # Agents may leave a note or escalate; the person decides whether to add a task.
    if owner.strip().lower() != "tejas":
        msg = f"REFUSED: AI agents don't assign tasks to team members (asked for {owner}: {title}). Leave a note instead."
        C.log(AGENT, "refused", msg)
        sys.exit(msg)
    cid = None
    if client_name:
        c = C.select("clients", f"select=id&name=ilike.*{client_name}*&limit=1")
        cid = c[0]["id"] if c else None
    p = C.select("people", f"select=id&name=ilike.{owner}&limit=1")
    row = {"title": title, "owner_id": p[0]["id"] if p else None, "status": "Not Started", "due": due, "client_id": cid,
           "priority": "Medium", "notes": "Created by AI Ops"}
    r = C.insert("tasks", [row])
    C.log(AGENT, "task", f"Task for {owner}: {title}" + (f" (due {due})" if due else ""))
    C.out({"ok": True, "id": r[0]["id"] if r else None})


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else "health"
    if cmd == "health": health()
    elif cmd == "invoices": invoices()
    elif cmd == "routine": routine()
    elif cmd == "note": note(a[1], a[2], a[3] if len(a) > 3 else "note")
    elif cmd == "brief": brief(a[1] if len(a) > 1 else "morning")
    elif cmd == "replies": replies(int(a[1]) if len(a) > 1 else 36)
    elif cmd == "task": task(a[1], a[2], a[3] if len(a) > 3 else None, a[4] if len(a) > 4 else None)
    else: print(__doc__)
