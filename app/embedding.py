"""OpenAI /embeddings 兼容网关。只在服务端调用，密钥不下发。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .config import Config


class EmbeddingUnavailable(Exception):
    """网关未配置、不可达，或返回维度与配置不一致。"""


class EmbeddingClient:
    def __init__(self, config: Config):
        self._config = config

    def configured(self) -> bool:
        return self._config.embedding_configured()

    def embed(self, text: str) -> list[float]:
        if not self.configured():
            raise EmbeddingUnavailable("嵌入网关未配置")
        url = self._config.embedding_base_url + "/embeddings"
        payload = {
            "model": self._config.embedding_model,
            "input": text,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self._config.embedding_api_key,
            },
            method="POST",
        )
        print("[EMBED] POST /embeddings")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            raise EmbeddingUnavailable("嵌入网关不可用") from exc
        vector = _first_vector(body)
        if len(vector) != self._config.embedding_dim:
            raise EmbeddingUnavailable("嵌入维度与 EMBEDDING_DIM 不一致")
        return vector


def _first_vector(body: dict) -> list[float]:
    try:
        data = body["data"]
        if not isinstance(data, list) or not data:
            raise EmbeddingUnavailable("嵌入响应缺少向量")
        vector = data[0]["embedding"]
        if not isinstance(vector, list) or not vector:
            raise EmbeddingUnavailable("嵌入响应缺少向量")
        return [float(x) for x in vector]
    except (KeyError, TypeError, ValueError) as exc:
        raise EmbeddingUnavailable("嵌入响应无法解析") from exc
