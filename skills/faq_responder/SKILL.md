---
name: faq_responder
description: Drafts answers to common questions ("what's the office address?") using only the answers you wrote in config/faq.yaml.
---

# faq_responder

Matches the incoming message against the question phrases in `config/faq.yaml` (60% of the
phrase's words must appear). It answers only with your exact text, and only to contacts whose
tier is listed for that answer. Unknown numbers never get an answer. The answer is handed to
FLINS's AI drafter as a verified fact (`faq_answer`), so the draft can use it; you approve it.
