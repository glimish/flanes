"""
Serializable mixin for dataclasses.

Provides automatic to_dict()/from_dict() using dataclasses.fields() introspection.
Handles nested Serializable objects, lists of them, and Path coercion.

All deserialization is lenient by default: missing fields that have defaults
are silently skipped. This ensures forward-compatibility when older serialized
data lacks newer fields.
"""

import dataclasses
from pathlib import Path
from typing import get_args, get_origin, get_type_hints


class Serializable:
    """Mixin that adds to_dict() and from_dict() to dataclasses.

    Usage:
        @dataclass
        class MyConfig(Serializable):
            name: str
            count: int = 0

        d = MyConfig("x", 5).to_dict()   # {"name": "x", "count": 5}
        obj = MyConfig.from_dict(d)       # MyConfig(name="x", count=5)

    Set `_skip_none = True` on the class to omit None-valued fields from to_dict().
    """

    # Subclasses can set this to True to omit None-valued fields from to_dict()
    _skip_none: bool = False

    def to_dict(self) -> dict:
        result = {}
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            if self._skip_none and value is None:
                continue
            result[f.name] = _serialize(value)
        return result

    @classmethod
    def from_dict(cls, d: dict) -> "Serializable":
        hints = get_type_hints(cls)
        kwargs = {}
        for f in dataclasses.fields(cls):  # type: ignore[arg-type]
            if f.name not in d:
                # Skip missing fields that have defaults (lenient)
                if f.default is not dataclasses.MISSING:
                    continue
                if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                    continue
                # Required field missing — let the constructor raise
                continue
            value = d[f.name]
            field_type = hints.get(f.name)
            kwargs[f.name] = _deserialize(value, field_type)
        return cls(**kwargs)


def _serialize(value):
    """Recursively serialize a value."""
    if isinstance(value, Serializable):
        return value.to_dict()
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    return value


def _deserialize(value, field_type):
    """Deserialize a value according to its type hint."""
    if value is None:
        return None

    # Unwrap Optional (X | None)
    actual_type = _unwrap_optional(field_type)

    # Handle nested Serializable
    if (
        isinstance(value, dict)
        and isinstance(actual_type, type)
        and issubclass(actual_type, Serializable)
    ):
        return actual_type.from_dict(value)

    # Handle list[Serializable]
    if isinstance(value, list):
        inner = _get_list_inner_type(actual_type)
        if inner and isinstance(inner, type) and issubclass(inner, Serializable):
            return [inner.from_dict(v) if isinstance(v, dict) else v for v in value]
        return value

    # Handle Path
    if actual_type is Path and isinstance(value, str):
        return Path(value)

    return value


def _unwrap_optional(tp):
    """Unwrap X | None to X."""
    origin = get_origin(tp)
    if origin is type(int | str):  # types.UnionType for X | Y syntax
        args = get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return tp


def _get_list_inner_type(tp):
    """Extract T from list[T]."""
    origin = get_origin(tp)
    if origin is list:
        args = get_args(tp)
        if args:
            return args[0]
    return None
