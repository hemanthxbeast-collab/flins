# JARVIS phase 6b: your own modes, per-contact topics, hold

This phase changes how incoming messages are decided, so the eval set grows first and every
new input to `decide()` is one that can only **lower** the level.

## What is built
### 1. Modes you define (DND, sleep, travel, anything)
A profile in `config/policy.yaml` is a named variant of one of the three busy modes. It gets
its own phrases, holding reply, default length and interrupt threshold:

```yaml
profiles:
  sleep:
    base: AWAY                      # WORKING, FOCUS or AWAY
    say: ["sleep mode", "i'm going to sleep", "good night flins"]
    default_duration: 8h
    holding_template: sleep
    interrupt_threshold: 3          # only emergencies get through
```
"sleep mode", "I'm going to sleep till 7am", "travel mode for 3 hours" then work by voice,
self-chat, CLI and dashboard, exactly like "working mood till 6". The example policy ships
`dnd`, `sleep` and `travel`.

No new entry in the mode state machine: the state is still WORKING / FOCUS / AWAY plus a
profile name, so expiry, restart, the kill switch and cancel behave as before.

### 2. Hold
"hold Rahul", "hold Rahul till 6", "release Rahul", "who is on hold".
While someone is on hold FLINS sends them **nothing** (no holding reply, no draft): their
messages wait in your summary. An urgent message still alerts you. A hold always ends: at
the time you give, else when the current mode ends, else after 4 hours; never more than 12.

### 3. Per-contact topics
`flins contacts topics "<name>" greeting_smalltalk availability_query` limits what FLINS may
answer by itself for that person; every other topic becomes a draft for your approval.
`flins contacts topics "<name>" all` removes the limit; `flins contacts topics "<name>"`
shows it. (The column and the rule existed since M2; there was no way to set it.)

## Files
- `core/policy_schema.py`: `ProfilePolicy` gains `say`, `default_duration`,
  `holding_template`, `interrupt_threshold`, `label`; validation.
- `core/grammar.py`: `parse(..., profiles=)` matches profile phrases.
- `core/modes.py`: a profile's default duration.
- `core/policy.py`: `DecisionInputs.hold`; profile template and interrupt threshold.
- `core/holds.py` + migration `0015_holds.sql`: `HoldStore`, hold phrases.
- `core/pipeline.py`: passes the active hold to `decide()`.
- `core/commands.py`: hold commands; profiles to the grammar.
- `core/readback.py`: says the profile's name.
- `cli.py`: `flins contacts topics`, `flins holds`.
- `evaluation.py`, `tests/eval/build_cases.py`: cases with `profile`, `hold`, `topics`.
- `tests/unit/test_j6b_modes_holds.py`.

## Safety rules (each has a test)
1. **A profile can't out-rank its mode.** Its ceiling is one more term in the `min()`; the
   mode's own ceiling still applies. Invariant 7 (money, codes, health, legal, conflict →
   NOTIFY) is untouched by any profile, hold or topic list.
2. **A profile always expires.** Its duration must be positive and at most 12 hours; without
   one the base mode's is used.
3. **A profile phrase can never shadow a fixed command.** "stop", "resume", "status", "I'm
   done", approvals are matched first; a policy that uses one of them as a phrase is refused
   at load. Phrases are at least 3 characters.
4. **A hold only lowers** (ceiling NOTIFY) and always has an end time.
5. **A topic list only lowers** (ceiling DRAFT for topics not on it). Listing a
   NOTIFY-only topic changes nothing.
6. **Loosening is not a voice command.** Removing a topic limit is CLI-only. Releasing a
   hold by voice is allowed: it restores what the policy already said, nothing more.
7. Everything shows in the decision trace (`ceilings.hold`, `ceilings.profile`,
   `ceilings.allowed_categories`) and therefore in `audit_log`.

## Decisions made where the spec is silent
- "HOLD" is read as a per-contact pause (see DECISIONS.md), the cautious reading: it stops
  replies, it never starts any.
- An emergency from someone on hold does not get the automatic "saw this, alerting him"
  reply (a hold means send nothing) but does interrupt you.

## Acceptance
- Eval grows from 162 to 200+ cases, still 0 unsafe, no good case over-blocked.
- `ruff`, `mypy`, `pytest`.
- Live on a throwaway database with made-up contacts: phrases, hold, topics end to end.
