# MightBeAWorkerRush

**Core idea:**
Launch a delayed SCV pressure with a hidden Barracks near the enemy main, then let Marines and a small one-base economy continue the attack if the worker rush does not decide the game.

**General guidance:**

- Check emergency_defence first, then worker_pressure. Otherwise use opening, approach, worker_pressure, proxy_marine_followup.
- Send only one SCV to build the forward Barracks; the remaining attacking SCVs are the pressure group. Do not abandon all home mining.
- Keep the strategy on one base with Marines and no gas, upgrades, expansion, or optional technology unless a later strategic instruction replaces it.
- Use damaged SCVs as a repair group (`EFFECT_REPAIR_SCV`) and return them to the fighting group when healthy enough.

## opening

**When to choose:**

- The delayed worker attack has not yet been assigned.

**Phase goal:**

- Prepare a compact worker force and a hidden forward Barracks plan.

**Guidance:**

- Train two SCVs, build one Supply Depot, and keep mining until the delayed attack timing begins.
- Do not build a normal home Barracks before the proxy plan is underway.

## approach

**When to choose:**

- The attack timing has begun.
- The forward Barracks is not yet complete and the worker group has not fully engaged.

**Phase goal:**

- Move the pressure group across the map and start the proxy Barracks.

**Guidance:**

- Send the worker group toward the enemy; use one designated SCV as the proxy builder.
- Keep workers together while crossing and briefly compact them with nearby minerals if needed.
- Start the forward Barracks at a buildable location near the enemy main and protect its builder when possible.

## worker_pressure

**When to choose:**

- Attacking SCVs are at the enemy base or fighting enemy workers and early units.

**Phase goal:**

- Win local worker fights while keeping the group repairable.

**Guidance:**

- Attack exposed workers and weak units with the main group.
- If the enemy wall is closed, threaten exposed workers or regroup around the best target instead of endlessly attacking the wall.
- Move low-health SCVs behind the healthy frontline so they can be repaired and returned.

## proxy_marine_followup

**When to choose:**

- The forward Barracks is complete or Marines are available to support worker pressure.

**Phase goal:**

- Use Marines to preserve pressure after the first SCV clash.

**Guidance:**

- Produce Marines from the proxy and send them to the attack target.
- Continue a small one-base Marine economy with about fourteen workers and do not add gas or expansion.
- If worker pressure loses momentum, use Marines to cover surviving SCVs while they repair or retreat.

## emergency_defence

**When to choose:**

- Enemy forces threaten the main base before the worker pressure has gained a decisive advantage.

**Phase goal:**

- Avoid losing the game to a counterattack.

**Guidance:**

- Preserve enough SCVs and nearby Marines to prevent immediate loss at home.
- Do not keep feeding the forward worker group if the counterattack is more dangerous than the current pressure.
