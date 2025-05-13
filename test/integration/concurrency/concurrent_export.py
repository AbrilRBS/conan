import json

from conan.test.assets.genconanfile import GenConanfile
from conan.test.utils.tools import TestClient


def test_export_concurrent():
    tc = TestClient()
    tc.save({"conanfile.py": GenConanfile("lib", "1.0")})
    # Create a thread pool to execute the export function concurrently
    from concurrent.futures import ThreadPoolExecutor

    def export_func(i):
        tc.run("export .")

    # Create a thread pool with 5 threads
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = [executor.submit(export_func, i) for i in range(12)]
        for future in futures:
            try:
                future.result()  # Wait for the thread to finish
            except Exception as e:
                print(f"Export failed: {e}")

    tc.run("list lib/1.0#* -f=json", redirect_stdout="out.json")
    list_json = json.loads(tc.load("out.json"))
    assert len(list_json["Local Cache"]["lib/1.0"]["revisions"]) == 1
    print()
