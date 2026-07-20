"""The only S13Code → GLC seam: ordinary authenticated HTTP."""
from __future__ import annotations

import os
from typing import Any

import httpx


class GatewayClient:
    def __init__(self, base_url: str | None = None, *, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = (base_url or os.getenv("GLC_BASE_URL", "http://127.0.0.1:8111")).rstrip("/")
        # An A2A-delegated run chains two completions (remote leg, then local
        # synthesis), so a slow local model can exceed a single-call budget.
        # Configurable rather than raised outright: hosted providers should
        # keep failing fast.
        timeout = float(os.getenv("S13_GATEWAY_TIMEOUT_SECONDS", "120"))
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def complete(self, prompt: str, system: str, *, session: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "system": system,
            "max_tokens": 700,
            "temperature": 0,
            "reasoning": "off",
            "agent": "s13_answer",
            "session": session,
        }
        # "gemini" is a logical gateway provider. GLC expands it to the
        # independently metered gemini_1..N key pool; S13Code never sees keys.
        if provider := os.getenv("S13_GATEWAY_PROVIDER"):
            payload["provider"] = provider
        response = await self._client.post(f"{self.base_url}/v1/chat", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"GLC /v1/chat returned {response.status_code}: {response.text[:500]}")
        body = response.json()
        return {"text": body.get("text", ""), "provider": body.get("provider"), "model": body.get("model")}

    async def health(self) -> dict[str, Any]:
        response = await self._client.get(f"{self.base_url}/healthz", timeout=3)
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
