#!/usr/bin/env python3
"""
OpenAI-compatible client for Ollama local model inference.
Unified interface: same client works with local Ollama (localhost:11434/v1)
or Claude escalation (#104) via base_url swap.
"""

import os
import requests
from typing import Optional, List, Dict
from dataclasses import dataclass


@dataclass
class OllamaConfig:
    """Runtime configuration for Ollama inference."""

    base_url: str
    model: str
    batch_size: int
    context_length: int
    max_tokens: int
    temperature: float = 0.3
    top_p: float = 0.9

    @classmethod
    def from_env(cls, profile: dict) -> "OllamaConfig":
        """
        Create config from environment and hardware probe result.

        Args:
            profile: dict from hardware_probe.py (model, batch_size, context_length, etc.)
        """
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
            model=os.getenv("OLLAMA_MODEL", profile.get("model", "qwen2.5:7b-instruct-q4_K_M")),
            batch_size=int(os.getenv("OLLAMA_NUM_PARALLEL", profile.get("batch_size", 8))),
            context_length=int(os.getenv("OLLAMA_NUM_CTX", profile.get("context_length", 8192))),
            max_tokens=int(os.getenv("OLLAMA_MAX_TOKENS", profile.get("max_tokens", 2048))),
            temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0.3")),
            top_p=float(os.getenv("OLLAMA_TOP_P", "0.9")),
        )


class OllamaClient:
    """
    OpenAI-compatible client for Ollama.

    Usage:
        client = OllamaClient(config)
        response = client.chat_completion(
            messages=[{"role": "user", "content": "extract..."}],
            model="qwen2.5:7b-instruct-q4_K_M"
        )
        print(response["choices"][0]["message"]["content"])
    """

    def __init__(self, config: OllamaConfig):
        self.config = config
        # Normalize base_url by removing trailing slashes (but keep the /v1 prefix if present).
        # base_url should end with either "" (http://localhost:11434) or "/v1" (http://localhost:11434/v1).
        # Both forms work with OpenAI-compatible endpoints, but we standardize on stripping only
        # trailing slashes to preserve the /v1 prefix if explicitly set.
        self.base_url = config.base_url.rstrip("/")

    def chat_completion(
        self,
        messages: List[Dict],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        top_p: Optional[float] = None,
    ) -> Dict:
        """
        POST to /v1/chat/completions (OpenAI-compatible).

        Args:
            messages: list of {"role": ..., "content": ...}
            model: override self.config.model
            temperature: override self.config.temperature
            max_tokens: override self.config.max_tokens
            top_p: override self.config.top_p

        Returns:
            Response dict with choices[0].message.content
        """
        payload = {
            "model": model or self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "top_p": top_p if top_p is not None else self.config.top_p,
            "max_tokens": max_tokens or self.config.max_tokens,
        }

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                timeout=300,  # 5 min for long extractions
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.ConnectionError:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self.base_url}. "
                "Is Ollama running? Start with: ollama serve"
            )
        except requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Ollama API error: {e.response.status_code} {e.response.text}")


def test_connection(config: OllamaConfig) -> bool:
    """Check if Ollama is reachable."""
    try:
        # Normalize base_url: ensure it ends with /v1 for the API path.
        # If already ends with /v1, use it; otherwise append /v1.
        if config.base_url.endswith('/v1'):
            api_base = config.base_url.rstrip('/')
        else:
            api_base = config.base_url.rstrip('/') + '/v1'
        response = requests.get(
            f"{api_base}/api/tags",
            timeout=2,
        )
        return response.status_code == 200
    except Exception:
        return False
