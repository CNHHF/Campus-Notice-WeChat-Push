# OpenClaw Windows 补丁：WorkerTaskPool `DataCloneError`

> 适用：openclaw 2026.9.x（Windows）。上游修复合入后可删除本补丁。

## 症状

Windows 上微信 bot **收到消息但从不回复**，网关日志里每条入站消息都报：

```
dispatchReplyFromConfig: error ... WorkerTaskError: DataCloneError: #<Object> could not be cloned.
```

定时任务（cron `agentTurn`）同样秒挂。纯推送链路（`openclaw message send`）不受影响。

## 根因

`cloneEnvWithPlatformSemantics`（dist 里的 `config-env-vars`）在 win32 上返回
`new Proxy(cloned, { ...大小写不敏感 handlers })`。Proxy 无法通过 worker 的
structured-clone 边界，`WorkerTaskPoolCore.start()` 里的
`worker.postMessage({ input, ... })` 直接抛 `DataCloneError`。

上游 issue：[openclaw/openclaw#157986](https://github.com/openclaw/openclaw/issues/157986)、
[#158788](https://github.com/openclaw/openclaw/issues/158788)，
修复 PR：[#158789](https://github.com/openclaw/openclaw/pull/158789)（未合入时用本补丁）。

## 修法

给所有 `worker.postMessage(input)` 的入参加一层消毒：**先试 `structuredClone`，
可克隆就原样返回（保留 transferList 语义）；不可克隆才做 JSON 往返**（丢掉
Proxy/functions，得到纯数据）。

编辑 `node_modules/openclaw/dist/worker-task-pool-*.mjs`
（文件名的 hash 段随版本变化；先备份），在 `const WORKER_WARM_WINDOW_MS` 之前插入：

```js
function _ocSanitizeForPostMessage(value) {
	try {
		structuredClone(value);
		return value;
	} catch {
		return JSON.parse(JSON.stringify(value));
	}
}
```

然后把该文件里两处 `worker.postMessage({ input, ... })` /
`slot.worker.postMessage({ ... input ... })` 中的 `input` 换成
`_ocSanitizeForPostMessage(input)`：

```js
// 1) start() 里
worker.postMessage({ input: _ocSanitizeForPostMessage(input), taskId: task.id, ...
// 2) receiveExchange() 里
slot.worker.postMessage({ taskId: task.id, responseId: exchange.id, input: _ocSanitizeForPostMessage(response.input) }, response.transferList);
```

改完重启网关（`openclaw gateway restart` 或计划任务重跑）。
验证：`openclaw agent -m "回复两个字：正常"` 有回复即生效。

## 注意

- `openclaw update` 会覆盖 dist 文件，补丁随之丢失。升级后若 bot 又不回消息，
  按上面重打即可（等上游 PR 合入就不用了）。
- 仅动 dist 产物，不动上游源码；请勿向 npm 包目录里的文件提 issue 时附带本补丁
  造成的 diff，直接指向上述上游 issue/PR 即可。
