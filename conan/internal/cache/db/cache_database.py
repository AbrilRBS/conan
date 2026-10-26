import os
import sqlite3
import time

from conan.api.output import ConanOutput
from conan.internal.cache.db.packages_table import PackagesDBTable
from conan.internal.cache.db.recipes_table import RecipesDBTable
from conan.api.model import PkgReference
from conan.api.model import RecipeReference
from conan.internal.errors import ConanReferenceDoesNotExistInDB
from conan.internal.model.version import Version


# The timestamps of the file systems are not precise (milliseconds in Linux, tens of ms in Windows)
_FILE_TIMESTAMP_MARGIN_NS = 100_000_000


class _RecipesSnapshot:
    """ The latest revision of every recipe in the cache, read with a single query. Resolving the
    version ranges of a graph needs to search all the versions of every dependency name, and the
    graph nodes the latest revision of their recipes, that would otherwise be one query to the
    whole table (or to a connection) each time """

    def __init__(self, stamp, reliable, rows):
        self.stamp = stamp  # state of the DB file when it was read, to know if it is still valid
        self.reliable = reliable  # if the ``stamp`` will certainly change with the next change
        self.latest = {row[0]: row for row in rows}  # {"name/version@user/channel": row}
        self._by_name = None

    def references(self, name):
        """ new RecipeReference (the callers modify them) for all the references of that name """
        if self._by_name is None:
            self._by_name = {}
            for reference, row in self.latest.items():
                self._by_name.setdefault(reference.split("/", 1)[0], []).append(row)
        return [RecipeReference.loads(row[0]) for row in self._by_name.get(name, ())]

    def all_references(self):
        return [RecipeReference.loads(reference) for reference in self.latest]


class CacheDatabase:

    def __init__(self, filename):
        version = sqlite3.sqlite_version
        if Version(version) < "3.7.11":
            ConanOutput().error(f"Your sqlite3 '{version} < 3.7.11' version is not supported")
        self._filename = filename
        self._snapshot = None  # see _recipes_snapshot()
        self._recipes = RecipesDBTable(filename)
        self._packages = PackagesDBTable(filename)
        if not os.path.isfile(filename):
            self._recipes.create_table()
            self._packages.create_table()

    def exists_prev(self, ref):
        return self._packages.get_package_revisions_reference_exists(ref)

    def get_latest_package_reference(self, pref):
        prevs = list(self._packages.get_package_revisions_references(pref, only_latest_prev=True))
        if not prevs:
            raise ConanReferenceDoesNotExistInDB(f"Package '{pref.repr_notime()}' not found")
        return prevs[0]["pref"]

    def get_latest_package_reference_data(self, pref):
        # Used just for PkgCache.pkg_layout_latest()
        # TODO: This can be refactored, unified with get_latest_package_reference()
        prevs = list(self._packages.get_package_revisions_references(pref, only_latest_prev=True))
        return prevs[0] if prevs else None

    def _recipes_snapshot(self, load):
        """ The snapshot of the recipes (reading it if ``load``), only if it is up to date: it is
        discarded when this instance modifies the recipes, and when the DB file is modified by
        anything else (like other Conan process), what is checked with the state of the file. That
        state is taken before reading the DB, so any later modification will always be detected,
        unless it happened too close to the previous modification to get a different timestamp,
        and then the snapshot is not reused """
        now = time.time_ns()
        try:
            stat = os.stat(self._filename)
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        snapshot = self._snapshot
        if snapshot is not None and snapshot.reliable and snapshot.stamp == stamp:
            return snapshot
        if not load:
            return None
        reliable = now - stat.st_mtime_ns > _FILE_TIMESTAMP_MARGIN_NS
        self._snapshot = _RecipesSnapshot(stamp, reliable, self._recipes.all_latest())
        return self._snapshot

    def update_recipe_timestamp(self, ref):
        self._snapshot = None
        self._recipes.update_timestamp(ref)

    def update_package_timestamp(self, pref: PkgReference, path: str, build_id: str):
        self._packages.update_timestamp(pref, path=path, build_id=build_id)

    def remove_recipe(self, ref: RecipeReference):
        # Removing the recipe must remove all the package binaries too from DB
        self._snapshot = None
        self._recipes.remove(ref)
        self._packages.remove_recipe(ref)

    def remove_package(self, ref: PkgReference):
        # Removing the recipe must remove all the package binaries too from DB
        self._packages.remove(ref)

    def remove_build_id(self, pref):
        self._packages.remove_build_id(pref)

    def get_matching_build_id(self, ref, build_id):
        result = self._packages.get_package_references_with_build_id_match(ref, build_id)
        if result:
            return result["pref"]
        return None

    def get_recipe(self, ref: RecipeReference):
        """ Returns the reference data as a dictionary (or fails) """
        return self._recipes.get_recipe(ref)

    def get_latest_recipe(self, ref: RecipeReference):
        """ Returns the reference data as a dictionary (or fails) """
        # If the snapshot was read (to search versions), use it. It is not read just for this
        snapshot = self._recipes_snapshot(load=False)
        if snapshot is None:
            return self._recipes.get_latest_recipe(ref)
        row = snapshot.latest.get(str(ref))
        if row is None:
            raise ConanReferenceDoesNotExistInDB(f"Recipe '{ref}' not found")
        return self._recipes.row_as_dict(row)

    def get_recipe_revisions_references(self, ref: RecipeReference):
        return self._recipes.get_recipe_revisions_references(ref)

    def try_get_package(self, ref: PkgReference):
        """ Returns the reference data as a dictionary (or fails) """
        ref_data = self._packages.get(ref)
        return ref_data

    def create_recipe(self, path, ref: RecipeReference):
        self._snapshot = None
        self._recipes.create(path, ref)

    def create_package(self, path, ref: PkgReference, build_id):
        self._packages.create(path, ref, build_id=build_id)

    def list_references(self, pattern=None, name=None):
        """Returns a list of all RecipeReference in the cache, optionally filtering by pattern.
         The references have their revision and timestamp attributes unset.
         If it is known that the pattern can only match references with a given ``name``, that is
         searched in a snapshot of the recipes, that is reused by the next searches"""
        snapshot = self._recipes_snapshot(load=name is not None)
        if snapshot is None:  # Any name, and nothing to reuse: read only what is necessary
            refs = self._recipes.all_references()
        elif name is None:
            refs = snapshot.all_references()
        else:
            refs = snapshot.references(name)
        return [ref for ref in refs if pattern is None or ref.partial_match(pattern)]

    def get_package_revisions_references(self, pref: PkgReference):
        return [d["pref"]
                for d in self._packages.get_package_revisions_references(pref,
                                                                         only_latest_prev=False)]

    def get_package_references(self, ref: RecipeReference, only_latest_prev=True):
        return [d["pref"]
                for d in self._packages.get_package_references(ref, only_latest_prev)]

    def path_to_ref(self, path):
        ref = self._recipes.path_to_ref(path)
        if ref is not None:
            return ref
        return self._packages.path_to_ref(path)
