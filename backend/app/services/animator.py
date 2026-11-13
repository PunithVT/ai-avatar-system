import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

import torch

from app.config import settings

TMPDIR = Path(tempfile.gettempdir())

logger = logging.getLogger(__name__)


class AvatarAnimator:
    """
    Avatar Animation Service.
    Supported engines (set AVATAR_ENGINE in .env):
      - musetalk   : MuseTalk V1.5 — persistent worker (models loaded once)
      - liveavatar : Alibaba LiveAvatar — higher fidelity, 48 GB+ VRAM, and a
                     full 14B model load per turn (see _animate_liveavatar)
      - simple     : ffmpeg static image + audio, no lip-sync

    Any engine failure falls back to simple rather than failing the turn.
    """

    def __init__(self):
        self.engine = settings.AVATAR_ENGINE
        self.resolution = settings.AVATAR_RESOLUTION
        self.fps = settings.AVATAR_FPS
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.use_float16 = self.device == "cuda"  # float16 on GPU = ~2× faster via Tensor Cores
        self._initialised = False
        self._musetalk_dir: Optional[Path] = None
        self._liveavatar_dir: Optional[Path] = None

        # Persistent worker handles
        self._worker_proc: Optional[asyncio.subprocess.Process] = None
        self._worker_lock = asyncio.Lock()
        self._worker_env: dict = {}
        self._worker_stderr_path: Optional[Path] = None
        # The in-flight worker start, shared by every caller waiting on it.
        self._worker_start: Optional[asyncio.Task] = None
        # Set when a start fails; suppresses further attempts for a while.
        self._worker_failed_at: Optional[float] = None
        self._worker_failure: Optional[str] = None

        if self.device == "cuda":
            gpu_name = torch.cuda.get_device_name(0)
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
            logger.info(
                f"AvatarAnimator: engine={self.engine}, device=cuda, "
                f"face_restore={settings.FACE_RESTORE}, "
                f"({gpu_name}, {vram_gb:.1f} GB VRAM), float16={self.use_float16}"
            )
        else:
            logger.info(
                f"AvatarAnimator: engine={self.engine}, device=cpu "
                f"(no GPU — consider AWS g5/g6 instance for real-time performance)"
            )

    # ── initialisation ────────────────────────────────────────────────────────

    async def initialize(self):
        if self._initialised:
            return

        if self.engine == "musetalk":
            self._musetalk_dir = self._find_dir(settings.MUSETALK_PATH, "scripts/inference.py")
            if self._musetalk_dir is None:
                logger.warning(
                    "MuseTalk not found at '%s'. "
                    "Run scripts/setup_musetalk.sh to install it. "
                    "Falling back to simple animation.",
                    settings.MUSETALK_PATH,
                )
                self.engine = "simple"
            else:
                logger.info(f"MuseTalk found at: {self._musetalk_dir}")
                # Build env once
                existing = os.environ.get("PYTHONPATH", "")
                self._worker_env = os.environ.copy()
                self._worker_env["PYTHONPATH"] = str(self._musetalk_dir) + (
                    ":" + existing if existing else ""
                )

        elif self.engine == "liveavatar":
            self._liveavatar_dir = self._find_dir(
                settings.LIVEAVATAR_PATH, "minimal_inference/s2v_streaming_interact.py"
            )
            if self._liveavatar_dir is None:
                logger.warning(
                    "LiveAvatar not found at '%s'. "
                    "Run scripts/setup_liveavatar.sh to install it. "
                    "Falling back to simple animation.",
                    settings.LIVEAVATAR_PATH,
                )
                self.engine = "simple"
            else:
                logger.info(f"LiveAvatar found at: {self._liveavatar_dir}")
                if self.device != "cuda":
                    logger.warning(
                        "LiveAvatar requires CUDA (48 GB+ VRAM with FP8). "
                        "Falling back to simple animation."
                    )
                    self.engine = "simple"

        elif self.engine not in ("simple",):
            logger.warning(f"Unknown engine '{self.engine}', using simple animation.")
            self.engine = "simple"

        self._initialised = True

    def _resolve_worker_script(self, musetalk_dir: Path) -> Path:
        """
        Locate the persistent-worker script.

        `musetalk_worker.py` is OUR custom driver, not part of the upstream
        MuseTalk repo that setup_musetalk.sh clones. The tracked copy at
        backend/musetalk_worker.py is authoritative: it is the one that
        changes with this codebase, and it speaks the protocol this class
        expects (job ids, a private result channel).

        setup_musetalk.sh also copies it into models/MuseTalk/scripts/, and
        that copy used to win. It is a snapshot from whenever setup last ran,
        under a gitignored directory, so it silently went stale — face
        restoration and every later worker fix never reached a checkout that
        had been set up earlier. It is now only the fallback for a deployment
        that ships the clone without the backend tree.

        The process runs with cwd=musetalk_dir and PYTHONPATH set to the
        clone, so `from musetalk.utils …` resolves wherever the file lives.
        """
        # backend/app/services/animator.py → backend/musetalk_worker.py
        tracked = Path(__file__).resolve().parent.parent.parent / "musetalk_worker.py"
        if tracked.exists():
            return tracked
        in_clone = musetalk_dir / "scripts" / "musetalk_worker.py"
        if in_clone.exists():
            logger.warning(f"Tracked MuseTalk worker missing; using the copy at {in_clone}")
            return in_clone
        raise FileNotFoundError(
            f"musetalk_worker.py not found in {tracked} or {in_clone}. "
            "Re-run scripts/setup_musetalk.sh."
        )

    def _find_dir(self, config_path: str, marker_file: str) -> Optional[Path]:
        candidates = [
            Path(config_path),
            Path(__file__).resolve().parent.parent.parent / config_path,
        ]
        for p in candidates:
            if (p / marker_file).exists():
                return p.resolve()
        return None

    # ── persistent worker management ─────────────────────────────────────────

    # After a failed start, wait this long before trying again. Loading takes
    # minutes and holds the worker lock; without a cool-off, a worker that
    # cannot start (missing weights, CUDA out of memory) was respawned for
    # every single sentence, each one stalling its turn for a full load before
    # falling back to the static image anyway.
    _WORKER_RETRY_COOLDOWN_SECS = 120

    def worker_state(self) -> str:
        """One-line engine status for /health — is lip-sync actually running?"""
        if self.engine != "musetalk":
            return self.engine
        if self._worker_proc is not None and self._worker_proc.returncode is None:
            return "musetalk (worker ready)"
        if self._worker_start is not None and not self._worker_start.done():
            return "musetalk (worker loading models)"
        if self._worker_failure:
            return f"musetalk (worker failed — static-image fallback): {self._worker_failure}"
        return "musetalk (worker not started)"

    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        """
        Return the running worker, starting it if needed.

        The start runs as its own task and callers wait on it through
        `shield`. A caller being cancelled (the user interrupted the avatar,
        or closed the tab, during the minutes-long model load) therefore no
        longer abandons a half-started process: previously that left a worker
        holding several GB of VRAM that nothing referenced, and the next call
        spawned another alongside it.
        """
        if self._worker_proc is not None and self._worker_proc.returncode is None:
            return self._worker_proc

        if self._worker_start is None or self._worker_start.done():
            if (
                self._worker_failed_at is not None
                and time.monotonic() - self._worker_failed_at < self._WORKER_RETRY_COOLDOWN_SECS
            ):
                raise RuntimeError(
                    f"MuseTalk worker unavailable (retrying later): {self._worker_failure}"
                )
            self._worker_start = asyncio.create_task(self._start_worker(), name="musetalk-start")
            # Every waiter may have been cancelled by the time this fails;
            # retrieve the exception so asyncio does not log it as unhandled.
            self._worker_start.add_done_callback(lambda t: t.cancelled() or t.exception())

        return await asyncio.shield(self._worker_start)

    async def _start_worker(self) -> asyncio.subprocess.Process:
        try:
            proc = await self._spawn_worker()
        except BaseException as e:
            self._worker_failed_at = time.monotonic()
            self._worker_failure = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
            raise
        self._worker_failed_at = None
        self._worker_failure = None
        self._worker_proc = proc
        return proc

    async def _spawn_worker(self) -> asyncio.subprocess.Process:
        musetalk_dir: Path = self._musetalk_dir  # type: ignore[assignment]
        worker_script = self._resolve_worker_script(musetalk_dir)

        logger.info("Starting persistent MuseTalk worker (loading models once)…")
        # stderr -> a file, NOT a pipe. The worker's model loading (tqdm progress,
        # HF "Loading weights" bars, library warnings) writes a lot of stderr
        # output; with stderr=PIPE and nobody ever draining it, the OS pipe
        # buffer (~64KB on Windows) fills up and the worker process BLOCKS on
        # its next stderr write — indefinitely, since nothing reads it. That
        # looked like "loading is just slow" but was actually a silent deadlock
        # that always ended in a timeout. File writes never block on a reader.
        self._worker_stderr_path = musetalk_dir / "worker_stderr.log"
        stderr_file = open(self._worker_stderr_path, "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(worker_script),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr_file,
                cwd=str(musetalk_dir),
                env=self._worker_env,
            )
        finally:
            stderr_file.close()  # child has its own fd now; safe to close our handle

        try:
            # Send init config — include float16 flag so worker can optimise for GPU
            init_msg = (
                json.dumps(
                    {
                        "unet_model_path": str(
                            musetalk_dir / "models" / "musetalkV15" / "unet.pth"
                        ),
                        "unet_config": str(
                            musetalk_dir / "models" / "musetalkV15" / "musetalk.json"
                        ),
                        "whisper_dir": str(musetalk_dir / "models" / "whisper"),
                        "vae_type": str(musetalk_dir / "models" / "sd-vae"),
                        "use_float16": self.use_float16,
                        # Optional GFPGAN pass over each composited frame. The
                        # worker loads it once and degrades to plain output if the
                        # weights or the package are missing, so an unset or
                        # half-finished install costs nothing.
                        "face_restore": settings.FACE_RESTORE,
                        "face_restore_model": str(
                            Path(settings.FACE_RESTORE_MODEL)
                            if Path(settings.FACE_RESTORE_MODEL).is_absolute()
                            else musetalk_dir / settings.FACE_RESTORE_MODEL
                        ),
                    }
                )
                + "\n"
            )
            proc.stdin.write(init_msg.encode())
            await proc.stdin.drain()

            # Wait for READY — GPU loads much faster (~60s) vs CPU (~5-10 min first
            # time). Bumped from 120s: cold-cache disk I/O for ~8.8GB of weights
            # (UNet, VAE, Whisper, BiSeNet) right after a fresh worker spawn was
            # measured timing out at 120s on this machine even on GPU.
            model_load_timeout = 300 if self.device == "cuda" else 600
            logger.info(
                f"Waiting for worker to finish loading models (timeout={model_load_timeout}s)…"
            )
            try:
                await asyncio.wait_for(self._await_ready(proc), timeout=model_load_timeout)
            except asyncio.TimeoutError:
                raise RuntimeError("MuseTalk worker timed out while loading models") from None
        except BaseException:
            # Includes cancellation at shutdown: never leave the child behind.
            if proc.returncode is None:
                proc.kill()
            raise

        logger.info("MuseTalk worker ready — models loaded")
        return proc

    async def _await_ready(self, proc: asyncio.subprocess.Process) -> None:
        """
        Read the worker's output until its READY line.

        Tolerates other lines first. The worker now keeps library output off
        this channel entirely, but an older copy of the script does not, and
        treating the first line as the verdict is what made every start fail.
        """
        while True:
            line = await proc.stdout.readline()
            if not line:
                stderr_out = ""
                if self._worker_stderr_path is not None:
                    try:
                        stderr_out = self._worker_stderr_path.read_text(errors="replace")[-4000:]
                    except OSError:
                        pass
                raise RuntimeError(
                    f"MuseTalk worker exited while loading models. stderr (tail):\n{stderr_out}"
                )
            if line.decode(errors="replace").strip() == "READY":
                return

    async def _read_result(self, proc: asyncio.subprocess.Process, job_id: str) -> dict:
        """
        Read lines until the result for `job_id`.

        Anything else is skipped: a result carrying a different id belongs to
        a job whose caller was cancelled while waiting (see _worker_infer),
        and a line that is not JSON is stray output.
        """
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise EOFError("worker closed its output")
            try:
                result = json.loads(line.decode(errors="replace").strip())
            except ValueError:
                continue
            if not isinstance(result, dict) or "status" not in result:
                continue
            if result.get("id") != job_id:
                logger.info("Discarding result of an abandoned MuseTalk job")
                continue
            return result

    async def _worker_infer(
        self, image_path: str, audio_path: str, output_path: str, coord_cache: Optional[str]
    ) -> str:
        """
        Send one job to the persistent worker and await its result.

        Cancellation-safe. If the caller is cancelled while waiting — the
        user spoke over the avatar — the lock is released but the worker is
        still computing, and its result arrives later. Results are matched to
        jobs by id, so the next job discards that late line instead of taking
        it for its own answer. (Before, one interruption left every later job
        reading the previous job's result: it "succeeded" with no output file,
        for every user, until the server was restarted.) The worker is also
        told to drop the abandoned job, so the next one does not wait for it.
        """
        async with self._worker_lock:
            proc = await self._ensure_worker()

            job_id = uuid.uuid4().hex
            # Creating this file tells the worker to drop the job — see
            # _check_cancelled in musetalk_worker.py.
            cancel_file = Path(f"{Path(output_path).resolve()}.cancel")
            job = (
                json.dumps(
                    {
                        "id": job_id,
                        "image": str(Path(image_path).resolve()),
                        "audio": str(Path(audio_path).resolve()),
                        "output": str(Path(output_path).resolve()),
                        "coord_cache": coord_cache,
                        "cancel_file": str(cancel_file),
                    }
                )
                + "\n"
            )

            # If the worker died (OOM/segfault) its stdin is closed; writing
            # raises BrokenPipeError. Reset the handle so the NEXT job respawns
            # a fresh worker instead of repeatedly failing against a dead pipe.
            try:
                proc.stdin.write(job.encode())
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                proc.kill()
                self._worker_proc = None
                raise RuntimeError(f"MuseTalk worker pipe is dead: {e}") from e

            # GPU: expect ~5-15s per sentence; CPU: up to 5 min
            infer_timeout = 60 if self.device == "cuda" else 300
            try:
                result = await asyncio.wait_for(
                    self._read_result(proc, job_id), timeout=infer_timeout
                )
            except asyncio.CancelledError:
                # Our caller is gone. Tell the worker to stop, so the next
                # job is not queued behind work nobody will watch. The file
                # is left for the worker to see; it lives in the session's
                # temp directory and goes when that does.
                try:
                    cancel_file.touch()
                except OSError:
                    pass
                raise
            except asyncio.TimeoutError:
                proc.kill()
                self._worker_proc = None
                raise RuntimeError(f"MuseTalk inference timed out after {infer_timeout}s") from None
            except EOFError:
                # Worker exited mid-job. Reset so the next call respawns
                # instead of erroring on a half-dead process.
                proc.kill()
                self._worker_proc = None
                raise RuntimeError("MuseTalk worker exited before returning a result") from None

            if result["status"] != "ok":
                raise RuntimeError(result.get("msg", "Unknown worker error"))
            if not Path(output_path).is_file():
                raise RuntimeError("MuseTalk worker reported success but wrote no video")

            return output_path

    async def warm(self) -> None:
        """
        Start the worker ahead of the first turn. Never raises.

        Model loading takes a minute or more; without this the first person
        to speak after a deploy waits for it. Only on a GPU — on CPU the
        engine is unusably slow either way and a 10-minute load at startup
        helps nobody.
        """
        try:
            if not self._initialised:
                await self.initialize()
            if self.engine != "musetalk" or self.device != "cuda":
                return
            async with self._worker_lock:
                await self._ensure_worker()
        except Exception as e:
            logger.warning(f"MuseTalk warm-up failed: {e}")

    async def shutdown(self) -> None:
        """Stop the worker process. Called at application shutdown."""
        start = self._worker_start
        if start is not None and not start.done():
            start.cancel()
            try:
                await start
            except BaseException:
                pass
        proc = self._worker_proc
        self._worker_proc = None
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass

    # ── public API ────────────────────────────────────────────────────────────

    async def animate(
        self,
        avatar_image_path: str,
        audio_path: str,
        output_path: str,
        cache_key: Optional[str] = None,
    ) -> str:
        """
        Animate avatar with audio. Returns path to the generated video.
        Falls back to simple (static image + audio) on any engine failure.
        """
        if not self._initialised:
            await self.initialize()

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Animating [{self.engine}] image={avatar_image_path} audio={audio_path}")

        try:
            if self.engine == "musetalk":
                return await self._animate_musetalk(avatar_image_path, audio_path, output_path)
            elif self.engine == "liveavatar":
                return await self._animate_liveavatar(avatar_image_path, audio_path, output_path)
            else:
                return await self._animate_simple(avatar_image_path, audio_path, output_path)
        except Exception as e:
            logger.error(f"Animation failed ({self.engine}): {e!r}. Falling back to simple.")
            return await self._animate_simple(avatar_image_path, audio_path, output_path)

    async def still_image_only(self) -> bool:
        """
        True when animate() is certain to produce the still image with the
        audio under it — so a caller that can show the still some other way
        (the WebRTC stream's idle picture) may skip the clip altogether.

        Answered from what is already known, without starting a worker or
        asking one: the simple engine (configured, or settled on because the
        lip-sync engine is not installed), or MuseTalk inside the cool-off
        after a failed start, when _ensure_worker refuses without trying.
        False whenever lip-sync might still happen, including a worker that
        has not been started yet or is loading — and a running worker whose
        job then fails still falls back to the still, through animate().
        """
        if not self._initialised:
            await self.initialize()
        if self.engine == "simple":
            return True
        if self.engine != "musetalk":
            return False
        if self._worker_proc is not None and self._worker_proc.returncode is None:
            return False
        if self._worker_start is not None and not self._worker_start.done():
            return False
        return (
            self._worker_failed_at is not None
            and time.monotonic() - self._worker_failed_at < self._WORKER_RETRY_COOLDOWN_SECS
        )

    async def animate_still(self, avatar_image_path: str, audio_path: str, output_path: str) -> str:
        """
        The still-image clip, whatever the configured engine. For audio that
        was already played (or promised) as a still: rendering it later must
        not go and wait for a lip-sync worker.
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        return await self._animate_simple(avatar_image_path, audio_path, output_path)

    # ── MuseTalk ──────────────────────────────────────────────────────────────

    async def _animate_musetalk(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Run MuseTalk via persistent worker (models stay loaded between calls)."""
        musetalk_dir: Path = self._musetalk_dir  # type: ignore[assignment]

        # Per-avatar face-coordinate cache (saves face-detection on repeat calls)
        avatar_id = hashlib.md5(str(Path(avatar_path).resolve()).encode()).hexdigest()
        coord_cache = str(musetalk_dir / "results" / "coords" / f"{avatar_id}.pkl")
        os.makedirs(os.path.dirname(coord_cache), exist_ok=True)

        await self._worker_infer(avatar_path, audio_path, output_path, coord_cache)

        logger.info(f"MuseTalk animation done: {output_path}")
        return output_path

    # ── LiveAvatar ────────────────────────────────────────────────────────────

    def _liveavatar_argv(
        self, live_dir: Path, avatar_path: str, audio_path: str, output_path: str
    ) -> list[str]:
        """
        Build the inference command.

        Split out from the runner so the argument contract can be asserted in
        tests without a GPU — this integration cannot be executed on ordinary
        hardware, so the flags being right is the only thing that can be
        checked cheaply.

        Mirrors infinite_inference_single_gpu.sh: torchrun with one process,
        --single_gpu, the DMD LoRA, and 4-step euler sampling with guidance
        off. --save_file is what makes this fit animate()'s contract; without
        it the script invents a timestamped name under ./output/ that the
        caller would then have to go hunting for.
        """
        argv = [
            "torchrun",
            "--nproc_per_node=1",
            "--standalone",
            "minimal_inference/s2v_streaming_interact.py",
            "--task",
            "s2v-14B",
            "--ckpt_dir",
            str(live_dir / settings.LIVEAVATAR_CKPT_DIR),
            "--single_gpu",
            "--image",
            str(Path(avatar_path).resolve()),
            "--audio",
            str(Path(audio_path).resolve()),
            "--save_file",
            str(Path(output_path).resolve()),
            "--size",
            settings.LIVEAVATAR_SIZE,
            "--infer_frames",
            str(settings.LIVEAVATAR_INFER_FRAMES),
            "--sample_steps",
            str(settings.LIVEAVATAR_SAMPLE_STEPS),
            "--sample_solver",
            "euler",
            "--sample_guide_scale",
            "0",
            "--load_lora",
            "--lora_path_dmd",
            settings.LIVEAVATAR_LORA,
            "--offload_model",
            "True",
        ]
        if settings.LIVEAVATAR_FP8:
            # Halves the weights; the difference between fitting on a 48 GB
            # card and needing 80 GB.
            argv.append("--fp8")
        return argv

    async def _animate_liveavatar(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """
        Run one LiveAvatar generation.

        NOT a persistent worker, unlike MuseTalk, because upstream has no such
        mode: s2v_streaming_interact.py runs once and exits, so every call pays
        a full 14B model load. That is minutes per turn, which is why this
        engine belongs on the offline Celery render path rather than the live
        WebSocket one — and why musetalk remains the default.

        Building a persistent worker for it would mean patching upstream, which
        is the maintenance debt that got SadTalker removed from this project.
        """
        live_dir: Path = self._liveavatar_dir  # type: ignore[assignment]
        argv = self._liveavatar_argv(live_dir, avatar_path, audio_path, output_path)

        logger.info(
            "LiveAvatar: starting generation (expect minutes — the 14B model "
            "loads on every invocation)"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(live_dir) + (
            ":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )

        # stderr to a file, not a pipe: the same deadlock that hit the MuseTalk
        # worker applies here. Model loading writes progress bars continuously,
        # and an undrained pipe buffer blocks the child indefinitely.
        stderr_path = live_dir / "liveavatar_stderr.log"
        with open(stderr_path, "ab") as stderr_file:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=stderr_file,
                cwd=str(live_dir),
                env=env,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=settings.LIVEAVATAR_TIMEOUT_SECS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    f"LiveAvatar timed out after {settings.LIVEAVATAR_TIMEOUT_SECS}s"
                )

        if proc.returncode != 0:
            tail = stderr_path.read_text(errors="replace")[-4000:]
            raise RuntimeError(f"LiveAvatar exited {proc.returncode}. stderr (tail):\n{tail}")

        # Exit code 0 is not proof of output: the script picks its own filename
        # when --save_file is not honoured as expected, and a missing file here
        # should surface as a clear error rather than a later confusing one.
        if not Path(output_path).is_file():
            raise RuntimeError(f"LiveAvatar reported success but produced no file at {output_path}")

        logger.info(f"LiveAvatar generation done: {output_path}")
        return output_path

    # ── Simple ffmpeg fallback ────────────────────────────────────────────────

    async def _animate_simple(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Combine static image + audio with FFmpeg. No lip-sync."""
        logger.info("Using simple animation (static image + audio, no lip-sync)")

        cmd = [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(avatar_path),
            "-i",
            str(audio_path),
            "-c:v",
            "libx264",
            "-tune",
            "stillimage",
            # Encoding speed is the whole cost of this engine; the picture is
            # one still frame, so the slower presets buy nothing.
            "-preset",
            "veryfast",
            # Index at the front, so the browser can start before the last byte.
            "-movflags",
            "+faststart",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            "-vf",
            (
                f"fps={self.fps},"
                f"scale={self.resolution}:{self.resolution}:"
                f"force_original_aspect_ratio=decrease,"
                f"pad={self.resolution}:{self.resolution}:(ow-iw)/2:(oh-ih)/2"
            ),
            output_path,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            logger.error(f"FFmpeg error:\n{err}")
            raise RuntimeError("Simple animation (ffmpeg) failed")

        logger.info(f"Simple animation done: {output_path}")
        return output_path

    # ── helpers ───────────────────────────────────────────────────────────────

    def generate_cache_key(self, text: str, avatar_id: str) -> str:
        return hashlib.md5(f"{avatar_id}:{text}".encode()).hexdigest()


# Global instance
avatar_animator = AvatarAnimator()
