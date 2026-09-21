Content:
- Determine the current phase from the complete tactic and observation.
- Use <actions_reference> only as a reference for capability boundaries. Give natural-language guidance, not specific action calls, argument assignments, or DSL.
- Give only concrete instructions that can be executed immediately in the observed state.
- Make each item atomic: one operation on one unit or group, structure, production target, or research target. Do not combine multiple steps in one item.
- Do not include future or conditional steps such as "after the Barracks finishes, build a Factory" or "when resources allow".
- Do not use vague instructions such as "do not retry", "wait for completion", "maintain production", or "prepare to expand". Omit unchanged or pending work instead of describing it as a new task.
- When guidance requires an ability, preserve its exact identifier from the tactic in backticks.

Format:
- Keep Guidance to at most 3 concrete numbered items and 400 characters total.
- Return only the format below, with actual line breaks and no DSL or explanations.

```text
## Current phase
Current phase name.

## Guidance
Brief, actionable guidance.
```
