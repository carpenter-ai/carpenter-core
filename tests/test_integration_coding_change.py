"""Integration tests for the coding-change arc flow.

Tests the full pipeline: chat tool_use → arc creation → workspace →
coding agent → diff review → approval/rejection.
"""

import asyncio
import json
import os

import pytest
from unittest.mock import patch, MagicMock

from carpenter.agent import invocation, conversation
from carpenter.core.arcs import manager as arc_manager
from carpenter.core.workflows import coding_change_handler
from carpenter.core import workspace_manager
from carpenter.core.engine import work_queue, main_loop
from carpenter.api.review import get_review, clear_reviews, create_diff_review
from carpenter.db import get_db


@pytest.fixture(autouse=True)
def register_handlers():
    """Register coding-change handlers for each test."""
    coding_change_handler.register_handlers(main_loop.register_handler)
    yield
    # Clear handler registry after test
    main_loop._handlers.clear()


@pytest.fixture(autouse=True)
def clean_reviews():
    """Clear review links between tests."""
    clear_reviews()


@pytest.fixture
def source_project(tmp_path):
    """Create a sample source project with files to modify."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def hello():\n    print('hello')\n")
    (project / "utils.py").write_text("def add(a, b):\n    return a + b\n")
    return str(project)


class TestChatToolUse:
    """Tests that the chat agent can use tools via tool_use."""

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_read_file(self, mock_client, source_project):
        """Chat agent uses read_file tool and gets result."""
        file_path = os.path.join(source_project, "main.py")

        # First response: tool_use to read a file
        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "read_file",
                 "input": {"path": file_path}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
        # Second response: text after reading
        text_response = {
            "content": [{"type": "text", "text": "The file contains a hello function."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 80, "output_tokens": 20},
        }

        mock_client.call.side_effect = [tool_response, text_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("Read main.py", api_key="test-key")

        assert "hello function" in result["response_text"]
        assert mock_client.call.call_count == 2

        # Verify tools were passed in the call
        call_args = mock_client.call.call_args_list[0]
        assert call_args.kwargs.get("tools") is not None

    @patch("carpenter.agent.invocation.claude_client")
    def test_chat_list_files(self, mock_client, source_project):
        """Chat agent uses list_files tool."""
        tool_response = {
            "content": [
                {"type": "tool_use", "id": "tool_1", "name": "list_files",
                 "input": {"dir": source_project}},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 50, "output_tokens": 30},
        }
        text_response = {
            "content": [{"type": "text", "text": "Found main.py and utils.py."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 80, "output_tokens": 20},
        }

        mock_client.call.side_effect = [tool_response, text_response]
        mock_client.extract_code_from_text.return_value = None

        result = invocation.invoke_for_chat("List project files", api_key="test-key")

        # Verify the tool call happened and second API call received file list
        assert mock_client.call.call_count == 2
        second_call_msgs = mock_client.call.call_args_list[1][0][1]
        # The tool result should contain file names
        tool_result_msg = second_call_msgs[-1]
        assert tool_result_msg["role"] == "user"

class TestCodingChangeArcFlow:
    """Tests the full coding-change arc lifecycle."""

    @pytest.mark.asyncio
    async def test_full_flow_approve(self, test_db, source_project):
        """Full arc flow: invoke-agent → review → approve → changes applied."""
        # Step 1: Create arc and enqueue work
        arc_id = arc_manager.create_arc(
            name="coding-change",
            goal=f"Add feature to {source_project}",
        )

        # Mock the coding agent to make a real file change
        def mock_coding_agent(workspace, prompt, agent_name=None):
            # Simulate agent editing a file in the workspace
            utils_path = os.path.join(workspace, "utils.py")
            with open(utils_path) as f:
                content = f.read()
            content += "\ndef word_count(s):\n    return len(s.split())\n"
            with open(utils_path, "w") as f:
                f.write(content)
            return {"stdout": "Added word_count function.", "exit_code": 0, "iterations": 2}

        # Step 2: Run invoke-agent handler
        with patch(
            "carpenter.agent.coding_dispatch.invoke_coding_agent",
            side_effect=mock_coding_agent,
        ):
            await coding_change_handler.handle_invoke_agent(
                1,
                {
                    "arc_id": arc_id,
                    "source_dir": source_project,
                    "prompt": "Add a word_count function",
                },
            )

        # Verify workspace was created
        ws = coding_change_handler._get_arc_state(arc_id, "workspace_path")
        assert ws is not None
        assert os.path.isdir(ws)

        # Step 3: Run generate-review handler
        await coding_change_handler.handle_generate_review(
            2, {"arc_id": arc_id},
        )

        # Verify review was created
        review_url = coding_change_handler._get_arc_state(arc_id, "review_url")
        assert review_url is not None
        review_id = coding_change_handler._get_arc_state(arc_id, "review_id")
        assert review_id is not None

        # Verify diff contains our change
        diff = coding_change_handler._get_arc_state(arc_id, "diff")
        assert "word_count" in diff

        # Verify arc is waiting for approval
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "waiting"

        # Step 4: Approve
        await coding_change_handler.handle_approval(
            3,
            {"arc_id": arc_id, "decision": "approve", "feedback": ""},
        )

        # Verify changes applied to source
        with open(os.path.join(source_project, "utils.py")) as f:
            content = f.read()
        assert "word_count" in content

        # Verify arc completed
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "completed"

        # Verify workspace cleaned up
        assert not os.path.isdir(ws)

    @pytest.mark.asyncio
    async def test_full_flow_reject(self, test_db, source_project):
        """Full arc flow: invoke-agent → review → reject → changes discarded."""
        arc_id = arc_manager.create_arc(
            name="coding-change",
            goal=f"Bad change to {source_project}",
        )

        def mock_coding_agent(workspace, prompt, agent_name=None):
            utils_path = os.path.join(workspace, "utils.py")
            with open(utils_path, "w") as f:
                f.write("# everything deleted\n")
            return {"stdout": "Deleted everything.", "exit_code": 0, "iterations": 1}

        with patch(
            "carpenter.agent.coding_dispatch.invoke_coding_agent",
            side_effect=mock_coding_agent,
        ):
            await coding_change_handler.handle_invoke_agent(
                1,
                {
                    "arc_id": arc_id,
                    "source_dir": source_project,
                    "prompt": "Delete everything",
                },
            )

        await coding_change_handler.handle_generate_review(
            2, {"arc_id": arc_id},
        )

        ws = coding_change_handler._get_arc_state(arc_id, "workspace_path")

        # Reject
        await coding_change_handler.handle_approval(
            3,
            {"arc_id": arc_id, "decision": "reject", "feedback": "bad idea"},
        )

        # Source should be UNCHANGED
        with open(os.path.join(source_project, "utils.py")) as f:
            content = f.read()
        assert "def add(a, b)" in content  # Original still there

        # Arc cancelled, workspace cleaned
        arc = arc_manager.get_arc(arc_id)
        assert arc["status"] == "cancelled"
        assert not os.path.isdir(ws)

    @pytest.mark.asyncio
    async def test_full_flow_revise(self, test_db, source_project):
        """Revise decision re-enqueues agent with feedback."""
        arc_id = arc_manager.create_arc(
            name="coding-change",
            goal=f"Revise change to {source_project}",
        )

        def mock_coding_agent(workspace, prompt, agent_name=None):
            with open(os.path.join(workspace, "utils.py"), "a") as f:
                f.write("\ndef stub(): pass\n")
            return {"stdout": "Added stub.", "exit_code": 0, "iterations": 1}

        with patch(
            "carpenter.agent.coding_dispatch.invoke_coding_agent",
            side_effect=mock_coding_agent,
        ):
            await coding_change_handler.handle_invoke_agent(
                1,
                {
                    "arc_id": arc_id,
                    "source_dir": source_project,
                    "prompt": "Add a stub function",
                },
            )

        # Drain any pending generate-review items from earlier
        while True:
            pending = work_queue.claim()
            if pending is None:
                break
            work_queue.complete(pending["id"])

        await coding_change_handler.handle_generate_review(
            2, {"arc_id": arc_id},
        )

        # Store original prompt for revision
        coding_change_handler._set_arc_state(arc_id, "original_prompt", "Add a stub function")

        # Revise
        await coding_change_handler.handle_approval(
            3,
            {"arc_id": arc_id, "decision": "revise", "feedback": "rename to helper()"},
        )

        # Should have enqueued a new invoke-agent work item
        item = work_queue.claim()
        assert item is not None
        assert item["event_type"] == "coding-change.invoke-agent"
        payload = json.loads(item["payload_json"])
        assert "rename to helper()" in payload["prompt"]


class TestMainLoopProcessing:
    """Tests that the main loop processes coding-change work items."""

    @pytest.mark.asyncio
    async def test_work_item_processed_by_loop(self, test_db, source_project):
        """Main loop picks up and processes a coding-change work item."""
        arc_id = arc_manager.create_arc(
            name="coding-change",
            goal=f"Loop test for {source_project}",
        )

        def mock_coding_agent(workspace, prompt, agent_name=None):
            with open(os.path.join(workspace, "main.py"), "a") as f:
                f.write("\ndef loop_test(): pass\n")
            return {"stdout": "Done.", "exit_code": 0, "iterations": 1}

        # Enqueue work item
        work_queue.enqueue(
            "coding-change.invoke-agent",
            {
                "arc_id": arc_id,
                "source_dir": source_project,
                "prompt": "Add loop_test function",
            },
        )

        # Run one iteration of the main loop
        with patch(
            "carpenter.agent.coding_dispatch.invoke_coding_agent",
            side_effect=mock_coding_agent,
        ):
            await main_loop._dispatch_work_items()
            if main_loop._in_flight:
                await asyncio.gather(*main_loop._in_flight.values(), return_exceptions=True)

        # Verify the invoke-agent handler ran
        ws = coding_change_handler._get_arc_state(arc_id, "workspace_path")
        assert ws is not None

        # Run another iteration to process generate-review
        await main_loop._dispatch_work_items()
        if main_loop._in_flight:
            await asyncio.gather(*main_loop._in_flight.values(), return_exceptions=True)

        # Should have a review link now
        review_url = coding_change_handler._get_arc_state(arc_id, "review_url")
        assert review_url is not None
