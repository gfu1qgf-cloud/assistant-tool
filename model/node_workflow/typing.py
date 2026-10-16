"""Graph-wide type unification, not unchecked wildcard connections."""


def resolve_port_types(graph, registry):
    """Return effective types and conflicts for (node id, output?, port key)."""
    parents, declared, groups = {}, {}, {}

    def find(key):
        root = key
        while parents[root] != root:
            root = parents[root]
        while key != root:
            previous = parents[key]
            parents[key] = root
            key = previous
        return root

    def union(a, b):
        first, second = find(a), find(b)
        if first != second:
            parents[second] = first

    for node in graph["nodes"]:
        definition = registry.definitions.get(node["kind"])
        if definition is None:
            continue
        for output, ports in ((False, definition.input_ports(node['params'])), (True, definition.output_ports(node['params']))):
            for port in ports:
                key = (node["id"], output, port.key)
                parents[key] = key
                declared[key] = port.data_type
                if port.type_group:
                    group = (node["id"], port.type_group)
                    if group in groups:
                        union(key, groups[group])
                    else:
                        groups[group] = key
    for source, output, target, input_ in graph["connections"]:
        a, b = (source, True, output), (target, False, input_)
        if a in parents and b in parents:
            union(a, b)
    # Feedback is an implicit, typed edge, not an unchecked runtime assignment.
    from .loops import paired_blocks
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs, _ends, _errors = paired_blocks(graph)
    for end, begin in pairs.items():
        if by_id[begin]["params"].get("feedback", "no") == "yes":
            a, b = (begin, True, "value"), (end, False, "result")
            if a in parents and b in parents:
                union(a, b)
    concrete = {}
    for key, dtype in declared.items():
        if dtype != "any":
            concrete.setdefault(find(key), set()).add(dtype)
    effective, conflicts = {}, {}
    for key in parents:
        choices = concrete.get(find(key), set())
        # Existing generic number ports accept either concrete numeric kind.
        # Integer and float remain distinct: conversion between them is explicit.
        if choices <= {"number", "integer", "float"} and len(choices - {"number"}) <= 1:
            choices = choices - {"number"} or choices
        effective[key] = next(iter(choices)) if len(choices) == 1 else ""
        if len(choices) > 1:
            conflicts[key] = tuple(sorted(choices))
    return effective, conflicts
