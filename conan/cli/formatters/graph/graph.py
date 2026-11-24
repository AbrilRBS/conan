import json
import os

from jinja2 import Template, select_autoescape

from conan.api.output import cli_out_write, cli_out_write_chunks
from conan.cli.formatters.graph.graph_info_text import filter_nodes
from conan.cli.formatters.graph.info_graph_dot import graph_info_dot
from conan.cli.formatters.graph.info_graph_html import graph_info_html


def _render_graph(graph, template, template_folder):
    deps_graph = graph.serialize()
    from conan import __version__
    template = Template(template, autoescape=select_autoescape(['html', 'xml']))
    return template.render(deps_graph=deps_graph,
                           base_template_path=template_folder, version=__version__)


def format_graph_html(result):
    graph = result["graph"]
    conan_api = result["conan_api"]

    template_folder = os.path.join(conan_api.cache_folder, "templates")
    user_template = os.path.join(template_folder, "graph.html")
    template = graph_info_html
    if os.path.isfile(user_template):
        with open(user_template, 'r', encoding="utf-8", newline="") as handle:
            template = handle.read()
    cli_out_write(_render_graph(graph, template, template_folder))


def format_graph_dot(result):
    graph = result["graph"]
    conan_api = result["conan_api"]

    template_folder = os.path.join(conan_api.cache_folder, "templates")
    user_template = os.path.join(template_folder, "graph.dot")
    template = graph_info_dot
    if os.path.isfile(user_template):
        with open(user_template, 'r', encoding="utf-8", newline="") as handle:
            template = handle.read()
    cli_out_write(_render_graph(graph, template, template_folder))


def format_graph_json(result):
    graph = result["graph"]
    field_filter = result.get("field_filter")
    package_filter = result.get("package_filter")
    serial = graph.serialize(lazy_nodes=True)
    serial["nodes"] = filter_nodes(serial["nodes"], package_filter, field_filter)
    cli_out_write_chunks(_graph_json_chunks(serial))


def _graph_json_chunks(serial):
    """ The text of ``json.dumps({"graph": serial}, indent=4)``, a node at a time: for a large graph,
    the serialized nodes and the json text take more memory than the graph itself
    """
    def _dumps(value, level):  # As if nested in the document <level> levels deep
        return json.dumps(value, indent=4).replace("\n", "\n" + "    " * level)

    yield '{\n    "graph": {\n        "nodes": {'
    any_node = False
    for id_, node in serial["nodes"]:
        yield ",\n" if any_node else "\n"
        yield f"            {json.dumps(id_)}: {_dumps(node, 3)}"
        any_node = True
    yield "\n        }" if any_node else "}"
    for key, value in serial.items():
        if key != "nodes":
            yield f",\n        {json.dumps(key)}: {_dumps(value, 2)}"
    yield "\n    }\n}"
