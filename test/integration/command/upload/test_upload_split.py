import json
import os
import shutil
import textwrap

import pytest

from conan.api.model import RecipeReference
from conan.internal.util.files import load, save
from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.test_files import temp_folder
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
    keeps working afterwards proves that no recipe was loaded """
    broken = 0
    for root, _, files in os.walk(os.path.join(client.cache_folder, "p")):
        for f in files:
            if f == "conanfile.py":
                path = os.path.join(root, f)
                save(path, 'raise Exception("Recipe module executed!")\n' + load(path))
                broken += 1
    assert broken, "No recipes found in the cache to break"


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
    c.run("remote add other fake://other", assert_error=False)
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    c.run("upload-artifacts -l pkglist.json -r=other", assert_error=True)
    assert "was not prepared for the remote 'other', but for 'default'" in c.out
    assert "conan upload-prepare ... -r=other" in c.out


def test_upload_artifacts_list_not_prepared():
    """ A package list straight out of 'conan list' records nothing about what to upload, using
    it must not silently upload nothing """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run("list pkg/1.0:* --format=json", redirect_stdout="plain.json")
    # 'conan list' keys the result by "Local Cache", rename it to get past the remote check
    plain = c.load("plain.json").replace('"Local Cache"', '"default"', 1)
    c.save({"plain.json": plain})

    c.run("upload-artifacts -l plain.json -r=default", assert_error=True)
    assert "has not been prepared for upload" in c.out
    assert "conan upload-prepare ... -r=default --format=json" in c.out


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
    c.run("upload-artifacts -l pkglist.json -r=default")
    for index in range(2):
        assert f"lib{index}/1.0: Uploading recipe" in c.out


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


def test_prepared_paths_are_cache_relative():
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    content = c.load("pkglist.json")
    assert c.cache_folder not in content  # no absolute path leaked
    pkglist = json.loads(content)
    rrev = next(iter(pkglist["default"]["pkg/1.0"]["revisions"].values()))
    prev = next(iter(next(iter(rrev["packages"].values()))["revisions"].values()))
    for files in (rrev["files"], prev["files"]):
        for path in files.values():
            assert not os.path.isabs(path)
            assert "\\" not in path  # always "/", the list can travel between platforms


def test_upload_artifacts_with_the_cache_in_another_location():
    """ The prepared list is resolved against the local cache, so the transfer can run in a
    different machine or container, as long as it has the same cache contents """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0").with_package_file("f.txt", "content")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    # A second client, with the very same cache contents but in a different folder
    moved_cache = os.path.join(temp_folder(), "moved", ".conan2")
    shutil.copytree(c.cache_folder, moved_cache)
    c2 = TestClient(cache_folder=moved_cache, servers=c.servers, inputs=["admin", "password"],
                    light=True)
    assert c2.cache_folder != c.cache_folder
    c2.save({"pkglist.json": c.load("pkglist.json")})

    c2.run("upload-artifacts -l pkglist.json -r=default")
    assert "pkg/1.0: Uploading recipe" in c2.out
    assert "pkg/1.0: Uploading package" in c2.out

    c3 = TestClient(servers=c.servers, inputs=["admin", "password"], light=True)
    c3.run("install --requires=pkg/1.0")
    assert "pkg/1.0: Downloaded package revision" in c3.out


def test_upload_artifacts_rejects_paths_outside_the_cache():
    """ The package list is a file, and whoever runs this step holds the upload credentials, so
    it must not be able to make it upload anything outside of the cache """
    c = _client()
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")})
    c.run("create .")
    c.run('upload-prepare "*" -r=default -c --format=json', redirect_stdout="pkglist.json")

    pkglist = json.loads(c.load("pkglist.json"))
    rrev = next(iter(pkglist["default"]["pkg/1.0"]["revisions"].values()))
    rrev["files"]["conanfile.py"] = "../../../secret.txt"
    c.save({"tampered.json": json.dumps(pkglist)})

    c.run("upload-artifacts -l tampered.json -r=default", assert_error=True)
    assert "is outside of the Conan cache" in c.out
    assert "refusing to upload it" in c.out


@pytest.mark.parametrize("kind", ["symlinked_folder", "empty_folder", "regular_file"])
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
