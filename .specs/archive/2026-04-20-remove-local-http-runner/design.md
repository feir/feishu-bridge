# Design: remove-local-http-runner

## 架构图（变更前后）

### Before

```
BaseRunner
├── ClaudeRunner   (Claude CLI)
├── CodexRunner    (Codex CLI)
├── PiRunner       (Pi CLI)
└── LocalHTTPRunner (HTTP → Ollama / vLLM / LM Studio)   ← 被移除
```

### After

```
BaseRunner
├── ClaudeRunner
├── CodexRunner    (+ OpenAI-compatible endpoints 覆盖 LocalHTTP 场景)
└── PiRunner
```

## 关键变更点

### 1. `_RUNNER_CLASSES` Registry（main.py, **not** runtime.py）

侦察校正：registry 实际位于 `feishu_bridge/main.py:138-142`，而不是 `runtime.py`。

```python
# main.py:138-142 — Before
_RUNNER_CLASSES: dict[str, type[BaseRunner]] = {
    "claude": ClaudeRunner,
    "codex": CodexRunner,
    "pi": PiRunner,
    "local": LocalHTTPRunner,   # ← 删除此行
}

# main.py:138-141 — After
_RUNNER_CLASSES: dict[str, type[BaseRunner]] = {
    "claude": ClaudeRunner,
    "codex": CodexRunner,
    "pi": PiRunner,
}
```

同步删除 `from feishu_bridge.runtime_local import LocalHTTPRunner`（`main.py:59`）。

一并删除 `_LOCAL_PROMPT_DEFAULTS` 常量（`main.py:146`）及其使用分支（`main.py:156-161`）——LocalHTTP 特有的 `{"safety": "minimal", "feishu_cli": False, "cron_mgr": False}` prompt 默认值集合随 runner 一起消失。剩余 3 runners 统一使用 `_GLOBAL_PROMPT_DEFAULTS`。

### 2. 加载层 + 热切换 Migration Error（main.py）

**位置 A — `load_config`**（`main.py:471-478` 附近）：在 `if agent_type not in _RUNNER_CLASSES` 校验之前加专门的 `"local"` 分支，风格与邻居一致（`log.error + sys.exit(1)`，**不用 raise**）。

```python
# main.py load_config 里的新增分支（在当前 _RUNNER_CLASSES 校验之前）
if agent_type == "local":
    log.error(
        "agent.type='local' 已于 2026-04-19 移除（LocalHTTPRunner 被删除）。"
        "请迁移至 type=\"codex\" + --oss --local-provider ollama，"
        "详见 README §本地模型接入。"
    )
    sys.exit(1)
```

**风格选择理由**（评审 H2）：周围的 `agent.type is required`（`main.py:471-474`）和 `Unknown agent type`（`main.py:475-478`）均用 `log.error + sys.exit(1)`；`ConfigError` 在 `_normalize_provider_profiles` 抛出后也由 load_config 内部的 `try/except ConfigError → log.error + sys.exit(1)`（`main.py:518-520`）承接。若用 `raise ValueError`，`main()` 的调用点 `main.py:1904` 无 try/except，用户会看到 Python traceback。统一为 `log.error + sys.exit(1)` 保证用户看到单行中文迁移提示。

**位置 B — `switch_agent`**（`main.py:879-884`）：hot-swap 用户运行中用 `/agent local` 会命中 L882 的通用 `未知 Agent 类型` 错误。在 `_RUNNER_CLASSES` 校验**之前**加 `"local"` 迁移分支，返回与 load_config 一致的迁移文案（单行中文）。

```python
# main.py switch_agent 入口，target_type 规范化后、_RUNNER_CLASSES 校验前
if target_type == "local":
    return (
        False,
        "Agent 'local' 已于 2026-04-19 移除。"
        "请在配置文件改用 type=\"codex\" + --oss --local-provider ollama，详见 README。",
        None,
    )
```

### 3. 清理分支代码（main.py — 侦察校正清单）

| 行号 | 分支 | 处理 |
|------|------|------|
| `main.py:59` | `from feishu_bridge.runtime_local import LocalHTTPRunner` | 删除 import |
| `main.py:138-142` | `_RUNNER_CLASSES["local"]` | 删除 dict 条目 |
| `main.py:146` | `_LOCAL_PROMPT_DEFAULTS = {...}` | 删除常量 |
| `main.py:156-161` | `base = _LOCAL_PROMPT_DEFAULTS if agent_type == "local" else _GLOBAL_PROMPT_DEFAULTS` | 简化为 `base = _GLOBAL_PROMPT_DEFAULTS`；同时更新 `_normalize_prompt_config` docstring 去掉 "For agent_type=='local' the base defaults flip to safety=minimal..." 提及（L1） |
| `main.py:176-190` | `_normalize_endpoint_config()` 整函数（LocalHTTP-only helper） | **删除整函数**（H1 补充） |
| `main.py:265-273` | `_normalize_provider_profiles` endpoint 块 + local-extras preserved keys (`max_tokens`/`context_window`/`openai_include_usage`) | **删除 endpoint 块**；preserved keys 收窄为 `("model_aliases", "workspace")`（H1 补充） |
| `main.py:315-320` | command 解析优先拿 provider profile 的 `commands.local`，fallback 到 `"local"` 字面量 sentinel | 删除整个 `if agent_type == "local"` 块 |
| `main.py:471-478` | load_config 里 `agent_type not in _RUNNER_CLASSES` 校验 | **在该校验之前**新增 `"local"` 迁移分支（§2 位置 A） |
| `main.py:483-484` | `elif agent_type == "local": default_cmd = "local"  # sentinel` | 删除分支 |
| `main.py:495-497` | `_prompt_raw` 注释 "claude→local would keep feishu_cli=True" | 更新注释去掉 `local` 举例（L1） |
| `main.py:522` | `if agent_type != "local" and not resolved_cmd:` | 简化为 `if not resolved_cmd:` |
| `main.py:614` | `runner_cls = _RUNNER_CLASSES[agent_type]` | 自动跟随（不含 "local" 自然抛 KeyError） |
| `main.py:631` | `if agent_type == "local":` create_runner 分支 | 删除（构造 LocalHTTPRunner 的路径） |
| `main.py:844, 904` | `switch_agent` / `switch_provider` 里 `if target_type != "local" and not resolved_cmd:` | 简化为 `if not resolved_cmd:` |
| `main.py:879-884` | `switch_agent` 入口 `target_type not in _RUNNER_CLASSES` 校验 | **在该校验之前**新增 `"local"` 迁移分支（§2 位置 B） |
| `main.py:881-882` | switch 错误信息里 `" / ".join(sorted(_RUNNER_CLASSES))` | 自动跟随 |

### 3b. Runner 类型识别（worker.py）

```python
# worker.py:41-50 — Before
def _runner_type(runner):
    name = type(runner).__name__.lower()
    if "claude" in name:
        return "claude"
    if name.startswith("pi"):
        return "pi"
    if "codex" in name:
        return "codex"
    if "local" in name:    # ← 删除此分支
        return "local"
    return name.replace("runner", "") or "unknown"
```

### 3c. Docstring 清理

- `feishu_bridge/runtime.py:424, 433`：BaseRunner 方法 docstring 提及 "LocalHTTPRunner overrides to..."，改为去掉 LocalHTTPRunner 提及
- `feishu_bridge/session_journal.py:17`：`"runner_type": "claude" | "pi" | "codex" | "local"` → 去掉 `"local"`
- `feishu_bridge/workflows/registry.py:105`：docstring `'claude', 'pi', 'codex', 'local'` → 去掉 `'local'`

### 3d. /agent 命令选项枚举（commands.py）

`commands.py:49-50` 和 `:602-603` 里 `_RUNNER_CLASSES` 读取自动跟随 registry，无需改动（dict 少了 "local" 之后枚举自然收窄）。

### 4. `tests/smoke_path_z.py`（M5 转换非删除）

当前 L27-35 断言 `create_runner(type='local')` raise `ValueError`（Path Z strict model-required 守卫）。删除整轮会丢失对 migration error 的廉价回归覆盖。

**转换**为 `load_config` 层 migration assertion，匹配 §2 位置 A：

```python
# Before (L27-35)
cfg = {'type': 'local', '_resolved_command': 'local',
       'providers': {'default': {'endpoint': {'base_url': 'http://127.0.0.1:8000'}}}}
try:
    create_runner(cfg, {'workspace': '/tmp'}, [])
    print('[local]  UNEXPECTED: did not raise')
    return 1
except ValueError as e:
    print(f'[local]  ValueError raised (expected): {e}')

# After — 写入 temp config JSON 后调 load_config，断言 SystemExit + log 输出含"已于 2026-04-19 移除"
# 用 monkeypatch 捕获 log.error 文案或 assert SystemExit(1)
```

设计意图：保留 `[local]` 轮次作为 smoke-level migration 回归测试，但断言从 runtime 构造错误改为 load-time 友好迁移提示。ROUNDS 数组从 4 项变 "3 runner round + 1 migration round"（即 4 段输出不变，最后一段语义从 ValueError 改为 SystemExit+log message）。

### 5. README 更新（M3 补枚举）

用户可见的 `local` 出现位置（评审 M3 findings + 侦察验证）：

- **L177**：agent-type 行内注释 `"type": "claude", // claude | codex | local | pi` → 去掉 `local`，变 `claude | codex | pi`
- **L206**：`/agent claude / /agent codex / /agent local / /agent pi` → 去掉 `/agent local`
- **L316**：commands dict 示例 `"local": "local"` 条目 → 删除该行
- §"模型相关配置归属 CLI" 表格 → 删除 LocalHTTPRunner 行
- **L387**：LocalHTTPRunner model-required 要点 → 删除整条
- 新增"迁移指引"段：展示 `"type": "local"` → `"type": "codex"` + `--oss --local-provider ollama` 配置映射（proposal.md 已定稿示例，README 引用或复制）

**保留不动**（不是 "local" 语义）：
- L189/229/280: `--local-provider ollama` 是 Codex CLI flag
- L317: `"pi": "/Users/feir/.local/bin/pi"` 是用户路径（含 `.local/bin`）
- L321/355: `"pi-local"` 是用户自定义 provider 名字

## 不变式

- Claude / Codex / Pi 三个 runner 的构造、行为、测试一字不改
- BaseRunner 抽象接口不变
- Bot.model_aliases / resolve_model_aliases 逻辑不变
- Path Z 在 multi-agent-runner change 已经确立的所有 invariants（empty config → no --model flag、max_ctx==0 → "未知"）对剩余 3 runners 继续成立

## 风险与 Mitigation

| 风险 | Mitigation |
|------|-----------|
| 活跃 LocalHTTP 用户升级后配置报错 | load_config 层 `log.error + sys.exit(1)` 打印单行中文迁移提示（不是 Python traceback）；switch_agent 运行中 `/agent local` 同样返回迁移文案 |
| 迁移文档示例有误，用户按图索骥失败 | **pre-merge 强制**：Captain 在本地跑一次 `codex --oss --local-provider ollama` 对接本地 Ollama，验证通过后才合并 |
| `grep` 找不全所有 "local" 引用 | 完成后 `grep -r "LocalHTTP\|runtime_local\|\"local\"" feishu_bridge/ tests/ README.md` 必须返回 0 行（除迁移文案字符串） |
| 外部 `~/.agents/skills/*/SKILL.md` 残留 `local: unsupported` frontmatter | **保留作惰性元数据**（plan/done/memory-gc 三个 skill 各一条）——bridge runtime 不读取 workflow metadata 的 runner 枚举，只有用户手动 `/<skill>` 时路径触发，此时 `"local"` 已被 load_config/switch_agent 拦截。drift 不造成运行时问题 |

## 测试策略

1. **删除验证**：`grep -r "LocalHTTP\|runtime_local" feishu_bridge/ tests/` 返回 0 行
2. **现有测试无回归**：Claude / Codex / Pi runner 单元测试全部通过
3. **迁移错误用户可见**：
   - `test_migration_error_on_local_agent_type_load_config`：`"type": "local"` 走 load_config → 捕获 `SystemExit(1)` + log 文案含 `"已于 2026-04-19 移除"` 和 `"codex"`
   - `test_migration_error_on_local_agent_type_switch_agent`：bot `switch_agent("local")` 返回 `(False, msg, None)`，msg 含相同迁移关键字
4. **smoke 层回归**：smoke_path_z.py 保留 `[local]` 轮（§4）断言 load_config 友好错误
4. **smoke_path_z.py**：降到 3 轮，全部 PASS

## 不变模块 Proof

执行前 `grep -l "LocalHTTPRunner\|runtime_local" feishu_bridge/` 的输出清单就是本次必须改动的文件。若有文件被遗漏 grep 会把它暴露出来；若有文件在清单外被改动就是超范围。
