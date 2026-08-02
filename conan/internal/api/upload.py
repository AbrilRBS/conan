import os

from conan.errors import ConanException
from conan.internal.rest.client_routes import ClientV2Router
from conan.internal.util.files import sha1sum


def _convert_files(package_list, convert):
    for ref, packages in package_list.items():
        ref_info = package_list.recipe_dict(ref)
        if ref_info.get("files"):
            ref_info["files"] = {f: convert(fp) for f, fp in ref_info["files"].items()}
        for pref in packages:
            pref_info = package_list.package_dict(pref)
            if pref_info.get("files"):
                pref_info["files"] = {f: convert(fp) for f, fp in pref_info["files"].items()}


def make_files_relative(package_list, cache_folder):
    """ Turn the absolute paths of the artifacts into paths relative to the cache folder, always
    with "/" separators, so that a serialized package list can be consumed in another machine or
    container, where the cache is not necessarily in the same location
    """
    cache_folder = os.path.abspath(cache_folder)

    def _relative(path):
        return os.path.relpath(path, cache_folder).replace("\\", "/")

    _convert_files(package_list, _relative)


def make_files_absolute(package_list, cache_folder):
    """ Resolve the cache relative paths of the artifacts against this cache folder.

    The paths are not allowed to escape the cache: they come from a file, and whoever runs the
    upload of those artifacts is typically the one holding the credentials for the server
    """
    cache_folder = os.path.abspath(cache_folder)

    def _absolute(path):
        full_path = os.path.abspath(os.path.join(cache_folder, path.replace("/", os.sep)))
        if os.path.commonpath([full_path, cache_folder]) != cache_folder:
            raise ConanException(f"Path '{path}' in the package list is outside of the Conan "
                                 f"cache '{cache_folder}', refusing to upload it")
        return full_path

    _convert_files(package_list, _absolute)


def add_urls(package_list, remote):
    router = ClientV2Router(remote.url.rstrip("/"))
    for ref, packages in package_list.items():
        ref_info = package_list.recipe_dict(ref)
        for f, fp in ref_info.get("files", {}).items():
            ref_info.setdefault("upload-urls", {})[f] = {
                'url': router.recipe_file(ref, f), 'checksum': sha1sum(fp)
            }
        for pref in packages:
            pref_info = package_list.package_dict(pref)
            for f, fp in pref_info.get("files", {}).items():
                pref_info.setdefault("upload-urls", {})[f] = {
                    'url': router.package_file(pref, f), 'checksum': sha1sum(fp)
                }
