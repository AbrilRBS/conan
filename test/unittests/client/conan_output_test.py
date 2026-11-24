import sys
from unittest import mock

import pytest
import textwrap

from conan.api.output import ConanOutput, init_colorama, cli_out_write, cli_out_write_chunks
from conan.test.utils.mocks import RedirectedTestOutput
from conan.test.utils.tools import redirect_output


class TestConanOutput:

    @pytest.mark.parametrize("isatty, env", [(True, {"NO_COLOR": "1"}),
                                             (True, {"NO_COLOR": "0"})])
    def test_output_color_prevent_strip(self, isatty, env):
        with mock.patch("colorama.init") as init:
            with mock.patch("sys.stderr.isatty", return_value=isatty), \
                 mock.patch.dict("os.environ", env, clear=True):
                init_colorama(sys.stderr)
                out = ConanOutput()
                assert out.color is False
                init.assert_not_called()


@pytest.mark.parametrize("force", ["1", "0", "foo"])
def test_output_forced(force):
    env = {"CLICOLOR_FORCE": force}
    forced = force != "0"
    with mock.patch("colorama.init") as init:
        with mock.patch("sys.stderr.isatty", return_value=False), \
             mock.patch.dict("os.environ", env, clear=True):
            init_colorama(sys.stderr)
            out = ConanOutput()

            assert out.color is forced
            if not forced:
                init.assert_not_called()


def test_output_chainable():
    stderr = RedirectedTestOutput()
    with redirect_output(stderr):
        ConanOutput(scope="My package")\
            .title("My title")\
            .highlight("Worked")\
            .info("But there was more that needed to be said")
    assert "My package" in stderr.getvalue()
    assert "My title" in stderr.getvalue()
    assert "Worked" in stderr.getvalue()
    assert "But there was more that needed to be said" in stderr.getvalue()


def test_output_no_trim():
    stderr = RedirectedTestOutput()
    with redirect_output(stderr):
        ConanOutput().write("Hello ", newline=False)
        ConanOutput().write("world ", newline=True)
    assert "Hello world " in stderr.getvalue()

    with redirect_output(stderr):
        output = ConanOutput()
        output.write("Hello ")
        output.writeln("world ")
        msg = "my "
        msg += "package"
        output.info(msg)
    assert "Hello world " in stderr.getvalue()
    assert "my package" in stderr.getvalue()


def test_cli_out_write():
    """ A large output is written in pieces, not copied, and the ones given in chunks are written
    as they come, with the same result """
    stdout = RedirectedTestOutput()
    big = "".join(chr(ord("a") + i % 26) for i in range(3 * 1024 * 1024 + 5))
    with redirect_output(RedirectedTestOutput(), stdout):
        cli_out_write(big)
        cli_out_write(42, indentation=2)  # Not only strings
        cli_out_write("no newline", endline="")
        cli_out_write_chunks(iter(["a", "", "b" * 10]))
        cli_out_write_chunks(iter([]))
    assert stdout.getvalue() == f"{big}\n  42\nno newlinea{'b' * 10}\n\n"
