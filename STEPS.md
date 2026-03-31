# AI Coworker 平台 — 基础实施步骤

将 NanoClaw Python 重写版改造为通用 AI Coworker 平台的基础步骤。

每个步骤包含完整的 GitHub Issue 内容和创建命令。每个 Issue 自包含，新开的 Claude Code 会话只需读取 Issue 即可独立完成工作。

---

## 执行顺序

```
Step 1 → Step 2 → Step 3 → Step 4 → Step 5 → Step 5.1 → Step 6
(重组)   (NATS)  (Executor) (PG)    (多租户)  (合并Role) (RoleMesh独立Repo)
```

Step 1-5.1 已完成。Step 6 将 `py/` 独立为新 GitHub repo **RoleMesh**。

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

## Step 3：AgentExecutor + ContainerRuntime 抽象层

**目标**：一步完成两个抽象层的提取（合并了原 Step 3 和原 Step 4）：

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

Issue 创建后，记下 Issue 编号，新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view <ISSUE_NUMBER> --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
分支：从 python-rewrite 创建新分支 step3/agent-executor-and-runtime
完成后提交代码并创建 PR。
```

> 替换为实际 Issue 编号。

---


## Step 4：SQLite → PostgreSQL

**目标**：用 PostgreSQL + `asyncpg` 替换 SQLite，所有 db 函数改为 async，schema 预留 `tenant_id` 列为将来多租户做准备。

**前置条件**：Step 1、2、3 完成。

### 为什么现在做

- 多租户需要 PG RLS（行级安全），SQLite 不支持
- 当前 SQLite 同步调用在 asyncio 事件循环中会阻塞
- `db/__init__.py` 已经是接口层，切换实现对调用方影响可控

### 改动范围

当前 `db/sqlite.py` 有 835 行、~30 个公共函数、7 张表。全部通过 `db/__init__.py` re-export，调用方从不直接 import `sqlite`。

**核心改动**：新写 `db/pg.py`，改 `__init__.py` 的 import 来源，所有调用方加 `await`。

### 创建 Issue 命令

```bash
gh issue create \
  --title "feat: replace SQLite with PostgreSQL (asyncpg)" \
  --label "enhancement,python-rewrite" \
  --body "$(cat <<'ISSUE_EOF'
## Context

NanoClaw currently uses SQLite (synchronous \`sqlite3\` stdlib) for persistence. This blocks the asyncio event loop and cannot support multi-tenant RLS. The database layer is already abstracted behind \`db/__init__.py\` — callers never import \`sqlite\` directly.

This is **Step 4** of the AI Coworker Platform foundation. See \`STEPS.md\` for the full plan.

**Prerequisites**: Steps 1, 2, 3 completed.

## Goals

1. Replace \`db/sqlite.py\` with \`db/pg.py\` using \`asyncpg\`
2. All ~30 db functions become \`async\`
3. All callers add \`await\`
4. Schema adds \`tenant_id\` column to every table (default \`'default'\`, for future RLS)
5. Development environment via docker-compose (PostgreSQL + NATS)

## Current Architecture

### \`src/nanoclaw/db/sqlite.py\` (835 lines)

Module-level connection:
\`\`\`python
_db: sqlite3.Connection | None = None

def _get_db() -> sqlite3.Connection:
    assert _db is not None
    return _db

def init_database() -> None:
    global _db
    _db = sqlite3.connect(str(db_path))
    _create_schema(_db)
\`\`\`

All functions are synchronous:
\`\`\`python
def store_message(msg: NewMessage) -> None: ...
def get_due_tasks() -> list[ScheduledTask]: ...
def get_all_registered_groups() -> dict[str, RegisteredGroup]: ...
# ... ~30 functions total
\`\`\`

### \`src/nanoclaw/db/__init__.py\` (re-exports)

\`\`\`python
from nanoclaw.db.sqlite import (
    init_database,
    store_message,
    get_due_tasks,
    # ... all 30 functions
)
\`\`\`

Callers import from \`nanoclaw.db\`:
\`\`\`python
from nanoclaw.db import init_database, store_message, get_all_registered_groups
\`\`\`

### 7 tables

\`chats\`, \`messages\`, \`scheduled_tasks\`, \`task_run_logs\`, \`router_state\`, \`sessions\`, \`registered_groups\`

### Callers that need \`await\` added

- \`main.py\` (~30 call sites)
- \`orchestration/task_scheduler.py\` (~15 call sites)
- \`ipc/\` related handlers (~10 call sites)
- Test files

## Target Architecture

### \`src/nanoclaw/db/pg.py\` (~700 lines)

\`\`\`python
import asyncpg
from nanoclaw.core.config import DATABASE_URL

_pool: asyncpg.Pool | None = None
DEFAULT_TENANT: str = "default"

async def init_database(database_url: str | None = None) -> None:
    \"\"\"Initialize PostgreSQL connection pool and create schema.\"\"\"
    global _pool
    url = database_url or DATABASE_URL
    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await _create_schema(conn)

async def close_database() -> None:
    \"\"\"Close the connection pool. Call on shutdown.\"\"\"
    global _pool
    if _pool:
        await _pool.close()
        _pool = None

def _get_pool() -> asyncpg.Pool:
    assert _pool is not None, "Database not initialized. Call await init_database() first."
    return _pool
\`\`\`

Every function becomes async:
\`\`\`python
# Before (sqlite):
def store_message(msg: NewMessage) -> None:
    db = _get_db()
    db.execute("INSERT OR REPLACE INTO messages ...", (...))
    db.commit()

# After (pg):
async def store_message(msg: NewMessage) -> None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            \"\"\"INSERT INTO messages (tenant_id, id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
               VALUES (\$1, \$2, \$3, \$4, \$5, \$6, \$7, \$8, \$9)
               ON CONFLICT (tenant_id, id, chat_jid) DO UPDATE SET
                   content = EXCLUDED.content,
                   timestamp = EXCLUDED.timestamp\"\"\",
            DEFAULT_TENANT, msg.id, msg.chat_jid, msg.sender, msg.sender_name,
            msg.content, msg.timestamp, msg.is_from_me, msg.is_bot_message,
        )
\`\`\`

### PostgreSQL Schema (with tenant_id)

All tables add \`tenant_id TEXT NOT NULL DEFAULT 'default'\` as part of the primary key. This has zero cost now (single tenant), but enables RLS later without schema migration.

\`\`\`sql
CREATE TABLE IF NOT EXISTS chats (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    jid TEXT NOT NULL,
    name TEXT,
    last_message_time TEXT,
    channel TEXT,
    is_group BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (tenant_id, jid)
);

CREATE TABLE IF NOT EXISTS messages (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    id TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TEXT NOT NULL,
    is_from_me BOOLEAN DEFAULT FALSE,
    is_bot_message BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (tenant_id, id, chat_jid)
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(tenant_id, timestamp);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    id TEXT PRIMARY KEY,
    group_folder TEXT NOT NULL,
    chat_jid TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    schedule_value TEXT NOT NULL,
    context_mode TEXT DEFAULT 'isolated',
    next_run TEXT,
    last_run TEXT,
    last_result TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_next ON scheduled_tasks(tenant_id, next_run);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON scheduled_tasks(tenant_id, status);

CREATE TABLE IF NOT EXISTS task_run_logs (
    id SERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    task_id TEXT NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    run_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

CREATE TABLE IF NOT EXISTS router_state (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (tenant_id, key)
);

CREATE TABLE IF NOT EXISTS sessions (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    group_folder TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (tenant_id, group_folder)
);

CREATE TABLE IF NOT EXISTS registered_groups (
    tenant_id TEXT NOT NULL DEFAULT 'default',
    jid TEXT NOT NULL,
    name TEXT NOT NULL,
    folder TEXT NOT NULL,
    trigger_pattern TEXT NOT NULL,
    added_at TEXT NOT NULL,
    container_config JSONB,
    requires_trigger BOOLEAN DEFAULT TRUE,
    is_main BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (tenant_id, jid),
    UNIQUE (tenant_id, folder)
);
\`\`\`

Key differences from SQLite schema:
- \`tenant_id\` in every table and primary key
- \`BOOLEAN\` instead of \`INTEGER\` for boolean fields
- \`JSONB\` instead of \`TEXT\` for \`container_config\` (native JSON queries)
- \`SERIAL\` instead of \`AUTOINCREMENT\` for \`task_run_logs.id\`
- \`ON DELETE CASCADE\` for \`task_run_logs.task_id\` FK
- Parameterized queries use \`\$1, \$2\` instead of \`?\`

### SQL Syntax Differences to Handle

| SQLite | PostgreSQL | Notes |
|--------|-----------|-------|
| \`INSERT OR REPLACE\` | \`INSERT ... ON CONFLICT DO UPDATE\` | Explicit conflict columns |
| \`?\` placeholders | \`\$1, \$2, \$3\` | Numbered params |
| \`INTEGER\` booleans | \`BOOLEAN\` | Native booleans |
| \`TEXT\` for JSON | \`JSONB\` | For container_config |
| \`AUTOINCREMENT\` | \`SERIAL\` | Auto-increment |
| \`executescript()\` | Individual \`execute()\` calls | No batch script |
| \`db.commit()\` | Auto-commit or \`async with conn.transaction()\` | asyncpg default |
| \`sqlite3.Row\` dict access | \`asyncpg.Record\` dict-like access | Similar API |

### \`src/nanoclaw/db/__init__.py\` — Switch Import Source

\`\`\`python
\"\"\"Data layer — PostgreSQL persistence.\"\"\"

from nanoclaw.db.pg import (
    ChatInfo,
    close_database,
    create_task,
    delete_task,
    # ... all functions from pg.py
    init_database,
)

from nanoclaw.db.pg import _init_test_database as _init_test_database
\`\`\`

Note: \`close_database()\` is new (asyncpg pool needs explicit shutdown). Add to \`__all__\` and call in \`main.py\` shutdown.

### Callers: Add \`await\` to All DB Calls

This is mechanical but touches many files:

#### \`main.py\`

\`\`\`python
# Before:
init_database()
groups = get_all_registered_groups()
store_message(msg)
set_session(folder, session_id)

# After:
await init_database()
groups = await get_all_registered_groups()
await store_message(msg)
await set_session(folder, session_id)

# Shutdown — add close_database():
await close_database()
\`\`\`

#### \`orchestration/task_scheduler.py\`

\`\`\`python
# Before:
tasks = get_due_tasks()
update_task_after_run(task.id, next_run, result)
log_task_run(run_log)

# After:
tasks = await get_due_tasks()
await update_task_after_run(task.id, next_run, result)
await log_task_run(run_log)
\`\`\`

### \`core/config.py\` — Add DATABASE_URL

\`\`\`python
DATABASE_URL: str = os.environ.get("DATABASE_URL", "postgresql://nanoclaw:nanoclaw@localhost:5432/nanoclaw")
\`\`\`

### \`pyproject.toml\`

\`\`\`toml
dependencies = [
    ...
    "asyncpg>=0.29",
]

[project.optional-dependencies]
dev = [
    ...
    "testcontainers[postgres]>=4.0",
]
\`\`\`

### Development Setup

\`docker-compose.dev.yml\` (update or create):

\`\`\`yaml
services:
  nats:
    image: nats:latest
    ports:
      - "4222:4222"
      - "8222:8222"
    command: ["--jetstream"]

  postgres:
    image: postgres:16
    ports:
      - "5432:5432"
    environment:
      POSTGRES_DB: nanoclaw
      POSTGRES_USER: nanoclaw
      POSTGRES_PASSWORD: nanoclaw
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
\`\`\`

### Test Strategy

Use \`testcontainers\` for isolated PG per test session:

\`\`\`python
# tests/db/conftest.py
import pytest
from testcontainers.postgres import PostgresContainer

@pytest.fixture(scope="session")
def pg_url():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("psycopg2", "postgresql")

@pytest.fixture
async def test_db(pg_url):
    from nanoclaw.db import init_database, close_database
    await init_database(pg_url)
    yield
    # Clean tables between tests
    pool = _get_pool()
    async with pool.acquire() as conn:
        for table in ["task_run_logs", "messages", "scheduled_tasks", "chats",
                       "sessions", "registered_groups", "router_state"]:
            await conn.execute(f"DELETE FROM {table}")
    await close_database()
\`\`\`

### What NOT to Do

- **No data migration tool** — clean start from empty PG
- **No SQLite fallback** — clean cut, delete \`sqlite.py\`
- **No JSON state migration code** — legacy NanoClaw migration not needed
- **No multi-tenant RLS yet** — just \`tenant_id\` columns and default values
- **No \`tenant_id\` parameter on public functions yet** — all functions use \`DEFAULT_TENANT\` internally. When multi-tenant is implemented, add \`tenant_id\` parameter to each function.

### DEFAULT_TENANT Constant

\`\`\`python
DEFAULT_TENANT: str = "default"
\`\`\`

All queries include \`tenant_id\` with \`DEFAULT_TENANT\`. When multi-tenant is implemented later, this becomes the actual tenant ID from request context.

## Files Summary

### New Files

| File | Content |
|------|---------|
| \`src/nanoclaw/db/pg.py\` | PostgreSQL implementation (~700 lines, all async) |
| \`docker-compose.dev.yml\` | PG 16 + NATS dev services |
| \`tests/db/conftest.py\` | PG test fixtures with testcontainers |

### Modified Files

| File | Changes |
|------|---------|
| \`db/__init__.py\` | Switch imports from \`sqlite\` to \`pg\`, add \`close_database\` |
| \`core/config.py\` | Add \`DATABASE_URL\` |
| \`pyproject.toml\` | Add \`asyncpg\`, add \`testcontainers\` to dev deps |
| \`main.py\` | ~30 call sites: add \`await\`, add \`await close_database()\` on shutdown |
| \`orchestration/task_scheduler.py\` | ~15 call sites: add \`await\` |
| \`tests/db/test_sqlite.py\` | Rename to \`tests/db/test_pg.py\`, rewrite for async |
| \`tests/conftest.py\` | Update db fixture for async PG |
| \`tests/test_e2e.py\` | Update db calls to async |
| \`tests/test_user_flow.py\` | Update db calls to async |

### Deleted Files

| File | Reason |
|------|--------|
| \`src/nanoclaw/db/sqlite.py\` | Replaced by \`pg.py\` |

## Acceptance Criteria

- [ ] \`asyncpg\` added to \`pyproject.toml\`
- [ ] \`src/nanoclaw/db/pg.py\` implements all ~30 functions as \`async\`
- [ ] Every table has \`tenant_id TEXT NOT NULL DEFAULT 'default'\` column
- [ ] \`tenant_id\` is part of every primary key and index
- [ ] \`container_config\` uses \`JSONB\` type
- [ ] \`db/__init__.py\` imports from \`pg\` (not \`sqlite\`)
- [ ] \`close_database()\` exported and called in \`main.py\` shutdown
- [ ] All callers use \`await\` for db calls
- [ ] \`DATABASE_URL\` config in \`core/config.py\`
- [ ] \`docker-compose.dev.yml\` has PG 16 + NATS services
- [ ] \`db/sqlite.py\` deleted
- [ ] No \`import sqlite3\` remains in production code
- [ ] All tests pass against real PostgreSQL
- [ ] \`ruff check . && mypy --strict src/nanoclaw && pytest\` all pass
- [ ] \`DEFAULT_TENANT\` used consistently across all queries
- [ ] No JSON state migration code

## Important Notes

- **Working directory**: Project root (\`py/\` directory)
- **asyncpg uses \`\$1, \$2\` params** — not \`?\`. Every query needs rewriting.
- **asyncpg returns \`asyncpg.Record\`** — dict-like access (\`row["col"]\`).
- **No \`executescript()\`** — each CREATE TABLE as separate \`execute()\`.
- **Transactions**: asyncpg auto-commits. Use \`async with conn.transaction():\` for atomicity.
- **Boolean**: PostgreSQL native \`BOOLEAN\`. No more \`1/0\` integers.
- **tenant_id now, RLS later** — just columns and defaults. Policies in future step.
- **Branch**: Create from \`python-rewrite\` branch (after Step 3 merged)
ISSUE_EOF
)"
```

### 启动 Claude Code 执行

Issue 创建后，记下 Issue 编号，新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view 8 --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
分支：从 python-rewrite 创建新分支 step4/sqlite-to-pg
完成后提交代码并创建 PR。
```

> 替换为实际 Issue 编号。

---

## Step 5：多租户 + 多 Coworker 架构

**目标**：将 NanoClaw 从单租户扩展为多租户、多 Coworker 架构。每个租户可有多种 AI 角色（Role 模板），每种角色可有多个实例（Coworker），每个 Coworker 通过独立 bot 身份在多个聊天群中与用户交互。容器并发控制支持全局 + 租户 + Coworker 三级限流。

**前置条件**：Step 4（PostgreSQL）完成。所有表已有 `tenant_id` 列，所有查询已带 `WHERE tenant_id = $1`。

**不包含**：审批流（L1/L2/L3）、A2A 跨 Coworker 协作、Role/Coworker 管理 UI、用户权限精细控制、Web Dashboard、PG RLS Policy。

### 创建 Issue 命令

```bash
gh issue create \
  --title "feat: multi-tenant multi-coworker architecture" \
  --label "enhancement,python-rewrite" \
  --body "$(cat <<'ISSUE_EOF'
## Context

After Step 4, NanoClaw has PostgreSQL with \`tenant_id\` columns and \`DEFAULT_TENANT = "default"\` throughout. The single-tenant architecture uses module-level global variables (\`_sessions\`, \`_registered_groups\`, \`_queue\`, \`_channels\`) and treats each \`RegisteredGroup\` as a 1:1 mapping between a chat and an agent.

This step transforms the architecture to support:
- Multiple **tenants** (organizations)
- Multiple **roles** (AI agent templates: operations AI, logistics AI, etc.)
- Multiple **coworkers** (role instances, each with own workspace)
- Multiple **conversations** per coworker (same coworker in different Telegram groups / Slack channels)
- Per-coworker **bot identity** (each coworker has its own Telegram bot, etc.)
- Three-level **concurrency control** (global + per-tenant + per-coworker)

This is **Step 5** of the AI Coworker Platform. See \`STEPS.md\` for the full plan.

**Prerequisites**: Steps 1-4 completed (src layout, NATS IPC, AgentExecutor + DockerRuntime, PostgreSQL).

## Concept Hierarchy

\`\`\`
Tenant "Acme Corp"
├── Role "Operations AI" (template: prompt + tools + skills + backend)
│   ├── Coworker "Ops AI - Product Line A"
│   │   ├── ChannelBinding: Telegram bot @acme_ops_a_bot
│   │   │   ├── Conversation: tg group 1001
│   │   │   └── Conversation: tg group 1002
│   │   └── ChannelBinding: Slack bot
│   │       └── Conversation: slack channel C789
│   │
│   └── Coworker "Ops AI - Product Line B"
│       └── ChannelBinding: Telegram bot @acme_ops_b_bot
│           └── Conversation: tg group 2001
│
├── Role "Customer Service AI"
│   └── Coworker "CS AI"
│       └── ChannelBinding: Telegram bot @acme_cs_bot
│           └── Conversation: tg group 1001 (same group, different bot)
│
├── SharedSpace (cross-coworker shared knowledge)
└── Users: Zhang San (admin), Li Si (member)
\`\`\`

Key design decisions:
- **Workspace** is per-coworker (shared across conversations) — same coworker in different groups operates on same files
- **Session** is per-conversation — different groups have independent conversation memory
- **Bot identity** is per-coworker per-channel-type — each coworker has its own Telegram bot, Slack app, etc.
- **Trigger pattern** is per-conversation (different groups may have different trigger rules), but the @mention name comes from the coworker

## Mapping from Current Concepts

| Current | Multi-tenant | Relationship |
|---------|-------------|-------------|
| \`DEFAULT_TENANT = "default"\` | \`tenant_id\` (dynamic, from routing) | Parameter replaces constant |
| \`ASSISTANT_NAME\` (global) | \`coworker.name\` (per-coworker) | Config becomes per-entity |
| \`TRIGGER_PATTERN\` (global) | Derived from \`coworker.name\` | Trigger text = coworker name; \`conversation.requires_trigger\` controls on/off |
| \`RegisteredGroup\` | \`Coworker\` + \`Conversation\` | **Split**: 1 group = 1 coworker + 1 conversation |
| \`group.folder\` | \`coworker.folder\` | Path becomes \`tenants/{tid}/coworkers/{folder}/\` |
| \`group.is_main\` | \`coworker.is_admin\` | Renamed, one per tenant |
| \`chatJid\` | \`conversation.channel_chat_id\` | 1 coworker -> N conversations |
| \`session\` (per group) | \`session\` (per conversation) | Session scope narrows |
| \`GroupQueue\` (global) | \`OrchestratorState\` + 3-level scheduling | Restructured |
| \`Channel\` (global singleton) | \`ChannelGateway\` + per-coworker bots | Gateway manages multiple bots |
| Module-level globals | \`OrchestratorState\` class | All state in structured object |

## New Database Tables

\`\`\`sql
CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT UNIQUE,                        -- URL-friendly identifier (optional, for future Web API)
    name TEXT NOT NULL,
    plan TEXT,                               -- starter/pro/enterprise (optional, for future billing)
    max_concurrent_containers INT DEFAULT 5, -- per-tenant concurrency limit
    last_message_cursor TIMESTAMPTZ,         -- replaces router_state "last_timestamp"
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Users: reserved for future permission control (Step 5 creates the table but
-- does not implement user-based authorization)
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,
    email TEXT,
    role TEXT DEFAULT 'member',              -- admin / manager / member
    channel_ids JSONB DEFAULT '{}',          -- {"telegram": "user123", "slack": "U456"}
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,                      -- "Operations AI"
    agent_backend TEXT DEFAULT 'claude-code', -- AgentBackendConfig.name
    system_prompt TEXT,
    tools JSONB DEFAULT '[]',                -- tool allowlist: ["Bash", "Read", ...]
    skills JSONB DEFAULT '[]',               -- skill names
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, name)
);

CREATE TABLE coworkers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    role_id UUID NOT NULL REFERENCES roles(id),
    name TEXT NOT NULL,                      -- "Ops AI - Product Line A"
    folder TEXT NOT NULL,                    -- filesystem namespace
    is_admin BOOLEAN DEFAULT FALSE,          -- legacy from is_main; refactor target
    container_config JSONB,                  -- resource overrides: {"memory_limit": "1g", "timeout": 600000}
    max_concurrent INT DEFAULT 2,            -- per-coworker concurrency limit
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, folder)
);

-- Bot credentials: per-coworker per-channel-type
CREATE TABLE channel_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id),  -- redundant (from coworker) but avoids JOIN
    channel_type TEXT NOT NULL,              -- "telegram" / "slack" / "web"
    credentials JSONB NOT NULL,              -- {"bot_token": "xxx"}
    bot_display_name TEXT,                   -- "@acme_ops_bot"
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (coworker_id, channel_type)
);

-- Conversation context: per-coworker per-chat
CREATE TABLE conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),   -- redundant but avoids JOIN in hot path
    coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,  -- same
    channel_binding_id UUID NOT NULL REFERENCES channel_bindings(id),
    channel_chat_id TEXT NOT NULL,           -- tg group ID / slack channel ID
    name TEXT,                               -- display name of the chat
    requires_trigger BOOLEAN DEFAULT TRUE,   -- FALSE for DMs / Web UI; trigger text derived from coworker.name
    last_agent_invocation TIMESTAMPTZ,       -- replaces router_state "last_agent_timestamp"
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (channel_binding_id, channel_chat_id)
);
\`\`\`

## Existing Tables: Rewrite with Consistent Types

All existing tables are rewritten with UUID \`tenant_id\` and TIMESTAMPTZ time fields, replacing the legacy TEXT types from Step 4. Legacy tables (\`chats\`, \`registered_groups\`, \`router_state\`) are dropped after migration.

\`\`\`sql
-- Sessions: per-conversation
CREATE TABLE sessions (
    conversation_id UUID PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    coworker_id UUID NOT NULL REFERENCES coworkers(id),
    session_id TEXT NOT NULL
);

-- Messages: keyed by conversation (not chat_jid)
CREATE TABLE messages (
    id TEXT NOT NULL,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    conversation_id UUID NOT NULL REFERENCES conversations(id),
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    timestamp TIMESTAMPTZ NOT NULL,
    is_from_me BOOLEAN DEFAULT FALSE,
    is_bot_message BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (tenant_id, id, conversation_id)
);
CREATE INDEX idx_messages_ts ON messages(tenant_id, conversation_id, timestamp);

-- Scheduled tasks: keyed by coworker
CREATE TABLE scheduled_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    coworker_id UUID NOT NULL REFERENCES coworkers(id),
    conversation_id UUID REFERENCES conversations(id),  -- which chat triggered it (NULL for API-created)
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL,              -- "cron" / "interval" / "once"
    schedule_value TEXT NOT NULL,
    context_mode TEXT DEFAULT 'isolated',     -- "group" / "isolated"
    next_run TIMESTAMPTZ,
    last_run TIMESTAMPTZ,
    last_result TEXT,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_tasks_next ON scheduled_tasks(tenant_id, next_run);
CREATE INDEX idx_tasks_status ON scheduled_tasks(tenant_id, status);

-- Task run logs: execution history
CREATE TABLE task_run_logs (
    id SERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    task_id UUID NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
    run_at TIMESTAMPTZ NOT NULL,
    duration_ms INT NOT NULL,
    status TEXT NOT NULL,                    -- "success" / "error"
    result TEXT,                             -- first 500 chars of agent output (truncated)
    error TEXT
);
CREATE INDEX idx_task_run_logs ON task_run_logs(task_id, run_at);

-- Tenants: add message cursor (replaces router_state "last_timestamp")
-- (this column is on the tenants table since the cursor is per-tenant)
-- ALTER TABLE tenants ADD COLUMN last_message_cursor TIMESTAMPTZ;
-- Note: included in the CREATE TABLE above for new installs; migration adds it
\`\`\`

## Tables Dropped After Migration

\`\`\`sql
-- These legacy tables are replaced by the new schema:
DROP TABLE IF EXISTS router_state;       -- replaced by conversations.last_agent_invocation + tenants.last_message_cursor
DROP TABLE IF EXISTS registered_groups;  -- replaced by coworkers + channel_bindings + conversations
DROP TABLE IF EXISTS chats;              -- replaced by conversations
\`\`\`

## Filesystem Layout

\`\`\`
data/tenants/{tenant_id}/
├── coworkers/{folder}/
│   ├── workspace/               # mount /workspace/group (rw), shared across conversations
│   ├── sessions/
│   │   ├── {conversation_id_1}/ # session for tg group A
│   │   └── {conversation_id_2}/ # session for tg group B (independent)
│   ├── logs/
│   └── CLAUDE.md                # coworker-level memory
├── shared/                      # mount /workspace/shared (ro)
│   └── knowledge/
└── env/
\`\`\`

## Runtime Architecture

### OrchestratorState (replaces module-level globals)

\`\`\`python
@dataclass
class CoworkerConfig:
    \"\"\"Runtime config, merged from Role + Coworker tables.\"\"\"
    id: str
    tenant_id: str
    name: str
    folder: str
    system_prompt: str | None
    trigger_pattern: re.Pattern[str]          # derived from name: re.compile(rf"@{name}\\b", re.I)
    agent_backend: str
    container_image: str | None
    max_concurrent: int
    role_config: dict[str, object]
    tools: list[str]
    skills: list[str]

@dataclass
class ConversationState:
    \"\"\"Per-conversation runtime state.\"\"\"
    conversation: Conversation
    session_id: str | None
    last_agent_timestamp: str

@dataclass
class CoworkerState:
    \"\"\"Per-coworker runtime state.\"\"\"
    config: CoworkerConfig
    conversations: dict[str, ConversationState]    # channel_chat_id -> state
    channel_bindings: dict[str, ChannelBinding]    # channel_type -> binding

class OrchestratorState:
    \"\"\"All runtime state, structured by tenant and coworker.\"\"\"
    tenants: dict[str, Tenant]                     # tenant_id -> Tenant
    coworkers: dict[str, CoworkerState]            # coworker_id -> state

    # Three-level scheduling counters
    global_active: int = 0
    global_limit: int = 20
    tenant_active: dict[str, int] = field(default_factory=dict)
    coworker_active: dict[str, int] = field(default_factory=dict)

    def can_start_container(self, tenant_id: str, coworker_id: str) -> bool:
        if self.global_active >= self.global_limit:
            return False
        tenant = self.tenants[tenant_id]
        if self.tenant_active.get(tenant_id, 0) >= tenant.max_concurrent_containers:
            return False
        cw = self.coworkers[coworker_id]
        if self.coworker_active.get(coworker_id, 0) >= cw.config.max_concurrent:
            return False
        return True
\`\`\`

### ChannelGateway (replaces global Channel singletons)

\`\`\`python
class ChannelGateway(Protocol):
    \"\"\"Manages multiple bot instances for one channel type.\"\"\"
    async def add_binding(self, binding: ChannelBinding) -> None: ...
    async def remove_binding(self, binding_id: str) -> None: ...
    async def send_message(self, binding_id: str, chat_id: str, text: str) -> None: ...
    async def shutdown(self) -> None: ...

class TelegramGateway:
    \"\"\"Manages multiple Telegram bots (one per coworker).\"\"\"
    _bots: dict[str, TelegramBot]      # binding_id -> bot instance
    _on_message: MessageCallback

    async def add_binding(self, binding: ChannelBinding) -> None:
        bot = TelegramBot(binding.credentials["bot_token"])
        bot.on_message = lambda update: self._on_message(binding.id, update)
        await bot.start()
        self._bots[binding.id] = bot

class SlackGateway:
    _apps: dict[str, SlackApp]
    # ... same pattern

# Orchestrator holds one gateway per channel type
_gateways: dict[str, ChannelGateway] = {
    "telegram": TelegramGateway(on_message=_handle_incoming),
    "slack": SlackGateway(on_message=_handle_incoming),
}
\`\`\`

### Message Routing

\`\`\`python
async def _handle_incoming(binding_id: str, chat_id: str, sender_id: str, text: str):
    \"\"\"Unified message handler for all channels.\"\"\"
    # 1. Resolve context
    binding = await db.get_channel_binding(binding_id)
    coworker_state = orchestrator.coworkers[binding.coworker_id]
    conversation = coworker_state.conversations.get(chat_id)
    if not conversation:
        return  # Unknown chat, ignore

    # 2. Check trigger
    if conversation.conversation.requires_trigger:
        if not coworker_state.config.trigger_pattern.search(text):
            return  # trigger text derived from coworker.name

    # 3. Store message
    await db.store_message(
        tenant_id=binding.tenant_id,
        conversation_id=conversation.conversation.id,
        sender=sender_id, content=text, ...
    )

    # 4. Three-level concurrency check + enqueue
    if orchestrator.can_start_container(binding.tenant_id, binding.coworker_id):
        await _execute_coworker(coworker_state, conversation)
    else:
        scheduler.enqueue(binding.coworker_id, conversation)
\`\`\`

### Agent Execution

\`\`\`python
async def _execute_coworker(cw: CoworkerState, conv: ConversationState):
    agent_input = AgentInput(
        prompt=formatted_messages,
        group_folder=cw.config.folder,
        chat_jid=conv.conversation.channel_chat_id,
        is_main=cw.config.is_admin,  # from coworker, not conversation
        session_id=conv.session_id,              # per-conversation
        system_prompt=cw.config.system_prompt,    # from Role
        role_config=cw.config.role_config,
        tenant_id=cw.config.tenant_id,
        coworker_id=cw.config.id,
    )
    backend = get_backend_config(cw.config.agent_backend, cw.config.container_image)
    output = await executor.execute(agent_input, on_process, on_output)
    if output.new_session_id:
        conv.session_id = output.new_session_id
        await db.set_session(conv.conversation.id, output.new_session_id)
\`\`\`

### Container Volume Mounts

\`\`\`python
def build_volume_mounts(
    tenant_id: str,
    coworker: Coworker,
    conversation_id: str,
    is_admin: bool,
    backend_config: AgentBackendConfig | None = None,
) -> list[VolumeMount]:
    tenant_dir = DATA_DIR / "tenants" / tenant_id
    coworker_dir = tenant_dir / "coworkers" / coworker.folder
    shared_dir = tenant_dir / "shared"
    session_dir = coworker_dir / "sessions" / conversation_id

    mounts = [
        VolumeMount(str(coworker_dir / "workspace"), "/workspace/group", readonly=False),
        VolumeMount(str(shared_dir), "/workspace/shared", readonly=True),
        VolumeMount(str(session_dir), "/workspace/sessions", readonly=False),
        VolumeMount(str(coworker_dir / "logs"), "/workspace/logs", readonly=False),
    ]
    # ... backend_config handling
    return mounts
\`\`\`

### NATS Subject Naming

\`\`\`
# Agent IPC (job_id is globally unique, unchanged)
agent.{job_id}.results / .input / .messages / .tasks / .close

# KV keys: add tenant prefix for snapshots
agent-init.{job_id}                                    # unchanged
snapshots.{tenant_id}.{coworker_folder}.tasks          # add tenant prefix
snapshots.{tenant_id}.{coworker_folder}.groups         # add tenant prefix
\`\`\`

## Data Migration Script

\`\`\`python
async def migrate_to_multi_tenant():
    \"\"\"Migrate existing single-tenant data to new multi-tenant schema.\"\"\"
    # 1. Create default tenant
    tenant = await db.create_tenant(slug="default", name="Default Tenant")

    # 2. Create default role
    role = await db.create_role(
        tenant_id=tenant.id, name="general",
    )

    # 3. For each registered_group -> coworker + channel_binding + conversation
    for jid, group in (await db.get_all_registered_groups_legacy()).items():
        coworker = await db.create_coworker(
            tenant_id=tenant.id, role_id=role.id,
            name=group.name, folder=group.folder, is_admin=group.is_main,
        )
        channel_type = _infer_channel_type(jid)
        binding = await db.create_channel_binding(
            coworker_id=coworker.id, tenant_id=tenant.id,
            channel_type=channel_type,
            credentials=_get_current_credentials(channel_type),
        )
        conversation = await db.create_conversation(
            tenant_id=tenant.id, coworker_id=coworker.id,
            channel_binding_id=binding.id,
            channel_chat_id=_extract_chat_id(jid),
            requires_trigger=group.requires_trigger,
        )
        old_session = await db.get_session_legacy(group.folder)
        if old_session:
            await db.set_session_new(conversation.id, tenant.id, coworker.id, old_session)

    # 4. Move groups/{folder}/ -> data/tenants/{tid}/coworkers/{folder}/workspace/
\`\`\`

## Files Summary

### New Files

| File | Content |
|------|---------|
| \`src/nanoclaw/core/orchestrator_state.py\` | \`CoworkerConfig\`, \`ConversationState\`, \`CoworkerState\`, \`OrchestratorState\` |
| \`src/nanoclaw/channels/gateway.py\` | \`ChannelGateway\` Protocol |
| \`src/nanoclaw/channels/telegram_gateway.py\` | \`TelegramGateway\` (manages multiple bots) |
| \`src/nanoclaw/channels/slack_gateway.py\` | \`SlackGateway\` |
| \`scripts/migrate_to_multi_tenant.py\` | Data + filesystem migration |

### Modified Files

| File | Changes |
|------|---------|
| \`db/pg.py\` | **Large**: 6 new tables; sessions/messages/tasks add coworker_id/conversation_id; all functions: DEFAULT_TENANT -> tenant_id param with default; CRUD for new entities |
| \`db/__init__.py\` | Re-export new functions |
| \`core/types.py\` | **Large**: new dataclasses (Tenant, Role, Coworker, ChannelBinding, Conversation, User); RegisteredGroup deprecated with converter |
| \`core/config.py\` | Add GLOBAL_MAX_CONTAINERS; remove ASSISTANT_NAME, TRIGGER_PATTERN |
| \`main.py\` | **Large**: globals -> OrchestratorState; routing via binding -> conversation -> coworker; init gateways |
| \`container/runner.py\` | **Medium**: paths change to tenants/{tid}/coworkers/{folder}/; build_volume_mounts takes tenant_id + conversation_id |
| \`container/scheduler.py\` | **Medium**: 3-level concurrency; queue key = coworker_id |
| \`agent/executor.py\` | **Small**: AgentInput adds tenant_id, coworker_id |
| \`agent/container_executor.py\` | **Small**: pass tenant_id, coworker_id to NATS KV |
| \`ipc/protocol.py\` | **Small**: AgentInitData adds tenant_id, coworker_id |
| \`channels/telegram.py\` | **Large**: refactor to TelegramGateway |
| \`channels/slack.py\` | **Large**: refactor to SlackGateway |
| \`channels/registry.py\` | **Medium**: static -> dynamic gateway management |
| \`orchestration/task_scheduler.py\` | **Medium**: tasks reference coworker_id + conversation_id |
| \`security/sender_allowlist.py\` | **Medium**: tenant-scoped check using users table |
| \`agent_runner/ipc_mcp.py\` | **Medium**: \`register_group\` MCP tool → \`register_conversation\`; \`refresh_groups\` adapts to new model |
| \`ipc/task_handler.py\` | **Medium**: \`register_group\` handler → \`register_conversation\` handler; creates conversation under current coworker's channel_binding |

## MCP Tool Adaptation: register_group → register_conversation

The current \`register_group\` MCP tool lets the admin agent register a new chat by providing a JID, name, folder, and trigger. In the multi-tenant model, this becomes \`register_conversation\` — simpler because the coworker and channel_binding are already known (they're the current agent's context).

\`\`\`python
# Before (register_group): agent must specify everything
register_group(jid="tg:12345", name="Sales Team", folder="sales", trigger="@Andy")
# → creates a new RegisteredGroup row

# After (register_conversation): only the chat ID is needed
register_conversation(channel_chat_id="-1001234567", name="Sales Team")
# → creates a new Conversation under the current coworker's channel_binding
# → coworker_id, tenant_id, channel_binding_id all derived from the agent's context
# → requires_trigger defaults to TRUE (group chat)
# → trigger text derived from coworker.name
\`\`\`

Container-side (\`agent_runner/ipc_mcp.py\`):
\`\`\`python
@tool("register_conversation", "Register a new chat group for this coworker.", {"channel_chat_id": str, "name": str})
async def register_conversation(args):
    _publish(f"agent.{job_id}.tasks", {
        "type": "register_conversation",
        "channel_chat_id": args["channel_chat_id"],
        "name": args.get("name", ""),
        "groupFolder": group_folder,  # used to identify the coworker
        "timestamp": datetime.now().isoformat(),
    })
    return _text_result("Conversation registered.")
\`\`\`

Orchestrator-side (\`ipc/task_handler.py\`):
\`\`\`python
elif task_type == "register_conversation":
    if not is_admin:  # only admin coworker can register new conversations
        return
    # Find the coworker and its channel_binding from source context
    coworker = await db.get_coworker_by_folder(tenant_id, source_group)
    binding = await db.get_channel_binding(coworker.id, channel_type)
    await db.create_conversation(
        tenant_id=tenant_id,
        coworker_id=coworker.id,
        channel_binding_id=binding.id,
        channel_chat_id=data["channel_chat_id"],
        name=data.get("name"),
        requires_trigger=True,  # group chat default
    )
\`\`\`

Similarly, \`refresh_groups\` becomes \`refresh_conversations\` — the admin coworker can request a re-sync of available chats from the channel gateway.

## Acceptance Criteria

### Data Model
- [ ] \`tenants\` table with \`max_concurrent_containers\` and \`last_message_cursor\`
- [ ] \`users\` table (reserved for future permission control)
- [ ] \`roles\` table (clean: name + agent_backend + system_prompt + tools + skills only — no role_type, no a2a_config, no authorization, no config_overrides)
- [ ] \`coworkers\` table with \`role_id\`, \`folder\`, \`max_concurrent\`, \`created_at\`
- [ ] \`channel_bindings\` table with \`created_at\`
- [ ] \`conversations\` table with \`last_agent_invocation\`, \`requires_trigger\` (no \`is_main\`, no \`trigger_pattern\` — trigger text derived from \`coworker.name\`)
- [ ] \`sessions\` rewritten: PK = \`conversation_id\`, all UUID types, no \`group_folder\`
- [ ] \`messages\` rewritten: \`conversation_id\` replaces \`chat_jid\`, all UUID/TIMESTAMPTZ types
- [ ] \`scheduled_tasks\` rewritten: \`coworker_id\` + \`conversation_id\`, all UUID/TIMESTAMPTZ types
- [ ] \`task_run_logs\`: UUID \`tenant_id\`, TIMESTAMPTZ \`run_at\`, \`result\` truncated to 500 chars
- [ ] Legacy tables dropped: \`router_state\`, \`registered_groups\`, \`chats\`
- [ ] All \`tenant_id\` columns are UUID (not TEXT) — consistent with \`tenants.id\`
- [ ] All time fields are TIMESTAMPTZ (not TEXT)
- [ ] Migration script converts existing data to new schema

### Runtime
- [ ] \`OrchestratorState\` replaces module-level globals
- [ ] \`CoworkerConfig\` merges Role + Coworker config
- [ ] Three-level concurrency: global + per-tenant + per-coworker
- [ ] \`ASSISTANT_NAME\` and \`TRIGGER_PATTERN\` globals removed

### Channels
- [ ] \`ChannelGateway\` Protocol defined
- [ ] \`TelegramGateway\` manages multiple bots
- [ ] \`SlackGateway\` manages multiple apps
- [ ] Routing: binding_id -> conversation -> coworker

### Agent Execution
- [ ] \`AgentInput\` includes \`tenant_id\`, \`coworker_id\`
- [ ] Volume mounts: \`data/tenants/{tid}/coworkers/{folder}/\`
- [ ] Session dir per-conversation, workspace per-coworker
- [ ] NATS KV snapshots with tenant prefix

### MCP Tools
- [ ] \`register_group\` MCP tool renamed to \`register_conversation\` — creates conversation under current coworker's binding
- [ ] \`register_conversation\` only needs \`channel_chat_id\` and optional \`name\` (coworker/tenant/binding derived from context)
- [ ] \`refresh_groups\` adapted to \`refresh_conversations\`
- [ ] Authorization: only admin coworker (\`is_admin=True\`) can register new conversations
- [ ] \`ipc/task_handler.py\` handles \`register_conversation\` task type

### DB & Compat
- [ ] All db functions: \`tenant_id\` param with DEFAULT_TENANT default
- [ ] CRUD for all new entities
- [ ] \`RegisteredGroup\` deprecated with converter function
- [ ] \`ruff check . && mypy --strict src/nanoclaw && pytest\` pass

## Important Notes

- **Working directory**: Project root (\`py/\` directory)
- **DEFAULT_TENANT stays as default param** — \`tenant_id: str = DEFAULT_TENANT\` for single-tenant compat
- **Gateway pattern**: One gateway per channel TYPE managing multiple bots. NOT one gateway per bot.
- **Session per-conversation, workspace per-coworker** — deliberate design. Same coworker shares files across groups, independent conversation memory.
- **RegisteredGroup backward compat**: Provide converter, remove in follow-up.
- **Bot identity**: Each coworker has own bot per channel type. Users @mention different bots in same group.
- **No RLS** — tenant isolation via WHERE clauses. RLS is future safety net.
- **Filesystem migration**: Move \`groups/{folder}/\` to \`data/tenants/default/coworkers/{folder}/workspace/\`
- **MCP tool rename**: \`register_group\` → \`register_conversation\`, \`refresh_groups\` → \`refresh_conversations\`. The new tools are simpler — coworker/tenant/binding context is known, only \`channel_chat_id\` is needed.
- **Schema is clean**: No placeholder JSONB fields without defined purpose. Fields for future features (a2a, authorization, RLS) are added when those features are implemented, not before.
- **Branch**: Create from \`python-rewrite\` branch

## Known Pitfalls (from prior implementation attempt)

These issues were discovered during a previous implementation of this step. Read carefully to avoid repeating them.

### P1: Token Deduplication in Gateway

**Trap**: Creating one polling instance per \`channel_binding\` row. After migration, multiple coworkers share the same bot token (legacy single-bot setup). Two polling instances on the same Telegram token causes \`Conflict: terminated by other getUpdates request\`.

**Rule**: \`TelegramGateway\` must deduplicate by token — one token = one polling connection. Messages are dispatched to ALL bindings associated with that token.

### P2: \`channel_chat_id\` Is NOT a Unique Routing Key

**Trap**: Using \`channel_chat_id\` (e.g., Telegram user ID) as dict key or queue key. In Telegram private chats, the same user talking to 3 different bots produces the same \`chat_id\`. Three coworkers' conversation dicts collide.

**Rule**: Always use \`conversation_id\` (UUID) as the internal routing key. \`channel_chat_id\` is an external identifier used only for lookup in combination with \`channel_binding_id\`.

### P3: Inbound Message Filtering in Multi-Bot Groups

**Trap**: Storing all incoming messages without checking relevance. When 3 bots are in the same Telegram group, every bot receives ALL messages. \`@CS_Bot hello\` gets stored in Ops Bot's conversation, and if Ops Bot's trigger later matches something, it activates on irrelevant context.

**Rule**: For group chats with \`requires_trigger=TRUE\`, check trigger BEFORE storing. If the message doesn't match this coworker's name, drop it entirely — do not store, do not enqueue.

### P4: IPC Reply Routing Must Use Coworker Context

**Trap**: \`send_message\` MCP tool sends a \`chatJid\`; Orchestrator scans all coworkers for a matching binding. In private chats, multiple coworkers share the same \`chatJid\`, and the reply goes through the wrong bot.

**Rule**: IPC message handler must use the source coworker's own binding for reply routing. Never scan all bindings by \`chatJid\`.

### P5: Dedup Between \`send_message\` and Results Stream

**Trap**: Agent sends real-time progress via \`send_message\` MCP tool (Channel 4), and the same text appears again in \`ResultMessage\` (Channel 2). User receives duplicate messages.

**Rule**: Track texts sent via \`send_message\` IPC; when processing \`ResultMessage\`, skip if the same text was already delivered via IPC.

### P6: Event-Driven Inbound, Not Poll-Driven

**Trap**: Relying on \`tenants.last_message_cursor\` polling to discover new messages. When multiple Slack apps receive the same group message at microsecond-level differences, one app's cursor advancement causes the other app's message to be permanently skipped.

**Rule**: \`_handle_incoming\` must immediately \`enqueue_message_check(conversation_id)\` after storing. Do NOT rely on polling to discover new messages.

### P7: Schema Migration Must Detect Legacy Tables

**Trap**: \`init_database()\` runs on an existing Step 4 database. \`CREATE TABLE IF NOT EXISTS\` won't modify existing tables, but new indexes referencing new columns fail on old schemas.

**Rule**: Detect legacy schema (e.g., check for \`chat_jid\` column in \`messages\`). If legacy, skip new-format creation until migration script runs. Migration: read old data → drop old tables → create new tables → write data.

### P8: Migration Coworker Name Must Be ASSISTANT_NAME

**Trap**: Using \`group.name\` (e.g., \`"all-rolemesh"\`) as \`coworker.name\`. Trigger pattern derived from \`coworker.name\` won't match the existing \`@Andy\` triggers in chat history.

**Rule**: Migration must use the global \`ASSISTANT_NAME\` (e.g., \`"Andy"\`) as \`coworker.name\`, not the group display name.

### P9: Migrate .claude Session Data Too

**Trap**: Only migrating \`groups/{folder}/\` → \`workspace/\`. Forgetting \`data/sessions/{folder}/.claude/\` → session directory. Container reports \`.claude.json not found\` on startup.

**Rule**: Migration must move BOTH workspace files AND \`.claude\` session data to the new directory structure.

### P10: NATS Startup Cleanup

**Trap**: After an unclean process exit, stale durable consumers block new subscriptions. Old messages in NATS stream get replayed on restart, flooding users with historical messages.

**Rule**: On startup, \`delete_consumer\` for known durables + \`purge_stream\` to clear old messages.
ISSUE_EOF
)"
```

### 启动 Claude Code 执行

Issue 创建后，记下 Issue 编号，新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view 12 --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
分支：从 python-rewrite 创建新分支 step5/multi-tenant
完成后提交代码并创建 PR。
```

> 替换为实际 Issue 编号。

---

## Step 5.1：合并 Role 到 Coworker（去除过度设计）

**目标**：删除 `roles` 表，将其字段合并到 `coworkers` 表。Role 的"模板复用"能力当前没有代码使用，属于过度设计。

**前置条件**：Step 5 完成。在 `step5/multi-tenant` 分支上直接做。

**改动范围**：~200 行改动，涉及 6 个文件。

### 创建 Issue 命令

```bash
gh issue create \
  --title "refactor: merge roles table into coworkers (remove over-engineering)" \
  --label "refactor,python-rewrite" \
  --body "$(cat <<'ISSUE_EOF'
## Context

Step 5 introduced a \`roles\` table as an AI agent template layer: Role defines (system_prompt, tools, skills, agent_backend), Coworker inherits from Role and optionally overrides. This was designed for a scenario where 5 operations AI coworkers share the same prompt configuration.

In practice, **no code uses the template-reuse capability**:
- There is no UI to "create coworker from role"
- \`CoworkerConfig\` merges Role + Coworker at load time, then Role is never accessed again
- Migration creates a single \`"general"\` role that all coworkers point to — no differentiation
- Every coworker creation requires a role to exist first, adding unnecessary ceremony

The indirection adds complexity (extra table, extra JOIN, extra CRUD functions, \`role_id\` FK) with zero current benefit. If template reuse is needed later, adding the Role table back is a one-day task.

**This refactoring runs on the \`step5/multi-tenant\` branch directly.**

## Changes

### 1. Database Schema

**Drop \`roles\` table. Add its columns to \`coworkers\`:**

\`\`\`sql
-- After (one table, no roles):
CREATE TABLE coworkers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    name TEXT NOT NULL,                           -- coworker display name (trigger derived from this)
    folder TEXT NOT NULL,
    agent_backend TEXT DEFAULT 'claude-code',      -- moved from roles
    system_prompt TEXT,                            -- moved from roles
    tools JSONB DEFAULT '[]',                      -- moved from roles
    skills JSONB DEFAULT '[]',                     -- moved from roles
    is_admin BOOLEAN DEFAULT FALSE,
    container_config JSONB,
    max_concurrent INT DEFAULT 2,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, folder)
);
\`\`\`

**Migration** (for existing Step 5 databases):
\`\`\`sql
-- Merge role fields into coworkers
ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS agent_backend TEXT DEFAULT 'claude-code';
ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS system_prompt TEXT;
ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS tools JSONB DEFAULT '[]';
ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS skills JSONB DEFAULT '[]';

UPDATE coworkers SET
    agent_backend = r.agent_backend,
    system_prompt = r.system_prompt,
    tools = r.tools,
    skills = r.skills
FROM roles r WHERE coworkers.role_id = r.id;

ALTER TABLE coworkers DROP COLUMN IF EXISTS role_id;
DROP TABLE IF EXISTS roles;
\`\`\`

### 2. \`src/nanoclaw/core/types.py\`

**Delete** \`Role\` dataclass entirely.

**Update** \`Coworker\` dataclass — add fields from Role, remove \`role_id\`:

\`\`\`python
@dataclass
class Coworker:
    id: str
    tenant_id: str
    name: str
    folder: str
    agent_backend: str = "claude-code"
    system_prompt: str | None = None
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    is_admin: bool = False
    container_config: ContainerConfig | None = None
    max_concurrent: int = 2
    status: str = "active"
    created_at: str = ""
\`\`\`

**Update** \`registered_group_to_coworker()\` — remove \`role_id\` parameter.

### 3. \`src/nanoclaw/core/orchestrator_state.py\`

**Update** \`CoworkerConfig\` docstring from "merged from Role + Coworker" to "loaded from coworkers table".

No field changes — CoworkerConfig already has all the right fields.

### 4. \`src/nanoclaw/main.py\`

**Remove**:
- Imports: \`create_role\`, \`get_roles_for_tenant\`
- Role loading loop in \`_load_multi_tenant_state()\`
- \`roles_by_id\` dict
- Role-Coworker merge logic when building \`CoworkerConfig\`
- \`create_role()\` call in migration

**Simplify** CoworkerConfig building — read directly from Coworker fields:
\`\`\`python
config = CoworkerConfig(
    id=cw.id,
    tenant_id=cw.tenant_id,
    name=cw.name,
    folder=cw.folder,
    system_prompt=cw.system_prompt,
    agent_backend=cw.agent_backend,
    tools=cw.tools,
    skills=cw.skills,
    trigger_pattern=CoworkerConfig.build_trigger_pattern(cw.name),
    container_image=None,
    max_concurrent=cw.max_concurrent,
    is_admin=cw.is_admin,
)
\`\`\`

**Update** coworker creation — remove \`role_id\`, add config fields directly.

### 5. \`src/nanoclaw/db/pg.py\`

- **Delete** \`roles\` table DDL from \`_create_schema()\`
- **Delete** \`create_role()\` and \`get_roles_for_tenant()\` functions
- **Update** \`coworkers\` DDL — add \`agent_backend\`, \`system_prompt\`, \`tools\`, \`skills\`; remove \`role_id\` FK
- **Update** \`create_coworker()\` signature — remove \`role_id\`, add new fields
- **Update** \`_row_to_coworker()\` — read new columns, remove \`role_id\`
- **Add** migration detection: if \`roles\` table exists, run ALTER + merge + DROP

### 6. \`src/nanoclaw/db/__init__.py\`

Remove re-exports: \`create_role\`, \`get_roles_for_tenant\`, \`Role\`.

### 7. Tests

Update tests that create roles before coworkers — pass config fields directly to \`create_coworker()\`.

## Acceptance Criteria

- [ ] \`roles\` table dropped from schema
- [ ] \`Role\` dataclass deleted from \`core/types.py\`
- [ ] \`create_role()\` and \`get_roles_for_tenant()\` deleted from \`db/pg.py\`
- [ ] \`Coworker\` dataclass has \`agent_backend\`, \`system_prompt\`, \`tools\`, \`skills\` fields
- [ ] \`Coworker\` dataclass has no \`role_id\` field
- [ ] \`coworkers\` table has new columns, no \`role_id\`
- [ ] \`main.py\` builds \`CoworkerConfig\` directly from \`Coworker\` (no role merging)
- [ ] Migration handles both fresh install and existing Step 5 database
- [ ] All tests pass
- [ ] \`ruff check . && mypy --strict src/nanoclaw && pytest\` pass
- [ ] No references to \`Role\`, \`role_id\`, \`create_role\`, or \`get_roles_for_tenant\` in production code

## Important Notes

- **Branch**: Work directly on \`step5/multi-tenant\` (not a new branch)
- **This is a simplification** — behavior is identical, fewer tables and concepts
- **If template reuse is needed later**, adding \`roles\` table back is straightforward
- **Migration must handle two cases**: (a) fresh install — merged schema; (b) existing Step 5 DB — ALTER TABLE migration
ISSUE_EOF
)"
```

### 启动 Claude Code 执行

Issue 创建后，记下 Issue 编号，新开 Claude Code 会话，粘贴以下内容：

```
请读取 GitHub Issue 并按要求完成任务：

$(gh issue view 14 --json title,body --jq '"# " + .title + "\n\n" + .body')

工作目录：py/
分支：直接在 step5/multi-tenant 上工作（不创建新分支）
完成后提交代码。
```

> 替换为实际 Issue 编号。

---

## Step 6：创建 RoleMesh 独立 Repo

**目标**：将 `py/` 目录中的代码独立为新的 GitHub repo `RoleMesh`，所有 NanoClaw 引用替换为 RoleMesh，保持两个分支：`main`（来自 python-rewrite）和 `feat/multi-tenant`（来自 step5/multi-tenant）。

**前置条件**：Step 5 和 Step 5.1 完成。

**这是手动操作步骤，不需要 Claude Code 执行。**

### 准备工作

```bash
# 确保两个分支都是最新的
cd /home/jerry/ai/nanoclaw-worktree/nanoclaw
git checkout python-rewrite && git pull
git checkout step5/multi-tenant && git pull
```

### 第 1 步：创建 RoleMesh Repo

```bash
# 在 GitHub 上创建空 repo
gh repo create Jerryguan777/rolemesh --private --description "AI Coworker Platform" --clone
cd /home/jerry/ai/rolemesh
```

### 第 2 步：从 python-rewrite 构建 main 分支

```bash
# 回到 nanoclaw repo，切到 python-rewrite
cd /home/jerry/ai/nanoclaw-worktree/nanoclaw
git checkout python-rewrite

# 复制 py/ 内容到 rolemesh repo 根目录
cp -r py/* /home/jerry/ai/rolemesh/
cp py/.gitignore /home/jerry/ai/rolemesh/ 2>/dev/null
# 注意：不要复制 py/.venv/、py/__pycache__/ 等

cd /home/jerry/ai/rolemesh
```

### 第 3 步：全局替换 NanoClaw → RoleMesh（main 分支）

**3a. 文件内容替换**（三种大小写）：

```bash
# 查找所有需要替换的文本文件
find . -type f \( \
    -name '*.py' -o -name '*.md' -o -name '*.toml' -o -name '*.yml' -o \
    -name '*.yaml' -o -name '*.sh' -o -name '*.json' -o -name '*.cfg' -o \
    -name '*.txt' -o -name 'Dockerfile*' -o -name '*.lock' \
\) -not -path './.venv/*' -not -path './__pycache__/*' -not -path './.git/*' \
  -exec grep -l -i 'nanoclaw' {} \;

# 执行替换（三种大小写）
find . -type f \( \
    -name '*.py' -o -name '*.md' -o -name '*.toml' -o -name '*.yml' -o \
    -name '*.yaml' -o -name '*.sh' -o -name '*.json' -o -name '*.cfg' -o \
    -name '*.txt' -o -name 'Dockerfile*' \
\) -not -path './.venv/*' -not -path './__pycache__/*' -not -path './.git/*' \
  -exec sed -i \
    -e 's/NanoClaw/RoleMesh/g' \
    -e 's/nanoclaw/rolemesh/g' \
    -e 's/Nanoclaw/Rolemesh/g' \
    -e 's/NANOCLAW/ROLEMESH/g' \
    {} \;
```

**3b. 重命名 Python 包目录**：

```bash
mv src/nanoclaw src/rolemesh
```

**3c. 重命名其他路径中的引用**（如果有）：

```bash
# 检查是否有其他目录/文件名包含 nanoclaw
find . -iname '*nanoclaw*' -not -path './.git/*' -not -path './.venv/*'
# 逐个 mv 重命名
```

**3d. 更新 uv.lock**（重新生成）：

```bash
# uv.lock 中包含包名，直接重新生成更安全
rm uv.lock
uv lock
```

**3e. 验证替换完整**：

```bash
# 确认没有遗漏
grep -r -i 'nanoclaw' --include='*.py' --include='*.md' --include='*.toml' \
  --exclude-dir=.venv --exclude-dir=.git --exclude-dir=__pycache__ .

# 应该返回空（没有结果）
# 如果有遗漏，手动修复
```

**3f. 验证代码能运行**：

```bash
uv pip install -e ".[dev]"
ruff check .
ruff format --check .
mypy --strict src/rolemesh
pytest
```

### 第 4 步：提交并推送 main

```bash
git add -A
git commit -s -m "initial: RoleMesh — AI Coworker Platform (from NanoClaw python-rewrite)"
git push -u origin main
```

### 第 5 步：从 step5/multi-tenant 构建 feat/multi-tenant 分支

```bash
# 在 rolemesh repo 中创建新分支
git checkout -b feat/multi-tenant

# 清理当前内容（保留 .git）
find . -maxdepth 1 -not -name '.git' -not -name '.' -exec rm -rf {} \;

# 从 nanoclaw 的 step5/multi-tenant 复制
cd /home/jerry/ai/nanoclaw-worktree/nanoclaw
git checkout step5/multi-tenant

cp -r py/* /home/jerry/ai/rolemesh/
cp py/.gitignore /home/jerry/ai/rolemesh/ 2>/dev/null

cd /home/jerry/ai/rolemesh
```

### 第 6 步：全局替换 NanoClaw → RoleMesh（feat/multi-tenant 分支）

```bash
# 和第 3 步完全相同的替换操作
find . -type f \( \
    -name '*.py' -o -name '*.md' -o -name '*.toml' -o -name '*.yml' -o \
    -name '*.yaml' -o -name '*.sh' -o -name '*.json' -o -name '*.cfg' -o \
    -name '*.txt' -o -name 'Dockerfile*' \
\) -not -path './.venv/*' -not -path './__pycache__/*' -not -path './.git/*' \
  -exec sed -i \
    -e 's/NanoClaw/RoleMesh/g' \
    -e 's/nanoclaw/rolemesh/g' \
    -e 's/Nanoclaw/Rolemesh/g' \
    -e 's/NANOCLAW/ROLEMESH/g' \
    {} \;

# 重命名包目录
mv src/nanoclaw src/rolemesh

# 检查其他路径
find . -iname '*nanoclaw*' -not -path './.git/*' -not -path './.venv/*'

# 重新生成 lock
rm uv.lock
uv lock

# 验证没有遗漏
grep -r -i 'nanoclaw' --include='*.py' --include='*.md' --include='*.toml' \
  --exclude-dir=.venv --exclude-dir=.git --exclude-dir=__pycache__ .

# 验证代码
uv pip install -e ".[dev]"
ruff check .
mypy --strict src/rolemesh
pytest
```

### 第 7 步：提交并推送 feat/multi-tenant

```bash
git add -A
git commit -s -m "feat: multi-tenant multi-coworker architecture"
git push -u origin feat/multi-tenant
```

### 第 8 步：验证 diff 正确

```bash
# 查看 main → feat/multi-tenant 的差异，确认是 step5 的改动
git diff main..feat/multi-tenant --stat

# 应该看到和 nanoclaw 中 python-rewrite → step5/multi-tenant 相同的文件变化
# （只是路径从 nanoclaw → rolemesh）
```

### 替换清单（需要人工检查的特殊位置）

以下位置可能需要手动确认替换是否正确（sed 全局替换可能不够精确）：

| 位置 | 检查项 |
|------|--------|
| `pyproject.toml` | `name = "rolemesh"`、`rolemesh = "rolemesh.main:main_sync"` |
| `src/rolemesh/__init__.py` | 包名正确 |
| `src/agent_runner/` | import 路径改为 `from rolemesh.xxx` |
| `container/Dockerfile` | 如果引用了包名 |
| `docker-compose.dev.yml` | 服务名（如果有） |
| `CLAUDE.md` | 项目名描述 |
| `docs/*.md` | 所有文档中的项目名 |
| `tests/` | import 路径 |
| `container/runner.py` | 容器名前缀 `nanoclaw-` → `rolemesh-` |
| `container/scheduler.py` | orphan cleanup 前缀 |
| `STEPS.md` | 整个文件（但此文件可能不需要复制到新 repo——它是 nanoclaw 的演进记录） |

### 不应复制到新 Repo 的文件

| 文件 | 原因 |
|------|------|
| `STEPS.md` | NanoClaw 的演进步骤记录，属于 nanoclaw repo |
| `py/.venv/` | 虚拟环境，不提交 |
| `py/__pycache__/` | 缓存 |
| `py/store/` | 运行时数据 |
| `py/data/` | 运行时数据 |
| `py/groups/` | 旧的运行时数据 |
| `py/.mypy_cache/` | 缓存 |

### 最终 Repo 结构

```
rolemesh/                        ← repo root（原 py/）
├── pyproject.toml               # name = "rolemesh"
├── uv.lock
├── ruff.toml
├── CLAUDE.md                    # 项目说明改为 RoleMesh
├── README.md                    # 新写（或从 nanoclaw 改编）
├── docker-compose.dev.yml
├── src/
│   ├── rolemesh/                # 原 nanoclaw/
│   │   ├── core/
│   │   ├── db/
│   │   ├── channels/
│   │   ├── security/
│   │   ├── container/
│   │   ├── ipc/
│   │   ├── agent/
│   │   ├── orchestration/
│   │   └── main.py
│   └── agent_runner/            # 不改名（和 rolemesh 无关）
├── container/
│   ├── Dockerfile
│   └── build.sh
├── scripts/
├── tests/
└── docs/
    ├── multi-tenant-architecture.md
    ├── nats-ipc-architecture.md
    ├── agent-executor-and-container-runtime.md
    └── ppi-integration-guide.md
```
