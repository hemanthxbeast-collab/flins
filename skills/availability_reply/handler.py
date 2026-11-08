"""'Are you free?': looks at the calendar for the first free half hour today."""

from datetime import datetime, timedelta

FMT = "%Y-%m-%d %H:%M"
MIN_GAP = timedelta(minutes=30)


def _at(day, hhmm):
    h, m = hhmm.split(":")
    return day.replace(hour=int(h), minute=int(m))


def first_free(start, end, busy):
    t = start
    for block in busy:
        b_start = datetime.strptime(block["start"], FMT)
        b_end = datetime.strptime(block["end"], FMT)
        if b_start - t >= MIN_GAP:
            return t
        t = max(t, b_end)
    return t if end - t >= MIN_GAP else None


def run(inp, ctx):
    now = datetime.strptime(inp["now_local"], FMT)
    start = now
    if inp.get("until_local"):
        start = max(now, _at(now, inp["until_local"]))
    end = now.replace(hour=22, minute=0)
    if end - start < MIN_GAP:
        return {"proposals": [], "logs": ["too late today to suggest a time"]}
    try:
        busy = ctx.calendar.free_busy(start=start.strftime(FMT), end=end.strftime(FMT))
    except Exception as exc:  # no calendar configured, or denied
        return {"proposals": [], "logs": [f"calendar unavailable: {exc}"]}
    free = first_free(start, end, busy)
    if free is None:
        return {"proposals": [], "facts": {"free_today": "no"}}
    free_at = free.strftime("%H:%M")
    name = (inp.get("contact") or {}).get("first_name") or "there"
    text = (
        f"Hi {name}, going by the calendar {inp['user_name']} looks free from {free_at} today. "
        "I'll pass on that you asked."
    )
    if inp.get("signature"):
        text = f"{text} {inp['signature']}"
    return {
        "proposals": [
            {"kind": "reply", "text": text, "confidence": 0.9, "facts_used": ["free_at"]}
        ],
        "facts": {"free_at": free_at},
    }
