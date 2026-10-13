"""Orchestrator digest — everything Tejas needs to decide on, in one JSON blob.

Usage: python3 digest.py [hours_back]     (default 24)
Also: python3 digest.py ack               mark all needs_tejas activity as seen (after the digest is sent)
"""
import sys, datetime
import common as C


def build(hours=24):
    since = (C.now_ist() - datetime.timedelta(hours=hours)).astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = C.now_ist().date().isoformat()
    act = C.select("agent_activity", f"select=agent,action,summary,needs_tejas,created_at,lead_id&created_at=gte.{since}&order=created_at.desc&limit=200")
    new_leads = C.select("leads", f"select=id,name,segment,score,why,stage,email,contact_phone,instagram&created_at=gte.{since}&order=score.desc&limit=30")
    esc = C.select("leads", "select=id,name,stage,next_action,score,notes&needs_tejas=eq.true&stage=not.in.(Won,Lost)&order=updated_at.desc&limit=20")
    hot = C.select("leads", "select=id,name,stage,next_date,next_action,est_value,kind&stage=in.(Meeting,Proposal,Negotiation)&order=next_date.asc.nullslast")
    stale = C.select("leads", f"select=id,name,stage,next_date,owner_name&stage=in.(Meeting,Proposal,Negotiation)&next_date=lt.{today}")
    inbound = [a for a in act if a["action"] in ("reply_in", "inbound")]
    pipeline = {}
    for l in C.select("leads", "select=stage"):
        pipeline[l["stage"]] = pipeline.get(l["stage"], 0) + 1
    goal = None
    try:
        goal = C.rpc("goal_progress")
    except Exception:
        pass
    C.out({"generated": C.now_ist().isoformat(), "window_hours": hours,
           "needs_tejas": [a for a in act if a.get("needs_tejas")],
           "inbound_replies": inbound, "new_leads": new_leads, "escalated_leads": esc,
           "hot_pipeline": hot, "stale_hot_leads": stale, "pipeline_counts": pipeline, "goal": goal,
           "agent_activity_counts": _counts(act)})


def _counts(act):
    c = {}
    for a in act:
        c[a["agent"]] = c.get(a["agent"], 0) + 1
    return c


def ack():
    C.update("agent_activity", "needs_tejas=eq.true", {"needs_tejas": False})
    C.out({"ok": True})


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "ack":
        ack()
    else:
        build(int(a[0]) if a else 24)
