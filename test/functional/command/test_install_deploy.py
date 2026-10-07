import os
import platform
import shutil
import textwrap

import pytest

from conan.test.assets.cmake import gen_cmakelists
from conan.test.assets.sources import gen_function_cpp
from conan.test.utils.test_files import temp_folder
from conan.test.utils.tools import TestClient


@pytest.fixture()
def client(matrix_client_shared):
    c = matrix_client_shared
    conanfile = textwrap.dedent("""
       import os
       from conan import ConanFile
       from conan.tools.files import save
       class Tool(ConanFile):
           name = "tool"
           version = "1.0"
           def package(self):
               save(self, os.path.join(self.package_folder, "build", "my_tools.cmake"),
                    'set(MY_TOOL_VARIABLE "Hello world!")')

           def package_info(self):
               self.cpp_info.includedirs = []
               self.cpp_info.libdirs = []
               self.cpp_info.bindirs = []
               path_build_modules = os.path.join("build", "my_tools.cmake")
               self.cpp_info.set_property("cmake_build_modules", [path_build_modules])
           """)
    c.save({"conanfile.py": conanfile}, clean_first=True)
    c.run("create .")
    return c


@pytest.mark.tool("cmake")
@pytest.mark.parametrize("powershell", [False, True])
def test_install_deploy(client, powershell):
    c = client
    custom_content = 'message(STATUS "MY_TOOL_VARIABLE=${MY_TOOL_VARIABLE}!")'
    cmake = gen_cmakelists(appname="my_app", appsources=["main.cpp"], find_package=["matrix", "tool"],
                           custom_content=custom_content)
    deploy = textwrap.dedent("""
        import os, shutil

        # USE **KWARGS to be robust against changes
        def deploy(graph, output_folder, **kwargs):
            conanfile = graph.root.conanfile
            for r, d in conanfile.dependencies.items():
                new_folder = os.path.join(output_folder, d.ref.name)
                shutil.copytree(d.package_folder, new_folder)
                d.set_deploy_folder(new_folder)
        """)
    c.save({"conanfile.txt": "[requires]\nmatrix/1.0\ntool/1.0",
            "deploy.py": deploy,
            "CMakeLists.txt": cmake,
            "main.cpp": gen_function_cpp(name="main", includes=["matrix"], calls=["matrix"])},
           clean_first=True)
    pwsh = "-c tools.env.virtualenv:powershell=powershell.exe" if powershell else ""
    c.run("install . -o *:shared=True "
          f"--deployer=deploy.py -of=mydeploy -g CMakeToolchain -g CMakeDeps {pwsh}")
    c.run("remove * -c")  # Make sure the cache is clean, no deps there
    arch = c.get_default_host_profile().settings['arch']
    deps = c.load(f"mydeploy/matrix-release-{arch}-data.cmake")
    assert 'set(matrix_PACKAGE_FOLDER_RELEASE "${CMAKE_CURRENT_LIST_DIR}/matrix")' in deps
    assert 'set(matrix_INCLUDE_DIRS_RELEASE "${matrix_PACKAGE_FOLDER_RELEASE}/include")' in deps
    assert 'set(matrix_LIB_DIRS_RELEASE "${matrix_PACKAGE_FOLDER_RELEASE}/lib")' in deps

    # We can fully move it to another folder, and still works
    tmp = os.path.join(temp_folder(), "relocated")
    shutil.copytree(c.current_folder, tmp)
    shutil.rmtree(c.current_folder)
    c2 = TestClient(current_folder=tmp)
    # I can totally build without errors with deployed
    c2.run_command("cmake . -DCMAKE_TOOLCHAIN_FILE=mydeploy/conan_toolchain.cmake "
                   "-DCMAKE_BUILD_TYPE=Release")
    assert "MY_TOOL_VARIABLE=Hello world!!" in c2.out
    c2.run_command("cmake --build . --config Release")
    if platform.system() == "Windows":  # Only the .bat env-generators are relocatable
        if powershell:
            cmd = r"powershell.exe mydeploy\conanrun.ps1 ; Release\my_app.exe"
        else:
            cmd = r"mydeploy\conanrun.bat && Release\my_app.exe"
        # For Lunux: cmd = ". mydeploy/conanrun.sh && ./my_app"
        c2.run_command(cmd)
        assert "matrix/1.0: Hello World Release!" in c2.out


@pytest.mark.tool("cmake")
def test_install_full_deploy_layout(client):
    c = client
    custom_content = 'message(STATUS "MY_TOOL_VARIABLE=${MY_TOOL_VARIABLE}!")'
    cmake = gen_cmakelists(appname="my_app", appsources=["main.cpp"], find_package=["matrix", "tool"],
                           custom_content=custom_content)
    conanfile = textwrap.dedent("""
        [requires]
        matrix/1.0
        tool/1.0
        [generators]
        CMakeDeps
        CMakeToolchain
        [layout]
        cmake_layout
        """)
    c.save({"conanfile.txt": conanfile,
            "CMakeLists.txt": cmake,
            "main.cpp": gen_function_cpp(name="main", includes=["matrix"], calls=["matrix"])},
           clean_first=True)
    c.run("install . -o *:shared=True --deployer=full_deploy.py")
    c.run("remove * -c")  # Make sure the cache is clean, no deps there
    arch = c.get_default_host_profile().settings['arch']
    folder = "/Release" if platform.system() != "Windows" else ""
    rel_path = "../../" if platform.system() == "Windows" else "../../../"
    deps = c.load(f"build{folder}/generators/matrix-release-{arch}-data.cmake")
    assert 'set(matrix_PACKAGE_FOLDER_RELEASE "${CMAKE_CURRENT_LIST_DIR}/' \
           f'{rel_path}full_deploy/host/matrix/1.0/Release/{arch}")' in deps
    assert 'set(matrix_INCLUDE_DIRS_RELEASE "${matrix_PACKAGE_FOLDER_RELEASE}/include")' in deps
    assert 'set(matrix_LIB_DIRS_RELEASE "${matrix_PACKAGE_FOLDER_RELEASE}/lib")' in deps

    # We can fully move it to another folder, and still works
    tmp = os.path.join(temp_folder(), "relocated")
    shutil.copytree(c.current_folder, tmp)
    shutil.rmtree(c.current_folder)
    c2 = TestClient(current_folder=tmp)
    with c2.chdir(f"build{folder}"):
        # I can totally build without errors with deployed
        cmakelist = "../.." if platform.system() != "Windows" else ".."
        c2.run_command(f"cmake {cmakelist} -DCMAKE_TOOLCHAIN_FILE=generators/conan_toolchain.cmake "
                       "-DCMAKE_BUILD_TYPE=Release")
        assert "MY_TOOL_VARIABLE=Hello world!!" in c2.out
        c2.run_command("cmake --build . --config Release")
        if platform.system() == "Windows":  # Only the .bat env-generators are relocatable atm
            cmd = r"generators\conanrun.bat && Release\my_app.exe"
            # For Lunux: cmd = ". mydeploy/conanrun.sh && ./my_app"
            c2.run_command(cmd)
            assert "matrix/1.0: Hello World Release!" in c2.out
