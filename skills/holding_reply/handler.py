"""Mode-aware holding reply: "{user} is working until {until}, will reply after"."""

TEXT = {
    "WORKING": (
        "Hey {name}! {user} is busy with work until {until} and will get back to you after that."
    ),
    "FOCUS": (
        "Hey {name}! {user} is in deep-work mode until {until} and will reply after that. "
        "If it's urgent, just say 'urgent'."
    ),
    "AWAY": "Hey {name}! {user} is away right now and will get back to you after {until}.",
}


def run(inp, ctx):
    template = TEXT.get(inp["mode"])
    if template is None:
        return {"proposals": []}
    contact = inp.get("contact") or {}
    text = template.format(
        name=contact.get("first_name") or "there",
        user=inp["user_name"],
        until=inp.get("until_local") or "a while",
    )
    if inp.get("signature"):
        text = f"{text} {inp['signature']}"
    return {"proposals": [{"kind": "reply", "text": text, "confidence": 1.0}]}
