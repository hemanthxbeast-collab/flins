---
name: holding_reply
description: Fixed, mode-aware holding reply ("Hemanth is busy with work until 18:00 and will get back to you after that").
---

# holding_reply

Answers small talk, "are you free?" and unclear messages while a busy mode is on, with a fixed
sentence per mode. No AI is involved, so it can run at AUTO_SAFE. It never says what the user is
doing beyond the mode, and never promises anything except replying later.

Input used: `mode`, `until_local`, `user_name`, `signature`, `contact.first_name`.
