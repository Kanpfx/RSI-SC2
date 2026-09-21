Content:
- Choose actions from the action reference, using documented argument names, types, and permitted values.
- Use <core_missions> as your primary decision guide. Focus on translating its phase, priorities, and concrete guidance into executable actions, subject to <general_guidance>, <observation>, and <actions_reference>. Do not reassess the tactical phase or output working memory.

Format:
- Return 0-{max_actions} actions, one DSL call per line. If no new action is needed, return only `# actions`.
- Use bare names for enums and landmarks, true/false for booleans, [...] for lists, and {key:value} for objects.
- Return only the format below, with actual line breaks and no explanations.

```text
# actions
ActionName(argument=value,...)
```
