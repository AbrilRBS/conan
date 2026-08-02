import os

from conan.errors import ConanException
from conan.internal.rest.client_routes import ClientV2Router
from conan.internal.util.files import sha1sum


# Mark that "conan upload-prepare" leaves on everything it prepares, so that
# "conan upload-artifacts" can tell a prepared list from any other package list. It goes in every
# revision, and not once for the whole document, because that is the granularity at which package
# lists are merged, and because the pkglist formatters treat every top level key as a remote name
# and every key below it as a reference
UPLOAD_PREPARED = "upload-prepared"
UPLOAD_PREPARED_FORMAT = 1


def mark_prepared(package_list):
    """ Mark every entry of the package list as prepared for upload """
    for ref, packages in package_list.items():
        package_list.recipe_dict(ref)[UPLOAD_PREPARED] = UPLOAD_PREPARED_FORMAT
        for pref in packages:
            package_list.package_dict(pref)[UPLOAD_PREPARED] = UPLOAD_PREPARED_FORMAT


def find_unprepared(package_list):
    """ The entries of the package list that were not left there by "conan upload-prepare".

    This walks the raw data instead of iterating the package list, because ``items()`` silently
    skips the references that have no recipe revision, and merging a plain package list into a
    prepared one is precisely how those appear.

    Package entries without a package revision are not reported: they are invisible to every
    upload path, prepared or not, "conan upload" ignores them just the same
    """
    unprepared = []
    for ref, ref_dict in package_list._data.items():  # noqa, no iteration can skip entries here
        revisions = ref_dict.get("revisions")
        if not revisions:
            unprepared.append(ref)
            continue
        for rrev, rrev_dict in revisions.items():
            if UPLOAD_PREPARED not in rrev_dict:
                unprepared.append(f"{ref}#{rrev}")
                continue
            for pkg_id, pkg_dict in rrev_dict.get("packages", {}).items():
                for prev, prev_dict in pkg_dict.get("revisions", {}).items():
                    if UPLOAD_PREPARED not in prev_dict:
                        unprepared.append(f"{ref}#{rrev}:{pkg_id}#{prev}")
    return unprepared


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
