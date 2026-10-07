import os
import textwrap

from conan.test.utils.test_files import temp_folder
from conan.test.utils.tools import TestClient


def test_git_clone_with_source_layout():
    client = TestClient()
    repo = temp_folder()
    conanfile = textwrap.dedent("""
           import os
           from conan import ConanFile
           class Pkg(ConanFile):
               exports_sources = "*.txt"

               def layout(self):
                   self.folders.source = "src"

               def source(self):
                   self.run('git clone "{}" .')
       """).format(repo.replace("\\", "/"))

    client.save({"conanfile.py": conanfile,
                 "myfile.txt": "My file is copied"})
    with client.chdir(repo):
        client.save({"cloned.txt": "foo"}, repo)
        client.init_git_repo()

    client.run("create . --name=hello --version=1.0")
    sf = client.exported_layout().source()
    assert os.path.exists(os.path.join(sf, "myfile.txt"))
    # The conanfile is cleared from the root before cloning
    assert not os.path.exists(os.path.join(sf, "conanfile.py"))
    assert not os.path.exists(os.path.join(sf, "cloned.txt"))

    assert os.path.exists(os.path.join(sf, "src", "cloned.txt"))
    assert not os.path.exists(os.path.join(sf, "src", "myfile.txt"))
