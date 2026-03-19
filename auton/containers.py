"""Per-user container management via podman.

Each user gets one long-running Alpine container. All the user's agents
share that container. Containers are labeled for bulk management.

    podman ps --filter label=auton.managed=true
    podman stop  $(podman ps -q --filter label=auton.managed=true)
    podman rm    $(podman ps -aq --filter label=auton.managed=true)
"""

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

IMAGE = os.environ.get("AUTON_WORKSPACE_IMAGE", "localhost/auton-workspace:latest")
WORKSPACES_ROOT = Path(os.environ.get("AUTON_WORKSPACES_ROOT", "./workspaces"))
CONTAINER_PREFIX = "auton-ws"

# Resource limits per container
DEFAULT_MEMORY = os.environ.get("AUTON_WS_MEMORY", "512m")
DEFAULT_PIDS = int(os.environ.get("AUTON_WS_PIDS", "256"))


def _run(args: list[str], timeout: int = 30, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)


class ContainerManager:
    """Manages one podman container per user for workspace isolation."""

    _instance: "ContainerManager | None" = None

    def __init__(self):
        self._ready: set[str] = set()  # user_ids with confirmed running containers

    @classmethod
    def get(cls) -> "ContainerManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def container_name(user_id: str) -> str:
        return f"{CONTAINER_PREFIX}-{user_id.replace('-', '')[:12]}"

    @staticmethod
    def host_workspace(user_id: str) -> Path:
        safe = user_id.replace("/", "_").replace("..", "_")
        return WORKSPACES_ROOT / safe

    def ensure_container(self, user_id: str) -> str:
        """Ensure a running container exists for this user. Returns container name."""
        name = self.container_name(user_id)

        # Fast path: already verified running this session
        if user_id in self._ready:
            return name

        # Check if container exists
        r = _run(["podman", "inspect", "--format", "{{.State.Running}}", name])
        if r.returncode == 0:
            if r.stdout.strip() == "true":
                self._ready.add(user_id)
                return name
            # Exists but stopped — start it
            _run(["podman", "start", name])
            self._ready.add(user_id)
            logger.info(f"Started existing container {name}")
            return name

        # Create workspace dir on host
        ws = self.host_workspace(user_id)
        ws.mkdir(parents=True, exist_ok=True)

        # Create and start container
        cmd = [
            "podman", "run", "-d",
            "--name", name,
            "--label", "auton.managed=true",
            "--label", f"auton.user_id={user_id}",
            "--hostname", "workspace",
            "-v", f"{ws.resolve()}:/workspace:Z",
            "--memory", DEFAULT_MEMORY,
            "--pids-limit", str(DEFAULT_PIDS),
            IMAGE,
        ]
        r = _run(cmd, timeout=60)
        if r.returncode != 0:
            logger.error(f"Failed to create container {name}: {r.stderr}")
            raise RuntimeError(f"Container creation failed: {r.stderr.strip()}")

        self._ready.add(user_id)
        logger.info(f"Created container {name} for user {user_id[:8]}")
        return name

    def exec_command(
        self, user_id: str, command: str, timeout: int = 60, workdir: str = "/workspace",
    ) -> dict:
        """Execute a command in the user's container. Returns {return_code, stdout, stderr}."""
        name = self.ensure_container(user_id)
        timeout = min(max(timeout, 1), 300)
        try:
            r = _run(
                ["podman", "exec", "-w", workdir, name, "bash", "-c", command],
                timeout=timeout,
            )
            stdout = r.stdout[-10_000:] if len(r.stdout) > 10_000 else r.stdout
            stderr = r.stderr[-5_000:] if len(r.stderr) > 5_000 else r.stderr
            return {"return_code": r.returncode, "stdout": stdout, "stderr": stderr}
        except subprocess.TimeoutExpired:
            return {"return_code": -1, "stdout": "", "stderr": f"Timed out after {timeout}s"}
        except Exception as e:
            return {"return_code": -1, "stdout": "", "stderr": str(e)}

    def stop_container(self, user_id: str) -> None:
        """Stop a user's container."""
        name = self.container_name(user_id)
        _run(["podman", "stop", "-t", "5", name])
        self._ready.discard(user_id)
        logger.info(f"Stopped container {name}")

    def remove_container(self, user_id: str) -> None:
        """Remove a user's container (must be stopped first)."""
        name = self.container_name(user_id)
        _run(["podman", "rm", "-f", name])
        self._ready.discard(user_id)
        logger.info(f"Removed container {name}")

    def workspace_usage(self, user_id: str) -> int:
        """Return workspace disk usage in bytes."""
        ws = self.host_workspace(user_id)
        if not ws.exists():
            return 0
        r = _run(["du", "-sb", str(ws)])
        if r.returncode == 0:
            return int(r.stdout.split()[0])
        return 0
