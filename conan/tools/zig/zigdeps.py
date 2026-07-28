import os

from jinja2 import Environment, StrictUndefined

from conan.internal import check_duplicated_generator
from conan.internal.model.dependencies import get_transitive_requires
from conan.internal.model.pkg_type import PackageType
from conan.tools.files import save


def _zigstr(value):
    """ Escape a value so it can be embedded in a Zig double-quoted string literal """
    result = []
    for ch in str(value):
        if ch == "\\":
            result.append("\\\\")
        elif ch == '"':
            result.append('\\"')
        elif ch == "\n":
            result.append("\\n")
        elif ch == "\r":
            result.append("\\r")
        elif ch == "\t":
            result.append("\\t")
        elif ord(ch) < 0x20:
            result.append("\\x%02x" % ord(ch))
        else:
            result.append(ch)
    return "".join(result)


class ZigDeps:
    """
    Generates ``conan_deps.zig``, a comptime map of dependency information (include dirs,
    library locations, defines, system libs, frameworks), and ``conan_setup.zig``, a set of
    helper functions to consume it from a user ``build.zig``.

    Every requirable "thing" (a package root, or one of its components) becomes a target keyed
    as ``"pkgname::targetname"``, mirroring ``CMakeConfigDeps``: a target only carries its own
    (unmerged) information, and depends on other targets through an explicit ``requires`` list,
    since Zig's build system does not propagate this information transitively on its own.

    This covers build time only. Making a shared dependency loadable at run time is left to
    Conan's own ``conanrun`` environment (or a deployer) rather than handled here - see the
    note in the generated ``conan_setup.zig``.
    """

    def __init__(self, conanfile):
        self._conanfile = conanfile

    def generate(self):
        """
        This method will save the generated files to the ``conanfile.generators_folder`` folder
        """
        self._conanfile.output.warning("ZigDeps is experimental, and might get "
                                       "breaking changes in future releases",
                                       warn_tag="experimental")
        check_duplicated_generator(self, self._conanfile)
        generator_files = self._content()
        for generator_file, content in generator_files.items():
            save(self._conanfile, os.path.join("conan_zig_deps", generator_file), content)

    def get_transitive_requires(self, dep):
        # Resolved from the consumer's perspective, as requirement traits (visible,
        # transitive_headers/libs, replace_requires) live on the require edge, not on ``dep``
        return get_transitive_requires(self._conanfile, dep)

    def _content(self):
        targets = {}
        for _, dep in self._conanfile.dependencies.host.items():
            self._add_package_targets(dep, targets)

        # A "requires" entry can point at something that was deliberately never turned into a
        # target (e.g. an executable-only component/package - there's nothing to link there).
        # Prune those instead of leaving a dangling reference: linkDependency()'s lookup would
        # otherwise silently no-op on it, which is harmless, but only by accident.
        for target in targets.values():
            target["requires"] = [r for r in target["requires"] if r in targets]

        direct_targets = [f"{dep.ref.name}::{dep.ref.name}"
                          for _, dep in self._conanfile.dependencies.direct_host.items()]
        direct_targets = [t for t in direct_targets if t in targets]

        env = Environment(trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined)
        env.filters["zigstr"] = _zigstr
        deps_template = env.from_string(_CONAN_DEPS_TEMPLATE)
        context = {"targets": dict(sorted(targets.items())), "direct_targets": direct_targets}
        return {"conan_deps.zig": deps_template.render(context),
                "conan_setup.zig": _CONAN_SETUP_ZIG}

    def _add_package_targets(self, dep, targets):
        full_cpp_info = dep.cpp_info.deduce_full_cpp_info(dep)
        pkg_name = dep.ref.name
        has_components = full_cpp_info.has_components
        components = full_cpp_info.components if has_components else {pkg_name: full_cpp_info}

        all_target_names = []
        for comp_name, info in components.items():
            if info.exe or not (info.frameworks or info.includedirs or info.libs
                                or info.system_libs or info.defines or info.requires):
                continue  # Nothing this target actually contributes
            target_key = f"{pkg_name}::{comp_name}"
            targets[target_key] = self._target_data(dep, info, has_components)
            all_target_names.append(target_key)

        root_key = f"{pkg_name}::{pkg_name}"
        if root_key not in targets and all_target_names:
            if full_cpp_info.default_components is not None:
                requires = [f"{pkg_name}::{c}" for c in full_cpp_info.default_components]
            else:
                # Every contributing component, not only the ones producing a library:
                # a header-only component still carries includedirs, defines and its own
                # requires, and would otherwise be unreachable from the package root.
                # This is what CMakeConfigDeps' _add_root_lib_target does too.
                requires = all_target_names
            targets[root_key] = self._interface_target(requires)

    @staticmethod
    def _interface_target(requires):
        return {"type": "INTERFACE", "include_paths": [], "defines": {}, "system_libs": [],
                "frameworks": [], "lib": None, "link_cpp": False, "requires": requires}

    def _target_data(self, dep, info, has_components):
        result = {
            "type": "INTERFACE",
            "include_paths": list(info.includedirs),
            "defines": self._defines(info.defines),
            "system_libs": list(info.system_libs),
            "frameworks": list(info.frameworks),
            "lib": None,
            "link_cpp": False,
            "requires": self._requires(dep, info, has_components),
        }
        if info.libs:
            assert info.location, f"{dep}: cpp_info.location missing for library {info.libs}"
            is_shared = info.type is PackageType.SHARED
            # ``link_location`` is only set when it differs from ``location`` - on Windows, where
            # a shared library links against its import lib rather than the runtime .dll
            link_path = info.link_location or info.location
            result["type"] = "SHARED" if is_shared else "STATIC"
            result["lib"] = link_path.replace("\\", "/")
            # A C++ dependency needs the C++ runtime linked into the consumer, or it fails
            # with undefined std:: symbols. Same source CMakeConfigDeps uses for its
            # link_languages, and the same component-then-package fallback.
            languages = info.languages or dep.languages or []
            result["link_cpp"] = "C++" in languages
        return result

    @staticmethod
    def _defines(defines):
        result = {}
        for define in defines:
            if "=" in define:
                name, value = define.split("=", 1)
            else:
                name, value = define, "1"
            result[name] = value
        return result

    def _requires(self, dep, info, has_components):
        requires = info.parsed_requires()
        pkg_name = dep.ref.name
        transitive_reqs = self.get_transitive_requires(dep)

        if not requires and not has_components:
            # No explicit requires: link against all of this package's own direct dependencies
            return [f"{d.ref.name}::{d.ref.name}" for d in transitive_reqs.values()
                    if d.package_type is not PackageType.APP]

        result = []
        for req_pkg, req_comp in requires:
            if req_pkg is None:  # Points to a component of the same package
                result.append(f"{pkg_name}::{req_comp}")
                continue
            try:
                _, req_dep = transitive_reqs.of(req_pkg)
            except KeyError:
                continue  # The transitive dep might have been skipped
            if req_dep.package_type is PackageType.APP:
                continue  # It doesn't make sense to link a package that is an App
            # Key off the *resolved* dependency, not the name the recipe wrote: under
            # ``replace_requires`` those differ, and targets are always created from the
            # resolved name, so using req_pkg here would dangle (and then be pruned away)
            req_name = req_dep.ref.name
            if req_dep.cpp_info.components.get(req_comp) is not None:
                result.append(f"{req_name}::{req_comp}")
            else:  # It must be the interface pkgname::pkgname target
                result.append(f"{req_name}::{req_name}")
        return result


_CONAN_DEPS_TEMPLATE = """\
// Generated by Conan, do not edit manually

const std = @import("std");

pub const Define = struct {
    name: []const u8,
    value: []const u8,
};

pub const TargetKind = enum { STATIC, SHARED, INTERFACE };

pub const Target = struct {
    kind: TargetKind,
    include_paths: []const []const u8,
    defines: []const Define,
    system_libs: []const []const u8,
    frameworks: []const []const u8,
    /// Path of the library to link, if this target produces one. For a shared library on
    /// Windows this is the import library, not the runtime .dll.
    lib: ?[]const u8,
    /// Whether this target is C++, and therefore needs the C++ runtime linked in.
    link_cpp: bool,
    requires: []const []const u8,
};

pub const direct_targets: []const []const u8 = &.{
{% for name in direct_targets %}
    "{{ name | zigstr }}",
{% endfor %}
};

pub const conan_targets = std.StaticStringMap(Target).initComptime(.{
{% for name, t in targets.items() %}
    .{ "{{ name | zigstr }}", Target{
        .kind = .{{ t.type }},
        .include_paths = &.{ {% for p in t.include_paths %}"{{ p | zigstr }}", {% endfor %} },
        .defines = &.{
{% for dname, dvalue in t.defines.items() %}
            .{ .name = "{{ dname | zigstr }}", .value = "{{ dvalue | zigstr }}" },
{% endfor %}
        },
        .system_libs = &.{ {% for l in t.system_libs %}"{{ l | zigstr }}", {% endfor %} },
        .frameworks = &.{ {% for f in t.frameworks %}"{{ f | zigstr }}", {% endfor %} },
{% if t.lib %}
        .lib = "{{ t.lib | zigstr }}",
{% else %}
        .lib = null,
{% endif %}
        .link_cpp = {{ "true" if t.link_cpp else "false" }},
        .requires = &.{ {% for r in t.requires %}"{{ r | zigstr }}", {% endfor %} },
    } },
{% endfor %}
});
"""

_CONAN_SETUP_ZIG = """\
// Generated by Conan, do not edit manually

const std = @import("std");
const conan_deps = @import("conan_deps.zig");

fn linkTarget(step: *std.Build.Step.Compile, target: conan_deps.Target) void {
    const module = step.root_module;
    for (target.include_paths) |path| {
        module.addIncludePath(.{ .cwd_relative = path });
    }
    for (target.defines) |define| {
        module.addCMacro(define.name, define.value);
    }
    for (target.system_libs) |lib| {
        // Conan already resolved exactly what to link, so don't let Zig second-guess it
        // through pkg-config (which it would do by default, use_pkg_config = .yes).
        module.linkSystemLibrary(lib, .{ .use_pkg_config = .no });
    }
    for (target.frameworks) |framework| {
        module.linkFramework(framework, .{});
    }
    if (target.lib) |lib| {
        module.addObjectFile(.{ .cwd_relative = lib });
    }
    if (target.link_cpp) {
        module.link_libcpp = true;
    }
}

// NOTE ON RUNTIME DISCOVERY
// This only makes dependencies available at *build* time. Making a shared dependency
// loadable at *run* time is deliberately left to Conan rather than handled here:
// activate the "conanrun" environment Conan generates for exactly this purpose (it sets
// PATH on Windows and (DY)LD_LIBRARY_PATH elsewhere, from every dependency's directories),
// e.g. `self.run("zig build run", env="conanrun")` from a recipe, or by sourcing the
// generated conanrun script directly. `conan install ... --deploy=runtime_deploy` is the
// other option, placing the runtime artifacts in one folder at install time.
//
// Watch out: VirtualRunEnv decides whether to export the library-path variables at all by
// looking at settings.os, so a consumer recipe that declares no `settings` gets a silently
// empty conanrun environment and the libraries stay unfindable.
//
// This differs from a CMake-based consumer, where CMake adds an rpath to build-tree
// binaries by itself, so they run without conanrun (it strips that rpath again on install,
// so installed binaries need the environment either way). Zig has no equivalent behaviour.
// Reproducing it here - emitting rpaths, or copying .dlls next to the executable - was
// intentionally left out of this first version: it duplicates what conanrun already does,
// and neither mechanism has a single obviously-correct form across the platforms Conan
// supports. If real usage shows the environment is not enough, this is the place to
// revisit.

fn linkDependencyVisited(
    step: *std.Build.Step.Compile,
    target_name: []const u8,
    visited: *std.StringHashMap(void),
) void {
    // A "requires" cycle isn't something Conan validates (unlike the package graph itself),
    // so guard against it here rather than risk an unbounded recursion / stack overflow. This
    // also means a diamond dependency only gets applied once, instead of once per path to it.
    if (visited.contains(target_name)) return;
    visited.put(target_name, {}) catch @panic("OOM");
    const target = conan_deps.conan_targets.get(target_name) orelse return;
    linkTarget(step, target);
    for (target.requires) |req_name| {
        linkDependencyVisited(step, req_name, visited);
    }
}

/// Links a single Conan target (a package root, e.g. "zlib::zlib", or one of its
/// components, e.g. "openssl::ssl") and, transitively, everything it requires.
pub fn linkDependency(step: *std.Build.Step.Compile, target_name: []const u8) void {
    var visited = std.StringHashMap(void).init(step.step.owner.allocator);
    defer visited.deinit();
    linkDependencyVisited(step, target_name, &visited);
}

/// Links every direct dependency declared by the consumer (and, transitively, everything
/// they require).
pub fn linkDependencies(step: *std.Build.Step.Compile) void {
    var visited = std.StringHashMap(void).init(step.step.owner.allocator);
    defer visited.deinit();
    for (conan_deps.direct_targets) |name| {
        linkDependencyVisited(step, name, &visited);
    }
}
"""
