import os
import time
from contextlib import contextmanager
from unittest import mock

from conan.internal.cache.db.recipes_table import RecipesDBTable
from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.tools import TestClient


@contextmanager
def _count_recipes_db_queries():
    """ Every query to the recipes table opens a new sqlite connection, count them """
    queries = []
    db_connection = RecipesDBTable.db_connection

    @contextmanager
    def counting_connection(self):
        queries.append(1)
        with db_connection(self) as connection:
            yield connection

    with mock.patch.object(RecipesDBTable, "db_connection", counting_connection):
        yield queries


def _recipes_queries(libs):
    """ a chain of "libs" libraries, requiring each one a version range of the previous one. The
    recipes table queries to install the last one """
    client = TestClient(light=True)
    client.save({"conanfile.py": GenConanfile("lib0", "1.0")})
    client.run("create .")
    for i in range(1, libs):
        conanfile = GenConanfile(f"lib{i}", "1.0").with_requires(f"lib{i-1}/[>=1.0]")
        client.save({"conanfile.py": conanfile})
        client.run("create .")
    # The cache snapshot is not reused right after a modification, when timestamps are not precise
    db = os.path.join(client.cache_folder, "p", "cache.sqlite3")
    old = time.time_ns() - 10 * 10 ** 9
    os.utime(db, ns=(old, old))
    with _count_recipes_db_queries() as queries:
        client.run(f"install --requires=lib{libs - 1}/[>=1.0]")
    assert "lib0/1.0: Already installed!" in client.out
    return len(queries)


def test_recipes_queries_do_not_grow_with_version_ranges():
    """ The recipes table is read once to resolve all the version ranges and the latest revisions
    of the graph, not once for every version range, or for every recipe """
    assert _recipes_queries(3) == _recipes_queries(10) == 1
