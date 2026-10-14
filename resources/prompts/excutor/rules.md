## Rules

- Translate the `guidence` section of <current_decision> into actions in priority order; use the other sections as context. Do not devise a replacement tactical plan; skip guidance that cannot be executed legally.
- Select units, targets, and coordinates from the current observation. Enemy memory has no selectable IDs; account for state changes during inference.
- Use only listed actions and parameters, respecting their types and constraints. Use ability names provided in <actions_reference> or the current observation; never invent unknown names.
- Leave harvesting, worker assignment, routine supply, Supply Depot lowering, and the automatic scouting worker under automation.
- Check action_history before submitting. Persistent actions run until the next action decision is received, including inference time. Resubmit every persistent action that should continue, even if unchanged; omission ends it. An empty decision ends all previous persistent control. Do not duplicate one-time work already in progress.
- A new configuration for a persistent macro action fully replaces its previous configuration. Include the complete intended configuration, not an incremental patch.
- Later valid actions override earlier control of overlapping units; groups retain unaffected members. Persistent macro configurations replace the same scope.
- One-time actions are attempted once; failures are reported without automatic retries. Ending persistent control stops issuing orders; existing game orders continue.
- Assign each unit to at most one control action per decision, including group membership. Prefer a group action when multiple units need the same behavior.
- Check action_history and reported errors before retrying. Accepted submissions do not imply completion; do not repeat failed calls unchanged.

## Experience

- To add a TECHLAB, use UseAbility on the corresponding building: BUILD_TECHLAB_BARRACKS for Barracks, BUILD_TECHLAB_FACTORY for Factory, or BUILD_TECHLAB_STARPORT for Starport.
