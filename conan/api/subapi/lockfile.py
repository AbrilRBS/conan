import os

from conan.api.output import ConanOutput
from conan.cli import make_abs_path
from conan.internal.cache.home_paths import HomePaths
from conan.internal.graph.graph import Overrides
from conan.errors import ConanException
from conan.internal.model.conanconfig import loadconanconfig
from conan.internal.model.lockfile import Lockfile, LOCKFILE


class LockfileAPI:
    """ Loads and saves Lockfiles from disk, modifies and manipulates them.

    At the moment Lockfile objects are "opaque" objects, they are not intended to be used
    independently, only to be retrieved from this API and passed as arguments to methods
    of the API.
    """

    def __init__(self, conan_api):
        self._conan_api = conan_api

    @staticmethod
    def get_lockfile(lockfile=None, conanfile_path=None, cwd=None, partial=False,
                     overrides=None) -> Lockfile:
        """ Obtain a lockfile, following this logic:

        If ``lockfile`` is explicitly defined, it would be either absolute or relative to ``cwd``
        and the lockfile file must exist. If ``lockfile=""`` (empty string) the default
        "conan.lock" lockfile will not be automatically used even if it is present.

        If ``lockfile`` is not defined, it will still look for a default conan.lock:

         - if ``conanfile_path`` is defined, it will be next to it
         - if ``conanfile_path`` is not defined, the default conan.lock should be in ``cwd``
         - if the default conan.lock cannot be found, it is not an error

        :param lockfile: The path to the lockfile file, absolute or relative to ``cwd``
        :param conanfile_path: The full path to the conanfile, if existing
        :param cwd: The current working dir, if ``None``, ``os.getcwd()`` will be used
        :param partial: If the obtained lockfile will allow partial resolving
        :param overrides: Dictionary of overrides ``{overridden: [new_ref1, new_ref2]}``.
            It is an error to define them if no lockfile is found.
        :return: The loaded lockfile, or ``None`` if no lockfile is used
        :raises ConanException: If an explicitly defined ``lockfile`` doesn't exist
        """
        if lockfile == "":
            # Allow a way with ``--lockfile=""`` to optout automatic usage of conan.lock
            return

        cwd = cwd or os.getcwd()
        if lockfile is None:  # Look for a default "conan.lock"
            # if path is defined, take it as reference
            base_path = os.path.dirname(conanfile_path) if conanfile_path else cwd
            lockfile_path = make_abs_path(LOCKFILE, base_path)
            if not os.path.isfile(lockfile_path):
                if overrides:
                    raise ConanException("Cannot define overrides without a lockfile")
                return
        else:  # explicit lockfile given
            lockfile_path = make_abs_path(lockfile, cwd)
            if not os.path.isfile(lockfile_path):
                raise ConanException("Lockfile doesn't exist: {}".format(lockfile_path))

        graph_lock = Lockfile.load(lockfile_path)
        graph_lock.partial = partial

        if overrides:
            graph_lock._overrides = Overrides.deserialize(overrides)
        ConanOutput().info("Using lockfile: '{}'".format(lockfile_path))
        return graph_lock

    def check_lockfile_config(self, lockfile: Lockfile):
        """Verify that installed configurations are aligned with lockfile config_requires.

        :param lockfile: The lockfile to check, can be ``None``, in which case nothing is checked
        :raises ConanException: If the installed configuration packages don't match the
            lockfile ``config_requires``
        """
        if lockfile is None:
            return

        config_version_path = HomePaths(self._conan_api.home_folder).config_version_path
        refs = loadconanconfig(config_version_path) if os.path.exists(config_version_path) else []
        lockfile.check_config_requires(refs)

    def update_lockfile_export(self, lockfile, conanfile, ref, is_build_require=False) -> Lockfile:
        """ Update the lockfile or create a new one with the information resulting from a
        conan export operation, so the recently exported version and revision can be locked and
        prioritized.

        :param lockfile: The lockfile to update. Can be ``None`` and a new lockfile will be created
        :param conanfile: The exported ``ConanFile`` object, as returned by
            :meth:`ExportAPI.export() <conan.api.subapi.export.ExportAPI.export>`
        :param ref: The :ref:`RecipeReference <conan.api.model.RecipeReference>` of the exported
            conanfile, including its recipe revision
        :param is_build_require: If ``True``, the exported conanfile is for a tool used as
            ``tool_requires``
        :return: The updated lockfile
        """
        # The package_type is not fully processed at export
        is_python_require = conanfile.package_type == "python-require"
        is_require = not is_python_require and not is_build_require
        if hasattr(conanfile, "python_requires"):
            python_requires = conanfile.python_requires.all_refs()
        else:
            python_requires = []
        python_requires = python_requires + ([ref] if is_python_require else [])
        new_lock = self.add_lockfile(lockfile,
                                     requires=[ref] if is_require else None,
                                     python_requires=python_requires,
                                     build_requires=[ref] if is_build_require else None)
        return new_lock

    @staticmethod
    def update_lockfile(lockfile, graph, lock_packages=False, clean=False) -> Lockfile:
        """ Update the lockfile with information from the dependency graph

        :param lockfile: The lockfile to update. It can be ``None``, and a new lockfile will be
            created.
        :param graph: The :ref:`dependency graph <reference_python_api_model_graph>`
        :param lock_packages: Unused, do not use or define it.
        :param clean: If ``True``, completely clean the lockfile, computing a new lockfile from
            the graph
        :return: The updated lockfile
        """
        if lockfile is None or clean:
            lockfile = Lockfile(graph, lock_packages)
            lockfile.partial = True
        else:
            lockfile.update_lock(graph, lock_packages)
        return lockfile

    @staticmethod
    def merge_lockfiles(lockfiles) -> Lockfile:
        """ Merge multiple lockfiles into a single lockfile.

        :param lockfiles: List of paths to the lockfile files to merge, absolute or relative to
            the current working directory
        :return: The merged lockfile
        """
        result = Lockfile()
        for lockfile in lockfiles:
            lockfile = make_abs_path(lockfile)
            graph_lock = Lockfile.load(lockfile)
            result.merge(graph_lock)
        return result

    @staticmethod
    def add_lockfile(lockfile=None, requires=None, build_requires=None, python_requires=None,
                     config_requires=None) -> Lockfile:
        """ Add requires to a lockfile. If ``lockfile`` is ``None``, a new one will be created

        :param lockfile: The lockfile to add to. It will be mutated in place. Can be ``None``.
        :param requires: The list of :ref:`RecipeReference <conan.api.model.RecipeReference>` of
            the requirements to add. Can be ``None``.
        :param build_requires: The list of :ref:`RecipeReference <conan.api.model.RecipeReference>`
            of the build requirements to add. Can be ``None``.
        :param python_requires: The list of
            :ref:`RecipeReference <conan.api.model.RecipeReference>` of the Python requirements
            to add. Can be ``None``.
        :param config_requires: The list of
            :ref:`RecipeReference <conan.api.model.RecipeReference>` of the configuration
            requirements to add. Can be ``None``.
        :return: The lockfile with the added information.
        """
        if lockfile is None:
            lockfile = Lockfile()  # create a new lockfile

        lockfile.add(requires=requires, build_requires=build_requires,
                     python_requires=python_requires, config_requires=config_requires)
        return lockfile

    @staticmethod
    def remove_lockfile(lockfile: Lockfile, requires=None, build_requires=None, python_requires=None,
                        config_requires=None) -> Lockfile:
        """ Remove entries from lockfile

        :param lockfile: The lockfile to remove entries from. It will be mutated in place.
        :param requires: The list of requires to remove, as reference strings or patterns,
            like ``"zlib/1.2.13"``, ``"zlib/*"`` or ``"zlib/[>=1.2 <2]"``
        :param build_requires: The list of build requires to remove, with the same format as
            ``requires``
        :param python_requires: The list of python_requires to remove, with the same format as
            ``requires``
        :param config_requires: The list of config_requires to remove, with the same format as
            ``requires``
        :return: The modified lockfile
        """
        lockfile.remove(requires=requires, build_requires=build_requires,
                        python_requires=python_requires, config_requires=config_requires)
        return lockfile

    @staticmethod
    def save_lockfile(lockfile: Lockfile, lockfile_out, path=None):
        """ Save lockfile to disk

        :param lockfile: The lockfile object to save
        :param lockfile_out: The output lockfile filename, absolute or relative to ``path``.
            If ``None``, nothing will be saved
        :param path: The folder to resolve a relative ``lockfile_out`` against. If ``None``,
            the current working directory will be used
        """
        if lockfile_out is not None:
            lockfile_out = make_abs_path(lockfile_out, path)
            lockfile.save(lockfile_out)
            ConanOutput().info(f"Generated lockfile: {lockfile_out}")
