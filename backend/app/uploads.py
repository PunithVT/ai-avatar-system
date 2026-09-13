"""
Shared upload handling.

`read_capped` lives here rather than beside one endpoint because the naive
alternative — `await file.read()` followed by a length check — is easy to
write, looks correct, and lets a client force the server to buffer an
arbitrarily large body before it is rejected. That bug was fixed on the avatar
endpoint and then written again on the voice endpoint, which is exactly what a
shared helper prevents.
"""

import logging

from fastapi import HTTPException, UploadFile, status

logger = logging.getLogger(__name__)

# Read granularity. Large enough that a 10 MB upload is a couple of hundred
# reads, small enough that peak memory stays close to the limit itself.
_CHUNK_SIZE = 64 * 1024


async def read_capped(file: UploadFile, limit: int, *, what: str = "File") -> bytes:
    """
    Read an upload in chunks, aborting as soon as it exceeds `limit`.

    Caps peak memory at `limit` + one chunk regardless of what the client
    sends, and reports 413 rather than 400 — the request is well-formed, it is
    simply too big.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            logger.warning(f"Rejected oversized upload: exceeded {limit} bytes")
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"{what} must be under {limit // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    return b"".join(chunks)
