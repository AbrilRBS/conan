from conan.internal.rest.client_routes import ClientV2Router
from conan.internal.util.files import sha1sum


# Mark that "conan upload-prepare" leaves on everything it prepares, so that
# "conan upload-artifacts" can tell a prepared list from any other package list, and can tell that
# it is being uploaded to the remote it was prepared against. It goes in every revision, and not
# once for the whole document, because that is the granularity at which package lists are merged,
# and because the pkglist formatters treat every top level key as a remote name and every key
# below it as a reference.
# This is a usability guard, not a security control: it lives in the file, so it can be forged
UPLOAD_PREPARED = "upload-prepared"
UPLOAD_PREPARED_FORMAT = 1


def mark_prepared(package_list, remote):
    """ Mark every entry of the package list as prepared for upload to ``remote`` """
    mark = {"format": UPLOAD_PREPARED_FORMAT, "remote": remote.name, "url": remote.url}
    for ref, ref_dict in package_list._data.items():  # noqa, items() can skip entries, see below
        for rrev_dict in ref_dict.get("revisions", {}).values():
            rrev_dict[UPLOAD_PREPARED] = mark
            for pkg_dict in rrev_dict.get("packages", {}).values():
                for prev_dict in pkg_dict.get("revisions", {}).values():
                    prev_dict[UPLOAD_PREPARED] = mark


def find_unprepared(package_list):
    """ The entries of the package list that were not left there by "conan upload-prepare".

    This walks the raw data instead of iterating the package list, because ``items()`` silently
    skips the references that have no recipe revision, and merging a plain package list into a
    prepared one is precisely how those appear. ``mark_prepared()`` walks it the same way, so the
    two always agree on which entries exist.

    Entries without a revision are not reported: they are invisible to every upload path,
    prepared or not, "conan upload" ignores them just the same. "conan upload-prepare" warns
    about the ones it was given, see ``find_unpreparable()``
    """
    unprepared = []
    for ref, ref_dict in package_list._data.items():  # noqa, no iteration can skip entries here
        for rrev, rrev_dict in ref_dict.get("revisions", {}).items():
            if UPLOAD_PREPARED not in rrev_dict:
                unprepared.append(f"{ref}#{rrev}")
                continue
            for pkg_id, pkg_dict in rrev_dict.get("packages", {}).items():
                for prev, prev_dict in pkg_dict.get("revisions", {}).items():
                    if UPLOAD_PREPARED not in prev_dict:
                        unprepared.append(f"{ref}#{rrev}:{pkg_id}#{prev}")
    return unprepared


def find_unpreparable(package_list):
    """ The entries of an input package list that cannot be prepared, nor uploaded, because they
    carry no revision. A "conan list" query only reports revisions when it is asked for them,
    like ``conan list "pkg/1.0#*:*#*"``, so a coarser query produces these
    """
    unpreparable = []
    for ref, ref_dict in package_list._data.items():  # noqa
        revisions = ref_dict.get("revisions")
        if not revisions:
            unpreparable.append(ref)
            continue
        for rrev, rrev_dict in revisions.items():
            for pkg_id, pkg_dict in rrev_dict.get("packages", {}).items():
                if not pkg_dict.get("revisions"):
                    unpreparable.append(f"{ref}#{rrev}:{pkg_id}")
    return unpreparable


def find_prepared_for_other_remote(package_list, remote):
    """ The remotes, other than ``remote``, that entries of this package list were prepared for.

    What has to be uploaded is decided against one specific server, so a list prepared elsewhere
    would upload the wrong things. Names are not enough: the same name can be repointed at a
    different server between preparing and uploading
    """
    others = set()
    for ref_dict in package_list._data.values():  # noqa
        for rrev_dict in ref_dict.get("revisions", {}).values():
            mark = rrev_dict.get(UPLOAD_PREPARED)
            if isinstance(mark, dict) and \
                    (mark.get("remote"), mark.get("url")) != (remote.name, remote.url):
                others.add(f"'{mark.get('remote')}' ({mark.get('url')})")
    return sorted(others)


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
