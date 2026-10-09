from typing import Optional, List

from conan.api.model import Remote
from conan.api.model import PkgReference
from conan.api.model import RecipeReference


class RemoveAPI:
    """This API is used to remove artifacts from either remotes or the Conan cache.
    It can either remove specific package references, or whole recipe references with all
    its associated packages"""

    def __init__(self, conan_api, api_helpers):
        self._conan_api = conan_api
        self._api_helpers = api_helpers

    def recipe(self, ref: RecipeReference, remote: Optional[Remote] = None):
        """ Removes the specified recipe reference alongside all its associated packages.

        If ``remote`` is specified, the recipe will be removed from the remote,
        otherwise they will be removed from the local cache.

        :param ref: The :ref:`RecipeReference <conan.api.model.RecipeReference>` to remove,
            including its recipe revision
        :param remote: Optional :ref:`Remote <conan.api.model.Remote>` to remove the recipe from
        """
        self.recipes([ref], remote)

    def recipes(self, refs: List[RecipeReference], remote: Optional[Remote] = None):
        """Removes the specified recipe references alongside all their associated packages.

        If ``remote`` is specified, the recipes will be removed from the remote,
        otherwise they will be removed from the local cache.

        .. warning::

            This method is not atomic with respect to each of the given references

        :param refs: List of :ref:`RecipeReference <conan.api.model.RecipeReference>` to
            remove, all of them including their recipe revision
        :param remote: Optional :ref:`Remote <conan.api.model.Remote>` to remove the recipes from
        """
        assert all(bool(ref.revision) for ref in refs), "Recipe revision cannot be None to remove a recipe"
        if remote:
            for ref in refs:
                self._api_helpers.remote_manager.remove_recipe(ref, remote)
        else:
            for ref in refs:
                recipe_layout = self._api_helpers.cache.recipe_layout(ref)
                self._api_helpers.cache.remove_recipe_layout(recipe_layout)

    def package(self, pref: PkgReference, remote: Optional[Remote] = None):
        """Removes the specified package reference.

        If ``remote`` is specified, the package will be removed from the remote,
        otherwise it will be removed from the local cache.

        :param pref: The ``PkgReference`` to remove, including its recipe and package revisions
        :param remote: Optional :ref:`Remote <conan.api.model.Remote>` to remove the package from
        """
        self.packages([pref], remote)

    def packages(self, prefs: List[PkgReference], remote: Optional[Remote] = None):
        """Removes all the specified package references.

        If ``remote`` is specified, the packages will be removed from the remote,
        otherwise they will be removed from the local cache.

        .. warning::

            This method is not atomic when performed in the local cache
            with respect to each of the given references,
            nor are remotes guaranteed to implement this call atomically either.

        :param prefs: List of ``PkgReference`` to remove, all of them including their recipe
            and package revisions
        :param remote: Optional :ref:`Remote <conan.api.model.Remote>` to remove the packages from
        """
        assert all(bool(pref.ref.revision) for pref in prefs), "Recipe revision cannot be None to remove a package"
        assert all(bool(pref.revision) for pref in prefs), "Package revision cannot be None to remove a package"
        if remote:
            self._api_helpers.remote_manager.remove_packages(prefs, remote)
        else:
            # TODO: Move this iteration to ``cache``, to align interface with RemoteManager
            for pref in prefs:
                package_layout = self._api_helpers.cache.pkg_layout(pref)
                self._api_helpers.cache.remove_package_layout(package_layout)
