import textwrap

import pytest

from conan.api.conan_api import ConanAPI
from conan.api.model import ListPattern, RecipeReference
from conan.internal.util.files import load, save
from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.mocks import RedirectedTestOutput
from conan.test.utils.tools import TestClient, redirect_output


def test_build_policies_in_conanfile():
    client = TestClient(default_server_user=True, light=True)
    base = GenConanfile("hello0", "1.0").with_exports("*")
    conanfile = str(base) + "\n    build_policy = 'missing'"
    client.save({"conanfile.py": conanfile})
    client.run("export . --user=lasote --channel=stable")

    # Install, it will build automatically if missing (without the --build missing option)
    client.run("install --requires=hello0/1.0@lasote/stable")
    assert "Building" in client.out

    # Try to do it again, now we have the package, so no build is done
    client.run("install --requires=hello0/1.0@lasote/stable")
    assert "Building" not in client.out

    # Try now to upload all packages, should not crash because of the "missing" build policy
    client.run("upload hello0/1.0@lasote/stable -r default")

    #  --- Build policy to always ---
    conanfile = str(base) + "\n    build_policy = 'always'"
    client.save({"conanfile.py": conanfile}, clean_first=True)
    client.run("export . --user=lasote --channel=stable")

    # Install, it will build automatically if missing (without the --build missing option)
    client.run("install --requires=hello0/1.0@lasote/stable", assert_error=True)
    assert "ERROR: hello0/1.0@lasote/stable: build_policy='always' has been removed" in client.out


def test_build_policy_missing():
    c = TestClient(default_server_user=True, light=True)
    conanfile = GenConanfile("pkg", "1.0").with_class_attribute('build_policy = "missing"')\
                                          .with_class_attribute('upload_policy = "skip"')
    c.save({"conanfile.py": conanfile})
    c.run("export .")

    # the --build=never has higher priority
    c.run("install --requires=pkg/1.0@ --build=never", assert_error=True)
    assert "ERROR: Missing prebuilt package for 'pkg/1.0'" in c.out

    c.run("install --requires=pkg/1.0@")
    assert "pkg/1.0: Building package from source as defined by build_policy='missing'" in c.out

    # If binary already there it should do nothing
    c.run("install --requires=pkg/1.0@")
    assert "pkg/1.0: Building package from source" not in c.out

    c.run("upload * -r=default -c")
    assert "Uploading package" not in c.out
    assert "pkg/1.0: Skipping upload of binaries, because upload_policy='skip'" in c.out


def _break_cache_recipe(client, ref):
    """ Make the recipe stored in the cache raise as soon as its module is imported.
    Everything that keeps working afterwards proves that the recipe was not loaded """
    conanfile_path = client.get_latest_ref_layout(RecipeReference.loads(ref)).conanfile()
    save(conanfile_path, 'raise Exception("Recipe module executed!")\n' + load(conanfile_path))


def _check_upstream(client, pattern="*"):
    """ Run only the ``check_upstream`` step of the upload, the one deciding, based on the
    ``upload_policy``, whether the binaries have to be uploaded or not.
    Returns the resulting binaries of the package list, and the captured output """
    stdout = RedirectedTestOutput()
    with redirect_output(stdout), client.mocked_servers():
        conan_api = ConanAPI(client.cache_folder)
        pkglist = conan_api.list.select(ListPattern(pattern, package_id="*"))
        conan_api.upload.check_upstream(pkglist, conan_api.remotes.get("default"),
                                        conan_api.remotes.list())
    binaries = {str(ref): list(prefs) for ref, prefs in pkglist.items()}
    return binaries, str(stdout)


@pytest.mark.parametrize("attribute, skipped", [
    # The attribute is located with a regex over the recipe text, admitting the usual spellings
    ('upload_policy = "skip"', True),
    ("upload_policy = 'skip'", True),
    ('upload_policy="skip"', True),
    ('upload_policy   =   "skip"', True),
    ('upload_policy = "skip"  # only the recipe is published', True),
    # A commented out attribute is not a declaration
    ('# upload_policy = "skip"', False),
    # Other values than "skip" do not skip anything
    ('upload_policy = "whatever"', False),
    # And the vast majority of recipes, that don't declare it at all
    (None, False),
], ids=["double_quotes", "single_quotes", "no_spaces", "extra_spaces", "trailing_comment",
        "commented_out", "other_value", "not_declared"])
def test_upload_policy_parsed_without_loading_recipe(attribute, skipped):
    """ The upload_policy is parsed statically from the recipe text, the recipe module must not
    be imported: that would execute arbitrary recipe code in a machine holding the credentials
    to upload to the server """
    c = TestClient(default_server_user=True, light=True)
    conanfile = GenConanfile("pkg", "1.0")
    if attribute is not None:
        conanfile = conanfile.with_class_attribute(attribute)
    c.save({"conanfile.py": conanfile})
    c.run("create .")
    _break_cache_recipe(c, "pkg/1.0")
    # Sanity check, the recipe in the cache does explode if something imports it
    c.run("graph info --requires=pkg/1.0", assert_error=True)
    assert "Recipe module executed!" in c.out

    binaries, out = _check_upstream(c)
    assert list(binaries) == ["pkg/1.0"]
    if skipped:
        assert binaries["pkg/1.0"] == []
        assert "pkg/1.0: Skipping upload of binaries, because upload_policy='skip'" in out
    else:
        assert len(binaries["pkg/1.0"]) == 1
        assert "Skipping upload of binaries" not in out


def test_upload_policy_skip():
    """ End to end, the binaries are not uploaded, but the recipe still is """
    c = TestClient(default_server_user=True, light=True)
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")
           .with_class_attribute('upload_policy = "skip"')})
    c.run("create .")
    c.run("upload * -r=default -c")
    assert "pkg/1.0: Skipping upload of binaries, because upload_policy='skip'" in c.out
    assert "Uploading recipe 'pkg/1.0" in c.out
    assert "Uploading package" not in c.out

    c.run("list pkg/1.0:* -r=default")
    assert "No packages found for this revision" in c.out


def test_upload_policy_commented_out():
    """ A commented out upload_policy is not a declaration, binaries are uploaded """
    c = TestClient(default_server_user=True, light=True)
    c.save({"conanfile.py": GenConanfile("pkg", "1.0")
           .with_class_attribute("# upload_policy = \"skip\"")})
    c.run("create .")
    c.run("upload * -r=default -c")
    assert "Skipping upload of binaries" not in c.out
    assert "Uploading package" in c.out


def test_upload_policy_from_python_requires():
    """ The attribute is not in the recipe text, but inherited from a python_require. That is the
    only case in which the recipe has to be loaded to know the upload_policy """
    c = TestClient(default_server_user=True, light=True)
    pyreq = textwrap.dedent("""
        from conan import ConanFile

        class MyBase:
            upload_policy = "skip"

        class PyReq(ConanFile):
            name = "pyreq"
            version = "1.0"
        """)
    pkg = GenConanfile("pkg", "1.0").with_python_requires("pyreq/1.0") \
                                    .with_class_attribute('python_requires_extend = "pyreq.MyBase"')
    c.save({"pyreq/conanfile.py": pyreq, "pkg/conanfile.py": pkg})
    c.run("create pyreq")
    c.run("create pkg")
    assert "upload_policy" not in c.load("pkg/conanfile.py")  # it is not there to be parsed

    c.run("upload pkg/1.0 -r=default -c")
    assert "pkg/1.0: Skipping upload of binaries, because upload_policy='skip'" in c.out
    assert "Uploading package" not in c.out

    c.run("list pkg/1.0:* -r=default")
    assert "No packages found for this revision" in c.out


def test_upload_policy_not_inherited_from_python_requires():
    """ A python_require declaring the attribute for itself doesn't propagate it to its consumers,
    the recipe is loaded and the resolved (empty) upload_policy is the one that counts """
    c = TestClient(default_server_user=True, light=True)
    pyreq = textwrap.dedent("""
        from conan import ConanFile

        class MyBase:
            pass

        class PyReq(ConanFile):
            name = "pyreq"
            version = "1.0"
            upload_policy = "skip"
        """)
    pkg = GenConanfile("pkg", "1.0").with_python_requires("pyreq/1.0") \
                                    .with_class_attribute('python_requires_extend = "pyreq.MyBase"')
    c.save({"pyreq/conanfile.py": pyreq, "pkg/conanfile.py": pkg})
    c.run("create pyreq")
    c.run("create pkg")

    c.run("upload pkg/1.0 -r=default -c")
    assert "Skipping upload of binaries" not in c.out
    assert "Uploading package" in c.out


def test_upload_policy_in_recipe_short_circuits_python_requires():
    """ Having python_requires is what allows loading the recipe, but if the recipe declares the
    attribute itself, it is parsed statically and nothing is loaded """
    c = TestClient(default_server_user=True, light=True)
    pkg = GenConanfile("pkg", "1.0").with_python_requires("pyreq/1.0") \
                                    .with_class_attribute('upload_policy = "skip"')
    c.save({"pyreq/conanfile.py": GenConanfile("pyreq", "1.0"), "pkg/conanfile.py": pkg})
    c.run("create pyreq")
    c.run("create pkg")
    _break_cache_recipe(c, "pkg/1.0")

    binaries, out = _check_upstream(c, pattern="pkg/1.0")
    assert binaries["pkg/1.0"] == []
    assert "pkg/1.0: Skipping upload of binaries, because upload_policy='skip'" in out
