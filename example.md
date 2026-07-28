# ZigDeps by example

Four worked examples of consuming Conan packages from a Zig `build.zig`, using the
experimental `ZigDeps` generator. Every one of these was built and run end to end against
real ConanCenter packages — see [Verification](#verification) for versions and logs.

> `ZigDeps` is experimental. The shape of the generated Zig API is expected to change.

**Contents**

1. [A C library — OpenSSL](#1-a-c-library--openssl) — components, transitive dependencies, static and shared
2. [A C++ library — pugixml + nlohmann_json](#2-a-c-library--pugixml--nlohmann_json) — compiled and header-only, C++ runtime
3. [A build tool — flex](#3-a-build-tool--flex) — `tool_requires`, code generation
4. [Tests — zlib + cmocka](#4-tests--zlib--cmocka) — `test_requires`, Zig-native and C test suites

---

## How it works

`conan install . -g ZigDeps` writes two files into a `conan_zig_deps/` folder:

| File | What it is |
| --- | --- |
| `conan_deps.zig` | Data. A `comptime` map of every dependency: include dirs, library paths, defines, system libs, frameworks, and each target's own `requires` list. |
| `conan_setup.zig` | Behaviour. Helpers your `build.zig` calls to push that data into a module. |

Zig has no native format for describing a prebuilt C library, and does not propagate
include or library paths through `linkLibrary()`, so there is nothing to emit *into* — the
generator emits Zig source that your build imports instead.

Everything is keyed `"package::target"`, the same vocabulary CMake users know:
`openssl::ssl` is a component, `openssl::openssl` is the package root.

```zig
const conan = @import("conan_zig_deps/conan_setup.zig");

conan.linkDependencies(mod);                  // every direct dependency
conan.linkDependency(mod, "openssl::crypto"); // …or one specific target
conan.toolPath(b, "flex", "flex");            // a tool_requires executable
```

The helpers take a `*std.Build.Module`, not a `*std.Build.Step.Compile`. In Zig 0.16 every
call they make exists only on `Module`, and a module is not necessarily an artifact's root —
so this also works for test modules and shared modules.

---

## 1. A C library — OpenSSL

Shows **component-level linking** and **transitive resolution**. OpenSSL's `crypto`
component depends on `zlib`, and `ssl` depends on `crypto`, so naming one target pulls the
rest in automatically.

**`conanfile.txt`**

```ini
[requires]
openssl/3.5.4

[generators]
ZigDeps
```

**`main.c`**

```c
#include <stdio.h>
#include <string.h>
#include <openssl/evp.h>
#include <openssl/crypto.h>

int main(void) {
    const char *msg = "conan + zig";
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int len = 0;

    EVP_MD_CTX *ctx = EVP_MD_CTX_new();
    EVP_DigestInit_ex(ctx, EVP_sha256(), NULL);
    EVP_DigestUpdate(ctx, msg, strlen(msg));
    EVP_DigestFinal_ex(ctx, digest, &len);
    EVP_MD_CTX_free(ctx);

    printf("%s\n", OpenSSL_version(OPENSSL_VERSION));
    printf("sha256(\"%s\") = ", msg);
    for (unsigned int i = 0; i < len; i++) printf("%02x", digest[i]);
    printf("\n");
    return 0;
}
```

**`build.zig`**

```zig
const std = @import("std");
const conan = @import("conan_zig_deps/conan_setup.zig");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const mod = b.createModule(.{ .target = target, .optimize = optimize });
    mod.addCSourceFile(.{ .file = b.path("main.c"), .flags = &.{} });
    mod.link_libc = true;

    // Only the "crypto" component is needed. Its own requires - openssl::crypto ->
    // zlib::zlib - are followed automatically, so zlib is linked without naming it.
    conan.linkDependency(mod, "openssl::crypto");

    const exe = b.addExecutable(.{ .name = "digest", .root_module = mod });
    b.installArtifact(exe);

    const run = b.addRunArtifact(exe);
    b.step("run", "Run the example").dependOn(&run.step);
}
```

```bash
conan install . -of . --build=missing
zig build run
```

```
OpenSSL 3.5.4 30 Sep 2025
sha256("conan + zig") = c69d96afb1f7a8ea85d27a29245f1a31bb3e0026de72f0e4f762ad93fac142e6
```

The generated graph, with no `build.zig` changes needed to walk it:

```
openssl::ssl ──> openssl::crypto ──> zlib::zlib
```

### The same thing, shared

```bash
conan install . -of . -o "openssl/*:shared=True" -o "zlib/*:shared=True" --build=missing
source ./conanrun.sh          # <- required
zig build run
```

`ZigDeps` covers **build time only**. It deliberately emits no rpaths and copies no
libraries, because Conan already solves runtime discovery with `conanrun` (which sets
`PATH` on Windows and `(DY)LD_LIBRARY_PATH` elsewhere). Skipping the activation gives:

```
dyld[49473]: Library not loaded: @rpath/libcrypto.3.dylib
```

which is the expected, documented behaviour — not a bug. From a recipe, the equivalent is
`self.run("zig build run", env="conanrun")`.

> **Watch out:** `VirtualRunEnv` decides whether to export the library-path variables at all
> by looking at `settings.os`. A consumer recipe that declares no `settings` gets a silently
> **empty** `conanrun` environment, and shared dependencies stay unfindable with no warning.

---

## 2. A C++ library — pugixml + nlohmann_json

Shows the **C++ runtime** being linked automatically, and the difference between a
**compiled** dependency and a **header-only** one. `pugixml` becomes a `.static` (or
`.shared`) target; `nlohmann_json` becomes an `.interface` target with no library at all.

**`conanfile.txt`**

```ini
[requires]
pugixml/1.14
nlohmann_json/3.11.3

[generators]
ZigDeps
```

**`main.cpp`**

```cpp
#include <pugixml.hpp>
#include <nlohmann/json.hpp>
#include <iostream>

int main() {
    const char *xml = R"(<deps><dep name="pugixml" kind="compiled"/>)"
                      R"(<dep name="nlohmann_json" kind="header-only"/></deps>)";

    pugi::xml_document doc;
    if (!doc.load_string(xml)) { std::cerr << "parse failed\n"; return 1; }

    nlohmann::json out = nlohmann::json::array();
    for (auto dep : doc.child("deps").children("dep")) {
        out.push_back({{"name", dep.attribute("name").as_string()},
                       {"kind", dep.attribute("kind").as_string()}});
    }
    std::cout << out.dump(2) << std::endl;
    return 0;
}
```

**`build.zig`**

```zig
const std = @import("std");
const conan = @import("conan_zig_deps/conan_setup.zig");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    const mod = b.createModule(.{ .target = target, .optimize = optimize });
    mod.addCSourceFile(.{ .file = b.path("main.cpp"), .flags = &.{"-std=c++17"} });

    // Neither link_libc nor link_libcpp is set here: ZigDeps knows both packages are C++
    // and requests the runtime itself.
    conan.linkDependencies(mod);

    const exe = b.addExecutable(.{ .name = "xml2json", .root_module = mod });
    b.installArtifact(exe);

    const run = b.addRunArtifact(exe);
    b.step("run", "Run the example").dependOn(&run.step);
}
```

```bash
conan install . -of . --build=missing
zig build run
```

```json
[
  { "kind": "compiled",    "name": "pugixml" },
  { "kind": "header-only", "name": "nlohmann_json" }
]
```

Add `-o "pugixml/*:shared=True"` for the shared build (and `source ./conanrun.sh` to run it);
`nlohmann_json` is unaffected, since a header-only package has nothing to make shared.

### How C++ is detected

A dependency's `languages` attribute is authoritative when the recipe sets it — a package
declaring `languages = "C"` never gets the C++ runtime. Most recipes still leave it unset,
so `ZigDeps` falls back to `compiler.libcxx`: Conan only keeps that setting for C++
packages, since a C recipe drops it with `settings.rm_safe("compiler.libcxx")`.

That fallback is not exhaustive. A **header-only C++ package that clears its settings**
looks identical to a C one, so set `mod.link_libcpp = true` yourself for those. CMake
consumers have the same gap, and resolve it the same way — through the consumer's own
`project(... CXX)` declaration.

### Troubleshooting: headers that rely on transitive includes

Some C++ libraries compile with Apple's or GNU's libc++ but not Zig's, because they lean on
an include they never asked for. `fmt` is one: `fmt/format.h` calls `malloc` and `free`
without including `<cstdlib>`, which fails under Zig's bundled libc++ with:

```
error: use of undeclared identifier 'malloc'
```

This is a property of the library, not of `ZigDeps`. Force the include from the consumer:

```zig
mod.addCSourceFile(.{ .file = b.path("main.cpp"),
                      .flags = &.{ "-std=c++17", "-include", "cstdlib" } });
```

---

## 3. A build tool — flex

Shows `tool_requires`: a dependency in the **build context**, where there is nothing to link
and the only thing you want is the path to a program. Here `flex` generates a C lexer at
build time, which Zig then compiles.

**`conanfile.txt`**

```ini
[tool_requires]
flex/2.6.4

[generators]
ZigDeps
```

**`counter.l`**

```lex
%option noyywrap nounput noinput
%{
#include <stdio.h>
int words = 0, numbers = 0;
%}
%%
[0-9]+      { numbers++; }
[a-zA-Z]+   { words++; }
.|\n        { /* skip */ }
%%
int main(void) {
    yylex();
    printf("words=%d numbers=%d\n", words, numbers);
    return 0;
}
```

**`build.zig`**

```zig
const std = @import("std");
const conan = @import("conan_zig_deps/conan_setup.zig");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // flex comes from [tool_requires], so it lives in the *build* context: there is nothing
    // to link, only a program to run.
    const flex = b.addSystemCommand(&.{ conan.toolPath(b, "flex", "flex"), "-o" });
    const lexer_c = flex.addOutputFileArg("counter.c");
    flex.addFileArg(b.path("counter.l"));

    const mod = b.createModule(.{ .target = target, .optimize = optimize });
    mod.addCSourceFile(.{ .file = lexer_c, .flags = &.{} });
    mod.link_libc = true;

    const exe = b.addExecutable(.{ .name = "counter", .root_module = mod });
    b.installArtifact(exe);

    const run = b.addRunArtifact(exe);
    run.setStdIn(.{ .bytes = "conan 2 zig 16 rules 42\n" });
    b.step("run", "Generate the lexer with flex, then build and run it").dependOn(&run.step);
}
```

```bash
conan install . -of . --build=missing
zig build run
```

```
words=3 numbers=3
```

### `toolPath()` versus PATH

Conan's normal way of exposing a tool is the **build environment**: `VirtualBuildEnv` puts
every `tool_requires` bindir on `PATH`, so this also works —

```bash
source ./conanbuild.sh    # flex's bindir is now first on PATH
zig build run             # with b.addSystemCommand(&.{"flex", ...})
```

Both are valid. The difference is what happens when the environment is *not* active, which
is the common case when someone just runs `zig build` in a shell. On a machine with a system
`flex` — macOS ships `/usr/bin/flex` — a bare `"flex"` silently resolves to that one
instead, with no error and a near-identical version string.

`toolPath()` resolves inside the package's own bindir, so the build does not depend on
whether the environment happens to be active. Use `"flex"` plus `conanbuild` if you prefer
the environment-driven route; use `toolPath()` if you want the build to be self-contained.

Note that `conan_tool_dirs` includes the tool's *own* transitive tools, so this example also
exposes `m4`, which `flex` requires.

---

## 4. Tests — zlib + cmocka

**Yes — you can test C/C++ Conan libraries from Zig**, in two different ways, and this
example does both in one `zig build test`.

`cmocka` is declared under `[test_requires]`. It becomes a real target in `conan_deps.zig`,
but is deliberately **excluded from `direct_targets`**, so `linkDependencies()` never drags
a test framework into the application. You name it explicitly, only in the test binary.

**`conanfile.txt`**

```ini
[requires]
zlib/1.3.1

[test_requires]
cmocka/1.1.7

[generators]
ZigDeps
```

### Zig-native tests over a C library

**`test_zlib.zig`**

```zig
const std = @import("std");
const c = @cImport({
    @cInclude("zlib.h");
});

test "crc32 of a known string" {
    const data = "conan + zig";
    const crc = c.crc32(0, data.ptr, @intCast(data.len));
    try std.testing.expect(crc != 0);
    try std.testing.expectEqual(crc, c.crc32(0, data.ptr, @intCast(data.len)));
}

test "compressBound grows with input size" {
    try std.testing.expect(c.compressBound(1000) > c.compressBound(10));
}

test "zlib version is the one Conan resolved" {
    const v = std.mem.span(c.zlibVersion());
    try std.testing.expect(std.mem.startsWith(u8, v, "1.3"));
}
```

`@cImport` works because `linkDependencies()` puts zlib's include directories on the test
module before the import is translated.

### A C test suite driven by cmocka

**`test_zlib_cmocka.c`**

```c
#include <stdarg.h>
#include <stddef.h>
#include <setjmp.h>
#include <cmocka.h>
#include <zlib.h>
#include <string.h>

static void test_crc32_is_stable(void **state) {
    (void)state;
    const char *msg = "conan + zig";
    uLong a = crc32(0L, (const Bytef *)msg, (uInt)strlen(msg));
    uLong b = crc32(0L, (const Bytef *)msg, (uInt)strlen(msg));
    assert_int_equal(a, b);
}

int main(void) {
    const struct CMUnitTest tests[] = { cmocka_unit_test(test_crc32_is_stable) };
    return cmocka_run_group_tests(tests, NULL, NULL);
}
```

**`build.zig`**

```zig
const std = @import("std");
const conan = @import("conan_zig_deps/conan_setup.zig");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});
    const test_step = b.step("test", "Run both test suites");

    // 1. Native Zig tests using the C dependency through @cImport.
    const zig_test_mod = b.createModule(.{
        .root_source_file = b.path("test_zlib.zig"),
        .target = target,
        .optimize = optimize,
    });
    conan.linkDependencies(zig_test_mod);
    const zig_tests = b.addTest(.{ .root_module = zig_test_mod });
    test_step.dependOn(&b.addRunArtifact(zig_tests).step);

    // 2. A C suite driven by cmocka, a [test_requires]: a real target, but not in
    //    direct_targets, so it never reaches the application. Name it explicitly.
    const c_test_mod = b.createModule(.{ .target = target, .optimize = optimize });
    c_test_mod.addCSourceFile(.{ .file = b.path("test_zlib_cmocka.c"), .flags = &.{} });
    conan.linkDependency(c_test_mod, "zlib::zlib");
    conan.linkDependency(c_test_mod, "cmocka::cmocka");
    const c_tests = b.addExecutable(.{ .name = "cmocka_tests", .root_module = c_test_mod });
    test_step.dependOn(&b.addRunArtifact(c_tests).step);
}
```

```bash
conan install . -of . --build=missing
zig build test --summary all
```

```
[==========] tests: Running 2 test(s).
[ RUN      ] test_crc32_is_stable
[       OK ] test_crc32_is_stable
[==========] tests: 2 test(s) run.
[  PASSED  ] 2 test(s).
Build Summary: 5/5 steps succeeded; 3/3 tests passed
```

Zig's own test runner prints nothing on success, which is why `--summary all` is used above
to show the 3 Zig tests alongside cmocka's output.

---

## Known limitations

| Limitation | Why |
| --- | --- |
| A dependency's `cflags` / `cxxflags` / link flags are **not applied** | Zig has no module-level flag injection — `Module.addCSourceFile` only applies flags to files added through it. They are emitted in `conan_deps.zig` and Conan warns when a dependency declares any, so pass them yourself. |
| No runtime discovery (no rpaths, no copied DLLs) | Deliberate: `conanrun` and deployers already solve this. See example 1. |
| No `set_property` / target-name customisation | Target names are fixed as `pkg::component`. |
| Paths are absolute | Output is not relocatable after a deployer. |
| Header-only C++ packages that clear settings look like C | Set `link_libcpp` yourself. See example 2. |

---

## Verification

Every example above was built and run before publishing. Nothing here is illustrative-only.

| | Toolchain |
| --- | --- |
| Platform | macOS 26, arm64 (Apple Silicon) |
| Zig | 0.16.0 |
| Conan | `ar/zigdeps-2` branch |
| Packages | `openssl/3.5.4`, `zlib/1.3.1`, `pugixml/1.14`, `nlohmann_json/3.11.3`, `flex/2.6.4` (+ `m4/1.4.19`), `cmocka/1.1.7` |

Full `conan install` and `zig build` logs for each example — in both static and shared
configurations — plus the exact generated `conan_deps.zig` / `conan_setup.zig` for each, are
kept under `.zigdeps-example-logs/`.

| Example | Static | Shared |
| --- | --- | --- |
| 1. OpenSSL | `01-c-openssl-static-*.log` | `01-c-openssl-shared-*.log` |
| 2. pugixml + nlohmann_json | `02-cpp-static-*.log` | `02-cpp-shared-*.log` |
| 3. flex | `03-tool-flex-*.log` | n/a — a build tool has no link variant |
| 4. zlib + cmocka | `04-tests-static-*.log` | `04-tests-shared-*.log` |

Because ConanCenter has no prebuilt binaries for `apple-clang 21`, these runs used a
`compatibility.py` plugin mapping it onto older ABI-compatible binaries. Without it, add
`--build=missing` and expect a longer first run.
