"""Text embeddings. ruri-v3-310m (cl-nagoya, Apache-2.0, 768 dims, Japanese): documents and queries get their own
prefix ("検索文書: " / "検索クエリ: "), as the model card says. The model is pinned to one revision and read from
/models; the API container has no internet, so the worker downloads it first (HF_HUB_OFFLINE in the API).
EMBEDDER=fake gives a deterministic stand-in for tests (no model)."""
from __future__ import annotations

import hashlib
import math
import os
import re
from functools import lru_cache

DIM = 768
RURI = "cl-nagoya/ruri-v3-310m"
RURI_REVISION = "18b60fb8c2b9df296fb4212bb7d23ef94e579cd3"  # 2025-04-17


class Embedder:
    name = "ruri_v3"

    def __init__(self, device: str | None = None) -> None:
        import torch
        from sentence_transformers import SentenceTransformer
        # EMBED_DEVICE: cpu, cuda (compose.gpu.yml) or auto (default: the GPU when this torch build sees one)
        wanted = device or os.environ.get("EMBED_DEVICE", "auto")
        device = ("cuda" if torch.cuda.is_available() else "cpu") if wanted == "auto" else wanted
        # as many threads as the container may use (compose: cpus), not the host's core count: more only waits
        if os.environ.get("TORCH_THREADS"):
            torch.set_num_threads(int(os.environ["TORCH_THREADS"]))
        self.model = SentenceTransformer(RURI, revision=RURI_REVISION, device=device, cache_folder=os.environ.get("HF_HOME", "/models/hf"))

    def documents(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(["検索文書: " + t for t in texts], batch_size=16, normalize_embeddings=True).tolist()

    def query(self, text: str) -> list[float]:
        return self.model.encode("検索クエリ: " + text, normalize_embeddings=True).tolist()


class FakeEmbedder:
    """Bag of character bigrams hashed into 768 dims: similar texts get similar vectors, no model needed."""
    name = "fake"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIM
        s = re.sub(r"\s+", "", text)
        for i in range(len(s) - 1):
            v[int(hashlib.md5(s[i:i + 2].encode()).hexdigest(), 16) % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def query(self, text: str) -> list[float]:
        return self._vec(text)


@lru_cache
def embedder() -> Embedder | FakeEmbedder:
    return FakeEmbedder() if os.environ.get("EMBEDDER") == "fake" else Embedder()


def literal(vec: list[float]) -> str:
    """pgvector's text form."""
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
