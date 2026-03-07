"""Person research agent — demo agent spec for Auton.

Factory function that creates the right SpawnRequest for researching a person.
The system prompt, tools, and budget are all explicit in the spec.
"""

from auton.models import AgentSpec, SpawnRequest

PERSON_RESEARCH_PROMPT = (
    "You are a thorough, autonomous research agent.\n\n"
    "Focus on verifiable, factual information. Deduplicate — don't repeat findings."
)


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
    goal = f"Research {person_name} and produce dossier.md with your findings."
    if known_details:
        goal += f" Known details: {known_details}"

    agent_id = f"research-{person_name.lower().replace(' ', '-').replace('.', '')}"

    return SpawnRequest(
        id=agent_id,
        spec=AgentSpec(
            name=f"Research: {person_name}",
            description=f"Autonomous research agent for {person_name}",
            system_prompt=PERSON_RESEARCH_PROMPT,
            goal=goal,
            model=model,
            tools=["web_search", "fetch_webpage", "write_file", "read_file", "list_files", "finish"],
            schedule=schedule,
            max_tokens=200_000,
            max_runtime_seconds=600,
        ),
    )
