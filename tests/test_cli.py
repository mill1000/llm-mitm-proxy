"""CLI and packaging: __version__ from installed metadata (setuptools_scm), the
llm-proxy console script, and cli_overrides() argument mapping (incl. the
two-port split). No mock upstream needed."""

from __future__ import annotations

import contextlib
import importlib
import io
import unittest

from llm_proxy.app import cli_overrides


class TestPackaging(unittest.TestCase):
    """__version__ must come from the installed package metadata (setuptools_scm), not a fallback."""

    def test_version_matches_installed_metadata(self):
        from importlib.metadata import version as pkg_version

        from llm_proxy import __version__

        self.assertEqual(__version__, pkg_version("llm-proxy"))

    def test_console_script_resolves(self):
        """The llm-proxy console script must resolve to a callable in the package."""
        from importlib.metadata import distribution

        eps = [ep for ep in distribution("llm-proxy").entry_points if ep.name == "llm-proxy"]
        self.assertEqual(len(eps), 1)
        module_name, _, attr = eps[0].value.partition(":")
        module = importlib.import_module(module_name)
        self.assertTrue(callable(getattr(module, attr)))


class TestCli(unittest.TestCase):
    """cli_overrides() maps command-line args onto Settings field names."""

    def test_no_args_no_overrides(self):
        self.assertEqual(cli_overrides([]), {})

    def test_positional_upstream_and_flags(self):
        self.assertEqual(
            cli_overrides(["http://127.0.0.1:8080", "--host", "0.0.0.0", "--ui-port", "9091"]),
            {"upstream_base_url": "http://127.0.0.1:8080", "listen_host": "0.0.0.0", "ui_port": 9091},
        )

    def test_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli_overrides(["--help"])
        self.assertEqual(cm.exception.code, 0)


class TestCliPorts(unittest.TestCase):
    """--llm-port/--ui-port map onto the two listener ports."""

    def test_two_port_overrides(self):
        self.assertEqual(
            cli_overrides(["http://x", "--llm-port", "8081", "--ui-port", "9091"]),
            {"upstream_base_url": "http://x", "llm_port": 8081, "ui_port": 9091},
        )


if __name__ == "__main__":
    unittest.main()
