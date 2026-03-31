import os
os.environ["COQUI_TOS_AGREED"] = "1"  # Accept Coqui non-commercial CPML license non-interactively

from TTS.api import TTS
import logging
import io
import numpy as np
import soundfile as sf
import torch
from typing import Optional
from pathlib import Path
from app.config import settings

logger = logging.getLogger(__name__)


class TTSService:
    """Text-to-Speech Service"""
    
    def __init__(self):
        self.provider = settings.TTS_PROVIDER
        self.model = None
        
    def _check_cuda(self) -> bool:
        """Check if CUDA is available"""
        try:
            return torch.cuda.is_available()
        except:
            return False
    
    async def initialize(self):
        """Initialize TTS model"""
        if self.model is not None:
            return
        
        try:
            if self.provider == "coqui":
                logger.info("Loading Coqui TTS model...")
                # Use XTTS v2 for high-quality multilingual TTS
                self.model = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
                
                # Move to GPU if available
                if self._check_cuda():
                    self.model.to("cuda")
                    logger.info("Coqui TTS loaded on GPU")
                else:
                    logger.info("Coqui TTS loaded on CPU")
            
            logger.info("TTS model loaded successfully")
        
        except Exception as e:
            logger.error(f"Failed to load TTS model: {e}")
            # Fallback to simpler model
            try:
                logger.info("Loading fallback TTS model...")
                self.model = TTS("tts_models/en/ljspeech/tacotron2-DDC")
                if self._check_cuda():
                    self.model.to("cuda")
                logger.info("Fallback TTS model loaded")
            except Exception as fallback_error:
                logger.error(f"Failed to load fallback model: {fallback_error}")
                raise
    
    async def synthesize(
        self,
        text: str,
        output_path: str,
        speaker_wav: Optional[str] = None,
        language: str = "en"
    ) -> str:
        """
        Synthesize speech from text
        
        Args:
            text: Text to synthesize
            output_path: Output audio file path
            speaker_wav: Optional speaker reference for voice cloning
            language: Language code
        
        Returns:
            Path to generated audio file
        """
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # Try Coqui TTS first; fall back to gTTS if model not ready
        try:
            if self.model is None:
                await self.initialize()

            logger.info(f"Synthesizing speech (coqui): {text[:100]}...")

            # Validate speaker WAV exists on disk before attempting voice clone
            if speaker_wav and not Path(speaker_wav).exists():
                logger.warning(
                    f"Speaker WAV not found: {speaker_wav!r} — using default TTS voice"
                )
                speaker_wav = None

            is_multilingual = getattr(self.model, 'is_multi_lingual', False)
            if speaker_wav and hasattr(self.model, 'tts_to_file'):
                kwargs = {"text": text, "speaker_wav": speaker_wav, "file_path": output_path}
                if is_multilingual:
                    kwargs["language"] = language
                self.model.tts_to_file(**kwargs)
                logger.info(f"Speech synthesized with cloned voice: {output_path}")
            else:
                self.model.tts_to_file(text=text, file_path=output_path)
                logger.info(f"Speech synthesized (default voice): {output_path}")

            return output_path

        except Exception as e:
            if speaker_wav:
                logger.warning(
                    f"Coqui voice-clone TTS failed — cloned voice NOT applied, "
                    f"falling back to gTTS default voice. Error: {e}"
                )
            else:
                logger.warning(f"Coqui TTS failed ({e}), falling back to gTTS")
            return await self._gtts_fallback(text, output_path, language)

    async def _gtts_fallback(self, text: str, output_path: str, language: str = "en") -> str:
        """Fallback TTS using Google TTS (no local model required)."""
        try:
            from gtts import gTTS
            from pydub import AudioSegment

            logger.info(f"Synthesizing speech (gTTS): {text[:100]}...")
            mp3_path = output_path.replace(".wav", "_gtts.mp3")
            gTTS(text=text, lang=language, slow=False).save(mp3_path)
            AudioSegment.from_mp3(mp3_path).export(output_path, format="wav")
            Path(mp3_path).unlink(missing_ok=True)
            logger.info(f"gTTS synthesis complete: {output_path}")
            return output_path
        except Exception as e:
            logger.error(f"gTTS also failed: {e}")
            raise
    
    async def synthesize_bytes(
        self,
        text: str,
        speaker_wav: Optional[str] = None,
        language: str = "en"
    ) -> bytes:
        """
        Synthesize speech and return as bytes
        
        Args:
            text: Text to synthesize
            speaker_wav: Optional speaker reference
            language: Language code
        
        Returns:
            Audio data as bytes
        """
        try:
            # Create temporary file
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                tmp_path = tmp_file.name
            
            # Synthesize to file
            await self.synthesize(text, tmp_path, speaker_wav, language)
            
            # Read file as bytes
            with open(tmp_path, 'rb') as f:
                audio_bytes = f.read()
            
            # Cleanup
            Path(tmp_path).unlink()
            
            return audio_bytes
        
        except Exception as e:
            logger.error(f"Failed to synthesize speech to bytes: {e}")
            raise
    
    async def get_available_speakers(self) -> list:
        """Get list of available speakers"""
        try:
            if self.model is None:
                await self.initialize()
            
            if hasattr(self.model, 'speakers'):
                return self.model.speakers
            return []
        
        except Exception as e:
            logger.error(f"Failed to get available speakers: {e}")
            return []


# Global instance
tts_service = TTSService()
