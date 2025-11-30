import anthropic
import openai
from typing import List, Dict, AsyncGenerator
import logging
from app.config import settings

logger = logging.getLogger(__name__)


class LLMService:
    """LLM Service for AI responses"""
    
    def __init__(self):
        self.provider = settings.LLM_PROVIDER
        self.model = settings.LLM_MODEL
        self.temperature = settings.LLM_TEMPERATURE
        self.max_tokens = settings.LLM_MAX_TOKENS
        
        if self.provider == "anthropic":
            self.client = anthropic.AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY
            )
        elif self.provider == "openai":
            self.client = openai.AsyncOpenAI(
                api_key=settings.OPENAI_API_KEY
            )
    
    async def generate_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None
    ) -> str:
        """Generate AI response"""
        try:
            if self.provider == "anthropic":
                return await self._generate_anthropic(messages, system_prompt)
            elif self.provider == "openai":
                return await self._generate_openai(messages, system_prompt)
            else:
                raise ValueError(f"Unsupported LLM provider: {self.provider}")
        
        except Exception as e:
            logger.error(f"Failed to generate LLM response: {e}")
            raise
    
    async def _generate_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None
    ) -> str:
        """Generate response using Anthropic Claude"""
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt or "You are a helpful AI assistant in an avatar conversation system.",
                messages=messages
            )
            
            return response.content[0].text
        
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            raise
    
    async def _generate_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None
    ) -> str:
        """Generate response using OpenAI"""
        try:
            # Add system message if provided
            if system_prompt:
                messages = [{"role": "system", "content": system_prompt}] + messages
            
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens
            )
            
            return response.choices[0].message.content
        
        except Exception as e:
            logger.error(f"OpenAI API error: {e}")
            raise
    
    async def stream_response(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None
    ) -> AsyncGenerator[str, None]:
        """Stream AI response"""
        try:
            if self.provider == "anthropic":
                async for chunk in self._stream_anthropic(messages, system_prompt):
                    yield chunk
            elif self.provider == "openai":
                async for chunk in self._stream_openai(messages, system_prompt):
                    yield chunk
        
        except Exception as e:
            logger.error(f"Failed to stream LLM response: {e}")
            raise
    
    async def _stream_anthropic(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None
    ) -> AsyncGenerator[str, None]:
        """Stream response from Anthropic"""
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                temperature=self.temperature,
                system=system_prompt or "You are a helpful AI assistant.",
                messages=messages
            ) as stream:
                async for text in stream.text_stream:
                    yield text
        
        except Exception as e:
            logger.error(f"Anthropic streaming error: {e}")
            raise
    
    async def _stream_openai(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None
    ) -> AsyncGenerator[str, None]:
        """Stream response from OpenAI"""
        try:
            if system_prompt:
                messages = [{"role": "system", "content": system_prompt}] + messages
            
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                stream=True
            )
            
            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        
        except Exception as e:
            logger.error(f"OpenAI streaming error: {e}")
            raise


# Global instance
llm_service = LLMService()
