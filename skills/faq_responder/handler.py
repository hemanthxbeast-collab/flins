"""Answers only from config/faq.yaml, and only for the tiers each answer allows."""

import re

STOP = {"the", "is", "are", "what", "whats", "your", "you", "his", "her", "can", "please", "for"}
MIN_SCORE = 0.6


def words(text):
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in STOP}


def best_match(message, faq):
    said = words(message)
    best, best_score = None, 0.0
    for item in faq:
        for phrase in item["q"]:
            want = words(phrase)
            if not want:
                continue
            score = len(want & said) / len(want)
            if score > best_score:
                best, best_score = item, score
    return best if best_score >= MIN_SCORE else None


def run(inp, ctx):
    contact = inp.get("contact")
    if not contact:
        return {"proposals": [], "logs": ["no contact card: FAQ answers are for known people only"]}
    try:
        faq = ctx.faq()
    except Exception as exc:
        return {"proposals": [], "logs": [f"faq unavailable: {exc}"]}
    item = best_match(" ".join(inp.get("messages", [])), faq)
    if item is None:
        return {"proposals": []}
    if contact["tier"] not in item["tiers"]:
        return {"proposals": [], "logs": [f"answer not shared with tier {contact['tier']}"]}
    text = f"Hi {contact['first_name']}, {item['a']}"
    if inp.get("signature"):
        text = f"{text} {inp['signature']}"
    return {
        "proposals": [{"kind": "reply", "text": text, "confidence": 0.85}],
        "facts": {"faq_answer": item["a"]},
    }
