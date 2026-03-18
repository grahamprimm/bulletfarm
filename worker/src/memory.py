"""Elasticsearch memory store for agent task results and shared knowledge.

Uses dependency injection — the ES client is passed in, not imported globally.
Two indices:
  - task_memory: per-task results and context
  - shared_memory: reusable knowledge across tasks

Memory Write Pipeline:
  - Buffers documents during task execution
  - Flushes to ES using bulk API only on task completion (success/failure)
  - Optional intermediate writes for long-running tasks (> 5 minutes)

Retrieval Fallback Strategy:
  - Timeout: 200-300ms per query
  - Retry once with jitter
  - Fallback to BM25-only (simpler query)
  - Fallback to no retrieval (empty results)
  - Agent never blocks indefinitely on ES
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
from typing import Any, TypedDict

from elasticsearch import Elasticsearch, helpers
from elasticsearch.exceptions import (
    ConnectionError,
    ConnectionTimeout,
    TransportError,
)

logger = logging.getLogger(__name__)

TASK_MEMORY_INDEX: str = "task_memory"
SHARED_MEMORY_INDEX: str = "shared_memory"

TASK_MEMORY_MAPPING: dict[str, Any] = {
    "mappings": {
        "properties": {
            "task_id": {"type": "keyword"},
            "agent_ref": {"type": "keyword"},
            "repository": {"type": "keyword"},
            "prompt": {"type": "text"},
            "output": {"type": "text"},
            "skills_used": {"type": "keyword"},
            "phase": {"type": "keyword"},
            "error": {"type": "text"},
            "pr_url": {"type": "keyword"},
            "rate_limited": {"type": "boolean"},
            "timestamp": {"type": "date"},
        }
    }
}

SHARED_MEMORY_MAPPING: dict[str, Any] = {
    "mappings": {
        "properties": {
            "summary": {"type": "text"},
            "context": {"type": "text"},
            "skills": {"type": "keyword"},
            "repository": {"type": "keyword"},
            "timestamp": {"type": "date"},
        }
    }
}

# Retrieval configuration
ES_QUERY_TIMEOUT_MS: int = 250  # 250ms timeout per query
ES_RETRY_JITTER_MS: int = 50  # Max 50ms jitter for retries


def is_retryable_error(exc: Exception) -> bool:
    """Classify if an error is retryable (transient) or permanent.

    Retryable errors:
    - ConnectionTimeout (timeout exceeded)
    - ConnectionError (network issues)
    - TransportError (general transport errors are considered retryable)

    Permanent errors:
    - Other exceptions (ValueError, TypeError, etc.)
    """
    if isinstance(exc, (ConnectionTimeout, ConnectionError, TransportError)):
        return True

    return False


def add_jitter(base_delay_ms: int, max_jitter_ms: int) -> float:
    """Add random jitter to a delay to avoid thundering herd.

    Returns delay in seconds with jitter added.
    """
    jitter_ms = random.randint(0, max_jitter_ms)
    total_ms = base_delay_ms + jitter_ms
    return total_ms / 1000.0


class IndexResult(TypedDict):
    id: str
    index: str


class BulkWriteResult(TypedDict):
    success_count: int
    failed_count: int
    errors: list[dict[str, Any]]


class MemoryWriteBuffer:
    """Buffers memory documents during task execution for bulk write on completion."""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.task_memory_docs: list[dict[str, Any]] = []
        self.shared_memory_docs: list[dict[str, Any]] = []
        self.start_time = datetime.now(timezone.utc)
        self.last_flush_time = self.start_time

    def add_task_memory(self, doc: dict[str, Any]) -> None:
        """Buffer a document for task_memory index."""
        doc_with_metadata = {
            "_index": TASK_MEMORY_INDEX,
            "_op_type": "index",
            "task_id": self.task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **doc,
        }
        self.task_memory_docs.append(doc_with_metadata)
        logger.debug(
            "[Buffer] Added task_memory doc for task_id=%s (total: %d)",
            self.task_id,
            len(self.task_memory_docs),
        )

    def add_shared_memory(self, doc: dict[str, Any]) -> None:
        """Buffer a document for shared_memory index."""
        doc_with_metadata = {
            "_index": SHARED_MEMORY_INDEX,
            "_op_type": "index",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **doc,
        }
        self.shared_memory_docs.append(doc_with_metadata)
        logger.debug(
            "[Buffer] Added shared_memory doc for task_id=%s (total: %d)",
            self.task_id,
            len(self.shared_memory_docs),
        )

    def should_flush_intermediate(self, max_runtime_seconds: int = 300) -> bool:
        """Check if intermediate flush is needed for long-running tasks (> 5 minutes by default)."""
        elapsed = (datetime.now(timezone.utc) - self.start_time).total_seconds()
        time_since_last_flush = (
            datetime.now(timezone.utc) - self.last_flush_time
        ).total_seconds()
        return (
            elapsed > max_runtime_seconds
            and time_since_last_flush > max_runtime_seconds
        )

    def get_buffered_count(self) -> tuple[int, int]:
        """Return (task_memory_count, shared_memory_count)."""
        return len(self.task_memory_docs), len(self.shared_memory_docs)

    def clear(self) -> None:
        """Clear all buffered documents."""
        self.task_memory_docs.clear()
        self.shared_memory_docs.clear()
        self.last_flush_time = datetime.now(timezone.utc)
        logger.debug("[Buffer] Cleared buffer for task_id=%s", self.task_id)


class MemoryStore(TypedDict):
    store: Any  # Callable[[str, dict], IndexResult]
    store_shared: Any  # Callable[[str, str, list[str], str], IndexResult]
    history: Any  # Callable[[str, int], list[dict]]
    search_shared: Any  # Callable[[str, list[str] | None, int], list[dict]]
    unified_search: (
        Any  # Callable[[str, list[str] | None, int, bool, bool], list[dict]]
    )
    delete_task_memory: Any  # Callable[[str], int]
    get_task_result: Any  # Callable[[str], dict | None]
    graduate_to_shared: Any  # Callable[[str, str], bool]
    create_buffer: Any  # Callable[[str], MemoryWriteBuffer]
    bulk_write: Any  # Callable[[MemoryWriteBuffer], BulkWriteResult]


def merge_and_rank_results(
    task_memory_results: list[dict[str, Any]],
    shared_memory_results: list[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Merge results from task_memory and shared_memory, rank by score, return top N.

    Combines results from both indices, sorts by _score (if available), and returns
    the top `limit` results. Adds a `_source_index` field to indicate origin.
    """
    # Add source index to each result
    for result in task_memory_results:
        result["_source_index"] = "task_memory"

    for result in shared_memory_results:
        result["_source_index"] = "shared_memory"

    # Combine all results
    all_results = task_memory_results + shared_memory_results

    # Sort by _score if available (higher is better), otherwise keep order
    all_results.sort(key=lambda x: x.get("_score", 0), reverse=True)

    # Return top N results
    return all_results[:limit]


def should_write_to_shared_memory(
    task_status: str,
    has_code_changes: bool,
    tools_called: list[str],
) -> bool:
    """Gating logic: determine if task results should be written to shared_memory.

    Criteria for shared_memory write:
    - Task succeeded (not failed or incomplete)
    - Task produced meaningful code changes
    - Task used at least one skill tool (code_edit, test_generator, doc_update)

    Returns True if task should write to shared_memory, False otherwise.
    """
    if task_status not in ["Succeeded", "success"]:
        logger.debug(
            "[Gating] Task status '%s' does not qualify for shared_memory", task_status
        )
        return False

    if not has_code_changes:
        logger.debug("[Gating] No code changes, skipping shared_memory write")
        return False

    skill_tools = {"code_edit", "test_generator", "doc_update", "graphql_debug"}
    used_skill_tools = [t for t in tools_called if t in skill_tools]

    if not used_skill_tools:
        logger.debug("[Gating] No skill tools used, skipping shared_memory write")
        return False

    logger.info(
        "[Gating] Task qualifies for shared_memory write (tools: %s)", used_skill_tools
    )
    return True


def create_memory_store(es_client: Elasticsearch) -> MemoryStore:
    """Factory: creates memory store functions bound to an ES client."""

    def _ensure_index(index: str, mapping: dict[str, Any]) -> None:
        try:
            if not es_client.indices.exists(index=index):
                es_client.indices.create(index=index, **mapping)
                logger.info("Created ES index: %s", index)
        except Exception as exc:
            logger.warning("Failed to ensure index %s: %s", index, exc)

    def store_task_result(task_id: str, result: dict[str, Any]) -> IndexResult:
        _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
        doc: dict[str, Any] = {
            "task_id": task_id,
            **result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        response = es_client.index(index=TASK_MEMORY_INDEX, document=doc)
        doc_id: str = response["_id"]
        logger.info(
            "[ES] Stored task_memory doc id=%s task_id=%s phase=%s",
            doc_id,
            task_id,
            result.get("phase", "?"),
        )
        return {"id": doc_id, "index": response["_index"]}

    def store_shared_knowledge(
        summary: str,
        context: str,
        skills: list[str],
        repository: str = "",
    ) -> IndexResult:
        _ensure_index(SHARED_MEMORY_INDEX, SHARED_MEMORY_MAPPING)
        doc: dict[str, Any] = {
            "summary": summary,
            "context": context,
            "skills": skills,
            "repository": repository,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        response = es_client.index(index=SHARED_MEMORY_INDEX, document=doc)
        doc_id: str = response["_id"]
        logger.info(
            "[ES] Stored shared_memory doc id=%s skills=%s repo=%s summary=%.80s",
            doc_id,
            skills,
            repository,
            summary,
        )
        return {"id": doc_id, "index": response["_index"]}

    def get_task_history(agent_ref: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
            response = es_client.search(
                index=TASK_MEMORY_INDEX,
                query={"term": {"agent_ref": agent_ref}},
                size=limit,
                sort=[{"timestamp": "desc"}],
            )
            results: list[dict[str, Any]] = [
                hit["_source"] for hit in response["hits"]["hits"]
            ]
            logger.info(
                "[ES] get_task_history agent_ref=%s returned %d results",
                agent_ref,
                len(results),
            )
            for i, r in enumerate(results):
                logger.debug(
                    "[ES]   history[%d]: task_id=%s phase=%s prompt=%.60s",
                    i,
                    r.get("task_id", "?"),
                    r.get("phase", "?"),
                    r.get("prompt", ""),
                )
            return results
        except Exception as exc:
            logger.warning("[ES] get_task_history failed: %s", exc)
            return []

    def search_shared_knowledge(
        query_text: str,
        skills: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Legacy function - kept for backward compatibility.

        Delegates to unified_search with shared_memory only.
        """
        return unified_search(
            query_text=query_text,
            skills=skills,
            limit=limit,
            search_task_memory=False,
            search_shared_memory=True,
        )

    def unified_search(
        query_text: str,
        skills: list[str] | None = None,
        limit: int = 10,
        search_task_memory: bool = True,
        search_shared_memory: bool = True,
    ) -> list[dict[str, Any]]:
        """Search both task_memory and shared_memory with fallback strategy.

        Fallback levels:
        1. Full query with timeout (multi_match on multiple fields)
        2. Retry with jitter
        3. BM25-only (simple match query)
        4. No retrieval (empty results)

        Returns top `limit` results combined from both indices.
        """
        if not search_task_memory and not search_shared_memory:
            logger.warning("[ES] unified_search called with both indices disabled")
            return []

        # Level 1: Try full query with timeout
        try:
            return _execute_full_query(
                query_text, skills, limit, search_task_memory, search_shared_memory
            )
        except Exception as exc:
            if not is_retryable_error(exc):
                logger.error("[ES] unified_search failed with permanent error: %s", exc)
                return []

            logger.warning(
                "[ES] unified_search failed (retryable): %s, will retry", exc
            )

        # Level 2: Retry with jitter
        try:
            delay = add_jitter(50, ES_RETRY_JITTER_MS)
            logger.info("[ES] Retrying unified_search after %.3fs", delay)
            time.sleep(delay)

            return _execute_full_query(
                query_text, skills, limit, search_task_memory, search_shared_memory
            )
        except Exception as exc:
            logger.warning(
                "[ES] unified_search retry failed: %s, falling back to BM25", exc
            )

        # Level 3: Fallback to BM25-only (simpler query)
        try:
            logger.info("[ES] Falling back to BM25-only query")
            return _execute_bm25_query(
                query_text, limit, search_task_memory, search_shared_memory
            )
        except Exception as exc:
            logger.error("[ES] BM25 fallback failed: %s, returning empty results", exc)

        # Level 4: No retrieval (empty results)
        logger.warning("[ES] All fallback levels exhausted, returning empty results")
        return []

    def _execute_full_query(
        query_text: str,
        skills: list[str] | None,
        limit: int,
        search_task_memory: bool,
        search_shared_memory: bool,
    ) -> list[dict[str, Any]]:
        """Execute full multi_match query on both indices."""
        _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
        _ensure_index(SHARED_MEMORY_INDEX, SHARED_MEMORY_MAPPING)

        task_results: list[dict[str, Any]] = []
        shared_results: list[dict[str, Any]] = []

        # Search task_memory
        if search_task_memory:
            must: list[dict[str, Any]] = [
                {
                    "multi_match": {
                        "query": query_text,
                        "fields": ["prompt", "output", "methodology"],
                    }
                },
            ]
            if skills:
                must.append({"terms": {"skills_used": skills}})

            query_body: dict[str, Any] = {"bool": {"must": must}}
            logger.debug("[ES] task_memory query=%s", query_body)

            response = es_client.search(
                index=TASK_MEMORY_INDEX,
                query=query_body,
                size=limit,
                timeout=f"{ES_QUERY_TIMEOUT_MS}ms",
            )
            task_results = [
                {**hit["_source"], "_score": hit["_score"]}
                for hit in response["hits"]["hits"]
            ]
            logger.info(
                "[ES] task_memory search returned %d results", len(task_results)
            )

        # Search shared_memory
        if search_shared_memory:
            must = [
                {
                    "multi_match": {
                        "query": query_text,
                        "fields": ["summary", "context"],
                    }
                },
            ]
            if skills:
                must.append({"terms": {"skills": skills}})

            query_body = {"bool": {"must": must}}
            logger.debug("[ES] shared_memory query=%s", query_body)

            response = es_client.search(
                index=SHARED_MEMORY_INDEX,
                query=query_body,
                size=limit,
                timeout=f"{ES_QUERY_TIMEOUT_MS}ms",
            )
            shared_results = [
                {**hit["_source"], "_score": hit["_score"]}
                for hit in response["hits"]["hits"]
            ]
            logger.info(
                "[ES] shared_memory search returned %d results", len(shared_results)
            )

        # Merge and rank results
        merged = merge_and_rank_results(task_results, shared_results, limit)
        logger.info(
            "[ES] unified_search returned %d results (task: %d, shared: %d)",
            len(merged),
            len(task_results),
            len(shared_results),
        )
        return merged

    def _execute_bm25_query(
        query_text: str,
        limit: int,
        search_task_memory: bool,
        search_shared_memory: bool,
    ) -> list[dict[str, Any]]:
        """Execute simple BM25 match query (fallback)."""
        _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
        _ensure_index(SHARED_MEMORY_INDEX, SHARED_MEMORY_MAPPING)

        task_results: list[dict[str, Any]] = []
        shared_results: list[dict[str, Any]] = []

        # Search task_memory with simple match
        if search_task_memory:
            query_body: dict[str, Any] = {"match": {"prompt": query_text}}
            logger.debug("[ES] task_memory BM25 query=%s", query_body)

            response = es_client.search(
                index=TASK_MEMORY_INDEX,
                query=query_body,
                size=limit,
                timeout=f"{ES_QUERY_TIMEOUT_MS}ms",
            )
            task_results = [
                {**hit["_source"], "_score": hit["_score"]}
                for hit in response["hits"]["hits"]
            ]
            logger.info(
                "[ES] task_memory BM25 search returned %d results", len(task_results)
            )

        # Search shared_memory with simple match
        if search_shared_memory:
            query_body = {"match": {"summary": query_text}}
            logger.debug("[ES] shared_memory BM25 query=%s", query_body)

            response = es_client.search(
                index=SHARED_MEMORY_INDEX,
                query=query_body,
                size=limit,
                timeout=f"{ES_QUERY_TIMEOUT_MS}ms",
            )
            shared_results = [
                {**hit["_source"], "_score": hit["_score"]}
                for hit in response["hits"]["hits"]
            ]
            logger.info(
                "[ES] shared_memory BM25 search returned %d results",
                len(shared_results),
            )

        # Merge and rank results
        merged = merge_and_rank_results(task_results, shared_results, limit)
        logger.info(
            "[ES] BM25 fallback returned %d results (task: %d, shared: %d)",
            len(merged),
            len(task_results),
            len(shared_results),
        )
        return merged

    def delete_task_memory(task_id: str) -> int:
        try:
            _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
            response = es_client.delete_by_query(
                index=TASK_MEMORY_INDEX,
                query={"term": {"task_id": task_id}},
            )
            deleted: int = response.get("deleted", 0)
            logger.info(
                "[ES] Deleted %d task_memory docs for task_id=%s", deleted, task_id
            )
            return deleted
        except Exception as exc:
            logger.warning("[ES] delete_task_memory failed for %s: %s", task_id, exc)
            return 0

    def get_task_result(task_id: str) -> dict[str, Any] | None:
        try:
            _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
            response = es_client.search(
                index=TASK_MEMORY_INDEX,
                query={
                    "bool": {
                        "must": [
                            {"term": {"task_id": task_id}},
                            {"term": {"phase": "Succeeded"}},
                        ]
                    }
                },
                size=1,
                sort=[{"timestamp": "desc"}],
            )
            hits: list[dict[str, Any]] = response["hits"]["hits"]
            return hits[0]["_source"] if hits else None
        except Exception as exc:
            logger.warning("[ES] get_task_result failed for %s: %s", task_id, exc)
            return None

    def graduate_to_shared(task_id: str, pr_state: str) -> bool:
        task_result: dict[str, Any] | None = get_task_result(task_id)
        if not task_result:
            logger.info("[ES] No succeeded task result to graduate for %s", task_id)
            delete_task_memory(task_id)
            return False

        methodology: str = task_result.get("methodology", "")
        output: str = task_result.get("output", "")
        prompt: str = task_result.get("prompt", "")
        repo: str = task_result.get("repository", "")
        skills: list[str] = task_result.get("skills_used", [])
        tools: list[str] = task_result.get("tools_called", [])
        files: list[str] = task_result.get("files_modified", [])

        outcome: str = "merged" if pr_state == "merged" else "closed without merge"
        summary: str = (
            f"Task '{prompt[:100]}' on {repo} — PR {outcome}. "
            f"Modified {len(files)} files using {len(tools)} tools."
        )
        context: str = (
            f"Outcome: PR {outcome}\n"
            f"Methodology: {methodology}\n"
            f"Tools used: {', '.join(tools) if tools else 'none'}\n"
            f"Files modified: {', '.join(files[:20]) if files else 'none'}\n"
            f"Agent output: {output[:800]}"
        )

        store_shared_knowledge(
            summary=summary, context=context, skills=skills, repository=repo
        )
        logger.info("[ES] Graduated task %s to shared_memory (PR %s)", task_id, outcome)

        deleted: int = delete_task_memory(task_id)
        logger.info("[ES] Cleaned up %d task_memory docs after graduation", deleted)
        return True

    def create_buffer(task_id: str) -> MemoryWriteBuffer:
        """Create a new memory write buffer for a task."""
        logger.info("[Buffer] Created buffer for task_id=%s", task_id)
        return MemoryWriteBuffer(task_id)

    def bulk_write(buffer: MemoryWriteBuffer) -> BulkWriteResult:
        """Flush buffered documents to Elasticsearch using bulk API.

        Writes all buffered documents in a single bulk operation.
        Returns success/failure counts and error details.
        """
        _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
        _ensure_index(SHARED_MEMORY_INDEX, SHARED_MEMORY_MAPPING)

        all_docs = buffer.task_memory_docs + buffer.shared_memory_docs

        if not all_docs:
            logger.info("[Bulk] No documents to write for task_id=%s", buffer.task_id)
            return {"success_count": 0, "failed_count": 0, "errors": []}

        task_count, shared_count = buffer.get_buffered_count()
        logger.info(
            "[Bulk] Flushing %d documents for task_id=%s (task_memory: %d, shared_memory: %d)",
            len(all_docs),
            buffer.task_id,
            task_count,
            shared_count,
        )

        try:
            # Use helpers.bulk() with raise_on_error=False to collect all failures
            # Returns (success_count, failed_items) where failed_items is a list
            result = helpers.bulk(
                es_client,
                all_docs,
                raise_on_error=False,
                stats_only=False,
                chunk_size=500,
                max_chunk_bytes=10485760,  # 10MB
            )

            # Unpack result - can be (int, list) or just int if stats_only=True
            if isinstance(result, tuple):
                success_count, failed_items = result
            else:
                success_count = result
                failed_items = []

            failed_count = len(failed_items) if failed_items else 0
            errors: list[dict[str, Any]] = []

            if failed_items:
                logger.warning(
                    "[Bulk] %d documents failed to index for task_id=%s",
                    failed_count,
                    buffer.task_id,
                )
                for item in failed_items:
                    error_info = {
                        "index": item.get("index", {}).get("_index", "unknown"),
                        "error": item.get("index", {}).get("error", {}),
                        "status": item.get("index", {}).get("status", 0),
                    }
                    errors.append(error_info)
                    logger.error("[Bulk] Failed item: %s", error_info)

            logger.info(
                "[Bulk] Completed for task_id=%s: success=%d, failed=%d",
                buffer.task_id,
                success_count,
                failed_count,
            )

            return {
                "success_count": success_count,
                "failed_count": failed_count,
                "errors": errors,
            }

        except Exception as exc:
            logger.exception(
                "[Bulk] Bulk write failed for task_id=%s: %s", buffer.task_id, exc
            )
            return {
                "success_count": 0,
                "failed_count": len(all_docs),
                "errors": [{"error": str(exc), "type": "bulk_exception"}],
            }

    return {
        "store": store_task_result,
        "store_shared": store_shared_knowledge,
        "history": get_task_history,
        "search_shared": search_shared_knowledge,
        "unified_search": unified_search,
        "delete_task_memory": delete_task_memory,
        "get_task_result": get_task_result,
        "graduate_to_shared": graduate_to_shared,
        "create_buffer": create_buffer,
        "bulk_write": bulk_write,
    }
