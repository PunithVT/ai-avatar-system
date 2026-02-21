"""
Voice management endpoints — clone, list, and delete voice profiles.
Voice profiles are stored as WAV reference files on disk and referenced
by ID when calling the TTS service.
"""

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse
import uuid
import shutil
import json
import logging
from pathlib import Path
from typing import Optional
import soundfile as sf
import io

logger = logging.getLogger(__name__)

router = APIRouter()

VOICE_DIR = Path("voice_profiles")
VOICE_DIR.mkdir(parents=True, exist_ok=True)

VOICE_INDEX = VOICE_DIR / "index.json"


def _load_index() -> list[dict]:
    if VOICE_INDEX.exists():
        try:
            return json.loads(VOICE_INDEX.read_text())
        except Exception:
            return []
    return []


def _save_index(data: list[dict]) -> None:
    VOICE_INDEX.write_text(json.dumps(data, indent=2, default=str))


@router.post("/clone")
async def clone_voice(
    audio: UploadFile = File(..., description="Audio sample (WAV/WebM/MP3, 5-30 seconds)"),
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
        except Exception:
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

        # Measure duration
        try:
            info = sf.info(str(wav_path))
            duration = round(info.duration, 1)
        except Exception:
            duration = 0.0

        if duration < 3:
            wav_path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=400,
                detail=f"Sample too short ({duration}s). Please record at least 5 seconds."
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
    }
    index = _load_index()
    index.append(entry)
    _save_index(index)

    logger.info(f"Voice profile created: {name} ({voice_id}, {duration}s)")
    return JSONResponse({"id": voice_id, "name": name, "language": language, "duration": duration})


@router.get("/")
async def list_voices():
    """List all custom voice profiles."""
    return _load_index()


@router.get("/{voice_id}")
async def get_voice(voice_id: str):
    """Get a single voice profile by ID."""
    for entry in _load_index():
        if entry["id"] == voice_id:
            return entry
    raise HTTPException(status_code=404, detail="Voice profile not found")


@router.delete("/{voice_id}")
async def delete_voice(voice_id: str):
    """Delete a voice profile and its audio file."""
    index = _load_index()
    entry = next((e for e in index if e["id"] == voice_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Voice profile not found")

    # Remove WAV file
    wav = Path(entry["wav_path"])
    wav.unlink(missing_ok=True)

    # Remove from index
    _save_index([e for e in index if e["id"] != voice_id])
    return {"deleted": voice_id}


@router.get("/{voice_id}/wav_path")
async def get_voice_wav_path(voice_id: str) -> str:
    """
    Internal helper: return the filesystem path to the voice WAV file
    so that the TTS service can pass it as speaker_wav.
    """
    for entry in _load_index():
        if entry["id"] == voice_id:
            return entry["wav_path"]
    raise HTTPException(status_code=404, detail="Voice profile not found")
