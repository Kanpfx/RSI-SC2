Content:
- Choose actions from the action reference, using documented argument names, types, and permitted values.
- Working memory should briefly record current priorities and next intentions.

Format:
- Return 0-{max_actions} actions, one DSL call per line.
- Use bare names for enums and landmarks, true/false for booleans, [...] for lists, and {key:value} for objects.
- Optionally include # working (up to {max_working_chars} characters); omit to preserve memory, leave empty to clear.
- Return only the format below, with actual line breaks and no explanations.

```text
# actions
ActionName(argument=value,...)

# working
Brief current priorities and next intentions.
```
