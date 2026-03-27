"""IPC layer -- NATS-based messaging between Orchestrator and Agent."""

from nanoclaw.ipc.nats_transport import NatsTransport
from nanoclaw.ipc.protocol import AgentInitData, IpcEnvelope
from nanoclaw.ipc.task_handler import IpcDeps, process_task_ipc

__all__ = ["AgentInitData", "IpcDeps", "IpcEnvelope", "NatsTransport", "process_task_ipc"]
