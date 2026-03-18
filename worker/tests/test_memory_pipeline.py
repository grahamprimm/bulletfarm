"""Tests for memory write pipeline (task completion only).

Verifies:
- No writes during task execution
- Bulk API used for all writes
- Memory entries correctly linked to task_id
- Gating logic for shared_memory writes
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, call

import pytest

from src.memory import (
    MemoryWriteBuffer,
    create_memory_store,
    should_write_to_shared_memory,
)


class TestMemoryWriteBuffer:
    """Test the MemoryWriteBuffer class."""

    def test_buffer_creation(self) -> None:
        """Test buffer is created with correct task_id."""
        buffer = MemoryWriteBuffer("task-123")
        assert buffer.task_id == "task-123"
        assert len(buffer.task_memory_docs) == 0
        assert len(buffer.shared_memory_docs) == 0

    def test_add_task_memory(self) -> None:
        """Test adding documents to task_memory buffer."""
        buffer = MemoryWriteBuffer("task-123")
        doc = {"agent_ref": "agent-1", "prompt": "test prompt"}

        buffer.add_task_memory(doc)

        assert len(buffer.task_memory_docs) == 1
        assert buffer.task_memory_docs[0]["task_id"] == "task-123"
        assert buffer.task_memory_docs[0]["agent_ref"] == "agent-1"
        assert "_index" in buffer.task_memory_docs[0]
        assert buffer.task_memory_docs[0]["_index"] == "task_memory"

    def test_add_shared_memory(self) -> None:
        """Test adding documents to shared_memory buffer."""
        buffer = MemoryWriteBuffer("task-123")
        doc = {"summary": "test summary", "context": "test context"}

        buffer.add_shared_memory(doc)

        assert len(buffer.shared_memory_docs) == 1
        assert buffer.shared_memory_docs[0]["summary"] == "test summary"
        assert "_index" in buffer.shared_memory_docs[0]
        assert buffer.shared_memory_docs[0]["_index"] == "shared_memory"

    def test_get_buffered_count(self) -> None:
        """Test getting buffered document counts."""
        buffer = MemoryWriteBuffer("task-123")
        buffer.add_task_memory({"test": "doc1"})
        buffer.add_task_memory({"test": "doc2"})
        buffer.add_shared_memory({"test": "doc3"})

        task_count, shared_count = buffer.get_buffered_count()

        assert task_count == 2
        assert shared_count == 1

    def test_clear_buffer(self) -> None:
        """Test clearing buffered documents."""
        buffer = MemoryWriteBuffer("task-123")
        buffer.add_task_memory({"test": "doc1"})
        buffer.add_shared_memory({"test": "doc2"})

        buffer.clear()

        assert len(buffer.task_memory_docs) == 0
        assert len(buffer.shared_memory_docs) == 0

    def test_should_flush_intermediate_before_threshold(self) -> None:
        """Test intermediate flush not triggered before 5 minutes."""
        buffer = MemoryWriteBuffer("task-123")
        # Just created, should not flush
        assert buffer.should_flush_intermediate(max_runtime_seconds=300) is False

    def test_should_flush_intermediate_after_threshold(self) -> None:
        """Test intermediate flush triggered after 5 minutes."""
        from datetime import timedelta

        buffer = MemoryWriteBuffer("task-123")
        # Simulate task started 6 minutes ago
        buffer.start_time = datetime.now(timezone.utc) - timedelta(minutes=6)
        buffer.last_flush_time = buffer.start_time

        assert buffer.should_flush_intermediate(max_runtime_seconds=300) is True


class TestGatingLogic:
    """Test the gating logic for shared_memory writes."""

    def test_should_write_to_shared_memory_success_with_changes(self) -> None:
        """Test shared_memory write for successful task with code changes."""
        result = should_write_to_shared_memory(
            task_status="Succeeded",
            has_code_changes=True,
            tools_called=["code_edit", "read_file"],
        )
        assert result is True

    def test_should_not_write_to_shared_memory_on_failure(self) -> None:
        """Test no shared_memory write for failed tasks."""
        result = should_write_to_shared_memory(
            task_status="Failed",
            has_code_changes=True,
            tools_called=["code_edit"],
        )
        assert result is False

    def test_should_not_write_to_shared_memory_without_changes(self) -> None:
        """Test no shared_memory write without code changes."""
        result = should_write_to_shared_memory(
            task_status="Succeeded",
            has_code_changes=False,
            tools_called=["code_edit"],
        )
        assert result is False

    def test_should_not_write_to_shared_memory_without_skill_tools(self) -> None:
        """Test no shared_memory write without skill tools."""
        result = should_write_to_shared_memory(
            task_status="Succeeded",
            has_code_changes=True,
            tools_called=["read_file", "list_files"],  # No skill tools
        )
        assert result is False

    def test_should_write_to_shared_memory_with_test_generator(self) -> None:
        """Test shared_memory write with test_generator tool."""
        result = should_write_to_shared_memory(
            task_status="Succeeded",
            has_code_changes=True,
            tools_called=["test_generator", "read_file"],
        )
        assert result is True


class TestBulkWrite:
    """Test bulk write functionality."""

    def test_bulk_write_empty_buffer(self) -> None:
        """Test bulk write with empty buffer."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        memory_store = create_memory_store(mock_es)
        buffer = memory_store["create_buffer"]("task-123")

        result = memory_store["bulk_write"](buffer)

        assert result["success_count"] == 0
        assert result["failed_count"] == 0
        assert len(result["errors"]) == 0

    def test_bulk_write_with_documents(self) -> None:
        """Test bulk write with buffered documents."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        # Mock helpers.bulk to return success
        from unittest.mock import patch

        with patch("src.memory.helpers.bulk") as mock_bulk:
            mock_bulk.return_value = (2, [])  # 2 successful, 0 failed

            memory_store = create_memory_store(mock_es)
            buffer = memory_store["create_buffer"]("task-123")

            buffer.add_task_memory({"test": "doc1"})
            buffer.add_shared_memory({"test": "doc2"})

            result = memory_store["bulk_write"](buffer)

            assert result["success_count"] == 2
            assert result["failed_count"] == 0
            assert mock_bulk.called

    def test_bulk_write_with_failures(self) -> None:
        """Test bulk write with some failures."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        from unittest.mock import patch

        failed_item = {
            "index": {
                "_index": "task_memory",
                "status": 400,
                "error": {"type": "mapper_parsing_exception", "reason": "failed"},
            }
        }

        with patch("src.memory.helpers.bulk") as mock_bulk:
            mock_bulk.return_value = (1, [failed_item])  # 1 success, 1 failed

            memory_store = create_memory_store(mock_es)
            buffer = memory_store["create_buffer"]("task-123")

            buffer.add_task_memory({"test": "doc1"})
            buffer.add_task_memory({"test": "doc2"})

            result = memory_store["bulk_write"](buffer)

            assert result["success_count"] == 1
            assert result["failed_count"] == 1
            assert len(result["errors"]) == 1
            assert result["errors"][0]["status"] == 400

    def test_bulk_write_exception_handling(self) -> None:
        """Test bulk write handles exceptions gracefully."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        from unittest.mock import patch

        with patch("src.memory.helpers.bulk") as mock_bulk:
            mock_bulk.side_effect = Exception("Connection error")

            memory_store = create_memory_store(mock_es)
            buffer = memory_store["create_buffer"]("task-123")

            buffer.add_task_memory({"test": "doc1"})

            result = memory_store["bulk_write"](buffer)

            assert result["success_count"] == 0
            assert result["failed_count"] == 1
            assert len(result["errors"]) == 1
            assert "Connection error" in result["errors"][0]["error"]


class TestMemoryStoreIntegration:
    """Integration tests for memory store with buffer."""

    def test_create_buffer(self) -> None:
        """Test creating a buffer from memory store."""
        mock_es = MagicMock()
        memory_store = create_memory_store(mock_es)

        buffer = memory_store["create_buffer"]("task-456")

        assert isinstance(buffer, MemoryWriteBuffer)
        assert buffer.task_id == "task-456"

    def test_no_immediate_writes_during_buffering(self) -> None:
        """Test that no ES writes occur during buffering phase."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        memory_store = create_memory_store(mock_es)
        buffer = memory_store["create_buffer"]("task-789")

        # Add documents to buffer
        buffer.add_task_memory({"test": "doc1"})
        buffer.add_shared_memory({"test": "doc2"})

        # Verify no index operations called yet
        mock_es.index.assert_not_called()

    def test_bulk_write_called_on_flush(self) -> None:
        """Test bulk write is called when flushing buffer."""
        mock_es = MagicMock()
        mock_es.indices.exists.return_value = True

        from unittest.mock import patch

        with patch("src.memory.helpers.bulk") as mock_bulk:
            mock_bulk.return_value = (2, [])

            memory_store = create_memory_store(mock_es)
            buffer = memory_store["create_buffer"]("task-999")

            buffer.add_task_memory({"test": "doc1"})
            buffer.add_shared_memory({"test": "doc2"})

            # Flush buffer
            memory_store["bulk_write"](buffer)

            # Verify helpers.bulk was called
            assert mock_bulk.called
            call_args = mock_bulk.call_args
            docs = call_args[0][1]  # Second argument is the documents list
            assert len(list(docs)) == 2
