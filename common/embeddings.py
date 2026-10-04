"""Embeddings from the Azure OpenAI deployment in the Foundry resource (Entra ID auth, HTTPS)."""
from functools import lru_cache

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

_SCOPE = "https://cognitiveservices.azure.com/.default"


def openai_client(endpoint: str, api_version: str) -> AzureOpenAI:
    if not endpoint.startswith("https://"):
        raise ValueError(f"Azure OpenAI endpoint must use HTTPS: {endpoint}")
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_version=api_version,
        azure_ad_token_provider=get_bearer_token_provider(DefaultAzureCredential(), _SCOPE),
        timeout=30,
        max_retries=3,
    )


class Embedder:
    def __init__(self, client: AzureOpenAI, deployment: str, dimensions: int, batch_size: int = 64):
        self.client, self.deployment, self.dimensions, self.batch_size = client, deployment, dimensions, batch_size
        self.embed_query = lru_cache(maxsize=256)(self._embed_query)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            resp = self.client.embeddings.create(
                model=self.deployment, input=texts[i:i + self.batch_size], dimensions=self.dimensions)
            vectors.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        for v in vectors:
            if len(v) != self.dimensions:
                raise ValueError(f"Expected {self.dimensions}-dim embeddings, got {len(v)}.")
        return vectors

    def _embed_query(self, text: str) -> tuple[float, ...]:
        return tuple(self.embed([text])[0])
