import os

from jinja2 import Environment, StrictUndefined

from conan.errors import ConanException
from conan.internal import check_duplicated_generator
from conan.internal.model.dependencies import get_transitive_requires
from conan.internal.model.pkg_type import PackageType
from conan.tools.files import save


def _csstr(value):
    """ Escape a value so it can be embedded in a C# double-quoted string literal """
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
            result.append("\\u%04x" % ord(ch))
        else:
            result.append(ch)
    return "".join(result)


def _csarr(items):
    """ Render a list of strings as the contents of a C# array initializer, e.g. ``"a", "b",``
    - a Jinja filter so a whole ``new string[] { ... }`` fits on one line in the template """
    return "".join(f'"{_csstr(item)}", ' for item in items)


def _csdefinearr(defines):
    """ Same as ``_csarr``, for the ``(string, string)[]`` Defines pairs """
    return "".join(f'("{_csstr(name)}", "{_csstr(value)}"), ' for name, value in defines)


class UnrealDeps:
    """
    Generates ``ConanDependencies.build.cs``, a static map of dependency information (include
    dirs, library locations, defines, system libs, frameworks), and
    ``ConanDependenciesSetup.build.cs``, a set of ``ModuleRules`` extension methods to consume
    it from a module's own ``.Build.cs``.

    Every requirable "thing" (a package root, or one of its components) becomes a target keyed
    as ``"pkgname::targetname"``, mirroring ``CMakeConfigDeps`` and ``ZigDeps``: a target only
    carries its own (unmerged) information, and depends on other targets through an explicit
    ``Requires`` list, since ``ModuleRules`` does not propagate this information transitively
    on its own (there is no equivalent of CMake's ``INTERFACE_LINK_LIBRARIES`` here).

    Executables from ``tool_requires`` (and application dependencies) are exposed separately as
    a path map, since there is nothing to link for those.

    Both generated files must end in ``.build.cs`` - not because they define a module (neither
    declares a ``ModuleRules`` subclass), but because that is the only mechanism Unreal Build
    Tool has for pulling extra source into a project's Rules assembly: it discovers every
    ``*.build.cs``/``*.target.cs`` file under a project's ``Source`` folder (recursively) and
    compiles all of them together into one assembly, rather than offering a C# ``#include`` or
    ``import`` a ``.Build.cs`` could use to pull in a helper file directly. Concretely, this
    means the ``generators_folder`` these files land in has to be somewhere under the
    consuming project's ``Source`` directory - e.g. run ``conan install`` with
    ``--output-folder=Source/ThirdParty/Conan`` - or Unreal Build Tool will simply never see
    them, the same way a project has to point CMake at ``conan_toolchain.cmake`` itself.

    One sharp edge in that discovery worth calling out: as soon as Unreal Build Tool finds a
    ``.build.cs`` file directly inside a folder, it stops recursing into that folder's own
    subfolders (a module's own ``Private``/``Public``/``Classes`` subfolders are never meant to
    hide another module). A stray ``.build.cs`` dropped straight into a project's ``Source``
    root - rather than its own subfolder, as every module's ``Build.cs`` should live in - will
    silently make every *other* module under ``Source`` invisible to Unreal Build Tool,
    generated ones from here included. Point ``--output-folder`` at its own dedicated
    subfolder (as in the example above), not at ``Source`` itself.
    """

    def __init__(self, conanfile):
        self._conanfile = conanfile

    def generate(self):
        """
        This method will save the generated files to the ``conanfile.generators_folder`` folder
        """
        self._conanfile.output.warning("UnrealDeps is experimental, and might get "
                                       "breaking changes in future releases",
                                       warn_tag="experimental")
        check_duplicated_generator(self, self._conanfile)
        generator_files = self._content()
        for generator_file, content in generator_files.items():
            save(self._conanfile, os.path.join("conan_unreal_deps", generator_file), content)

    def get_transitive_requires(self, dep):
        # Resolved from the consumer's perspective, as requirement traits (visible,
        # transitive_headers/libs, replace_requires) live on the require edge, not on ``dep``
        return get_transitive_requires(self._conanfile, dep)

    @property
    def _is_windows(self):
        return self._conanfile.settings.get_safe("os") == "Windows"

    def _content(self):
        targets = {}
        exes = {}
        host_req = self._conanfile.dependencies.host
        test_req = self._conanfile.dependencies.test

        for require, dep in list(host_req.items()) + list(test_req.items()):
            full_cpp_info = dep.cpp_info.deduce_full_cpp_info(dep)
            self._add_package_targets(require, dep, full_cpp_info, targets)
            self._add_package_exes(dep, full_cpp_info, exes)
        # tool_requires: nothing to link, but a Build.cs needs to be able to find the
        # executables (e.g. a packaged code generator invoked from a custom build step).
        tool_dirs = {}
        for _, dep in self._conanfile.dependencies.build.items():
            full_cpp_info = dep.cpp_info.deduce_full_cpp_info(dep)
            self._add_package_exes(dep, full_cpp_info, exes)
            bindirs = [d.replace("\\", "/")
                       for d in full_cpp_info.aggregated_components().bindirs]
            if bindirs:
                tool_dirs[dep.ref.name] = bindirs

        # A "Requires" entry can point at something that was deliberately never turned into a
        # target (e.g. an executable-only component - there's nothing to link there). Prune
        # those instead of leaving a dangling reference that would silently resolve to nothing.
        for target in targets.values():
            target["requires"] = [r for r in target["requires"] if r in targets]

        direct_targets = [f"{dep.ref.name}::{dep.ref.name}"
                          for _, dep in self._conanfile.dependencies.direct_host.items()
                          if dep.package_type is not PackageType.APP]
        direct_targets = [t for t in direct_targets if t in targets]

        env = Environment(trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined)
        env.filters["csstr"] = _csstr
        env.filters["csarr"] = _csarr
        env.filters["csdefinearr"] = _csdefinearr
        deps_template = env.from_string(_CONAN_DEPENDENCIES_TEMPLATE)
        context = {"targets": dict(sorted(targets.items())),
                   "exes": dict(sorted(exes.items())),
                   "tool_dirs": dict(sorted(tool_dirs.items())),
                   "direct_targets": direct_targets}
        return {"ConanDependencies.build.cs": deps_template.render(context),
                "ConanDependenciesSetup.build.cs": _CONAN_DEPENDENCIES_SETUP_CS}

    @staticmethod
    def _add_package_exes(dep, full_cpp_info, exes):
        pkg_name = dep.ref.name
        components = full_cpp_info.components if full_cpp_info.has_components \
            else {pkg_name: full_cpp_info}
        for comp_name, info in components.items():
            if info.exe or info.type is PackageType.APP:
                if info.location:
                    exes[f"{pkg_name}::{comp_name}"] = info.location.replace("\\", "/")

    def _add_package_targets(self, require, dep, full_cpp_info, targets):
        pkg_name = dep.ref.name
        has_components = full_cpp_info.has_components
        components = full_cpp_info.components if has_components else {pkg_name: full_cpp_info}

        all_target_names = []
        for comp_name, info in components.items():
            if info.exe or not (info.frameworks or info.package_framework or info.includedirs
                                or info.libs or info.system_libs or info.defines
                                or info.requires):
                continue  # Nothing this target actually contributes
            target_key = f"{pkg_name}::{comp_name}"
            targets[target_key] = self._target_data(require, dep, info, has_components)
            all_target_names.append(target_key)

        root_key = f"{pkg_name}::{pkg_name}"
        if root_key not in targets and all_target_names:
            if full_cpp_info.default_components is not None:
                # A default component may itself have been skipped (exe-only or empty)
                requires = [f"{pkg_name}::{c}" for c in full_cpp_info.default_components]
                requires = [r for r in requires if r in targets]
            else:
                # Every contributing component, not only the ones producing a library: a
                # header-only component still carries includedirs, defines and its own
                # requires, and would otherwise be unreachable from the package root. This is
                # what CMakeConfigDeps' _add_root_lib_target (and ZigDeps) do too.
                requires = all_target_names
            targets[root_key] = self._interface_target(requires)

    @staticmethod
    def _empty_target():
        return {"kind": "Interface", "include_paths": [], "defines": [], "system_libs": [],
                "frameworks": [], "framework_paths": [], "lib": None, "runtime_libs": [],
                "requires": []}

    @classmethod
    def _interface_target(cls, requires):
        result = cls._empty_target()
        result["requires"] = requires
        return result

    def _target_data(self, require, dep, info, has_components):
        result = self._empty_target()
        result["requires"] = self._requires(dep, info, has_components)
        result["frameworks"] = list(info.frameworks)
        result["framework_paths"] = [p.replace("\\", "/") for p in info.frameworkdirs]
        # ``headers`` says whether this consumer may use the dependency's headers at all;
        # when it is False, its include dirs and defines must not leak in
        if require.headers:
            result["include_paths"] = [p.replace("\\", "/") for p in info.includedirs]
            result["defines"] = self._defines(info.defines)
        if info.package_framework:
            # An Apple .framework bundle: link it by name, with its parent as search path -
            # PublicAdditionalFrameworks needs a concrete path per framework, there is no
            # generic search-path list the way PublicSystemIncludePaths is for headers
            path = info.package_framework.replace("\\", "/")
            result["framework_paths"].append(os.path.dirname(path))
            name = os.path.basename(path)
            result["frameworks"].append(name[:-len(".framework")]
                                        if name.endswith(".framework") else name)
        result["system_libs"] = [self._system_lib_name(lib) for lib in info.system_libs]
        if require.libs:
            if info.libs:
                assert info.location, f"{dep}: cpp_info.location missing for {info.libs}"
                is_shared = info.type is PackageType.SHARED
                result["kind"] = "Shared" if is_shared else "Static"
                # ``link_location`` is only set when it differs from ``location`` - on
                # Windows, where a shared library links against its import lib, not the .dll
                result["lib"] = (info.link_location or info.location).replace("\\", "/")
                if is_shared:
                    # Unlike ZigDeps, which can only hand over paths and leaves run-time
                    # loadability to Conan's own conanrun environment, Unreal Build Tool has a
                    # native mechanism for this: RuntimeDependencies makes it copy the shared
                    # library into the staged/packaged build itself.
                    result["runtime_libs"] = self._runtime_dependency_paths(info.location)
        return result

    @staticmethod
    def _runtime_dependency_paths(location):
        """ Every file that needs to ship alongside a shared library for it to actually be
        found at run time - not just ``location`` itself.

        On Unix, a shared library's own install name / SONAME (what a consumer's load command
        actually asks the dynamic linker for) is very often a *different* filename from the
        dev-time one Conan resolves as ``location`` - e.g. ``libz.dylib`` is conventionally a
        symlink to ``libz.1.dylib``, which is itself a symlink to the real ``libz.1.3.2.dylib``,
        and it is ``libz.1.dylib`` (the SONAME) that ends up embedded in a consumer's binary,
        not ``libz.dylib``. ``RuntimeDependencies`` copies files by their literal name, so
        shipping only ``location`` would silently produce a package that fails to load the
        library at run time the moment it's copied anywhere without the rest of that symlink
        chain alongside it (e.g. once actually staged/packaged, away from the Conan cache).
        Shipping every same-target name sidesteps having to know which one is the real SONAME.
        """
        directory = os.path.dirname(location)
        real_path = os.path.realpath(location)
        try:
            candidates = [os.path.join(directory, name) for name in os.listdir(directory)]
        except OSError:
            return [location.replace("\\", "/")]
        siblings = [c for c in candidates if os.path.realpath(c) == real_path]
        return sorted(p.replace("\\", "/") for p in siblings) or [location.replace("\\", "/")]

    def _system_lib_name(self, name):
        # UBT passes this string straight to the linker command line (VCToolChain.cs), so on
        # Windows it must carry the ".lib" extension - which Conan's own convention omits
        if self._is_windows and not name.lower().endswith(".lib"):
            return name + ".lib"
        return name

    @staticmethod
    def _defines(defines):
        # A list of pairs rather than a dict: duplicate names are legal and must not be
        # silently collapsed, and the emitted order is the order the recipe declared
        result = []
        for define in defines:
            if "=" in define:
                name, value = define.split("=", 1)
            else:
                name, value = define, ""
            result.append((name, value))
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
            elif req_pkg != req_comp:
                # Not a component of that package, and not the "pkg::pkg" root form either,
                # so the recipe is referring to something that does not exist
                raise ConanException(f"{dep} cpp_info requires '{req_pkg}::{req_comp}', but "
                                     f"component '{req_comp}' was not found in '{req_pkg}'")
            else:  # It must be the interface pkgname::pkgname target
                result.append(f"{req_name}::{req_name}")
        return result


_CONAN_DEPENDENCIES_TEMPLATE = """\
// Generated by Conan, do not edit manually
//
// This file deliberately has no ModuleRules subclass of its own - only its ".build.cs" name
// matters, since that is what makes Unreal Build Tool compile it into this project's Rules
// assembly alongside every other Build.cs/Target.cs file it finds under Source. It must live
// somewhere under this project's Source folder to be picked up at all.

using System.Collections.Generic;

namespace Conan
{
    public enum ConanTargetKind { Static, Shared, Interface }

    /// <summary>
    /// One requirable "thing" from the Conan dependency graph: a package root (e.g.
    /// "zlib::zlib") or one of its components (e.g. "openssl::ssl"). Carries only its own
    /// (unmerged) information, plus the other targets it Requires.
    /// </summary>
    public class ConanTarget
    {
        public ConanTargetKind Kind = ConanTargetKind.Interface;
        public string[] IncludePaths = System.Array.Empty<string>();
        public (string Name, string Value)[] Defines = System.Array.Empty<(string, string)>();
        public string[] SystemLibraries = System.Array.Empty<string>();
        public string[] Frameworks = System.Array.Empty<string>();
        public string[] FrameworkPaths = System.Array.Empty<string>();
        /// Full path of the static or import library to link, or null for a target that
        /// produces nothing to link (header-only, or a pure interface/aggregate target).
        public string Library = null;
        /// Full paths Unreal Build Tool should copy next to the staged binary for this shared
        /// library to actually be found at run time (empty for anything that isn't one). Not
        /// just one path: a Unix shared library is typically a chain of symlinks - the name a
        /// consumer's load command asks for at run time (its SONAME) is very often not the same
        /// filename as the one linked against - so every same-target name ships, not only the
        /// one ``Library`` points at.
        public string[] RuntimeLibraries = System.Array.Empty<string>();
        public string[] Requires = System.Array.Empty<string>();
    }

    public static class ConanDeps
    {
        /// Every direct (non tool_require, non-App) dependency of this consumer.
        public static readonly string[] DirectTargets =
        {
        {% for name in direct_targets %}
            "{{ name | csstr }}",
        {% endfor %}
        };

        public static readonly Dictionary<string, ConanTarget> Targets = new()
        {
        {% for name, t in targets.items() %}
            ["{{ name | csstr }}"] = new ConanTarget
            {
                Kind = ConanTargetKind.{{ t.kind }},
                IncludePaths = new string[] { {{ t.include_paths | csarr }} },
                Defines = new (string, string)[] { {{ t.defines | csdefinearr }} },
                SystemLibraries = new string[] { {{ t.system_libs | csarr }} },
                Frameworks = new string[] { {{ t.frameworks | csarr }} },
                FrameworkPaths = new string[] { {{ t.framework_paths | csarr }} },
        {% if t.lib %}
                Library = "{{ t.lib | csstr }}",
        {% endif %}
                RuntimeLibraries = new string[] { {{ t.runtime_libs | csarr }} },
                Requires = new string[] { {{ t.requires | csarr }} },
            },
        {% endfor %}
        };

        /// Executables provided by dependencies (tool_requires and application packages), by
        /// "pkg::name". There is nothing to link for these - read the path directly.
        public static readonly Dictionary<string, string> Exes = new()
        {
        {% for name, path in exes.items() %}
            ["{{ name | csstr }}"] = "{{ path | csstr }}",
        {% endfor %}
        };

        /// Directories holding the executables of build-context dependencies (tool_requires),
        /// by package name. Conan exposes tools through PATH; this is the same information, so
        /// a Build.cs can locate one without depending on the ambient environment.
        public static readonly Dictionary<string, string[]> ToolDirs = new()
        {
        {% for name, dirs in tool_dirs.items() %}
            ["{{ name | csstr }}"] = new string[] { {{ dirs | csarr }} },
        {% endfor %}
        };
    }
}
"""

_CONAN_DEPENDENCIES_SETUP_CS = """\
// Generated by Conan, do not edit manually

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using UnrealBuildTool;

namespace Conan
{
    /// <summary>
    /// ``ModuleRules`` extension methods that apply a ``ConanTarget`` (and, transitively,
    /// everything it Requires) to a module - the equivalent of ZigDeps' ``linkDependency`` /
    /// ``linkDependencies``, adapted to the fact that a module only ever has ONE ``ModuleRules``
    /// instance rather than a build graph of separate ``Module`` objects to apply targets to.
    /// </summary>
    public static class ConanDependenciesSetup
    {
        // Targets already applied to a given ModuleRules instance, so linking the same
        // dependency twice - through a diamond in the Requires graph, or through two separate
        // AddConanDependency calls in the same constructor - applies it once. Keyed off the
        // instance itself (via ConditionalWeakTable, not a static Dictionary) so it never
        // outlives - or leaks memory for - the Rules object it was collected for.
        private static readonly ConditionalWeakTable<ModuleRules, HashSet<string>> s_applied
            = new();

        private static HashSet<string> VisitedFor(ModuleRules rules)
            => s_applied.GetOrCreateValue(rules);

        /// <summary>
        /// Applies a single Conan target (a package root, e.g. "zlib::zlib", or one of its
        /// components, e.g. "openssl::ssl") and, transitively, everything it Requires.
        /// </summary>
        public static void AddConanDependency(this ModuleRules rules, string targetName)
        {
            if (!ConanDeps.Targets.ContainsKey(targetName))
            {
                // Fail loudly: a typo here would otherwise surface much later as an unrelated
                // unresolved-symbol error, with nothing pointing back at this call.
                string available = String.Join("\\n  ", ConanDeps.Targets.Keys.OrderBy(k => k));
                throw new Exception(
                    $"Conan: unknown target '{targetName}'. Available targets:\\n  {available}");
            }
            AddConanDependencyVisited(rules, targetName, VisitedFor(rules));
        }

        /// <summary>
        /// Applies every direct dependency declared by the consumer (and, transitively,
        /// everything they Require).
        /// </summary>
        public static void AddConanDependencies(this ModuleRules rules)
        {
            HashSet<string> visited = VisitedFor(rules);
            foreach (string name in ConanDeps.DirectTargets)
            {
                AddConanDependencyVisited(rules, name, visited);
            }
        }

        private static void AddConanDependencyVisited(
            ModuleRules rules, string targetName, HashSet<string> visited)
        {
            // A "Requires" cycle isn't something Conan validates (unlike the package graph
            // itself), so guard against it here rather than risk unbounded recursion.
            if (!visited.Add(targetName)) return;
            if (!ConanDeps.Targets.TryGetValue(targetName, out ConanTarget target)) return;
            ApplyTarget(rules, target);
            foreach (string required in target.Requires)
            {
                AddConanDependencyVisited(rules, required, visited);
            }
        }

        private static void ApplyTarget(ModuleRules rules, ConanTarget target)
        {
            // PublicSystemIncludePaths, not PublicIncludePaths: these are stable third-party
            // headers, not checked when resolving this module's own header dependencies.
            rules.PublicSystemIncludePaths.AddRange(target.IncludePaths);
            foreach ((string name, string value) in target.Defines)
            {
                rules.PublicDefinitions.Add(value.Length > 0 ? $"{name}={value}" : name);
            }
            rules.PublicSystemLibraries.AddRange(target.SystemLibraries);
            if (target.Library != null)
            {
                rules.PublicAdditionalLibraries.Add(target.Library);
            }
            foreach (string runtimeLibrary in target.RuntimeLibraries)
            {
                rules.RuntimeDependencies.Add(runtimeLibrary);
            }
            foreach (string name in target.Frameworks)
            {
                // A framework path was recorded alongside this name (from cpp_info.frameworkdirs
                // or a package_framework) - resolve "<path>/<name>.framework" against each one
                // and link that concrete bundle. Nothing resolves: assume it is an OS-provided
                // system framework instead, which PublicFrameworks finds on the SDK's own
                // search paths without needing a path at all.
                string resolved = target.FrameworkPaths
                    .Select(dir => Path.Combine(dir, name + ".framework"))
                    .FirstOrDefault(Directory.Exists);
                if (resolved != null)
                {
                    rules.PublicAdditionalFrameworks.Add(new ModuleRules.Framework(name, resolved));
                }
                else
                {
                    rules.PublicFrameworks.Add(name);
                }
            }
        }
    }
}
"""
