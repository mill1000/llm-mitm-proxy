"""Dissector package: importing it registers the bundled dissectors.

Two dissectors: the OpenAI-compatible chat decoder and the generic raw-capture
fallback. Selection is per request by method+path (chat path -> chat decoder,
anything else -> generic); there is no global setting.
"""

from . import base  # noqa: F401
from . import generic  # noqa: F401
from . import openai  # noqa: F401
