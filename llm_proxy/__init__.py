"""LLM Proxy — a low-overhead OpenAI-compatible reverse proxy with a live conversation UI."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version setuptools_scm derives from git tags
    # at install/build time (see pyproject.toml).
    __version__ = version("llm-proxy")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
