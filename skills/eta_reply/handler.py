"""'Where are you?' while AWAY: a fixed "out right now, back around {until}" reply."""


def run(inp, ctx):
    if inp["mode"] != "AWAY":
        return {"proposals": []}
    contact = inp.get("contact") or {}
    until = inp.get("until_local")
    when = f"should be back around {until}" if until else "will reply when back"
    name = contact.get("first_name") or "there"
    text = f"Hi {name}, {inp['user_name']} is out right now and {when}."
    if inp.get("signature"):
        text = f"{text} {inp['signature']}"
    return {"proposals": [{"kind": "reply", "text": text, "confidence": 1.0}]}
