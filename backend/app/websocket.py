from fastapi import WebSocket
import logging
import json
import asyncio
from typing import Dict, Set
import base64
from datetime import datetime
from pathlib import Path
import tempfile

from app.services.stt import stt_service
from app.services.tts import tts_service
from app.services.llm import llm_service
from app.services.animator import avatar_animator
from app.services.storage import storage_service

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manage WebSocket connections"""
    
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.session_data: Dict[str, dict] = {}
    
    async def connect(self, session_id: str, websocket: WebSocket):
        """Connect new WebSocket client"""
        await websocket.accept()
        self.active_connections[session_id] = websocket
        self.session_data[session_id] = {
            'messages': [],
            'avatar_id': None,
            'user_id': None,
            'connected_at': datetime.utcnow()
        }
        logger.info(f"WebSocket connected: {session_id}")
    
    async def disconnect(self, session_id: str):
        """Disconnect WebSocket client"""
        if session_id in self.active_connections:
            del self.active_connections[session_id]
        if session_id in self.session_data:
            del self.session_data[session_id]
        logger.info(f"WebSocket disconnected: {session_id}")
    
    async def send_message(self, session_id: str, message: dict):
        """Send message to specific session"""
        if session_id in self.active_connections:
            websocket = self.active_connections[session_id]
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Failed to send message to {session_id}: {e}")
                await self.disconnect(session_id)
    
    async def broadcast(self, message: dict):
        """Broadcast message to all connected clients"""
        disconnected = []
        for session_id, websocket in self.active_connections.items():
            try:
                await websocket.send_json(message)
            except Exception as e:
                logger.error(f"Failed to broadcast to {session_id}: {e}")
                disconnected.append(session_id)
        
        # Clean up disconnected clients
        for session_id in disconnected:
            await self.disconnect(session_id)
    
    async def handle_audio_input(self, session_id: str, audio_data: str):
        """Handle audio input from client"""
        try:
            logger.info(f"Processing audio input for session {session_id}")
            
            # Send processing status
            await self.send_message(session_id, {
                "type": "status",
                "message": "Processing audio...",
                "stage": "transcription"
            })
            
            # Decode base64 audio
            audio_bytes = base64.b64decode(audio_data)
            
            # Save to temporary file
            with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as tmp_audio:
                tmp_audio.write(audio_bytes)
                audio_path = tmp_audio.name
            
            # Transcribe audio
            text = await stt_service.transcribe(audio_path)
            Path(audio_path).unlink()  # Clean up
            
            if not text:
                await self.send_message(session_id, {
                    "type": "error",
                    "message": "Could not transcribe audio"
                })
                return
            
            logger.info(f"Transcribed text: {text}")
            
            # Send transcription
            await self.send_message(session_id, {
                "type": "transcription",
                "text": text
            })
            
            # Process as text input
            await self.handle_text_input(session_id, text)
        
        except Exception as e:
            logger.error(f"Error processing audio: {e}")
            await self.send_message(session_id, {
                "type": "error",
                "message": f"Audio processing failed: {str(e)}"
            })
    
    async def handle_text_input(self, session_id: str, text: str):
        """Handle text input from client"""
        try:
            logger.info(f"Processing text input for session {session_id}: {text}")
            
            # Get session data
            session_data = self.session_data.get(session_id, {})
            messages = session_data.get('messages', [])
            
            # Add user message
            messages.append({"role": "user", "content": text})
            
            # Send thinking status
            await self.send_message(session_id, {
                "type": "status",
                "message": "Thinking...",
                "stage": "llm"
            })
            
            # Generate LLM response
            response_text = await llm_service.generate_response(messages)
            
            # Add assistant message
            messages.append({"role": "assistant", "content": response_text})
            session_data['messages'] = messages
            
            logger.info(f"LLM response: {response_text}")
            
            # Send text response
            await self.send_message(session_id, {
                "type": "message",
                "role": "assistant",
                "content": response_text
            })
            
            # Generate avatar video
            await self.generate_avatar_video(session_id, response_text)
        
        except Exception as e:
            logger.error(f"Error processing text: {e}")
            await self.send_message(session_id, {
                "type": "error",
                "message": f"Text processing failed: {str(e)}"
            })
    
    async def generate_avatar_video(self, session_id: str, text: str):
        """Generate animated avatar video"""
        try:
            session_data = self.session_data.get(session_id, {})
            avatar_id = session_data.get('avatar_id')
            
            if not avatar_id:
                logger.warning(f"No avatar_id for session {session_id}")
                return
            
            # Send video generation status
            await self.send_message(session_id, {
                "type": "status",
                "message": "Generating speech...",
                "stage": "tts"
            })
            
            # Generate speech audio
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp_audio:
                audio_path = tmp_audio.name
            
            await tts_service.synthesize(text, audio_path)
            
            # Send animation status
            await self.send_message(session_id, {
                "type": "status",
                "message": "Creating avatar animation...",
                "stage": "animation"
            })
            
            # Get avatar image from storage
            # TODO: Fetch avatar image path from database
            avatar_image_path = f"/tmp/avatars/{avatar_id}.jpg"
            
            # Generate animation
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as tmp_video:
                video_path = tmp_video.name
            
            await avatar_animator.animate(
                avatar_image_path,
                audio_path,
                video_path
            )
            
            # Upload to S3
            with open(video_path, 'rb') as f:
                video_data = f.read()
            
            s3_key = f"videos/{session_id}/{datetime.utcnow().timestamp()}.mp4"
            video_url = await storage_service.upload_file(
                video_data,
                s3_key,
                content_type="video/mp4"
            )
            
            # Clean up temporary files
            Path(audio_path).unlink()
            Path(video_path).unlink()
            
            # Send video URL
            await self.send_message(session_id, {
                "type": "video",
                "video_url": video_url,
                "text": text
            })
            
            logger.info(f"Avatar video generated: {video_url}")
        
        except Exception as e:
            logger.error(f"Error generating avatar video: {e}")
            await self.send_message(session_id, {
                "type": "error",
                "message": f"Video generation failed: {str(e)}"
            })
    
    async def set_avatar(self, session_id: str, avatar_id: str):
        """Set avatar for session"""
        if session_id in self.session_data:
            self.session_data[session_id]['avatar_id'] = avatar_id
            logger.info(f"Avatar set for session {session_id}: {avatar_id}")


# Global instance
websocket_manager = ConnectionManager()
