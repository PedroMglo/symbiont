"""BM25 sparse vectorizer — produces Qdrant-compatible SparseVector dicts.

Custom Okapi BM25 implementation (~100 lines) that:
  1. Builds a vocabulary + IDF table from a corpus at ingest time.
  2. Converts a token list into ``{"indices": [...], "values": [...]}``
     ready for Qdrant's ``SparseVector`` storage.

Zero external dependencies — tokenisation is whitespace + punctuation strip.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

# Default BM25 parameters (Okapi)
_K1 = 1.5
_B = 0.75

# Simple tokeniser: lowercase, strip accents, split on non-alphanum
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase, normalise (strip accents), split on non-alphanum. Returns token list."""
    normalised = unicodedata.normalize("NFKD", text)
    # Remove combining marks (accents) so "café" → "cafe"
    stripped = "".join(ch for ch in normalised if unicodedata.category(ch) != "Mn")
    return _TOKEN_RE.findall(stripped.lower())


class BM25Vectorizer:
    """Okapi BM25 sparse vectorizer backed by a vocabulary + IDF table.

    Typical lifecycle::

        # At ingest time
        vec = BM25Vectorizer()
        vec.fit(corpus_tokens)          # list[list[str]]
        vec.save(path)

        # At query time
        vec = BM25Vectorizer.load(path)
        sparse = vec.transform(query_tokens)
        # sparse == {"indices": [12, 45, ...], "values": [1.23, 0.87, ...]}
    """

    def __init__(
        self,
        *,
        k1: float = _K1,
        b: float = _B,
    ) -> None:
        self.k1 = k1
        self.b = b
        # Populated by fit()
        self._vocab: dict[str, int] = {}       # token → index
        self._idf: dict[int, float] = {}        # index → IDF value
        self._avgdl: float = 0.0
        self._n_docs: int = 0

    # ------------------------------------------------------------------
    # Fit
    # ------------------------------------------------------------------

    def fit(self, corpus: list[list[str]]) -> "BM25Vectorizer":
        """Build vocab + IDF from a tokenised corpus.

        Args:
            corpus: list of documents, each a list of tokens.
        """
        n = len(corpus)
        if n == 0:
            return self
        self._n_docs = n

        # 1. Build vocabulary and document frequency (df)
        df: dict[str, int] = {}
        total_len = 0
        for tokens in corpus:
            total_len += len(tokens)
            seen: set[str] = set()
            for tok in tokens:
                if tok not in seen:
                    df[tok] = df.get(tok, 0) + 1
                    seen.add(tok)

        self._avgdl = total_len / n if n else 1.0

        # 2. Assign indices and compute IDF
        vocab: dict[str, int] = {}
        idf: dict[int, float] = {}
        for idx, (tok, freq) in enumerate(sorted(df.items())):
            vocab[tok] = idx
            # Standard BM25 IDF: log((N - df + 0.5) / (df + 0.5) + 1)
            idf[idx] = math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)

        self._vocab = vocab
        self._idf = idf
        self._df = df
        self._total_len = total_len
        return self

    def update(self, new_docs: list[list[str]]) -> "BM25Vectorizer":
        """Incrementally add new documents to the vocabulary + IDF.

        Expands the vocabulary with new terms and recalculates IDF values
        for all affected terms. Much faster than full fit() for small batches.
        """
        if not new_docs:
            return self
        if not self._n_docs:
            return self.fit(new_docs)

        # Get existing df (rebuild from vocab if not cached)
        if not hasattr(self, "_df") or self._df is None:
            self._df = {}
            self._total_len = int(self._avgdl * self._n_docs)

        df = self._df
        total_len = getattr(self, "_total_len", int(self._avgdl * self._n_docs))

        for tokens in new_docs:
            total_len += len(tokens)
            seen: set[str] = set()
            for tok in tokens:
                if tok not in seen:
                    df[tok] = df.get(tok, 0) + 1
                    seen.add(tok)

        n = self._n_docs + len(new_docs)
        self._n_docs = n
        self._avgdl = total_len / n if n else 1.0
        self._total_len = total_len

        # Rebuild vocab + IDF (indices may shift for new terms)
        vocab: dict[str, int] = {}
        idf: dict[int, float] = {}
        for idx, (tok, freq) in enumerate(sorted(df.items())):
            vocab[tok] = idx
            idf[idx] = math.log((n - freq + 0.5) / (freq + 0.5) + 1.0)

        self._vocab = vocab
        self._idf = idf
        self._df = df
        return self

    # ------------------------------------------------------------------
    # Transform
    # ------------------------------------------------------------------

    def transform(self, tokens: list[str], *, doc_len: int | None = None) -> dict:
        """Convert tokens to a sparse vector dict ``{indices, values}``.

        For **documents** pass the actual token count as *doc_len* (used in
        the BM25 length normalisation term).  For **queries** omit *doc_len*
        to use the corpus average — this matches the standard BM25 scoring
        where the query TF component is un-normalised.
        """
        if not self._vocab:
            return {"indices": [], "values": []}

        dl = doc_len if doc_len is not None else self._avgdl

        # Term frequency
        tf: dict[int, int] = {}
        for tok in tokens:
            idx = self._vocab.get(tok)
            if idx is not None:
                tf[idx] = tf.get(idx, 0) + 1

        indices: list[int] = []
        values: list[float] = []
        for idx, freq in sorted(tf.items()):
            idf = self._idf.get(idx, 0.0)
            # BM25 TF component
            numerator = freq * (self.k1 + 1)
            denominator = freq + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
            score = idf * (numerator / denominator)
            if score > 0:
                indices.append(idx)
                values.append(score)

        return {"indices": indices, "values": values}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def vocab_hash(self) -> str:
        """Stable short hash of the vocabulary (for drift/version detection)."""
        digest = hashlib.sha256()
        for term in sorted(self._vocab):
            digest.update(term.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:16]

    def save(self, path: str | Path) -> None:
        """Persist vocab + IDF to a JSON file (with governance metadata)."""
        data = {
            "k1": self.k1,
            "b": self.b,
            "vocab": self._vocab,
            "idf": {str(k): v for k, v in self._idf.items()},
            "avgdl": self._avgdl,
            "n_docs": self._n_docs,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "vocab_hash": self.vocab_hash(),
        }
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "BM25Vectorizer":
        """Load a previously saved vectorizer. Unknown keys are ignored."""
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        vec = cls(k1=data["k1"], b=data["b"])
        vec._vocab = data["vocab"]
        vec._idf = {int(k): v for k, v in data["idf"].items()}
        vec._avgdl = data["avgdl"]
        vec._n_docs = data["n_docs"]
        return vec

    @property
    def vocab_size(self) -> int:
        """Number of terms in the vocabulary."""
        return len(self._vocab)

    @property
    def fitted(self) -> bool:
        """True if ``fit()`` has been called with a non-empty corpus."""
        return self._n_docs > 0


def bm25_status(collection: str, data_dir: str | Path | None = None) -> dict:
    """Return governance/health info for a collection's persisted BM25 model.

    Reads the on-disk JSON without loading the full vectorizer. Always returns
    a dict; ``available`` is False when the model file is missing or unreadable.
    """
    if data_dir is None:
        from rag_config import settings

        data_dir = settings.paths.data_dir
    path = Path(data_dir) / "bm25" / f"{collection}.json"
    if not path.exists():
        return {"collection": collection, "available": False, "path": str(path)}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report unreadable model gracefully
        return {
            "collection": collection,
            "available": False,
            "path": str(path),
            "error": str(exc),
        }

    generated_at = data.get("generated_at")
    age_seconds: float | None = None
    if generated_at:
        try:
            ts = dt.datetime.fromisoformat(generated_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            age_seconds = round(
                (dt.datetime.now(dt.timezone.utc) - ts).total_seconds(), 1
            )
        except ValueError:
            age_seconds = None

    return {
        "collection": collection,
        "available": True,
        "path": str(path),
        "vocab_size": len(data.get("vocab", {})),
        "n_docs": data.get("n_docs", 0),
        "avgdl": data.get("avgdl", 0.0),
        "generated_at": generated_at,
        "age_seconds": age_seconds,
        "vocab_hash": data.get("vocab_hash"),
    }
