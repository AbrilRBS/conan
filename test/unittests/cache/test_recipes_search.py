import os
import re
import sqlite3
import time
from fnmatch import translate
from unittest import mock

import pytest

from conan.api.model import RecipeReference
from conan.internal.cache.cache import PkgCache
from conan.internal.errors import ConanReferenceDoesNotExistInDB
from conan.internal.model.conf import ConfDefinition
from conan.test.utils.test_files import temp_folder


def _create(cache, text, timestamp):
    ref = RecipeReference.loads(text)
    ref.timestamp = timestamp
    cache.create_ref_layout(ref)


def _search(cache, pattern):
    return [str(ref) for ref in cache.search_recipes(pattern)]


def _age_db(cache_folder, seconds=10):
    """ The recipes snapshot is only reused if the DB file wasn't modified very recently """
    db = os.path.join(cache_folder, "p", "cache.sqlite3")
    old = time.time_ns() - seconds * 10 ** 9
    os.utime(db, ns=(old, old))
    return db


@pytest.fixture()
def cache_folder():
    return temp_folder()


@pytest.fixture()
def cache(cache_folder):
    cache = PkgCache(cache_folder, ConfDefinition())
    refs = ["zlib/1.0#r1", "zlib/1.1#r1", "zlib/1.1#r2", "zlib/2.0@user/stable#r1",
            "zlibng/1.0#r1", "openssl/3.0#r1"]
    for i, text in enumerate(refs):
        _create(cache, text, 1000.0 + i)
    return cache


def _connections():
    """ count the sqlite connections opened (one for every DB query) """
    connect = sqlite3.connect
    return mock.patch("sqlite3.connect", side_effect=connect)


@pytest.mark.parametrize("pattern", ["zlib/*", "zlib", "zlib/1.0", "zlib/*@user/*", "zlib/*@",
                                     "zlib/2.0@user/stable", "zlibng/*", "nothing/*", "zlib*/*",
                                     "*/1.0", "z[l]ib/*", "zlib?/*", "*"])
def test_search_by_name_is_the_same_as_searching_all(cache, cache_folder, pattern):
    """ When the name of the references is known, the references of other names are not checked,
    but the result is the same as always checking all of them """
    _age_db(cache_folder)
    expected = [str(ref) for ref in cache._db.list_references(re.compile(translate(pattern)))]
    assert _search(cache, pattern) == expected
    assert _search(cache, pattern) == expected  # reusing the snapshot


def test_search_by_name(cache):
    assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/2.0@user/stable"]
    assert _search(cache, "zlibng/*") == ["zlibng/1.0"]


def test_returned_references_can_be_modified(cache, cache_folder):
    _age_db(cache_folder)
    for ref in cache.search_recipes("zlib/*"):
        ref.revision = "modified"
        ref.version = None
    assert all(ref.revision is None and ref.version is not None
               for ref in cache.search_recipes("zlib/*"))


def test_snapshot_is_reused(cache, cache_folder):
    """ If the DB was not modified, the searches and latest recipes don't need new queries """
    _age_db(cache_folder)
    assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/2.0@user/stable"]
    with _connections() as connections:
        for _ in range(3):
            assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/2.0@user/stable"]
            assert _search(cache, "openssl/*") == ["openssl/3.0"]
            assert _search(cache, "nothing/*") == []
        layout = cache.recipe_layout_latest(RecipeReference.loads("zlib/1.1"))
        assert layout.reference.repr_notime() == "zlib/1.1#r2"
        with pytest.raises(ConanReferenceDoesNotExistInDB):
            cache.recipe_layout_latest(RecipeReference.loads("nothing/1.0"))
    assert connections.call_count == 0


def test_generic_search_does_not_read_more(cache):
    """ A search that can match any name has nothing to reuse: it is one query, as always """
    for pattern in (None, "*", "zlib*/*"):
        with _connections() as connections:
            assert _search(cache, pattern)
        assert connections.call_count == 1


def test_latest_recipe_is_not_read_without_a_search(cache, cache_folder):
    _age_db(cache_folder)
    with _connections() as connections:
        cache.recipe_layout_latest(RecipeReference.loads("zlib/1.1"))
        cache.recipe_layout_latest(RecipeReference.loads("openssl/3.0"))
    assert connections.call_count == 2  # one for each recipe, the whole table is not read


def test_modifications_of_this_cache(cache, cache_folder):
    _age_db(cache_folder)
    assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/2.0@user/stable"]
    db = os.path.join(cache_folder, "p", "cache.sqlite3")
    initial = os.stat(db).st_mtime_ns

    _create(cache, "zlib/1.2#r1", 2000.0)
    os.utime(db, ns=(initial, initial))  # the file looks as before, only the cache knows
    assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/1.2", "zlib/2.0@user/stable"]

    _age_db(cache_folder)
    assert _search(cache, "zlib/*")
    layout = cache.recipe_layout(RecipeReference.loads("zlib/1.0#r1"))
    initial = os.stat(db).st_mtime_ns
    cache.remove_recipe_layout(layout)
    os.utime(db, ns=(initial, initial))
    assert _search(cache, "zlib/*") == ["zlib/1.1", "zlib/1.2", "zlib/2.0@user/stable"]

    _age_db(cache_folder)
    assert _search(cache, "zlib/*")
    ref = RecipeReference.loads("zlib/1.1#r1")
    ref.timestamp = 3000.0  # a newer timestamp makes it the latest
    cache.update_recipe_timestamp(ref)
    assert cache.recipe_layout_latest(RecipeReference.loads("zlib/1.1")).reference.revision == "r1"


def test_modifications_by_others(cache, cache_folder):
    """ Other Conan processes (other instance with the same DB) can modify the cache while a
    snapshot exists """
    other = PkgCache(cache_folder, ConfDefinition())
    _age_db(cache_folder)
    assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/2.0@user/stable"]

    _create(other, "zlib/1.2#r1", 2000.0)
    assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/1.2", "zlib/2.0@user/stable"]

    _age_db(cache_folder)
    assert cache.recipe_layout_latest(RecipeReference.loads("zlib/1.2")).reference.revision == "r1"
    _create(other, "zlib/1.2#r2", 2001.0)
    assert cache.recipe_layout_latest(RecipeReference.loads("zlib/1.2")).reference.revision == "r2"


def test_snapshot_just_after_a_modification_is_not_reused(cache, cache_folder):
    """ The timestamp of a file system is not precise, a modification right after the previous one
    could not be detected, so what was read just after a modification is not trusted """
    other = PkgCache(cache_folder, ConfDefinition())
    db = os.path.join(cache_folder, "p", "cache.sqlite3")
    assert _search(cache, "zlib/*") == ["zlib/1.0", "zlib/1.1", "zlib/2.0@user/stable"]
    read_stat = os.stat(db)  # the state of the file that the snapshot was read with
    _create(other, "zlib/1.2#r1", 2000.0)
    # make it look as if the file was not modified: same timestamp as when it was read
    os.utime(db, ns=(read_stat.st_mtime_ns, read_stat.st_mtime_ns))
    assert "zlib/1.2" in _search(cache, "zlib/*")
