from conan.api.conan_api import ConanAPI
from conan.api.model import MultiPackagesList, PackagesList
from conan.api.output import ConanOutput
from conan.cli import make_abs_path
from conan.cli.command import conan_command, OnceArgument
from conan.cli.commands.list import print_list_json
from conan.cli.commands.upload import summary_upload_list
from conan.errors import ConanException
from conan.internal.api.upload import find_prepared_for_other_remote, find_unprepared


def _select_prepared(multi_package_list, listfile, remote):
    """ The package list prepared for ``remote``, checking that the whole file was prepared for it.

    Every other top level key is an error and not something to quietly drop: merging a prepared
    package list with a plain one produces a document with two keys, and uploading only the
    prepared one would silently leave the rest out
    """
    package_list = multi_package_list.lists.get(remote.name)
    if package_list is None:
        prepared_for = ", ".join(f"'{n}'" for n in multi_package_list.lists) or "nothing"
        raise ConanException(f"The package list '{listfile}' was not prepared for the remote "
                             f"'{remote.name}', but for {prepared_for}.\nWhat has to be uploaded "
                             f"is decided against one specific remote, so it has to be prepared "
                             f"again with 'conan upload-prepare ... -r={remote.name}'")
    if not isinstance(package_list, PackagesList):  # a "conan list" error entry, not a package list
        raise ConanException(f"The package list '{listfile}' holds an error for the remote "
                             f"'{remote.name}' instead of packages: {package_list}")
    unexpected = [n for n in multi_package_list.lists if n != remote.name]
    if unexpected:
        names = ", ".join(f"'{n}'" for n in unexpected)
        raise ConanException(f"The package list '{listfile}' has entries for {names}, and not only "
                             f"for the remote '{remote.name}' being uploaded to.\nUploading it "
                             f"would silently leave those out. Prepare everything for "
                             f"'{remote.name}' before merging package lists")
    return package_list


def _check_prepared(package_list, listfile, remote):
    """ Refuse anything that "conan upload-prepare" did not leave for this remote """
    unprepared = find_unprepared(package_list)
    if unprepared:
        listed = "\n".join(f"\t{u}" for u in unprepared[:10])
        more = f"\n\t... and {len(unprepared) - 10} more" if len(unprepared) > 10 else ""
        raise ConanException(
            f"{len(unprepared)} entries of the package list '{listfile}' were not prepared for "
            f"upload:\n{listed}{more}\n"
            f"Uploading it would silently leave them out. Prepare them and upload the result:\n"
            f"\tconan upload-prepare -l {listfile} -r={remote.name} --format=json > prepared.json\n"
            f"\tconan upload-artifacts -l prepared.json -r={remote.name}")

    others = find_prepared_for_other_remote(package_list, remote)
    if others:
        raise ConanException(f"The package list '{listfile}' was prepared for "
                             f"{', '.join(others)}, but it is being uploaded to "
                             f"'{remote.name}' ({remote.url}).\nWhat has to be uploaded is decided "
                             f"against one specific server, so it has to be prepared again")


@conan_command(group="Creator", formatters={"text": summary_upload_list,
                                            "json": print_list_json})
def upload_artifacts(conan_api: ConanAPI, parser, *args):
    """
    (Experimental) Upload the artifacts prepared by 'conan upload-prepare'.

    This is the second half of 'conan upload': it transfers to the remote the artifacts that
    'conan upload-prepare' selected and compressed, reading only the package list it produced.
    That list holds absolute paths, so both steps need to see the same cache.

    No recipe is read, and therefore no recipe code is executed, so this is the only step that
    needs credentials to write to the server, and it does not expose them to recipe code.
    """
    parser.add_argument("-l", "--list", required=True,
                        help="Package list file produced by 'conan upload-prepare'")
    parser.add_argument("-r", "--remote", action=OnceArgument, required=True,
                        help='Upload to this specific remote. It must be the same remote the '
                             'package list was prepared for')
    parser.add_argument('--dry-run', default=False, action='store_true',
                        help='Do not execute the real upload (experimental)')
    parser.add_argument('--allow-disabled', default=False, action='store_true',
                        help='Allow uploading to disabled remote')

    args = parser.parse_args(*args)

    remote = conan_api.remotes.get(args.remote)
    if args.allow_disabled:
        remote.disabled = False

    listfile = make_abs_path(args.list)
    multi_package_list = MultiPackagesList.load(listfile)
    package_list = _select_prepared(multi_package_list, args.list, remote)

    if package_list:
        _check_prepared(package_list, args.list, remote)
        conan_api.upload.upload_artifacts(package_list, remote, args.dry_run)
    else:
        # Don't error on no recipes for automated workflows using list,
        # but warn to tell the user that no packages were uploaded
        ConanOutput().warning("No packages were uploaded because the selection is empty.")

    pkglist = MultiPackagesList()
    pkglist.add(remote.name, package_list)
    return {
        "results": pkglist.serialize(),
        "conan_api": conan_api
    }
