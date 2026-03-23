import asyncio
import hashlib
import logging
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

import torch

from app.config import settings

TMPDIR = Path(tempfile.gettempdir())

logger = logging.getLogger(__name__)


class AvatarAnimator:
    """
    Avatar Animation Service using SadTalker or simple ffmpeg fallback.
    """

    def __init__(self):
        self.engine = settings.AVATAR_ENGINE
        self.resolution = settings.AVATAR_RESOLUTION
        self.fps = settings.AVATAR_FPS
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        # None = not yet initialised; set to a truthy value after first init
        self._initialised = False
        self._sadtalker_dir: Optional[Path] = None

        logger.info(f"AvatarAnimator: engine={self.engine}, device={self.device}")

    # ── initialisation ────────────────────────────────────────────────────────

    async def initialize(self):
        if self._initialised:
            return

        if self.engine == "sadtalker":
            self._sadtalker_dir = self._find_sadtalker()
            if self._sadtalker_dir is None:
                logger.warning(
                    "SadTalker not found at '%s'. "
                    "Run scripts/setup_sadtalker.sh to install it. "
                    "Falling back to simple (static-image) animation.",
                    settings.SADTALKER_PATH,
                )
                self.engine = "simple"
            else:
                logger.info(f"SadTalker found at: {self._sadtalker_dir}")
        elif self.engine == "liveportrait":
            logger.warning("Live Portrait not yet implemented, using simple animation.")
            self.engine = "simple"

        self._initialised = True

    def _find_sadtalker(self) -> Optional[Path]:
        """Return the SadTalker root dir if inference.py is present, else None."""
        # Try configured path (resolved relative to backend/ working dir)
        candidates = [
            Path(settings.SADTALKER_PATH),
            Path(__file__).resolve().parent.parent.parent / settings.SADTALKER_PATH,
        ]
        for p in candidates:
            if (p / "inference.py").exists():
                return p.resolve()
        return None

    # ── public API ────────────────────────────────────────────────────────────

    async def animate(
        self,
        avatar_image_path: str,
        audio_path: str,
        output_path: str,
        cache_key: Optional[str] = None,
    ) -> str:
        """
        Animate avatar with audio.  Returns path to the generated video.
        Falls back to simple (static image + audio) if SadTalker fails.
        """
        if not self._initialised:
            await self.initialize()

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Animating [{self.engine}] image={avatar_image_path} audio={audio_path}")

        try:
            if self.engine == "sadtalker":
                return await self._animate_sadtalker(avatar_image_path, audio_path, output_path)
            else:
                return await self._animate_simple(avatar_image_path, audio_path, output_path)
        except Exception as e:
            logger.error(f"Animation failed ({self.engine}): {e}. Retrying with simple fallback.")
            return await self._animate_simple(avatar_image_path, audio_path, output_path)

    # ── engines ───────────────────────────────────────────────────────────────

    async def _animate_sadtalker(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """Run SadTalker via subprocess and move its timestamped output to output_path."""
        assert self._sadtalker_dir is not None, "SadTalker dir must be set before calling _animate_sadtalker"
        result_dir = TMPDIR / f"sadtalker_{Path(output_path).stem}"
        result_dir.mkdir(parents=True, exist_ok=True)

        # SadTalker writes  result_dir/YYYY_MM_DD_HH.MM.SS.mp4
        cmd = [
            sys.executable,                          # same venv Python
            str(self._sadtalker_dir / "inference.py"),
            "--driven_audio", str(audio_path),
            "--source_image", str(avatar_path),
            "--result_dir", str(result_dir),
            "--still",                               # reduce head movement
            "--preprocess", "crop",                  # safe for portrait photos
            "--size", "256",
        ]

        logger.info("Running SadTalker: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._sadtalker_dir),            # required — model paths are relative
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")
            logger.error(f"SadTalker stderr:\n{err}")
            raise RuntimeError(f"SadTalker exited with code {proc.returncode}")

        # Find the mp4 SadTalker produced
        mp4_files = sorted(result_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
        if not mp4_files:
            raise FileNotFoundError(f"SadTalker produced no .mp4 in {result_dir}")

        shutil.move(str(mp4_files[-1]), output_path)
        shutil.rmtree(str(result_dir), ignore_errors=True)

        logger.info(f"SadTalker animation done: {output_path}")
        return output_path

    async def _animate_simple(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str,
    ) -> str:
        """
        Fallback: combine static image + audio with FFmpeg.
        No lip-sync — use only when SadTalker is unavailable.
        """
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
            "-vf", f"fps={self.fps},scale={self.resolution}:{self.resolution}:force_original_aspect_ratio=decrease,pad={self.resolution}:{self.resolution}:(ow-iw)/2:(oh-ih)/2",
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
