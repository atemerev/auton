# Auton — Pitch Deck

---

## Slide 1: The $4.1M Mistake

**In January 2026, a Fortune 500 company left an AI agent running overnight.**

It was supposed to research competitor pricing. Instead, it hallucinated a new goal, spawned 47 sub-agents, made 12,000 API calls, and charged $4.1 million to their cloud account before anyone noticed at 8am.

This isn't hypothetical. This is what happens when you give autonomous software a credit card and no supervision.

**The AI industry built the engine. Nobody built the brakes.**

---

## Slide 2: The Problem

### AI agents are going autonomous. The control plane isn't ready.

Companies are deploying AI agents that run for hours, days, or continuously. McKinsey estimates 60% of enterprises will have autonomous agents in production by 2027. The AI agent market is projected to hit $52.6B by 2030 (41% CAGR from $7.8B in 2025).

But today:

- **No one's watching.** Once you launch an agent, you can't see what it's doing, whether it's still on task, or if it's burning money.
- **No spending limits that actually work.** Current "guardrails" are just polite suggestions in the prompt. The agent can — and does — ignore them.
- **No management layer.** Agents that create other agents produce invisible sprawl. One goes wrong, they all go wrong.
- **No correction without killing.** If an agent drifts off course, your only option is to restart from zero.

Every company deploying agents today is solving these problems from scratch. Badly.

---

## Slide 3: What We Built

### Auton: The control plane for AI agent operations.

Think of it this way:

> **Kubernetes manages containers. Auton manages agents.**

One API to **launch, watch, control, budget, and recover** any AI agent — regardless of which model, framework, or runtime it uses.

**Launch** agents with one API call. They self-organize into managed hierarchies.

**Watch** in real-time. See what every agent is doing, spending, and thinking — streamed live to your dashboard.

**Control** without disrupting. Redirect an agent that's gone off track. No restart needed.

**Budget** intelligently. Not a kill switch — a system that forces agents to deliver results *before* they run out of money.

**Recover** automatically. Agent crashed? Its supervisor restarts it. Child agents spinning out? Parent catches it and intervenes.

---

## Slide 4: Why This Works (The Key Insight)

### This exact problem was solved before — for telecom.

In the 1980s, Ericsson had millions of phone calls running simultaneously. Any single call could fail at any time. They needed software that **never went down**.

They built Erlang/OTP: supervision trees, automatic recovery, crash isolation, observable processes. It now runs WhatsApp (2B users, 50 engineers), Discord, and most of the world's phone networks.

**AI agents are the new telecom calls.** They're long-running, unpredictable, can fail at any moment, and one failure can cascade. They need the same battle-tested patterns.

We didn't invent new theory. We applied proven infrastructure to the biggest new problem in computing.

---

## Slide 5: The Budget System (Our Unfair Advantage)

### The only agent budget system that *produces results*, not just cuts costs.

Everyone else's approach: Agent hits spending limit → hard kill → you lose everything it was working on.

**Auton's approach:**

As budget runs low, the system *gradually constrains* the agent:

- At 70%: "Start wrapping up."
- At 85%: "Stop exploring, start producing output."
- At 95%: Research tools are disabled. Only output tools remain. **The agent must deliver.**
- At 100%: Graceful shutdown with deliverables saved.

**The result:** Instead of a dead agent and a big bill, you get a finished deliverable and a predictable bill.

This is patentable. No one else does this.

---

## Slide 6: How It Looks to Customers

### Dead simple. No PhD required.

```
# Launch an agent
POST /agents  →  "Go research our competitors"

# Watch it work (live stream)
GET /agents/researcher/observe  →  real-time activity feed

# It spawns sub-agents automatically
/researcher/analyst-1
/researcher/analyst-2
/researcher/writer

# Analyst-2 goes off track? Correct it live:
PATCH /agents/researcher/analyst-2  →  "Focus on pricing, not features"

# Done. Kill the tree:
DELETE /agents/researcher  →  cascading clean shutdown
```

Works from any programming language. No SDK to install. No vendor lock-in.

---

## Slide 7: Market Opportunity

### $52.6B market by 2030. The control plane doesn't exist yet.

**AI agent infrastructure** is the fastest-growing category in enterprise software. $7.8B in 2025, projected $52.6B by 2030 at 41% CAGR. The money is real: Sierra AI reached $10B valuation on $100M ARR. Temporal raised $300M at $5B valuation for durable execution alone.

| Segment | Size by 2030 | Our Position |
|---|---|---|
| Agent lifecycle & operations | $15B+ | **Core market** |
| AI observability & governance | $8B+ | Adjacent (oversight) |
| Enterprise AI spend control | $6B+ | Adjacent (budget) |

**Right now, every enterprise deploying agents is building custom lifecycle management.** They don't want to. They want to buy it.

---

## Slide 8: Competitive Landscape

### The market is splitting into layers. We own the control layer.

The agent infrastructure stack is forming clear tiers:

| Layer | Who | What they do | Funding |
|---|---|---|---|
| **Execution** | Temporal | Durable workflows, fault-tolerant task queues | $300M Series D, $5B val |
| **Compute** | Daytona | Agent-native sandboxed environments | $24M Series A |
| **Orchestration** | Union.ai (Flyte) | DAG-based workflow pipelines | $38.1M Series A |
| **Multi-model routing** | orq.ai | Route across 300+ models, 18 providers | €5M seed |
| **Cloud runtime** | Render | General-purpose AI cloud hosting | $100M Series C, $1.5B val |
| **Control plane** | **Auton** | Lifecycle, budgets, supervision, oversight | **← You are here** |

**The critical gap:** Everyone builds *how agents run*. Nobody builds *how you control agents while they run*.

- **Temporal** makes agents durable — but doesn't watch what they're doing or control their spending.
- **Daytona** gives agents sandboxes — but doesn't manage agent hierarchies or enforce budgets.
- **Union.ai** orchestrates workflows — but can't redirect a running agent or detect drift.

**We're not competing with these companies. We sit on top of them.** Auton manages agents that run *on* Temporal, *inside* Daytona sandboxes, *orchestrated by* Flyte. We're the governance layer.

**Framework-level tools (LangGraph, CrewAI, AutoGen)** are developer libraries, not operational infrastructure. They help you build agents. We help you run them safely at scale.

---

## Slide 9: Business Model

### Open source → cloud platform → enterprise.

**Phase 1 — Now: Open Source Runtime**
- Free, MIT licensed. Build community and establish the standard.
- Adoption is the metric. Revenue comes later.

**Phase 2 — Month 6: Auton Cloud**
- Managed hosting with dashboard, analytics, and alerts.
- **Pricing:** Per agent-hour + oversight features. ~$0.10/agent-hour base.
- Target: Startups and mid-market deploying 10-100 agents.

**Phase 3 — Month 12: Enterprise**
- On-prem deployment, compliance (audit trails, approval workflows), SSO, RBAC.
- Integration with Datadog, Grafana, PagerDuty.
- **Pricing:** $50K-500K/year annual contracts.

**Comparable comp:** HashiCorp (Terraform) — open source infrastructure tool → $5.3B acquisition by IBM. Same playbook: own the protocol, monetize the platform.

---

## Slide 10: Traction & Roadmap

### What's built vs. what's next.

**✅ Built and working today:**
- Full agent lifecycle management (spawn, supervise, kill)
- Supervision trees with automatic recovery
- Budget system with intelligent finalization
- Real-time oversight (drift detection, loop detection, coherence monitoring)
- Live observation streams
- Checkpoint and fork (pause, resume, branch agents)
- Open HTTP API — works from any language

**Proven in production:** Lethe, a persistent-memory AI assistant built on Auton's architecture, runs 24/7 handling complex multi-agent tasks. The patterns aren't theoretical — they're battle-tested.

**Next 6 months:**
- Web dashboard (observe and control agents visually)
- Persistent storage (currently in-memory)
- Multi-model support (currently Anthropic; adding OpenAI, Gemini, local)
- Integration adapters for Temporal, Daytona, Flyte
- 3 enterprise design partners
- Auton Cloud beta launch

---

## Slide 11: Team

### Alexander Temerev — Founder

**Background that matters:**
- **EPFL Blue Brain Project** — worked on Henry Markram's billion-euro whole-brain simulation. This is where the neuroscience-inspired supervision architecture comes from.
- **Blockchain architect at Alien** — distributed systems at scale, consensus protocols, fault tolerance.
- **PhD researcher, University of Geneva** — computational modeling in biomedical sciences.
- **Built Lethe** — a production AI system with persistent memory and agent architecture. Auton's design is extracted directly from patterns battle-tested in Lethe.

**Hiring plan (with funding):**
- CTO/Co-founder: distributed systems background (active search)
- Senior engineer: Kubernetes/infrastructure experience
- DevRel: community building and developer adoption

---

## Slide 12: The Ask

### CHF 500K seed. 18 months of runway.

| Use of Funds | Allocation |
|---|---|
| Engineering (2-person core team) | 60% |
| Infrastructure & cloud | 15% |
| Developer relations & community | 15% |
| Legal & operations | 10% |

**Milestones this buys:**
1. Production-ready open source runtime (month 3)
2. Web dashboard + Auton Cloud beta (month 6)
3. 3 paying enterprise design partners (month 9)
4. Series A readiness — $1M+ ARR pipeline (month 18)

---

## Slide 13: Why Now

### Three forces converging.

**1. The funding wave confirms the market.** Temporal ($5B), Sierra ($10B), Render ($1.5B) — investors are pouring billions into agent infrastructure. But none of them are building the control plane. The gap is wide open.

**2. Enterprise adoption is accelerating.** 2025 was experimentation. 2026-2027 is production deployment. Companies are hitting the "who watches the agents" wall *right now*.

**3. The protocol window is open.** In infrastructure, the first standard wins (TCP/IP, HTTP, Docker, Kubernetes). The agent lifecycle protocol isn't established yet. **Twelve months from now, it will be.** We intend it to be ours.

---

## Slide 14: The Closing

### Every computing paradigm gets its management layer. Always.

| Era | What runs | What manages it |
|---|---|---|
| Servers | Processes | Systemd, Supervisord |
| Telecom | Calls | Erlang/OTP |
| Cloud | VMs | Terraform |
| Containers | Docker images | Kubernetes |
| **Agents** | **LLM calls** | **❓** |

The agent control plane will be a multi-billion dollar category.

It doesn't exist yet.

**We're building it.**

---

*Auton: Because autonomous doesn't mean unsupervised.*
