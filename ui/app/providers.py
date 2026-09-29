from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
import httpx


def _headers(api_key: str, extra_json: str = '') -> dict[str, str]:
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    if extra_json:
        try:
            headers.update({str(k): str(v) for k, v in json.loads(extra_json).items()})
        except Exception as exc:
            raise ValueError(f'Invalid provider headers JSON: {exc}') from exc
    return headers


class ChatProvider:
    def __init__(self, settings):
        self.provider = settings.chat_provider
        self.base_url = settings.chat_base_url.rstrip('/').removesuffix('/v1')
        self.model = settings.chat_model
        self.api_key = settings.chat_api_key
        self.headers = _headers(settings.chat_api_key, settings.chat_headers_json)
        self.timeout = settings.model_timeout_seconds
        self.max_tokens = settings.model_num_predict
        self.temperature = settings.chat_temperature
        self.cache_mode = settings.model_service_cache_mode
        self.routing_strategy = settings.model_service_routing_strategy
        if self.provider == 'capella_model_service':
            if self.cache_mode in {'standard', 'semantic', 'none'}:
                self.headers['X-cb-cache'] = self.cache_mode
            if self.routing_strategy:
                self.headers['X-cb-routing-strategy'] = self.routing_strategy
            self.headers['X-cb-debug'] = 'true'

    def health(self) -> dict[str, Any]:
        if self.provider == 'disabled':
            return {'healthy': True, 'provider': 'disabled', 'model': None, 'streaming': False}
        endpoint = '/api/tags' if self.provider == 'ollama' else '/v1/models'
        with httpx.Client(base_url=self.base_url, headers=self.headers, timeout=8.0) as client:
            response = client.get(endpoint)
            response.raise_for_status()
            data = response.json()
        available = [m.get('name') or m.get('id') for m in data.get('models', data.get('data', []))]
        return {'healthy': True, 'provider': self.provider, 'model': self.model, 'available_models': available, 'streaming': True, 'cacheMode': self.cache_mode if self.provider == 'capella_model_service' else None, 'routingStrategy': self.routing_strategy if self.provider == 'capella_model_service' else None}

    async def stream(self, messages: list[dict[str, str]], usage_out: dict[str, Any] | None = None) -> AsyncIterator[str]:
        timeout = httpx.Timeout(connect=10.0, read=self.timeout, write=30.0, pool=10.0)
        if self.provider == 'ollama':
            payload = {'model': self.model, 'messages': messages, 'stream': True, 'keep_alive': '30m', 'options': {'temperature': self.temperature, 'num_predict': self.max_tokens}}
            async with httpx.AsyncClient(base_url=self.base_url, timeout=timeout) as client:
                async with client.stream('POST', '/api/chat', json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line: continue
                        event=json.loads(line)
                        token=str(event.get('message',{}).get('content',''))
                        if token: yield token
                        if event.get('done'):
                            if usage_out is not None:
                                usage_out.update({'promptTokens': event.get('prompt_eval_count'), 'completionTokens': event.get('eval_count'), 'source': 'ollama_exact'})
                            break
            return
        if self.provider == 'disabled':
            yield 'I can only answer grounded catalogue, profile, history and action requests while the chat provider is disabled.'
            return
        payload={'model': self.model, 'messages': messages, 'stream': True, 'temperature': self.temperature, 'max_tokens': self.max_tokens}
        async with httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=timeout) as client:
            async with client.stream('POST','/v1/chat/completions',json=payload) as response:
                response.raise_for_status()
                if usage_out is not None and self.provider == 'capella_model_service':
                    cache_headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key.lower().startswith('x-cb-')
                    }
                    usage_out.update({
                        'cacheMode': self.cache_mode,
                        'routingStrategy': self.routing_strategy,
                        'modelServiceHeaders': cache_headers,
                    })
                async for line in response.aiter_lines():
                    if not line or line == 'data: [DONE]': continue
                    if line.startswith('data: '): line=line[6:]
                    event=json.loads(line)
                    choices=event.get('choices') or []
                    if choices:
                        token=str((choices[0].get('delta') or {}).get('content') or '')
                        if token: yield token
                    usage=event.get('usage')
                    if usage and usage_out is not None:
                        usage_out.update({'promptTokens': usage.get('prompt_tokens'), 'completionTokens': usage.get('completion_tokens'), 'source': f'{self.provider}_exact'})


class EmbeddingProvider:
    def __init__(self, settings):
        self.provider=settings.embedding_provider
        self.base_url=settings.embedding_base_url.rstrip('/').removesuffix('/v1')
        self.model=settings.embedding_model
        self.dimensions=settings.embedding_dimensions
        self.send_input_type=getattr(settings, "embedding_send_input_type", self.provider == "capella_model_service")
        self.headers=_headers(settings.embedding_api_key, settings.embedding_headers_json)
        self.timeout=settings.embedding_timeout_seconds
        self._client=httpx.Client(base_url=self.base_url, headers=self.headers, timeout=httpx.Timeout(self.timeout))

    def close(self): self._client.close()

    def embed(self, text: str, *, input_type: str='query') -> list[float]:
        body={'model': self.model, 'input': text}
        if self.send_input_type and input_type:
            body['input_type']=input_type
        response=self._client.post('/v1/embeddings', json=body)
        response.raise_for_status()
        vector=[float(v) for v in response.json()['data'][0]['embedding']]
        if self.dimensions and len(vector) != self.dimensions:
            raise ValueError(f'Embedding dimension mismatch: configured {self.dimensions}, provider returned {len(vector)}')
        return vector
