import pytest
import textwrap

from conan.test.assets.sources import gen_function_cpp
from test.functional.toolchains.meson._base import check_binary


@pytest.mark.tool("ninja")
@pytest.mark.tool("meson")
@pytest.mark.tool("pkg_config")
class TestMeson:
    _test_package_meson_build = textwrap.dedent("""
        project('test_package', 'cpp')
        matrix = dependency('matrix', version : '>=1.0')
        test_package = executable('test_package', 'test_package.cpp', dependencies: matrix)
        test('test package', test_package)
        """)

    _test_package_conanfile_py = textwrap.dedent("""
        import os
        from conan import ConanFile
        from conan.tools.meson import Meson, MesonToolchain


        class TestConan(ConanFile):
            settings = "os", "compiler", "build_type", "arch"
            generators = "PkgConfigDeps"

            def requirements(self):
                self.requires(self.tested_reference_str)

            def layout(self):
                self.folders.build = "build"

            def generate(self):
                tc = MesonToolchain(self)
                tc.generate()

            def build(self):
                meson = Meson(self)
                meson.configure()
                meson.build()

            def test(self):
                meson = Meson(self)
                meson.configure()
                meson.test()
        """)

    def test_reuse(self, matrix_client):
        t = matrix_client  # matrix/1.0 static library, only its Meson test_package is built

        test_package_cpp = gen_function_cpp(name="main", includes=["matrix"], calls=["matrix"])

        t.save({"test_package/conanfile.py": self._test_package_conanfile_py,
                "test_package/meson.build": self._test_package_meson_build,
                "test_package/test_package.cpp": test_package_cpp})

        t.run("test test_package matrix/1.0")

        check_binary(t)
