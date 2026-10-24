"""Optional diagnosis ablations and their fixed evidence schedule."""


def diagnosis_prompt(prompt, options):
    """Apply the configured diagnosis ablations to the standard task prompt."""
    if not options["competing_hypotheses"]:
        prompt = prompt.replace("at least two competing hypotheses", "one hypothesis")
    if options["query_policy"] == "fixed":
        prompt = prompt.replace(
            "Use the query tools to test the hypotheses; revise them with select_issue when evidence changes.",
            "Use only the predeclared evidence pack; do not call query_logs, read_reference or query_memory.")
    return prompt


def fixed_pack(retriever, batches, max_queries):
    """Read a predeclared evidence schedule for the fixed-policy ablation."""
    queries = []
    schedule = []
    for batch in batches:
        for kind, params in (("overview", {}),
                             ("metrics", {"names": ["minerals", "vespene", "supply_left", "workers"]}),
                             ("events", {})):
            schedule.append(("query_logs", dict(kind=kind, purpose="Predeclared fixed schedule",
                                               params=dict(batch_id=batch["batch_id"], **params))))
    for path in ("components/tactics/bc_rush.py", "components/tasks/economy.py", "components/tasks/tech.py"):
        schedule.append(("read_reference", dict(path="source/" + path)))
    for tool, arguments in schedule[:max_queries]:
        result = (retriever.query_logs(arguments["kind"], **arguments["params"]) if tool == "query_logs"
                  else retriever.read_reference(**arguments))
        queries.append(dict(tool=tool, arguments=arguments, result=result))
    return queries

