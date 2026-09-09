"""Adapter protocols + a tiny registry.

The registry is populated by the ``@register_in`` / ``@register_out`` decorators.
``llm_proxy/adapters/__init__.py`` imports the bundled adapter modules so their
decorators run on package import.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..model.ir import NormalizedRequest, NormalizedResponse, WireRequest, WireResponse


@runtime_checkable
class InAdapter(Protocol):
    """Speaks to the client. Translates client wire -> IR, and IR -> client wire."""

    name: str
    supported_paths: list[str]

    def parse_request(self, wire: WireRequest) -> NormalizedRequest: ...

    def serialize_response(self, norm: NormalizedResponse, wire_ctx=None) -> bytes:
        """IR -> client wire bytes. ``wire_ctx`` may carry the original upstream
        body bytes for a same-format passthrough."""
        raise NotImplementedError


@runtime_checkable
class OutAdapter(Protocol):
    """Speaks to the upstream. Translates IR -> upstream wire, and upstream wire -> IR."""

    name: str

    def build_request(self, norm: NormalizedRequest) -> WireRequest: ...

    def parse_response(self, wire: WireResponse) -> NormalizedResponse: ...


_IN_REGISTRY: dict[str, type] = {}
_OUT_REGISTRY: dict[str, type] = {}


def register_in(name: str):
    def deco(cls):
        _IN_REGISTRY[name] = cls
        return cls

    return deco


def register_out(name: str):
    def deco(cls):
        _OUT_REGISTRY[name] = cls
        return cls

    return deco


class AdapterRegistry:
    def in_adapter(self, name: str):
        if name not in _IN_REGISTRY:
            raise KeyError(f"unknown in adapter: {name!r}; available: {sorted(_IN_REGISTRY)}")
        return _IN_REGISTRY[name]()

    def out_adapter(self, name: str):
        if name not in _OUT_REGISTRY:
            raise KeyError(f"unknown out adapter: {name!r}; available: {sorted(_OUT_REGISTRY)}")
        return _OUT_REGISTRY[name]()
