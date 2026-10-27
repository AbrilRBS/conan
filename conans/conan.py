import sys

# Modules that are exported by their package under the same name (e.g. the ``cross_building``
# function and the ``conan.tools.build.cross_building`` module). If imported lazily,
# ``from conan.tools.build import cross_building`` could resolve to the module, not the function
_EAGER_IMPORTS = ("conan.tools.build.cross_building",
                  "conan.tools.build.stdcpp_library",
                  "conan.tools.files.symlinks",
                  "conans.server.conf.default_server_conf")


def _enable_lazy_imports():
    # Python >= 3.15 (PEP 810). Deferring imports until first use reduces the CLI startup time
    # by 25-45%. It must be called before importing anything from "conan"
    set_lazy_imports_filter = getattr(sys, "set_lazy_imports_filter", None)
    if set_lazy_imports_filter is None:
        return
    set_lazy_imports_filter(lambda importing, imported, fromlist:
                            not imported.startswith(_EAGER_IMPORTS))
    sys.set_lazy_imports("all")


def run():
    _enable_lazy_imports()
    from conan.cli.cli import main
    main(sys.argv[1:])


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    run()
