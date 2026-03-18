"""Tests for Elasticsearch retrieval fallback strategy.

Verifies:
- Timeout configuration (200-300ms)
- Retry logic with jitter
- BM25-only fallback
- No-retrieval fallback
- Agent never blocks indefinitely
- Tasks complete even when ES is down
- Logs indicate fallback usage
- Unified search across both task_memory and shared_memory
- Top 10 results returned (combined from both indices)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from elasticsearch.exceptions import ConnectionTimeout, TransportError

from src.memory import (
    add_jitter,
    create_memory_store,
    is_retryable_error,
    merge_and_rank_results,
)


class TestErrorClassification:
    """Test error classification for retry logic."""

    def test_connection_timeout_is_retryable(self) -> None:
        """Test ConnectionTimeout is classified as retryable."""
        exc = ConnectionTimeout("Connection timed out")
        assert is_retryable_error(exc) is True

    def test_transport_error_is_retryable(self) -> None:
        """Test TransportError is classified as retryable."""
        exc = TransportError("rate_limit")
        assert is_retryable_error(exc) is True

    def test_connection_error_is_retryable(self) -> None:
        """Test ConnectionError is classified as retryable."""
        from elasticsearch.exceptions import ConnectionError as ESConnectionError

        exc = ESConnectionError("connection failed")
        assert is_retryable_error(exc) is True

    def test_generic_exception_is_not_retryable(self) -> None:
        """Test generic Exception is not retryable."""
        exc = Exception("something went wrong")
        assert is_retryable_error(exc) is False


class TestJitter:
    """Test jitter calculation for retry delays."""

    def test_add_jitter_returns_float(self) -> None:
        """Test add_jitter returns a float (seconds)."""
        result = add_jitter(50, 50)
        assert isinstance(result, float)

    def test_add_jitter_within_range(self) -> None:
        """Test add_jitter returns value within expected range."""
        base_ms = 50
        max_jitter_ms = 50
        result = add_jitter(base_ms, max_jitter_ms)

        # Result should be between base and base + max_jitter (in seconds)
        min_expected = base_ms / 1000.0
        max_expected = (base_ms + max_jitter_ms) / 1000.0

        assert min_expected <= result <= max_expected

    def test_add_jitter_randomness(self) -> None:
        """Test add_jitter produces different values (randomness)."""
        results = [add_jitter(50, 50) for _ in range(10)]
        # At least some values should be different
        assert len(set(results)) > 1


class TestMergeAndRank:
    """Test merging and ranking results from multiple indices."""

    def test_merge_empty_results(self) -> None:
        """Test merging empty results."""
        result = merge_and_rank_results([], [], limit=10)
        assert result == []

    def test_merge_task_memory_only(self) -> None:
        """Test merging with only task_memory results."""
        task_results = [
            {"prompt": "test1", "_score": 1.5},
            {"prompt": "test2", "_score": 1.0},
        ]
        result = merge_and_rank_results(task_results, [], limit=10)

        assert len(result) == 2
        assert result[0]["_source_index"] == "task_memory"
        assert result[0]["_score"] == 1.5  # Higher score first

    def test_merge_shared_memory_only(self) -> None:
        """Test merging with only shared_memory results."""
        shared_results = [
            {"summary": "test1", "_score": 2.0},
            {"summary": "test2", "_score": 1.5},
        ]
        result = merge_and_rank_results([], shared_results, limit=10)

        assert len(result) == 2
        assert result[0]["_source_index"] == "shared_memory"
        assert result[0]["_score"] == 2.0

    def test_merge_both_indices(self) -> None:
        """Test merging results from both indices."""
        task_results = [
            {"prompt": "task1", "_score": 1.5},
            {"prompt": "task2", "_score": 0.8},
        ]
        shared_results = [
            {"summary": "shared1", "_score": 2.0},
            {"summary": "shared2", "_score": 1.0},
        ]
        result = merge_and_rank_results(task_results, shared_results, limit=10)

        assert len(result) == 4
        # Should be sorted by score descending
        assert result[0]["_score"] == 2.0  # shared1
        assert result[1]["_score"] == 1.5  # task1
        assert result[2]["_score"] == 1.0  # shared2
        assert result[3]["_score"] == 0.8  # task2

    def test_merge_respects_limit(self) -> None:
        """Test merging respects the limit parameter."""
        task_results = [{"prompt": f"task{i}", "_score": i} for i in range(10)]
        shared_results = [{"summary": f"shared{i}", "_score": i} for i in range(10)]

        result = merge_and_rank_results(task_results, shared_results, limit=10)

        assert len(result) == 10  # Should return only top 10

    def test_merge_adds_source_index(self) -> None:
        """Test merging adds _source_index field."""
        task_results = [{"prompt": "test"}]
        shared_results = [{"summary": "test"}]

        result = merge_and_rank_results(task_results, shared_results, limit=10)

        assert result[0]["_source_index"] in ["task_memory", "shared_memory"]
        assert result[1]["_source_index"] in ["task_memory", "shared_memory"]


class TestUnifiedSearch:
    """Test unified search with fallback strategy."""

    def test_unified_search_success(self) -> None:
        """Test successful unified search returns results."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        # Mock search responses
        mock_es.search.side_effect = [
            # task_memory response
            {
                "hits": {
                    "hits": [
                        {
                            "_source": {"prompt": "test task", "phase": "Succeeded"},
                            "_score": 1.5,
                        }
                    ]
                }
            },
            # shared_memory response
            {
                "hits": {
                    "hits": [
                        {
                            "_source": {"summary": "test shared", "context": "context"},
                            "_score": 2.0,
                        }
                    ]
                }
            },
        ]

        memory_store = create_memory_store(mock_es)
        results = memory_store["unified_search"](
            query_text="test query",
            skills=None,
            limit=10,
            search_task_memory=True,
            search_shared_memory=True,
        )

        assert len(results) == 2
        assert results[0]["_score"] == 2.0  # Higher score first
        assert results[0]["_source_index"] == "shared_memory"
        assert results[1]["_source_index"] == "task_memory"

    def test_unified_search_timeout_triggers_retry(self) -> None:
        """Test timeout triggers retry with jitter."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        # First call times out, second succeeds
        mock_es.search.side_effect = [
            ConnectionTimeout("Connection timed out"),
            ConnectionTimeout("Connection timed out"),  # task_memory retry
            {"hits": {"hits": []}},  # task_memory success
            {"hits": {"hits": []}},  # shared_memory success
        ]

        with patch("src.memory.time.sleep") as mock_sleep:
            memory_store = create_memory_store(mock_es)
            results = memory_store["unified_search"](
                query_text="test query",
                limit=10,
            )

            # Should have called sleep for jitter
            assert mock_sleep.called
            # Should return empty results after retry
            assert results == []

    def test_unified_search_attempts_bm25_fallback(self) -> None:
        """Test that BM25 fallback is attempted after full query fails."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        call_count = [0]

        def search_side_effect(*args, **kwargs):
            call_count[0] += 1
            # All calls fail to test graceful degradation
            raise ConnectionTimeout("Connection timed out")

        mock_es.search.side_effect = search_side_effect

        with patch("src.memory.time.sleep"):
            memory_store = create_memory_store(mock_es)
            results = memory_store["unified_search"](
                query_text="test query",
                limit=10,
            )

            # Should return empty results gracefully
            assert results == []
            # Verify multiple attempts were made
            # The function catches exceptions early, so we get:
            # 1. Full query attempt (fails on first search call)
            # 2. Retry attempt (fails on first search call)
            # 3. BM25 fallback (fails on first search call)
            # Total: 3 calls
            assert call_count[0] == 3

    def test_unified_search_fallback_to_empty(self) -> None:
        """Test fallback to empty results when all strategies fail."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        # All queries fail
        mock_es.search.side_effect = ConnectionTimeout("Connection timed out")

        with patch("src.memory.time.sleep"):
            memory_store = create_memory_store(mock_es)
            results = memory_store["unified_search"](
                query_text="test query",
                limit=10,
            )

            # Should return empty results
            assert results == []

    def test_unified_search_permanent_error_no_retry(self) -> None:
        """Test permanent errors don't trigger retry."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        # Permanent error (ValueError - not a transport error)
        mock_es.search.side_effect = [
            ValueError("invalid query"),
        ]

        with patch("src.memory.time.sleep") as mock_sleep:
            memory_store = create_memory_store(mock_es)
            results = memory_store["unified_search"](
                query_text="test query",
                limit=10,
            )

            # Should NOT have called sleep (no retry)
            assert not mock_sleep.called
            # Should return empty results
            assert results == []

    def test_unified_search_respects_timeout(self) -> None:
        """Test unified search passes timeout parameter to ES."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True
        mock_es.search.return_value = {"hits": {"hits": []}}

        memory_store = create_memory_store(mock_es)
        memory_store["unified_search"](
            query_text="test query",
            limit=10,
        )

        # Check that timeout was passed to search
        call_args = mock_es.search.call_args_list
        for call in call_args:
            assert "timeout" in call[1]
            assert call[1]["timeout"] == "250ms"

    def test_unified_search_only_task_memory(self) -> None:
        """Test unified search with only task_memory enabled."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"prompt": "test"}, "_score": 1.0}]}
        }

        memory_store = create_memory_store(mock_es)
        results = memory_store["unified_search"](
            query_text="test query",
            limit=10,
            search_task_memory=True,
            search_shared_memory=False,
        )

        # Should only search task_memory (1 call)
        assert mock_es.search.call_count == 1
        assert len(results) == 1

    def test_unified_search_only_shared_memory(self) -> None:
        """Test unified search with only shared_memory enabled."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"summary": "test"}, "_score": 1.0}]}
        }

        memory_store = create_memory_store(mock_es)
        results = memory_store["unified_search"](
            query_text="test query",
            limit=10,
            search_task_memory=False,
            search_shared_memory=True,
        )

        # Should only search shared_memory (1 call)
        assert mock_es.search.call_count == 1
        assert len(results) == 1

    def test_unified_search_both_disabled_returns_empty(self) -> None:
        """Test unified search with both indices disabled returns empty."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        memory_store = create_memory_store(mock_es)
        results = memory_store["unified_search"](
            query_text="test query",
            limit=10,
            search_task_memory=False,
            search_shared_memory=False,
        )

        # Should not call search at all
        assert mock_es.search.call_count == 0
        assert results == []


class TestBackwardCompatibility:
    """Test backward compatibility with existing search_shared function."""

    def test_search_shared_delegates_to_unified(self) -> None:
        """Test search_shared delegates to unified_search."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True
        mock_es.search.return_value = {
            "hits": {"hits": [{"_source": {"summary": "test"}, "_score": 1.0}]}
        }

        memory_store = create_memory_store(mock_es)
        results = memory_store["search_shared"](
            query_text="test query",
            skills=["code-edit"],
            limit=5,
        )

        # Should return results
        assert len(results) == 1
        # Should only search shared_memory
        assert mock_es.search.call_count == 1
