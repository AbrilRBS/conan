import os

from jinja2 import Environment, StrictUndefined

from conan.internal import check_duplicated_generator
from conan.internal.model.dependencies import get_transitive_requires
from conan.internal.model.pkg_type import PackageType
from conan.tools.files import save


def _zigstr(value):
    """ Escape a value so it can be embedded in a Zig double-quoted string literal """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


class ZigDeps:
    """
    Generates ``conan_deps.zig``, a comptime map of dependency information (include dirs,
    library locations, defines, system libs, frameworks), and ``conan_setup.zig``, a set of
    helper functions to consume it from a user ``build.zig``.

    Every requirable "thing" (a package root, or one of its components) becomes a target keyed
    as ``"pkgname::targetname"``, mirroring ``CMakeConfigDeps``: a target only carries its own
    (unmerged) information, and depends on other targets through an explicit ``requires`` list,
    since Zig's build system does not propagate this information transitively on its own.
    """

    def __init__(self, conanfile):
        self._conanfile = conanfile

    def generate(self):
        """
        This method will save the generated files to the ``conanfile.generators_folder`` folder
        """
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

        direct_targets = [f"{dep.ref.name}::{dep.ref.name}"
                          for _, dep in self._conanfile.dependencies.direct_host.items()]

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

        lib_target_names = []
        for comp_name, info in components.items():
            if info.exe or not (info.frameworks or info.includedirs or info.libs
                                or info.system_libs or info.defines or info.requires):
                continue  # Nothing this target actually contributes
            target_key = f"{pkg_name}::{comp_name}"
            targets[target_key] = self._target_data(dep, info, has_components)
            if info.libs:
                lib_target_names.append(target_key)

        root_key = f"{pkg_name}::{pkg_name}"
        if root_key not in targets and lib_target_names:
            if full_cpp_info.default_components is not None:
                requires = [f"{pkg_name}::{c}" for c in full_cpp_info.default_components]
            else:
                requires = lib_target_names
            targets[root_key] = self._interface_target(requires)

    @staticmethod
    def _interface_target(requires):
        return {"type": "INTERFACE", "include_paths": [], "defines": {}, "system_libs": [],
                "frameworks": [], "lib": None, "requires": requires}

    def _target_data(self, dep, info, has_components):
        result = {
            "type": "INTERFACE",
            "include_paths": list(info.includedirs),
            "defines": self._defines(info.defines),
            "system_libs": list(info.system_libs),
            "frameworks": list(info.frameworks),
            "lib": None,
            "requires": self._requires(dep, info, has_components),
        }
        if info.libs:
            assert info.location, f"{dep}: cpp_info.location missing for library {info.libs}"
            is_shared = info.type is PackageType.SHARED
            link_path = info.link_location or info.location
            needs_rpath = is_shared and not info.location.endswith(".dll")
            # Windows has no rpath equivalent: a shared lib's runtime .dll is a different
            # file than the .lib it links against, and must be deployed next to the
            # consumer's executable to be found at all. Expose it so the generated glue can.
            needs_runtime_path = is_shared and info.link_location and \
                info.link_location != info.location
            result["type"] = "SHARED" if is_shared else "STATIC"
            result["lib"] = {
                "path": link_path.replace("\\", "/"),
                "rpath_dir": os.path.dirname(info.location).replace("\\", "/")
                if needs_rpath else None,
                "runtime_path": info.location.replace("\\", "/") if needs_runtime_path else None,
            }
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
            if req_dep.cpp_info.components.get(req_comp) is not None:
                result.append(f"{req_pkg}::{req_comp}")
            else:  # It must be the interface pkgname::pkgname target
                result.append(f"{req_pkg}::{req_pkg}")
        return result


_CONAN_DEPS_TEMPLATE = """\
// Generated by Conan, do not edit manually

const std = @import("std");

pub const Lib = struct {
    path: []const u8,
    rpath_dir: ?[]const u8,
    runtime_path: ?[]const u8,
};

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
    lib: ?Lib,
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
        .lib = Lib{
            .path = "{{ t.lib.path | zigstr }}",
{% if t.lib.rpath_dir %}
            .rpath_dir = "{{ t.lib.rpath_dir | zigstr }}",
{% else %}
            .rpath_dir = null,
{% endif %}
{% if t.lib.runtime_path %}
            .runtime_path = "{{ t.lib.runtime_path | zigstr }}",
{% else %}
            .runtime_path = null,
{% endif %}
        },
{% else %}
        .lib = null,
{% endif %}
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
        module.linkSystemLibrary(lib, .{});
    }
    for (target.frameworks) |framework| {
        module.linkFramework(framework, .{});
    }
    if (target.lib) |lib| {
        module.addObjectFile(.{ .cwd_relative = lib.path });
        if (lib.rpath_dir) |rpath_dir| {
            module.addRPath(.{ .cwd_relative = rpath_dir });
        }
        if (lib.runtime_path) |runtime_path| {
            // No rpath equivalent on Windows: deploy the .dll next to the executable
            // (matching where std.Build installs it) or it won't be found at runtime.
            const b = step.step.owner;
            const basename = std.fs.path.basename(runtime_path);
            const install_dll = b.addInstallFileWithDir(
                .{ .cwd_relative = runtime_path }, .bin, basename);
            b.getInstallStep().dependOn(&install_dll.step);
        }
    }
}

/// Links a single Conan target (a package root, e.g. "zlib::zlib", or one of its
/// components, e.g. "openssl::ssl") and, transitively, everything it requires.
pub fn linkDependency(step: *std.Build.Step.Compile, target_name: []const u8) void {
    const target = conan_deps.conan_targets.get(target_name) orelse return;
    linkTarget(step, target);
    for (target.requires) |req_name| {
        linkDependency(step, req_name);
    }
}

/// Links every direct dependency declared by the consumer (and, transitively, everything
/// they require).
pub fn linkDependencies(step: *std.Build.Step.Compile) void {
    for (conan_deps.direct_targets) |name| {
        linkDependency(step, name);
    }
}
"""
