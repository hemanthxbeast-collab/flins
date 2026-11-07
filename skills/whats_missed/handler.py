"""'What did I miss?': recent messages grouped by person, urgent ones first."""

from datetime import datetime

FMT = "%Y-%m-%d %H:%M"
DONE = {
    "auto": "FLINS replied",
    "draft": "draft waiting",
    "notify": "needs you",
    "ignore": "ignored",
}


def _hours(inp):
    if not inp.get("since_local"):
        return 8
    gap = datetime.strptime(inp["now_local"], FMT) - datetime.strptime(inp["since_local"], FMT)
    return max(0.25, min(24, gap.total_seconds() / 3600))


def _short(text, n=60):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def run(inp, ctx):
    try:
        recent = ctx.messages.recent(hours=_hours(inp))
    except Exception as exc:
        return {"proposals": [], "logs": [f"messages unavailable: {exc}"]}
    if not recent:
        return {"proposals": [{"kind": "note", "text": "Nothing new while you were busy."}]}
    people = {}
    for m in recent:
        p = people.setdefault(m["who"], {"n": 0, "urgent": False, "scam": False, "last": "", "actions": set()})
        p["n"] += 1
        p["urgent"] = p["urgent"] or bool(m.get("urgent"))
        p["scam"] = p["scam"] or "credentials" in (m.get("flags") or [])
        p["last"] = m["text"]
        if m.get("action"):
            p["actions"].add(m["action"])
    order = sorted(people.items(), key=lambda kv: (not kv[1]["urgent"], -kv[1]["n"], kv[0]))
    lines = []
    for who, p in order:
        mark = "⚠ " if p["urgent"] else ""
        what = ", ".join(DONE.get(a, a) for a in sorted(p["actions"])) or "not handled"
        if p["scam"]:
            what += ", looked like a scam"
        count = f" ({p['n']})" if p["n"] > 1 else ""
        lines.append(f'{mark}{who}{count}: "{_short(p["last"])}" ({what})')
    head = f"{len(recent)} message{'s' if len(recent) != 1 else ''} from {len(people)} {'person' if len(people) == 1 else 'people'}."
    return {"proposals": [{"kind": "note", "text": " ".join([head, *lines])}]}
