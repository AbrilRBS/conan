import platform
import textwrap

import pytest

from conan.test.utils.tools import TestClient


class TestBasic:

    @pytest.mark.tool("visual_studio")
    @pytest.mark.skipif(platform.system() != "Windows", reason="Only for windows")
    def test_toolchain_windows(self):
        client = TestClient()
        conanfile = textwrap.dedent("""
            from conan import ConanFile
            from conan.tools.microsoft import MSBuildToolchain
            class Pkg(ConanFile):
                name = "Pkg"
                version = "0.1"
                settings = "os", "compiler", "arch", "build_type"
                generators = "MSBuildDeps"

                def generate(self):
                    tc = MSBuildToolchain(self)
                    tc.generate()
        """)

        client.save({"conanfile.py": conanfile})

        client.run('install . -s os=Windows -s compiler=msvc -s compiler.version=191'
                   ' -s compiler.runtime=dynamic')

        conan_toolchain_props = client.load("conantoolchain.props")
        assert "<ConanPackageName>Pkg</ConanPackageName>" in conan_toolchain_props
        assert "<ConanPackageVersion>0.1</ConanPackageVersion>" in conan_toolchain_props
