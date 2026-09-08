"""Adapter package. Importing it registers the bundled adapters.

Add a new adapter by creating a module here and importing it below so its
``@register_in`` / ``@register_out`` decorators run at package import time.
"""

from . import openai  # noqa: F401  (registers the openai in/out adapter)
