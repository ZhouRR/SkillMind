---
name: matrix-message
description: 使用已绑定的 HTTP 资源调用 n8n Webhook，向指定 Matrix Room 发送通知、状态、进度或文本消息。
---

# Matrix Message

## 目的与输入

通过已绑定的 n8n HTTP 资源向指定 Matrix Room 发送消息。

- `room_id`：目标 Matrix Room ID，必填。请求正文中的对应字段是 `roomId`。
- `message`：消息正文，必填，保留原始换行和字符。
- `sender`：消息中显示的发送者，选填；未指定时使用 `@zhourr:192.170.10.66`。

只询问缺少的必填输入。服务地址和认证信息由 HTTP 资源提供，不要求用户重复输入。

## HTTP 请求

使用运行环境提供的 HTTP 请求能力和已绑定的资源，按其工具说明构造调用。
资源基址使用服务的 origin，请求路径为：

`/n8n/webhook/jaf-progress-7d3c8f6b3fdd4cb3`

请求方式为 `POST`，请求头为 `Content-Type: application/json`。正文是 JSON 对象：

| 字段 | 值 |
| --- | --- |
| `roomId` | `room_id` 的值 |
| `sender` | 指定的 `sender`，或上述默认值 |
| `status` | `message` 的原文 |

通过 JSON 序列化构造正文，正确转义换行、引号、中文和日文；不拼接 JSON 字符串。
若调用能力使用文件作为请求输入，保存为 UTF-8 JSON 文件后按工具契约提交。

## 执行结果

按当前资源权限和审批方式发送请求，保存或引用实际响应与原操作回执。
汇总目标房间、发送者和请求结果。只有响应或回执明确确认 Matrix 消息发送成功时，才报告发送成功；
仅确认 Webhook 已受理时，报告已受理，不推断消息已经送达。

请求超时、连接中断或结果不明时，报告已知事实，不自动重复发送。
根据本次请求的 HTTP 状态码和响应正文报告结果，不额外要求 GET 回读或虚构确认接口。
