"""Bounded-depth JSON decoding for untrusted remote-provider responses."""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_JSON_DEPTH = 64


class JSONNestingTooDeepError(ValueError):
    """The JSON document exceeds the provider-response nesting limit."""


def loads_bounded_depth(
    raw_text: str, *, max_depth: int = DEFAULT_MAX_JSON_DEPTH
) -> Any:
    """Decode JSON after bounding structural depth independently of byte size.

    The pre-scan counts object and array delimiters only outside JSON strings, so
    braces in provider-supplied text do not consume the depth budget. Invalid JSON
    is still rejected by :func:`json.loads` after this resource bound is enforced.
    """
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")

    depth = 0
    in_string = False
    escaped = False
    for char in raw_text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise JSONNestingTooDeepError(
                    f"JSON nesting exceeds maximum depth of {max_depth}"
                )
        elif char in "]}":
            # Do not let unmatched closing delimiters create a negative baseline
            # that could conceal a later deeply nested fragment.
            depth = max(0, depth - 1)

    try:
        return json.loads(raw_text)
    except RecursionError as exc:
        # Keep implementation-specific decoder recursion failures inside the same
        # ValueError boundary used by provider protocol-error handling.
        raise JSONNestingTooDeepError(
            f"JSON nesting exceeds safe decoder depth of {max_depth}"
        ) from exc
