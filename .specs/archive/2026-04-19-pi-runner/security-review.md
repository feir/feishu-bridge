# Security Review: Pi Runner Phase 3

Date: 2026-04-19

## Scope

Remote Feishu message input flows through Feishu Bridge into `PiRunner`, which launches the local Pi CLI against the configured workspace and model provider.

## Decisions

- Pi remains read-only by default. `PiRunner` injects `--tools read,grep,find,ls` when config does not explicitly provide `--tools` or `--no-tools`.
- Write and shell tools are not enabled for staging or production by this phase.
- Staging keeps Pi context loading disabled through CLI flags until the compact context files are reviewed in live usage.
- Bridge workflows own side effects for `/plan`, `/done`, and `/memory-gc`; the local model should not create hidden writes outside the workflow runtime.

## Findings

- PASS: Default Pi tool exposure is read-only and enforced in `feishu_bridge/runtime_pi.py`.
- PASS: The staging Pi config disables context files, extensions, skills, prompt templates, and themes, reducing accidental prompt expansion.
- PASS: Provider/model display now reports the actual Pi model name through `/model` and `/status`.
- PASS: Runner-neutral workflow commands are scoped by registry and unsupported commands have explicit fallback behavior.
- DEFER: Optional write/edit enablement needs a dedicated allowlist and disposable-file smoke test before use.
- DEFER: Optional shell enablement needs a command policy, denylist or allowlist, and tests for bridge restart and destructive git commands.
- DEFER: Broad context loading should stay off for Pi until token growth is measured with the compact global and project-local context files.

## Acceptance

Phase 3 may proceed with read-only PiRunner usage. Enabling write or shell tools is explicitly outside this gate and remains blocked until Phase 5.

