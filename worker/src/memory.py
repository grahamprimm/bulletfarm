"""Elasticsearch memory store for agent task results and shared knowledge.

Uses dependency injection — the ES client is passed in, not imported globally.
Two indices:
  - task_memory: per-task results and context
  - shared_memory: reusable knowledge across tasks
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, TypedDict

from elasticsearch import Elasticsearch

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


class IndexResult(TypedDict):
    id: str
    index: str


class MemoryStore(TypedDict):
    store: Any  # Callable[[str, dict], IndexResult]
    store_shared: Any  # Callable[[str, str, list[str], str], IndexResult]
    history: Any  # Callable[[str, int], list[dict]]
    search_shared: Any  # Callable[[str, list[str] | None, int], list[dict]]
    delete_task_memory: Any  # Callable[[str], int]
    get_task_result: Any  # Callable[[str], dict | None]
    graduate_to_shared: Any  # Callable[[str, str], bool]


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
            doc_id, task_id, result.get("phase", "?"),
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
            doc_id, skills, repository, summary,
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
            results: list[dict[str, Any]] = [hit["_source"] for hit in response["hits"]["hits"]]
            logger.info("[ES] get_task_history agent_ref=%s returned %d results", agent_ref, len(results))
            for i, r in enumerate(results):
                logger.debug(
                    "[ES]   history[%d]: task_id=%s phase=%s prompt=%.60s",
                    i, r.get("task_id", "?"), r.get("phase", "?"), r.get("prompt", ""),
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
        try:
            _ensure_index(SHARED_MEMORY_INDEX, SHARED_MEMORY_MAPPING)
            must: list[dict[str, Any]] = [
                {"multi_match": {"query": query_text, "fields": ["summary", "context"]}},
            ]
            if skills:
                must.append({"terms": {"skills": skills}})

            query_body: dict[str, Any] = {"bool": {"must": must}}
            logger.debug("[ES] search_shared_knowledge query=%s", query_body)

            response = es_client.search(
                index=SHARED_MEMORY_INDEX,
                query=query_body,
                size=limit,
                sort=[{"_score": "desc"}],
            )
            results: list[dict[str, Any]] = [hit["_source"] for hit in response["hits"]["hits"]]
            logger.info(
                "[ES] search_shared_knowledge query=%.60s skills=%s returned %d results",
                query_text, skills, len(results),
            )
            return results
        except Exception as exc:
            logger.warning("[ES] search_shared_knowledge failed: %s", exc)
            return []

    def delete_task_memory(task_id: str) -> int:
        try:
            _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
            response = es_client.delete_by_query(
                index=TASK_MEMORY_INDEX,
                query={"term": {"task_id": task_id}},
            )
            deleted: int = response.get("deleted", 0)
            logger.info("[ES] Deleted %d task_memory docs for task_id=%s", deleted, task_id)
            return deleted
        except Exception as exc:
            logger.warning("[ES] delete_task_memory failed for %s: %s", task_id, exc)
            return 0

    def get_task_result(task_id: str) -> dict[str, Any] | None:
        try:
            _ensure_index(TASK_MEMORY_INDEX, TASK_MEMORY_MAPPING)
            response = es_client.search(
                index=TASK_MEMORY_INDEX,
                query={"bool": {"must": [
                    {"term": {"task_id": task_id}},
                    {"term": {"phase": "Succeeded"}},
                ]}},
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

        store_shared_knowledge(summary=summary, context=context, skills=skills, repository=repo)
        logger.info("[ES] Graduated task %s to shared_memory (PR %s)", task_id, outcome)

        deleted: int = delete_task_memory(task_id)
        logger.info("[ES] Cleaned up %d task_memory docs after graduation", deleted)
        return True

    return {
        "store": store_task_result,
        "store_shared": store_shared_knowledge,
        "history": get_task_history,
        "search_shared": search_shared_knowledge,
        "delete_task_memory": delete_task_memory,
        "get_task_result": get_task_result,
        "graduate_to_shared": graduate_to_shared,
    }
