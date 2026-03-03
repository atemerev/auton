# Auton

Agent runtime for long-running autonomous agents with built-in oversight.

**Auton manages agent lifecycle the way MCP manages tools** — spawn, observe, correct, suspend, terminate, checkpoint, and fork agents through a simple HTTP + SSE protocol.

## Why

LLM agents that run for hours or days need infrastructure that current frameworks don't provide:

- **Lifecycle management** — spawn agents, restart on failure, enforce budgets
- **Supervision trees** — OTP-style parent-child relationships with restart policies
- **Behavioral oversight** — drift detection, loop detection, coherence monitoring
- **Live observation** — SSE streams of agent health and event logs

## Quick Start

```bash
git clone https://github.com/atemerev/auton.git
cd auton
uv sync
uv run python main.py
```

Server starts on `http://localhost:8420`. API docs at `http://localhost:8420/docs`.

## Protocol

### Agent Tree Navigation

Agents form a filesystem-like tree. Navigate with paths:

```
GET  /agents                    → full agent tree
GET  /agents/{path...}          → subtree at path
```

### Lifecycle

```
POST   /agents                         → spawn root agent
POST   /agents/{parent}/               → spawn under parent
DELETE /agents/{path...}               → terminate (cascades to children)
POST   /agents/{path...}/checkpoint    → save snapshot
POST   /agents/{path...}/fork          → fork from latest checkpoint
POST   /agents/{path...}/restart       → restart from snapshot
```

### Oversight & Control

```
PATCH  /agents/{path...}              → correct (inject guidance)
POST   /agents/{path...}/suspend      → manual suspend
POST   /agents/{path...}/resume       → resume from suspended
POST   /oversight/check               → run oversight on all agents
```

### Communication & Observation

```
POST   /agents/{path...}/message      → send message to agent
GET    /agents/{path...}/observe      → SSE health stream
GET    /agents/{path...}/log          → SSE event log stream
```

## State Machine

```
spawning ──► running ◄──► idle
                │              │
                ▼              │
           correcting ────────┘
                │
                ▼
           suspended
                │
                ▼
          terminating ──► dead
```

Any state can transition to `terminating` (explicit kill).
Suspended agents can be resumed back to `running`.

### Suspension Reasons

- `budget_exceeded` — token or runtime budget hit
- `drift_detected` — agent drifted from original goal
- `loop_detected` — repeated action pattern detected
- `depth_exceeded` — child spawning too deep
- `manual` — operator-initiated

## Examples

### Spawn an agent

```bash
curl -X POST http://localhost:8420/agents \
  -H "Content-Type: application/json" \
  -d '{
    "id": "researcher",
    "spec": {
      "goal": "Monitor arXiv daily for papers on agent architectures",
      "model": "claude-sonnet-4-6",
      "tools": ["web_search", "fetch_webpage"]
    },
    "policy": {
      "restart": "on_failure",
      "max_restarts": 3,
      "budget": {"max_tokens_per_hour": 5000},
      "drift_threshold": 0.4,
      "max_children": 5,
      "max_depth": 2
    }
  }'
```

### Spawn a child agent

```bash
curl -X POST http://localhost:8420/agents/researcher \
  -H "Content-Type: application/json" \
  -d '{
    "id": "summarizer",
    "spec": {
      "goal": "Summarize papers found by parent",
      "model": "claude-haiku-4-5"
    }
  }'
```

### Observe agent health (SSE)

```bash
curl -N http://localhost:8420/agents/researcher/observe
```

### Correct a drifting agent

```bash
curl -X PATCH http://localhost:8420/agents/researcher \
  -H "Content-Type: application/json" \
  -d '{"guidance": "Focus only on papers about memory architectures, not general ML"}'
```

### Checkpoint and fork

```bash
# Save state
curl -X POST http://localhost:8420/agents/researcher/checkpoint

# Fork for a different direction
curl -X POST http://localhost:8420/agents/researcher/fork
```

### Suspend and resume

```bash
curl -X POST http://localhost:8420/agents/researcher/suspend \
  -H "Content-Type: application/json" \
  -d '{"reason": "manual"}'

curl -X POST http://localhost:8420/agents/researcher/resume
```

## Oversight Engine

The oversight engine runs periodic checks on all agents:

| Check | Threshold | Action |
|-------|-----------|--------|
| Token budget | `budget.max_total_tokens` | Auto-suspend |
| Token rate | `budget.max_tokens_per_hour` | Warn |
| Runtime | `budget.max_runtime_seconds` | Auto-suspend |
| Goal drift | `policy.drift_threshold` | Auto-suspend |
| Loop detection | 3+ repeated patterns | Warn → suspend |
| Coherence | < 0.3 | Auto-suspend |

Trigger manually:

```bash
curl -X POST http://localhost:8420/oversight/check
```

Or observe via SSE — heartbeat events include oversight results every 30s.

## Supervision Policies

OTP-inspired restart strategies:

| Policy | Behavior |
|--------|----------|
| `never` | Don't restart on failure |
| `on_failure` | Restart up to `max_restarts` times |
| `always` | Always restart |

| Strategy | Behavior |
|----------|----------|
| `one_for_one` | Only restart the failed child |
| `one_for_all` | Restart all children if one fails |
| `escalate` | Propagate failure to parent |

## Architecture

```
auton/
├── models.py     # State machine, specs, policies, AgentNode
├── registry.py   # Agent tree with path-based navigation
├── oversight.py  # Health checks, drift/loop/budget detection
└── api.py        # FastAPI HTTP + SSE endpoints
```

## Relationship to MCP

MCP defines how agents call tools. Auton defines how agents are born, supervised, and die. They are complementary:

- **MCP** = agent ↔ tools (what an agent *can do*)
- **Auton** = agent lifecycle (how agents are *managed*)

## License

MIT
