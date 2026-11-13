# =========================================================================
# Stale endpoint memo fallback (#155009) — a transient probe failure while the
# fresh endpoint memo has expired must serve the memo's last-known window, not
# the hard 256,000 default. The stale value is never persisted.
# =========================================================================

import json
import time

from unittest.mock import patch, MagicMock


def _write_expired_memo(tmp_path, base_url: str, models: dict) -> None:
    """Write an endpoint memo whose entry is past the fresh-serve TTL."""
    import agent.model_metadata as mm

    path = tmp_path / "endpoint_model_metadata.json"
    path.write_text(json.dumps({
        base_url: {"at": time.time() - mm._ENDPOINT_MODEL_CACHE_TTL - 1, "models": models},
    }))
    return path


class TestStaleEndpointMemoFallback:
    def _isolate(self, monkeypatch, tmp_path):
        import agent.model_metadata as mm

        monkeypatch.setattr(
            mm, "_get_endpoint_metadata_cache_path", lambda: tmp_path / "endpoint_model_metadata.json"
        )
        monkeypatch.setattr(mm, "_get_context_cache_path", lambda: tmp_path / "context_length_cache.yaml")
        monkeypatch.setattr(mm, "_endpoint_model_metadata_cache", {})
        monkeypatch.setattr(mm, "_endpoint_model_metadata_cache_time", {})
        return mm

    def _dead_probe(self):
        def _raise(*args, **kwargs):
            raise TimeoutError("timed out after 15.0s; worker abandoned")
        return patch("agent.model_metadata.model_metadata_http.stream", side_effect=_raise)

    def test_expired_memo_serves_stale_context_when_probe_dead(self, tmp_path, monkeypatch):
        """The exact #155009 scenario: a healthy probe memoized 1,048,576; the memo
        expires; the next probe dead-times out. Resolution must answer from the
        stale memo instead of the 256,000 hard default."""
        mm = self._isolate(monkeypatch, tmp_path)
        base_url = "https://inference-api.nousresearch.com/v1"
        _write_expired_memo(tmp_path, base_url, {"meituan/longcat-2.5:free": {"context_length": 1_048_576}})

        with self._dead_probe(), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             patch("agent.model_metadata._resolve_provider_aware_context_length", return_value=None):
            ctx = mm.get_model_context_length(
                model="meituan/longcat-2.5:free", base_url=base_url, api_key="fake", provider="nous",
            )
        assert ctx == 1_048_576, f"stale memo value should serve; got {ctx}"

    def test_stale_fallback_is_not_persisted(self, tmp_path, monkeypatch):
        """The stale value answers the call only — like the OR-fallback rule, it must
        not be written into the persistent cache, or a later revalidation could be
        blocked by a step-1 short-circuit."""
        mm = self._isolate(monkeypatch, tmp_path)
        base_url = "https://custom.example/v1"
        _write_expired_memo(tmp_path, base_url, {"some/model": {"context_length": 131_072}})

        with self._dead_probe(), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             patch("agent.model_metadata._resolve_provider_aware_context_length", return_value=None):
            ctx = mm.get_model_context_length(
                model="some/model", base_url=base_url, api_key="fake", provider="nous",
            )
        assert ctx == 131_072
        cache_file = tmp_path / "context_length_cache.yaml"
        assert not cache_file.exists() or not (cache_file.read_text(encoding="utf-8").strip()), (
            "stale memo fallback must not be persisted"
        )

    def test_no_memo_still_falls_to_default(self, tmp_path, monkeypatch):
        """Without any memo on disk the resolution must still land on the hard
        default (and its deduped warning) — the stale read adds nothing."""
        mm = self._isolate(monkeypatch, tmp_path)

        with self._dead_probe(), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata.fetch_model_metadata", return_value={}), \
             patch("agent.model_metadata._resolve_provider_aware_context_length", return_value=None):
            ctx = mm.get_model_context_length(
                model="unknown/model", base_url="https://custom.example/v1", api_key="fake", provider="nous",
            )
        assert ctx == mm.DEFAULT_FALLBACK_CONTEXT

    def test_custom_endpoint_path_serves_stale_memo(self, tmp_path, monkeypatch):
        """The custom-endpoint branch (step 3) shares the same last-resort: a dead
        probe with an expired memo serves the memo value, not 256K."""
        mm = self._isolate(monkeypatch, tmp_path)
        base_url = "https://custom.example/v1"
        _write_expired_memo(tmp_path, base_url, {"some/model": {"context_length": 200_000}})

        with self._dead_probe(), \
             patch("agent.model_metadata.get_cached_context_length", return_value=None), \
             patch("agent.model_metadata._probe_local_context_length", return_value=None), \
             patch("agent.model_metadata._query_ollama_api_show", return_value=None):
            ctx = mm._resolve_custom_endpoint_context_length("some/model", base_url, "fake", "nous")
        assert ctx == 200_000, f"custom-endpoint path should serve stale memo; got {ctx}"

    def test_fresh_memo_still_wins_without_going_stale(self, tmp_path, monkeypatch):
        """Sanity: with a fresh memo the normal endpoint resolution answers before
        any fallback — the stale read is only reachable when the probe path fails."""
        import json as _json

        from agent.model_metadata_memo_keys import endpoint_disk_slot, endpoint_memo_key

        mm = self._isolate(monkeypatch, tmp_path)
        base_url = "https://custom.example/v1"
        path = tmp_path / "endpoint_model_metadata.json"
        # Keyed probes get their own disk slot (fingerprint suffix), so write under that slot.
        slot = endpoint_disk_slot(endpoint_memo_key(base_url, "fake"))
        path.write_text(_json.dumps({slot: {"at": time.time(), "models": {"some/model": {"context_length": 200_000}}}}))

        with patch("agent.model_metadata.model_metadata_http.stream", side_effect=AssertionError("must not probe")):
            ctx = mm._resolve_endpoint_context_length("some/model", base_url, api_key="fake")
        assert ctx == 200_000
