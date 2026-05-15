#!/usr/bin/env python3
"""Regression test for AlmaRunner tool-only response bug fix."""

import json
import queue
import threading
import time
from unittest import mock

import pytest

from feishu_bridge.runtime_alma import AlmaRunner, AlmaWSManager


@pytest.fixture
def runner(tmp_path):
    """AlmaRunner with mocked WS + HTTP to avoid real Alma dependency."""
    r = AlmaRunner(
        model="sonnet",
        workspace=str(tmp_path),
        timeout=10,
        bot_id="test-bot",
        extra_system_prompts=[],
        safety_prompt_mode="off",
    )
    return r


def test_tool_only_response_not_empty(runner):
    """Test that tool-only responses (no text_append) don't show '(空回复)'."""
    
    # Mock WebSocket manager
    mock_mgr = mock.MagicMock(spec=AlmaWSManager)
    mock_queue = queue.Queue()
    mock_mgr.register_run.return_value = mock_queue
    
    # Simulate tool-only response: part_add for tool, then generation_completed
    events = [
        {
            "type": "message_delta",
            "data": {
                "delta": {
                    "type": "part_add",
                    "part": {
                        "type": "tool-invocation",
                        "toolCallId": "tool_call_1",
                        "toolName": "bash",
                        "args": {"command": "echo test"}
                    }
                }
            }
        },
        {
            "type": "message_delta", 
            "data": {
                "delta": {
                    "type": "tool_output_set",
                    "toolCallId": "tool_call_1"
                }
            }
        },
        {
            "type": "generation_completed",
            "data": {}
        }
    ]
    
    # Queue events
    for event in events:
        mock_queue.put(event)
    
    with mock.patch.object(runner, "_resolve_thread", return_value="thread_123"), \
         mock.patch.object(runner, "_get_ws_mgr", return_value=mock_mgr), \
         mock.patch("feishu_bridge.runtime_alma._is_alma_running", return_value=True):
        
        result = runner.run("test prompt", session_id="test_session")
        
        # Should not be empty - should show tool execution message
        assert result["is_error"] is False
        assert result["result"] != ""
        assert "已执行" in result["result"]
        assert "Bash" in result["result"]


def test_multiple_tools_response(runner):
    """Test tool-only response with multiple tools."""
    
    mock_mgr = mock.MagicMock(spec=AlmaWSManager)
    mock_queue = queue.Queue()
    mock_mgr.register_run.return_value = mock_queue
    
    # Multiple tool calls
    events = [
        {
            "type": "message_delta",
            "data": {
                "delta": {
                    "type": "part_add", 
                    "part": {
                        "type": "tool-invocation",
                        "toolCallId": "tool_call_1",
                        "toolName": "read",
                        "args": {"file_path": "test.txt"}
                    }
                }
            }
        },
        {
            "type": "message_delta",
            "data": {
                "delta": {
                    "type": "part_add",
                    "part": {
                        "type": "tool-invocation", 
                        "toolCallId": "tool_call_2",
                        "toolName": "write",
                        "args": {"file_path": "output.txt", "content": "test"}
                    }
                }
            }
        },
        {
            "type": "generation_completed",
            "data": {}
        }
    ]
    
    for event in events:
        mock_queue.put(event)
    
    with mock.patch.object(runner, "_resolve_thread", return_value="thread_123"), \
         mock.patch.object(runner, "_get_ws_mgr", return_value=mock_mgr), \
         mock.patch("feishu_bridge.runtime_alma._is_alma_running", return_value=True):
        
        result = runner.run("test prompt", session_id="test_session")
        
        assert result["is_error"] is False
        assert result["result"] != ""
        assert "已执行 2 个工具" in result["result"]
        assert "Read, Write" in result["result"]


def test_normal_text_response_unchanged(runner):
    """Test that normal text responses are not affected by the fix."""
    
    mock_mgr = mock.MagicMock(spec=AlmaWSManager)
    mock_queue = queue.Queue()
    mock_mgr.register_run.return_value = mock_queue
    
    # Normal response with text
    events = [
        {
            "type": "message_delta",
            "data": {
                "delta": {
                    "type": "text_append",
                    "text": "Hello, this is a normal response."
                }
            }
        },
        {
            "type": "generation_completed", 
            "data": {}
        }
    ]
    
    for event in events:
        mock_queue.put(event)
    
    with mock.patch.object(runner, "_resolve_thread", return_value="thread_123"), \
         mock.patch.object(runner, "_get_ws_mgr", return_value=mock_mgr), \
         mock.patch("feishu_bridge.runtime_alma._is_alma_running", return_value=True):
        
        result = runner.run("test prompt", session_id="test_session")
        
        assert result["is_error"] is False
        assert result["result"] == "Hello, this is a normal response."


def test_text_and_tools_response(runner):
    """Test response with both text and tools - should use original text."""
    
    mock_mgr = mock.MagicMock(spec=AlmaWSManager)
    mock_queue = queue.Queue()
    mock_mgr.register_run.return_value = mock_queue
    
    # Response with both text and tools
    events = [
        {
            "type": "message_delta",
            "data": {
                "delta": {
                    "type": "text_append",
                    "text": "I'll run a command for you."
                }
            }
        },
        {
            "type": "message_delta",
            "data": {
                "delta": {
                    "type": "part_add",
                    "part": {
                        "type": "tool-invocation",
                        "toolCallId": "tool_call_1", 
                        "toolName": "bash",
                        "args": {"command": "ls"}
                    }
                }
            }
        },
        {
            "type": "generation_completed",
            "data": {}
        }
    ]
    
    for event in events:
        mock_queue.put(event)
    
    with mock.patch.object(runner, "_resolve_thread", return_value="thread_123"), \
         mock.patch.object(runner, "_get_ws_mgr", return_value=mock_mgr), \
         mock.patch("feishu_bridge.runtime_alma._is_alma_running", return_value=True):
        
        result = runner.run("test prompt", session_id="test_session")
        
        assert result["is_error"] is False
        assert result["result"] == "I'll run a command for you."
        # Should use original text, not tool summary