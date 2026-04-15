"""
Ollama think-proxy: translates Anthropic /v1/messages → Ollama /api/chat
with think:false injected, then translates the response back.

Allows Claude CLI to use Ollama without triggering extended thinking,
since Ollama's /v1/messages endpoint always enables thinking and ignores
the think parameter.

Usage:
    from feishu_bridge.ollama_proxy import start_proxy
    start_proxy(port=11435, ollama_url="http://127.0.0.1:11434")
"""

import json
import logging
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

log = logging.getLogger(__name__)

_DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"


# ---------------------------------------------------------------------------
# Request translation: Anthropic /v1/messages → Ollama /api/chat
# ---------------------------------------------------------------------------

def _anthropic_content_to_text(content) -> str:
    """Flatten Anthropic content (string or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "tool_result":
                inner = block.get("content", "")
                parts.append(_anthropic_content_to_text(inner))
        else:
            parts.append(str(block))
    return "\n".join(p for p in parts if p)


def _convert_tools(anthropic_tools: list) -> list:
    """Convert Anthropic tools schema to Ollama format."""
    ollama_tools = []
    for t in anthropic_tools:
        schema = t.get("input_schema", {})
        ollama_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": schema,
            },
        })
    return ollama_tools


def _build_ollama_request(anthropic_req: dict) -> dict:
    """Build Ollama /api/chat request from Anthropic /v1/messages request."""
    messages = []

    # System prompt
    system = anthropic_req.get("system")
    if system:
        text = _anthropic_content_to_text(system) if isinstance(system, list) else system
        messages.append({"role": "system", "content": text})

    # Conversation messages
    for msg in anthropic_req.get("messages", []):
        role = msg["role"]
        content = msg.get("content", "")

        if role == "assistant" and isinstance(content, list):
            # Separate text blocks and tool_use blocks
            tool_calls = []
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
                elif isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            ollama_msg = {"role": "assistant", "content": "\n".join(text_parts)}
            if tool_calls:
                ollama_msg["tool_calls"] = tool_calls
            messages.append(ollama_msg)

        elif role == "user" and isinstance(content, list):
            # Check for tool_result blocks — Ollama expects role:"tool" messages
            tool_results = [b for b in content
                            if isinstance(b, dict) and b.get("type") == "tool_result"]
            other_blocks = [b for b in content
                            if not (isinstance(b, dict) and b.get("type") == "tool_result")]

            for block in tool_results:
                result_content = _anthropic_content_to_text(block.get("content", ""))
                messages.append({
                    "role": "tool",
                    "content": result_content,
                    "tool_call_id": block.get("tool_use_id", ""),
                })

            if other_blocks:
                messages.append({
                    "role": "user",
                    "content": _anthropic_content_to_text(other_blocks),
                })

        else:
            messages.append({
                "role": role,
                "content": _anthropic_content_to_text(content),
            })

    ollama_req = {
        "model": anthropic_req.get("model", ""),
        "messages": messages,
        "think": False,  # KEY: disable thinking
        "stream": anthropic_req.get("stream", False),
    }

    # Tools
    if anthropic_req.get("tools"):
        ollama_req["tools"] = _convert_tools(anthropic_req["tools"])

    # Max tokens → num_predict
    if anthropic_req.get("max_tokens"):
        ollama_req["options"] = {"num_predict": anthropic_req["max_tokens"]}

    return ollama_req


# ---------------------------------------------------------------------------
# Response translation: Ollama /api/chat → Anthropic /v1/messages
# ---------------------------------------------------------------------------

def _make_message_id() -> str:
    return f"msg_{int(time.time() * 1000):x}"


def _build_anthropic_response(ollama_resp: dict, model: str) -> dict:
    """Build Anthropic response from Ollama non-streaming response."""
    msg = ollama_resp.get("message", {})
    content_text = msg.get("content", "")
    tool_calls = msg.get("tool_calls", [])

    content_blocks = []
    if content_text:
        content_blocks.append({"type": "text", "text": content_text})

    stop_reason = "end_turn"
    for tc in tool_calls:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                input_data = json.loads(raw_args)
            except json.JSONDecodeError:
                input_data = {}
        else:
            input_data = raw_args

        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{int(time.time()*1000):x}"),
            "name": fn.get("name", ""),
            "input": input_data,
        })
        stop_reason = "tool_use"

    in_tok = ollama_resp.get("prompt_eval_count", 0)
    out_tok = ollama_resp.get("eval_count", 0)
    log.info("ollama tokens: model=%s prefill=%d gen=%d", model, in_tok, out_tok)
    return {
        "id": _make_message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
        },
    }


def _stream_anthropic_events(ollama_resp_iter: Iterator[bytes], model: str):
    """Convert Ollama NDJSON stream to Anthropic SSE events."""
    msg_id = _make_message_id()

    def sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield sse("content_block_start", {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    })
    yield sse("ping", {"type": "ping"})

    input_tokens = 0
    output_tokens = 0
    stop_reason = "end_turn"
    tool_calls_buf: list = []

    for line in ollama_resp_iter:
        line = line.strip()
        if not line:
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg = chunk.get("message", {})
        delta_text = msg.get("content", "")
        delta_tools = msg.get("tool_calls", [])

        if delta_text:
            yield sse("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": delta_text},
            })

        if delta_tools:
            tool_calls_buf.extend(delta_tools)

        if chunk.get("prompt_eval_count"):
            input_tokens = chunk["prompt_eval_count"]
        if chunk.get("eval_count"):
            output_tokens = chunk["eval_count"]

        if chunk.get("done"):
            break

    log.info("ollama tokens (stream): model=%s prefill=%d gen=%d", model, input_tokens, output_tokens)
    yield sse("content_block_stop", {"type": "content_block_stop", "index": 0})

    # Emit tool_use blocks if any
    for i, tc in enumerate(tool_calls_buf, start=1):
        fn = tc.get("function", {})
        raw_args = fn.get("arguments", "{}")
        if isinstance(raw_args, str):
            try:
                input_json = json.dumps(json.loads(raw_args))
            except json.JSONDecodeError:
                input_json = "{}"
        else:
            input_json = json.dumps(raw_args)

        tool_id = tc.get("id", f"toolu_{int(time.time()*1000):x}")
        yield sse("content_block_start", {
            "type": "content_block_start", "index": i,
            "content_block": {"type": "tool_use", "id": tool_id,
                              "name": fn.get("name", ""), "input": {}},
        })
        yield sse("content_block_delta", {
            "type": "content_block_delta", "index": i,
            "delta": {"type": "input_json_delta", "partial_json": input_json},
        })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": i})
        stop_reason = "tool_use"

    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    })
    yield sse("message_stop", {"type": "message_stop"})


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _ProxyHandler(BaseHTTPRequestHandler):
    ollama_url: str = _DEFAULT_OLLAMA_URL
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # suppress default access log
        log.debug("proxy: " + fmt, *args)

    def _send_error(self, code: int, message: str):
        body = json.dumps({"error": {"type": "proxy_error", "message": message}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path not in ("/v1/messages", "/v1/messages?beta=true"):
            self._send_error(404, f"unknown path: {self.path}")
            return

        content_length = self.headers.get("Content-Length")
        if not content_length:
            self._send_error(400, "missing Content-Length")
            return
        body = self.rfile.read(int(content_length))
        try:
            anthropic_req = json.loads(body)
        except json.JSONDecodeError as e:
            self._send_error(400, f"invalid JSON: {e}")
            return

        model = anthropic_req.get("model", "")
        streaming = anthropic_req.get("stream", False)
        ollama_req = _build_ollama_request(anthropic_req)

        ollama_body = json.dumps(ollama_req).encode()
        req = urllib.request.Request(
            f"{self.ollama_url}/api/chat",
            data=ollama_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except Exception as e:
            log.error("proxy: ollama request failed: %s", e)
            self._send_error(502, str(e))
            return

        if streaming:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for chunk in _stream_anthropic_events(resp, model):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except BrokenPipeError:
                pass
            finally:
                resp.close()
        else:
            try:
                raw = resp.read()
            finally:
                resp.close()
            try:
                ollama_resp = json.loads(raw)
            except json.JSONDecodeError:
                self._send_error(502, "ollama returned invalid JSON")
                return
            result = _build_anthropic_response(ollama_resp, model)
            body_out = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_out)))
            self.end_headers()
            self.wfile.write(body_out)

    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_error(404, "not found")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_proxy(port: int = 11435, ollama_url: str = _DEFAULT_OLLAMA_URL) -> ThreadingHTTPServer:
    """Start the think-proxy in a daemon thread. Returns the server instance."""

    class _Handler(_ProxyHandler):
        pass
    _Handler.ollama_url = ollama_url

    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True,
                         name="ollama-think-proxy")
    t.start()
    log.info("ollama-think-proxy started on http://127.0.0.1:%d → %s", port, ollama_url)
    return server
