import atexit
import gc
import os
import sys
import threading


def run():
    # No cyclic garbage collector in the "conan" command: almost all the objects it creates live
    # until the end (the graph), so the collector didn't free memory, but it traversed all of them
    # again every time they had grown by 25%, up to 50% of the time of a command with a large graph.
    # Disabled before importing Conan, which also skips the collections of the imports
    gc.disable()
    from conan.cli.cli import main
    try:
        main(sys.argv[1:])
    except SystemExit as e:
        if threading.active_count() > 1:  # The interpreter must wait for them, a normal exit
            raise
        _exit(e.code)


def _exit(code):
    """ Exit without the interpreter teardown, which frees all the objects one by one, ~10% of the
    time of a command with a large graph. Before, do what the interpreter would: run the atexit
    handlers (colorama resets the terminal) and flush the outputs
    """
    if code is None:
        code = 0
    elif not isinstance(code, int):  # sys.exit("message") prints it, and the exit code is 1
        sys.stderr.write(f"{code}\n")
        code = 1
    atexit._run_exitfuncs()  # noqa
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: e.g. the output was piped to a closed process, exit as usual
        sys.exit(code)
    os._exit(code)


if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    run()
