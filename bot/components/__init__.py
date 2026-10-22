"""Editable business modules for scoped RSI experiments.

Tactics owns global goals and cross-task unit allocation. Tasks choose local
targets and compose capabilities. Behaviors implement local control without
deciding global resource budgets or issuing native Unit/BotAI commands.

Invoke modules through frame.run so priorities and call ancestry are tracked.
Read native observations through frame.game and propose commands through
frame.actions. An execute() result of True means control is handled for at
least one subject; it never proves arbitration or engine success. Group callers
must explicitly route unhandled subjects rather than assume all units acted.

Keep only necessary local state across calls and pass business goals explicitly.
Core, Spatial and telemetry remain outside this editable business boundary.
"""
