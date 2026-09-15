def summarize(records):
    counts = dict(wins=0, losses=0, ties=0, crashes=0)
    keys = {"win": "wins", "loss": "losses", "tie": "ties", "crash": "crashes"}
    for record in records:
        result = record["result"]
        if result not in keys or record["crashed"] != (result == "crash"):
            raise ValueError("Inconsistent game result")
        counts[keys[result]] += 1
    return {"games": len(records), **counts, "results": records}
