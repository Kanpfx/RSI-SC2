def ranking(node):
    return (-node["wins"], node["generation"], node["created_order"])


def eligible(nodes):
    return sorted((node for node in nodes if node["crashes"] == 0), key=ranking)
