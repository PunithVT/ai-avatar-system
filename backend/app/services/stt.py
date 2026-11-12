"""
Speech-to-text service backed by faster-whisper.

The Whisper model is several hundred MB and takes 30–60 s to load on a cold
start. We defer loading until the first transcription so FastAPI's lifespan
hook stays fast and the /health endpoint becomes available promptly.
"""

from __future__ import annotations

import asyncio
import contextvars
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Union

import numpy as np
import soundfile as sf

from app.config import settings

logger = logging.getLogger(__name__)

# Stream-microphone transcriptions allowed inside the model at once, across
# every session.
#
# Server-side turn-taking multiplies the calls: every utterance can cost a
# speculative final and, on a GPU, a running partial every second or so, all
# over audio that is already decoded. Left on asyncio's default pool (up to
# 32 threads) they would all contend for the same GPU or the same CPU cores,
# so THOSE calls are bounded.
#
# Recordings (push-to-talk, the browser's own hands-free: a path or encoded
# bytes) are not: they run on the default pool exactly as they always have.
# There is one of them per turn, and capping them changed how a server with
# no stream microphone at all behaves under a burst of uploads.
#
# Two, not one: CTranslate2 runs one decode at a time per model anyway, but
# feature extraction and Whisper's VAD filter are Python and overlap
# usefully. More than two only adds contention. The rest wait in line, in
# arrival order.
MAX_CONCURRENT_TRANSCRIPTIONS = 2

AudioInput = Union[bytes, str, np.ndarray]


class STTService:
    def __init__(self):
        self.provider = settings.STT_PROVIDER
        self.model_name = settings.WHISPER_MODEL
        self.model = None
        # "cuda" or "cpu" once the model is built.
        self.device: Optional[str] = None
        # Lock ensures the model is loaded exactly once even under burst load.
        self._load_lock: Optional[asyncio.Lock] = None
        # The bound is the pool's size rather than a semaphore around
        # to_thread: a caller that is cancelled (barge-in) stops waiting, but
        # its thread runs on to the end — Whisper cannot be interrupted — and
        # a semaphore released on cancellation would let the next call in
        # beside it. A slot here is free only when its thread really is.
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_TRANSCRIPTIONS, thread_name_prefix="stt"
        )
        self._inflight = 0
        # Recordings inside the model right now (they have no slot to hold;
        # counted so `idle` can see them).
        self._recordings = 0
        self._inflight_lock = threading.Lock()

    def _check_cuda(self) -> bool:
        try:
            import torch

            return torch.cuda.is_available()
        except Exception:
            return False

    def _build_model(self):
        """Synchronous model load — run inside a thread to avoid blocking the loop."""
        from faster_whisper import WhisperModel

        device = "cuda" if self._check_cuda() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info(f"Loading Whisper model {self.model_name!r} on {device} ({compute_type})…")
        model = WhisperModel(self.model_name, device=device, compute_type=compute_type)
        self.device = device
        logger.info("Whisper model loaded")
        return model

    @property
    def on_gpu(self) -> bool:
        """Whether transcription runs (or, before the model loads, will run) on a GPU."""
        if self.device is not None:
            return self.device == "cuda"
        return self._check_cuda()

    @property
    def busy(self) -> bool:
        """True when a stream-microphone transcription started now would have to wait for a slot."""
        return self._inflight >= MAX_CONCURRENT_TRANSCRIPTIONS

    @property
    def idle(self) -> bool:
        """True when nothing at all is being transcribed, of either kind."""
        return self._inflight == 0 and self._recordings == 0

    async def initialize(self) -> None:
        """Eager warm-up. Optional — `transcribe` will load on first call too."""
        if self.model is not None or self.provider != "whisper":
            return
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()
        async with self._load_lock:
            if self.model is None:
                self.model = await asyncio.to_thread(self._build_model)

    async def warm(self) -> None:
        """Load the model ahead of the first voice turn. Never raises."""
        try:
            await self.initialize()
        except Exception as e:
            logger.warning(f"STT warm-up skipped: {e}")

    async def transcribe(self, audio_data: AudioInput, language: str = "en") -> str:
        """
        Transcribe a file path, encoded bytes (any container PyAV reads), or
        samples already decoded: a mono numpy array at 16 kHz, int16 or
        float in [-1, 1].
        """
        if self.provider != "whisper":
            raise ValueError(f"Unsupported STT provider: {self.provider}")
        if self.model is None:
            await self.initialize()

        if not isinstance(audio_data, np.ndarray):
            # A recording: the default pool, unbounded, as before the stream
            # microphone existed (see MAX_CONCURRENT_TRANSCRIPTIONS).
            return await asyncio.to_thread(self._transcribe_recording, audio_data, language)

        def _finished(_future) -> None:
            with self._inflight_lock:
                self._inflight -= 1

        with self._inflight_lock:
            self._inflight += 1
        try:
            # The caller's context goes with it, as asyncio.to_thread would
            # carry it (tracing spans hang off context variables).
            job = self._executor.submit(
                contextvars.copy_context().run, self._transcribe_sync, audio_data, language
            )
        except BaseException:
            _finished(None)
            raise
        job.add_done_callback(_finished)
        return await asyncio.wrap_future(job)

    def _transcribe_recording(self, audio_data: Union[bytes, str], language: str) -> str:
        # Counted on the thread itself, so the count is of calls really
        # inside the model: a caller cancelled mid-way does not end it early,
        # and one cancelled before its thread started never adds to it.
        with self._inflight_lock:
            self._recordings += 1
        try:
            return self._transcribe_sync(audio_data, language)
        finally:
            with self._inflight_lock:
                self._recordings -= 1

    def _decode_with_soundfile(self, audio_data: Union[bytes, str]) -> np.ndarray:
        """Fallback decoder for formats PyAV chokes on (raw WAV/FLAC/OGG)."""
        if isinstance(audio_data, bytes):
            audio, sample_rate = sf.read(io.BytesIO(audio_data))
        else:
            audio, sample_rate = sf.read(audio_data)

        # Mono mixdown
        if len(audio.shape) > 1:
            audio = audio.mean(axis=1)

        # Resample to 16 kHz (Whisper's expected rate)
        if sample_rate != 16000:
            import librosa

            audio = librosa.resample(audio, orig_sr=sample_rate, target_sr=16000)

        return audio.astype(np.float32)

    @staticmethod
    def _as_samples(audio: np.ndarray) -> np.ndarray:
        """Decoded samples in the form faster-whisper takes: float32, 1-D, [-1, 1]."""
        if audio.ndim > 1:
            # (samples, channels), as soundfile lays it out.
            audio = audio.mean(axis=1)
        if np.issubdtype(audio.dtype, np.integer):
            return audio.astype(np.float32) / 32768.0
        return np.ascontiguousarray(audio, dtype=np.float32)

    def _transcribe_sync(self, audio_data: AudioInput, language: str) -> str:
        try:
            assert self.model is not None  # for type checker

            if isinstance(audio_data, np.ndarray):
                # Already decoded (the WebRTC microphone): nothing for PyAV to
                # do, and no second decoder to fall back to.
                segments, info = self.model.transcribe(
                    self._as_samples(audio_data),
                    language=language,
                    beam_size=settings.WHISPER_BEAM_SIZE,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
                transcription = " ".join(seg.text for seg in segments).strip()
                logger.info(f"Transcribed {len(transcription)} chars (lang={info.language})")
                return transcription

            # Hand the path / raw bytes straight to faster-whisper: it decodes
            # via PyAV, which handles the browser's WebM/Opus mic recordings
            # (libsndfile can NOT — decoding with soundfile first broke every
            # voice turn coming from MediaRecorder). PyAV also resamples to
            # 16 kHz mono internally, so no librosa pass is needed.
            source = io.BytesIO(audio_data) if isinstance(audio_data, bytes) else audio_data
            try:
                segments, info = self.model.transcribe(
                    source,
                    language=language,
                    beam_size=settings.WHISPER_BEAM_SIZE,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
                transcription = " ".join(seg.text for seg in segments).strip()
            except Exception as decode_err:
                logger.warning(f"PyAV decode failed ({decode_err}); retrying via soundfile")
                audio = self._decode_with_soundfile(audio_data)
                segments, info = self.model.transcribe(
                    audio,
                    language=language,
                    beam_size=settings.WHISPER_BEAM_SIZE,
                    vad_filter=True,
                    vad_parameters=dict(min_silence_duration_ms=500),
                )
                transcription = " ".join(seg.text for seg in segments).strip()

            logger.info(f"Transcribed {len(transcription)} chars (lang={info.language})")
            return transcription

        except Exception as e:
            logger.error(f"Whisper transcription error: {e}")
            raise


stt_service = STTService()
