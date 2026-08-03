from conan.api.conan_api import ConanAPI
from conan.api.model import ListPattern, MultiPackagesList
from conan.api.output import ConanOutput
from conan.cli import make_abs_path
from conan.cli.command import conan_command, OnceArgument
from conan.cli.commands.list import print_list_json, print_serial
from conan.cli.commands.upload import ask_confirm_upload
from conan.errors import ConanException
from conan.internal.api.upload import UPLOAD_PREPARED, find_unpreparable


def _single_package_list(multi_package_list, listfile):
    """ The one package list in the file, whatever it happens to be keyed by.

    The key is not fixed, only how many there can be. "conan list" keys its output by what it
    queried: "Local Cache" when no remote is given, the name of each remote that is, and both when
    the cache is asked for alongside a remote. "conan upload-prepare" keys its own by the remote it
    prepared for, and that output is valid input here, because preparing it again re-targets it.

    So anything with a single key is taken, and several is an error rather than a guess: picking
    one would silently leave the rest unprepared and unuploaded.

    The key says where the packages were found, and it is not checked against anything: preparing
    compresses artifacts out of the local cache, so that is where they have to be. A listing of a
    remote is not valid input, however it is keyed, and fails on the first reference that is not
    in the cache
    """
    names = list(multi_package_list.lists)
    if len(names) == 1:
        return multi_package_list[names[0]]
    if not names:
        raise ConanException(f"The package list '{listfile}' has no entries")
    listed = ", ".join(f"'{n}'" for n in names)
    raise ConanException(f"The package list '{listfile}' has entries for {listed}, and preparing "
                         f"is done for one set of packages at a time.\nPass a list holding a "
                         f"single one, so that nothing is silently left out")


def summary_prepare_list(results):
    """ Do a little format modification to serialized
    package list, so it looks prettier on text output
    """
    ConanOutput().subtitle("Prepared upload summary")
    info = results["results"]

    def format_prepare(item):
        if isinstance(item, dict):
            result = {}
            for k, v in item.items():
                if isinstance(v, dict):
                    v.pop("info", None)
                    v.pop("timestamp", None)
                    v.pop("files", None)
                    v.pop("upload-urls", None)
                    v.pop(UPLOAD_PREPARED, None)
                    upload_value = v.pop("upload", None)
                    if upload_value is not None:
                        msg = "To upload" if upload_value else "Already in server, will be skipped"
                        force_upload = v.pop("force_upload", None)
                        if force_upload:
                            msg = "To upload - forced"
                        k = f"{k} ({msg})"
                result[k] = format_prepare(v)
            return result
        return item
    info = {remote: format_prepare(values) for remote, values in info.items()}
    print_serial(info)


@conan_command(group="Creator", formatters={"text": summary_prepare_list,
                                            "json": print_list_json})
def upload_prepare(conan_api: ConanAPI, parser, *args):
    """
    (Experimental) Prepare an upload without transferring anything, for 'conan upload-artifacts'.

    This is the first half of 'conan upload': it checks which revisions the remote already has,
    applies the recipes 'upload_policy', and compresses the artifacts, leaving them ready in the
    cache. It always works from the local cache, which is where the artifacts to compress are.
    The resulting package list, saved with '--format=json', is the input of
    'conan upload-artifacts', which does the transfer. It holds absolute paths, so both
    steps need to see the same cache.

    Preparing reads the recipes, and reading a recipe imports it, which executes its code. Split
    this way, that only happens here, and never in the step that holds the upload credentials.
    This step only reads from the remotes, it never writes to them, so it should be given
    read-only credentials.
    """
    parser.add_argument('pattern', nargs="?",
                        help="A pattern in the form 'pkg/version#revision:package_id#revision', "
                             "e.g: \"zlib/1.2.13:*\" means all binaries for zlib/1.2.13. "
                             "If revision is not specified, it is assumed latest one.")
    parser.add_argument('-p', '--package-query', default=None, action=OnceArgument,
                        help="Only prepare packages matching a specific query. e.g: os=Windows AND "
                             "(arch=x86 OR compiler=gcc)")
    parser.add_argument("-r", "--remote", action=OnceArgument, required=True,
                        help='Prepare the upload for this specific remote, which is only read '
                             'from, so read-only credentials are enough. The result is only '
                             'valid for it, and it is the remote that must later be passed to '
                             '"conan upload-artifacts"')
    parser.add_argument("--only-recipe", action='store_true', default=False,
                        help='Prepare only the recipe/s, not the binary packages.')
    parser.add_argument("--force", action='store_true', default=False,
                        help='Prepare the artifacts even if the revision already exists in the '
                             'server')
    parser.add_argument("--check", action='store_true', default=False,
                        help='Perform an integrity check, using the manifests')
    parser.add_argument('-c', '--confirm', default=False, action='store_true',
                        help='Prepare all matching recipes without confirmation')
    parser.add_argument('--allow-disabled', default=False, action='store_true',
                        help='Allow preparing for a disabled remote')
    parser.add_argument("-l", "--list",
                        help="Package list file, of packages in the local cache. The output of a "
                             "previous 'conan upload-prepare' is accepted too, and preparing it "
                             "again re-targets it at the remote given here. A listing of a remote "
                             "('conan list -r=...') is not: there is nothing local to compress")
    parser.add_argument("-m", "--metadata", action='append',
                        help='Prepare the metadata, even if the package is already in the server '
                             'and not uploaded')

    args = parser.parse_args(*args)

    remote = conan_api.remotes.get(args.remote)
    enabled_remotes = conan_api.remotes.list()

    if args.pattern is None and args.list is None:
        raise ConanException("Missing pattern or package list file")
    if args.pattern and args.list:
        raise ConanException("Cannot define both the pattern and the package list file")
    if args.package_query and args.list:
        raise ConanException("Cannot define package-query and the package list file")
    if args.allow_disabled:
        remote.disabled = False

    if args.list:
        listfile = make_abs_path(args.list)
        multi_package_list = MultiPackagesList.load(listfile)
        package_list = _single_package_list(multi_package_list, args.list)
        if args.only_recipe:
            package_list.only_recipes()
    else:
        ref_pattern = ListPattern(args.pattern, package_id="*", only_recipe=args.only_recipe)
        package_list = conan_api.list.select(ref_pattern, package_query=args.package_query)

    unpreparable = find_unpreparable(package_list)
    if unpreparable:
        listed = ", ".join(unpreparable[:5])
        more = f" and {len(unpreparable) - 5} more" if len(unpreparable) > 5 else ""
        count = ("1 entry has" if len(unpreparable) == 1
                 else f"{len(unpreparable)} entries have")
        ConanOutput().warning(f"{count} no revision and will not be prepared nor uploaded: "
                              f"{listed}{more}. Ask for revisions in the query, like "
                              f"'conan list \"pkg/1.0#*:*#*\"'")

    if package_list:
        # If only if search with "*" we ask for confirmation
        if not args.list and not args.confirm and "*" in args.pattern:
            package_list = ask_confirm_upload(conan_api, package_list)

        conan_api.upload.prepare_full(package_list, remote, enabled_remotes, args.check,
                                      args.force, args.metadata)
    else:
        # Don't error on no recipes for automated workflows using list,
        # but warn to tell the user that nothing was prepared
        ConanOutput().warning("Nothing was prepared because the selection is empty.")

    pkglist = MultiPackagesList()
    pkglist.add(remote.name, package_list)
    return {
        "results": pkglist.serialize(),
        "conan_api": conan_api
    }
