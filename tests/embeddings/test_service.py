"""Tests for ``carpenter.embeddings.service.EmbeddingService``.

These cover the API contract for Phase 2 PR-1: happy-path embedding,
model identity format, idempotent warm-up, lock serialisation,
config-driven provider switching, and singleton identity.  The KB
integration tests (``tests/kb/test_search.py``) cover the shim layer.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from carpenter.embeddings import (
    EmbeddingModelMismatchError,
    EmbeddingService,
    get_embedding_service,
    reset_embedding_service,
)
from carpenter.embeddings import service as svc_mod


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _StaticProvider:
    """Returns one fixed vector per input text; counts calls."""

    def __init__(self, dim: int = 384, model_name: str = "fake-model") -> None:
        self.model_name = model_name
        self.vector_dim = dim
        self.calls: list[list[str]] = []
        self._ready = False

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[0.1] * self.vector_dim for _ in texts]

    def is_ready(self) -> bool:
        return self._ready


class _CountingProvider:
    """Increments a counter, sleeps briefly, returns deterministic vectors.

    Used to detect overlapping concurrent calls — if the lock works,
    ``in_flight`` should never exceed 1.
    """

    def __init__(self) -> None:
        self.model_name = "counter"
        self.vector_dim = 4
        self._in_flight = 0
        self.max_in_flight = 0
        self._cond = threading.Lock()
        self.total_calls = 0

    def embed(self, texts):
        with self._cond:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
            self.total_calls += 1
        try:
            # Tiny sleep so concurrent callers actually overlap if the
            # service's lock is missing.
            time.sleep(0.02)
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]
        finally:
            with self._cond:
                self._in_flight -= 1

    def is_ready(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

class TestEmbedHappyPath:
    def test_returns_one_vector_per_input(self):
        prov = _StaticProvider(dim=8)
        service = EmbeddingService(prov, batch_size=10)
        out = service.embed(["a", "b", "c"])
        assert len(out) == 3
        assert all(len(v) == 8 for v in out)

    def test_empty_input(self):
        prov = _StaticProvider()
        service = EmbeddingService(prov)
        assert service.embed([]) == []

    def test_batches_larger_than_batch_size(self):
        prov = _StaticProvider(dim=4)
        service = EmbeddingService(prov, batch_size=2)
        out = service.embed(["a", "b", "c", "d", "e"])
        assert len(out) == 5
        # Batches of (2, 2, 1)
        assert [len(c) for c in prov.calls] == [2, 2, 1]

    def test_batch_size_floored_to_one(self):
        prov = _StaticProvider(dim=4)
        service = EmbeddingService(prov, batch_size=0)
        out = service.embed(["a", "b"])
        assert len(out) == 2


# ---------------------------------------------------------------------------
# Identity / introspection
# ---------------------------------------------------------------------------

class TestModelIdentity:
    def test_format(self):
        prov = _StaticProvider(dim=384, model_name="all-MiniLM-L6-v2")
        service = EmbeddingService(prov, provider_kind="local")
        assert service.model_identity == "local:all-MiniLM-L6-v2:384"

    def test_ollama_kind(self):
        prov = _StaticProvider(dim=768, model_name="nomic-embed-text")
        service = EmbeddingService(prov, provider_kind="ollama")
        assert service.model_identity == "ollama:nomic-embed-text:768"

    def test_vector_dim_and_model_name_passthrough(self):
        prov = _StaticProvider(dim=16, model_name="tiny")
        service = EmbeddingService(prov)
        assert service.vector_dim == 16
        assert service.model_name == "tiny"


# ---------------------------------------------------------------------------
# warm_up()
# ---------------------------------------------------------------------------

class TestWarmUp:
    def test_local_calls_provider_once(self):
        prov = _StaticProvider()
        service = EmbeddingService(prov, provider_kind="local")
        service.warm_up()
        assert len(prov.calls) == 1

    def test_idempotent(self):
        prov = _StaticProvider()
        service = EmbeddingService(prov, provider_kind="local")
        service.warm_up()
        service.warm_up()
        service.warm_up()
        # warm-up should hit the provider exactly once
        assert len(prov.calls) == 1

    def test_ollama_is_noop(self):
        prov = _StaticProvider()
        service = EmbeddingService(prov, provider_kind="ollama")
        service.warm_up()
        assert prov.calls == []

    def test_failure_is_swallowed(self):
        class _BrokenProvider:
            model_name = "broken"
            vector_dim = 4

            def embed(self, texts):
                raise RuntimeError("nope")

            def is_ready(self):
                return False

        service = EmbeddingService(
            _BrokenProvider(), provider_kind="local",
        )
        # Should not raise; warm-up failure is logged as a warning.
        service.warm_up()


# ---------------------------------------------------------------------------
# Lock serialisation (D5)
# ---------------------------------------------------------------------------

class TestLockSerialisation:
    def test_concurrent_calls_are_serialised(self):
        prov = _CountingProvider()
        service = EmbeddingService(prov, batch_size=10)

        threads = [
            threading.Thread(target=service.embed, args=(["t"],))
            for _ in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Even under contention, no two embed() calls should overlap.
        assert prov.max_in_flight == 1, (
            f"Provider saw {prov.max_in_flight} concurrent calls; "
            "process-wide lock is missing or broken."
        )
        assert prov.total_calls == 8


# ---------------------------------------------------------------------------
# Config-driven provider switching
# ---------------------------------------------------------------------------

class TestConfigSwitch:
    def setup_method(self):
        reset_embedding_service()

    def teardown_method(self):
        reset_embedding_service()

    def test_local_default(self):
        with patch.object(svc_mod, "CONFIG", {"embedding": {"provider": "local"}}):
            service = get_embedding_service()
            assert service.provider_kind == "local"

    def test_ollama_via_config(self):
        with patch.object(svc_mod, "CONFIG", {
            "embedding": {
                "provider": "ollama",
                "ollama": {
                    "url": "http://test:11434",
                    "model": "nomic-embed-text",
                    "dim": 768,
                },
            },
        }):
            # Patch the actual HTTP call so no network is touched.
            with patch("carpenter.embeddings.providers.ollama.httpx.post") as mock_post:
                mock_post.return_value.json.return_value = {
                    "embeddings": [[0.1] * 768, [0.2] * 768],
                }
                mock_post.return_value.raise_for_status = lambda: None

                service = get_embedding_service()
                assert service.provider_kind == "ollama"
                assert service.model_identity.startswith("ollama:")
                out = service.embed(["a", "b"])
                assert len(out) == 2
                assert len(out[0]) == 768

    def test_unknown_provider_falls_back_to_local(self):
        with patch.object(svc_mod, "CONFIG", {"embedding": {"provider": "bogus"}}):
            service = get_embedding_service()
            assert service.provider_kind == "local"

    def test_batch_size_from_config(self):
        with patch.object(svc_mod, "CONFIG", {
            "embedding": {"provider": "local", "batch_size": 25},
        }):
            service = get_embedding_service()
            assert service.batch_size == 25


# ---------------------------------------------------------------------------
# Singleton identity
# ---------------------------------------------------------------------------

class TestSingleton:
    def setup_method(self):
        reset_embedding_service()

    def teardown_method(self):
        reset_embedding_service()

    def test_returns_same_instance(self):
        a = get_embedding_service()
        b = get_embedding_service()
        c = get_embedding_service()
        assert a is b is c

    def test_reset_creates_new_instance(self):
        a = get_embedding_service()
        reset_embedding_service()
        b = get_embedding_service()
        assert a is not b


# ---------------------------------------------------------------------------
# Exported error type
# ---------------------------------------------------------------------------

def test_model_mismatch_error_is_exported():
    """PR-2 will raise this from PackageVectorStore; PR-1 just exports it."""
    assert issubclass(EmbeddingModelMismatchError, RuntimeError)
