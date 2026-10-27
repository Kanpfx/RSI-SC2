"""Optional diagnosis ablations and their fixed evidence schedule."""


def diagnosis_prompt(prompt, options):
    """Append explicit experiment rules without depending on the base prompt's wording."""
    hypotheses = "at least two competing hypotheses" if options["competing_hypotheses"] else "one hypothesis"
    rules = [f"Start with select_issue: record one issue and {hypotheses}."]
    if options["query_policy"] == "fixed":
        rules.append("Use only the predeclared evidence pack; do not call query_logs, read_reference or query_memory.")
    else:
        rules.extend((
            "Query evidence to test hypotheses.",
            "Log, reference and memory queries share max_queries; keep reads focused.",
        ))
    rules.append("Revise the issue or hypotheses with select_issue as evidence changes; explain revisions in reason.")
    return "\n\n".join((prompt.rstrip(), "\n".join(rules)))


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

