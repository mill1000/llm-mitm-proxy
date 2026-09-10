"""CLI and packaging: __version__ from installed metadata (setuptools_scm), the
llm-mitm-proxy console script, and cli_overrides() argument mapping (incl. the
two-port split). No mock upstream needed."""

from __future__ import annotations

import contextlib
import importlib
import io
import unittest
from pathlib import Path

import llm_proxy
from llm_proxy.app import cli_overrides

UI_DIR = Path(llm_proxy.__file__).resolve().parent / "web"


class TestPackaging(unittest.TestCase):
    """__version__ must come from the installed package metadata (setuptools_scm), not a fallback."""

    def test_version_matches_installed_metadata(self):
        from importlib.metadata import version as pkg_version

        from llm_proxy import __version__

        self.assertEqual(__version__, pkg_version("llm-mitm-proxy"))

    def test_console_script_resolves(self):
        """The llm-mitm-proxy console script must resolve to a callable in the package."""
        from importlib.metadata import distribution

        eps = [ep for ep in distribution("llm-mitm-proxy").entry_points if ep.name == "llm-mitm-proxy"]
        self.assertEqual(len(eps), 1)
        module_name, _, attr = eps[0].value.partition(":")
        module = importlib.import_module(module_name)
        self.assertTrue(callable(getattr(module, attr)))

    def test_ui_assets_ship_in_package(self):
        """The WebUI build output must live inside the package (wheel = full app for PyPI)."""
        if not UI_DIR.is_dir():
            self.skipTest("WebUI not built; run `npm run build`")
        for asset in ("index.html", "app.js", "styles.css", "favicon.svg"):
            self.assertTrue((UI_DIR / asset).is_file(), asset)


class TestCli(unittest.TestCase):
    """cli_overrides() maps command-line args onto Settings field names."""

    def test_no_args_no_overrides(self):
        self.assertEqual(cli_overrides([]), {})

    def test_positional_upstream_and_flags(self):
        self.assertEqual(
            cli_overrides(["http://127.0.0.1:8080", "--host", "0.0.0.0", "--web-port", "9091"]),
            {"upstream_base_url": "http://127.0.0.1:8080", "listen_host": "0.0.0.0", "ui_port": 9091},
        )

    def test_help_exits_zero(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                cli_overrides(["--help"])
        self.assertEqual(cm.exception.code, 0)


class TestCliPorts(unittest.TestCase):
    """--proxy-port/--web-port map onto the two listener ports."""

    def test_two_port_overrides(self):
        self.assertEqual(
            cli_overrides(["http://x", "--proxy-port", "8081", "--web-port", "9091"]),
            {"upstream_base_url": "http://x", "llm_port": 8081, "ui_port": 9091},
        )


if __name__ == "__main__":
    unittest.main()
