from rsi.evaluation.selector import eligible


def select_parents(nodes, beam_width):
    return eligible([node for node in nodes if not node["expanded"]])[:beam_width]
