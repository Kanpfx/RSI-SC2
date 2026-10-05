## Rules

- Translate <current_decision> into actions in priority order. Do not devise a replacement tactical plan; skip guidance that cannot be executed legally.
- Select units, targets, and coordinates from the current observation. Enemy memory has no selectable IDs; account for state changes during inference.
- Use only actions and parameters listed in <actions_reference>, respecting their types and constraints. Do not guess unprovided ability names.
- Leave harvesting, worker assignment, routine supply, Supply Depot lowering, and the automatic scouting worker under automation.
- Check action_history before submitting. Omit unchanged persistent actions, queued tasks, and identical work already in progress; omission does not cancel persistent control.
- A new configuration for a persistent macro action fully replaces its previous configuration. Include the complete intended configuration, not an incremental patch.
- Assign each unit to at most one control action per decision, including group membership. Prefer a group action when multiple units need the same behavior.
- Correct calls according to the reported error. Accepted submissions are not proof of completion; notices are not necessarily failures. Do not retry unchanged calls blindly.

## Experience
