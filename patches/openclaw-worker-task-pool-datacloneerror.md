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
可克隆就原样返回（保留 transferList 语义）；不可克隆才做"带可转移对象保留的
JSON 往返"**（丢掉 Proxy/functions，得到纯数据；MessagePort 等 transferList
里的对象必须原样保留，见下方"回归"）。

编辑 `node_modules/openclaw/dist/worker-task-pool-*.mjs`
（文件名的 hash 段随版本变化；先备份），在 `const WORKER_WARM_WINDOW_MS` 之前插入：

```js
function _ocSanitizeForPostMessage(value, transferList) {
	try {
		structuredClone(value);
		return value;
	} catch {
		return _ocSanitizeViaJson(value, Array.isArray(transferList) ? transferList : [], new Map());
	}
}
function _ocSanitizeViaJson(value, transferList, seen) {
	if (value === null || typeof value !== "object" && typeof value !== "function") return value;
	// 可转移对象（MessagePort 等）必须原样保留：postMessage 靠 transferList 搬运它们。
	// JSON 往返会把 MessagePort 毁成 {}，worker 端 task?.port?.close 就会抛
	// "task?.port?.close is not a function"，gateway 启动卡死（DataCloneError 补丁的回归）
	if (transferList.includes(value) || typeof MessagePort !== "undefined" && value instanceof MessagePort) return value;
	if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) return value;
	if (seen.has(value)) return seen.get(value);
	if (typeof value.toJSON === "function") return _ocSanitizeViaJson(value.toJSON(), transferList, seen);
	if (Array.isArray(value)) {
		const out = [];
		seen.set(value, out);
		for (let i = 0; i < value.length; i++) out[i] = _ocSanitizeViaJson(value[i], transferList, seen) ?? null;
		return out;
	}
	const out = {};
	seen.set(value, out);
	for (const key of Object.keys(value)) {
		const item = _ocSanitizeViaJson(value[key], transferList, seen);
		if (typeof item !== "function" && item !== undefined) out[key] = item;
	}
	return out;
}
```

然后把该文件里两处 `worker.postMessage({ input, ... })` /
`slot.worker.postMessage({ ... input ... })` 中的 `input` 换成
**带 transferList 参数的**消毒调用：

```js
// 1) start() 里
worker.postMessage({ input: _ocSanitizeForPostMessage(input, transferList), taskId: task.id, ...
// 2) receiveExchange() 里
slot.worker.postMessage({ taskId: task.id, responseId: exchange.id, input: _ocSanitizeForPostMessage(response.input, response.transferList) }, response.transferList);
```

改完重启网关（`openclaw gateway restart` 或计划任务重跑）。
验证：`openclaw gateway status` 显示 `Connectivity probe: ok`，且
`openclaw agent -m "回复两个字：正常"` 有回复即生效。

## 回归与二次修复（2026-10-03）

初版补丁的 fallback 是裸 `JSON.parse(JSON.stringify(value))`，会把 **MessagePort
毁成 `{}`**（`JSON.stringify(port) === "{}"`）。`canonical-validation-pool` 任务的
`input` 里带 `port`（配 `transferList: (task) => [task.port]`），于是 worker 端
`task.port` 变成 `{}`，`finally` 里的 `task?.port?.close()` 抛出
`TypeError: task?.port?.close is not a function`，**网关启动即挂**（启动时要做
数据库 canonical 校验，必经这条 worker 通路），稳定包里全是
`WorkerTaskError | task?.port?.close is not a function`（reason: gateway.startup_failed）。

顺带说明：worker 端 `session-accessor.sqlite-archive.worker.js` 里
`task?.port instanceof MessagePort` 检查失败本来会抛更准确的
"Canonical validation task requires its own message port"，但 `finally` 的
`task?.port?.close()` 又抛 TypeError 把原错误**盖掉了**——所以日志里只看到
close 的报错。排查时以它为线索找 `task.port` 被毁的位置即可。

修复：fallback 改成上面的 `_ocSanitizeViaJson`，transferList 里的对象（以及
MessagePort / ArrayBuffer / TypedArray 视图）原样保留，交给 `postMessage` 的
transferList 真正搬运；其余值维持 JSON 往返语义。两处调用都要把各自的
transferList 传进消毒函数。

## 注意

- `openclaw update` 会覆盖 dist 文件，补丁随之丢失。升级后若 bot 又不回消息，
  按上面重打即可（等上游 PR 合入就不用了）。
- 仅动 dist 产物，不动上游源码；请勿向 npm 包目录里的文件提 issue 时附带本补丁
  造成的 diff，直接指向上述上游 issue/PR 即可。
