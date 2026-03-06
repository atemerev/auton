"""Person research agent — demo agent for Auton.

Factory function that creates the right SpawnRequest for researching a person.
"""

from auton.models import AgentPolicy, AgentSpec, BudgetSpec, SpawnRequest


def create_person_research_spec(
    person_name: str,
    known_details: str = "",
    schedule: str | None = None,
    model: str = "openrouter/anthropic/claude-sonnet-4-6",
) -> SpawnRequest:
    """Create a SpawnRequest for a person research agent.

    Args:
        person_name: Name of the person to research
        known_details: Any known details (company, role, location, etc.)
        schedule: Cron expression for recurring runs (e.g. "0 */6 * * *" for every 6 hours)
        model: LLM model to use
    """
    goal = f"Research {person_name}"
    if known_details:
        goal += f". Known details: {known_details}"

    agent_id = f"research-{person_name.lower().replace(' ', '-').replace('.', '')}"

    return SpawnRequest(
        id=agent_id,
        spec=AgentSpec(
            goal=goal,
            model=model,
            tools=["web_search", "fetch_webpage"],
            schedule=schedule,
        ),
        policy=AgentPolicy(
            budget=BudgetSpec(
                max_total_tokens=200_000,
                max_runtime_seconds=600,
            ),
        ),
    )
