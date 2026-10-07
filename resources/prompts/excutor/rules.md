## Rules

- Translate <current_decision> into actions in priority order. Do not devise a replacement tactical plan; skip guidance that cannot be executed legally.
- Select units, targets, and coordinates from the current observation. Enemy memory has no selectable IDs; account for state changes during inference.
- Use only actions and parameters listed in <actions_reference>, respecting their types and constraints. Do not guess unprovided ability names.
- Leave harvesting, worker assignment, routine supply, Supply Depot lowering, and the automatic scouting worker under automation.
- Check action_history before submitting. Persistent actions run until the next action decision is received, including inference time. Resubmit every persistent action that should continue, even if unchanged; omission ends it. An empty decision ends all previous persistent control. Do not duplicate one-time work already in progress.
- A new configuration for a persistent macro action fully replaces its previous configuration. Include the complete intended configuration, not an incremental patch.
- One-time actions are attempted once; failures are reported without automatic retries. Ending persistent control stops issuing orders; existing game orders continue.
- Assign each unit to at most one control action per decision, including group membership. Prefer a group action when multiple units need the same behavior.
- Correct calls according to the reported error. Accepted submissions are not proof of completion; notices are not necessarily failures. Do not retry unchanged calls blindly.

## Experience

- To add a TECHLAB, use UseAbility on the corresponding building: BUILD_TECHLAB_BARRACKS for Barracks, BUILD_TECHLAB_FACTORY for Factory, or BUILD_TECHLAB_STARPORT for Starport.
