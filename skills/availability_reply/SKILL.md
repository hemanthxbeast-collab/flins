---
name: availability_reply
description: For "are you free?" messages, finds the first free half hour today in your calendar and drafts a reply with it.
---

# availability_reply

Reads busy times (never titles) from the `.ics` file set as `skills.calendar_ics` in
`config/flins.yaml`, starting after the current mode ends, until 22:00. It returns a
`free_at` fact and a suggested reply. FLINS's AI drafter writes the actual draft using `free_at`,
with all its checks, and you approve it (calendar details are private, so never AUTO). With no
calendar file it does nothing.
