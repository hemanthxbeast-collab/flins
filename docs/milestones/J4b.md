# JARVIS phase 4b: online tools

Goal: FLINS can answer from three public sources when you ask: the weather, Wikipedia and
GitHub. Phase 4a built the tool layer (the model asks, code checks and runs); this adds the
first tools whose input leaves the PC. **Off by default**: nothing goes online until you set
`assistant.tools.online.enabled: true`.

## What is built
| Tool | Source (no account needed) | Input |
|---|---|---|
| `weather` | Open-Meteo (geocoding + forecast) | a place name; empty = your `weather_place` |
| `wikipedia` | English Wikipedia API (needs `wikipedia_contact`) | a topic |
| `github` | GitHub REST API, read-only | `owner/repo`, `owner/repo issues / pulls / commits / releases`, `repos [user]` |

Not built: general web search and news. Every usable search service needs an account and a
key, which is the user's choice of provider. The fetcher and the rules below are ready for it.

## Files
- `src/flins/assistant/tools/net.py`: `Fetcher.get_json` (the only way a tool goes online)
  and `safe_query` (what may be sent).
- `tools/weather.py`, `tools/wiki.py`, `tools/github.py`: one `make_*_tool` each.
- `tools/base.py`: risk `online`; a tool may set its own `timeout_s`.
- `settings.py`: `assistant.tools.online` (`enabled`, `allow`, `weather_place`,
  `wikipedia_contact`, `github_user`, `github_token_env`).
- `wiring.py`: `online_tools`, `all_tools`, `tool_box`.
- `cli.py`: `flins tools` shows the online tools and whether they are on.
- `prompts/chat_v6.md`: when to use them and what must not go into their input.
- `tests/unit/test_j4b_online.py`. No test touches the network (`httpx.MockTransport`).

## Rules (each has a test)
1. **One door.** In `assistant/tools/` only `net.py` imports `httpx`. `Fetcher` can only GET,
   only `https`, only four hosts fixed in code (`api.open-meteo.com`,
   `geocoding-api.open-meteo.com`, `en.wikipedia.org`, `api.github.com`), never follows a
   redirect, stops reading after 1 MB and has a timeout.
2. **Off until you say so.** With the default config the online tools are not in the tool box
   and not in the model's `<tools>` list.
3. **What goes out is checked by code.** Input is short, and refused when it looks like a
   secret, an email address, a phone number or any long run of digits. `weather` accepts only
   a place name; `github` only the fixed forms above. The model never builds a URL.
4. **What comes back is outside content.** Same rule as files: neutralised, labelled, and
   after it no further tool and no memory suggestion in that turn. So a web result can't make
   FLINS do anything, and a file's text can't be sent online in the same turn (a file read
   taints the turn before any online tool could run).
5. **The GitHub token is optional and stays put.** Read from `.env` (`GITHUB_TOKEN`), sent only
   in the header of requests to `api.github.com`, never in a result, a log or the audit row.
6. **No contact details are sent unless you give them.** Requests carry a generic client
   name. Wikipedia refuses that (403), so its tool exists only when you set
   `wikipedia_contact`, and that value goes to Wikipedia only.
7. **Audit** as in 4a: which tool, risk `online`, ok, time, sizes. Never the input or result.
8. **Failures are plain.** Offline, timeout, rate limit, not found: one sentence, never a
   made-up answer.

## Acceptance
- Unit tests for each tool against recorded-shape responses, the fetcher's refusals, the input
  check, the config gate, the taint rule and the audit row.
- `ruff`, `mypy`, `pytest`, eval (0 unsafe of 162) all pass.
- Live check with the real services: done on 2026-11-12, all three tools (Wikipedia with
  a generic contact string, since the user's own was not set).
