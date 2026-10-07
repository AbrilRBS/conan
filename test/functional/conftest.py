import contextlib
import os
import platform
import shutil
import textwrap

import fasteners
import pytest

from conan.test.assets.sources import gen_function_h, gen_function_cpp
from conan.test.utils.env import environment_update
from conan.test.utils.tools import TestClient
from test.conftest import _get_tool
from test.functional.utils import save_cache, client_from


@pytest.hookimpl(tryfirst=True)  # Before the "-m" markers deselection
def pytest_collection_modifyitems(items):
    """
    The "os_agnostic" tests are covered by the Linux and Windows CI runners, consider them slow
    in macOS, so they run there only in the complete runs (develop2), not in every PR
    """
    if platform.system() == "Darwin":
        for item in items:
            if item.get_closest_marker("os_agnostic"):
                item.add_marker(pytest.mark.slow)


@pytest.fixture(scope="session")
def build_once(tmp_path_factory):
    """
    Session fixtures run once per pytest-xdist worker, so every worker would build the same
    packages again. Build them once per test session instead, sharing the result between the
    workers. If other worker is building it right now, build it locally instead of waiting idle.
    The returned folder can be shared by all the tests, it must not be modified, use copies of it.
    """
    worker_root = str(tmp_path_factory.getbasetemp())
    shared_root = worker_root
    if os.getenv("PYTEST_XDIST_WORKER"):
        shared_root = os.path.dirname(worker_root)  # Common to all the workers of this session
    shared_root = os.path.join(shared_root, "build_once")
    os.makedirs(shared_root, exist_ok=True)

    def _build(build, folder):
        tmp_folder = folder + ".tmp"
        shutil.rmtree(tmp_folder, ignore_errors=True)  # From a previous failed build
        with _default_cmake():
            build(tmp_folder)
        os.rename(tmp_folder, folder)

    def _build_once(name, build):
        folder = os.path.join(shared_root, name)
        if os.path.exists(folder):
            return folder
        lock = fasteners.InterProcessLock(folder + ".lock")
        if lock.acquire(blocking=False):
            try:
                if not os.path.exists(folder):
                    _build(build, folder)
            finally:
                lock.release()
            return folder
        # Other worker is building it, do not wait for it
        folder = os.path.join(worker_root, "build_once", name)
        _build(build, folder)
        return folder
    return _build_once


def _default_cmake():
    """ build always with the default CMake, not with the version in the PATH of the test that
    happens to need the shared packages first
    """
    cmake_path = _get_tool("cmake", None)
    if not isinstance(cmake_path, str):  # Not available, leave the PATH untouched
        return contextlib.nullcontext()
    return environment_update({"PATH": cmake_path + os.pathsep + os.environ["PATH"]})


@pytest.fixture(scope="session")
def _matrix(build_once):
    """
    matrix/1.0, just static, no test_package
    """
    def build(folder):
        c = TestClient()
        c.run("new cmake_lib -d name=matrix -d version=1.0")
        c.run("create . -tf=")
        save_cache(c, folder)
    return build_once("matrix", build)


def _add_matrix_binaries(build_once, name, base, args):
    def build(folder):
        c = client_from(base)
        c.run("new cmake_lib -d name=matrix -d version=1.0")
        c.run(f"create . {args} -tf=")
        save_cache(c, folder)
    return build_once(name, build)


@pytest.fixture(scope="session")
def _matrix_shared(build_once, _matrix):
    """ matrix/1.0 static and shared """
    return _add_matrix_binaries(build_once, "matrix_shared", _matrix, "-o *:shared=True")


@pytest.fixture(scope="session")
def _matrix_debug(build_once, _matrix):
    """ matrix/1.0 static Release and Debug """
    return _add_matrix_binaries(build_once, "matrix_debug", _matrix, "-s build_type=Debug")


@pytest.fixture(scope="session")
def _matrix_shared_debug(build_once, _matrix_shared):
    """ matrix/1.0 static and shared Release, static Debug """
    return _add_matrix_binaries(build_once, "matrix_shared_debug", _matrix_shared,
                                "-s build_type=Debug")


@pytest.fixture()
def matrix_client(_matrix):
    return client_from(_matrix)


@pytest.fixture()
def matrix_client_nospace(_matrix):
    return client_from(_matrix, path_with_spaces=False)


@pytest.fixture()
def matrix_client_shared(_matrix_shared):
    return client_from(_matrix_shared)


@pytest.fixture()
def matrix_client_shared_debug(_matrix_shared_debug):
    return client_from(_matrix_shared_debug)


@pytest.fixture()
def matrix_client_debug(_matrix_debug):
    return client_from(_matrix_debug)


@pytest.fixture(scope="session")
def _transitive_libraries(build_once, _matrix):
    """
    engine/1.0->matrix/1.0, engine static and shared, matrix static
    """
    def build(folder):
        c = client_from(_matrix)
        c.run("new cmake_lib -d name=engine -d version=1.0 -d requires=matrix/1.0")
        # create both static and shared
        c.run("create . -tf=")
        c.run("create . -o engine/*:shared=True -tf=")
        save_cache(c, folder)
    return build_once("transitive_libraries", build)


@pytest.fixture()
def transitive_libraries(_transitive_libraries):
    return client_from(_transitive_libraries)


@pytest.fixture(scope="session")
def _matrix_client_components(build_once):
    """
    2 components, different than the package name
    """
    return build_once("matrix_components", _build_matrix_components)


def _build_matrix_components(folder):
    c = TestClient()
    headers_h = textwrap.dedent("""
        #include <iostream>
        #ifndef MY_MATRIX_HEADERS_DEFINE
        #error "Fatal error MY_MATRIX_HEADERS_DEFINE not defined"
        #endif
        void headers(){ std::cout << "Matrix headers: Release!" << std::endl;
            #if __cplusplus
            std::cout << "  Matrix headers __cplusplus: __cplusplus" << __cplusplus << std::endl;
            #endif
        }
        """)
    vector_h = gen_function_h(name="vector")
    vector_cpp = gen_function_cpp(name="vector", includes=["vector"])
    module_h = gen_function_h(name="module")
    module_cpp = gen_function_cpp(name="module", includes=["module", "vector"], calls=["vector"])

    conanfile = textwrap.dedent("""
        from conan import ConanFile
        from conan.tools.cmake import CMake

        class Matrix(ConanFile):
            name = "matrix"
            version = "1.0"
            settings = "os", "compiler", "build_type", "arch"
            generators = "CMakeToolchain"
            exports_sources = "src/*", "CMakeLists.txt"

            def build(self):
                cmake = CMake(self)
                cmake.configure()
                cmake.build()

            def package(self):
                cmake = CMake(self)
                cmake.install()

            def package_info(self):
                self.cpp_info.default_components = ["vector", "module"]

                self.cpp_info.components["headers"].includedirs = ["include/headers"]
                self.cpp_info.components["headers"].set_property("cmake_target_name", "MatrixHeaders")
                self.cpp_info.components["headers"].defines = ["MY_MATRIX_HEADERS_DEFINE=1"]
                # Few flags to cover that CMakeDeps doesn't crash with them
                if self.settings.compiler == "msvc":
                    self.cpp_info.components["headers"].cxxflags = ["/Zc:__cplusplus"]
                    self.cpp_info.components["headers"].cflags = ["/Zc:__cplusplus"]
                    self.cpp_info.components["headers"].system_libs = ["ws2_32"]
                else:
                    self.cpp_info.components["headers"].system_libs = ["m"]
                    # Just to verify CMake don't break
                    self.cpp_info.sharedlinkflags = ["-z now", "-z relro"]
                    self.cpp_info.exelinkflags = ["-z now", "-z relro"]

                self.cpp_info.components["vector"].libs = ["vector"]
                self.cpp_info.components["vector"].includedirs = ["include"]
                self.cpp_info.components["vector"].libdirs = ["lib"]

                self.cpp_info.components["module"].libs = ["module"]
                self.cpp_info.components["module"].includedirs = ["include"]
                self.cpp_info.components["module"].libdirs = ["lib"]
                self.cpp_info.components["module"].requires = ["vector"]
          """)

    cmakelists = textwrap.dedent("""
       set(CMAKE_CXX_COMPILER_WORKS 1)
       set(CMAKE_CXX_ABI_COMPILED 1)
       cmake_minimum_required(VERSION 3.15)
       project(matrix CXX)

       add_library(vector src/vector.cpp)
       add_library(module src/module.cpp)
       add_library(headers INTERFACE)
       target_link_libraries(module PRIVATE vector)

       set_target_properties(headers PROPERTIES PUBLIC_HEADER "src/headers.h")
       set_target_properties(module PROPERTIES PUBLIC_HEADER "src/module.h")
       set_target_properties(vector PROPERTIES PUBLIC_HEADER "src/vector.h")
       install(TARGETS vector module)
       install(TARGETS headers PUBLIC_HEADER DESTINATION include/headers)
       """)
    c.save({"src/headers.h": headers_h,
            "src/vector.h": vector_h,
            "src/vector.cpp": vector_cpp,
            "src/module.h": module_h,
            "src/module.cpp": module_cpp,
            "CMakeLists.txt": cmakelists,
            "conanfile.py": conanfile})
    c.run("create .")
    save_cache(c, folder)


@pytest.fixture()
def matrix_client_components(_matrix_client_components):
    return client_from(_matrix_client_components)


@pytest.fixture(scope="session")
def _matrix_c_interface_client(build_once):
    return build_once("matrix_c_interface", _build_matrix_c_interface)


def _build_matrix_c_interface(folder):
    c = TestClient()
    matrix_h = textwrap.dedent("""\
        #pragma once
        #ifdef __cplusplus
        extern "C" {
        #endif
            void matrix();
        #ifdef __cplusplus
        }
        #endif
        """)
    matrix_cpp = textwrap.dedent("""\
        #include "matrix.h"
        #include <iostream>
        #include <string>
        void matrix(){
            std::cout<< std::string("Hello Matrix!") <<std::endl;
        }
        """)
    # Having here the config.cmake code to be able to manually check what CMake generates
    cmake = textwrap.dedent("""\
        set(CMAKE_CXX_COMPILER_WORKS 1)
        set(CMAKE_CXX_ABI_COMPILED 1)
        cmake_minimum_required(VERSION 3.15)
        project(matrix C CXX)
        add_library(matrix STATIC src/matrix.cpp)
        target_include_directories(matrix PUBLIC
          $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
          $<INSTALL_INTERFACE:include>
        )
        set_target_properties(matrix PROPERTIES PUBLIC_HEADER "include/matrix.h")
        install(TARGETS matrix EXPORT matrixConfig)
        export(TARGETS matrix
            NAMESPACE matrix::
            FILE "${CMAKE_CURRENT_BINARY_DIR}/matrixConfig.cmake"
        )
        install(EXPORT matrixConfig
            DESTINATION "matrix/cmake"
            NAMESPACE matrix::
        )
        """)
    conanfile = textwrap.dedent("""\
        from conan import ConanFile
        from conan.tools.cmake import CMake, cmake_layout
        class Recipe(ConanFile):
            name = "matrix"
            version = "0.1"
            settings = "os", "compiler", "build_type", "arch"
            package_type = "static-library"
            generators = "CMakeToolchain"
            exports_sources = "CMakeLists.txt", "src/*", "include/*"
            languages = "C++"
            def build(self):
                cmake = CMake(self)
                cmake.configure()
                cmake.build()
            def layout(self):
                cmake_layout(self)
            def package(self):
                cmake = CMake(self)
                cmake.install()
            def package_info(self):
                self.cpp_info.libs = ["matrix"]
        """)
    c.save({"include/matrix.h": matrix_h,
            "src/matrix.cpp": matrix_cpp,
            "conanfile.py": conanfile,
            "CMakeLists.txt": cmake})
    c.run("create .")
    save_cache(c, folder)


@pytest.fixture()
def matrix_c_interface_client(_matrix_c_interface_client):
    return client_from(_matrix_c_interface_client)
