"""Dossier management tools for the person research agent.

These tools are bound to a specific agent_id at registration time
via closures in the executor.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# In-memory dossier cache (synced to DB by executor)
_dossiers: dict[str, dict[str, Any]] = {}

VALID_SECTIONS = [
    "basic_info", "social_media", "career", "education",
    "publications", "news", "other",
]


def get_dossier(agent_id: str) -> dict:
    """Get or create the in-memory dossier for an agent."""
    if agent_id not in _dossiers:
        _dossiers[agent_id] = {
            "sections": {s: [] for s in VALID_SECTIONS},
            "summary": "",
            "last_updated": "",
        }
    return _dossiers[agent_id]


def set_dossier(agent_id: str, data: dict) -> None:
    """Set the in-memory dossier (e.g. loaded from DB)."""
    _dossiers[agent_id] = data


def _deduplicate_entries(entries: list[dict]) -> list[dict]:
    """Deduplicate entries by URL or by title+content similarity."""
    seen_urls = set()
    seen_titles = set()
    unique = []
    for entry in entries:
        url = entry.get("url", "")
        title = entry.get("title", "").lower().strip()
        if url and url in seen_urls:
            continue
        if title and title in seen_titles and not url:
            continue
        if url:
            seen_urls.add(url)
        if title:
            seen_titles.add(title)
        unique.append(entry)
    return unique


def make_read_dossier(agent_id: str):
    """Create a read_dossier tool bound to a specific agent."""

    def read_dossier() -> str:
        """Read the current research dossier.

        Returns the full dossier with all sections and findings so far.

        Returns:
            JSON with the current dossier contents
        """
        dossier = get_dossier(agent_id)
        return json.dumps(dossier, indent=2)

    return read_dossier


def make_update_dossier(agent_id: str):
    """Create an update_dossier tool bound to a specific agent."""

    def update_dossier(section: str, entries: str) -> str:
        """Add or update entries in a dossier section.

        Each entry should be a JSON object with relevant fields (e.g. title, url, description).
        Entries are deduplicated by URL and title.

        Args:
            section: Section name (basic_info, social_media, career, education, publications, news, other)
            entries: JSON array of entry objects to add (e.g. [{"title": "...", "url": "...", "description": "..."}])

        Returns:
            Confirmation of the update
        """
        if section not in VALID_SECTIONS:
            return json.dumps({"status": "error", "message": f"Invalid section. Use one of: {VALID_SECTIONS}"})

        try:
            new_entries = json.loads(entries)
            if isinstance(new_entries, dict):
                new_entries = [new_entries]
        except json.JSONDecodeError as e:
            return json.dumps({"status": "error", "message": f"Invalid JSON: {e}"})

        dossier = get_dossier(agent_id)
        existing = dossier["sections"].get(section, [])
        combined = existing + new_entries
        dossier["sections"][section] = _deduplicate_entries(combined)

        from datetime import datetime, timezone
        dossier["last_updated"] = datetime.now(timezone.utc).isoformat()

        added = len(dossier["sections"][section]) - len(existing)
        return json.dumps({
            "status": "OK",
            "section": section,
            "entries_added": added,
            "total_entries": len(dossier["sections"][section]),
        })

    return update_dossier


def make_finish_research(agent_id: str, stop_callback):
    """Create a finish_research tool that signals completion."""

    def finish_research(summary: str) -> str:
        """Signal that the research session is complete.

        Call this when you have exhausted available search avenues
        or gathered sufficient information.

        Args:
            summary: Brief summary of findings and any notable discoveries

        Returns:
            Confirmation
        """
        dossier = get_dossier(agent_id)
        dossier["summary"] = summary
        stop_callback()
        return json.dumps({"status": "OK", "message": "Research session complete."})

    return finish_research
