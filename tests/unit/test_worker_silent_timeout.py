#!/usr/bin/env python3
"""Worker silent-timeout progress-aware auto-continue (fix #4).

When a silent timeout reports tool_was_active=True (a tool ran past the
active-tool window and was killed), the worker must NOT blindly re-send "继续"
— it surfaces to the user. When tool_was_active=False (model-level hang /
finished-without-text), it keeps the one-shot auto-continue nudge.
"""

from feishu_bridge import worker as bridge_worker
from feishu_bridge.runtime_pi import PiRunner


class _FakeHandle:
    def __init__(self, client, chat_id, thread_id, message_id, bot_id=None):
        self.client = client
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.source_message_id = message_id
        self.bot_id = bot_id
        self.deliveries = []
        self.streamed = []
        self._terminated = False
        self._card_fallback_timer = None
        self._typing_reaction_id = None

    def send_processing_indicator(self):
        return True

    def stream_update(self, content):
        self.streamed.append(content)

    def deliver(self, content, is_error=False, total_tokens=0, **kwargs):
        self.deliveries.append((content, is_error, total_tokens))
        return content


class _DummySessionMap:
    def __init__(self):
        self.saved = []

    def get(self, key):
        return None

    def put(self, key, session_id):
        self.saved.append((key, session_id))

    def delete(self, key):
        pass


class _SilentRunner(PiRunner):
    """Real runner interface (supports_auto_compact, footer helpers, …) with a
    scripted run() that returns queued results and records the prompts seen."""

    def __init__(self, results):
        super().__init__(command="pi", model="m", workspace="/tmp",
                         timeout=30, safety_prompt_mode="off")
        self._results = list(results)
        self.calls = []

    def run(self, prompt, *args, **kwargs):
        self.calls.append(prompt)
        if self._results:
            return self._results.pop(0)
        return {"result": "", "is_error": False, "session_id": "sid"}


def _process(runner):
    return bridge_worker.process_message(
        item={
            "bot_id": "bot",
            "chat_id": "chat",
            "thread_id": None,
            "message_id": "mid",
            "text": "do a long thing",
        },
        bot_config={"workspace": "/tmp"},
        lark_client=None,
        session_map=_DummySessionMap(),
        runner=runner,
        response_handle_cls=_FakeHandle,
        download_image_fn=lambda *a, **k: None,
        fetch_quoted_message_fn=lambda *a, **k: None,
        remove_typing_indicator_fn=lambda *a, **k: None,
        session_not_found_signatures=[],
    )


def test_active_tool_silent_timeout_surfaces_without_retry():
    runner = _SilentRunner([
        {
            "silent_timeout": True,
            "tool_was_active": True,
            "result": "部分输出\n\n⚠️ 长时间无文本输出（>1800s），自动中断恢复中…",
            "accumulated_text": "部分输出",
            "session_id": "sid",
            "is_error": False,
        },
    ])
    handle = _process(runner)

    # Exactly one run() — the initial turn; NO "继续" retry.
    assert runner.calls == ["do a long thing"]
    delivered = " ".join(c for c, _e, _t in handle.deliveries)
    assert "工具运行超过 30 分钟" in delivered
    assert "部分输出" in delivered
    assert all(is_err is False for _c, is_err, _t in handle.deliveries)


def test_model_hang_silent_timeout_auto_continues_once():
    runner = _SilentRunner([
        {
            "silent_timeout": True,
            "tool_was_active": False,
            "result": "thinking…\n\n⚠️ 长时间无文本输出（>480s），自动中断恢复中…",
            "accumulated_text": "thinking…",
            "session_id": "sid",
            "is_error": False,
        },
        {"result": "最终答案", "is_error": False, "session_id": "sid"},
    ])
    handle = _process(runner)

    # Two run() calls: initial turn + the "继续" nudge.
    assert runner.calls == ["do a long thing", "继续"]
    delivered = " ".join(c for c, _e, _t in handle.deliveries)
    assert "最终答案" in delivered


def test_model_hang_second_silent_timeout_marks_error():
    runner = _SilentRunner([
        {
            "silent_timeout": True, "tool_was_active": False,
            "result": "", "accumulated_text": "", "session_id": "sid",
            "is_error": False,
        },
        {
            "silent_timeout": True, "tool_was_active": False,
            "result": "", "accumulated_text": "", "session_id": "sid",
            "is_error": False,
        },
    ])
    handle = _process(runner)

    assert runner.calls == ["do a long thing", "继续"]
    delivered = " ".join(c for c, _e, _t in handle.deliveries)
    assert "自动恢复仍无文本输出" in delivered
    assert any(is_err for _c, is_err, _t in handle.deliveries)
