## Known Pitfalls

- 飞书群聊中，bot 只会收到 `@mention` 自己的消息；不要假设所有群消息都会投递。
- bot 发件人的 WebSocket `sender_id` 可能为空；识别 bot 身份要在 worker 层调用 REST API。
- WebSocket SDK 事件回调应保持零 I/O；网络请求应下沉到 worker。
- CardKit `card_element.content()` 只改文字，不会清除已有 `icon`。
- `stream-json` 的 assistant 事件里 `output_tokens` 只是 message-start 快照；最终输出 token 统计要看 result 事件。
- README 声明必须对照代码验证，不能依赖历史记忆。
- **fill_defaults=True normalize 后 setdefault 是 no-op，默认值须在 normalize 内部分型**：配置 normalize 函数带 fill_defaults=True 时会填满所有 key 的默认值；后续对同一 dict 调用 setdefault 找到已存在的 key 直接返回，无法覆盖。防错：需要按上下文类型选不同默认值时，在 normalize 函数内部通过 type 参数切换 base dict，不要在 normalize 外部再做 setdefault
- **hot-swap 路径从 materialized 状态 re-normalize 会继承旧默认值**：load_config 把 prompt 填充为类型 A 的默认值后，switch_agent 切到类型 B 从已 materialized 的 dict re-normalize，B 的 normalize 把每个已存在 key 当显式输入处理，默认值完全不生效。防错：load_config 时备份原始用户 prompt 到 _prompt_raw（copy 在 normalize 之前）；switch 路径从 _prompt_raw 而非 materialized dict 重新 normalize
- **SSE generator 中 blanket except 吞掉 cancel/transport 信号，cancel 误报为空文本错误**：在 generator 里用 except Exception 防御，cancel() 调用 response.close() 产生的异常被吞掉变成 EOF，streaming loop 跑完后 cancelled=False，落到「未返回内容」错误分支。防错：streaming generator 只处理自身协议解析错误（StopIteration），传输层异常（socket.timeout, OSError）向上穿透；_do_request 顶层通过 call.is_cancelled() 仲裁 cancel vs transport error
- **超时错误文案引用错误常量导致数量级误报**：socket_timeout 和 wall_clock_timeout 是两套独立计时器；error handler 格式化时引用了 wall clock 常量（7200），而实际触发 socket.timeout 的是 socket read cap（2s），导致 4.1s 实际体验被显示为 '7200s wall clock'。防错：except 子句内 format string 必须引用触发该 except 的计时器变量；代码审查时逐一核查 timeout 相关 except 块内的格式化参数是否匹配异常语义
- **sibling if-block 变量作用域陷阱：在 if-block 之前初始化变量**：变量在第一个 if-block 赋值，在 sibling if-block 引用时，若第一个条件为 false 触发 UnboundLocalError。linter/mypy 不报告此问题，测试夹具通常保持第一条件为 true 掩盖 bug。防错：任何在 if-block 内赋值、在 block 外或 sibling block 引用的变量，必须在 if-blocks 之前初始化为默认值
- **never-raises 热路径函数必须 catch Exception，不能只 catch 特定子类**：文档明确 never-raises 但只 catch sqlite3.Error，上游数据形状错误（usage.get() on None 的 AttributeError）在特定 except 层之前逃逸，破坏合约。防错：热路径 never-raises 函数必须 except Exception 并 log 详情；不用特定子类
- **Claude CLI stream 事件 contextWindow 字段对 Opus/Sonnet 报告过时 200K 值**：modelUsage[m].contextWindow 在 Claude CLI 流式输出中，对 Opus 4.7 / Sonnet 4.6 固定报告 200_000，即使模型实际以 1M 上下文运行。以 stream 字段作分母会将 context 使用率虚增 ~5 倍，频繁误报 /new 建议。防错：对 opus/sonnet 家族忽略 stream 的 contextWindow，改用推断表（1M）；只对 haiku/gpt/local 等家族信任 stream 报告值
- **批量 docstring 清理任务需 grep 全文件，任务行号仅作定位参考**：tasks.md 任务指定了特定行号的 docstring 清理，但同一文件在另一行也有相同 token，导致任务描述行号已处理但文件仍含遗留项。防错：执行 docstring/comment 清理任务后立即 grep 目标文件全文确认 0 残留；行号只用于定位上下文
- **Spec-Check result 字段必须用 PASS/WARN/BLOCK/WAIVED，禁止自定义枚举值**：在 tasks.md Spec-Check 节写入 'PASS-WITH-EDITS' 是自创枚举值，spec-archive-validate.py 白名单只接受四个标准值，导致归档 pipeline 阻断。防错：写 spec-check result 时只用 PASS/WARN/BLOCK/WAIVED；WARN 用于'有 findings 但已修复'，不发明中间状态
