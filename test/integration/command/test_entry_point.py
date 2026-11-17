import os
import subprocess
import sys
import textwrap

import conan
from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.tools import TestClient


def _run_conan(client, *args):
    """ The real "conan" entry point, in a new process: TestClient doesn't use it """
    code = textwrap.dedent("""
        import atexit, gc, sys
        atexit.register(lambda: print("requests imported:", "requests" in sys.modules))
        atexit.register(lambda: print("gc enabled:", gc.isenabled()))
        from conans.conan import run
        run()
        """)
    root = os.path.dirname(os.path.dirname(os.path.abspath(conan.__file__)))
    env = dict(os.environ, CONAN_HOME=client.cache_folder, PYTHONPATH=root)
    return subprocess.run([sys.executable, "-c", code, *args], cwd=client.current_folder, env=env,
                          capture_output=True, text=True)


def test_entry_point_exit():
    c = TestClient(light=True)
    c.save({"conanfile.py": GenConanfile("pkg", "0.1")})
    c.run("export .")

    result = _run_conan(c, "graph", "info", "--requires=pkg/0.1")
    assert result.returncode == 0, result.stderr
    assert "pkg/0.1#" in result.stdout  # The output is flushed before exiting
    # The atexit handlers run, and the commands without remote requests don't import "requests"
    assert "requests imported: False" in result.stdout
    assert "gc enabled: False" in result.stdout

    result = _run_conan(c, "install", "--requires=missing/0.1")
    assert result.returncode == 1
    assert "ERROR: Package 'missing/0.1' not resolved" in result.stderr
    assert "requests imported: False" in result.stdout
