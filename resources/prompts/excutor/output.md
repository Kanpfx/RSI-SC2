- Return exactly one `## actions` heading, a blank line, then 0-{max_actions} DSL calls, one complete call per line.
- Use `ActionName(argument=value, ...)` with named arguments only; no numbering or bullets.
- If no action is needed, return only `## actions`.
- Use actual line breaks; no code fences, explanations, or additional sections.

```text
## actions

ActionName(argument=value,...)
```
