"""
Embeddings

OpenAI-compatible embedding API client via urllib.
Cosine similarity search over stored intent embeddings.
"""

import json
import math
import struct
import urllib.error
import urllib.request


class EmbeddingError(Exception):
    """Raised when an embedding API call fails."""
    pass


class EmbeddingClient:
    """Client for OpenAI-compatible embedding APIs."""

    def __init__(self, api_url: str, api_key: str, model: str, dimensions: int = 1536):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.dimensions = dimensions

    def embed(self, texts: list) -> list:
        """Embed a list of texts, returning a list of embedding vectors."""
        url = f"{self.api_url}/embeddings"
        payload = json.dumps({
            "input": texts,
            "model": self.model,
            "dimensions": self.dimensions,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise EmbeddingError(
                f"Embedding API returned HTTP {e.code}: {e.reason}"
            ) from e
        except urllib.error.URLError as e:
            raise EmbeddingError(
                f"Failed to connect to embedding API at {url}: {e.reason}"
            ) from e
        except json.JSONDecodeError as e:
            raise EmbeddingError(
                f"Embedding API returned invalid JSON: {e}"
            ) from e

        if "data" not in body:
            raise EmbeddingError(
                f"Embedding API response missing 'data' key: {list(body.keys())}"
            )

        # Sort by index to ensure correct order
        sorted_data = sorted(body["data"], key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in sorted_data]

    def embed_single(self, text: str) -> list:
        """Embed a single text string."""
        results = self.embed([text])
        return results[0]


def cosine_similarity(a: list, b: list) -> float:
    """Compute cosine similarity between two vectors.

    Raises ValueError if vectors have different lengths.
    """
    if len(a) != len(b):
        raise ValueError(
            f"Vector length mismatch: {len(a)} vs {len(b)}"
        )
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embedding_to_bytes(embedding: list) -> bytes:
    """Pack a float list into bytes for storage."""
    return struct.pack(f"{len(embedding)}f", *embedding)


def bytes_to_embedding(data: bytes) -> list:
    """Unpack bytes back to a float list."""
    if len(data) % 4 != 0:
        raise ValueError(
            f"Embedding data length {len(data)} is not a multiple of 4 bytes"
        )
    count = len(data) // 4
    return list(struct.unpack(f"{count}f", data))


def get_embedding_client(config: dict) -> EmbeddingClient | None:
    """Create an EmbeddingClient from config, or None if not configured."""
    api_url = config.get("embedding_api_url")
    api_key = config.get("embedding_api_key")
    model = config.get("embedding_model")
    if not api_url or not api_key or not model:
        return None
    dimensions = int(config.get("embedding_dimensions", 1536))
    return EmbeddingClient(api_url, api_key, model, dimensions)
