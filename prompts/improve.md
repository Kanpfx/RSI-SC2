Implement only the assigned candidate direction in bot/. The supplied context
contains the parent code, evaluation and lineage. Use the actual editing tools;
a prose proposal does not count as completion. Keep changes small and focused.
Keep bot.main.SeedBot as a subclass of sc2.bot_ai.BotAI, safe to import.
Never modify rsi/, config/, prompts/, tests/, Git metadata or the benchmark.
Do not change branches or make commits. Do not read credentials or use network,
filesystem or subprocess code to access anything outside the bot's game needs.
Allowed run_command argv values:
["python", "-m", "compileall", "-q", "bot"]
["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_bot_smoke.py"]
["python", "-c", "import bot.main"]
["rg", "-n", "--", "PATTERN", "bot"]
Run the fixed smoke pytest after your edit; fix code failures before finishing.
Finish with a concise summary of the one main change, expected effect, and checks.

Before adding or using an uncertain SC2 API, call lookup_sc2_api to check its
signature, whether it is async, its docstring and source in the installed version.
Use a qualified name (BotAI.build, Unit.train) or a method name (build). If several
matches are returned, query the full symbol. Inherited methods are listed under
their defining class; search by method name if a class-qualified lookup is missing.
