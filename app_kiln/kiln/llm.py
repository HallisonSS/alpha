"""Cliente LLM compativel com a API de chat completions (OpenAI e Ollama /v1).

Troca de provedor = trocar KILN_LLM_BASE_URL / KILN_LLM_MODEL / KILN_LLM_API_KEY.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class LLM(Protocol):
    def chat(self, messages: list[dict], *, json_mode: bool = True) -> LLMResult: ...


class OpenAICompatLLM:
    def __init__(self, settings):
        self.s = settings

    def _cost(self, tin: int, tout: int) -> float:
        return (tin * self.s.price_in + tout * self.s.price_out) / 1_000_000

    def chat(self, messages: list[dict], *, json_mode: bool = True) -> LLMResult:
        import httpx  # import tardio: o nucleo (policy/gates) nao depende de httpx

        body: dict = {"model": self.s.llm_model, "messages": messages}
        if "api.openai.com" in self.s.llm_base_url:
            body["max_completion_tokens"] = self.s.llm_max_output
        else:
            body["max_tokens"] = self.s.llm_max_output
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.s.llm_api_key}"} if self.s.llm_api_key else {}
        url = f"{self.s.llm_base_url}/chat/completions"
        last: Exception | None = None
        for attempt, delay in enumerate((0, 2, 5, 10)):
            if delay:
                time.sleep(delay)
            try:
                r = httpx.post(url, json=body, headers=headers, timeout=self.s.llm_timeout)
                if r.status_code == 400 and "response_format" in body and "response_format" in r.text:
                    body.pop("response_format")  # provedor sem json_mode
                    continue
                if r.status_code in (429, 500, 502, 503, 504):
                    last = LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                    continue
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"] or ""
                u = data.get("usage") or {}
                tin = int(u.get("prompt_tokens") or sum(len(m["content"]) for m in messages) // 4)
                tout = int(u.get("completion_tokens") or len(text) // 4)
                return LLMResult(text, tin, tout, self._cost(tin, tout))
            except (httpx.TransportError, httpx.HTTPStatusError, KeyError, ValueError) as e:
                last = e
        raise LLMError(f"LLM indisponivel apos retentativas: {last!r}")
