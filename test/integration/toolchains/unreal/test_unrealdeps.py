import re
import textwrap

from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.tools import TestClient


def _target_block(content, target_name):
    """ Extract a single target's own ``new ConanTarget { ... }`` body, so assertions can
    check that data isn't leaking between targets (matching on a bare "pkg::name" substring is
    not enough, since that name can also appear inside another target's Requires array) """
    pattern = re.escape(f'["{target_name}"] = new ConanTarget') + r"\s*\{(.*?)\n            \},"
    match = re.search(pattern, content, re.DOTALL)
    assert match, f'target "{target_name}" not found in:\n{content}'
    return match.group(1)


def test_unrealdeps_simple_package():
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
    client.run("install . -g UnrealDeps")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")

    assert content.count('["pkg::pkg"] = new ConanTarget') == 1
    block = _target_block(content, "pkg::pkg")
    assert "Kind = ConanTargetKind.Static" in block
    assert 'Library = "/fake/pkg/lib/libmylib.a"' in block
    assert '("FOO", "1")' in block
    assert '("BAR", "")' in block
    assert '"pthread"' in block
    assert "RuntimeLibraries = new string[] {  }" in block  # static, nothing to copy at run time

    setup = client.load("conan_unreal_deps/ConanDependenciesSetup.build.cs")
    assert "public static void AddConanDependency(this ModuleRules rules" in setup
    assert "public static void AddConanDependencies(this ModuleRules rules" in setup
    for member in ("PublicSystemIncludePaths", "PublicDefinitions", "PublicSystemLibraries",
                  "PublicAdditionalLibraries", "RuntimeDependencies", "PublicAdditionalFrameworks",
                  "PublicFrameworks"):
        assert member in setup
    # Dependency headers go through PublicSystemIncludePaths, not PublicIncludePaths: their
    # own warnings aren't checked when resolving this module's header dependencies
    assert "rules.PublicIncludePaths" not in setup


def test_unrealdeps_components_own_data_not_merged():
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
    client.run("install . -g UnrealDeps")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")

    comp1_block = _target_block(content, "pkg::comp1")
    assert "include/comp1" in comp1_block
    assert "include/comp2" not in comp1_block
    assert '"pkg::comp2"' in comp1_block  # internal requires resolved

    comp2_block = _target_block(content, "pkg::comp2")
    assert "include/comp2" in comp2_block
    assert "include/comp1" not in comp2_block

    # Synthetic root target requires every real lib-producing component
    root_block = _target_block(content, "pkg::pkg")
    assert '"pkg::comp1"' in root_block
    assert '"pkg::comp2"' in root_block
    assert "Kind = ConanTargetKind.Interface" in root_block


def test_unrealdeps_component_with_multiple_libs_expands_to_one_target_per_lib():
    """ ``ConanTarget.Library`` is a single path, not an array - but that is not a limitation,
    because a component listing more than one entry in ``cpp_info.libs`` never reaches this
    generator (or ZigDeps, or CMakeConfigDeps - all three call the same
    ``cpp_info.deduce_full_cpp_info``) in that shape to begin with. That shared helper expands
    such a component into one synthetic single-lib sub-component per library ("_<comp>_<lib>"),
    plus the original component name kept as a header-only aggregate that Requires all of them
    - the same "warns but supports it" splitting CMakeConfigDeps relies on too. """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "components": {"core": {"libs": ["core1", "core2"], "includedirs": ["include"]}},
        }).with_package_file("lib/libcore1.a", "fake").with_package_file("lib/libcore2.a", "fake"),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g UnrealDeps")
    assert "contains more than 1 library" in client.out  # the same deprecation warning
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")

    lib1_block = _target_block(content, "pkg::_core_core1")
    assert "Kind = ConanTargetKind.Static" in lib1_block
    assert lib1_block.count("libcore1.a") == 1
    assert "libcore2.a" not in lib1_block  # each split target only carries its own library
    lib2_block = _target_block(content, "pkg::_core_core2")
    assert lib2_block.count("libcore2.a") == 1
    assert "libcore1.a" not in lib2_block

    # The original component name survives as a header-only aggregate over both split libs, so
    # AddConanDependency("pkg::core") - the name a recipe/consumer actually knows about - still
    # pulls in every one of the split archives
    core_block = _target_block(content, "pkg::core")
    assert "Kind = ConanTargetKind.Interface" in core_block
    assert '"pkg::_core_core1"' in core_block
    assert '"pkg::_core_core2"' in core_block
    assert "Library" not in core_block


def test_unrealdeps_shared_library_gets_runtime_dependency():
    """ A shared library needs both a Library to link against (the import lib on Windows,
    same file as the runtime one elsewhere) and RuntimeLibraries Unreal Build Tool should
    stage next to the packaged binary - unlike ZigDeps, this generator can actually solve
    run-time loadability itself instead of punting it to a separate mechanism. This ``.dll``
    has no symlink siblings (it's the only file in its fake directory, which does not even
    exist on disk), so RuntimeLibraries falls back to just the one path - see
    test_unrealdeps_shared_library_ships_every_symlink_in_the_chain for the interesting case """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "libs": ["mylib"],
            "type": '"shared-library"',
            "location": '"/fake/pkg/bin/mylib.dll"',
            "link_location": '"/fake/pkg/lib/mylib.lib"',
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g UnrealDeps")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")
    block = _target_block(content, "pkg::pkg")

    assert "Kind = ConanTargetKind.Shared" in block
    assert 'Library = "/fake/pkg/lib/mylib.lib"' in block
    assert 'RuntimeLibraries = new string[] { "/fake/pkg/bin/mylib.dll",  }' in block

    setup = client.load("conan_unreal_deps/ConanDependenciesSetup.build.cs")
    assert "rules.RuntimeDependencies.Add(runtimeLibrary)" in setup


def test_unrealdeps_shared_library_ships_every_symlink_in_the_chain():
    """ A Unix shared library's own install name / SONAME - what a consumer's load command
    actually asks the dynamic linker for at run time - is very often a *different* filename
    from the dev-time symlink Conan resolves as cpp_info.location: "libmylib.so" conventionally
    points at "libmylib.1.so", which points at the real "libmylib.1.2.3.so", and it is
    "libmylib.1.so" a consumer's binary embeds. Shipping only ``location`` would silently
    produce a build that fails to load the library at run time once copied anywhere without
    the rest of that chain (e.g. once actually staged/packaged) - so every same-target name
    must ship, not only the one file "Library" links against. """
    client = TestClient()
    conanfile = textwrap.dedent("""
        import os
        from conan import ConanFile

        class Pkg(ConanFile):
            name = "pkg"
            version = "1.0"
            package_type = "shared-library"

            def package(self):
                lib_dir = os.path.join(self.package_folder, "lib")
                os.makedirs(lib_dir)
                open(os.path.join(lib_dir, "libmylib.1.2.3.so"), "w").close()
                os.symlink("libmylib.1.2.3.so", os.path.join(lib_dir, "libmylib.1.so"))
                os.symlink("libmylib.1.so", os.path.join(lib_dir, "libmylib.so"))

            def package_info(self):
                self.cpp_info.libs = ["mylib"]
                self.cpp_info.type = "shared-library"
                self.cpp_info.location = os.path.join(self.package_folder, "lib", "libmylib.so")
        """)
    client.save({
        "pkg/conanfile.py": conanfile,
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g UnrealDeps")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")
    block = _target_block(content, "pkg::pkg")

    runtime_libs_line = next(l for l in block.splitlines() if "RuntimeLibraries" in l)
    for name in ("libmylib.so", "libmylib.1.so", "libmylib.1.2.3.so"):
        assert name in runtime_libs_line
    assert runtime_libs_line.count(".so") == 3  # exactly these three, nothing else swept in


def test_unrealdeps_package_framework():
    """ A dependency that *is* a single .framework bundle (cpp_info.package_framework) is
    exposed as a name + its parent directory as a search path, matching how ZigDeps handles
    the same field """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_package_info(cpp_info={
            "package_framework": '"/fake/pkg/Frameworks/Awesome.framework"',
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create pkg")
    client.run("install . -g UnrealDeps")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")
    block = _target_block(content, "pkg::pkg")

    assert 'Frameworks = new string[] { "Awesome",' in block
    assert 'FrameworkPaths = new string[] { "/fake/pkg/Frameworks",' in block

    setup = client.load("conan_unreal_deps/ConanDependenciesSetup.build.cs")
    # Concrete path resolved at consume-time: PublicAdditionalFrameworks needs one, there is
    # no generic framework search-path list the way there is for headers
    assert "new ModuleRules.Framework(name, resolved)" in setup


def test_unrealdeps_windows_system_libs_get_lib_suffix():
    """ Conan's own convention omits the extension (e.g. "ws2_32"), but VCToolChain passes a
    PublicSystemLibraries entry straight to the linker command line, so on Windows it must
    carry the ".lib" suffix """
    client = TestClient()
    client.save({
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_settings("os").with_package_info(
            cpp_info={"system_libs": ["ws2_32", "already.lib"]}),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0").with_settings("os"),
    })
    client.run("create pkg -s os=Windows")
    client.run("install . -g UnrealDeps -s os=Windows")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")
    block = _target_block(content, "pkg::pkg")
    assert '"ws2_32.lib"' in block
    assert '"already.lib"' in block
    assert '"ws2_32",' not in block  # not left bare


def test_unrealdeps_tool_requires_exposed_as_tool_dirs_and_exes():
    """ tool_requires contribute no linkable target, but their executables and bindirs are
    still exposed - there is nothing else for a custom build step to find them by """
    client = TestClient()
    client.save({
        "tool/conanfile.py": GenConanfile("mytool", "1.0").with_package_info(cpp_info={
            "exe": True,
            "location": '"/fake/mytool/bin/mytool"',
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_tool_requires("mytool/1.0"),
    })
    client.run("create tool")
    client.run("install . -g UnrealDeps")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")

    assert '["mytool::mytool"] = "/fake/mytool/bin/mytool"' in content
    # The bindir itself comes from the real (on-disk) package layout, not the faked exe
    # location, so only check its shape rather than pin the whole path
    tool_dirs_line = next(l for l in content.splitlines() if '["mytool"] = new string[]' in l)
    assert tool_dirs_line.strip().split('"')[3].endswith("/bin")
    # A tool_require never becomes a linkable Requires target
    assert '["mytool::mytool"] = new ConanTarget' not in content


def test_unrealdeps_unknown_component_requires_raises():
    """ A recipe requiring a component that does not exist is a recipe bug, not something to
    silently drop. Conan's own recipe-time validation only catches an internal (same-package)
    dangling component reference, not a cross-package one like this - that only shows up once
    the full graph is available, which is exactly what this generator resolves against """
    client = TestClient()
    client.save({
        "otherpkg/conanfile.py": GenConanfile("otherpkg", "1.0").with_package_info(cpp_info={
            "components": {"real": {"libs": ["reallib"]}},
        }),
        "pkg/conanfile.py": GenConanfile("pkg", "1.0").with_require("otherpkg/1.0")
                                                       .with_package_info(cpp_info={
            "requires": ["otherpkg::badcomponent"],
            "libs": ["mylib"],
            "type": '"static-library"',
            "location": '"/fake/pkg/lib/libmylib.a"',
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("pkg/1.0"),
    })
    client.run("create otherpkg")
    client.run("create pkg")
    client.run("install . -g UnrealDeps", assert_error=True)
    assert "component 'badcomponent' was not found in 'otherpkg'" in client.out


def test_unrealdeps_app_excluded_from_direct_targets():
    """ An application dependency may still contribute a target (e.g. some include dirs), but
    it must never show up in DirectTargets - there is nothing to usefully link for an App """
    client = TestClient()
    client.save({
        "tool/conanfile.py": GenConanfile("mytool", "1.0").with_package_type("application")
                                                          .with_package_info(cpp_info={
            "includedirs": ["include"],
        }),
        "conanfile.py": GenConanfile("app", "1.0").with_require("mytool/1.0"),
    })
    client.run("create tool")
    client.run("install . -g UnrealDeps")
    content = client.load("conan_unreal_deps/ConanDependencies.build.cs")
    direct_targets_section = content.split("DirectTargets =")[1].split("};")[0]
    assert "mytool" not in direct_targets_section
