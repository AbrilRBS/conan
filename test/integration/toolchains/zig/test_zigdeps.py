import re

from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.tools import TestClient


def _target_block(content, target_name):
    """ Extract a single target's own ``Target{ ... }`` body, so assertions can check that
    data isn't leaking between targets (matching on a bare "pkg::name" substring is not
    enough, since that name can also appear inside another target's "requires" list) """
    match = re.search(re.escape(f'.{{ "{target_name}", Target{{') + r"(.*?)\n    } },",
                      content, re.DOTALL)
    assert match, f'target "{target_name}" not found in:\n{content}'
    return match.group(1)


def test_zigdeps_simple_package():
    """ A package without components generates a single "pkg::pkg" target, no redundant
    interface indirection """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "libs": ["mylib"],
            "location": '"/fake/pkg/lib/libmylib.a"',
            "type": '"static-library"',
            "defines": ["FOO=1", "BAR"],
            "system_libs": ["pthread"],
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert content.count('.{ "pkg::pkg"') == 1
    assert '.kind = .STATIC' in content
    assert '.path = "/fake/pkg/lib/libmylib.a"' in content
    assert '.rpath_dir = null' in content
    assert '.name = "FOO", .value = "1"' in content
    assert '.name = "BAR", .value = "1"' in content
    assert '"pthread"' in content
    assert '"pkg::pkg"' in client.load("conan_zig_deps/conan_deps.zig")

    setup = client.load("conan_zig_deps/conan_setup.zig")
    assert "pub fn linkDependency(" in setup
    assert "pub fn linkDependencies(" in setup
    assert "step.xxx" not in setup  # sanity: nothing left calling the old direct-on-step API
    assert "module.addObjectFile" in setup
    assert "module.addRPath" in setup


def test_zigdeps_components_own_data_not_merged():
    """ Each component is its own target, carrying only its own includedirs/libs - not merged
    with sibling components - and internal component requires resolve to "pkg::comp" """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "components": {
                "comp1": {
                    "libs": ["comp1lib"],
                    "location": '"/fake/pkg/lib/libcomp1lib.a"',
                    "type": '"static-library"',
                    "includedirs": ["include/comp1"],
                    "requires": ["comp2"],
                },
                "comp2": {
                    "libs": ["comp2lib"],
                    "location": '"/fake/pkg/lib/libcomp2lib.a"',
                    "type": '"static-library"',
                    "includedirs": ["include/comp2"],
                },
            }
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    # comp1's own includedirs must not leak comp2's, and vice versa
    comp1_block = _target_block(content, "pkg::comp1")
    assert "include/comp1" in comp1_block
    assert "include/comp2" not in comp1_block
    comp2_block = _target_block(content, "pkg::comp2")
    assert "include/comp2" in comp2_block
    assert "include/comp1" not in comp2_block
    assert '"pkg::comp2"' in comp1_block  # internal requires resolved

    # Synthetic root target requires every real lib-producing component
    root_block = _target_block(content, "pkg::pkg")
    assert '"pkg::comp1"' in root_block
    assert '"pkg::comp2"' in root_block
    assert ".kind = .INTERFACE" in root_block


def test_zigdeps_cross_package_component_requires():
    """ A component's cross-package require resolves to "otherpkg::othercomp" when that
    component exists, or falls back to "otherpkg::otherpkg" when it doesn't """
    client = TestClient()
    client.save({
        "dep/conanfile.py": GenConanfile("dep", "1.0").with_package_info(cpp_info={
            "components": {
                "thecomp": {"libs": ["thelib"], "location": '"/fake/dep/lib/libthelib.a"',
                           "type": '"static-library"'},
            }
        }),
        "other/conanfile.py": GenConanfile("other", "1.0").with_package_info(
            cpp_info={"libs": ["otherlib"], "location": '"/fake/other/lib/libotherlib.a"',
                     "type": '"static-library"'}),
        "pkg/conanfile.py": GenConanfile("pkg", "1.0")
            .with_require("dep/1.0")
            .with_require("other/1.0")
            .with_package_info(cpp_info={
                "components": {
                    "comp1": {
                        "libs": ["comp1lib"],
                        "location": '"/fake/pkg/lib/libcomp1lib.a"',
                        "type": '"static-library"',
                        "requires": ["dep::thecomp", "other::other"],
                    },
                }
            }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create dep")
    client.run("create other")
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    comp1_block = _target_block(content, "pkg::comp1")
    assert '"dep::thecomp"' in comp1_block  # real component in "dep"
    assert '"other::other"' in comp1_block  # "other" has no components -> falls back to root


def test_zigdeps_transitive_chain():
    """ Plain (non-component) packages: liba -> libb -> libc, resolved through
    get_transitive_requires (the same helper CMakeConfigDeps uses) """
    client = TestClient()
    client.save({
        "libc/conanfile.py": GenConanfile("libc", "1.0").with_package_info(
            cpp_info={"libs": ["c"], "location": '"/fake/c/libc.a"', "type": '"static-library"'}),
        "libb/conanfile.py": GenConanfile("libb", "1.0").with_require("libc/1.0").with_package_info(
            cpp_info={"libs": ["b"], "location": '"/fake/b/libb.a"', "type": '"static-library"'}),
        "liba/conanfile.py": GenConanfile("liba", "1.0").with_require("libb/1.0").with_package_info(
            cpp_info={"libs": ["a"], "location": '"/fake/a/liba.a"', "type": '"static-library"'}),
        "conanfile.py": GenConanfile("app", "1.0").with_require("liba/1.0"),
    })
    client.run("create libc")
    client.run("create libb")
    client.run("create liba")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    direct_targets_block = content.split("direct_targets")[1].split("conan_targets")[0]
    assert '"liba::liba"' in direct_targets_block
    assert "libb::libb" not in direct_targets_block  # only direct deps, transitive linking
                                                     # is left to the "requires" recursion

    liba_block = _target_block(content, "liba::liba")
    assert '"libb::libb"' in liba_block
    libb_block = _target_block(content, "libb::libb")
    assert '"libc::libc"' in libb_block


def test_zigdeps_windows_shared_uses_import_lib_no_rpath():
    """ A Windows shared lib must link against the import lib (.lib), not the .dll, and must
    not get an rpath (rpath is a Unix ELF/Mach-O concept, meaningless for .dll loading) """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "libs": ["mylib"],
            "location": '"C:/pkg/bin/mylib.dll"',
            "link_location": '"C:/pkg/lib/mylib.lib"',
            "type": '"shared-library"',
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert ".kind = .SHARED" in content
    assert '.path = "C:/pkg/lib/mylib.lib"' in content
    assert "mylib.dll" not in content
    assert ".rpath_dir = null" in content


def test_zigdeps_unix_shared_gets_rpath():
    """ A Unix shared lib (.so/.dylib) gets its directory registered as an rpath so the
    resulting binary can find it at runtime (mirrors the fix already applied in BazelDeps
    for https://github.com/conan-io/conan/issues/19190) """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "libs": ["mylib"],
            "location": '"/fake/pkg/lib/libmylib.so"',
            "type": '"shared-library"',
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert ".kind = .SHARED" in content
    assert '.path = "/fake/pkg/lib/libmylib.so"' in content
    assert '.rpath_dir = "/fake/pkg/lib"' in content


def test_zigdeps_header_only_no_lib_entry():
    """ A header-only package/component contributes includedirs/defines but no ``lib`` entry,
    and doesn't get skipped just because it has no library file """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(
            cpp_info={"defines": ["HEADER_ONLY"]}),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert '.{ "pkg::pkg"' in content
    assert ".kind = .INTERFACE" in content
    assert ".lib = null" in content
    assert '.name = "HEADER_ONLY", .value = "1"' in content
