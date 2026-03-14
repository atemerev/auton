# Auton — Pitch Deck

---

## Slide 1: The Problem Is Already Here

**AI agents are going autonomous. The bills — and the failures — are piling up.**

Developers are reporting five- and six-figure surprise bills from autonomous agents left running unsupervised. Research agents that hallucinate new goals and spawn sub-agents. Coding agents stuck in loops, retrying the same failing call thousands of times. The pattern repeats: launch an agent, go to sleep, wake up to a disaster.

Every team deploying agents in production has a story like this. Most have several.

**The AI industry built the engine. Nobody built the brakes.**

---

## Slide 2: The Problem

### AI agents are going autonomous. The control plane isn't ready.

Companies are deploying AI agents that run for hours, days, or continuously. The AI agent market is projected to hit $52B by 2030 (41% CAGR from ~$8B in 2025). AI agents now capture ~33% of total global VC funding. Gartner predicts 40%+ of enterprise AI agent projects will be scrapped without proper governance infrastructure.

But today:

- **No one's watching.** Once you launch an agent, you can't see what it's doing, whether it's still on task, or if it's burning money.
- **No spending limits that actually work.** Current "guardrails" are just polite suggestions in the prompt. The agent can — and does — ignore them.
- **No management layer.** Agents that create other agents produce invisible sprawl. One goes wrong, they all go wrong.
- **No correction without killing.** If an agent drifts off course, your only option is to restart from zero.

Enterprises deploy agents but monitor them with "fragmented point solutions built for simpler, deterministic use cases" (RPS Ventures, 2026). Every company deploying agents today is solving these problems from scratch. Badly.

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

This is our core technical moat. No one else does this.

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

### $52B market by 2030. The control plane is up for grabs.

**AI agent infrastructure** is the fastest-growing category in enterprise software. ~$8B in 2025, projected $52B by 2030 at 41% CAGR. AI agents capture ~33% of total global VC. The money is real: Sierra AI reached $10B valuation, Temporal raised $300M at $5B for durable execution alone, and even the closest governance competitor (Fiddler) has raised $100M on 4x revenue growth.

| Segment | Size by 2030 | Our Position |
|---|---|---|
| Agent lifecycle & operations | $15B+ | **Core market** |
| AI observability & governance | $8B+ | Adjacent (oversight) |
| Enterprise AI spend control | $6B+ | Adjacent (budget) |

**Right now, every enterprise deploying agents is building custom lifecycle management.** They don't want to. They want to buy it.

---

## Slide 8: Competitive Landscape

### The category is forming. Nobody has the full stack yet.

**Direct competitors — agent governance startups:**

| Company | Focus | Funding | Gap vs. Auton |
|---|---|---|---|
| **Fiddler AI** | ML observability → agent monitoring | $100M (Series C) | Reactive monitoring, not proactive control. No budget enforcement or supervision trees. Enterprise-only. |
| **Overmind** | Agent drift detection | £2M seed | Drift detection only — no lifecycle management, no budgets. Single capability. |
| **Swept AI** | Pre-deployment agent testing | $1.4M pre-seed | Evaluation/certification, not runtime governance. Complementary, not competitive. |
| **Prefactor** | Agent visibility & audit trails | ~$100K pre-seed | Uses "Agent Control Plane" positioning but minimal product. Validates our category. |

Fiddler is the best-funded direct competitor ($100M, ranked #1 in AI Agent Security by CB Insights). But Fiddler's heritage is reactive ML monitoring — observing after the fact. Auton's control plane is proactive and structural: enforcing budgets, managing lifecycles, and providing supervision trees *before and during* execution, not just after.

**Infrastructure layer — we sit above, not beside:**

| Layer | Who | Funding | Relationship to Auton |
|---|---|---|---|
| **Execution** | Temporal | $300M, $5B val | Makes agents durable. Auton makes them governed. |
| **Frameworks** | LangGraph, CrewAI, AutoGen | $260M+ combined | Build agents. Auton runs them safely. |
| **Cloud platforms** | AWS, Azure, GCP | ∞ | Locked to one cloud. Auton is cross-cloud. |

**Cloud incumbents** (Azure AI Agent Service, Bedrock Agents, Vertex AI) offer agent hosting within their walled gardens but no cross-cloud governance, no agent-level budget enforcement, no supervision trees, no drift detection. Auton is the neutral, framework-agnostic control plane.

**The key insight:** Most existing tools address one dimension — orchestration OR observability OR governance. Nobody integrates lifecycle + budget + drift + supervision trees into a single control plane. That's us.

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
- Full agent lifecycle management (spawn, supervise, suspend, resume, kill)
- Supervision trees with automatic recovery (OTP-inspired)
- Budget system with intelligent finalization and write gate
- LLM judge drift detection — evaluates goal progress, not just topic similarity
- Loop detection with auto-suspension
- Live observation streams (SSE)
- Checkpoint, fork, and restart
- API key authentication
- SQLite persistence
- Open HTTP API — works from any language

**Origin:** Auton's core patterns are extracted from Lethe, a persistent-memory AI system running in production. The architecture is proven — not theoretical.

**Next 6 months:**
- Web dashboard (observe and control agents visually)
- PostgreSQL + horizontal scaling
- Multi-model support (currently via LiteLLM; expanding provider coverage)
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

### Three forces converging — and a closing window.

**1. The funding wave confirms the market.** Temporal ($5B), Sierra ($10B), LangChain ($1.25B), Fiddler ($100M) — billions are pouring into agent infrastructure. But none of them have the full control plane. The gap is open, and competitors are circling — Fiddler, Overmind, Prefactor all entered in 2025-2026.

**2. Enterprise adoption is accelerating.** 2025 was experimentation. 2026-2027 is production deployment. Gartner says 40%+ of agent projects will fail without governance. Companies are hitting the "who watches the agents" wall *right now*.

**3. The protocol window is closing.** In infrastructure, the first standard wins (TCP/IP, HTTP, Docker, Kubernetes). Anthropic MCP and Google A2A are establishing agent communication standards. The agent lifecycle protocol isn't established yet — **but the window is twelve months, not five years.** We intend it to be ours.

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
