import httpx
from typing import Dict, Optional, Any, List
from .posthog_tracker import LLMTracker


class LLMClient:
    """OpenAI-compatible chat completions client with PostHog AI observability."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        site_url: str = "",
        site_name: str = "Wiki Bot",
        whisper_api_key: Optional[str] = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.site_url = site_url
        self.site_name = site_name
        # Whisper uses OpenAI directly (not available on all OpenRouter models)
        self.whisper_api_key = whisper_api_key or api_key

    def _headers(self) -> Dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        if self.site_url:
            h["HTTP-Referer"] = self.site_url
        if self.site_name:
            h["X-Title"] = self.site_name
        return h

    async def chat(
        self,
        system: str,
        user: str,
        *,
        temperature: float = 0.7,
        timeout: float = 60.0,
        chat_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send a chat completion request and track it in PostHog."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }

        provider = "openrouter" if "openrouter" in self.base_url else "openai"

        with LLMTracker(
            chat_id=chat_id,
            model=self.model,
            provider=provider,
            base_url=self.base_url,
            metadata=metadata or {},
        ) as tracker:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage")
                tracker.track_success(messages=messages, response=content, usage=usage)
                return content

    async def transcribe(
        self,
        audio_bytes: bytes,
        filename: str = "voice.ogg",
        *,
        chat_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Transcribe audio using OpenAI Whisper. Track in PostHog."""
        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {self.whisper_api_key}",
        }
        files = {
            "file": (filename, audio_bytes, "audio/ogg"),
            "model": (None, "whisper-1"),
        }

        provider = "openai"
        model = "whisper-1"

        with LLMTracker(
            chat_id=chat_id,
            model=model,
            provider=provider,
            base_url="https://api.openai.com/v1",
            metadata={**(metadata or {}), "transcription": True},
        ) as tracker:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    headers=headers,
                    files=files,
                    timeout=120.0,
                )
                resp.raise_for_status()
                data = resp.json()
                text = data.get("text", "")
                # Whisper doesn't return usage; estimate ~10 tokens/sec
                tracker.track_success(
                    messages=[{"role": "user", "content": "[audio transcription request]"}],
                    response=text,
                    usage=None,
                )
                return text
