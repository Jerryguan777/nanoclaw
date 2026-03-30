# PPI (pi-mono) 适配指南 — NanoClaw 容器模式

本文档提供将 PPI 适配为 NanoClaw Agent 后端的全部信息。新开的 Claude Code 会话只需阅读本文档即可独立完成改写工作。

## 概述

- **PPI**（Python Pi-mono）：AI Agent 框架，代码位于 `/home/jerry/ai/ppi-worktree/ppi/`
- **NanoClaw**：AI Coworker 编排平台，代码位于 `/home/jerry/ai/nanoclaw-worktree/nanoclaw/py/`

NanoClaw 在 Docker 容器中运行 Agent 后端，Orchestrator 与 Agent 之间通过 NATS 6 通道通信。目前仅支持 Claude Code（`agent_runner`）。本次适配将 PPI 作为第二个 Agent 后端接入。

**需要做的事情：**
1. 给 PPI 新增一个 `nanoclaw` 运行模式（与现有的 `interactive`、`print`、`rpc` 并列）
2. 将 NanoClaw 的 IPC 工具（send_message、schedule_task 等）包装为 PPI 的 `AgentTool` 实例
3. 构建 PPI 容器镜像

**PPI 核心不需要改动**（`Agent`、`AgentSession`、内置工具、LLM provider 全部不变）。

---

## 架构

```
NanoClaw Orchestrator（宿主机）
    │
    │  NATS（6 通道）
    │
    ▼
Docker 容器
    │
    ├── Claude Code agent_runner（当前后端）
    │   └── 读 KV、发布结果、MCP 工具
    │
    └── PPI nanoclaw 模式（新后端）← 需要构建的部分
        ├── 从 NATS KV 读取初始输入
        ├── 创建 AgentSession + 工具
        ├── 订阅 Agent 事件 → 发布到 NATS
        ├── 监听 NATS 追加消息
        └── 将 NanoClaw IPC 操作包装为 AgentTool
```

---

## NATS 6 通道协议

容器内的 Agent 通过 NATS 与 Orchestrator 通信。Orchestrator 提供以下环境变量：

- `NATS_URL` — 例如 `nats://host.docker.internal:4222`
- `JOB_ID` — 每次容器调用唯一，例如 `main-a1b2c3d4e5f6`

### 通道 1：初始输入（KV Store，读一次）

**Agent 读取** KV bucket `agent-init`，key = `{JOB_ID}`：

```json
{
  "prompt": "分析 ASIN B09XXX 的广告表现",
  "group_folder": "main",
  "chat_jid": "tg:12345",
  "is_main": true,
  "session_id": "uuid-或-null",
  "is_scheduled_task": false,
  "assistant_name": "Andy",
  "system_prompt": "You are an operations AI...",
  "role_config": {"allowedTools": ["bash", "read"], "mcpServers": {}}
}
```

`system_prompt` 和 `role_config` 是可选字段（可能为 `null`）。

### 通道 2：流式结果（JetStream，Agent → Orchestrator）

**Agent 发布到** subject `agent.{JOB_ID}.results`：

```json
{
  "status": "success",
  "result": "分析结果...",
  "newSessionId": "uuid-用于恢复",
  "error": null
}
```

可发布多条消息（流式输出）。Orchestrator 将每条转发给用户。容器退出前的最后一条是最终结果。

### 通道 3：追加消息 + 关闭信号（Orchestrator → Agent）

**追加消息**：Agent 订阅 JetStream `agent.{JOB_ID}.input`：
```json
{"type": "input", "text": "也帮我看一下 ASIN B08YYY"}
```

**关闭信号**：Agent 处理 Core NATS request `agent.{JOB_ID}.close`：
- Orchestrator 发送 `nc.request("agent.{JOB_ID}.close", b"close")`
- Agent 回复 `b"ack"` 并发起优雅关闭

### 通道 4：Agent 消息（JetStream，Agent → Orchestrator）

**Agent 发布到** `agent.{JOB_ID}.messages`（通过 `send_message` 工具）：

```json
{
  "type": "message",
  "chatJid": "tg:12345",
  "text": "进度更新：发现 3 个表现不佳的广告活动",
  "groupFolder": "main",
  "timestamp": "2026-03-27T10:00:00+00:00",
  "sender": null
}
```

### 通道 5：任务操作（JetStream，Agent → Orchestrator）

**Agent 发布到** `agent.{JOB_ID}.tasks`（通过 MCP 工具）：

```json
{
  "type": "task",
  "operation": "schedule_task",
  "groupFolder": "main",
  "prompt": "每天早上8点检查广告",
  "scheduleType": "cron",
  "scheduleValue": "0 8 * * *",
  "contextMode": "group"
}
```

操作类型：`schedule_task`、`pause_task`、`resume_task`、`cancel_task`、`update_task`、`refresh_groups`、`register_group`。

### 通道 6：快照查询（KV Store，只读）

**Agent 读取** KV bucket `snapshots`：
- Key `{group_folder}.tasks` — 定时任务列表 JSON 数组
- Key `{group_folder}.groups` — 可用群组 JSON 对象

权限：main group 看到全部，非 main group 只看到自己的任务，看不到群组列表。

---

## PPI 架构要点

### 入口

CLI：`pi` → `ppi.coding_agent.main:cli()` → `asyncio.run(main(sys.argv[1:]))`

运行模式在 `main()` 中通过参数选择。我们新增 `--mode nanoclaw`。

### 关键类

**`ppi.agent.Agent`** — 核心 Agent 循环：
```python
agent = Agent(AgentOptions(
    session_id="...",
    stream_fn=stream_simple,    # LLM 流式调用函数
    max_turns=50,
))
agent.set_model(model)
agent.set_thinking_level("high")
agent.set_tools([read_tool, bash_tool, ...])

# 订阅事件（流式输出）
unsub = agent.subscribe(event_listener)

# 发送 prompt
await agent.prompt("分析我的广告")

# 运行中追加消息
agent.follow_up(UserMessage(content=[TextContent(text="也看看...")]))

# 等待完成
await agent.wait_for_idle()

# 中止
agent.abort()
```

**`ppi.coding_agent.core.AgentSession`** — 高层封装：
```python
result = await create_agent_session(CreateAgentSessionOptions(
    cwd="/workspace/group",
    model=model,
    thinking_level="high",
    system_prompt="You are...",
    session_manager=session_manager,  # JSONL 持久化
))
session = result.session
agent = result.agent
```

**`ppi.agent.types.AgentTool`** — 工具接口（ABC）：
```python
class AgentTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def label(self) -> str: ...

    @property
    @abstractmethod
    def description(self) -> str: ...

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]: ...  # JSON Schema

    @abstractmethod
    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, Any],
        signal: asyncio.Event | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult: ...
```

**`ppi.agent.types.AgentToolResult`**：
```python
@dataclass
class AgentToolResult:
    content: list[TextContent | ImageContent]
    details: Any
```

**Agent 事件**（通过 `agent.subscribe()` 发出）：
- `AgentStartEvent`、`AgentEndEvent`
- `MessageStartEvent`、`MessageUpdateEvent`、`MessageEndEvent`
- `ToolExecutionStartEvent`、`ToolExecutionUpdateEvent`、`ToolExecutionEndEvent`
- `TurnStartEvent`、`TurnEndEvent`

### Session 持久化

PPI 使用追加写入的 JSONL 文件，通过 `SessionManager` 管理：
```python
# 创建新 session
sm = SessionManager.create(cwd="/workspace/group", session_dir="/workspace/group/.ppi/sessions")

# 恢复已有 session
sm = SessionManager.open(session_path)

# 继续最近的 session
sm = SessionManager.continue_recent(cwd="/workspace/group")
```

Session 文件保存所有消息、模型切换、compaction 事件。容器挂载确保容器退出后文件仍保留在宿主机上。

---

## 实现方案

### 文件 1：`src/ppi/coding_agent/modes/nanoclaw/nanoclaw_mode.py`（~250 行）

NanoClaw 容器模式的主入口。

```python
"""NanoClaw container mode — runs PPI as an Agent backend in NanoClaw containers."""

import asyncio
import json
import os
from datetime import UTC, datetime

import nats
from nats.js import JetStreamContext

from ppi.agent.types import AgentEvent, AgentMessage, UserMessage, TextContent
from ppi.coding_agent.core.sdk import create_agent_session, CreateAgentSessionOptions
from ppi.coding_agent.core.session_manager import SessionManager


async def run_nanoclaw_mode() -> None:
    """NanoClaw 容器模式主入口。"""
    nats_url = os.environ["NATS_URL"]
    job_id = os.environ["JOB_ID"]

    # 连接 NATS
    nc = await nats.connect(nats_url)
    js = nc.jetstream()

    try:
        # --- 通道 1：从 KV 读取初始输入 ---
        kv_init = await js.key_value("agent-init")
        entry = await kv_init.get(job_id)
        init_data = json.loads(entry.value)

        prompt = init_data["prompt"]
        group_folder = init_data["group_folder"]
        session_id = init_data.get("session_id")
        system_prompt = init_data.get("system_prompt")
        is_main = init_data.get("is_main", False)

        # --- 设置 session ---
        cwd = "/workspace/group"
        session_dir = f"{cwd}/.ppi/sessions"

        if session_id:
            # 恢复已有 session
            session_manager = SessionManager.open(
                session_path=f"{session_dir}/{session_id}.jsonl",
                session_dir=session_dir,
            )
        else:
            session_manager = SessionManager.create(cwd=cwd, session_dir=session_dir)

        # --- 创建 AgentSession ---
        from ppi.coding_agent.modes.nanoclaw.ipc_tools import create_nanoclaw_tools

        nanoclaw_tools = create_nanoclaw_tools(nc, js, job_id, group_folder, is_main)

        result = await create_agent_session(CreateAgentSessionOptions(
            cwd=cwd,
            session_manager=session_manager,
            system_prompt=system_prompt,
            tools=nanoclaw_tools,  # NanoClaw IPC 工具和默认工具一起注册
        ))
        session = result.session
        agent = result.agent

        # --- 通道 2：订阅 Agent 事件 → 发布结果到 NATS ---
        new_session_id: str | None = None

        async def on_agent_event(event: AgentEvent) -> None:
            nonlocal new_session_id
            # 每次 assistant 消息完成时，发布到 NATS
            if hasattr(event, "type") and event.type == "message_end":
                msg = getattr(event, "message", None)
                if msg and hasattr(msg, "content"):
                    text = _extract_text(msg)
                    new_session_id = session_manager.session_id
                    await js.publish(
                        f"agent.{job_id}.results",
                        json.dumps({
                            "status": "success",
                            "result": text,
                            "newSessionId": new_session_id,
                            "error": None,
                        }).encode(),
                    )

        agent.subscribe(on_agent_event)

        # --- 通道 3：监听追加消息 ---
        input_sub = await js.subscribe(f"agent.{job_id}.input")

        async def listen_follow_ups() -> None:
            async for msg in input_sub.messages:
                data = json.loads(msg.data)
                text = data.get("text", "")
                if text:
                    agent.follow_up(UserMessage(
                        content=[TextContent(text=text)]
                    ))
                await msg.ack()

        follow_up_task = asyncio.create_task(listen_follow_ups())

        # --- 通道 3：处理关闭信号 ---
        close_event = asyncio.Event()

        async def handle_close(msg: nats.aio.client.Msg) -> None:
            await msg.respond(b"ack")
            agent.abort()
            close_event.set()

        await nc.subscribe(f"agent.{job_id}.close", cb=handle_close)

        # --- 执行 prompt ---
        await agent.prompt(prompt)
        await agent.wait_for_idle()

        # --- 如果没有流式输出，发布最终结果 ---
        if new_session_id is None:
            new_session_id = session_manager.session_id
            await js.publish(
                f"agent.{job_id}.results",
                json.dumps({
                    "status": "success",
                    "result": None,
                    "newSessionId": new_session_id,
                    "error": None,
                }).encode(),
            )

        # 清理
        follow_up_task.cancel()
        await input_sub.unsubscribe()

    except Exception as e:
        # 发布错误
        await js.publish(
            f"agent.{job_id}.results",
            json.dumps({
                "status": "error",
                "result": None,
                "newSessionId": None,
                "error": str(e),
            }).encode(),
        )
    finally:
        await nc.close()


def _extract_text(msg: AgentMessage) -> str:
    """从 AgentMessage 中提取纯文本。"""
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            getattr(c, "text", "") for c in content
            if hasattr(c, "text")
        )
    return str(content)
```

### 文件 2：`src/ppi/coding_agent/modes/nanoclaw/ipc_tools.py`（~200 行）

将 NanoClaw IPC 操作包装为 PPI `AgentTool` 实例。

```python
"""NanoClaw IPC tools — wrapped as PPI AgentTool for use inside containers."""

import json
from datetime import UTC, datetime
from typing import Any

from nats.aio.client import Client as NatsClient
from nats.js import JetStreamContext

from ppi.agent.types import AgentTool, AgentToolResult
from ppi.ai.types import TextContent


def create_nanoclaw_tools(
    nc: NatsClient,
    js: JetStreamContext,
    job_id: str,
    group_folder: str,
    is_main: bool,
) -> list[AgentTool]:
    """Create all NanoClaw IPC tools for this container session."""
    return [
        SendMessageTool(nc, js, job_id, group_folder),
        ScheduleTaskTool(nc, js, job_id, group_folder),
        PauseTaskTool(nc, js, job_id, group_folder),
        ResumeTaskTool(nc, js, job_id, group_folder),
        CancelTaskTool(nc, js, job_id, group_folder),
        UpdateTaskTool(nc, js, job_id, group_folder),
        GetTasksTool(js, group_folder),
        GetGroupsTool(js, group_folder, is_main),
    ]


class _NatsToolBase(AgentTool):
    """NATS IPC 工具基类。"""

    def __init__(self, nc: NatsClient, js: JetStreamContext, job_id: str, group_folder: str) -> None:
        self._nc = nc
        self._js = js
        self._job_id = job_id
        self._group = group_folder


class SendMessageTool(_NatsToolBase):
    """Send a message to a chat channel."""

    @property
    def name(self) -> str: return "send_message"

    @property
    def label(self) -> str: return "Send Message"

    @property
    def description(self) -> str:
        return "Send a message to a user or group chat. Use this to proactively communicate progress, ask questions, or send notifications."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "chat_jid": {"type": "string", "description": "Target chat JID (e.g. tg:12345)"},
                "text": {"type": "string", "description": "Message text to send"},
            },
            "required": ["chat_jid", "text"],
        }

    async def execute(self, tool_call_id, params, signal=None, on_update=None):
        await self._js.publish(
            f"agent.{self._job_id}.messages",
            json.dumps({
                "type": "message",
                "chatJid": params["chat_jid"],
                "text": params["text"],
                "groupFolder": self._group,
                "timestamp": datetime.now(UTC).isoformat(),
                "sender": None,
            }).encode(),
        )
        return AgentToolResult(content=[TextContent(text="Message sent.")], details=None)


class ScheduleTaskTool(_NatsToolBase):
    """Create a scheduled task."""

    @property
    def name(self) -> str: return "schedule_task"

    @property
    def label(self) -> str: return "Schedule Task"

    @property
    def description(self) -> str:
        return "Create a scheduled task (cron, interval, or one-time). The task will run automatically."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Task prompt"},
                "schedule_type": {"type": "string", "enum": ["cron", "interval", "once"]},
                "schedule_value": {"type": "string", "description": "Cron expression, interval ms, or ISO datetime"},
                "context_mode": {"type": "string", "enum": ["group", "isolated"], "default": "isolated"},
            },
            "required": ["prompt", "schedule_type", "schedule_value"],
        }

    async def execute(self, tool_call_id, params, signal=None, on_update=None):
        await self._js.publish(
            f"agent.{self._job_id}.tasks",
            json.dumps({
                "type": "task",
                "operation": "schedule_task",
                "groupFolder": self._group,
                **params,
            }).encode(),
        )
        return AgentToolResult(content=[TextContent(text="Task scheduled.")], details=None)


# PauseTaskTool, ResumeTaskTool, CancelTaskTool, UpdateTaskTool 遵循相同模式。
# 每个都发布到 agent.{job_id}.tasks，operation 字段不同。
# 参数差异：pause/resume/cancel 需要 task_id；update 需要 task_id + 要修改的字段。


class GetTasksTool(AgentTool):
    """Read scheduled tasks from KV snapshot."""

    def __init__(self, js: JetStreamContext, group_folder: str) -> None:
        self._js = js
        self._group = group_folder

    @property
    def name(self) -> str: return "get_tasks"

    @property
    def label(self) -> str: return "Get Tasks"

    @property
    def description(self) -> str:
        return "List all scheduled tasks for this group."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, tool_call_id, params, signal=None, on_update=None):
        kv = await self._js.key_value("snapshots")
        try:
            entry = await kv.get(f"{self._group}.tasks")
            tasks = json.loads(entry.value)
        except Exception:
            tasks = []
        return AgentToolResult(
            content=[TextContent(text=json.dumps(tasks, indent=2))],
            details=None,
        )


class GetGroupsTool(AgentTool):
    """Read available groups from KV snapshot (main group only)."""

    def __init__(self, js: JetStreamContext, group_folder: str, is_main: bool) -> None:
        self._js = js
        self._group = group_folder
        self._is_main = is_main

    @property
    def name(self) -> str: return "get_groups"

    @property
    def label(self) -> str: return "Get Groups"

    @property
    def description(self) -> str:
        return "List available groups. Only the main group can see all groups."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, tool_call_id, params, signal=None, on_update=None):
        if not self._is_main:
            return AgentToolResult(
                content=[TextContent(text="Permission denied: only main group can list groups.")],
                details=None,
            )
        kv = await self._js.key_value("snapshots")
        try:
            entry = await kv.get(f"{self._group}.groups")
            groups = json.loads(entry.value)
        except Exception:
            groups = {"groups": []}
        return AgentToolResult(
            content=[TextContent(text=json.dumps(groups, indent=2))],
            details=None,
        )
```

### 文件 3：`src/ppi/coding_agent/modes/nanoclaw/__init__.py`

```python
from ppi.coding_agent.modes.nanoclaw.nanoclaw_mode import run_nanoclaw_mode

__all__ = ["run_nanoclaw_mode"]
```

### 文件 4：修改 `src/ppi/coding_agent/main.py`（~5 行）

在模式选择逻辑中添加 `--mode nanoclaw`：

```python
# 在 main() 中，现有模式判断之后添加：
if args.mode == "nanoclaw":
    from ppi.coding_agent.modes.nanoclaw import run_nanoclaw_mode
    await run_nanoclaw_mode()
    return
```

### 文件 5：`container/Dockerfile.ppi`（~30 行）

```dockerfile
FROM python:3.12-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/ /app/src/
COPY pyproject.toml /app/

RUN pip install --no-cache-dir ".[nanoclaw]"

# NanoClaw 模式的默认入口
ENTRYPOINT ["python", "-m", "ppi.coding_agent", "--mode", "nanoclaw"]
```

在 `pyproject.toml` 中添加 `nanoclaw` 可选依赖：
```toml
[project.optional-dependencies]
nanoclaw = ["nats-py>=2.9"]
```

---

## 概念映射：NanoClaw → PPI

| NanoClaw | PPI 对应物 | 说明 |
|----------|-----------|------|
| `AgentInitData.prompt` | `agent.prompt(text)` | 直接映射 |
| `AgentInitData.session_id` | `SessionManager.open(path)` | 通过 session 文件路径恢复 |
| `AgentInitData.system_prompt` | `CreateAgentSessionOptions.system_prompt` | 创建 session 时传入 |
| `AgentInitData.role_config` | `CreateAgentSessionOptions` 各字段 | 映射到 model、tools、thinking_level |
| `AgentOutput.result` | `MessageEndEvent.message.content` | 从 Agent 事件中提取文本 |
| `AgentOutput.new_session_id` | `session_manager.session_id` | JSONL 文件名 = session ID |
| `AgentOutput.status` | 异常处理 | 无异常 = success，捕获异常 = error |
| 追加消息 | `agent.follow_up(UserMessage(...))` | 直接映射 |
| 关闭信号 | `agent.abort()` + 退出 | 直接映射 |
| MCP `send_message` | `SendMessageTool.execute()` | AgentTool 包装 NATS publish |
| MCP `schedule_task` | `ScheduleTaskTool.execute()` | AgentTool 包装 NATS publish |
| 容器 volume `/workspace/group` | `cwd="/workspace/group"` | 工具的工作目录 |
| 容器 volume `.claude/` | 不需要 | PPI 用 `.ppi/sessions/` 替代 |

---

## NanoClaw 容器中的 Session 生命周期

```
容器启动
  ↓
从 KV 读取 AgentInitData（session_id 可能为 null 或已有值）
  ↓
如果 session_id 不为空：
  SessionManager.open("{session_dir}/{session_id}.jsonl")
  → 恢复消息历史、模型、thinking_level
否则：
  SessionManager.create(cwd="/workspace/group")
  → 创建空白 session
  ↓
create_agent_session(options)
  → 返回 (agent, session)
  ↓
agent.prompt(init_data.prompt)
  → Agent 运行，发出事件
  → 每个 MessageEndEvent → 发布到 NATS results
  → 工具执行（bash、read、edit + NanoClaw IPC 工具）
  ↓
await agent.wait_for_idle()
  ↓
发布最终 AgentOutput，new_session_id = session_manager.session_id
  ↓
容器退出
  ↓
Session JSONL 文件保留在宿主机上（通过 volume mount）
  ↓
下次调用：Orchestrator 传入 session_id → session 恢复
```

---

## PPI 容器的 Volume Mounts

| 容器路径 | 宿主机路径 | 模式 | 用途 |
|---------|-----------|------|------|
| `/workspace/group` | `groups/{folder}/` | rw | 工作目录、session 文件 |
| `/workspace/group/.ppi/sessions` | （包含在上面目录中） | rw | PPI session JSONL 文件 |
| `/app/src` | （内置在镜像中） | ro | PPI 源码 |

注意：PPI **不需要** `.claude/` session 目录（那是 Claude Code 专用的）。NanoClaw 中通过 `AgentBackendConfig.skip_claude_session = True` 控制。

---

## 测试策略

1. **IPC 工具单元测试**：Mock NATS client，验证发布的 subject 和 payload 正确
2. **nanoclaw_mode 单元测试**：Mock NATS + Mock Agent，验证生命周期（读 KV → prompt → 发布结果）
3. **集成测试**：启动 NATS，用 mock LLM provider 运行 nanoclaw_mode，验证端到端流程
4. **在 dev 依赖中添加 `nats-py`** 用于测试

---

## 验收清单

- [ ] 创建 `src/ppi/coding_agent/modes/nanoclaw/` 目录
- [ ] `nanoclaw_mode.py` — 读 KV、创建 session、订阅事件、发布结果
- [ ] `ipc_tools.py` — 全部 8 个 NanoClaw IPC 工具包装为 AgentTool
- [ ] `__init__.py` — 导出 `run_nanoclaw_mode`
- [ ] `main.py` — 添加 `--mode nanoclaw` 处理
- [ ] `pyproject.toml` — `nanoclaw` 可选依赖包含 `nats-py`
- [ ] `container/Dockerfile.ppi` — PPI 后端的容器镜像
- [ ] Session 持久化：JSONL 文件位于 `/workspace/group/.ppi/sessions/`
- [ ] 追加消息：NATS 订阅 → `agent.follow_up()`
- [ ] 关闭信号：NATS request-reply → `agent.abort()` + 优雅退出
- [ ] PPI 现有测试全部通过
- [ ] nanoclaw mode 和 IPC 工具的新测试
