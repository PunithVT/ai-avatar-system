"""
Voice management endpoints — clone, list, and delete voice profiles.
Voice profiles are stored as WAV reference files on disk and referenced
by ID when calling the TTS service.
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
import asyncio
import uuid
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import soundfile as sf
import io

from sqlalchemy import select, update

logger = logging.getLogger(__name__)

router = APIRouter()

VOICE_DIR = Path("voice_profiles")
VOICE_DIR.mkdir(parents=True, exist_ok=True)

VOICE_INDEX = VOICE_DIR / "index.json"

# Serialize concurrent index reads/writes to prevent corruption
_index_lock = asyncio.Lock()

MIN_DURATION_SECS = 5   # Minimum sample length accepted


async def _load_index() -> list[dict]:
    async with _index_lock:
        if VOICE_INDEX.exists():
            try:
                return json.loads(VOICE_INDEX.read_text())
            except Exception:
                return []
        return []


async def _save_index(data: list[dict]) -> None:
    async with _index_lock:
        # Write to a temp file then rename (atomic on POSIX)
        tmp = VOICE_INDEX.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(VOICE_INDEX)


@router.post("/clone")
async def clone_voice(
    audio: UploadFile = File(..., description="Audio sample (WAV/WebM/MP3, 5–30 seconds)"),
    name: str = Form(..., description="Display name for the voice profile"),
    language: Optional[str] = Form("en"),
):
    """
    Accept an audio sample and create a named voice profile that the TTS
    service can later use as a speaker reference for voice cloning.
    """
    voice_id = str(uuid.uuid4())
    audio_bytes = await audio.read()

    if len(audio_bytes) < 1000:
        raise HTTPException(status_code=400, detail="Audio sample too short or empty")

    # Convert to WAV if needed and save as reference file
    try:
        wav_path = VOICE_DIR / f"{voice_id}.wav"

        # Try reading with soundfile (handles WAV, FLAC, OGG)
        try:
            buf = io.BytesIO(audio_bytes)
            data, samplerate = sf.read(buf)
            sf.write(str(wav_path), data, samplerate)
        except Exception as sf_err:
            logger.debug(f"soundfile could not read audio, falling back to ffmpeg: {sf_err}")
            # Fallback: save raw bytes and let ffmpeg handle conversion
            raw_path = VOICE_DIR / f"{voice_id}_raw"
            raw_path.write_bytes(audio_bytes)
            try:
                import subprocess
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(raw_path), "-ar", "22050", "-ac", "1", str(wav_path)],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                raw_path.unlink(missing_ok=True)
            except Exception as ffmpeg_err:
                raw_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=422,
                    detail=f"Could not decode audio: {ffmpeg_err}. Please upload WAV or WebM."
                )

        # Validate the output WAV is actually readable
        try:
            info = sf.info(str(wav_path))
            duration = round(info.duration, 1)
        except Exception as e:
            wav_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail=f"Converted audio file is not a valid WAV: {e}"
            )

        if duration < MIN_DURATION_SECS:
            wav_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"Sample too short ({duration}s). Please record at least {MIN_DURATION_SECS} seconds."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Voice cloning storage error")
        raise HTTPException(status_code=500, detail=f"Failed to process audio: {e}")

    # Persist to index
    entry = {
        "id": voice_id,
        "name": name.strip(),
        "language": language or "en",
        "wav_path": str(wav_path),
        "duration": duration,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    index = await _load_index()
    index.append(entry)
    await _save_index(index)

    logger.info(f"Voice profile created: {name!r} ({voice_id}, {duration}s)")
    return JSONResponse({
        "id": voice_id,
        "name": name.strip(),
        "language": language or "en",
        "duration": duration,
        "created_at": entry["created_at"],
    })


@router.get("/")
async def list_voices():
    """List all custom voice profiles."""
    return await _load_index()


@router.get("/{voice_id}")
async def get_voice(voice_id: str):
    """Get a single voice profile by ID."""
    for entry in await _load_index():
        if entry["id"] == voice_id:
            return entry
    raise HTTPException(status_code=404, detail="Voice profile not found")


@router.delete("/{voice_id}")
async def delete_voice(voice_id: str):
    """Delete a voice profile, its audio file, and clear any avatar references."""
    index = await _load_index()
    entry = next((e for e in index if e["id"] == voice_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Voice profile not found")

    # Remove WAV file
    Path(entry["wav_path"]).unlink(missing_ok=True)

    # Remove from index
    await _save_index([e for e in index if e["id"] != voice_id])

    # Clear voice_id from any avatars that reference this profile
    cleared = 0
    try:
        from app.database import AsyncSessionLocal
        from app.models import Avatar
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                update(Avatar)
                .where(Avatar.voice_id == voice_id)
                .values(voice_id=None)
            )
            cleared = result.rowcount or 0
            await db.commit()
    except Exception as e:
        logger.warning(f"Could not clear avatar voice references for {voice_id}: {e}")

    logger.info(f"Voice profile deleted: {voice_id} (cleared from {cleared} avatar(s))")
    return {"deleted": voice_id, "avatars_cleared": cleared}


@router.get("/{voice_id}/wav_path")
async def get_voice_wav_path(voice_id: str) -> str:
    """
    Internal helper: return the filesystem path to the voice WAV file
    so that the TTS service can pass it as speaker_wav.
    Raises 404 if the voice or its file no longer exists.
    """
    for entry in await _load_index():
        if entry["id"] == voice_id:
            wav = Path(entry["wav_path"])
            if not wav.exists():
                raise HTTPException(
                    status_code=404,
                    detail=f"WAV file for voice {voice_id} is missing from disk"
                )
            return entry["wav_path"]
    raise HTTPException(status_code=404, detail="Voice profile not found")
