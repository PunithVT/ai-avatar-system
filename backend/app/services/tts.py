"""
Text-to-Speech service backed by Chatterbox Multilingual (Resemble AI).

Replaces the deprecated Coqui XTTS v2. Voice profile WAVs in `voice_profiles/`
remain compatible — Chatterbox accepts any WAV reference for zero-shot cloning.

Fallback chain when Chatterbox can't load or fails mid-synthesis:

    chatterbox (cloned voice, GPU) → edge-tts (neural, free, no GPU) → gTTS

Edge TTS uses Microsoft's neural voices — dramatically better prosody than
gTTS — and needs no API key or GPU, so the degraded experience stays good.
gTTS remains as the last-ditch network fallback.

Synthesis result reports which engine produced the audio so the caller can
notify the user when voice cloning was silently dropped during fallback.

TTS_PROVIDER picks where the chain starts:

    chatterbox  the full chain above (default)
    edge        start at Edge TTS — the right setting for an install without
                the Chatterbox package, which otherwise re-attempted the import
                and logged an error on every sentence of every reply
    gtts        gTTS only
    supertonic  Supertonic 3, a local ONNX model on the CPU: no network once
                its weights are cached, so speech does not depend on an
                unofficial endpoint staying up. For the languages it does not
                speak, and any sentence it refuses, the Edge -> gTTS chain
                above. No voice cloning. Optional rather than the default
                because of its weights' licence — see SUPERTONIC_VOICE in
                config.py.
"""

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import torch
import torchaudio

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SynthResult:
    output_path: str
    engine: str  # "chatterbox", "supertonic", "edge-tts" or "gtts"
    fallback: bool  # True if the caller's preferred path was not taken
    voice_cloned: bool  # True if a speaker WAV was actually applied


# Microsoft neural voices for the Edge TTS fallback, one per supported
# language (the same 23-language set the voices API allows). Anything not
# listed falls back to the English voice.
_EDGE_VOICES = {
    "ar": "ar-SA-ZariyahNeural",
    "da": "da-DK-ChristelNeural",
    "de": "de-DE-KatjaNeural",
    "el": "el-GR-AthinaNeural",
    "en": "en-US-AriaNeural",
    "es": "es-ES-ElviraNeural",
    "fi": "fi-FI-NooraNeural",
    "fr": "fr-FR-DeniseNeural",
    "he": "he-IL-HilaNeural",
    "hi": "hi-IN-SwaraNeural",
    "it": "it-IT-ElsaNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "ms": "ms-MY-YasminNeural",
    "nl": "nl-NL-ColetteNeural",
    "no": "nb-NO-PernilleNeural",
    "pl": "pl-PL-ZofiaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "sv": "sv-SE-SofieNeural",
    "sw": "sw-KE-ZuriNeural",
    "tr": "tr-TR-EmelNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
}

# The app's languages Supertonic 3 speaks. The other five (he, ms, no, sw, zh)
# go to the Edge chain: the package rejects "zh" outright and has no code for
# the rest, and its "na" (unknown language) mode is untested guesswork.
SUPERTONIC_LANGUAGES = frozenset(
    {
        "ar", "da", "de", "el", "en", "es", "fi", "fr", "hi",
        "it", "ja", "ko", "nl", "pl", "pt", "ru", "sv", "tr",
    }
)  # fmt: skip

# Supertonic renders at 44.1 kHz. Nothing downstream needs a particular rate
# — MuseTalk loads its audio at 16 kHz itself, ffmpeg and the stream decoder
# resample whatever they are given — so the file is written at the rate of
# the one consumer where time matters: the WebRTC stream (48 kHz), which with
# the still-image engine takes this file straight onto the audio track.
SUPERTONIC_OUTPUT_RATE = 48000


class NothingToSay(ValueError):
    """The text has nothing in it that can be spoken; see `sayable`."""


def sayable(text: str) -> bool:
    """
    Whether there is anything in `text` to say: a letter or a digit, in any
    script (str.isalnum is true for all of them).

    A reply's last piece is often just an emoji, a dash or "...", split off
    behind the sentence it decorated. No engine has words for it, and what
    each does instead is worse than nothing: Supertonic returns 1.3 s of
    hiss or a grunt that Whisper hears as "And", the networked engines spend
    a round trip to fail or to read the punctuation's name aloud. Asked here,
    once, so that no engine is handed such text — and so the pipeline can
    ask the same question before it makes work of the sentence.
    """
    return any(ch.isalnum() for ch in text or "")


# Older configs may still say "coqui"/"xtts" (the engine Chatterbox replaced).
# Treat them as chatterbox instead of breaking every synthesis call.
_LEGACY_PROVIDER_ALIASES = {"coqui": "chatterbox", "xtts": "chatterbox", "xtts_v2": "chatterbox"}


class TTSService:
    """Text-to-Speech service. Lazy-loads the model on first synthesis."""

    def __init__(self):
        provider = settings.TTS_PROVIDER
        if provider in _LEGACY_PROVIDER_ALIASES:
            logger.warning(
                f"TTS_PROVIDER={provider!r} is deprecated (Coqui XTTS was replaced "
                f"by Chatterbox) — using 'chatterbox'. Update your .env."
            )
            provider = _LEGACY_PROVIDER_ALIASES[provider]
        self.provider = provider
        self.model = None
        self._init_lock = asyncio.Lock()
        # Set once the chatterbox package turns out not to be installed, so it
        # is discovered once rather than on every sentence.
        self._chatterbox_missing = False
        # One generation at a time. The model is a single object holding the
        # CURRENT speaker's conditioning as mutable state; two sessions
        # generating at once would each be reading and replacing it.
        self._generate_lock = threading.Lock()
        # Conditioning captured from the freshly-loaded model (its built-in
        # voice), and per reference recording for cloned voices.
        self._default_conds = None
        self._conds_cache: dict = {}
        # Supertonic (TTS_PROVIDER=supertonic): the loaded engine and voice.
        self._supertonic = None
        self._supertonic_style = None
        # Set when it cannot be loaded (package or weights missing, download
        # failed): found out once, not per sentence, as with Chatterbox.
        self._supertonic_unavailable = False
        # One synthesis at a time. Each already spreads over every core, so
        # two at once finish no sooner and both first sentences arrive late.
        self._supertonic_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        """Whether the configured local model is in memory (for /health)."""
        return self.model is not None or self._supertonic is not None

    def _check_cuda(self) -> bool:
        try:
            return torch.cuda.is_available()
        except Exception:
            return False

    async def initialize(self):
        """Load the Chatterbox model (downloaded from HuggingFace on first run).

        Guarded by a lock — without it, concurrent WebSocket sessions each
        call initialize() before the first one finishes, and they all race
        to download/load the same multi-GB model at once, fighting over the
        same HuggingFace cache lock files (worse on Windows, where the cache
        already runs in degraded no-symlink mode).
        """
        if self.model is not None:
            return

        async with self._init_lock:
            if self.model is not None:  # re-check: another coroutine may have just finished
                return

            if self.provider != "chatterbox":
                raise ValueError(f"Unsupported TTS provider: {self.provider}")
            if self._chatterbox_missing:
                raise ModuleNotFoundError("chatterbox is not installed")

            try:
                from chatterbox.mtl_tts import ChatterboxMultilingualTTS
            except ModuleNotFoundError as e:
                missing = e.name or ""
                if missing == "chatterbox" or missing.startswith("chatterbox."):
                    self._chatterbox_missing = True
                    logger.warning(
                        "TTS_PROVIDER=chatterbox but the chatterbox package is not installed — "
                        "using Edge TTS. Install requirements-tts.txt for voice cloning, or set "
                        "TTS_PROVIDER=edge to make this the configured engine."
                    )
                else:
                    # chatterbox IS installed but one of its own dependencies
                    # is not. Say which: reporting this as "chatterbox is not
                    # installed" sends whoever reads the log to reinstall the
                    # wrong thing.
                    logger.error(f"Chatterbox is installed but failed to import: {e}")
                raise

            try:
                device = "cuda" if self._check_cuda() else "cpu"
                logger.info(f"Loading Chatterbox multilingual TTS on {device}...")
                self.model = await asyncio.to_thread(
                    ChatterboxMultilingualTTS.from_pretrained, device=device
                )
                self._default_conds = getattr(self.model, "conds", None)
                logger.info(f"Chatterbox loaded (sr={self.model.sr}, device={device})")

            except Exception as e:
                logger.error(f"Failed to load Chatterbox: {e}")
                raise

    async def warm(self) -> None:
        """Load the configured model ahead of the first turn. Never raises."""
        try:
            if self.provider == "supertonic":
                await self._ensure_supertonic()
            elif self.provider == "chatterbox":
                await self.initialize()
        except Exception as e:
            logger.warning(f"TTS warm-up skipped: {e}")

    # ── Supertonic ────────────────────────────────────────────────────────────

    @staticmethod
    def _load_supertonic():
        """Import and load the engine and the configured voice. Blocking."""
        from supertonic import TTS

        # Fetches the weights (~400 MB, from Hugging Face) the first time,
        # into ~/.cache/supertonic3 or SUPERTONIC_CACHE_DIR.
        engine = TTS(auto_download=True)
        voice = settings.SUPERTONIC_VOICE
        try:
            style = engine.get_voice_style(voice)
        except Exception as e:
            # A typo in the voice name should cost the voice, not the engine.
            logger.warning(f"SUPERTONIC_VOICE={voice!r} could not be loaded ({e}); using F1")
            style = engine.get_voice_style("F1")
        return engine, style

    async def _ensure_supertonic(self):
        """The loaded engine. Raises if it cannot be had — once; see below."""
        if self._supertonic is not None:
            return self._supertonic
        async with self._init_lock:
            if self._supertonic is not None:
                return self._supertonic
            if self._supertonic_unavailable:
                raise RuntimeError("Supertonic is unavailable")
            try:
                logger.info("Loading Supertonic TTS (downloads its weights on first use)...")
                engine, style = await asyncio.to_thread(self._load_supertonic)
            except Exception as e:
                # Not retried: a failed load is a missing package or a failed
                # 400 MB download, and trying again for every sentence would
                # put that wait in front of each one.
                self._supertonic_unavailable = True
                logger.warning(
                    f"TTS_PROVIDER=supertonic but the engine could not be loaded ({e}) — "
                    "using Edge TTS until the server is restarted."
                )
                raise
            self._supertonic_style = style
            self._supertonic = engine
            logger.info(f"Supertonic loaded (sr={engine.sample_rate})")
            return engine

    def _supertonic_sync(self, engine, text: str, language: str) -> np.ndarray:
        """
        Synthesise one sentence: mono float32 at SUPERTONIC_OUTPUT_RATE.
        Blocking. It writes nothing — see _synthesize_supertonic for why the
        file is a separate step.
        """
        import soxr

        with self._supertonic_lock:
            wav, _ = engine.synthesize(
                text,
                voice_style=self._supertonic_style,
                total_steps=max(1, int(settings.SUPERTONIC_STEPS)),
                lang=language,
            )
        samples = np.asarray(wav, dtype=np.float32).reshape(-1)
        if not len(samples):
            raise RuntimeError("Supertonic produced no audio")
        rate = int(engine.sample_rate)
        if rate != SUPERTONIC_OUTPUT_RATE:
            samples = soxr.resample(samples, rate, SUPERTONIC_OUTPUT_RATE)
        return np.clip(samples, -1.0, 1.0)

    @staticmethod
    def _write_wav(output_path: str, samples: np.ndarray) -> None:
        """16-bit mono WAV at SUPERTONIC_OUTPUT_RATE. Blocking."""
        import soundfile as sf

        sf.write(output_path, samples, SUPERTONIC_OUTPUT_RATE, subtype="PCM_16")

    async def _save_supertonic(self, output_path: str, samples: np.ndarray) -> None:
        """
        Write the sentence's file, such that a caller cancelled meanwhile
        still leaves nothing on disk.

        A thread cannot be stopped: cancelled during the write, the caller
        cleans up its temp files at once — before this one exists — and the
        thread then creates it with nobody left to remove it. It is only a
        few milliseconds wide, but it costs nothing to close: the write is
        shielded so it can be waited out, and whoever was cancelled leaves
        behind the instruction to delete what it produces.
        """

        def remove(done: asyncio.Future) -> None:
            done.cancelled() or done.exception()  # nobody else will look
            Path(output_path).unlink(missing_ok=True)

        write = asyncio.ensure_future(asyncio.to_thread(self._write_wav, output_path, samples))
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            write.add_done_callback(remove)
            raise

    async def _synthesize_supertonic(
        self, text: str, output_path: str, language: str, wanted_clone: bool
    ) -> SynthResult:
        if self._supertonic_unavailable:
            # Known since an earlier sentence, and logged then. Edge is what
            # this install speaks with; only a lost cloned voice is news.
            return await self._synthesize_networked(
                text, output_path, language, configured="edge", wanted_clone=wanted_clone
            )
        lang = (language or "").lower()
        if lang in SUPERTONIC_LANGUAGES:
            try:
                engine = await self._ensure_supertonic()
                logger.info(f"Synthesizing (supertonic, lang={lang}): {text[:80]}...")
                # Two steps, as with Chatterbox. Synthesis takes seconds
                # (and may wait its turn first), and a turn cancelled in
                # that time — every barge-in — abandons this await while the
                # thread runs on. When the thread also wrote the file, it
                # did so after the turn had already cleaned up, and the WAV
                # stayed for the rest of the session. A cancelled caller
                # never reaches the second step.
                samples = await asyncio.to_thread(self._supertonic_sync, engine, text, lang)
                await self._save_supertonic(output_path, samples)
                return SynthResult(
                    output_path=output_path,
                    engine="supertonic",
                    # It has no cloning: a cloned voice that was asked for is
                    # lost, and reported the way the edge engine reports it.
                    fallback=wanted_clone,
                    voice_cloned=False,
                )
            except ValueError as e:
                # What the package raises for characters outside its alphabet
                # (it strips emoji itself; a CJK or Hebrew word inside an
                # English sentence is the usual cause). This sentence only.
                logger.warning(f"Supertonic cannot speak this sentence ({e}); using Edge TTS")
            except Exception as e:
                if not self._supertonic_unavailable:  # that case is logged where it is found
                    logger.warning(f"Supertonic failed ({e}), trying Edge TTS fallback")
        else:
            # Not a fallback: for this language Edge is where this provider
            # is configured to go, on every sentence of every reply, and
            # nothing has failed. Reported as one, the user was told "voice
            # engine fell back" once per reply for the whole session. As
            # with an install that has no Chatterbox, it is news only if a
            # cloned voice was lost or Edge itself fell through to gTTS.
            logger.info(f"Supertonic does not speak {language!r}; using Edge TTS")
            return await self._synthesize_networked(
                text, output_path, language, configured="edge", wanted_clone=wanted_clone
            )
        # A sentence it refused or failed on: a real fallback, and said so.
        return await self._synthesize_networked(
            text, output_path, language, configured=None, wanted_clone=wanted_clone
        )

    def _generate_sync(self, text: str, language: str, speaker_wav: Optional[str]):
        """
        Run one Chatterbox generation with the right speaker.

        Chatterbox keeps the active speaker's conditioning on the model
        (`model.conds`). `generate(audio_prompt_path=...)` replaces it;
        `generate()` without one REUSES whatever is there. On a shared model
        that meant a session with no cloned voice spoke in the voice of
        whichever cloned session had generated last — one user's cloned voice
        coming out of another user's avatar.

        So the conditioning is set explicitly for every call, under a lock:
        the built-in voice's for an uncloned session, and a cached copy per
        reference recording for a cloned one (which also stops the reference
        audio being re-encoded for every sentence).

        The cache relies on `prepare_conditionals` and `conds`. If a
        Chatterbox release drops either, this degrades to passing
        `audio_prompt_path` per call — correct, just slower — still under the
        lock.
        """
        model = self.model
        with self._generate_lock:
            can_manage = hasattr(model, "conds") and self._default_conds is not None
            if speaker_wav:
                if can_manage and hasattr(model, "prepare_conditionals"):
                    try:
                        st = Path(speaker_wav).stat()
                        key = (speaker_wav, st.st_mtime_ns, st.st_size)
                    except OSError:
                        key = (speaker_wav, 0, 0)
                    conds = self._conds_cache.get(key)
                    if conds is None:
                        model.prepare_conditionals(speaker_wav)
                        conds = model.conds
                        if len(self._conds_cache) >= 64:
                            self._conds_cache.pop(next(iter(self._conds_cache)))
                        self._conds_cache[key] = conds
                    model.conds = conds
                    return model.generate(text, language_id=language)
                return model.generate(text, language_id=language, audio_prompt_path=speaker_wav)
            if can_manage:
                model.conds = self._default_conds
            return model.generate(text, language_id=language)

    async def synthesize(
        self,
        text: str,
        output_path: str,
        speaker_wav: Optional[str] = None,
        language: str = "en",
    ) -> SynthResult:
        """
        Synthesize speech.

        Args:
            text: Text to speak.
            output_path: Destination WAV path.
            speaker_wav: Optional reference audio for voice cloning (≥10s recommended).
            language: 2-letter code from Chatterbox's 23-language set.

        Returns:
            SynthResult describing the WAV path and which engine was used.
            `fallback=True` indicates the preferred Chatterbox path failed
            and gTTS was used instead — voice cloning is lost in that case.

        Raises NothingToSay, before any engine is touched or any file made,
        for text with no letter or digit in it (see `sayable`).
        """
        if not sayable(text):
            raise NothingToSay(f"nothing to say in {text[:40]!r}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if self.provider == "supertonic":
            return await self._synthesize_supertonic(
                text, output_path, language, wanted_clone=bool(speaker_wav)
            )
        if self.provider in ("edge", "edge-tts", "gtts"):
            return await self._synthesize_networked(
                text,
                output_path,
                language,
                configured=self.provider,
                wanted_clone=bool(speaker_wav),
            )
        if self._chatterbox_missing:
            # Known not to be installed: go straight to the fallback rather
            # than fail the import again. Only count it as a fallback worth
            # telling the user about when a cloned voice was actually lost.
            return await self._synthesize_networked(
                text, output_path, language, configured="edge", wanted_clone=bool(speaker_wav)
            )

        try:
            if self.model is None:
                await self.initialize()

            logger.info(f"Synthesizing (chatterbox, lang={language}): {text[:80]}...")

            if speaker_wav and not Path(speaker_wav).exists():
                logger.warning(f"Speaker WAV not found: {speaker_wav!r} — using default voice")
                speaker_wav = None

            wav = await asyncio.to_thread(self._generate_sync, text, language, speaker_wav)
            await asyncio.to_thread(torchaudio.save, output_path, wav, self.model.sr)

            logger.info(
                f"Synthesis complete{' (cloned voice)' if speaker_wav else ''}: {output_path}"
            )
            return SynthResult(
                output_path=output_path,
                engine="chatterbox",
                fallback=False,
                voice_cloned=bool(speaker_wav),
            )

        except Exception as e:
            if self._chatterbox_missing:
                pass  # already explained once, in initialize()
            elif speaker_wav:
                logger.warning(
                    f"Chatterbox voice-clone failed — cloned voice NOT applied, "
                    f"falling back to a default neural voice. Error: {e}"
                )
            else:
                logger.warning(f"Chatterbox failed ({e}), trying Edge TTS fallback")

            return await self._synthesize_networked(
                text,
                output_path,
                language,
                configured="edge" if self._chatterbox_missing and not speaker_wav else None,
                wanted_clone=bool(speaker_wav),
            )

    async def _synthesize_networked(
        self,
        text: str,
        output_path: str,
        language: str,
        configured: Optional[str],
        wanted_clone: bool,
    ) -> SynthResult:
        """
        Edge TTS, then gTTS.

        `configured` is the engine the operator asked for, if this chain was
        entered by choice ("edge"/"gtts") rather than because Chatterbox
        failed (None). `fallback` in the result is True whenever what was
        delivered is worse than what was asked for — a different engine, or a
        cloned voice that could not be applied.
        """
        engine = "gtts"
        if configured != "gtts":
            # Prefer Edge TTS (Microsoft neural voices — much better prosody
            # than gTTS, still free and CPU-only); gTTS is the last resort.
            try:
                await self._edge_fallback(text, output_path, language)
                engine = "edge-tts"
            except Exception as edge_err:
                logger.warning(f"Edge TTS failed ({edge_err}), falling back to gTTS")
                await self._gtts_fallback(text, output_path, language)
        else:
            await self._gtts_fallback(text, output_path, language)

        asked_for = {"edge": "edge-tts", "edge-tts": "edge-tts", "gtts": "gtts"}.get(
            configured or ""
        )
        return SynthResult(
            output_path=output_path,
            engine=engine,
            fallback=wanted_clone or engine != asked_for,
            voice_cloned=False,
        )

    async def _edge_fallback(self, text: str, output_path: str, language: str = "en") -> str:
        """Free neural-voice fallback via Microsoft Edge TTS (no key, no GPU)."""
        import edge_tts
        from pydub import AudioSegment

        voice = _EDGE_VOICES.get(language, _EDGE_VOICES["en"])
        logger.info(f"Synthesizing (edge-tts, {voice}): {text[:80]}...")
        mp3_path = str(Path(output_path).with_suffix(".edge.mp3"))

        try:
            await edge_tts.Communicate(text, voice).save(mp3_path)
            await asyncio.to_thread(
                lambda: AudioSegment.from_mp3(mp3_path).export(output_path, format="wav")
            )
            logger.info(f"Edge TTS synthesis complete: {output_path}")
            return output_path
        finally:
            Path(mp3_path).unlink(missing_ok=True)

    @staticmethod
    def _gtts_language(language: str) -> str:
        """
        Map a session language onto one gTTS actually supports.

        gTTS covers 59 languages but not the same 23 this app offers — Hebrew
        ("he") is accepted everywhere else in the stack and rejected here with
        ValueError. Since gTTS is the last link in the chain, letting that
        raise turned a supported language into silence, which is exactly what
        the fallback chain exists to prevent. Speaking the text in an English
        voice is a poor result; producing nothing is a broken one.
        """
        try:
            from gtts.lang import tts_langs

            if language in tts_langs():
                return language
        except Exception:
            # Language list unavailable (offline, API change) — try as given
            # and let the synth attempt decide.
            return language
        logger.warning(f"gTTS does not support {language!r}; speaking with the English voice")
        return "en"

    async def _gtts_fallback(self, text: str, output_path: str, language: str = "en") -> str:
        """Network-only fallback using Google TTS — no GPU/local model required."""
        # Build the temp name from the path's stem rather than replacing the
        # first ".wav" found anywhere in the string, which would corrupt a path
        # like /tmp/a.wav.d/out.wav.
        mp3_path = str(Path(output_path).with_suffix(".gtts.mp3"))
        try:
            from gtts import gTTS
            from pydub import AudioSegment

            lang = self._gtts_language(language)
            logger.info(f"Synthesizing (gTTS, lang={lang}): {text[:80]}...")

            await asyncio.to_thread(lambda: gTTS(text=text, lang=lang, slow=False).save(mp3_path))
            await asyncio.to_thread(
                lambda: AudioSegment.from_mp3(mp3_path).export(output_path, format="wav")
            )

            logger.info(f"gTTS synthesis complete: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"gTTS also failed: {e}")
            raise
        finally:
            # In a finally so a failed mp3->wav conversion doesn't strand the
            # intermediate file in the session directory.
            Path(mp3_path).unlink(missing_ok=True)

    async def synthesize_bytes(
        self,
        text: str,
        speaker_wav: Optional[str] = None,
        language: str = "en",
    ) -> bytes:
        """Synthesize and return WAV bytes (used by REST callers)."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
            tmp_path = tmp_file.name

        try:
            await self.synthesize(text, tmp_path, speaker_wav, language)
            return Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)


# Suppress unused-name warning — re-exported for type hints elsewhere
__all__ = ["TTSService", "SynthResult", "NothingToSay", "sayable", "tts_service"]


# Global instance
tts_service = TTSService()
