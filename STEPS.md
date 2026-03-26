# AI Coworker 平台 — 基础实施步骤

将 NanoClaw Python 重写版改造为通用 AI Coworker 平台的前 4 个基础步骤。

每个步骤包含完整的 GitHub Issue 内容和创建命令。每个 Issue 自包含，新开的 Claude Code 会话只需读取 Issue 即可独立完成工作。

---

## 执行顺序

```
Step 1（文件重组）──→ Step 2（NATS IPC）
                  ├→ Step 3（AgentExecutor）
                  └→ Step 4（Docker API）
```

Step 1 必须先完成。Step 2/3/4 改动不同模块，Step 1 完成后可并行开展，按上述顺序执行可最小化合并冲突。

---

## 前置准备：创建 GitHub Labels

Issue 用到了 `refactor` 和 `python-rewrite` 两个自定义 label，需要先创建（仅需执行一次）：

```bash
gh label create "refactor" --description "Code restructuring without behavior change" --color "c5def5"
gh label create "python-rewrite" --description "Python rewrite of NanoClaw" --color "f9d0c4"
```

---

## Step 1：采用 src layout 重组项目结构

**目标**：将项目从扁平布局改为 Python 标准 src layout，源码分层子包，为多租户、多 coworker 架构做准备。

**关键改动**：
- 采用 **src layout**（PyPA 推荐），所有源码放入 `src/`
- `container/agent_runner/` 移入 `src/agent_runner/`（独立 Python 包）
- `nanoclaw/` 内部按职责分层为 7 个子包
- `groups/` 运行时数据移出源码树（加入 `.gitignore`）
- 删除根 `conftest.py`（src layout 不需要 sys.path hack）
- `tests/` 保持在项目根，内部按子包镜像组织

### 创建 Issue 命令

```bash
gh issue create \
  --title "refactor: adopt src layout and reorganize into layered sub-packages" \
  --label "refactor,python-rewrite" \
  --body "$(cat <<'ISSUE_EOF'
## Context

The \`py/\` directory (project root) contains the Python rewrite of NanoClaw. The current layout has several issues:

1. **No src layout** — \`nanoclaw/\` package sits directly in project root (flat layout), which can cause import ambiguity when running tests or scripts from the project root
2. **All modules flat** under \`nanoclaw/\` — no sub-package organization, hard to reason about dependency direction
3. **\`container/agent_runner/\`** is a Python package sitting outside the source tree
4. **\`groups/\`** runtime data mixed into source tree
5. **Root \`conftest.py\`** is a sys.path hack that src layout eliminates

This is **Step 1** of the AI Coworker Platform foundation. See \`STEPS.md\` for the full plan.

## Goal

Adopt Python standard **src layout** (recommended by PyPA, Hatch, uv) and restructure \`nanoclaw/\` into layered sub-packages. **No logic changes** — pure file moves, renames, and import path updates.

## Current Structure

\`\`\`
py/                              # <-- project root
├── pyproject.toml
├── uv.lock
├── ruff.toml
├── CLAUDE.md
├── PLAN.md
├── conftest.py                  # sys.path hack (to be removed)
├── nanoclaw/                    # flat layout, directly in root
│   ├── __init__.py
│   ├── py.typed
│   ├── types.py
│   ├── config.py
│   ├── env.py
│   ├── logger.py
│   ├── timezone.py
│   ├── group_folder.py
│   ├── db.py                    # 834 lines, SQLite
│   ├── router.py
│   ├── container_runtime.py     # 117 lines, subprocess docker calls
│   ├── credential_proxy.py      # 145 lines, aiohttp proxy
│   ├── container_runner.py      # 794 lines, container spawning
│   ├── group_queue.py           # 326 lines, concurrency control
│   ├── ipc.py                   # 393 lines, file-based IPC
│   ├── task_scheduler.py        # 309 lines
│   ├── remote_control.py        # 250 lines
│   ├── sender_allowlist.py      # 120 lines
│   ├── mount_security.py        # 305 lines
│   ├── main.py                  # 705 lines, entry point
│   └── channels/
│       ├── __init__.py
│       ├── registry.py
│       ├── telegram.py
│       └── slack.py
├── container/                   # agent runner outside source tree
│   ├── Dockerfile
│   ├── build.sh
│   └── agent_runner/
│       ├── __init__.py
│       ├── __main__.py
│       ├── main.py
│       └── ipc_mcp.py
├── groups/                      # runtime data in source tree
│   ├── slack_all-rolemesh/
│   └── telegram_group/
├── tests/
│   ├── conftest.py
│   ├── __init__.py
│   └── test_*.py (20+ files)
└── scripts/
    └── check_parity.py
\`\`\`

## Target Structure

\`\`\`
py/                                  # project root
├── pyproject.toml                   # updated: src layout config
├── uv.lock
├── ruff.toml                        # updated: src path
├── CLAUDE.md
├── PLAN.md
│
├── src/                             # ALL source code under src/
│   ├── nanoclaw/                    # main package (host process)
│   │   ├── __init__.py
│   │   ├── py.typed
│   │   ├── core/                    # foundation layer (zero cross-module deps)
│   │   │   ├── __init__.py
│   │   │   ├── types.py
│   │   │   ├── config.py
│   │   │   ├── env.py
│   │   │   ├── logger.py
│   │   │   ├── timezone.py
│   │   │   └── group_folder.py
│   │   ├── db/                      # data layer
│   │   │   ├── __init__.py
│   │   │   └── sqlite.py
│   │   ├── channels/                # channel layer (internal structure unchanged)
│   │   │   ├── __init__.py
│   │   │   ├── registry.py
│   │   │   ├── telegram.py
│   │   │   └── slack.py
│   │   ├── security/                # security layer
│   │   │   ├── __init__.py
│   │   │   ├── sender_allowlist.py
│   │   │   ├── mount_security.py
│   │   │   └── credential_proxy.py
│   │   ├── container/               # container management layer
│   │   │   ├── __init__.py
│   │   │   ├── runtime.py
│   │   │   ├── runner.py
│   │   │   └── scheduler.py
│   │   ├── ipc/                     # IPC layer
│   │   │   ├── __init__.py
│   │   │   └── file_transport.py
│   │   ├── agent/                   # agent execution layer (placeholder for Step 3)
│   │   │   └── __init__.py
│   │   ├── orchestration/           # orchestration layer
│   │   │   ├── __init__.py
│   │   │   ├── router.py
│   │   │   ├── task_scheduler.py
│   │   │   └── remote_control.py
│   │   └── main.py                  # entry point
│   │
│   └── agent_runner/                # container agent (separate package)
│       ├── __init__.py
│       ├── __main__.py
│       ├── main.py
│       └── ipc_mcp.py
│
├── tests/                           # tests at project root
│   ├── conftest.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── test_types.py
│   │   ├── test_config.py
│   │   ├── test_env.py
│   │   ├── test_timezone.py
│   │   └── test_group_folder.py
│   ├── db/
│   │   ├── __init__.py
│   │   └── test_sqlite.py
│   ├── channels/
│   │   ├── __init__.py
│   │   ├── test_channels.py
│   │   └── test_channel_registry.py
│   ├── security/
│   │   ├── __init__.py
│   │   ├── test_sender_allowlist.py
│   │   ├── test_mount_security.py
│   │   └── test_credential_proxy.py
│   ├── container/
│   │   ├── __init__.py
│   │   ├── test_runtime.py
│   │   ├── test_runner.py
│   │   └── test_scheduler.py
│   ├── ipc/
│   │   ├── __init__.py
│   │   └── test_file_transport.py
│   ├── orchestration/
│   │   ├── __init__.py
│   │   ├── test_router.py
│   │   └── test_task_scheduler.py
│   ├── test_e2e.py
│   └── test_remote_control.py
│
├── container/                       # Docker build context (NOT a Python package)
│   ├── Dockerfile
│   └── build.sh
│
└── scripts/
    └── check_parity.py
\`\`\`

## Tasks

### 1. Create \`src/\` directory and move packages

\`\`\`bash
mkdir -p src
git mv nanoclaw src/nanoclaw
git mv container/agent_runner src/agent_runner
\`\`\`

### 2. Create sub-package directories inside \`src/nanoclaw/\`

Create the following directories with \`__init__.py\`:
- \`src/nanoclaw/core/\`
- \`src/nanoclaw/db/\`
- \`src/nanoclaw/security/\`
- \`src/nanoclaw/container/\`
- \`src/nanoclaw/ipc/\`
- \`src/nanoclaw/agent/\`
- \`src/nanoclaw/orchestration/\`

### 3. Move and rename files within \`src/nanoclaw/\` (git mv)

| Source | Destination | Notes |
|--------|------------|-------|
| \`src/nanoclaw/types.py\` | \`src/nanoclaw/core/types.py\` | |
| \`src/nanoclaw/config.py\` | \`src/nanoclaw/core/config.py\` | |
| \`src/nanoclaw/env.py\` | \`src/nanoclaw/core/env.py\` | |
| \`src/nanoclaw/logger.py\` | \`src/nanoclaw/core/logger.py\` | |
| \`src/nanoclaw/timezone.py\` | \`src/nanoclaw/core/timezone.py\` | |
| \`src/nanoclaw/group_folder.py\` | \`src/nanoclaw/core/group_folder.py\` | |
| \`src/nanoclaw/db.py\` | \`src/nanoclaw/db/sqlite.py\` | **Rename** |
| \`src/nanoclaw/sender_allowlist.py\` | \`src/nanoclaw/security/sender_allowlist.py\` | |
| \`src/nanoclaw/mount_security.py\` | \`src/nanoclaw/security/mount_security.py\` | |
| \`src/nanoclaw/credential_proxy.py\` | \`src/nanoclaw/security/credential_proxy.py\` | |
| \`src/nanoclaw/container_runtime.py\` | \`src/nanoclaw/container/runtime.py\` | **Rename** |
| \`src/nanoclaw/container_runner.py\` | \`src/nanoclaw/container/runner.py\` | **Rename** |
| \`src/nanoclaw/group_queue.py\` | \`src/nanoclaw/container/scheduler.py\` | **Rename** |
| \`src/nanoclaw/ipc.py\` | \`src/nanoclaw/ipc/file_transport.py\` | **Rename** |
| \`src/nanoclaw/router.py\` | \`src/nanoclaw/orchestration/router.py\` | |
| \`src/nanoclaw/task_scheduler.py\` | \`src/nanoclaw/orchestration/task_scheduler.py\` | |
| \`src/nanoclaw/remote_control.py\` | \`src/nanoclaw/orchestration/remote_control.py\` | |

### 4. Clean up root

\`\`\`bash
# Remove sys.path hack conftest — no longer needed with src layout
git rm conftest.py

# Remove runtime data from source tree
echo "groups/" >> .gitignore
git rm -r --cached groups/   # keep files on disk, remove from git
\`\`\`

### 5. Update all internal imports

Every \`from nanoclaw.xxx import ...\` must be updated to new paths. This affects files in \`src/nanoclaw/\`, \`src/agent_runner/\`, and \`tests/\`.

Key import patterns to update:
\`\`\`python
# Before:
from nanoclaw.types import RegisteredGroup
from nanoclaw.config import GROUPS_DIR
from nanoclaw import db
from nanoclaw.container_runner import run_container_agent
from nanoclaw.group_queue import GroupQueue
from nanoclaw.ipc import start_ipc_watcher

# After:
from nanoclaw.core.types import RegisteredGroup
from nanoclaw.core.config import GROUPS_DIR
from nanoclaw.db import sqlite as db  # or from nanoclaw.db.sqlite import ...
from nanoclaw.container.runner import run_container_agent
from nanoclaw.container.scheduler import GroupQueue  # class name stays for now
from nanoclaw.ipc.file_transport import start_ipc_watcher
\`\`\`

### 6. Write \`__init__.py\` re-exports

Each sub-package \`__init__.py\` should re-export its public API for convenience:

\`\`\`python
# src/nanoclaw/core/__init__.py
from nanoclaw.core.types import *  # noqa: F401,F403
from nanoclaw.core.config import *  # noqa: F401,F403
# etc.

# src/nanoclaw/db/__init__.py
from nanoclaw.db.sqlite import (
    init_database,
    store_message,
    get_all_registered_groups,
    # ... all public functions
)
\`\`\`

### 7. Reorganize tests to mirror source structure

Move test files into subdirectories matching source packages:

| Source Test | Destination |
|------------|-------------|
| \`tests/test_types.py\` | \`tests/core/test_types.py\` |
| \`tests/test_config.py\` | \`tests/core/test_config.py\` |
| \`tests/test_env.py\` | \`tests/core/test_env.py\` |
| \`tests/test_timezone.py\` | \`tests/core/test_timezone.py\` |
| \`tests/test_group_folder.py\` | \`tests/core/test_group_folder.py\` |
| \`tests/test_db.py\` | \`tests/db/test_sqlite.py\` |
| \`tests/test_channels.py\` | \`tests/channels/test_channels.py\` |
| \`tests/test_channel_registry.py\` | \`tests/channels/test_channel_registry.py\` |
| \`tests/test_sender_allowlist.py\` | \`tests/security/test_sender_allowlist.py\` |
| \`tests/test_mount_security.py\` | \`tests/security/test_mount_security.py\` |
| \`tests/test_credential_proxy.py\` | \`tests/security/test_credential_proxy.py\` |
| \`tests/test_container_runtime.py\` | \`tests/container/test_runtime.py\` |
| \`tests/test_container_runner.py\` | \`tests/container/test_runner.py\` |
| \`tests/test_group_queue.py\` | \`tests/container/test_scheduler.py\` |
| \`tests/test_ipc.py\` | \`tests/ipc/test_file_transport.py\` |
| \`tests/test_router.py\` | \`tests/orchestration/test_router.py\` |
| \`tests/test_task_scheduler.py\` | \`tests/orchestration/test_task_scheduler.py\` |
| \`tests/test_remote_control.py\` | \`tests/orchestration/test_remote_control.py\` |
| \`tests/test_placeholder.py\` | **Delete** (no longer needed) |
| \`tests/test_e2e.py\` | \`tests/test_e2e.py\` (stays) |

Add \`__init__.py\` to each test subdirectory. Remove \`tests/__init__.py\` from root tests dir (pytest discovers without it; having it can cause issues with src layout).

### 8. Update pyproject.toml for src layout

\`\`\`toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/nanoclaw", "src/agent_runner"]

[project.scripts]
nanoclaw = "nanoclaw.main:main_sync"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.mypy]
python_version = "3.12"
strict = true
mypy_path = "src"
# ... rest of mypy config unchanged

[tool.coverage.run]
source_pkgs = ["nanoclaw"]
omit = [
    "src/nanoclaw/container/runner.py",
    "src/nanoclaw/security/credential_proxy.py",
    "src/nanoclaw/main.py",
    "src/nanoclaw/ipc/*",
    "src/nanoclaw/orchestration/task_scheduler.py",
    "src/nanoclaw/orchestration/remote_control.py",
]

[tool.coverage.report]
fail_under = 80
\`\`\`

### 9. Update ruff.toml

\`\`\`toml
[lint.isort]
known-first-party = ["nanoclaw", "agent_runner"]
\`\`\`

### 10. Update container/Dockerfile

The Dockerfile copies \`agent_runner/\` source — update the COPY path:
\`\`\`dockerfile
# Before: COPY agent_runner/ /app/agent_runner/
# After:  COPY src/agent_runner/ /app/agent_runner/
\`\`\`

Review \`container/build.sh\` for similar path references.

### 11. Re-install in editable mode and verify

\`\`\`bash
# Re-install (src layout requires editable install for development)
uv pip install -e ".[dev]"

# Full quality gate
ruff check .
ruff format --check .
mypy --strict src/nanoclaw
pytest --cov --cov-fail-under=80

# Verify no circular imports
python -c "import nanoclaw; print('OK')"
python -c "import agent_runner; print('OK')"
\`\`\`

## Acceptance Criteria

- [ ] \`src/\` directory exists with \`nanoclaw/\` and \`agent_runner/\` packages inside
- [ ] No Python source files remain in project root (except scripts/)
- [ ] Root \`conftest.py\` removed (sys.path hack no longer needed)
- [ ] \`groups/\` added to \`.gitignore\` and removed from git tracking
- [ ] \`nanoclaw/\` has 7 sub-packages: core, db, channels, security, container, ipc, orchestration (+ agent placeholder)
- [ ] No Python files remain in \`src/nanoclaw/\` root except \`__init__.py\`, \`py.typed\`, and \`main.py\`
- [ ] Renamed files: \`group_queue.py\` -> \`container/scheduler.py\`, \`container_runtime.py\` -> \`container/runtime.py\`, \`container_runner.py\` -> \`container/runner.py\`, \`ipc.py\` -> \`ipc/file_transport.py\`, \`db.py\` -> \`db/sqlite.py\`
- [ ] \`pyproject.toml\` updated: \`[tool.hatch.build.targets.wheel] packages\`, \`mypy_path = "src"\`, \`source_pkgs\`
- [ ] \`ruff check .\` passes with zero errors
- [ ] \`ruff format --check .\` passes
- [ ] \`mypy --strict src/nanoclaw\` passes
- [ ] \`pytest\` passes — all existing tests green
- [ ] \`pytest --cov --cov-fail-under=80\` passes
- [ ] \`git log --follow\` works for moved files (used \`git mv\`)
- [ ] Test directory structure mirrors source package structure
- [ ] No circular imports (\`python -c "import nanoclaw"\` succeeds)
- [ ] \`container/Dockerfile\` and \`container/build.sh\` updated for new paths
- [ ] \`uv pip install -e ".[dev]"\` succeeds

## Important Notes

- **Working directory**: Project root is \`py/\` directory
- **No logic changes**: This is purely a structural refactoring
- **Use \`git mv\`**: Preserve file history
- **Python 3.12+**: Use modern import syntax
- **src layout**: After this change, you MUST run \`uv pip install -e .\` (or \`pip install -e .\`) for imports to work. This is by design — it prevents accidental import of uninstalled local code.
- **Branch**: Create from \`python-rewrite\` branch
- **\`agent_runner\` is a separate package** in \`src/\`, not a sub-package of \`nanoclaw\`. It runs inside containers and has its own dependencies. It imports from \`nanoclaw\` but \`nanoclaw\` does NOT import from it.
ISSUE_EOF
)"
```

### 启动 Claude Code 执行

Issue 创建后，记下 Issue 编号（如 `#1`），新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view 1 --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
完成后提交代码。
```

> 将 `1` 替换为实际 Issue 编号。

---

## Step 2：用 NATS 完整替换所有 IPC 通道

**目标**：用 NATS 完整替换 Orchestrator 与 Agent 之间的全部 6 个通信通道，包括 stdin/stdout。不保留文件 IPC 回退。

**前置条件**：Step 1 完成后，IPC 代码位于 `src/nanoclaw/ipc/file_transport.py`。

### 当前 6 个 IPC 通道

| # | 方向 | 当前实现 | 用途 |
|---|------|---------|------|
| 1 | Orch → Agent | stdin JSON | 初始 prompt + config |
| 2 | Agent → Orch | stdout markers (`---NANOCLAW_OUTPUT_START---`) | 流式结果输出 |
| 3 | Orch → Agent | `input/*.json` + `_close` 哨兵 | 空闲容器追加消息、关闭信号 |
| 4 | Agent → Orch | `messages/*.json` | Agent 主动发消息给用户 |
| 5 | Agent → Orch | `tasks/*.json` | 创建/管理定时任务 |
| 6 | Orch → Agent | `current_tasks.json` / `available_groups.json` | 只读快照查询 |

### 目标设计：NATS 替换全部 6 通道

统一使用 NATS，本地和分布式共用同一套代码。本地开发跑一个单节点 NATS Server（单二进制，零配置）。

根据语义选择 3 种 NATS 原语：

| NATS 原语 | 用于通道 | 原因 |
|-----------|---------|------|
| **KV Store** | 1（初始输入）、6（快照） | 点读语义，最新值覆盖，不需要历史 |
| **JetStream** | 2（结果流）、3（追加消息）、4（消息回传）、5（任务控制） | 有序、持久、支持 ack |
| **Core request-reply** | 3（关闭信号） | 需要确认送达 |

Subject 和 Key 命名（用 `jobId` 而非 `group`，支持同 coworker 多容器并发）：

```
# JetStream Streams
agent.{jobId}.results        # 通道2：流式结果
agent.{jobId}.input          # 通道3：追加消息
agent.{jobId}.messages       # 通道4：消息回传
agent.{jobId}.tasks          # 通道5：任务控制

# KV Store
agent-init.{jobId}           # 通道1：初始 prompt + config
snapshots.{group}.tasks      # 通道6：任务快照
snapshots.{group}.groups     # 通道6：群组快照

# Core NATS request-reply
agent.{jobId}.close          # 通道3：关闭信号（确认送达）
```

### 创建 Issue 命令

```bash
gh issue create \
  --title "feat: replace all IPC channels (stdin/stdout/files) with NATS" \
  --label "enhancement,python-rewrite" \
  --body "$(cat <<'ISSUE_EOF'
## Context

NanoClaw currently uses 3 different mechanisms for Orchestrator-Agent communication: stdin JSON, stdout marker parsing, and file-based IPC (polling JSON files). This is fragile, high-overhead, and not Kubernetes-friendly.

This is **Step 2** of the AI Coworker Platform foundation. See \`STEPS.md\` for the full plan.

**Prerequisite**: Step 1 (src layout reorg) should be completed first.

## Goal

Replace **all 6 IPC channels** between Orchestrator and Agent with NATS. No file-based IPC fallback — clean cut.

## Current 6 IPC Channels

| # | Direction | Current impl | Purpose |
|---|-----------|-------------|---------|
| 1 | Orch -> Agent | stdin JSON | Initial prompt + config |
| 2 | Agent -> Orch | stdout markers (\`---NANOCLAW_OUTPUT_START---\`) | Streaming result output |
| 3 | Orch -> Agent | \`input/*.json\` + \`_close\` sentinel | Follow-up messages to idle container, close signal |
| 4 | Agent -> Orch | \`messages/*.json\` | Agent sends messages to users |
| 5 | Agent -> Orch | \`tasks/*.json\` | Create/manage scheduled tasks |
| 6 | Orch -> Agent | \`current_tasks.json\` / \`available_groups.json\` | Read-only snapshot queries |

## Target Design: NATS Replaces All 6 Channels

Use 3 NATS primitives based on semantics:

| NATS Primitive | Channels | Why |
|---------------|----------|-----|
| **KV Store** | 1 (initial input), 6 (snapshots) | Point-read semantics, latest-value-wins, no history needed |
| **JetStream** | 2 (results), 3 (follow-up msgs), 4 (messages), 5 (tasks) | Ordered, durable, ack support |
| **Core request-reply** | 3 (close signal) | Need delivery confirmation |

### Subject & Key Naming

Use \`jobId\` (not \`group\`) — same coworker may have concurrent containers (message container vs task container).

\`\`\`
# JetStream Streams
agent.{jobId}.results        # Channel 2: streaming results
agent.{jobId}.input          # Channel 3: follow-up messages
agent.{jobId}.messages       # Channel 4: agent -> user messages
agent.{jobId}.tasks          # Channel 5: task operations

# KV Store
agent-init.{jobId}           # Channel 1: initial prompt + config
snapshots.{group}.tasks      # Channel 6: tasks snapshot
snapshots.{group}.groups     # Channel 6: groups snapshot

# Core NATS request-reply
agent.{jobId}.close          # Channel 3: close signal (confirmed delivery)
\`\`\`

### Channel-by-Channel Migration

#### Channel 1: Initial Input (Orch -> Agent)

**Before**: Orchestrator writes JSON to container stdin via \`process.stdin.write()\`.

**After**: Orchestrator writes to KV \`agent-init.{jobId}\`, Agent reads on startup.

\`\`\`python
# Orchestrator side (nanoclaw/container/runner.py)
kv = await js.key_value("agent-init")
await kv.put(job_id, json.dumps(agent_input).encode())

# Agent side (agent_runner/main.py)
kv = await js.key_value("agent-init")
entry = await kv.get(job_id)
agent_input = json.loads(entry.value)
\`\`\`

**Why KV**: Agent Pod in K8s cannot receive stdin pipe. KV is pull-based — agent starts, reads config, begins work. Orch writes before starting the container.

#### Channel 2: Streaming Results (Agent -> Orch)

**Before**: Agent writes to stdout with \`OUTPUT_START_MARKER\` / \`OUTPUT_END_MARKER\`. Orchestrator reads stdout line by line, parsing markers.

**After**: Agent publishes result blocks to JetStream \`agent.{jobId}.results\`. Orchestrator subscribes and processes each message.

\`\`\`python
# Agent side
await js.publish(f"agent.{job_id}.results", json.dumps({
    "status": "success",
    "result": "analysis complete...",
    "newSessionId": "uuid-xxx",
}).encode())

# Orchestrator side
async for msg in subscription:
    output = json.loads(msg.data)
    await on_output(output)
    await msg.ack()
\`\`\`

**Why JetStream**: Results must be ordered and durable. Multiple result blocks per job (streaming).

#### Channel 3: Follow-up Messages + Close (Orch -> Agent)

**Before**: Orch writes \`input/{timestamp}.json\` files; Agent polls. Close signal via \`_close\` sentinel file.

**After**: Follow-up messages via JetStream \`agent.{jobId}.input\`. Close signal via Core NATS request-reply \`agent.{jobId}.close\`.

\`\`\`python
# Orchestrator: send follow-up message
await js.publish(f"agent.{job_id}.input", json.dumps({
    "type": "input",
    "text": "user follow-up message",
}).encode())

# Orchestrator: send close signal (confirmed delivery)
response = await nc.request(f"agent.{job_id}.close", b"close", timeout=5.0)
# response confirms agent received the close signal

# Agent: subscribe to follow-up messages
sub = await js.subscribe(f"agent.{job_id}.input")
async for msg in sub:
    data = json.loads(msg.data)
    # feed into agent conversation
    await msg.ack()

# Agent: handle close signal
async def handle_close(msg):
    await msg.respond(b"ack")
    # initiate graceful shutdown
await nc.subscribe(f"agent.{job_id}.close", cb=handle_close)
\`\`\`

**Why request-reply for close**: Orch needs confirmation that agent received the close signal before cleaning up resources. Fire-and-forget could lose the signal.

#### Channel 4 & 5: Messages + Tasks (Agent -> Orch)

**Before**: Agent writes JSON files to \`messages/*.json\` and \`tasks/*.json\`. Orch polls directories every 1 second.

**After**: Agent publishes to JetStream. Orch subscribes — zero latency.

\`\`\`python
# Agent: send message to user (Channel 4)
await js.publish(f"agent.{job_id}.messages", json.dumps({
    "type": "message",
    "chatJid": "tg:12345",
    "text": "Hello from agent",
    "groupFolder": "main",
    "timestamp": datetime.now(UTC).isoformat(),
}).encode())

# Agent: task operation (Channel 5)
await js.publish(f"agent.{job_id}.tasks", json.dumps({
    "type": "task",
    "operation": "schedule_task",
    "groupFolder": "main",
    "prompt": "daily ad review",
    "scheduleType": "cron",
    "scheduleValue": "0 9 * * *",
}).encode())

# Orchestrator: subscribe to both
messages_sub = await js.subscribe(f"agent.{job_id}.messages")
tasks_sub = await js.subscribe(f"agent.{job_id}.tasks")
\`\`\`

Task operation types unchanged: \`schedule_task\`, \`pause_task\`, \`resume_task\`, \`cancel_task\`, \`update_task\`, \`refresh_groups\`, \`register_group\`.

#### Channel 6: Snapshots (Orch -> Agent)

**Before**: Orch writes \`current_tasks.json\` and \`available_groups.json\` files. Agent reads via MCP tools.

**After**: Orch writes to KV Store. Agent reads via MCP tools. Permission filtering unchanged (main sees all, non-main restricted).

\`\`\`python
# Orchestrator: write snapshots before agent starts
kv_snapshots = await js.key_value("snapshots")
await kv_snapshots.put(f"{group}.tasks", json.dumps(tasks_data).encode())
await kv_snapshots.put(f"{group}.groups", json.dumps(groups_data).encode())

# Agent MCP tool: read snapshot
entry = await kv_snapshots.get(f"{group}.tasks")
tasks = json.loads(entry.value)
\`\`\`

**Why KV**: Snapshots are point-in-time state, not event streams. Agent reads once when needed. KV semantics (get latest) fit perfectly.

## New Files

### \`nanoclaw/ipc/__init__.py\` — Public API

\`\`\`python
"""IPC layer — NATS-based messaging between Orchestrator and Agent."""
from nanoclaw.ipc.protocol import IpcEnvelope, AgentInitData
from nanoclaw.ipc.nats_transport import NatsTransport

__all__ = ["IpcEnvelope", "AgentInitData", "NatsTransport"]
\`\`\`

### \`nanoclaw/ipc/protocol.py\` — IPC message types

\`\`\`python
from __future__ import annotations
from dataclasses import dataclass, asdict
import json

@dataclass(frozen=True)
class IpcEnvelope:
    """Wrapper for all IPC messages."""
    type: str
    group_folder: str
    timestamp: str
    payload: dict[str, object]

    def serialize(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def deserialize(cls, data: bytes) -> IpcEnvelope:
        return cls(**json.loads(data))

@dataclass(frozen=True)
class AgentInitData:
    """Channel 1: initial input written to KV before container starts."""
    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None

    def serialize(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def deserialize(cls, data: bytes) -> AgentInitData:
        return cls(**json.loads(data))
\`\`\`

### \`nanoclaw/ipc/nats_transport.py\` — NATS transport (Orchestrator side)

\`\`\`python
import nats
from nats.js import JetStreamContext, kv

class NatsTransport:
    """NATS transport for Orchestrator-side IPC."""

    def __init__(self, url: str = "nats://localhost:4222") -> None:
        self._url = url
        self._nc: nats.NATS | None = None
        self._js: JetStreamContext | None = None

    async def connect(self) -> None:
        self._nc = await nats.connect(self._url)
        self._js = self._nc.jetstream()
        # Create JetStream stream for agent communication
        await self._js.add_stream(
            name="agent-ipc",
            subjects=["agent.>"],
            retention="workqueue",
            max_age=3600_000_000_000,  # 1 hour TTL
        )
        # Create KV buckets
        await self._js.create_key_value(config=kv.KeyValueConfig(
            bucket="agent-init", ttl=3600,
        ))
        await self._js.create_key_value(config=kv.KeyValueConfig(
            bucket="snapshots", ttl=3600,
        ))

    @property
    def nc(self) -> nats.NATS:
        assert self._nc is not None
        return self._nc

    @property
    def js(self) -> JetStreamContext:
        assert self._js is not None
        return self._js

    async def close(self) -> None:
        if self._nc:
            await self._nc.close()
\`\`\`

## Files to Modify

### 1. \`nanoclaw/container/runner.py\`

Major changes — this file currently handles channels 1, 2, 3, 6:

- **Remove stdin writing**: No longer pipe JSON to subprocess stdin. Instead, write to KV \`agent-init.{jobId}\` before starting container.
- **Remove stdout marker parsing**: No longer read stdout line by line. Instead, subscribe to JetStream \`agent.{jobId}.results\`.
- **Remove IPC directory creation**: No \`/workspace/ipc\` mount needed.
- **Remove \`write_tasks_snapshot()\` / \`write_groups_snapshot()\` file writes**: Replace with KV Store writes to \`snapshots.{group}.tasks\` / \`snapshots.{group}.groups\`.
- **Add \`NATS_URL\` env var** to container.
- **Add \`JOB_ID\` env var** to container (unique per container invocation).

### 2. \`nanoclaw/container/scheduler.py\` (was \`group_queue.py\`)

- **\`send_message()\`**: Publish to \`agent.{jobId}.input\` instead of writing file.
- **\`close_stdin()\`**: Use \`nc.request(f"agent.{jobId}.close", ...)\` instead of writing sentinel file.
- Store \`job_id\` in \`_GroupState\` alongside \`container_name\`.

### 3. \`src/agent_runner/main.py\`

Major changes — this is the container entry point:

- **Remove stdin reading**: Read initial input from KV \`agent-init.{JOB_ID}\`.
- **Remove stdout marker writing**: Publish results to JetStream \`agent.{JOB_ID}.results\`.
- **Subscribe to \`agent.{JOB_ID}.input\`**: Receive follow-up messages.
- **Handle \`agent.{JOB_ID}.close\`**: Graceful shutdown on close signal.
- **Connect to NATS** on startup via \`NATS_URL\` env var.

### 4. \`src/agent_runner/ipc_mcp.py\`

- **Remove \`_write_ipc_file()\`** entirely.
- **MCP tools publish to NATS** instead of writing files:
  - \`send_message\` -> \`agent.{JOB_ID}.messages\`
  - \`schedule_task\`, \`pause_task\`, etc. -> \`agent.{JOB_ID}.tasks\`
  - \`refresh_groups\`, \`register_group\` -> \`agent.{JOB_ID}.tasks\`
- **Snapshot queries** read from KV \`snapshots.{group}.tasks\` / \`snapshots.{group}.groups\`.

### 5. \`nanoclaw/ipc/file_transport.py\`

**Delete** this file. No fallback — clean cut to NATS.

### 6. \`nanoclaw/main.py\`

- Initialize \`NatsTransport\` on startup, pass to runner/scheduler/etc.
- Remove \`start_ipc_watcher()\` — replaced by NATS subscriptions in runner.
- Close NATS on shutdown.

### 7. \`nanoclaw/core/config.py\`

\`\`\`python
NATS_URL: str = os.environ.get("NATS_URL", "nats://localhost:4222")
\`\`\`

### 8. \`pyproject.toml\`

\`\`\`toml
dependencies = [
    ...
    "nats-py>=2.9",
]
\`\`\`

## Development Setup

\`\`\`yaml
# docker-compose.dev.yml
services:
  nats:
    image: nats:latest
    ports:
      - "4222:4222"   # Client
      - "8222:8222"   # Monitoring
    command: ["--jetstream", "--store_dir=/data"]
    volumes:
      - nats-data:/data

volumes:
  nats-data:
\`\`\`

Or simply: \`docker run -d --name nats -p 4222:4222 -p 8222:8222 nats:latest --jetstream\`

Container agents connect via \`NATS_URL=nats://host.docker.internal:4222\`.

## Acceptance Criteria

- [ ] \`nats-py\` added to \`pyproject.toml\`
- [ ] \`nanoclaw/ipc/protocol.py\` defines \`IpcEnvelope\` and \`AgentInitData\`
- [ ] \`nanoclaw/ipc/nats_transport.py\` implements \`NatsTransport\` with JetStream + KV
- [ ] **Channel 1**: Initial input written to KV, agent reads from KV (not stdin)
- [ ] **Channel 2**: Agent publishes results to JetStream (no stdout markers)
- [ ] **Channel 3**: Follow-up messages via JetStream, close signal via request-reply
- [ ] **Channel 4**: Agent messages published to JetStream (not file writes)
- [ ] **Channel 5**: Task operations published to JetStream (not file writes)
- [ ] **Channel 6**: Snapshots stored in KV Store (not JSON files)
- [ ] \`file_transport.py\` deleted — no file IPC fallback
- [ ] IPC directory creation removed from \`build_volume_mounts()\`
- [ ] \`NATS_URL\` and \`JOB_ID\` env vars passed to containers
- [ ] Container agent runner connects to NATS on startup
- [ ] NATS dev setup documented (docker-compose)
- [ ] All existing tests updated and passing
- [ ] New unit tests for each NATS channel (mock NATS client)
- [ ] \`ruff check . && mypy --strict src/nanoclaw && pytest\` all pass

## Important Notes

- **Working directory**: Project root (\`py/\` directory)
- **Subject naming uses \`jobId\`** not \`group\` — same coworker may have concurrent containers (message vs task), jobId ensures precise routing
- **JetStream stream**: Single stream \`agent-ipc\` with subject filter \`agent.>\`, workqueue retention (delete after ack)
- **KV buckets**: \`agent-init\` (TTL 1h) and \`snapshots\` (TTL 1h)
- **Container NATS access**: via \`host.docker.internal:4222\` (same pattern as credential proxy)
- **This is a complete replacement** — stdin, stdout markers, and file IPC are all removed
- **Branch**: Create from \`python-rewrite\` branch (after Step 1 merge)
ISSUE_EOF
)"
```

### 启动 Claude Code 执行

Issue 创建后，记下 Issue 编号（如 `#2`），新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view 2 --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
分支：从 python-rewrite 创建新分支 step2/nats-ipc
完成后提交代码并创建 PR。
```

> 将 `2` 替换为实际 Issue 编号。

---

## Step 3：AgentExecutor 抽象层

**目标**：从 `container_runner.py` 中提取 Agent 执行逻辑到 `AgentExecutor` 协议，为未来 pi-mono 替换做准备。

**前置条件**：Step 1 完成。

### 当前问题

`container_runner.py`（794 行）混合了两个关注点：
1. **容器基础设施**：volume mount、docker 参数、进程管理
2. **Agent 执行协议**：输入/输出 JSON、会话管理、输出流解析

### 目标设计

- `AgentExecutor` Protocol：定义 agent 执行后端的统一接口
- `ClaudeCodeExecutor`：当前的 Claude Code 容器实现
- 未来：`PiMonoExecutor` 实现同一接口

### 创建 Issue 命令

```bash
gh issue create \
  --title "refactor: extract AgentExecutor protocol from container_runner" \
  --label "refactor,python-rewrite" \
  --body "$(cat <<'ISSUE_EOF'
## Context

\`container_runner.py\` (794 lines, after Step 1 at \`nanoclaw/container/runner.py\`) mixes two concerns:
1. **Container infrastructure**: volume mounts, docker args, process management
2. **Agent execution protocol**: input/output JSON, session management, output stream parsing

Separating these enables future replacement of the agent backend (e.g., pi-mono instead of Claude Code) without touching container infrastructure.

This is **Step 3** of the AI Coworker Platform foundation. See \`STEPS.md\` for the full plan.

**Prerequisite**: Step 1 (file reorg) should be completed first.

## Goal

Extract agent execution logic into an \`AgentExecutor\` protocol with a \`ClaudeCodeExecutor\` implementation. The rest of the codebase programs against the protocol, not the concrete implementation.

## Current Architecture

\`\`\`python
# nanoclaw/container/runner.py (after Step 1)

@dataclass
class ContainerInput:
    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None

@dataclass
class ContainerOutput:
    status: Literal["success", "error"]
    result: str | None
    new_session_id: str | None = None
    error: str | None = None

async def run_container_agent(
    group: RegisteredGroup,
    inp: ContainerInput,
    on_process: Callable[[asyncio.subprocess.Process, str], None],
    on_output: Callable[[ContainerOutput], Awaitable[None]] | None = None,
) -> ContainerOutput:
    """794-line function that:
    1. Builds volume mounts
    2. Builds docker args
    3. Spawns subprocess (docker run)
    4. Writes JSON to stdin
    5. Streams stdout, parses OUTPUT_START/END markers
    6. Handles timeout (resets per output block)
    7. Returns final ContainerOutput
    """
\`\`\`

**Callers**:
- \`nanoclaw/main.py\`: \`_invoke_agent()\` calls \`run_container_agent()\`
- \`nanoclaw/container/scheduler.py\`: \`_run_task()\` calls \`run_container_agent()\` indirectly via \`_process_messages_fn\`

## Target Architecture

### New Files

#### \`nanoclaw/agent/executor.py\` — Protocol + Data Types

\`\`\`python
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal, Protocol

@dataclass(frozen=True)
class AgentInput:
    """Input to an agent execution."""
    prompt: str
    group_folder: str
    chat_jid: str
    is_main: bool
    session_id: str | None = None
    is_scheduled_task: bool = False
    assistant_name: str | None = None
    # Future fields for multi-tenant/role support:
    system_prompt: str | None = None
    role_config: dict[str, object] | None = None

@dataclass(frozen=True)
class AgentOutput:
    """Output from an agent execution."""
    status: Literal["success", "error"]
    result: str | None
    new_session_id: str | None = None
    error: str | None = None

class AgentExecutor(Protocol):
    """Protocol for agent execution backends.

    Implementations:
    - ClaudeCodeExecutor: Runs Claude Code in Docker containers
    - (Future) PiMonoExecutor: Runs pi-mono agent framework
    """

    @property
    def name(self) -> str:
        """Human-readable executor name (e.g. 'claude-code', 'pi-mono')."""
        ...

    async def execute(
        self,
        inp: AgentInput,
        on_process: Callable[[asyncio.subprocess.Process, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        """Execute an agent with the given input.

        Args:
            inp: Agent input (prompt, context, session)
            on_process: Called when container process starts (for tracking)
            on_output: Called for each intermediate output block (streaming)

        Returns:
            Final AgentOutput with result or error
        """
        ...
\`\`\`

#### \`nanoclaw/agent/claude_code.py\` — Claude Code Implementation

\`\`\`python
from nanoclaw.agent.executor import AgentExecutor, AgentInput, AgentOutput
from nanoclaw.container.runner import (
    build_volume_mounts,
    build_container_args,
    OUTPUT_START_MARKER,
    OUTPUT_END_MARKER,
)
from nanoclaw.core.types import RegisteredGroup

class ClaudeCodeExecutor:
    """Executes agents via Claude Code in Docker containers.

    Wraps the container lifecycle:
    1. Build volume mounts from RegisteredGroup config
    2. Build docker run args
    3. Spawn container subprocess
    4. Write AgentInput as JSON to stdin
    5. Stream stdout, parse output markers
    6. Return AgentOutput
    """

    name: str = "claude-code"

    def __init__(self, registered_groups: Callable[[], dict[str, RegisteredGroup]]) -> None:
        self._registered_groups = registered_groups

    async def execute(
        self,
        inp: AgentInput,
        on_process: Callable[[asyncio.subprocess.Process, str], None],
        on_output: Callable[[AgentOutput], Awaitable[None]] | None = None,
    ) -> AgentOutput:
        """Execute Claude Code agent in a container."""
        ...

    async def _parse_output_stream(
        self,
        stdout: asyncio.StreamReader,
        on_output: Callable[[AgentOutput], Awaitable[None]] | None,
    ) -> AgentOutput:
        """Parse container stdout for OUTPUT_START/END markers."""
        ...
\`\`\`

#### \`nanoclaw/agent/__init__.py\` — Public API

\`\`\`python
from nanoclaw.agent.executor import AgentExecutor, AgentInput, AgentOutput
from nanoclaw.agent.claude_code import ClaudeCodeExecutor

__all__ = ["AgentExecutor", "AgentInput", "AgentOutput", "ClaudeCodeExecutor"]
\`\`\`

### Files to Modify

#### 1. \`nanoclaw/container/runner.py\`

**Keep** (container infrastructure, pure functions):
- \`build_volume_mounts()\` — volume mount computation
- \`build_container_args()\` — docker CLI arg construction
- \`write_tasks_snapshot()\` — task state publishing
- \`write_groups_snapshot()\` — group state publishing
- \`OUTPUT_START_MARKER\`, \`OUTPUT_END_MARKER\` constants
- \`VolumeMount\`, \`AvailableGroup\` dataclasses
- \`_generate_container_name()\` helper

**Remove** (moves to \`agent/claude_code.py\`):
- \`run_container_agent()\` — the main function
- \`ContainerInput\` — replaced by \`AgentInput\`
- \`ContainerOutput\` — replaced by \`AgentOutput\`
- Output stream parsing logic
- Timeout management logic

**Add backward-compat aliases** (temporary, with deprecation comment):
\`\`\`python
# Backward compatibility — remove after all callers updated
ContainerInput = AgentInput
ContainerOutput = AgentOutput
\`\`\`

#### 2. \`nanoclaw/main.py\`

Update \`_invoke_agent()\`:
\`\`\`python
# Before:
from nanoclaw.container.runner import run_container_agent, ContainerInput
output = await run_container_agent(group, container_input, on_process, on_output)

# After:
from nanoclaw.agent import ClaudeCodeExecutor, AgentInput
executor: AgentExecutor = ClaudeCodeExecutor(self._registered_groups)
output = await executor.execute(agent_input, on_process, on_output)
\`\`\`

The executor instance can be created once at startup and stored, or created per-invocation (it's lightweight).

#### 3. \`nanoclaw/container/scheduler.py\`

Update task/message processing to use \`AgentExecutor\` protocol:
\`\`\`python
class ContainerScheduler:  # was GroupQueue
    def __init__(self, executor: AgentExecutor) -> None:
        self._executor = executor
\`\`\`

#### 4. Tests

- New: \`tests/agent/test_executor.py\` — test AgentInput/AgentOutput dataclasses
- New: \`tests/agent/test_claude_code.py\` — test ClaudeCodeExecutor (mock subprocess)
- Update: existing container_runner tests — adjust imports, some tests move to agent/

### Data Type Mapping

| Old (container/runner.py) | New (agent/executor.py) | Notes |
|--------------------------|------------------------|-------|
| \`ContainerInput\` | \`AgentInput\` | Added \`system_prompt\`, \`role_config\` fields |
| \`ContainerOutput\` | \`AgentOutput\` | Identical fields |
| \`run_container_agent()\` | \`ClaudeCodeExecutor.execute()\` | Method on class |

### \`AgentInput\` JSON Format (sent to container stdin)

The JSON format sent to the container agent-runner must remain backward-compatible:
\`\`\`json
{
  "prompt": "User message",
  "groupFolder": "main",
  "chatJid": "tg:12345",
  "isMain": true,
  "sessionId": null,
  "isScheduledTask": false,
  "assistantName": "Andy"
}
\`\`\`

\`AgentInput\` uses snake_case Python fields but serializes to camelCase JSON for the container protocol. Use a \`to_container_json()\` method or explicit dict construction.

## Acceptance Criteria

- [ ] \`nanoclaw/agent/executor.py\` defines \`AgentInput\`, \`AgentOutput\`, \`AgentExecutor\` protocol
- [ ] \`nanoclaw/agent/claude_code.py\` implements \`ClaudeCodeExecutor\`
- [ ] \`ClaudeCodeExecutor.execute()\` has the same behavior as the current \`run_container_agent()\`
- [ ] \`nanoclaw/container/runner.py\` retains only container infrastructure functions
- [ ] \`nanoclaw/main.py\` uses \`AgentExecutor\` protocol (not \`run_container_agent\` directly)
- [ ] \`nanoclaw/container/scheduler.py\` receives \`AgentExecutor\` via dependency injection
- [ ] \`ContainerInput\`/\`ContainerOutput\` have backward-compat aliases with deprecation comments
- [ ] Container stdin JSON format unchanged (camelCase, same fields)
- [ ] All tests pass, new tests added for executor protocol
- [ ] \`ruff check . && mypy --strict nanoclaw && pytest\` all pass
- [ ] No circular imports

## Important Notes

- **Working directory**: Project root (\`py/\` directory)
- **Python 3.12+**: Use \`Protocol\` for structural typing, not ABC
- **Preserve exact container I/O protocol**: The container agent-runner expects specific JSON format
- **\`on_process\` callback**: Must still be called when container process starts — the scheduler uses it to track active containers
- **\`on_output\` callback**: Must still be called for each intermediate output block — used for streaming responses
- **Branch**: Create from \`python-rewrite\` branch (after Step 1 merge)
ISSUE_EOF
)"
```

### 启动 Claude Code 执行

Issue 创建后，记下 Issue 编号（如 `#3`），新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view 3 --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
分支：从 python-rewrite 创建新分支 step3/agent-executor
完成后提交代码并创建 PR。
```

> 将 `3` 替换为实际 Issue 编号。

---

## Step 4：Docker API + ContainerRuntime 抽象层

**目标**：用 Docker Engine API（`aiodocker`）替换 `subprocess.run("docker ...")`，创建 `ContainerRuntime` 协议统一 Docker（开发）和 K8s（生产）接口。

**前置条件**：Step 1 完成。Step 3（AgentExecutor）建议先完成但非必须。

### 当前问题

- `container_runtime.py`（117 行）用 `subprocess.run` 调用 Docker CLI
- `container_runner.py` 用 `asyncio.create_subprocess_exec("docker", "run", ...)` 启动容器
- 基于字符串的参数构造脆弱，不支持原生 stdin/stdout 流，无法抽象到 K8s

### 目标设计

- `ContainerSpec`：纯数据对象，描述容器规格
- `ContainerHandle`：运行中容器的句柄，提供 stdin/stdout 流
- `ContainerRuntime`：容器运行时协议
- `DockerRuntime`：基于 `aiodocker` 的实现
- `K8sRuntime`：占位符（仅 Protocol + stub）

### 创建 Issue 命令

```bash
gh issue create \
  --title "feat: replace Docker CLI with Docker API and add ContainerRuntime abstraction" \
  --label "enhancement,python-rewrite" \
  --body "$(cat <<'ISSUE_EOF'
## Context

NanoClaw currently invokes Docker via \`subprocess.run("docker ...")\` and \`asyncio.create_subprocess_exec("docker", "run", ...)\`. This is fragile (string-based arg construction, output parsing), doesn't support streaming stdin/stdout natively, and cannot be abstracted to Kubernetes.

This is **Step 4** of the AI Coworker Platform foundation. See \`STEPS.md\` for the full plan.

**Prerequisite**: Step 1 (file reorg) should be completed first. Step 3 (AgentExecutor) is recommended but not strictly required.

## Goal

1. Replace Docker CLI subprocess calls with the Docker Engine API via \`aiodocker\`
2. Create a \`ContainerRuntime\` protocol that can be implemented by both \`DockerRuntime\` (dev) and \`K8sRuntime\` (prod, future)
3. Runtime selection via \`CONTAINER_RUNTIME=docker|k8s\` env var

## Current Docker Usage

### \`nanoclaw/container/runtime.py\` (after Step 1, was \`container_runtime.py\`, 117 lines)

\`\`\`python
CONTAINER_RUNTIME_BIN: str = "docker"

def _detect_proxy_bind_host() -> str:
    """Detect bind host for credential proxy (platform-specific)."""
    # macOS/WSL: 127.0.0.1
    # Linux: docker0 bridge IP via fcntl.ioctl

def host_gateway_args() -> list[str]:
    """Returns: ['--add-host=host.docker.internal:host-gateway'] on Linux."""

def readonly_mount_args(host_path: str, container_path: str) -> list[str]:
    """Returns: ['-v', f'{host_path}:{container_path}:ro']"""

def stop_container(name: str) -> str:
    """Returns: f'docker stop -t 1 {name}'"""

def ensure_container_runtime_running() -> None:
    """subprocess.run(['docker', 'info']) — check Docker is running."""

def cleanup_orphans() -> None:
    """subprocess: docker ps --filter name=nanoclaw- -> docker rm -f each."""
\`\`\`

### \`nanoclaw/container/runner.py\` (was \`container_runner.py\`, 794 lines)

\`\`\`python
def build_container_args(mounts: list[VolumeMount], container_name: str) -> list[str]:
    """Build docker run CLI args list."""
    # Returns: ['run', '-i', '--rm', '--name', name, '-e', ..., '-v', ..., image]

async def run_container_agent(...) -> ContainerOutput:
    """asyncio.create_subprocess_exec('docker', *args, stdin=PIPE, stdout=PIPE, stderr=PIPE)"""
    # Writes JSON to stdin
    # Reads stdout line by line
    # Parses output markers
\`\`\`

### \`nanoclaw/container/scheduler.py\` (was \`group_queue.py\`, 326 lines)

\`\`\`python
async def shutdown(self, grace_period_ms: int = 0) -> None:
    """For each active container: subprocess docker stop."""
\`\`\`

## Target Design

### New Protocol & Types

#### \`nanoclaw/container/runtime.py\` — Protocol + Types (rewrite)

\`\`\`python
from __future__ import annotations
import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

@dataclass(frozen=True)
class VolumeMount:
    """Container volume mount specification."""
    host_path: str
    container_path: str
    readonly: bool = False

@dataclass(frozen=True)
class ContainerSpec:
    """Full specification for running a container."""
    name: str
    image: str
    mounts: list[VolumeMount] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    stdin_data: str | None = None
    user: str | None = None              # "uid:gid"
    memory_limit: str | None = None      # "512m"
    cpu_limit: float | None = None       # 1.0
    extra_hosts: dict[str, str] = field(default_factory=dict)
    remove_on_exit: bool = True
    interactive: bool = True

class ContainerHandle(Protocol):
    """Handle to a running container — provides I/O streams."""

    @property
    def name(self) -> str: ...

    async def read_stdout_line(self) -> str | None:
        """Read one line from stdout. Returns None at EOF."""
        ...

    async def write_stdin(self, data: bytes) -> None:
        """Write data to container stdin."""
        ...

    async def close_stdin(self) -> None:
        """Close stdin (signal end of input)."""
        ...

    async def wait(self) -> int:
        """Wait for container to exit, return exit code."""
        ...

    async def stop(self, timeout: int = 1) -> None:
        """Stop the container."""
        ...

class ContainerRuntime(Protocol):
    """Protocol for container execution backends."""

    @property
    def name(self) -> str: ...

    async def ensure_available(self) -> None:
        """Check runtime is available. Raises RuntimeError if not."""
        ...

    async def run(self, spec: ContainerSpec) -> ContainerHandle:
        """Create and start a container from spec."""
        ...

    async def stop(self, name: str, timeout: int = 1) -> None:
        """Stop a container by name."""
        ...

    async def cleanup_orphans(self, prefix: str) -> list[str]:
        """Remove orphaned containers with given name prefix."""
        ...

    async def close(self) -> None:
        """Close the runtime client connection."""
        ...


# Keep platform-specific helpers as module-level functions
def detect_proxy_bind_host() -> str: ...
def get_host_gateway_extra_hosts() -> dict[str, str]: ...

def get_runtime(runtime_name: str | None = None) -> ContainerRuntime:
    """Factory: create runtime from name or CONTAINER_RUNTIME env var."""
    name = runtime_name or os.environ.get("CONTAINER_RUNTIME", "docker")
    if name == "docker":
        from nanoclaw.container.docker_runtime import DockerRuntime
        return DockerRuntime()
    elif name == "k8s":
        raise NotImplementedError("K8sRuntime not yet implemented")
    else:
        raise ValueError(f"Unknown container runtime: {name}")
\`\`\`

#### \`nanoclaw/container/docker_runtime.py\` — Docker API Implementation

\`\`\`python
import aiodocker

class DockerContainerHandle:
    """Handle to a running Docker container."""

    def __init__(self, container: aiodocker.containers.DockerContainer, ws: Any) -> None:
        self._container = container
        self._ws = ws

    @property
    def name(self) -> str:
        return self._container._id[:12]

    async def read_stdout_line(self) -> str | None: ...
    async def write_stdin(self, data: bytes) -> None: ...
    async def close_stdin(self) -> None: ...
    async def wait(self) -> int: ...
    async def stop(self, timeout: int = 1) -> None: ...


class DockerRuntime:
    """Docker Engine API implementation using aiodocker."""

    name: str = "docker"

    def __init__(self) -> None:
        self._client: aiodocker.Docker | None = None

    async def ensure_available(self) -> None:
        self._client = aiodocker.Docker()
        await self._client.system.info()

    async def run(self, spec: ContainerSpec) -> ContainerHandle:
        assert self._client is not None
        config = self._spec_to_docker_config(spec)
        container = await self._client.containers.create_or_replace(name=spec.name, config=config)
        await container.start()
        ws = await container.websocket(stdin=True, stdout=True, stderr=True, stream=True)
        handle = DockerContainerHandle(container, ws)
        if spec.stdin_data:
            await handle.write_stdin(spec.stdin_data.encode())
            await handle.close_stdin()
        return handle

    async def stop(self, name: str, timeout: int = 1) -> None:
        assert self._client is not None
        container = self._client.containers.container(name)
        await container.stop(t=timeout)
        await container.delete()

    async def cleanup_orphans(self, prefix: str) -> list[str]:
        assert self._client is not None
        containers = await self._client.containers.list(filters={"name": [prefix]})
        removed = []
        for c in containers:
            cname = c._container.get("Names", [""])[0].lstrip("/")
            await c.stop(t=1)
            await c.delete()
            removed.append(cname)
        return removed

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    def _spec_to_docker_config(self, spec: ContainerSpec) -> dict:
        binds = []
        for m in spec.mounts:
            mode = "ro" if m.readonly else "rw"
            binds.append(f"{m.host_path}:{m.container_path}:{mode}")

        config = {
            "Image": spec.image,
            "OpenStdin": spec.interactive,
            "StdinOnce": True,
            "AttachStdin": True,
            "AttachStdout": True,
            "AttachStderr": True,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "HostConfig": {
                "Binds": binds,
                "AutoRemove": spec.remove_on_exit,
            },
        }
        if spec.user:
            config["User"] = spec.user
        if spec.memory_limit:
            config["HostConfig"]["Memory"] = self._parse_memory(spec.memory_limit)
        if spec.cpu_limit:
            config["HostConfig"]["NanoCpus"] = int(spec.cpu_limit * 1e9)
        if spec.extra_hosts:
            config["HostConfig"]["ExtraHosts"] = [
                f"{host}:{ip}" for host, ip in spec.extra_hosts.items()
            ]
        return config
\`\`\`

### Files to Modify

#### 1. \`nanoclaw/container/runner.py\`

Refactor \`build_container_args()\` -> \`build_container_spec()\`:

\`\`\`python
# Before: returns list[str] of docker CLI args
def build_container_args(mounts: list[VolumeMount], container_name: str) -> list[str]:

# After: returns ContainerSpec
def build_container_spec(
    mounts: list[VolumeMount],
    container_name: str,
    stdin_data: str | None = None,
) -> ContainerSpec:
\`\`\`

Keep \`build_volume_mounts()\` unchanged.

#### 2. \`nanoclaw/agent/claude_code.py\` (from Step 3)

If Step 3 is done, update \`ClaudeCodeExecutor\` to accept \`ContainerRuntime\`:

\`\`\`python
class ClaudeCodeExecutor:
    def __init__(self, runtime: ContainerRuntime, ...) -> None:
        self._runtime = runtime

    async def execute(self, inp: AgentInput, ...) -> AgentOutput:
        spec = build_container_spec(mounts, name, stdin_data=inp_json)
        handle = await self._runtime.run(spec)
        # Read stdout from handle instead of subprocess
\`\`\`

If Step 3 is NOT done yet, update \`run_container_agent()\` to accept \`ContainerRuntime\`.

#### 3. \`nanoclaw/container/scheduler.py\`

Update container stop logic:
\`\`\`python
# Before: subprocess.run(stop_container(name))
# After: await self._runtime.stop(name)
\`\`\`

#### 4. \`nanoclaw/main.py\`

Initialize runtime at startup:
\`\`\`python
from nanoclaw.container.runtime import get_runtime

runtime = get_runtime()  # Reads CONTAINER_RUNTIME env var
await runtime.ensure_available()
# Pass runtime to executor, scheduler, etc.
# On shutdown: await runtime.close()
\`\`\`

#### 5. \`nanoclaw/core/config.py\`

Add config:
\`\`\`python
CONTAINER_RUNTIME: str = os.environ.get("CONTAINER_RUNTIME", "docker")
\`\`\`

#### 6. \`pyproject.toml\`

Add dependency:
\`\`\`toml
dependencies = [
    ...
    "aiodocker>=0.23",
]
\`\`\`

### Key Implementation Details

#### stdin/stdout Streaming

The current code uses \`asyncio.create_subprocess_exec\` with \`stdin=PIPE, stdout=PIPE\`. The Docker API equivalent uses container attach (WebSocket or raw stream).

**Important**: The output parsing logic reads stdout **line by line**, looking for \`OUTPUT_START_MARKER\` and \`OUTPUT_END_MARKER\`. The \`ContainerHandle.read_stdout_line()\` must support this pattern.

\`aiodocker\` approach:
\`\`\`python
# Option A: Use exec + WebSocket
ws = await container.websocket(stdin=True, stdout=True, stderr=True)
# Read from ws.receive() — need to parse line boundaries

# Option B: Use container.log(follow=True, stdout=True)
# and attach for stdin separately
\`\`\`

Choose the approach that best supports line-by-line stdout reading with concurrent stdin writing.

#### Container Naming

Current naming: \`nanoclaw-{group_folder}-{short_uuid}\` (used for orphan cleanup with prefix filter).

Keep this convention. The \`cleanup_orphans(prefix="nanoclaw-")\` method should find all matching containers.

#### Platform-Specific Host Gateway

Keep \`detect_proxy_bind_host()\` and \`get_host_gateway_extra_hosts()\` as module-level functions in \`runtime.py\`. These are used by \`build_container_spec()\` to configure host access.

#### Error Handling

- \`ensure_available()\` raises \`RuntimeError\` with helpful diagnostic message (same as current behavior)
- \`run()\` raises \`RuntimeError\` if container fails to start
- \`stop()\` is idempotent (no error if container already stopped)
- \`cleanup_orphans()\` logs and continues if individual container removal fails

## Acceptance Criteria

- [ ] \`aiodocker\` added to \`pyproject.toml\`
- [ ] \`nanoclaw/container/runtime.py\` defines \`ContainerSpec\`, \`ContainerHandle\`, \`ContainerRuntime\` protocols
- [ ] \`nanoclaw/container/docker_runtime.py\` implements \`DockerRuntime\` using \`aiodocker\`
- [ ] \`DockerRuntime.ensure_available()\` verifies Docker daemon connectivity
- [ ] \`DockerRuntime.run()\` creates and starts containers with proper stdin/stdout streaming
- [ ] \`DockerRuntime.stop()\` stops containers via Docker API
- [ ] \`DockerRuntime.cleanup_orphans()\` finds and removes containers by name prefix
- [ ] \`ContainerHandle.read_stdout_line()\` supports line-by-line reading (for output marker parsing)
- [ ] \`ContainerHandle.write_stdin()\` supports writing input data
- [ ] \`build_container_args()\` replaced with \`build_container_spec()\` returning \`ContainerSpec\`
- [ ] \`get_runtime()\` factory reads \`CONTAINER_RUNTIME\` env var, returns \`DockerRuntime\` (or raises for \`k8s\`)
- [ ] Platform-specific helpers (\`detect_proxy_bind_host\`, \`get_host_gateway_extra_hosts\`) preserved
- [ ] All callers updated to use \`ContainerRuntime\` protocol
- [ ] K8sRuntime stub exists (raises \`NotImplementedError\`)
- [ ] All existing tests pass
- [ ] New unit tests for \`DockerRuntime\` (mock \`aiodocker\`)
- [ ] New unit tests for \`ContainerSpec\` construction
- [ ] Integration test: \`DockerRuntime.run()\` -> container starts -> read stdout -> stop
- [ ] \`ruff check . && mypy --strict nanoclaw && pytest\` all pass
- [ ] No \`subprocess.run("docker ...")\` calls remain in the codebase

## Important Notes

- **Working directory**: Project root (\`py/\` directory)
- **\`aiodocker\`** is the recommended async Docker client for Python — fits the asyncio architecture
- **stdin/stdout streaming** is the trickiest part — test thoroughly with actual containers
- **Docker socket**: \`aiodocker\` connects to \`/var/run/docker.sock\` by default (Linux) or Docker Desktop socket (macOS)
- **AutoRemove**: Docker API's \`AutoRemove\` + attach can have race conditions — consider managing removal manually
- **Branch**: Create from \`python-rewrite\` branch (after Step 1 merge)
- **K8s implementation is NOT in scope** — only the protocol and a stub class
ISSUE_EOF
)"
```

### 启动 Claude Code 执行

Issue 创建后，记下 Issue 编号（如 `#4`），新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view 4 --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
分支：从 python-rewrite 创建新分支 step4/docker-api
完成后提交代码并创建 PR。
```

> 将 `4` 替换为实际 Issue 编号。
