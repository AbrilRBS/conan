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
    assert '.lib = "/fake/pkg/lib/libmylib.a"' in content
    assert '.name = "FOO", .value = "1"' in content
    assert '.name = "BAR", .value = "1"' in content
    assert '"pthread"' in content

    setup = client.load("conan_zig_deps/conan_setup.zig")
    assert "pub fn linkDependency(" in setup
    assert "pub fn linkDependencies(" in setup
    # Every mutating call must go through .root_module - these moved off Step.Compile
    # directly in Zig 0.16 (see std/Build/Module.zig vs the older Step/Compile.zig API)
    for call in ("addIncludePath", "addObjectFile", "linkSystemLibrary", "linkFramework",
                "addCMacro"):
        assert f"step.{call}(" not in setup
        assert f"module.{call}(" in setup
    # Runtime discovery is deliberately Conan's job (conanrun), not the generator's
    assert "addRPath" not in setup
    assert "addInstallFileWithDir" not in setup


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


def test_zigdeps_windows_shared_links_import_lib():
    """ A Windows shared lib links against the import lib (.lib), not the runtime .dll.
    Nothing is emitted to make the .dll findable at run time - that is left to Conan's
    conanrun environment, so the .dll path must not appear anywhere """
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
    assert '.lib = "C:/pkg/lib/mylib.lib"' in content
    assert "mylib.dll" not in content


def test_zigdeps_unix_shared_links_library_no_rpath():
    """ A Unix shared lib is linked directly, and deliberately gets no rpath - making it
    loadable at run time is Conan's job via conanrun, not the generator's """
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
    assert '.lib = "/fake/pkg/lib/libmylib.so"' in content
    assert "rpath" not in content


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


def test_zigdeps_header_only_components_get_root_target():
    """ Regression test: a components-based package where NO component produces a lib (all
    header-only) must still get a "pkg::pkg" root target, aggregating every contributing
    component - otherwise it's silently missing from linkDependencies()'s direct_targets,
    even though it's a real direct dependency """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "components": {
                "comp1": {"includedirs": ["include/comp1"], "defines": ["FOO"]},
                "comp2": {"includedirs": ["include/comp2"]},
            }
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert '.{ "pkg::pkg"' in content
    direct_targets_block = content.split("direct_targets")[1].split("conan_targets")[0]
    assert '"pkg::pkg"' in direct_targets_block
    root_block = _target_block(content, "pkg::pkg")
    assert '"pkg::comp1"' in root_block
    assert '"pkg::comp2"' in root_block


def test_zigdeps_dangling_component_reference_pruned():
    """ Regression test: a "requires" pointing at an exe-only component (which never becomes
    a target, since there's nothing to link) must be pruned rather than left dangling - the
    same applies to the analogous package-level (non-component) case """
    client = TestClient()
    client.save({
        "dep/conanfile.py": GenConanfile("dep", "1.0").with_package_info(cpp_info={
            "components": {
                "lib": {"libs": ["lib"], "location": '"/fake/dep/lib/liblib.a"',
                       "type": '"static-library"'},
                "tool": {"exe": '"mytool"', "location": '"/fake/dep/bin/mytool"'},
            }
        }),
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_require("dep/1.0").with_package_info(
            cpp_info={"requires": ["dep::tool", "dep::lib"]}),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create dep")
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert "dep::tool" not in content  # never a target: nothing to link for an exe
    pkg_block = _target_block(content, "pkg::pkg")
    assert '"dep::lib"' in pkg_block
    dep_block = _target_block(content, "dep::dep")
    assert '"dep::lib"' in dep_block  # the auto-created root also excludes the exe component


def test_zigdeps_versioned_shared_lib_links_link_location():
    """ A Unix shared lib with a distinct link_location (the common libfoo.so.1.2.3 +
    unversioned libfoo.so link-name pattern) links the unversioned name, since that is what
    link_location is for - the versioned runtime file is not referenced """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "libs": ["mylib"],
            "location": '"/fake/pkg/lib/libmylib.so.1.2.3"',
            "link_location": '"/fake/pkg/lib/libmylib.so"',
            "type": '"shared-library"',
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert '.lib = "/fake/pkg/lib/libmylib.so"' in content
    assert "libmylib.so.1.2.3" not in content


def test_zigdeps_control_characters_escaped():
    """ Regression test: a raw control character (not just backslash/quote) reaching
    _zigstr must be escaped, or it produces a Zig string literal that fails to compile """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(
            cpp_info={"defines": ["WEIRD=a\\nb"]}),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert '.value = "a\\\\nb"' in content  # escaped, not a literal raw newline
    assert "a\nb" not in content


def test_zigdeps_linkdependency_cycle_guard_present():
    """ The generated setup must guard linkDependency's recursion against a "requires" cycle
    (cpp_info component requires are free-form strings Conan doesn't validate for cycles,
    unlike the package graph) - see the functional cyclic-requires test for an end-to-end
    proof this doesn't crash a real `zig build` """
    client = TestClient()
    client.save({"conanfile.py": GenConanfile("app", "1.0")})
    client.run("install . -g ZigDeps")
    setup = client.load("conan_zig_deps/conan_setup.zig")

    assert "std.StringHashMap" in setup
    assert "visited.contains" in setup


def test_zigdeps_default_components():
    """ When cpp_info.default_components is set, the root target requires exactly those
    components - not every lib-producing one """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "default_components": ["comp1"],
            "components": {
                "comp1": {"libs": ["comp1lib"], "location": '"/fake/pkg/lib/libcomp1lib.a"',
                         "type": '"static-library"'},
                "comp2": {"libs": ["comp2lib"], "location": '"/fake/pkg/lib/libcomp2lib.a"',
                         "type": '"static-library"'},
            }
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    root_block = _target_block(content, "pkg::pkg")
    assert '"pkg::comp1"' in root_block
    assert '"pkg::comp2"' not in root_block


def test_zigdeps_app_dependency_excluded_from_requires():
    """ A dependency whose cpp_info marks it as an executable (.exe set, regardless of the
    recipe's own package_type) never becomes a target, so the implicit "link all direct
    deps" fallback _requires uses for a plain package with no explicit .requires must not
    leave a dangling reference to it - it doesn't make sense to link an executable """
    client = TestClient()
    client.save({
        "tool/conanfile.py": GenConanfile("tool", "1.0").with_package_info(
            cpp_info={"exe": '"mytool"', "location": '"/fake/tool/bin/mytool"'}),
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_require("tool/1.0").with_package_info(
            cpp_info={"libs": ["pkg"], "location": '"/fake/pkg/lib/libpkg.a"',
                     "type": '"static-library"'}),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create tool")
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert "tool::tool" not in content
    pkg_block = _target_block(content, "pkg::pkg")
    assert "tool" not in pkg_block


def test_zigdeps_exe_component_produces_no_target():
    """ A component with .exe set is entirely omitted from conan_deps.zig - there is nothing
    to link for an executable """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "components": {
                "lib": {"libs": ["lib"], "location": '"/fake/pkg/lib/liblib.a"',
                       "type": '"static-library"'},
                "tool": {"exe": '"mytool"', "location": '"/fake/pkg/bin/mytool"'},
            }
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    assert '.{ "pkg::lib"' in content
    assert "pkg::tool" not in content


def test_zigdeps_frameworks():
    """ Apple frameworks are collected and rendered for linkFramework() to consume """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(
            cpp_info={"frameworks": ["CoreFoundation", "Security"]}),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    pkg_block = _target_block(content, "pkg::pkg")
    assert '"CoreFoundation"' in pkg_block
    assert '"Security"' in pkg_block

    setup = client.load("conan_zig_deps/conan_setup.zig")
    assert "module.linkFramework(framework, .{})" in setup


def test_zigdeps_explicit_root_requires_on_plain_package():
    """ A non-components package that sets cpp_info.requires explicitly (rather than relying
    on the implicit "link all direct deps" fallback) resolves through the same
    parsed_requires() path a components-based package uses, not the transitive_reqs fallback """
    client = TestClient()
    client.save({
        "dep/conanfile.py": GenConanfile("dep", "1.0").with_package_info(cpp_info={
            "components": {
                "used": {"libs": ["used"], "location": '"/fake/dep/lib/libused.a"',
                         "type": '"static-library"'},
                "unused": {"libs": ["unused"], "location": '"/fake/dep/lib/libunused.a"',
                          "type": '"static-library"'},
            }
        }),
        "pkg/conanfile.py": GenConanfile("pkg", "1.0")
            .with_require("dep/1.0")
            .with_package_info(cpp_info={
                "libs": ["pkg"], "location": '"/fake/pkg/lib/libpkg.a"',
                "type": '"static-library"',
                "requires": ["dep::used"],  # only "used", not the whole "dep::dep" root
            }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create dep")
    client.run("create pkg")
    client.run("install . -g ZigDeps")
    content = client.load("conan_zig_deps/conan_deps.zig")

    pkg_block = _target_block(content, "pkg::pkg")
    assert '"dep::used"' in pkg_block
    assert '"dep::dep"' not in pkg_block
    assert '"dep::unused"' not in pkg_block
