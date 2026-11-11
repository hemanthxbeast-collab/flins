# J3b: planning (JARVIS phase 3b)

Goal: "make a plan to prepare for my thermodynamics exam" gives an ordered list of steps that
lands on the task list once you agree, and that FLINS can walk you through ("what's next").

## What gets built
| Piece | Where | Notes |
|---|---|---|
| Table | migration 0014 | `plans` (title, goal, status open / done / cancelled, source, channel, created / updated). `tasks` gains `plan_id` and `step_no`: **a plan step is a task**, so reminders, "done with …", the list and the dashboard all work on steps with no new code path |
| Planner | `assistant/plans.py` + `prompts/plan_v1.md` | One model call (`reason` job, no tools) that returns a title and 2–10 short steps, or one question when the goal is too vague. It sees the time and your open tasks, nothing else |
| Proposal, then yes | `PlanCommands` | The plan is shown and FLINS asks "Save this plan?". Only "yes" writes anything. "no", silence or a failed model call writes nothing |
| Commands (no model) | `PlanCommands` | show my plans / what's my plan for X; what's next / next step; delete the plan [X] (asks first) |
| Progress | `TaskStore` | Finishing a step says what is next; finishing the last one closes the plan. Deleting a plan cancels its open steps |
| Context | `<tasks>` lines | Steps carry "[plan title, step n]" so the conversation model knows the order |
| CLI / dashboard | `flins plans`, `/tasks` | list, show, delete; the Tasks page lists plans with progress |

## Safety and honesty
- The model proposes; a row exists only after your "yes" (same rule as memory suggestions).
- Steps are things **you** do. No step can act: there are no tools yet (phase 4). When tools
  arrive, a step that acts gets an approval gate before it runs; the `plans` / `tasks` shape
  leaves room for that (a step kind), but nothing is built ahead of need.
- No times are invented for steps. You add them with "remind me to … at 7".
- Model text is clipped, neutralised and deduplicated in code before it is shown or stored.
- Plans never touch the WhatsApp pipeline or the Outbox.

## Tests
proposal needs yes; no / failure / no model / budget write nothing; vague goal → question;
steps become ordered tasks; next step; finishing steps and the plan; delete with confirmation;
self-chat addressing; "what's my plan for today" with no such plan falls through to
conversation; model output cleaned; CLI; dashboard; no send path in the module.
