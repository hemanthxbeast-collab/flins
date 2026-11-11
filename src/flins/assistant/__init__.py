"""The assistant core: one path for everything you say to FLINS, on any channel.

See docs/JARVIS_ARCHITECTURE.md. Messages from *other people* never come through here: they
stay in core/pipeline.py, where the policy engine decides and the Outbox is the only sender.
"""
