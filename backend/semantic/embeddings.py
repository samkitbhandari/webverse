"""Embeddings and the LanceDB vector index.

Embeddings here do exactly one job: **generate candidates**. They surface the
entities and claims a new passage might be about, so the semantic compiler can
be grounded in what the graph already holds and entity resolution has something
to score. They never decide what is true -- similarity is not evidence.

Offline the ``HashingEmbedder`` takes over. It is honest about what it is: a
hashed word/character n-gram projection, so it captures lexical overlap and not
meaning. "Battery pack BP-7" and "BP-7 traction battery" land close together;
"vehicle" and "car" do not. For candidate retrieval on a corpus that shares
vocabulary that is enough, and it keeps the system runnable with no network.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from backend.core.config import settings

log = logging.getLogger("nexus.embeddings")

HASH_DIM = 384


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class HashingEmbedder:
    """Deterministic, offline, no dependencies beyond the standard library."""

    name = "hashing"
    dim = HASH_DIM

    _token_re = re.compile(r"[a-z0-9]+")

    def _features(self, text: str) -> list[str]:
        low = (text or "").lower()
        words = self._token_re.findall(low)
        feats: list[str] = list(words)
        feats += [f"{a}_{b}" for a, b in zip(words, words[1:])]      # bigrams
        for w in words:                                              # char 4-grams
            if len(w) > 4:
                feats += [f"#{w[i:i+4]}" for i in range(len(w) - 3)]
        return feats

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            feats = self._features(text)
            if not feats:
                out.append(vec)
                continue
            # sublinear term weighting, the usual antidote to repeated tokens
            counts: dict[str, int] = {}
            for f in feats:
                counts[f] = counts.get(f, 0) + 1
            for feat, n in counts.items():
                h = int.from_bytes(
                    hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little"
                )
                idx = h % self.dim
                sign = 1.0 if (h >> 63) & 1 else -1.0
                vec[idx] += sign * (1.0 + math.log(n))
            out.append(_normalise(vec))
        return out


class OpenAIEmbedder:
    name = "openai"

    def __init__(self, api_key: str, model: str, dim: int):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        clean = [t.replace("\n", " ").strip() or " " for t in texts]
        try:
            resp = self.client.embeddings.create(model=self.model, input=clean)
            return [d.embedding for d in resp.data]
        except Exception as exc:
            log.warning("embedding call failed (%s); falling back to hashing", exc)
            fallback = HashingEmbedder()
            return [
                v + [0.0] * (self.dim - fallback.dim) if self.dim > fallback.dim else v[: self.dim]
                for v in fallback.embed(clean)
            ]


_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        if settings.openai_api_key:
            try:
                _embedder = OpenAIEmbedder(
                    settings.openai_api_key, settings.embedding_model, settings.embedding_dim
                )
            except Exception as exc:
                log.warning("openai embedder init failed (%s)", exc)
                _embedder = HashingEmbedder()
        else:
            _embedder = HashingEmbedder()
        log.info("embedder: %s (dim=%d)", _embedder.name, _embedder.dim)
    return _embedder


def reset_embedder() -> None:
    global _embedder
    _embedder = None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ---------------------------------------------------------------------------
# Vector index
# ---------------------------------------------------------------------------
TABLE = "chunks"


class VectorIndex:
    """LanceDB-backed store of embedded chunks and node texts."""

    def __init__(self, path: Path | None = None, embedder: Embedder | None = None):
        import lancedb

        self.embedder = embedder or get_embedder()
        self.path = path or settings.lance_dir
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = lancedb.connect(str(self.path))
        self._table = None
        self._ensure_table()

    def _schema(self):
        import pyarrow as pa

        return pa.schema(
            [
                pa.field("id", pa.string()),
                pa.field("kind", pa.string()),          # "chunk" | "node"
                pa.field("text", pa.string()),
                pa.field("node_id", pa.string()),
                pa.field("source_path", pa.string()),
                pa.field("source_label", pa.string()),
                pa.field("locator", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), self.embedder.dim)),
            ]
        )

    def _list_tables(self) -> set[str]:
        """Table names, across LanceDB API generations.

        ``table_names()`` returned a plain list and is deprecated;
        ``list_tables()`` returns a paginated ``ListTablesResponse``. Both are
        handled so the code works either side of the rename, and pagination is
        followed rather than assumed to fit in one page.
        """
        lister = getattr(self.db, "list_tables", None)
        if lister is None:
            return set(self.db.table_names())

        names: set[str] = set()
        token = None
        while True:
            page = lister(page_token=token) if token else lister()
            if isinstance(page, (list, tuple, set)):
                names.update(page)
                return names
            names.update(getattr(page, "tables", []) or [])
            token = getattr(page, "page_token", None)
            if not token:
                return names

    def _ensure_table(self) -> None:
        names = self._list_tables()
        if TABLE in names:
            tbl = self.db.open_table(TABLE)
            field = tbl.schema.field("vector")
            existing_dim = getattr(field.type, "list_size", None)
            if existing_dim and existing_dim != self.embedder.dim:
                # Switching embedders changes dimensionality; an index built by
                # the other one is unusable rather than merely stale.
                log.warning(
                    "vector index dim %s != embedder dim %s; rebuilding",
                    existing_dim, self.embedder.dim,
                )
                self.db.drop_table(TABLE)
                self._table = self.db.create_table(TABLE, schema=self._schema())
                return
            self._table = tbl
            return
        self._table = self.db.create_table(TABLE, schema=self._schema())

    @property
    def table(self):
        if self._table is None:
            self._ensure_table()
        return self._table

    # --- writes -----------------------------------------------------------
    def add(self, records: Sequence[dict[str, Any]]) -> int:
        """Records need 'id' and 'text'; everything else is optional metadata."""
        rows = [r for r in records if (r.get("text") or "").strip()]
        if not rows:
            return 0
        vectors = self.embedder.embed([r["text"] for r in rows])
        payload = [
            {
                "id": str(r["id"]),
                "kind": r.get("kind", "chunk"),
                "text": r["text"][:8000],
                "node_id": r.get("node_id", "") or "",
                "source_path": r.get("source_path", "") or "",
                "source_label": r.get("source_label", "") or "",
                "locator": r.get("locator", "") or "",
                "vector": v,
            }
            for r, v in zip(rows, vectors)
        ]
        with self._lock:
            ids = "', '".join(p["id"].replace("'", "''") for p in payload)
            try:
                self.table.delete(f"id IN ('{ids}')")     # upsert semantics
            except Exception:
                pass
            self.table.add(payload)
        return len(payload)

    def delete_source(self, source_path: str) -> None:
        with self._lock:
            try:
                self.table.delete(f"source_path = '{source_path.replace(chr(39), chr(39)*2)}'")
            except Exception as exc:
                log.warning("vector delete failed for %s: %s", source_path, exc)

    def delete_node(self, node_id: str) -> None:
        with self._lock:
            try:
                self.table.delete(f"node_id = '{node_id}'")
            except Exception:
                pass

    # --- reads ------------------------------------------------------------
    def search(
        self, query: str, k: int = 8, kind: str | None = None, exclude_source: str | None = None
    ) -> list[dict[str, Any]]:
        vec = self.embedder.embed([query])[0]
        with self._lock:
            try:
                q = self.table.search(vec).limit(max(k * 3, k))
                rows = q.to_list()
            except Exception as exc:
                log.warning("vector search failed: %s", exc)
                return []
        out: list[dict[str, Any]] = []
        for r in rows:
            if kind and r.get("kind") != kind:
                continue
            if exclude_source and r.get("source_path") == exclude_source:
                continue
            dist = float(r.get("_distance", 0.0))
            r["score"] = round(1.0 / (1.0 + dist), 4)
            r.pop("vector", None)
            out.append(r)
            if len(out) >= k:
                break
        return out

    def count(self) -> int:
        try:
            return int(self.table.count_rows())
        except Exception:
            return 0


_index: VectorIndex | None = None


def get_index() -> VectorIndex:
    global _index
    if _index is None:
        settings.ensure_dirs()
        _index = VectorIndex()
    return _index


def set_index(index: VectorIndex | None) -> None:
    global _index
    _index = index
