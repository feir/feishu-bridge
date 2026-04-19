"""Path Z code-level smoke (6.9 interim). Run: uv run python tests/smoke_path_z.py"""
import io
import json
import logging
import os
import tempfile

from feishu_bridge.main import create_runner, load_config, resolve_model_aliases


def main() -> int:
    print('=== 6.9 Code-level smoke: Path Z empty configs ===\n')

    cfg = {'type': 'claude', '_resolved_command': 'claude'}
    r = create_runner(cfg, {'workspace': '/tmp'}, [])
    args = r.build_args('hi', None, False, False)
    print(f'[claude] type={type(r).__name__} model={r.model!r} '
          f'--model in args={("--model" in args)} aliases={resolve_model_aliases(cfg)}')

    cfg = {'type': 'codex', '_resolved_command': 'codex'}
    r = create_runner(cfg, {'workspace': '/tmp'}, [])
    args = r.build_args('hi', None, False, True)
    print(f'[codex]  type={type(r).__name__} model={r.model!r} '
          f'--model in args={("--model" in args)} aliases={resolve_model_aliases(cfg)}')

    cfg = {'type': 'pi', '_resolved_command': 'pi'}
    r = create_runner(cfg, {'workspace': '/tmp'}, [])
    args = r.build_args('hi', None, False, True)
    print(f'[pi]     type={type(r).__name__} model={r.model!r} '
          f'--model in args={("--model" in args)} aliases={resolve_model_aliases(cfg)}')

    # Migration check: agent.type='local' must be rejected at load_config with
    # a friendly log.error + sys.exit(1) (no ValueError, no LocalHTTPRunner).
    log_buf = io.StringIO()
    log_handler = logging.StreamHandler(log_buf)
    log_handler.setLevel(logging.ERROR)
    logging.getLogger("feishu-bridge").addHandler(log_handler)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(
            {"bots": [{"name": "b", "app_id": "x", "app_secret": "s",
                       "workspace": "/tmp", "allowed_users": ["u1"]}],
             "agent": {"type": "local"}},
            tmp,
        )
        tmp.flush()
        tmp.close()
        try:
            load_config(tmp.name, "b")
            print("[local]  UNEXPECTED: load_config did not sys.exit")
            return 1
        except SystemExit as e:
            out = log_buf.getvalue()
            ok = e.code == 1 and "已于 2026-04-19 移除" in out and "codex" in out
            if not ok:
                print(f"[local]  UNEXPECTED: exit={e.code!r} log={out!r}")
                return 1
            print("[local]  migration rejected at load_config (expected)")
    finally:
        logging.getLogger("feishu-bridge").removeHandler(log_handler)
        os.unlink(tmp.name)

    cfg = {
        'type': 'pi', '_resolved_command': 'pi',
        'providers': {'default': {
            'model_aliases': {'qwen': 'Qwen3.6-35B-A3B-mxfp4',
                              'gemma': 'gemma-4-26b-a4b-it-mxfp4'},
        }},
    }
    aliases = resolve_model_aliases(cfg)
    print()
    print(f'[alias] qwen   -> {aliases.get("qwen", "qwen")}')
    print(f'[alias] gemma  -> {aliases.get("gemma", "gemma")}')
    print(f'[alias] sonnet -> {aliases.get("sonnet", "sonnet")} (unknown, passes through)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
