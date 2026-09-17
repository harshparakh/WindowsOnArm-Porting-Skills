from __future__ import annotations

import base64
import binascii
import gzip
import io
import json

from .core import WorkbenchError


MAX_PATCH_BYTES = 250000
MAX_WIRE_BYTES = 50000
PREFIX = "gzip64:"


def encode_patch(patch: str) -> str:
    raw = patch.encode("utf-8")
    if len(raw) > MAX_PATCH_BYTES:
        raise WorkbenchError("The reviewed text patch exceeds the 250 KB source limit.")
    if len(json.dumps(patch, ensure_ascii=False).encode("utf-8")) <= MAX_WIRE_BYTES:
        return patch
    value = PREFIX + base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    if len(value) > MAX_WIRE_BYTES:
        raise WorkbenchError("The patch cannot fit the bounded workflow input after compression.")
    return value


def decode_patch(value: str) -> str:
    if len(value.encode("utf-8")) > MAX_WIRE_BYTES:
        raise WorkbenchError("The patch transport exceeds the workflow input limit.")
    if not value.startswith(PREFIX):
        return value
    try:
        compressed = base64.b64decode(value[len(PREFIX):], validate=True)
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(MAX_PATCH_BYTES + 1)
        if len(raw) > MAX_PATCH_BYTES:
            raise WorkbenchError("The compressed patch exceeds the expanded source limit.")
        return raw.decode("utf-8")
    except (binascii.Error, OSError, EOFError, UnicodeError) as error:
        raise WorkbenchError(f"Invalid compressed patch transport: {error}") from error
