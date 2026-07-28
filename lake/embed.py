"""Local CPU embeddings (spec 10 §3.2).

`/v1/embeddings` answers 501 on all three school servers, so vectors are computed
here. The prefix is asymmetric: query only (09:94).
"""
import threading

import numpy as np
from sentence_transformers import SentenceTransformer

from .models import EMBED_DIM

MODEL = "Snowflake/snowflake-arctic-embed-s"           # 384d, Apache-2.0 (09:94)
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Loaded once per module, lazily: /retrieve runs in a ThreadingHTTPServer and a
# per-thread load would cost memory and seconds on every request (§3.2).
_model: SentenceTransformer | None = None
_load_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _load_lock:
            if _model is None:
                _model = SentenceTransformer(MODEL)
    return _model


def embed_docs(texts: list[str]) -> np.ndarray:
    """(n, 384) float32, L2-normalized. No prefix, no cache (§3.2)."""
    if not texts:
        return np.zeros((0, EMBED_DIM), dtype=np.float32)
    return _get_model().encode(texts, normalize_embeddings=True,
                               convert_to_numpy=True).astype(np.float32)


def embed_query(text: str) -> np.ndarray:
    """(384,) float32, L2-normalized, with QUERY_PREFIX."""
    return _get_model().encode(QUERY_PREFIX + text, normalize_embeddings=True,
                               convert_to_numpy=True).astype(np.float32)


if __name__ == "__main__":
    docs = ["cascaded evaluation: cheap proxy filters candidates before expensive scoring",
            "island model with periodic migration maintains diversity"]
    D = embed_docs(docs)
    assert D.shape == (2, EMBED_DIM), D.shape
    assert D.dtype == np.float32, D.dtype
    assert np.allclose(np.linalg.norm(D, axis=1), 1.0, atol=1e-5)

    q = embed_query("speed up candidate evaluation with a cheap proxy")
    assert q.shape == (EMBED_DIM,), q.shape
    sims = q @ D.T
    assert sims[0] > sims[1], sims          # near text beats far text
    assert embed_docs([]).shape == (0, EMBED_DIM)
    print(f"ok: shape={D.shape} sims={np.round(sims, 3).tolist()} "
          f"contrast={sims[0] - sims[1]:.3f}")
