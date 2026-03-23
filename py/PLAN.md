# NanoClaw TypeScript → Python 重写计划

## Context

将 NanoClaw TypeScript 单进程应用（约 5,648 行生产代码 + 898 行容器代码）重写为 Python，保留原有架构和功能，采用 Pythonic 最佳实践。所有 Python 代码位于 `py/` 目录下。工作分为 5 个阶段（0–4），每个阶段代码量适中，可在一次 Claude Code session 中完成，因此 **Phase 内不再拆分子任务**。

**分支**: `python-rewrite`
**预估总量**: 约 6,000 行 Python（主进程 + 容器 agent-runner + 测试）

---

## 关键设计决策

### 1. asyncio vs 同步轮询 → **使用 asyncio**
- TS 版用 `setInterval` + `Promise`，Python 用 `asyncio.gather` + `asyncio.sleep` 实现 3 个并行轮询循环
- 容器进程用 `asyncio.create_subprocess_exec()`
- Credential proxy 用 `aiohttp.web`
- SQLite 保持同步（`sqlite3` stdlib），与 TS 版 better-sqlite3 同步模型一致

### 2. 数据模型 → **dataclasses**
- 模型简单（6 个接口），不需要 pydantic
- `@dataclass(frozen=True)` 提供不可变性，`asdict()` 做 JSON 序列化

### 3. Channel 接口 → **Protocol**
- `@runtime_checkable class Channel(Protocol)` 匹配 TS interface 语义
- 可选方法用 Protocol 更自然，不强制继承

### 4. 容器 agent-runner → **改写为 Python**
- Python Claude Agent SDK (`claude-agent-sdk`) 功能等价于 TS 版本
- `query()`, `ClaudeAgentOptions`, `SystemPromptPreset`, hooks, MCP servers 全部支持
- `add_dirs` 对应 TS `additionalDirectories`
- `resumeSessionAt` 无直接对应，可通过 `extra_args={"resume-session-at": uuid}` 传递
- MCP server 可用 Python in-process SDK MCP server（`create_sdk_mcp_server`），比 TS stdio 更简洁
- 容器 Dockerfile 需从 `node:22-slim` 改为 `python:3.12-slim` + 安装 `claude-agent-sdk`

### 5. TS→Python 类型映射

| TS 模式 | Python 等价 |
|---------|-----------|
| `interface X { ... }` | `@dataclass(frozen=True) class X` |
| `interface Channel` | `@runtime_checkable class Channel(Protocol)` |
| `type Callback = (...) => void` | `Callback = Callable[[...], None]` |
| `RegExp` | `re.Pattern[str]` |
| `path.resolve()` | `pathlib.Path.resolve()` |
| `fs.readFileSync()` | `pathlib.Path.read_text()` |
| `Intl.DateTimeFormat` | `datetime` + `zoneinfo` stdlib |
| `Map<string, T>` | `dict[str, T]` |
| pino | structlog |
| `setInterval` / `setTimeout` | `asyncio.create_task` + `asyncio.sleep` |
| `child_process.spawn` | `asyncio.create_subprocess_exec` |
| `http.createServer` | `aiohttp.web` |
| `Promise<T>` | `async def ... -> T` / `Awaitable[T]` |
| `EventStream` / `AsyncIterator` | `AsyncIterator` / `async for` |

---

## 依赖关系图

```
types.py (无依赖)    logger.py (无依赖)    timezone.py (无依赖)    env.py (无依赖)
    │                    │                      │                      │
    ├────────────────────┼──────────────────────┼──────────────────────┤
    │                config.py ← env            │                      │
    │                    │             group_folder.py ← config         │
    │                    │                      │                      │
    │         db.py ← config, group_folder, types                      │
    │                    │                                             │
router.py ← types, tz   │  sender_allowlist.py ← config               │
channels/registry ← types  mount_security.py ← config, types          │
    │                    │  credential_proxy.py ← env                  │
    │                    │  container_runtime.py (无内部依赖)           │
    │                    │                                             │
container_runner.py ← config, runtime, proxy, folder, mount, types     │
group_queue.py ← config                                                │
ipc.py ← db, group_folder, types                                      │
task_scheduler.py ← db, group_queue, container_runner                  │
remote_control.py ← config, db                                        │
    │                                                                  │
main.py ← ALL modules
```

---

## Phase 0: 基础设施

**预估**: ~400 行 | **分支**: `py/infra`

创建项目脚手架，所有模块为 stub（`pass` 或空类），确保工具链跑通。

**范围**:
- `py/pyproject.toml` — Python >=3.12, 依赖: structlog, croniter, aiohttp, claude-agent-sdk
- `py/ruff.toml` — line-length=120, target-version="py312"
- `py/conftest.py` — pytest 全局 fixture
- `py/nanoclaw/` — 包结构（所有 `__init__.py` + `py.typed` + stub 模块）
- `py/tests/` — 测试目录结构 + `conftest.py`
- `py/scripts/check_parity.py` — TS/Python API 对等检查脚本
- `py/CLAUDE.md` — Python 重写指南和约定
- `.github/workflows/python-quality.yml` — CI

**目录结构**:
```
py/
├── pyproject.toml
├── ruff.toml
├── conftest.py
├── CLAUDE.md
├── nanoclaw/
│   ├── __init__.py
│   ├── py.typed
│   ├── types.py / config.py / env.py / logger.py / timezone.py
│   ├── group_folder.py / db.py / router.py
│   ├── channels/ (__init__.py + registry.py)
│   ├── sender_allowlist.py / mount_security.py
│   ├── credential_proxy.py / container_runtime.py / container_runner.py
│   ├── group_queue.py / ipc.py / task_scheduler.py / remote_control.py
│   └── main.py
├── tests/
│   ├── __init__.py / conftest.py / fixtures/
│   └── test_*.py (stubs)
└── scripts/
    └── check_parity.py
```

**验收**: `cd py && uv sync && ruff check . && ruff format --check . && mypy --strict .` 全部通过

---

## Phase 1: 基础模块（无跨模块依赖）

**预估**: ~960 行 Python + ~500 行测试 | **分支**: `py/foundation`

将所有无跨模块依赖（或仅依赖同 phase 内模块）的文件一次性改写。

**TS 源文件 → Python 输出**:

| TS 文件 | 行数 | Python 文件 | 说明 |
|---------|------|------------|------|
| `types.ts` | 107 | `types.py` | dataclass + Protocol |
| `config.ts` | 73 | `config.py` | 模块级常量 + pathlib + re.compile |
| `env.ts` | 42 | `env.py` | .env 解析器 |
| `logger.ts` | 16 | `logger.py` | structlog 配置 |
| `timezone.ts` | 16 | `timezone.py` | datetime + zoneinfo |
| `group-folder.ts` | 44 | `group_folder.py` | re.match + Path.relative_to |
| `router.ts` | 52 | `router.py` | XML 格式化 + 出站路由 |
| `channels/registry.ts` | 28 | `channels/registry.py` | dict 注册表 |
| `channels/index.ts` | 12 | `channels/__init__.py` | barrel import（空，skill 添加时填充） |
| `sender-allowlist.ts` | 128 | `sender_allowlist.py` | dataclass + JSON 加载 |
| `mount-security.ts` | 419 | `mount_security.py` | pathlib + 缓存 + 验证 |
| `container-runtime.ts` | 129 | `container_runtime.py` | subprocess.run + 平台检测 |

**测试文件**: `test_types.py`, `test_config.py`, `test_env.py`, `test_timezone.py`, `test_group_folder.py`, `test_router.py`, `test_channel_registry.py`, `test_sender_allowlist.py`, `test_mount_security.py`, `test_container_runtime.py`

**验收**: `ruff + mypy + pytest --cov-fail-under=80` 通过

---

## Phase 2: 数据层 + 服务层

**预估**: ~1,300 行 Python + ~600 行测试 | **分支**: `py/services`

需要 Phase 1 合并后开始。改写数据库和核心服务模块。

**TS 源文件 → Python 输出**:

| TS 文件 | 行数 | Python 文件 | 设计要点 |
|---------|------|------------|---------|
| `db.ts` | 697 | `db.py` | `sqlite3` stdlib 同步 API，`Database` 类封装连接，contextmanager 支持测试 |
| `credential-proxy.ts` | 125 | `credential_proxy.py` | `aiohttp.web` HTTP 代理，`aiohttp.ClientSession` 转发 |
| `container-runner.ts` | 717 | `container_runner.py` | `asyncio.create_subprocess_exec`，stdout StreamReader 解析 marker |
| `group-queue.ts` | 365 | `group_queue.py` | `asyncio.Lock` 保护状态，`asyncio.create_task` 后台任务，指数退避 |

**注意**: `group_queue.py` 需特别注意——TS 版依赖单线程隐式安全，Python asyncio 虽也单线程但 `await` 点可能被打断，需在关键操作用 `asyncio.Lock` 保护。

**测试文件**: `test_db.py`, `test_credential_proxy.py`, `test_container_runner.py`, `test_group_queue.py`

**验收**: `ruff + mypy + pytest --cov-fail-under=80` 通过

---

## Phase 3: IPC + 调度 + 远程控制 + 主编排器

**预估**: ~1,600 行 Python + ~800 行测试 | **分支**: `py/orchestrator`

需要 Phase 2 合并后开始。改写 IPC 通信、任务调度、主循环。

**TS 源文件 → Python 输出**:

| TS 文件 | 行数 | Python 文件 | 设计要点 |
|---------|------|------------|---------|
| `ipc.ts` | 461 | `ipc.py` | asyncio.Task 轮询 JSON 文件，`croniter` 替代 cron-parser |
| `task-scheduler.ts` | 282 | `task_scheduler.py` | `compute_next_run()` 纯函数，`run_task()` async |
| `remote-control.ts` | 224 | `remote_control.py` | asyncio subprocess detached，URL 轮询 |
| `index.ts` | 669 | `main.py` | `async def main()`, `asyncio.gather()` 三循环，`signal` 优雅关闭 |

**主编排器设计**:
- `AppState` dataclass 封装 `last_timestamp`, `sessions`, `registered_groups` 等
- `asyncio.gather(message_loop(), scheduler_loop(), ipc_watcher())` 并行运行
- `asyncio.Event` 做关闭信号
- `if __name__ == "__main__": asyncio.run(main())`
- `pyproject.toml` 中 `[project.scripts] nanoclaw = "nanoclaw.main:main"`

**测试文件**: `test_ipc.py`, `test_task_scheduler.py`, `test_remote_control.py`, `test_main.py`（集成测试：mock channel → 消息 → 容器 mock → 响应）

**验收**: `ruff + mypy + pytest --cov-fail-under=80` 通过

---

## Phase 4: 容器 Agent Runner + Dockerfile

**预估**: ~900 行 Python + ~300 行测试 | **分支**: `py/container`

需要 Phase 3 合并后开始。改写容器内运行的 agent-runner 和 MCP server。

**TS 源文件 → Python 输出**:

| TS 文件 | 行数 | Python 文件 | 设计要点 |
|---------|------|------------|---------|
| `agent-runner/src/index.ts` | 559 | `container/agent_runner/main.py` | `claude_agent_sdk.query()` + `async for`，`MessageStream` 用 `asyncio.Queue` |
| `agent-runner/src/ipc-mcp-stdio.ts` | 339 | `container/agent_runner/ipc_mcp.py` | `create_sdk_mcp_server()` in-process MCP 或 stdio MCP |
| `container/Dockerfile` | — | `container/Dockerfile` | `python:3.12-slim` 基础镜像，`pip install claude-agent-sdk` |
| `container/build.sh` | — | `container/build.sh` | 更新构建脚本 |

**Agent Runner 设计**:
- `MessageStream` 类用 `asyncio.Queue` + `async def __aiter__` 替代 TS 版 Promise-based 队列
- `run_query()` async 函数，`async for message in query(...)` 流式处理
- `ClaudeAgentOptions(add_dirs=extra_dirs, resume=session_id, ...)` 替代 TS options
- `resumeSessionAt` → `extra_args={"resume-session-at": resume_at}`
- `PreCompact` hook 用 Python `HookMatcher` + async callback
- MCP server 可选两种方式：
  - **方案 A**: `create_sdk_mcp_server()` in-process（更 Pythonic，无 subprocess 开销）
  - **方案 B**: stdio MCP server（与 TS 版一致）
  - 推荐方案 A

**Dockerfile 变更**:
- 基础镜像: `python:3.12-slim`（替代 `node:22-slim`）
- 安装: `pip install claude-agent-sdk`（Claude Code CLI 已内置打包）
- 仍需安装 chromium + 字体（agent-browser 需要）
- 入口: `python -m agent_runner`

**测试文件**: `test_agent_runner.py`, `test_ipc_mcp.py`

**验收**: `ruff + mypy + pytest` 通过 + `docker build` 成功 + 容器内 `python -m agent_runner` 可执行

---

## Python 依赖

```toml
[project]
requires-python = ">=3.12"
dependencies = [
    "structlog>=24.0",
    "croniter>=2.0",
    "aiohttp>=3.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "pytest-cov>=5.0",
    "ruff>=0.8",
    "mypy>=1.13",
]

# Container agent-runner 的依赖（单独 pyproject.toml 或 requirements.txt）
# claude-agent-sdk>=0.1.0
```

## 质量保证

每个 Phase 提交前:
```bash
cd py
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict .
uv run pytest tests/ --cov=nanoclaw --cov-fail-under=80 -v
```

## 合并策略

1. 每个 Phase 一个分支，按顺序合并到 `python-rewrite`
2. 合并命令: `git merge --no-ff py/<branch> -m "merge: [Phase N] <description>"`
3. 合并后运行全量质量检查

## 不重写的部分

- `container/skills/` — 保留原样（SKILL.md 指令文件，运行在容器内）
- Channel 实现 — 通过 skill 分支添加，registry 接口已重写
- `setup/` — 安装脚本单独考虑
