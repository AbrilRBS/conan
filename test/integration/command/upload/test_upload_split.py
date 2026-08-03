import json
import os
import platform
import textwrap

import pytest

from conan.api.model import RecipeReference
from conan.internal.util.files import load, save
from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.tools import TestClient, TestServer


def _client():
    """ A client already authenticated, so that the several commands each test runs don't have
    to consume the interactive credentials """
    c = TestClient(default_server_user=True, light=True)
    c.run("remote login default admin -p password")
    return c


def _two_servers_client():
    servers = {f"server{i}": TestServer([("*/*@*/*", "*")], [("*/*@*/*", "*")],
                                        users={"user": "password"}) for i in range(2)}
    c = TestClient(servers=servers, inputs=8 * ["user", "password"], light=True)
    for name in servers:
        c.run(f"remote login {name} user -p password")
    return c


def _server_has_sources(server):
    store = server.test_server.server_store.store
    return any("conan_sources.tgz" in files for _, _, files in os.walk(store))


def _break_cache_recipes(client):
    """ Make every recipe in the cache raise as soon as its module is imported. Everything that
    keeps working afterwards proves that no recipe was loaded.

    Only the exported "e/" copies are touched: the "d/" ones are what the prepared list points at,
    and poisoning those would upload a corrupt recipe and make the test pass on its log lines
    while publishing garbage
    """
    broken = 0
    for root, _, files in os.walk(os.path.join(client.cache_folder, "p")):
        if os.path.basename(root) != "e":
            continue
        for f in files:
            if f == "conanfile.py":
                path = os.path.join(root, f)
                save(path, 'raise Exception("Recipe module executed!")\n' + load(path))
                broken += 1
    assert broken, "No exported recipes found in the cache to break"


def _revisions(pkglist, remote="default"):
    """ Every recipe revision and package revision dict of a serialized package list """
    for ref_dict in pkglist[remote].values():
        for rrev_dict in ref_dict.get("revisions", {}).values():
            yield rrev_dict
            for pkg_dict in rrev_dict.get("packages", {}).values():
                yield from pkg_dict.get("revisions", {}).values()


def test_prepare_and_upload_artifacts():
    """ The two halves of an upload, run as separate commands, land the same artifacts in the
    server as 'conan upload' does """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0").with_package_file("f.txt", "content")})
    c.run("create .")

    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")
    assert "Preparing upload to remote default" in c.out
    assert "Uploading recipe" not in c.out  # nothing is transferred yet
    c.run("list pkg/1.0:* -r=default")
    assert "ERROR: Recipe not found" in c.out  # nothing reached the server yet

    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c.out
    assert "pkg/1.0: Uploading package" in c.out

    c.run("list pkg/1.0:* -r=default")
    assert "da39a3ee5e6b4b0d3255bfef95601890afd80709" in c.out

    # And the artifacts in the server are complete, a different client can consume them
    c2 = TestClient(servers=c.servers, inputs=["admin", "password"], light=True)
    c2.run("install --requires=pkg/1.0")
    assert "pkg/1.0: Downloaded recipe revision" in c2.out
    assert "pkg/1.0: Downloaded package revision" in c2.out
    c2.run("cache path pkg/1.0:da39a3ee5e6b4b0d3255bfef95601890afd80709")
    assert load(os.path.join(c2.stdout.strip(), "f.txt")) == "content"


def test_upload_artifacts_does_not_load_the_recipes():
    """ The point of the split: the step that holds the credentials for the server does not read
    any recipe, so it cannot execute recipe code """
    c = _client()
    pyreq = textwrap.dedent("""
        from conan import ConanFile
        class MyBase:
            pass
        class PyReq(ConanFile):
            name = "pyreq"
            version = "1.0"
        """)
    pkg = GenConanfile("pkg", "1.0").with_python_requires("pyreq/1.0") \
                                    .with_exports_sources("*.txt")
    c.save({"pyreq/conanfile.py": pyreq, "pkg/conanfile.py": pkg, "pkg/f.txt": "content"})
    c.run("create pyreq")
    c.run("create pkg")

    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    _break_cache_recipes(c)
    # Sanity check, the recipes in the cache do explode if anything imports them
    c.run("graph info --requires=pkg/1.0", assert_error=True)
    assert "Recipe module executed!" in c.out

    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "Recipe module executed!" not in c.out
    assert "pkg/1.0: Uploading recipe" in c.out
    assert "pkg/1.0: Uploading package" in c.out
    assert "pyreq/1.0: Uploading recipe" in c.out

    # And what reached the server is usable, not the poisoned copy
    c2 = TestClient(servers=c.servers, inputs=["admin", "password"], light=True)
    c2.run("install --requires=pkg/1.0")
    assert "pkg/1.0: Downloaded package revision" in c2.out


def test_prepare_does_not_write_to_the_remote():
    """ The other half of the split's claim: preparing only reads from the remotes, so it can be
    given read-only credentials. Pinned here so that adding any write to it fails """
    server = TestServer(read_permissions=[("*/*@*/*", "*")], write_permissions=[],
                        users={"reader": "password"})
    c = TestClient(servers={"default": server}, inputs=4 * ["reader", "password"], light=True)
    c.run("remote login default reader -p password")
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")

    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")
    assert "Upload prepared in" in c.out
    assert "Permission denied" not in c.out

    # And the transfer is the step that needs write access, so it is the one that is refused
    c.run("upload-artifacts -l pkglist.json -r=default", assert_error=True)
    assert "Permission denied" in c.out


def test_prepare_applies_the_upload_policy():
    """ upload_policy='skip' is resolved while preparing, the artifacts step just honours the
    package list it is given """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")
           .with_class_attribute('upload_policy = "skip"')})
    c.run("create .")

    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")
    assert "pkg/1.0: Skipping upload of binaries, because upload_policy='skip'" in c.out
    pkglist = json.loads(c.load("pkglist.json"))
    rrev = next(iter(pkglist["default"]["pkg/1.0"]["revisions"].values()))
    assert rrev["packages"] == {}

    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c.out
    assert "Uploading package" not in c.out

    c.run("list pkg/1.0:* -r=default")
    assert "No packages found for this revision" in c.out


def test_prepare_skips_what_is_already_in_the_server():
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run("upload * -r=default -c")

    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")
    assert "already in server, skipping upload" in c.out
    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "Uploading recipe" not in c.out
    assert "Uploading package" not in c.out

    # Unless it is forced
    c.run('upload-prepare "*" -r=default -c --force --format=json', redirect_stdout="forced.json")
    c.run("upload-artifacts -l forced.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c.out


def test_upload_artifacts_dry_run():
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    c.run("upload-artifacts -l pkglist.json -r=default --dry-run")
    assert "Uploading recipe" not in c.out
    c.run("list pkg/1.0:* -r=default")
    assert "ERROR: Recipe not found" in c.out


def test_upload_artifacts_prepared_for_another_remote():
    """ What has to be uploaded is decided against one specific remote, using the result for a
    different one would upload the wrong things """
    c = _client()
    c.run("remote add other fake://other")
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    c.run("upload-artifacts -l pkglist.json -r=other", assert_error=True)
    assert "was not prepared for the remote 'other', but for 'default'" in c.out
    assert "conan upload-prepare ... -r=other" in c.out


def test_upload_artifacts_plain_list():
    """ A package list straight out of 'conan list' was never prepared, using it must not
    silently upload nothing """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run("list pkg/1.0:* --format=json", redirect_stdout="plain.json")
    # 'conan list' keys the result by "Local Cache", rename it to get past the remote check
    plain = c.load("plain.json").replace('"Local Cache"', '"default"', 1)
    c.save({"plain.json": plain})

    c.run("upload-artifacts -l plain.json -r=default", assert_error=True)
    assert "not prepared for upload" in c.out
    # And it says exactly how to fix it, that same file is valid input for upload-prepare
    assert "conan upload-prepare -l plain.json -r=default --format=json > prepared.json" in c.out
    assert "conan upload-artifacts -l prepared.json -r=default" in c.out


def test_upload_artifacts_list_from_conan_upload():
    """ The output of 'conan upload' does carry upload decisions and compressed files, so it is
    the preparation mark, and not the presence of those, that decides whether a list is usable """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run("upload * -r=default -c --format=json", redirect_stdout="uploaded.json")

    c.run("upload-artifacts -l uploaded.json -r=default", assert_error=True)
    assert "not prepared for upload" in c.out
    assert "pkg/1.0#" in c.out  # and it names them


def test_upload_artifacts_partially_prepared_list():
    """ One single reference, prepared, with the mark removed from one of its package revisions.
    The count of unprepared entries then reaches the count of references, which used to take the
    wrong branch and report the whole list as never prepared without naming anything """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0").with_settings("os")})
    c.run("create . -s os=Linux")
    c.run("create . -s os=Windows")
    c.run('upload-prepare "pkg/1.0:*" -r=default -c --format=json', redirect_stdout="prep.json")

    pkglist = json.loads(c.load("prep.json"))
    rrev = next(iter(pkglist["default"]["pkg/1.0"]["revisions"].values()))
    unmarked = sorted(rrev["packages"])[0]
    for prev in rrev["packages"][unmarked]["revisions"].values():
        prev.pop("upload-prepared")
    c.save({"mix.json": json.dumps(pkglist)})

    c.run("upload-artifacts -l mix.json -r=default", assert_error=True)
    assert "1 entry of the package list 'mix.json' was not prepared for upload" in c.out
    assert unmarked in c.out  # and it names the one that was not
    c.run("list *:* -r=default")
    assert "pkg/1.0" not in c.out  # nothing was uploaded, not even the prepared part


def test_merge_two_prepared_lists():
    """ Preparing on several agents and merging before a single upload has to keep working """
    c = _client()
    c.save({"conanfile.py": GenConanfile()})
    c.run("create . --name=liba --version=1.0")
    c.run("create . --name=libb --version=1.0")
    c.run("upload-prepare liba/1.0 -r=default -c --format=json", redirect_stdout="a.json")
    c.run("upload-prepare libb/1.0 -r=default -c --format=json", redirect_stdout="b.json")
    c.run("pkglist merge -l a.json -l b.json --format=json", redirect_stdout="merged.json")
    # The merged list is still a valid package list for the text formatters
    c.run("pkglist merge -l a.json -l b.json")

    c.run("upload-artifacts -l merged.json -r=default")
    assert "liba/1.0: Uploading recipe" in c.out
    assert "libb/1.0: Uploading recipe" in c.out


def test_upload_artifacts_empty_list():
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "nonexistent*" -r=default -c --format=json',
          redirect_stdout="pkglist.json")
    assert "Nothing was prepared because the selection is empty." in c.out

    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "No packages were uploaded because the selection is empty." in c.out


def test_prepare_only_recipe():
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --only-recipe --format=json',
          redirect_stdout="pkglist.json")
    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c.out
    assert "Uploading package" not in c.out


@pytest.mark.parametrize("parallel", [1, 2])
def test_prepare_and_upload_artifacts_parallel(parallel):
    c = _client()
    c.save_home({"global.conf": f"core.upload:parallel={parallel}"})
    c.save({"conanfile.py": GenConanfile()})
    for index in range(2):
        c.run(f"create . --name=lib{index} --version=1.0")

    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")
    assert (f"Preparing with {parallel} parallel threads" in c.out) is (parallel > 1)
    c.run("upload-artifacts -l pkglist.json -r=default")
    assert (f"Uploading with {parallel} parallel threads" in c.out) is (parallel > 1)
    for index in range(2):
        assert f"lib{index}/1.0: Uploading recipe" in c.out
        assert f"lib{index}/1.0: Uploading package" in c.out


def test_prepare_from_a_package_list():
    """ upload-prepare accepts a "conan list" package list as input, like upload does """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run("list pkg/1.0:* --format=json", redirect_stdout="selection.json")

    c.run("upload-prepare -l selection.json -r=default --format=json",
          redirect_stdout="pkglist.json")
    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c.out


def test_upload_artifacts_returns_the_same_as_conan_upload():
    """ Both commands share the same formatters, and the prepared list holds absolute paths just
    like 'conan upload' does, so what they return is the same except for the preparation mark.
    This used to differ: 'upload-artifacts' emitted cache relative paths and 'upload' absolute
    ones, silently changing the shape for anything consuming '--format=json' """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0").with_package_file("f.txt", "content")})
    c.run("create .")

    c.run("upload * -r=default -c --format=json", redirect_stdout="whole.json")
    whole = json.loads(c.load("whole.json"))

    # Clear the server so the two step flow really transfers, and do the very same thing again
    c.run("remove pkg/1.0 -r=default -c")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="prep.json")
    c.run("upload-artifacts -l prep.json -r=default --format=json", redirect_stdout="split.json")
    split = json.loads(c.load("split.json"))

    # The preparation mark is the one intended difference, and only the split flow carries it
    assert not any("upload-prepared" in r for r in _revisions(whole))
    marks = [r.pop("upload-prepared", None) for r in _revisions(split)]
    assert marks and all(m == {"format": 1, "remote": "default",
                               "url": c.servers["default"].fake_url} for m in marks)

    assert split == whole


def test_prepared_paths_are_absolute():
    """ The prepared list points at the artifacts with absolute paths, so it has to be used
    against the cache that produced it. Same trust model as any other package list Conan reads
    from a file """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    pkglist = json.loads(c.load("pkglist.json"))
    rrev = next(iter(pkglist["default"]["pkg/1.0"]["revisions"].values()))
    prev = next(iter(next(iter(rrev["packages"].values()))["revisions"].values()))
    for files in (rrev["files"], prev["files"]):
        assert files
        for path in files.values():
            assert os.path.isabs(path)
            assert os.path.isfile(path)  # and they are really there, in this cache


def test_prepared_list_records_the_remote_it_was_prepared_for():
    """ Names are not enough: the same remote name can be repointed at another server between
    preparing and uploading, and what has to be uploaded is decided against one server """
    c = _two_servers_client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run("upload-prepare pkg/1.0 -r=server0 -c --format=json", redirect_stdout="p.json")
    c.run("upload-artifacts -l p.json -r=server0")  # everything goes to server0
    assert "pkg/1.0: Uploading recipe" in c.out

    # Prepared again for server0, which now has it, so every entry is marked as not to upload
    c.run("upload-prepare pkg/1.0 -r=server0 -c --format=json", redirect_stdout="p2.json")
    # Repoint 'server0' at the other, empty server. Uploading the same list must not claim success
    c.run("remote remove server1")  # free its url, remotes cannot share one
    c.run(f'remote update server0 --url="{c.servers["server1"].fake_url}"')
    c.run("upload-artifacts -l p2.json -r=server0", assert_error=True)
    assert "was prepared for 'server0'" in c.out
    assert "it is being uploaded to 'server0'" in c.out
    assert "has to be prepared again" in c.out


def test_upload_artifacts_rejects_entries_for_other_remotes():
    """ Merging a prepared list with a plain one produces a document with two top level keys;
    uploading only the prepared one would silently leave the rest out """
    c = _client()
    c.save({"conanfile.py": GenConanfile()})
    c.run("create . --name=liba --version=1.0")
    c.run("create . --name=libb --version=1.0")
    c.run("upload-prepare liba/1.0 -r=default -c --format=json", redirect_stdout="prep.json")
    c.run('list "libb/1.0#*:*#*" --format=json', redirect_stdout="plain.json")
    c.run("pkglist merge -l prep.json -l plain.json --format=json", redirect_stdout="mix.json")

    c.run("upload-artifacts -l mix.json -r=default", assert_error=True)
    assert "has entries for 'Local Cache'" in c.out
    assert "would silently leave those out" in c.out
    c.run("list *:* -r=default")
    assert "liba/1.0" not in c.out  # not even the prepared half was uploaded


def test_upload_artifacts_error_entry_in_the_list():
    """ A "conan list" against an unreachable remote records an error instead of packages """
    c = _client()
    c.save({"bad.json": '{"default": {"error": "Could not reach the remote"}}'})
    c.run("upload-artifacts -l bad.json -r=default", assert_error=True)
    assert "holds an error for the remote 'default' instead of packages" in c.out
    assert "Traceback" not in c.out


def test_prepare_warns_about_entries_without_revisions():
    """ A coarse "conan list" query reports no revisions, and those entries can neither be
    prepared nor uploaded. Warn instead of dropping them silently """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('list "pkg*" --format=json', redirect_stdout="coarse.json")

    c.run("upload-prepare -l coarse.json -r=default --format=json", redirect_stdout="prep.json")
    assert "no revision and will not be prepared nor uploaded" in c.out
    assert "pkg/1.0" in c.out


def test_prepare_accepts_its_own_output_again():
    """ The advice printed when a list is not prepared says to run 'upload-prepare -l' on it, so
    that has to work for a list keyed by a remote name, not only by "Local Cache" """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('list "pkg/1.0#*:*#*" --format=json', redirect_stdout="plain.json")
    c.save({"renamed.json": c.load("plain.json").replace('"Local Cache"', '"default"', 1)})

    # This is the exact remediation the error advertises, run verbatim
    c.run("upload-artifacts -l renamed.json -r=default", assert_error=True)
    assert "conan upload-prepare -l renamed.json -r=default --format=json > prepared.json" in c.out
    c.run("upload-prepare -l renamed.json -r=default --format=json",
          redirect_stdout="prepared.json")
    c.run("upload-artifacts -l prepared.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c.out


def test_prepare_retargets_an_already_prepared_list():
    """ Preparing an already prepared list is how it is pointed at another remote: what has to be
    uploaded is asked of the new server, and the mark moves with it """
    c = _two_servers_client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run("upload-prepare pkg/1.0 -r=server0 -c --format=json", redirect_stdout="p0.json")
    c.run("upload-artifacts -l p0.json -r=server0")
    assert "pkg/1.0: Uploading recipe" in c.out

    # Re-targeted at the other server, which does not have it yet
    c.run("upload-prepare -l p0.json -r=server1 --format=json", redirect_stdout="p1.json")
    pkglist = json.loads(c.load("p1.json"))
    assert list(pkglist) == ["server1"]
    rrev = next(iter(pkglist["server1"]["pkg/1.0"]["revisions"].values()))
    assert rrev["upload-prepared"] == {"format": 1, "remote": "server1",
                                       "url": c.servers["server1"].fake_url}
    assert rrev["upload"] is True  # asked of server1, not carried over from server0
    assert "upload-urls" not in c.load("p1.json")  # and the ones for server0 are gone

    c.run("upload-artifacts -l p1.json -r=server1")
    assert "pkg/1.0: Uploading recipe" in c.out
    # The list is only valid for the remote it was last prepared for
    c.run("upload-artifacts -l p1.json -r=server0", assert_error=True)
    assert "was not prepared for the remote 'server0', but for 'server1'" in c.out


@pytest.mark.parametrize("kind", [
    pytest.param("symlinked_folder",
                 marks=pytest.mark.skipif(platform.system() == "Windows",
                                          reason="symlink need admin privileges")),
    "empty_folder", "regular_file"])
def test_prepare_retrieves_exports_sources_between_remotes(kind):
    """ Recipes installed from one remote and uploaded to a different one need their exported
    sources fetched first, and that has to happen while preparing, which is the step allowed to
    read the recipe.

    Exported sources that are only a symlinked or an empty folder are NOT recorded in the recipe
    manifest (FileTreeManifest.create discards gather_files() symlinked_folders) although they
    are uploaded, so any logic deciding this from the manifest silently loses them
    """
    c = _two_servers_client()
    if kind == "symlinked_folder":
        c.save({"conanfile.py": GenConanfile("pkg", "1.0").with_exports_sources("linked*"),
                "target/data.txt": "content"})
        os.symlink(os.path.join(c.current_folder, "target"),
                   os.path.join(c.current_folder, "linked"))
    elif kind == "empty_folder":
        c.save({"conanfile.py": textwrap.dedent("""
            import os
            from conan import ConanFile
            class Pkg(ConanFile):
                name = "pkg"
                version = "1.0"
                def export_sources(self):
                    os.makedirs(os.path.join(self.export_sources_folder, "placeholder"))
            """)})
    else:
        c.save({"conanfile.py": GenConanfile("pkg", "1.0").with_exports_sources("*.txt"),
                "data.txt": "content"})
    c.run("create .")

    # The manifest does not mention the sources for the folder cases, but they are uploaded
    layout = c.get_latest_ref_layout(RecipeReference.loads("pkg/1.0"))
    manifest = load(os.path.join(layout.export(), "conanmanifest.txt"))
    assert ("export_source/" in manifest) is (kind == "regular_file")
    c.run("upload-prepare pkg/1.0 -r=server0 -c --format=json", redirect_stdout="p0.json")
    c.run("upload-artifacts -l p0.json -r=server0")
    assert _server_has_sources(c.servers["server0"])

    # Installing brings the recipe, but not its exported sources
    c.run("remove * -c")
    c.run("install --requires=pkg/1.0 -r=server0")
    layout = c.get_latest_ref_layout(RecipeReference.loads("pkg/1.0"))
    assert not os.path.exists(layout.export_sources())

    c.run("upload-prepare pkg/1.0 -r=server1 -c --format=json", redirect_stdout="p1.json")
    assert "pkg/1.0: Sources downloaded from 'server0'" in c.out
    c.run("upload-artifacts -l p1.json -r=server1")
    assert "pkg/1.0: Uploading recipe" in c.out
    assert _server_has_sources(c.servers["server1"])


def test_prepare_metadata():
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    ref = RecipeReference.loads("pkg/1.0")
    metadata = os.path.join(c.get_latest_ref_layout(ref).metadata(), "logs", "mylog.txt")
    save(metadata, "log contents")

    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")
    assert "Recipe metadata: 1 files" in c.out
    c.run("upload-artifacts -l pkglist.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c.out
