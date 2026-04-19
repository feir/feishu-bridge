"""Tests for feishu_bridge.session_journal (Phase 6.3 minimal session journal).

Covers:
- Scope hashing by (bot_id, chat_id, thread_id), JSONL filename stability
- Append-only round trip (user_turn + assistant_turn)
- Restart persistence (fresh Journal instance reads prior entries)
- Truncation markers when text exceeds limits
- Redaction of bearer tokens / sk-* keys / long hex blobs
- Scope isolation (different keys → different files)
- Prune-on-write at MAX_ENTRIES cap keeps tail
- Read API: latest_timestamp() + entry_count()
- Writes for all runner types (observational-only governs consumption, not write)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from feishu_bridge import session_journal
from feishu_bridge.session_journal import SessionJournal


@pytest.fixture()
def journal_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("FEISHU_BRIDGE_HOME", str(tmp_path))
    return tmp_path / "journals"


@pytest.fixture()
def scope() -> tuple[str, str, str | None]:
    return ("bot_abc", "chat_xyz", None)


# ---------- scope hashing & path ----------

def test_scope_hash_stable(scope):
    j = SessionJournal()
    h1 = j._scope_hash(*scope)
    h2 = j._scope_hash(*scope)
    assert h1 == h2
    assert len(h1) == 40  # sha1 hex


def test_scope_hash_distinct_for_different_thread():
    j = SessionJournal()
    a = j._scope_hash("bot", "chat", None)
    b = j._scope_hash("bot", "chat", "thread1")
    assert a != b


def test_journal_path_under_bridge_home(journal_root, scope):
    j = SessionJournal()
    path = j._path_for(*scope)
    assert path.parent == journal_root
    assert path.suffix == ".jsonl"


# ---------- append / round trip ----------

def test_append_user_turn_creates_file(journal_root, scope):
    j = SessionJournal()
    j.append_user_turn(*scope, text="hello world", runner_type="pi",
                       provider=None, model="qwen")
    path = j._path_for(*scope)
    assert path.is_file()
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["kind"] == "user_turn"
    assert entry["runner_type"] == "pi"
    assert entry["provider"] is None
    assert entry["model"] == "qwen"
    assert entry["text"] == "hello world"
    assert entry["truncated"] is False
    assert "ts" in entry


def test_append_assistant_turn_appends(journal_root, scope):
    j = SessionJournal()
    j.append_user_turn(*scope, text="ping", runner_type="claude",
                       provider="anthropic", model="opus")
    j.append_assistant_turn(*scope, text="pong", runner_type="claude",
                            provider="anthropic", model="opus",
                            session_id="sid-1")
    entries = list(j.read(*scope))
    assert len(entries) == 2
    assert [e["kind"] for e in entries] == ["user_turn", "assistant_turn"]
    assert entries[1]["session_id"] == "sid-1"


def test_restart_persistence(journal_root, scope):
    j1 = SessionJournal()
    j1.append_user_turn(*scope, text="first", runner_type="pi")
    j1.append_assistant_turn(*scope, text="reply", runner_type="pi",
                             session_id="sid-2")
    del j1
    j2 = SessionJournal()
    entries = list(j2.read(*scope))
    assert len(entries) == 2
    assert entries[0]["text"] == "first"
    assert entries[1]["session_id"] == "sid-2"


# ---------- truncation ----------

def test_user_turn_truncated_at_limit(journal_root, scope):
    # Use non-hex chars to avoid triggering the hex-blob redactor.
    big = "x y z " * ((session_journal.USER_MAX_BYTES // 6) + 200)
    j = SessionJournal()
    j.append_user_turn(*scope, text=big, runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert entry["truncated"] is True
    assert len(entry["text"].encode("utf-8")) <= session_journal.USER_MAX_BYTES


def test_assistant_turn_truncated_at_limit(journal_root, scope):
    big = "u v w " * ((session_journal.ASSISTANT_MAX_BYTES // 6) + 900)
    j = SessionJournal()
    j.append_assistant_turn(*scope, text=big, runner_type="claude")
    entry = list(j.read(*scope))[0]
    assert entry["truncated"] is True
    assert len(entry["text"].encode("utf-8")) <= session_journal.ASSISTANT_MAX_BYTES


def test_short_text_not_truncated(journal_root, scope):
    j = SessionJournal()
    j.append_user_turn(*scope, text="short", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert entry["truncated"] is False


# ---------- redaction ----------

def test_redacts_sk_ant_key(journal_root, scope):
    secret = "sk-ant-api03-" + "x" * 80
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"token is {secret}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert secret not in entry["text"]
    assert "[REDACTED" in entry["text"]
    assert entry["redactions"] >= 1


def test_redacts_bearer_token(journal_root, scope):
    j = SessionJournal()
    j.append_user_turn(*scope,
                       text="Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdef.xyz",
                       runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert "eyJhbGciOiJIUzI1NiJ9" not in entry["text"]
    assert entry["redactions"] >= 1


def test_redacts_long_hex_blob(journal_root, scope):
    hex_blob = "a1b2c3" * 20  # 120 hex chars, > 40 threshold
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"key={hex_blob}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert hex_blob not in entry["text"]
    assert entry["redactions"] >= 1


def test_no_redaction_for_clean_text(journal_root, scope):
    j = SessionJournal()
    j.append_user_turn(*scope, text="hello friend", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert entry["text"] == "hello friend"
    assert entry["redactions"] == 0


# ---------- scope isolation ----------

def test_different_scopes_write_different_files(journal_root):
    j = SessionJournal()
    j.append_user_turn("botA", "chat1", None, text="A", runner_type="pi")
    j.append_user_turn("botB", "chat1", None, text="B", runner_type="pi")
    j.append_user_turn("botA", "chat1", "thread9", text="C", runner_type="pi")
    files = sorted(p.name for p in journal_root.glob("*.jsonl"))
    assert len(files) == 3


def test_read_isolated_per_scope(journal_root):
    j = SessionJournal()
    j.append_user_turn("botA", "chat1", None, text="A", runner_type="pi")
    j.append_user_turn("botB", "chat1", None, text="B", runner_type="pi")
    a = list(j.read("botA", "chat1", None))
    b = list(j.read("botB", "chat1", None))
    assert len(a) == 1 and a[0]["text"] == "A"
    assert len(b) == 1 and b[0]["text"] == "B"


# ---------- prune-on-write ----------

def test_prune_on_write_keeps_tail(journal_root, scope, monkeypatch):
    monkeypatch.setattr(session_journal, "MAX_ENTRIES", 5)
    j = SessionJournal()
    for i in range(12):
        j.append_user_turn(*scope, text=f"msg{i}", runner_type="pi")
    entries = list(j.read(*scope))
    assert len(entries) == 5
    assert entries[0]["text"] == "msg7"
    assert entries[-1]["text"] == "msg11"


# ---------- read API ----------

def test_entry_count_empty_scope(journal_root, scope):
    j = SessionJournal()
    assert j.entry_count(*scope) == 0


def test_entry_count_after_writes(journal_root, scope):
    j = SessionJournal()
    j.append_user_turn(*scope, text="1", runner_type="pi")
    j.append_user_turn(*scope, text="2", runner_type="pi")
    assert j.entry_count(*scope) == 2


def test_latest_timestamp_none_when_empty(journal_root, scope):
    j = SessionJournal()
    assert j.latest_timestamp(*scope) is None


def test_latest_timestamp_returns_most_recent(journal_root, scope):
    j = SessionJournal()
    j.append_user_turn(*scope, text="first", runner_type="pi")
    j.append_assistant_turn(*scope, text="second", runner_type="pi")
    ts = j.latest_timestamp(*scope)
    assert ts is not None
    assert isinstance(ts, (int, float))


# ---------- runner-agnostic writes ----------

@pytest.mark.parametrize("rtype", ["claude", "pi", "codex", "local"])
def test_writes_for_all_runner_types(journal_root, scope, rtype):
    j = SessionJournal()
    j.append_user_turn(*scope, text="hi", runner_type=rtype)
    entry = list(j.read(*scope))[0]
    assert entry["runner_type"] == rtype


# ---------- workflow / artifact kinds ----------

def test_append_workflow_event(journal_root, scope):
    j = SessionJournal()
    j.append_workflow_event(*scope, command="/plan",
                            decision="bridge_workflow",
                            runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert entry["kind"] == "workflow_event"
    assert entry["command"] == "/plan"
    assert entry["decision"] == "bridge_workflow"


def test_append_artifact(journal_root, scope):
    j = SessionJournal()
    j.append_artifact(*scope, path="/tmp/out.md", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert entry["kind"] == "artifact"
    assert entry["path"] == "/tmp/out.md"


# ---------- error-turn semantics ----------

def test_error_turn_records_user_without_assistant(journal_root, scope):
    """Error path: worker only calls append_user_turn, not append_assistant_turn.

    Simulates the worker.py split where assistant_turn is gated on success.
    """
    j = SessionJournal()
    # Error turn: only user_turn is written (effective_sid is None).
    j.append_user_turn(*scope, text="ask that failed", runner_type="claude",
                       session_id=None)
    entries = list(j.read(*scope))
    assert len(entries) == 1
    assert entries[0]["kind"] == "user_turn"
    assert entries[0]["session_id"] is None


# =========================================================================
# Round 2 fixes (from Codex review-6.3)
# =========================================================================


# ---------- expanded redaction coverage (finding #2) ----------

def test_redacts_aws_access_key(journal_root, scope):
    akia = "AKIA" + "A" * 16
    asia = "ASIA" + "B" * 16
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"keys: {akia} and {asia}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert akia not in entry["text"]
    assert asia not in entry["text"]
    assert entry["redactions"] >= 2


def test_redacts_slack_token(journal_root, scope):
    tok = "xoxb-" + "A" * 40
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"slack={tok}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert tok not in entry["text"]
    assert entry["redactions"] >= 1


def test_redacts_github_classic_pat(journal_root, scope):
    tok = "ghp_" + "A" * 36
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"gh={tok}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert tok not in entry["text"]
    assert entry["redactions"] >= 1


def test_redacts_github_fine_grained_pat(journal_root, scope):
    tok = "github_pat_" + "A" * 80
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"gh={tok}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert tok not in entry["text"]
    assert entry["redactions"] >= 1


def test_redacts_github_oauth_variants(journal_root, scope):
    # gho_ (OAuth app), ghu_ (user-to-server), ghs_ (server-to-server), ghr_ (refresh)
    tokens = ["gho_" + "A" * 30, "ghu_" + "B" * 30,
              "ghs_" + "C" * 30, "ghr_" + "D" * 30]
    j = SessionJournal()
    j.append_user_turn(*scope, text=" ".join(tokens), runner_type="pi")
    entry = list(j.read(*scope))[0]
    for t in tokens:
        assert t not in entry["text"]
    assert entry["redactions"] >= 4


def test_redacts_raw_jwt(journal_root, scope):
    # Minimal valid-shape JWT: eyJ... . payload . signature
    jwt = ("eyJhbGciOiJIUzI1NiJ9"
           ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
           ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"token: {jwt}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    # The eyJ... prefix is what marks it; after redaction the full 3-segment
    # token must not be present.
    assert jwt not in entry["text"]
    assert entry["redactions"] >= 1


def test_does_not_redact_commit_sha(journal_root, scope):
    """40-char hex = git commit SHA. Narrowed hex rule (≥64) must let it through."""
    sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"  # 40 chars
    assert len(sha) == 40
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"see commit {sha}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert sha in entry["text"]
    assert entry["redactions"] == 0


def test_does_not_redact_uuid_without_hyphens(journal_root, scope):
    """32-char hex (UUID without hyphens). Narrowed rule must let it through."""
    uuid_hex = "550e8400e29b41d4a716446655440000"
    assert len(uuid_hex) == 32
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"id={uuid_hex}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert uuid_hex in entry["text"]


def test_redacts_sha256_hex(journal_root, scope):
    """64-char hex (SHA-256 digest). Must still be redacted."""
    sha256 = "a" * 64
    j = SessionJournal()
    j.append_user_turn(*scope, text=f"hash={sha256}", runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert sha256 not in entry["text"]
    assert entry["redactions"] >= 1


# ---------- workflow / artifact sanitization (finding #3) ----------

def test_workflow_event_redacts_command_secret(journal_root, scope):
    """Defensive: if a caller ever forwards raw user text as command."""
    tok = "ghp_" + "A" * 36
    j = SessionJournal()
    j.append_workflow_event(*scope,
                            command=f"/plan {tok}",
                            decision="bridge_workflow",
                            runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert tok not in entry["command"]
    assert entry["redactions"] >= 1


def test_workflow_event_truncates_long_decision(journal_root, scope):
    from feishu_bridge import session_journal as sj
    long_decision = "x" * (sj.WORKFLOW_DECISION_MAX_BYTES + 500)
    j = SessionJournal()
    j.append_workflow_event(*scope, command="/plan",
                            decision=long_decision, runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert len(entry["decision"].encode("utf-8")) <= sj.WORKFLOW_DECISION_MAX_BYTES
    assert entry["truncated"] is True


def test_artifact_redacts_path_secret(journal_root, scope):
    signed = "https://example.com/upload?token=" + "ghp_" + "A" * 36
    j = SessionJournal()
    j.append_artifact(*scope, path=signed, runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert "ghp_" + "A" * 36 not in entry["path"]
    assert entry["redactions"] >= 1


def test_artifact_truncates_long_path(journal_root, scope):
    from feishu_bridge import session_journal as sj
    long_path = "/tmp/" + "p" * (sj.ARTIFACT_PATH_MAX_BYTES + 500)
    j = SessionJournal()
    j.append_artifact(*scope, path=long_path, runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert len(entry["path"].encode("utf-8")) <= sj.ARTIFACT_PATH_MAX_BYTES
    assert entry["truncated"] is True


# ---------- advisory flock around append + prune (finding #1) ----------

def test_append_acquires_exclusive_flock(journal_root, scope, monkeypatch):
    """Structural test: _append must acquire LOCK_EX and release LOCK_UN."""
    import fcntl
    from feishu_bridge import session_journal as sj
    calls: list[int] = []
    real_flock = fcntl.flock

    def spy_flock(fd, op):
        calls.append(op)
        return real_flock(fd, op)

    monkeypatch.setattr(sj.fcntl, "flock", spy_flock)
    j = SessionJournal()
    j.append_user_turn(*scope, text="hello", runner_type="pi")
    assert fcntl.LOCK_EX in calls
    assert fcntl.LOCK_UN in calls
    # LOCK_EX precedes LOCK_UN for each lock/unlock pair.
    assert calls.index(fcntl.LOCK_EX) < calls.index(fcntl.LOCK_UN)


def test_concurrent_appends_preserve_cap(journal_root, scope, monkeypatch):
    """Under concurrent writers + aggressive pruning, the cap holds and
    the tail contains genuine entries (no corruption / no over-prune)."""
    import threading
    from feishu_bridge import session_journal as sj
    monkeypatch.setattr(sj, "MAX_ENTRIES", 20)

    j = SessionJournal()
    barrier = threading.Barrier(4)

    def writer(tag: str, n: int):
        barrier.wait()
        for i in range(n):
            j.append_user_turn(*scope, text=f"{tag}-{i}", runner_type="pi")

    threads = [threading.Thread(target=writer, args=(tag, 50))
               for tag in "ABCD"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    count = j.entry_count(*scope)
    # After 200 writes with cap=20, the tail must contain exactly 20 entries.
    # Without the flock, prune races would cause count < 20 intermittently.
    assert count == 20
    entries = list(j.read(*scope))
    assert len(entries) == 20
    # Every retained entry must parse cleanly and carry a legitimate text tag.
    for e in entries:
        assert e["kind"] == "user_turn"
        assert "-" in e["text"]
        tag, _, idx = e["text"].partition("-")
        assert tag in {"A", "B", "C", "D"}
        assert idx.isdigit()


# ---------- multibyte truncation boundary (finding #8) ----------

def test_truncation_respects_multibyte_codepoint_boundary(journal_root, scope,
                                                          monkeypatch):
    """If the truncation byte boundary lands mid-codepoint, the decoder must
    drop the split fragment — the result must never contain invalid UTF-8
    and must never exceed max_bytes."""
    from feishu_bridge import session_journal as sj
    # Each '好' is 3 bytes in UTF-8. Set a cap that lands mid-codepoint.
    monkeypatch.setattr(sj, "USER_MAX_BYTES", 50)
    text = "好" * 40  # 120 bytes, well over 50
    j = SessionJournal()
    j.append_user_turn(*scope, text=text, runner_type="pi")
    entry = list(j.read(*scope))[0]
    assert entry["truncated"] is True
    encoded = entry["text"].encode("utf-8")
    assert len(encoded) <= sj.USER_MAX_BYTES
    # Round-tripping must succeed (no replacement chars, no raw partial bytes).
    entry["text"].encode("utf-8").decode("utf-8")


# ---------- prune fd-leak resilience (finding #7) ----------

def test_prune_cleans_up_temp_file_on_fdopen_failure(journal_root, scope,
                                                      monkeypatch):
    """If os.fdopen raises inside _maybe_prune_locked, the temp file must
    not leak into the journal directory."""
    import os as _os
    from feishu_bridge import session_journal as sj
    monkeypatch.setattr(sj, "MAX_ENTRIES", 3)
    j = SessionJournal()
    # Pre-populate enough entries to trigger prune on next append.
    for i in range(3):
        j.append_user_turn(*scope, text=f"seed-{i}", runner_type="pi")

    def boom_fdopen(fd, *args, **kwargs):
        # Close the fd so we don't leak it ourselves; then raise to simulate
        # an OSError inside the prune write path.
        _os.close(fd)
        raise OSError("simulated prune fdopen failure")

    monkeypatch.setattr(sj.os, "fdopen", boom_fdopen)
    # This append triggers prune; prune fails but must not propagate.
    j.append_user_turn(*scope, text="trigger-prune", runner_type="pi")

    # No stray temp files should remain in the journal dir.
    tmp_files = list(journal_root.glob(".journal-*.tmp"))
    assert tmp_files == [], f"leaked temp files: {tmp_files}"
