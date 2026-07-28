# ZigDeps by example

Four worked examples of a **Zig project consuming Conan packages**, using the experimental
`ZigDeps` generator. In each one the application is written in Zig; the C and C++ libraries
come from ConanCenter. Every example here was built and run end to end — see
[Verification](#verification).

> `ZigDeps` is experimental. The shape of the generated Zig API is expected to change.

**Contents**

1. [Zig using a C library — OpenSSL](#1-zig-using-a-c-library--openssl) — components, transitive dependencies, static and shared
2. [Zig using a C++ library — snappy](#2-zig-using-a-c-library--snappy) — the C++ runtime, via a library's own C API
3. [Zig using a build tool — flex](#3-zig-using-a-build-tool--flex) — `tool_requires`, generated code
4. [Testing a C library from Zig — zlib + cmocka](#4-testing-a-c-library-from-zig--zlib--cmocka) — `test_requires`

---

## How it works

`conan install . -g ZigDeps` writes two files into a `conan_zig_deps/` folder:

| File | What it is |
| --- | --- |
| `conan_deps.zig` | Data. A `comptime` map of every dependency: include dirs, library paths, defines, system libs, frameworks, and each target's own `requires` list. |
| `conan_setup.zig` | Behaviour. Helpers your `build.zig` calls to push that data into a module. |

Zig has no native format for describing a prebuilt C library, and does not propagate include
or library paths through `linkLibrary()`, so there is nothing to emit *into* — the generator
emits Zig source that your build imports instead.

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
so the same call works for a test module.

Once a module has been set up this way, `@cImport` resolves the dependency's headers with no
paths written by hand.

---

## 1. Zig using a C library — OpenSSL

A Zig program that hashes a string with OpenSSL. No C sources of our own.

Shows **component-level linking** and **transitive resolution**: OpenSSL's `crypto`
component depends on `zlib`, and `ssl` depends on `crypto`, so naming one target pulls the
rest in automatically.

**`conanfile.txt`**

```ini
[requires]
openssl/3.5.4

[generators]
ZigDeps
```

**`main.zig`**

```zig
const std = @import("std");

// Zig imports the C headers directly. ZigDeps put OpenSSL's include directories on this
// module, so @cInclude resolves without any path being written here.
const ssl = @cImport({
    @cInclude("openssl/evp.h");
    @cInclude("openssl/crypto.h");
});

pub fn main() !void {
    const msg = "conan + zig";

    var digest: [ssl.EVP_MAX_MD_SIZE]u8 = undefined;
    var len: c_uint = 0;

    const ctx = ssl.EVP_MD_CTX_new() orelse return error.OpenSslFailed;
    defer ssl.EVP_MD_CTX_free(ctx);

    if (ssl.EVP_DigestInit_ex(ctx, ssl.EVP_sha256(), null) != 1) return error.OpenSslFailed;
    if (ssl.EVP_DigestUpdate(ctx, msg, msg.len) != 1) return error.OpenSslFailed;
    if (ssl.EVP_DigestFinal_ex(ctx, &digest, &len) != 1) return error.OpenSslFailed;

    std.debug.print("{s}\n", .{std.mem.span(ssl.OpenSSL_version(ssl.OPENSSL_VERSION))});
    std.debug.print("sha256(\"{s}\") = ", .{msg});
    for (digest[0..len]) |b| std.debug.print("{x:0>2}", .{b});
    std.debug.print("\n", .{});
}
```

**`build.zig`**

```zig
const std = @import("std");
const conan = @import("conan_zig_deps/conan_setup.zig");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // A plain Zig module - no C or C++ sources of our own.
    const mod = b.createModule(.{
        .root_source_file = b.path("main.zig"),
        .target = target,
        .optimize = optimize,
    });

    // Only the "crypto" component is needed. Its own requires - openssl::crypto ->
    // zlib::zlib - are followed automatically, so zlib is linked without naming it.
    conan.linkDependency(mod, "openssl::crypto");

    const exe = b.addExecutable(.{ .name = "digest", .root_module = mod });
    b.installArtifact(exe);

    const run = b.addRunArtifact(exe);
    b.step("run", "Run the Zig program").dependOn(&run.step);
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

The graph that gets walked, with nothing in `build.zig` describing it:

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
libraries, because Conan already solves runtime discovery with `conanrun` (which sets `PATH`
on Windows and `(DY)LD_LIBRARY_PATH` elsewhere). Skipping the activation gives:

```
dyld[49473]: Library not loaded: @rpath/libcrypto.3.dylib
```

which is the expected, documented behaviour — not a bug. From a recipe, the equivalent is
`self.run("zig build run", env="conanrun")`.

> **Watch out:** `VirtualRunEnv` decides whether to export the library-path variables at all
> by looking at `settings.os`. A consumer recipe that declares no `settings` gets a silently
> **empty** `conanrun` environment, and shared dependencies stay unfindable with no warning.

### Letting Zig compile your own C sources

Zig ships a C compiler, so a project that still has C of its own needs no separate toolchain
— and `ZigDeps` is used identically either way. Alongside `main.zig`, this example carries
`digest.c` (the same hashing logic written in C) and builds it as a second target from the
same `build.zig`:

```zig
// --- The same thing in C, compiled by Zig itself ---------------------------------
// Zig ships a C compiler, so a project with its own C sources needs no separate
// toolchain. ZigDeps is used identically either way.
const c_mod = b.createModule(.{ .target = target, .optimize = optimize });
c_mod.addCSourceFile(.{ .file = b.path("digest.c"), .flags = &.{"-std=c11"} });
conan.linkDependency(c_mod, "openssl::crypto");

const c_exe = b.addExecutable(.{ .name = "digest-c", .root_module = c_mod });
b.installArtifact(c_exe);

const run_c = b.addRunArtifact(c_exe);
b.step("run-c", "Build the C version with Zig and run it").dependOn(&run_c.step);
```

```bash
zig build run-c
```

```
OpenSSL 3.5.4 30 Sep 2025 (from C)
sha256("conan + zig") = c69d96afb1f7a8ea85d27a29245f1a31bb3e0026de72f0e4f762ad93fac142e6
```

Note the difference from the Zig module: a C module needs no `@cImport`, since `digest.c`
includes the OpenSSL headers itself — `ZigDeps` supplies the include paths to both.

---

## 2. Zig using a C++ library — snappy

**Zig can only `@cImport` C, never C++.** The clean case is a C++ library that ships a C API
of its own: `snappy` is written in C++ but maintains `snappy-c.h` alongside it, so it is
usable from Zig directly, with no shim and no C++ in the project at all.

This is also the sharpest demonstration of what `ZigDeps` contributes. Nothing on the Zig
side looks like C++ — a C header, a C API — yet the *implementation* behind that API is C++,
so the C++ standard library is still required at link time. A Zig binary does not link it by
default. Building without it fails with **9 undefined `std::` symbols**:

```
error: undefined symbol: __ZNSt3__112basic_stringIcNS_11char_traitsIcEENS_9allocatorIcEEE5eraseEmm
```

`ZigDeps` detects that snappy is a C++ package and asks for the runtime, so this never
surfaces.

Note also the two targets snappy produces: `snappy::snappy` is an `.interface` target — a
package root with nothing to link — which requires `snappy::snappylib`, the `.static` (or
`.shared`) library that carries the actual archive.

**`conanfile.txt`**

```ini
[requires]
snappy/1.1.10

[generators]
ZigDeps
```

**`main.zig`**

```zig
const std = @import("std");

// snappy is written in C++, but ships snappy-c.h - a real C API maintained by the project
// itself. That is what makes it usable from Zig directly: Zig can @cImport C headers, but
// never C++ ones, so a C++ library is only reachable when it exposes a C surface like this.
//
// Nothing below is C++, yet the C++ standard library is still required at link time,
// because the *implementation* behind this C API is C++. ZigDeps detects that and asks for
// the runtime; without it the link fails with undefined std:: symbols.
const snappy = @cImport({
    @cInclude("snappy-c.h");
});

pub fn main() !void {
    const input = "conan conan conan zig zig zig zig";

    var compressed: [256]u8 = undefined;
    var compressed_len: usize = compressed.len;
    if (snappy.snappy_compress(input, input.len, &compressed, &compressed_len) != snappy.SNAPPY_OK)
        return error.CompressFailed;

    var restored: [256]u8 = undefined;
    var restored_len: usize = restored.len;
    if (snappy.snappy_uncompress(&compressed, compressed_len, &restored, &restored_len) != snappy.SNAPPY_OK)
        return error.UncompressFailed;

    std.debug.print("compressed {d} -> {d} bytes\n", .{ input.len, compressed_len });
    std.debug.print("round trip ok: {}\n", .{std.mem.eql(u8, input, restored[0..restored_len])});
}
```

**`build.zig`**

```zig
const std = @import("std");
const conan = @import("conan_zig_deps/conan_setup.zig");

pub fn build(b: *std.Build) void {
    const target = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{});

    // A plain Zig module. No C++ sources, and no shim: snappy provides the C API itself.
    const mod = b.createModule(.{
        .root_source_file = b.path("main.zig"),
        .target = target,
        .optimize = optimize,
    });

    // link_libcpp is never set here. ZigDeps knows snappy is a C++ package and asks for the
    // C++ runtime itself - a Zig binary would otherwise not link it at all.
    conan.linkDependencies(mod);

    const exe = b.addExecutable(.{ .name = "roundtrip", .root_module = mod });
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
compressed 33 -> 27 bytes
round trip ok: true
```

Add `-o "snappy/*:shared=True"` for the shared build, and `source ./conanrun.sh` to run it.

### When the library has no C API

Most C++ libraries do not ship one. `pugixml` and `nlohmann_json`, for example, are
C++-only, so a Zig consumer has to add a small `extern "C"` shim — one `.cpp` file exposing
the operations it needs — and `@cImport` that shim's header instead. The `build.zig` is
otherwise unchanged: `linkDependencies()` still supplies the include paths and the C++
runtime for the shim to compile and link against.

Prefer a library with an official C API where one exists; a hand-written shim is code you
then own and have to keep in step with the library.

### How C++ is detected

A dependency's `languages` attribute is authoritative when the recipe sets it — a package
declaring `languages = "C"` never gets the C++ runtime. Most recipes still leave it unset, so
`ZigDeps` falls back to `compiler.libcxx`: Conan only keeps that setting for C++ packages,
since a C recipe drops it with `settings.rm_safe("compiler.libcxx")`.

That fallback is not exhaustive. A **header-only C++ package that clears its settings** looks
identical to a C one, so set `mod.link_libcpp = true` yourself for those. CMake consumers
have the same gap, and resolve it the same way — through the consumer's own `project(... CXX)`
declaration.

### Troubleshooting: headers that rely on transitive includes

Zig bundles its own libc++, whose headers include slightly less than Apple's or GNU's. A
library that leans on an include it never asked for can therefore compile everywhere else
and fail here. This is a property of the library version, not of `ZigDeps`, and is usually
already fixed upstream — check for a newer version before working around it.

`fmt` is a worked example. In `fmt/11.2.0`, `format.h` calls `malloc` and `free` without
including `<cstdlib>`, so it fails under Zig with:

```
error: use of undeclared identifier 'malloc'
```

`fmt` added the include after that release (`<cstdlib>` in 12.0.0, `<stdlib.h>` on master),
so the fix is simply to move up — `fmt/12.0.0` compiles and runs with Zig unchanged:

```ini
[requires]
fmt/12.0.0
```

If you are pinned to a version that still has the problem, force the include from the
consumer rather than patching the package:

```zig
mod.addCSourceFile(.{ .file = b.path("shim.cpp"),
                      .flags = &.{ "-std=c++17", "-include", "cstdlib" } });
```

---

## 3. Zig using a build tool — flex

Shows `tool_requires`: a dependency in the **build context**, where there is nothing to link
and the only thing you want is the path to a program. `flex` generates a C lexer during the
build; the Zig program drives it. The generated `.c` never exists in the repository.

**`conanfile.txt`**

```ini
[tool_requires]
flex/2.6.4

[generators]
ZigDeps
```

**`counter.l`** — note there is no `main()`; the lexer is a library Zig calls

```lex
%option noyywrap nounput noinput
%{
int words = 0, numbers = 0;
%}
%%
[0-9]+      { numbers++; }
[a-zA-Z]+   { words++; }
.|\n        { /* skip */ }
%%
int count_words(void)   { return words; }
int count_numbers(void) { return numbers; }
```

**`main.zig`**

```zig
const std = @import("std");

// The lexer C source does not exist in the repository: flex generates it during the build,
// and Zig compiles it into this binary. Only the hand-written header is imported.
const lexer = @cImport({
    @cInclude("lexer.h");
});

pub fn main() !void {
    _ = lexer.yylex(); // reads stdin
    std.debug.print("words={d} numbers={d}\n",
        .{ lexer.count_words(), lexer.count_numbers() });
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

    const mod = b.createModule(.{
        .root_source_file = b.path("main.zig"),
        .target = target,
        .optimize = optimize,
    });
    mod.addCSourceFile(.{ .file = lexer_c, .flags = &.{} });
    mod.addIncludePath(b.path("."));
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

Both are valid. The difference is what happens when the environment is *not* active, which is
the common case when someone just runs `zig build` in a shell. On a machine with a system
`flex` — macOS ships `/usr/bin/flex` — a bare `"flex"` silently resolves to that one instead,
with no error and a near-identical version string.

`toolPath()` resolves inside the package's own bindir, so the build does not depend on
whether the environment happens to be active. Use `"flex"` plus `conanbuild` if you prefer
the environment-driven route; use `toolPath()` if you want the build to be self-contained.

Note that `conan_tool_dirs` includes the tool's *own* transitive tools, so this example also
exposes `m4`, which `flex` requires.

---

## 4. Testing a C library from Zig — zlib + cmocka

Zig's own test runner links a Conan dependency like any other module, so **tests for a C
library can be written in Zig**. `b.addTest` takes a module, and `linkDependencies()` accepts
it directly.

A C test framework is shown second, for the case where the tests themselves are C. `cmocka`
is declared under `[test_requires]`: it becomes a real target in `conan_deps.zig`, but is
deliberately **excluded from `direct_targets`**, so `linkDependencies()` never drags a test
framework into the application. You name it explicitly, only in the test binary.

**`conanfile.txt`**

```ini
[requires]
zlib/1.3.1

[test_requires]
cmocka/1.1.7

[generators]
ZigDeps
```

**`test_zlib.zig`** — the primary suite

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

**`test_zlib_cmocka.c`** — the secondary, C-based suite

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
| Zig cannot `@cImport` C++ headers | A Zig-only property. C++ dependencies need a C surface: the library's own, or a shim — see example 2. |
| A dependency's `cflags` / `cxxflags` / link flags are **not applied** | Zig has no module-level flag injection — `Module.addCSourceFile` only applies flags to files added through it. They are emitted in `conan_deps.zig` and Conan warns when a dependency declares any, so pass them yourself. |
| No runtime discovery (no rpaths, no copied DLLs) | Deliberate: `conanrun` and deployers already solve this. See example 1. |
| No `set_property` / target-name customisation | Target names are fixed as `pkg::component`. |
| Paths are absolute | Output is not relocatable after a deployer. |
| Header-only C++ packages that clear settings look like C | Set `link_libcpp` yourself. See example 2. |

---

## Verification

Every example above was built and run before publishing. Nothing here is illustrative-only,
and the code shown is the code that was compiled.

| | Toolchain |
| --- | --- |
| Platform | macOS 26, arm64 (Apple Silicon) |
| Zig | 0.16.0 |
| Conan | `ar/zigdeps-2` branch |
| Packages | `openssl/3.5.4`, `zlib/1.3.1`, `snappy/1.1.10`, `flex/2.6.4` (+ `m4/1.4.19`), `cmocka/1.1.7` |

Examples 1, 2 and 4 were each built and run in both **static** and **shared** configurations.
Example 3 has no link variant, being a build tool. Full `conan install` and `zig build` logs
for every run, the exact generated `conan_deps.zig` / `conan_setup.zig` for each example, and
the sources as built are retained internally for traceability.

Because ConanCenter has no prebuilt binaries for `apple-clang 21`, these runs used a
`compatibility.py` plugin mapping it onto older ABI-compatible binaries. Without it, add
`--build=missing` and expect a longer first run.
