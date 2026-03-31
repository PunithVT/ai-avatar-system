import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
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
      - musetalk : MuseTalk V1.5 — persistent worker (models loaded once)
      - simple   : ffmpeg static image + audio, no lip-sync
    """

    def __init__(self):
        self.engine = settings.AVATAR_ENGINE
        self.resolution = settings.AVATAR_RESOLUTION
        self.fps = settings.AVATAR_FPS
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.use_float16 = self.device == "cuda"  # float16 on GPU = ~2× faster via Tensor Cores
        self._initialised = False
        self._musetalk_dir: Optional[Path] = None

        # Persistent worker handles
        self._worker_proc: Optional[asyncio.subprocess.Process] = None
        self._worker_lock = asyncio.Lock()
        self._worker_env: dict = {}

        if self.device == "cuda":
            gpu_name = torch.cuda.get_device_name(0)
            vram_gb = torch.cuda.get_device_properties(0).total_memory / 1024**3
            logger.info(
                f"AvatarAnimator: engine={self.engine}, device=cuda "
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
            self._musetalk_dir = self._find_dir(
                settings.MUSETALK_PATH, "scripts/inference.py"
            )
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
                self._worker_env["PYTHONPATH"] = (
                    str(self._musetalk_dir) + (":" + existing if existing else "")
                )

        elif self.engine not in ("simple",):
            logger.warning(f"Unknown engine '{self.engine}', using simple animation.")
            self.engine = "simple"

        self._initialised = True

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

    async def _ensure_worker(self) -> asyncio.subprocess.Process:
        """Start the persistent worker if not already running."""
        if self._worker_proc is not None and self._worker_proc.returncode is None:
            return self._worker_proc

        musetalk_dir: Path = self._musetalk_dir  # type: ignore[assignment]
        worker_script = musetalk_dir / "scripts" / "musetalk_worker.py"

        logger.info("Starting persistent MuseTalk worker (loading models once)…")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(worker_script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(musetalk_dir),
            env=self._worker_env,
        )

        # Send init config — include float16 flag so worker can optimise for GPU
        init_msg = json.dumps({
            "unet_model_path": str(musetalk_dir / "models" / "musetalkV15" / "unet.pth"),
            "unet_config":     str(musetalk_dir / "models" / "musetalkV15" / "musetalk.json"),
            "whisper_dir":     str(musetalk_dir / "models" / "whisper"),
            "vae_type":        str(musetalk_dir / "models" / "sd-vae"),
            "use_float16":     self.use_float16,
        }) + "\n"
        proc.stdin.write(init_msg.encode())
        await proc.stdin.drain()

        # Wait for READY — GPU loads much faster (~60s) vs CPU (~5-10 min first time)
        model_load_timeout = 120 if self.device == "cuda" else 600
        logger.info(f"Waiting for worker to finish loading models (timeout={model_load_timeout}s)…")
        try:
            ready_line = await asyncio.wait_for(proc.stdout.readline(), timeout=model_load_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError("MuseTalk worker timed out while loading models")

        if not ready_line.decode().strip().startswith("READY"):
            stderr_out = await proc.stderr.read()
            proc.kill()
            raise RuntimeError(
                f"Worker failed to start. stderr:\n{stderr_out.decode(errors='replace')}"
            )

        logger.info("MuseTalk worker ready — models loaded")
        self._worker_proc = proc
        return proc

    async def _worker_infer(self, image_path: str, audio_path: str,
                             output_path: str, coord_cache: Optional[str]) -> str:
        """Send one job to the persistent worker and await its result."""
        async with self._worker_lock:
            proc = await self._ensure_worker()

            job = json.dumps({
                "image":       str(Path(image_path).resolve()),
                "audio":       str(Path(audio_path).resolve()),
                "output":      str(Path(output_path).resolve()),
                "coord_cache": coord_cache,
            }) + "\n"

            proc.stdin.write(job.encode())
            await proc.stdin.drain()

            # GPU: expect ~5-15s per sentence; CPU: up to 5 min
            infer_timeout = 60 if self.device == "cuda" else 300
            try:
                result_line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=infer_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                self._worker_proc = None
                raise RuntimeError(
                    f"MuseTalk inference timed out after {infer_timeout}s"
                )

            result = json.loads(result_line.decode().strip())
            if result["status"] != "ok":
                raise RuntimeError(result.get("msg", "Unknown worker error"))

            return output_path

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
            else:
                return await self._animate_simple(avatar_image_path, audio_path, output_path)
        except Exception as e:
            logger.error(f"Animation failed ({self.engine}): {e}. Falling back to simple.")
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
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(avatar_path),
            "-i", str(audio_path),
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-shortest",
            "-vf", (
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
