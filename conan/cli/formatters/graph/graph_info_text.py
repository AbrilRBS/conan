import fnmatch

from conan.api.model import RecipeReference
from conan.api.output import ConanOutput, cli_out_write


def filter_nodes(nodes, package_filter=None, field_filter=None):
    """ The (id, serialized node) items of ``nodes``, the root one first, that match the package
    filter, with only the fields in the field filter. One by one, as they are iterated, so it
    works with the lazy nodes of ``DepsGraph.serialize(lazy_nodes=True)``
    """
    if field_filter is not None and "ref" not in field_filter:
        field_filter.append("ref")
    root = None
    for id_, node in nodes:
        if root is None:
            root = node
        if package_filter is not None and not any(_matching(node, p, root)
                                                  for p in package_filter):
            continue
        if field_filter is not None:
            node = {k: v for k, v in node.items() if k in field_filter}
        yield id_, node


def _matching(node, pattern, root):
    if fnmatch.fnmatch(node["ref"] or "", pattern):
        return True
    if pattern == "&":  # Handle the consumer pattern
        if node["recipe"] == "Consumer":
            return True
        # How to deal with --requires=xxx --package-filter=& "consumers"
        if root["recipe"] == "Cli" and node is not root:
            # We look if the current node is a direct dependency of the root node
            node_ref = RecipeReference.loads(node["ref"])
            for dep in root["dependencies"].values():
                if dep["direct"] and node_ref == RecipeReference.loads(dep["ref"]):
                    return True
    return False


def format_graph_info(result):
    """ More complete graph output, including information for every node in the graph
    Used for 'graph info' command
    """
    graph = result["graph"]
    field_filter = result["field_filter"]
    package_filter = result["package_filter"]

    ConanOutput().subtitle("Basic graph information")
    serial = graph.serialize(lazy_nodes=True)
    for _, n in filter_nodes(serial["nodes"], package_filter, field_filter):
        # A write per node, not per line: every cli_out_write() call checks isatty() (and resets
        # colorama in a terminal), and large graphs print hundreds of thousands of lines
        lines = [f"{n['ref']}:"]  # FIXME: This can be empty for consumers and it is ugly ":"
        _serial_pretty_printer(n, lines, indent="  ")
        cli_out_write("\n".join(lines))


def _serial_pretty_printer(data, lines, indent=""):
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"{indent}{k}:")
            # TODO: increment color too
            _serial_pretty_printer(v, lines, indent=indent+"  ")
        else:
            lines.append(f"{indent}{k}: {v}")
