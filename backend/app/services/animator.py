import torch
import cv2
import numpy as np
from pathlib import Path
import logging
import subprocess
import hashlib
import asyncio
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)


class AvatarAnimator:
    """
    Avatar Animation Service using SadTalker or Live Portrait
    """
    
    def __init__(self):
        self.engine = settings.AVATAR_ENGINE
        self.resolution = settings.AVATAR_RESOLUTION
        self.fps = settings.AVATAR_FPS
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None
        
        logger.info(f"Avatar Animator initialized with engine: {self.engine}, device: {self.device}")
    
    async def initialize(self):
        """Initialize animation models"""
        if self.model is not None:
            return
        
        try:
            if self.engine == "sadtalker":
                await self._init_sadtalker()
            elif self.engine == "liveportrait":
                await self._init_liveportrait()
            else:
                raise ValueError(f"Unsupported avatar engine: {self.engine}")
            
            logger.info(f"Avatar animation model loaded successfully")
        
        except Exception as e:
            logger.error(f"Failed to initialize animation model: {e}")
            raise
    
    async def _init_sadtalker(self):
        """Initialize SadTalker model"""
        try:
            # Import SadTalker
            import sys
            sadtalker_path = Path("./models/SadTalker")
            sys.path.insert(0, str(sadtalker_path))
            
            from src.facerender.animate import AnimateFromCoeff
            from src.generate_batch import get_data
            from src.generate_facerender_batch import get_facerender_data
            
            logger.info("SadTalker modules loaded")
            
            # Initialize model components
            self.model = {
                'animate': AnimateFromCoeff,
                'get_data': get_data,
                'get_facerender_data': get_facerender_data
            }
            
        except Exception as e:
            logger.error(f"Failed to initialize SadTalker: {e}")
            logger.info("Falling back to simplified animation")
            self.engine = "simple"
    
    async def _init_liveportrait(self):
        """Initialize Live Portrait model"""
        try:
            # TODO: Implement Live Portrait initialization
            logger.warning("Live Portrait not yet implemented, using simple animation")
            self.engine = "simple"
        
        except Exception as e:
            logger.error(f"Failed to initialize Live Portrait: {e}")
            self.engine = "simple"
    
    async def animate(
        self,
        avatar_image_path: str,
        audio_path: str,
        output_path: str,
        cache_key: Optional[str] = None
    ) -> str:
        """
        Animate avatar with audio
        
        Args:
            avatar_image_path: Path to avatar image
            audio_path: Path to audio file
            output_path: Path to save output video
            cache_key: Optional cache key for reusing animations
        
        Returns:
            Path to generated video
        """
        try:
            if self.model is None:
                await self.initialize()
            
            logger.info(f"Animating avatar: {avatar_image_path} with audio: {audio_path}")
            
            # Ensure output directory exists
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            
            if self.engine == "sadtalker":
                return await self._animate_sadtalker(avatar_image_path, audio_path, output_path)
            elif self.engine == "liveportrait":
                return await self._animate_liveportrait(avatar_image_path, audio_path, output_path)
            else:
                return await self._animate_simple(avatar_image_path, audio_path, output_path)
        
        except Exception as e:
            logger.error(f"Animation failed: {e}")
            # Fallback to simple animation
            try:
                return await self._animate_simple(avatar_image_path, audio_path, output_path)
            except Exception as fallback_error:
                logger.error(f"Fallback animation also failed: {fallback_error}")
                raise
    
    async def _animate_sadtalker(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str
    ) -> str:
        """Animate using SadTalker"""
        try:
            # Run SadTalker inference
            # This is a simplified version - actual implementation would use SadTalker's full pipeline
            logger.info("Running SadTalker animation...")
            
            # Use subprocess to run SadTalker
            cmd = [
                "python", "models/SadTalker/inference.py",
                "--driven_audio", audio_path,
                "--source_image", avatar_path,
                "--result_dir", str(Path(output_path).parent),
                "--still", "--preprocess", "full",
                "--enhancer", "gfpgan"
            ]
            
            result = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await result.communicate()
            
            if result.returncode != 0:
                logger.error(f"SadTalker error: {stderr.decode()}")
                raise Exception("SadTalker animation failed")
            
            logger.info("SadTalker animation completed")
            return output_path
        
        except Exception as e:
            logger.error(f"SadTalker animation error: {e}")
            raise
    
    async def _animate_liveportrait(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str
    ) -> str:
        """Animate using Live Portrait"""
        # TODO: Implement Live Portrait animation
        logger.warning("Live Portrait not implemented, using simple animation")
        return await self._animate_simple(avatar_path, audio_path, output_path)
    
    async def _animate_simple(
        self,
        avatar_path: str,
        audio_path: str,
        output_path: str
    ) -> str:
        """
        Simple animation fallback using FFmpeg
        Creates a video by combining static image with audio
        """
        try:
            logger.info("Using simple animation (static image + audio)")
            
            # Get audio duration
            import librosa
            y, sr = librosa.load(audio_path, sr=None)
            duration = len(y) / sr
            
            # Create video using FFmpeg
            cmd = [
                'ffmpeg', '-y',
                '-loop', '1',
                '-i', avatar_path,
                '-i', audio_path,
                '-c:v', 'libx264',
                '-tune', 'stillimage',
                '-c:a', 'aac',
                '-b:a', '192k',
                '-pix_fmt', 'yuv420p',
                '-shortest',
                '-t', str(duration),
                '-vf', f'fps={self.fps},scale={self.resolution}:{self.resolution}',
                output_path
            ]
            
            result = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await result.communicate()
            
            if result.returncode != 0:
                logger.error(f"FFmpeg error: {stderr.decode()}")
                raise Exception("Simple animation failed")
            
            logger.info(f"Simple animation completed: {output_path}")
            return output_path
        
        except Exception as e:
            logger.error(f"Simple animation error: {e}")
            raise
    
    def generate_cache_key(self, text: str, avatar_id: str) -> str:
        """Generate cache key for animation"""
        content = f"{avatar_id}:{text}"
        return hashlib.md5(content.encode()).hexdigest()


# Global instance
avatar_animator = AvatarAnimator()
