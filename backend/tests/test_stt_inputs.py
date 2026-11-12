"""
What the speech-to-text service accepts, and how many transcriptions it lets
into the model at once. The model is a stand-in: no weights are loaded.
"""

import asyncio
import io
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.services.stt import MAX_CONCURRENT_TRANSCRIPTIONS, STTService


class FakeModel:
    def __init__(self):
        self.sources: list = []
        self.gate = threading.Event()
        self.gate.set()
        self.running = 0
        self.peak = 0
        self._lock = threading.Lock()

    def transcribe(self, source, **kwargs):
        with self._lock:
            self.sources.append(source)
            self.running += 1
            self.peak = max(self.peak, self.running)
        try:
            assert self.gate.wait(5)
        finally:
            with self._lock:
                self.running -= 1
        return [SimpleNamespace(text="hello")], SimpleNamespace(language=kwargs["language"])


@pytest.fixture
def stt():
    service = STTService()
    service.provider = "whisper"
    service.model = FakeModel()
    yield service
    service.model.gate.set()
    service._executor.shutdown(wait=True)


async def test_decoded_samples_go_straight_to_the_model(stt):
    """The WebRTC microphone arrives decoded: int16 at 16 kHz."""
    pcm = (np.sin(np.arange(1600) / 10) * 16384).astype(np.int16)
    assert await stt.transcribe(pcm, language="de") == "hello"
    given = stt.model.sources[0]
    assert isinstance(given, np.ndarray) and given.dtype == np.float32 and given.ndim == 1
    # Scaled to [-1, 1], which is what faster-whisper takes an array to be.
    assert np.allclose(given, pcm / 32768.0, atol=1e-6)
    assert 0.45 < np.abs(given).max() <= 0.5


async def test_float_samples_are_passed_as_they_are_and_stereo_is_mixed_down(stt):
    mono = np.linspace(-0.5, 0.5, 1600, dtype=np.float64)
    await stt.transcribe(mono)
    assert stt.model.sources[0].dtype == np.float32
    assert np.allclose(stt.model.sources[0], mono, atol=1e-6)

    stereo = np.stack([np.full(100, 0.2, np.float32), np.full(100, 0.4, np.float32)], axis=1)
    await stt.transcribe(stereo)
    assert stt.model.sources[1].shape == (100,) and np.allclose(stt.model.sources[1], 0.3)


async def test_paths_and_encoded_bytes_are_handled_as_before(stt, tmp_path):
    path = str(tmp_path / "input.webm")
    await stt.transcribe(path)
    assert stt.model.sources[0] == path, "a path is handed over for PyAV to decode"
    await stt.transcribe(b"webm-bytes")
    assert isinstance(stt.model.sources[1], io.BytesIO)
    assert stt.model.sources[1].getvalue() == b"webm-bytes"


async def test_only_so_many_transcriptions_are_inside_the_model_at_once(stt):
    stt.model.gate.clear()
    audio = np.zeros(160, np.int16)
    tasks = [asyncio.create_task(stt.transcribe(audio)) for _ in range(5)]
    deadline = time.monotonic() + 2
    while stt.model.running < MAX_CONCURRENT_TRANSCRIPTIONS and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.05)  # time for a third to slip in, if it could
    assert stt.model.running == MAX_CONCURRENT_TRANSCRIPTIONS == 2
    assert stt.busy

    stt.model.gate.set()
    assert await asyncio.gather(*tasks) == ["hello"] * 5
    assert stt.model.peak == 2
    assert not stt.busy


async def test_a_cancelled_caller_does_not_free_a_slot_its_thread_still_holds(stt):
    """
    Whisper cannot be interrupted. A barge-in cancels the caller, the thread
    runs on — and the next transcription must still count it.
    """
    stt.model.gate.clear()
    audio = np.zeros(160, np.int16)
    first = [asyncio.create_task(stt.transcribe(audio)) for _ in range(2)]
    while stt.model.running < 2:
        await asyncio.sleep(0.005)
    for task in first:
        task.cancel()
    await asyncio.gather(*first, return_exceptions=True)
    assert stt.busy, "both threads are still in the model"

    third = asyncio.create_task(stt.transcribe(audio))
    await asyncio.sleep(0.05)
    assert stt.model.running == 2 and len(stt.model.sources) == 2, "it waits, not joins them"
    stt.model.gate.set()
    assert await third == "hello"
    assert stt.model.peak == 2


async def test_on_gpu_reports_the_loaded_device_and_guesses_before_that(stt, monkeypatch):
    monkeypatch.setattr(stt, "_check_cuda", lambda: True)
    assert stt.on_gpu is True
    stt.device = "cpu"  # what the model was actually built on wins
    assert stt.on_gpu is False


async def test_recordings_are_not_held_to_the_stream_microphones_bound(stt, tmp_path):
    """
    Push-to-talk and the browser's own hands-free upload a recording (a path
    or bytes). Those run as they did before the stream microphone existed —
    each on a thread of the default pool, none waiting for another — so a
    server that never uses the stream microphone behaves exactly as it did.
    """
    stt.model.gate.clear()
    path = str(tmp_path / "input.webm")
    tasks = [asyncio.create_task(stt.transcribe(source)) for source in (path, path, b"x", b"y")]
    deadline = time.monotonic() + 2
    while stt.model.running < 4 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert stt.model.running == 4 > MAX_CONCURRENT_TRANSCRIPTIONS, "all four at once"
    # They take none of the stream microphone's slots...
    assert not stt.busy and stt._inflight == 0
    # ...but they are transcriptions, and a speculative pass on CPU is not
    # started beside them.
    assert not stt.idle

    # And the other way round: a full pool does not hold a recording up.
    samples = np.zeros(160, np.int16)
    pooled = [asyncio.create_task(stt.transcribe(samples)) for _ in range(3)]
    while stt.model.running < 6 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert stt.busy
    fifth = asyncio.create_task(stt.transcribe(path))
    while stt.model.running < 7 and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert stt.model.running == 7, "four recordings, two samples, and one more recording"

    stt.model.gate.set()
    assert await asyncio.gather(*tasks, *pooled, fifth) == ["hello"] * 8
    assert stt.idle and not stt.busy


async def test_idle_counts_a_recording_whose_caller_has_gone(stt, tmp_path):
    assert stt.idle
    stt.model.gate.clear()
    task = asyncio.create_task(stt.transcribe(str(tmp_path / "input.webm")))
    while stt.model.running < 1:
        await asyncio.sleep(0.005)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not stt.idle, "the thread is still in the model"
    stt.model.gate.set()
    deadline = time.monotonic() + 2
    while not stt.idle and time.monotonic() < deadline:
        await asyncio.sleep(0.005)
    assert stt.idle
