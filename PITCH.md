# Auton — Pitch Deck

## The One-Liner

**Auton is OTP for AI agents** — a runtime that manages agent lifecycle the way Erlang manages processes: spawn, supervise, correct, checkpoint, fork, kill.

---

## Slide 1: The Problem

### Agents are getting longer-running. Nobody's managing them.

The industry is converging on autonomous agents that run for hours, days, or indefinitely. But the infrastructure assumes request-response:

- **No lifecycle management.** Spawn an agent, lose track of it. No restart-on-failure, no cascading shutdown, no checkpointing.
- **No budget control.** An agent with a credit card and a loop can burn $10K in an hour. Current "guardrails" are prompt-level — trivially bypassed by context drift.
- **No oversight at runtime.** You can't observe what a long-running agent is doing, detect when it's drifting from its goal, or correct it without killing it.
- **No hierarchy.** Agents that spawn sub-agents create unmanaged trees. One rogue child = cascading failure with no supervision.

Every serious agent deployment reinvents these primitives. Badly.

---

## Slide 2: The Insight

### This problem was solved in 1986. For processes.

Erlang/OTP solved exactly this for telecom systems:
- Processes organized in supervision trees
- Restart policies (one-for-one, one-for-all, escalate)
- Let it crash + automatic recovery
- Observable, correctable, budget-aware

**Agents are the new processes.** They need the same infrastructure. Not another framework — a *runtime*.

---

## Slide 3: The Solution

### Auton: Agent Lifecycle Runtime

Auton manages agents the way MCP manages tools — through a simple HTTP + SSE protocol.

**What it does:**

| Capability | How |
|---|---|
| **Spawn & kill** | `POST /agents` → agent starts. `DELETE /agents/path` → cascading termination. |
| **Supervision trees** | Parent-child hierarchies with OTP restart policies (never / on_failure / always). |
| **Budget enforcement** | Token budgets, runtime limits, rate caps. Escalating warnings → forced finalization → hard stop. |
| **Live oversight** | Drift detection, loop detection, coherence monitoring. Auto-suspend on anomaly. |
| **Observe & correct** | SSE streams for health + events. `PATCH` to inject guidance without restarting. |
| **Checkpoint & fork** | Save agent state, resume later, or fork into parallel explorations. |

**What it is NOT:**

- Not a framework (no BaseAgent, no decorators, no opinions about your agent logic)
- Not a prompt library
- Not another LangChain

---

## Slide 4: Architecture

### Agents as Data, Runtime as Infrastructure

```
┌──────────────────────────────────────────────┐
│                   Auton Runtime               │
│                                               │
│  ┌─────────┐  ┌──────────┐  ┌────────────┐  │
│  │ Registry │  │ Executor │  │ Oversight  │  │
│  │ (tree)   │  │ (LLM     │  │ (drift,    │  │
│  │          │  │  loop)   │  │  budget,   │  │
│  │ /root    │  │          │  │  loops)    │  │
│  │ /root/a  │  │ Budget   │  │            │  │
│  │ /root/b  │  │ Planner  │  │ SSE events │  │
│  └─────────┘  └──────────┘  └────────────┘  │
│                                               │
│  HTTP + SSE API (FastAPI)                     │
└──────────────────────────────────────────────┘
         ▲              ▲              ▲
         │              │              │
    Spawn/Kill    Observe/Correct   Dashboard
```

Key architectural decisions:

1. **Agents as data (AgentNode)** — not subclasses. The runtime operates *on* agents, not *inside* them. Clean separation of concerns.
2. **Filesystem-like paths** — `/researcher/summarizer/editor`. Navigate, scope, cascade naturally.
3. **State machine with formal transitions** — SPAWNING → RUNNING ↔ IDLE → CORRECTING → SUSPENDED → TERMINATING → DEAD. Invalid transitions are errors, not silent bugs.
4. **Budget planner with write gate** — near budget exhaustion, only output tools (write_file, finish) are available. Forces deliverables before death.
5. **Closure-bound tools** — workspace isolation per agent. No shared mutable state.

---

## Slide 5: The Budget System (Deep Dive)

### Agents that produce deliverables, not just burn tokens

This is where Auton is genuinely novel. Most budget systems are kill switches. Auton's is a *behavioral nudge system*:

```
0%  ─────── 70% ──── 85% ──── 95% ──── 115%
  healthy    warn    urgent   FINAL    HARD STOP
                                │
                          write gate activates
                          (only output tools available)
```

- **70%**: "Budget past midpoint. Prioritize deliverables."
- **85%**: "URGENT. Stop new research. Start producing output."
- **95%**: "FINAL TURN. Write files NOW." + **write gate** (research tools disabled)
- **115%**: Hard kill (safety valve — should never reach this if planner works)

The `BudgetPlanner` uses exponential moving averages of per-call token costs to estimate remaining calls. It triggers finalization *before* the budget runs out, not after.

**Result:** Agents that hit budget limits still produce useful output instead of dying mid-thought.

---

## Slide 6: Oversight Engine

### Detect problems before they become expensive

| Check | Method | Action |
|---|---|---|
| **Budget** | Token counting + rate estimation | Warn → finalize → suspend |
| **Goal drift** | Embedding similarity between original goal and recent activity | Auto-suspend at threshold |
| **Loop detection** | Pattern matching on repeated tool calls | Warn after 3 repeats → suspend |
| **Coherence** | Semantic coherence of recent conversation | Suspend below threshold |
| **Runtime** | Wall-clock elapsed time | Hard suspend |

All checks are **observable via SSE** — every 30s heartbeat includes oversight results. Build dashboards, alerts, or automated responses.

The key insight: **oversight is not safety theater**. It's infrastructure. Like health checks in Kubernetes — automated, continuous, actionable.

---

## Slide 7: The Protocol

### HTTP + SSE. That's it.

No SDK required. No client library. cURL is a first-class citizen.

```bash
# Spawn
curl -X POST localhost:8420/agents -d '{"id":"r","spec":{"goal":"..."}}'

# Observe (SSE stream)
curl -N localhost:8420/agents/r/observe

# Correct without restarting
curl -X PATCH localhost:8420/agents/r -d '{"guidance":"Focus on X, not Y"}'

# Checkpoint and fork
curl -X POST localhost:8420/agents/r/checkpoint
curl -X POST localhost:8420/agents/r/fork

# Kill (cascades to children)
curl -X DELETE localhost:8420/agents/r
```

**Why this matters:**
- Any language, any client, any orchestrator can manage Auton agents
- SSE means real-time observation without polling
- REST semantics mean the API is self-documenting
- Filesystem-like paths mean hierarchies are intuitive

---

## Slide 8: Relationship to MCP

### MCP + Auton = Complete Agent Infrastructure

```
         ┌──────────────────────────┐
         │      Your Agent Code     │
         └──────┬───────────┬───────┘
                │           │
    ┌───────────▼──┐   ┌───▼──────────┐
    │     MCP      │   │    Auton     │
    │              │   │              │
    │ What agents  │   │ How agents   │
    │ CAN DO       │   │ ARE MANAGED  │
    │              │   │              │
    │ Tools,       │   │ Lifecycle,   │
    │ Resources,   │   │ Supervision, │
    │ Protocols    │   │ Oversight    │
    └──────────────┘   └──────────────┘
```

MCP defines the tool interface. Auton defines the lifecycle. They're orthogonal and complementary — like Docker (containers) and Kubernetes (orchestration).

---

## Slide 9: Market & Positioning

### The agent infrastructure layer is wide open

**Market reality (2026):**
- Agent frameworks are plentiful (LangChain, CrewAI, AutoGen, etc.)
- Agent *runtimes* barely exist
- Every enterprise deploying agents is building ad-hoc lifecycle management
- The "who watches the agents" problem is becoming urgent as agents get longer-running

**TAM:** The agent orchestration market is nascent but tracks to the container orchestration trajectory. Kubernetes went from 0 to $8B+ market in ~5 years. Agent orchestration could follow a similar curve.

**Positioning:**
- Not competing with frameworks (we don't care how you build agents)
- Not competing with model providers (we're model-agnostic)
- Competing with the duct tape and prayer that everyone currently uses for agent lifecycle

**Comparable precedent:** Erlang/OTP for telecom → Kubernetes for containers → **Auton for agents**

---

## Slide 10: Competitive Landscape

| | Auton | LangGraph | CrewAI | AutoGen |
|---|---|---|---|---|
| **Focus** | Runtime/lifecycle | Workflow graphs | Role-based teams | Multi-agent chat |
| **Long-running** | ✅ Core design | ❌ Batch-oriented | ❌ Task-oriented | ⚠️ Limited |
| **Supervision** | ✅ OTP-style trees | ❌ | ❌ | ❌ |
| **Budget control** | ✅ Multi-layer | ⚠️ Basic limits | ⚠️ Basic limits | ❌ |
| **Live oversight** | ✅ Drift/loop/coherence | ❌ | ❌ | ❌ |
| **Observe/correct** | ✅ SSE + PATCH | ❌ | ❌ | ❌ |
| **Checkpoint/fork** | ✅ | ⚠️ State persistence | ❌ | ❌ |
| **Protocol** | HTTP + SSE (open) | Python SDK | Python SDK | Python SDK |
| **Lock-in** | None (protocol-first) | LangChain ecosystem | CrewAI ecosystem | Microsoft ecosystem |

**The moat:** Supervision + budget + oversight as a *protocol*, not a library. Once agents are managed through Auton's API, switching cost is in the orchestration layer, not the agent code.

---

## Slide 11: Business Model

### Open core → managed platform

**Phase 1 (Now): Open Source Runtime**
- MIT licensed, community-driven
- Build adoption, establish the protocol
- Developer mindshare is the asset

**Phase 2: Auton Cloud (Managed)**
- Hosted runtime with dashboard
- Multi-tenant agent management
- Enhanced oversight (ML-based drift detection, anomaly detection)
- SLA-backed uptime for production agent deployments
- **Pricing:** Per-agent-hour + oversight tier

**Phase 3: Enterprise**
- On-prem deployment
- Compliance features (audit logs, approval workflows, kill switches)
- Integration with existing observability stacks (Datadog, Grafana, etc.)
- Role-based access control for agent management
- **Pricing:** Annual license + support

---

## Slide 12: Technical Roadmap

### What exists vs. what's coming

**✅ Built (working today):**
- Full agent lifecycle (spawn → supervise → kill)
- Supervision trees with restart policies
- Budget planner with write gate and escalating warnings
- Oversight engine (drift, loops, coherence, budget)
- SSE observation streams
- Checkpoint and fork
- HTTP API (FastAPI)
- Workspace isolation per agent
- Agent coordination tools (spawn_child, message, status)

**🔨 Next (weeks):**
- Persistent storage backend (currently in-memory)
- MCP tool integration (agents use MCP servers as tool providers)
- Web dashboard for observation and control
- Multi-model support (currently Anthropic-focused)

**🗺️ Roadmap (months):**
- Distributed execution (agents across machines)
- Agent marketplace (reusable agent templates)
- Advanced oversight (learned drift models, cost prediction)
- Time-travel debugging (replay from any checkpoint)
- Approval workflows (human-in-the-loop for sensitive operations)

---

## Slide 13: The Team

### Alexander Temerev

- **Background:** Blockchain architect (Alien), PhD researcher (biomedical sciences, University of Geneva), former EPFL Blue Brain Project (whole-brain simulation under Henry Markram)
- **Relevant:** Built Lethe — a persistent-memory AI system with agent architecture that Auton's design is directly derived from. The executor, supervision model, and budget system are battle-tested patterns from production use.
- **Edge:** Rare combination of distributed systems (blockchain), neuroscience-inspired architecture (Blue Brain), and hands-on agent engineering (Lethe)

---

## Slide 14: The Ask

### What we need

**Seed round: CHF 500K**

- **6 months runway** for 2-person core team
- **Deliverables:**
  - Production-ready open-source runtime (persistent storage, multi-model)
  - Web dashboard (observe, correct, manage agents)
  - 3 enterprise design partners
  - Auton Cloud beta

**Why now:**
- Agent capabilities are scaling faster than agent infrastructure
- The window for establishing the lifecycle protocol standard is ~12-18 months
- First mover with the right abstraction wins (see: Docker, Kubernetes, Terraform)

---

## Slide 15: The Closing Frame

### The infrastructure layer always wins

Every computing paradigm follows the same arc:

| Era | Primitive | Framework Era | Infrastructure Winner |
|---|---|---|---|
| Web | HTTP requests | Rails, Django | Nginx, Apache |
| Cloud | VMs | CloudFormation | Terraform |
| Containers | Docker images | Docker Compose | Kubernetes |
| **Agents** | **LLM calls** | **LangChain, CrewAI** | **?** |

The framework era is loud and crowded. The infrastructure layer is quiet, essential, and durable.

**Auton is the infrastructure layer for autonomous agents.**

---

## Appendix: Key Code Metrics

- **~2,500 lines** of core runtime code (Python, async-native)
- **7 source files:** models, registry, executor, oversight, budget, llm, api
- **Clean dependency graph:** FastAPI, Anthropic SDK, numpy (for embeddings). No framework dependencies.
- **State machine:** 8 states, 15 valid transitions, formally enforced
- **Budget system:** 4-tier escalation with EMA-based cost prediction
- **API:** 15 endpoints, full OpenAPI spec auto-generated

---

*"MCP is the nervous system. Auton is the immune system."*
