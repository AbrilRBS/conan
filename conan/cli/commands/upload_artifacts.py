from conan.api.conan_api import ConanAPI
from conan.api.model import MultiPackagesList
from conan.api.output import ConanOutput
from conan.cli import make_abs_path
from conan.cli.command import conan_command, OnceArgument
from conan.cli.commands.list import print_list_json
from conan.cli.commands.upload import summary_upload_list
from conan.errors import ConanException
from conan.internal.api.upload import find_unprepared


def _looks_uploaded(package_list):
    """ Whether the list carries upload decisions without being prepared, which is what the
    output of "conan upload" looks like. Only used to tell the user which mistake they made
    """
    return any("upload" in rrev_dict
               for ref_dict in package_list._data.values()  # noqa
               for rrev_dict in ref_dict.get("revisions", {}).values())


def _unprepared_error(listfile, remote_name, package_list, unprepared):
    total = len(package_list._data)  # noqa
    if len(unprepared) >= total:  # nothing in it was prepared at all
        if _looks_uploaded(package_list):
            what = (f"The package list '{listfile}' looks like the output of 'conan upload', "
                    f"not of 'conan upload-prepare'.")
        else:
            what = (f"The package list '{listfile}' is a plain package list, it has not been "
                    f"prepared for upload.")
        return (f"{what}\nPrepare it first, and upload the result:\n"
                f"\tconan upload-prepare -l {listfile} -r={remote_name} --format=json "
                f"> prepared.json\n"
                f"\tconan upload-artifacts -l prepared.json -r={remote_name}")
    listed = "\n".join(f"\t{u}" for u in unprepared[:10])
    more = f"\n\t... and {len(unprepared) - 10} more" if len(unprepared) > 10 else ""
    return (f"The package list '{listfile}' is only partly prepared for upload, these entries "
            f"were not:\n{listed}{more}\n"
            f"Uploading it would silently leave them out. This is what merging a prepared "
            f"package list with one that is not looks like, prepare them all before merging")


@conan_command(group="Creator", formatters={"text": summary_upload_list,
                                            "json": print_list_json})
def upload_artifacts(conan_api: ConanAPI, parser, *args):
    """
    (Experimental) Upload the artifacts prepared by 'conan upload-prepare'.

    This is the second half of 'conan upload': it transfers to the remote the artifacts that
    'conan upload-prepare' selected and compressed, reading only the package list it produced.
    The paths in that list are relative to the cache folder and are resolved against the local
    one, so the two steps do not need the cache in the same location.

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
    if remote.name not in multi_package_list.lists:
        prepared_for = ", ".join(f"'{n}'" for n in multi_package_list.lists) or "nothing"
        raise ConanException(f"The package list '{args.list}' was not prepared for the remote "
                             f"'{remote.name}', but for {prepared_for}.\nWhat has to be uploaded "
                             f"is decided against one specific remote, so it has to be prepared "
                             f"again with 'conan upload-prepare ... -r={remote.name}'")
    package_list = multi_package_list[remote.name]

    if package_list:
        unprepared = find_unprepared(package_list)
        if unprepared:
            raise ConanException(_unprepared_error(args.list, remote.name, package_list,
                                                   unprepared))
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
