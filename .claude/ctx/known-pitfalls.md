## Known Pitfalls

- 飞书群聊中，bot 只会收到 `@mention` 自己的消息；不要假设所有群消息都会投递。
- bot 发件人的 WebSocket `sender_id` 可能为空；识别 bot 身份要在 worker 层调用 REST API。
- WebSocket SDK 事件回调应保持零 I/O；网络请求应下沉到 worker。
- CardKit `card_element.content()` 只改文字，不会清除已有 `icon`。
- `stream-json` 的 assistant 事件里 `output_tokens` 只是 message-start 快照；最终输出 token 统计要看 result 事件。
- README 声明必须对照代码验证，不能依赖历史记忆。
