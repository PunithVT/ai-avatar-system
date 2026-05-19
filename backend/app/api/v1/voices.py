"""
Voice management endpoints — clone, list, and delete voice profiles.

Voice profiles are stored as WAV reference files on disk plus an index.json
metadata file. Each profile is owned by the user who created it (or `demo-user`
in unauthenticated dev mode); the list/get/delete endpoints filter by owner so
users cannot see or mutate someone else's voices.
"""

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, JSONResponse
import asyncio
import subprocess
import uuid
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import soundfile as sf
import io

from sqlalchemy import select, update

from app.api.v1.users import get_current_user
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

VOICE_DIR = Path("voice_profiles")
VOICE_DIR.mkdir(parents=True, exist_ok=True)

VOICE_INDEX = VOICE_DIR / "index.json"

# Serialize concurrent index reads/writes to prevent corruption
_index_lock = asyncio.Lock()

# Chatterbox recommends ≥10s reference audio for clean cloning, and most
# cloning quality plateaus well before 60s — anything longer just wastes disk
# and creates a DoS vector for large uploads.
MIN_DURATION_SECS = 10
MAX_DURATION_SECS = 60
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB hard cap
# Chatterbox Multilingual supports 23 languages — keep this in sync with
# https://www.resemble.ai/introducing-chatterbox-multilingual-open-source-tts-for-23-languages/
_ALLOWED_LANGUAGES = {
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja",
    "ko", "ms", "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr", "zh",
}
_NAME_MAX_LEN = 100


def _user_id(current_user: Optional[User]) -> str:
    return current_user.id if current_user else "demo-user"


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


def _owned(entry: dict, uid: str) -> bool:
    """Treat legacy entries (no user_id) as belonging to demo-user."""
    return entry.get("user_id", "demo-user") == uid


@router.post("/clone")
async def clone_voice(
    audio: UploadFile = File(..., description="Audio sample (WAV/WebM/MP3, 10–60 seconds)"),
    name: str = Form(..., description="Display name for the voice profile"),
    language: Optional[str] = Form("en"),
    current_user: Optional[User] = Depends(get_current_user),
):
    """
    Accept an audio sample and create a named voice profile owned by the
    current user. The TTS service can later use the stored WAV as a speaker
    reference for zero-shot voice cloning.
    """
    name = name.strip()
    if not name or len(name) > _NAME_MAX_LEN:
        raise HTTPException(status_code=400, detail=f"Name must be 1–{_NAME_MAX_LEN} characters")

    lang = (language or "en").strip().lower()
    if lang not in _ALLOWED_LANGUAGES:
        raise HTTPException(status_code=400, detail=f"Unsupported language '{lang}'")

    voice_id = str(uuid.uuid4())
    audio_bytes = await audio.read()

    if len(audio_bytes) < 1000:
        raise HTTPException(status_code=400, detail="Audio sample too short or empty")
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )

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
                await asyncio.to_thread(
                    subprocess.run,
                    # 24kHz mono — matches Chatterbox's recommended reference format
                    ["ffmpeg", "-y", "-i", str(raw_path), "-ar", "24000", "-ac", "1", str(wav_path)],
                    capture_output=True,
                    check=True,
                    timeout=30,
                )
                raw_path.unlink(missing_ok=True)
            except subprocess.CalledProcessError as e:
                logger.warning(f"ffmpeg conversion failed for {voice_id}: {e.stderr.decode(errors='replace')}")
                raw_path.unlink(missing_ok=True)
                raise HTTPException(status_code=422, detail="Could not decode audio. Please upload WAV or WebM.")
            except Exception as ffmpeg_err:
                logger.warning(f"ffmpeg error for {voice_id}: {ffmpeg_err}")
                raw_path.unlink(missing_ok=True)
                raise HTTPException(status_code=422, detail="Could not decode audio. Please upload WAV or WebM.")

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
        if duration > MAX_DURATION_SECS:
            wav_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"Sample too long ({duration}s). Maximum {MAX_DURATION_SECS} seconds allowed."
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Voice cloning storage error")
        raise HTTPException(status_code=500, detail="Failed to process audio")

    # Persist to index
    uid = _user_id(current_user)
    entry = {
        "id": voice_id,
        "name": name,
        "language": lang,
        "wav_path": str(wav_path),
        "duration": duration,
        "user_id": uid,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    index = await _load_index()
    index.append(entry)
    await _save_index(index)

    logger.info(f"Voice profile created: {name!r} ({voice_id}, {duration}s, user={uid})")
    return JSONResponse({
        "id": voice_id,
        "name": name,
        "language": lang,
        "duration": duration,
        "created_at": entry["created_at"],
    })


@router.get("/")
async def list_voices(current_user: Optional[User] = Depends(get_current_user)):
    """List voice profiles owned by the current user."""
    uid = _user_id(current_user)
    return [
        {k: v for k, v in e.items() if k != "wav_path"}
        for e in await _load_index()
        if _owned(e, uid)
    ]


@router.get("/{voice_id}")
async def get_voice(
    voice_id: str,
    current_user: Optional[User] = Depends(get_current_user),
):
    """Get a single voice profile by ID (must own it)."""
    uid = _user_id(current_user)
    for entry in await _load_index():
        if entry["id"] == voice_id:
            if not _owned(entry, uid):
                raise HTTPException(status_code=403, detail="Not authorised to access this voice")
            return {k: v for k, v in entry.items() if k != "wav_path"}
    raise HTTPException(status_code=404, detail="Voice profile not found")


@router.get("/{voice_id}/preview")
async def preview_voice(
    voice_id: str,
    current_user: Optional[User] = Depends(get_current_user),
):
    """Stream the original reference WAV so the UI can preview a cloned voice."""
    uid = _user_id(current_user)
    for entry in await _load_index():
        if entry["id"] == voice_id:
            if not _owned(entry, uid):
                raise HTTPException(status_code=403, detail="Not authorised to access this voice")
            wav = Path(entry["wav_path"])
            if not wav.exists():
                raise HTTPException(status_code=404, detail="WAV file missing")
            return FileResponse(
                str(wav),
                media_type="audio/wav",
                filename=f"{entry['name']}.wav",
            )
    raise HTTPException(status_code=404, detail="Voice profile not found")


@router.delete("/{voice_id}")
async def delete_voice(
    voice_id: str,
    current_user: Optional[User] = Depends(get_current_user),
):
    """Delete a voice profile (owner only), its audio file, and clear any avatar references."""
    uid = _user_id(current_user)
    index = await _load_index()
    entry = next((e for e in index if e["id"] == voice_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Voice profile not found")
    if not _owned(entry, uid):
        raise HTTPException(status_code=403, detail="Not authorised to delete this voice")

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
