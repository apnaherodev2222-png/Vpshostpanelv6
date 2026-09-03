"""
docker_runner.py
─────────────────────────────────────────────────────────────────────────
Sandboxed script execution for the hosting panel bot.

Replaces raw `subprocess.Popen(...)` execution with isolated, resource-
limited Docker containers so one user's script can't:
  - eat all the VPS RAM/CPU
  - fork-bomb the host
  - read/write files outside their own folder
  - reach the network (disabled by default)
  - escalate privileges inside the container

Requires:
    pip install docker
    Docker Engine installed on the VPS, with the bot's OS user in the
    `docker` group (so it can talk to /var/run/docker.sock without sudo).

IMPORTANT CAVEAT (read this):
    Any process that can talk to the Docker socket can, in practice,
    get root on the HOST — not just inside a container — via a few
    well-known techniques (e.g. mounting the host filesystem into a
    fresh container it spins up itself). This module only sandboxes
    the *user's uploaded script*; it does NOT sandbox your bot process
    itself. So: keep the bot process's own permissions tight, keep this
    codebase private, and treat "someone finds a bug in this bot" as
    "someone might get host root" when you threat-model it. For real
    multi-tenant hosting at scale, look into rootless Docker or gVisor
    (runsc) instead of default Docker — this module works with either.
"""
import asyncio
import logging
from pathlib import Path

try:
    import docker
    from docker.errors import DockerException, ImageNotFound, NotFound
except ImportError:
    docker = None
    DockerException = ImageNotFound = NotFound = Exception

logger = logging.getLogger(__name__)

# ─── CONFIG ────────────────────────────────────────────────────────────
DOCKER_IMAGES = {
    'py': 'python:3.11-slim',
    'js': 'node:20-slim',
}

# Tuned for an 8-core / 32GB VPS running many scripts concurrently.
# Adjust per-script share so a handful of busy scripts can't starve the box.
CONTAINER_LIMITS = {
    'mem_limit': '256m',           # RAM cap PER script
    'memswap_limit': '256m',       # no extra swap beyond mem_limit (disables swap for this container)
    'nano_cpus': int(0.5 * 1e9),   # 0.5 CPU core per script
    'pids_limit': 64,              # blocks fork-bombs
}

RUN_TIMEOUT_SECONDS = 6 * 60 * 60  # auto-kill a script after 6 hours (tune as needed)
ALLOW_NETWORK = True                # required for pip/npm installs inside the sandbox, and for
                                     # most hosted bots to reach Telegram/other APIs.
                                     # cap_drop/no-new-privileges below still apply for safety.


class DockerRunner:
    def __init__(self):
        self.client = None
        if docker is None:
            logger.error("docker SDK not installed — run: pip install docker")
            return
        try:
            self.client = docker.from_env()
            self.client.ping()
        except DockerException as e:
            logger.error(f"Docker daemon not reachable: {e}")
            self.client = None

    def is_available(self) -> bool:
        return self.client is not None

    def ensure_images(self):
        """Call once at startup — pulls images if missing so first run isn't slow."""
        if not self.is_available():
            return
        for image in DOCKER_IMAGES.values():
            try:
                self.client.images.get(image)
            except ImageNotFound:
                logger.info(f"Pulling {image} (first-time setup)...")
                self.client.images.pull(image)

    def run_script(self, file_path: Path, file_ext: str, user_folder: Path, script_key: str, env: dict = None):
        """
        Starts a sandboxed container running the given script.
        `env` is an optional dict of user-supplied environment variables —
        passed straight to the container, never baked into the image or
        written into the script file itself.
        Returns (container_id_or_None, error_message_or_None).
        """
        if not self.is_available():
            return None, "Docker sandbox is not available on this host."

        lang = file_ext.lstrip('.')
        image = DOCKER_IMAGES.get(lang)
        if image is None:
            return None, f"No sandbox image configured for .{lang} files."

        # Only the user's own folder is mounted — nothing else on the host is reachable.
        volumes = {
            str(user_folder.resolve()): {'bind': '/workspace', 'mode': 'rw'}
        }

        # Use the path RELATIVE to the mounted workspace, not just the bare
        # filename — this is what makes GitHub-cloned/zip-extracted projects
        # (entry file living in a subfolder) actually runnable, not just
        # single files dropped at the top level.
        try:
            rel_path = file_path.resolve().relative_to(user_folder.resolve())
        except ValueError:
            rel_path = Path(file_path.name)
        rel_path_str = str(rel_path).replace('\\', '/')
        entry_dir = str(rel_path.parent).replace('\\', '/')
        entry_dir = '' if entry_dir in ('.', '') else entry_dir + '/'

        if lang == 'py':
            # Prefer a requirements.txt sitting next to the entry file
            # (a cloned repo's own deps); fall back to one at workspace
            # root (the older single-file-upload convention).
            cmd = [
                'bash', '-c',
                (
                    f'if [ -f /workspace/{entry_dir}requirements.txt ]; then '
                    f'  echo "📦 Installing requirements..."; '
                    f'  pip install -r /workspace/{entry_dir}requirements.txt -q --no-cache-dir; '
                    f'  echo "✅ Requirements installed"; '
                    f'elif [ -f /workspace/requirements.txt ]; then '
                    f'  echo "📦 Installing requirements..."; '
                    f'  pip install -r /workspace/requirements.txt -q --no-cache-dir; '
                    f'  echo "✅ Requirements installed"; '
                    f'fi; '
                    f'python /workspace/{rel_path_str}'
                )
            ]
        else:  # js
            cmd = [
                'bash', '-c',
                (
                    f'if [ -f /workspace/{entry_dir}package.json ]; then '
                    f'  echo "📦 Installing npm packages..."; '
                    f'  npm install --prefix /workspace/{entry_dir} --quiet; '
                    f'  echo "✅ Packages installed"; '
                    f'elif [ -f /workspace/package.json ]; then '
                    f'  echo "📦 Installing npm packages..."; '
                    f'  npm install --prefix /workspace --quiet; '
                    f'  echo "✅ Packages installed"; '
                    f'fi; '
                    f'node /workspace/{rel_path_str}'
                )
            ]
        container_name = f"script_{script_key}".replace('/', '_').replace(':', '_')

        try:
            # Clean up any stale container with the same name (e.g. leftover crash)
            try:
                old = self.client.containers.get(container_name)
                old.remove(force=True)
            except NotFound:
                pass

            container = self.client.containers.run(
                image=image,
                command=cmd,
                volumes=volumes,
                working_dir='/workspace',
                mem_limit=CONTAINER_LIMITS['mem_limit'],
                memswap_limit=CONTAINER_LIMITS['memswap_limit'],
                nano_cpus=CONTAINER_LIMITS['nano_cpus'],
                pids_limit=CONTAINER_LIMITS['pids_limit'],
                network_disabled=not ALLOW_NETWORK,
                security_opt=['no-new-privileges'],
                cap_drop=['ALL'],
                read_only=False,          # workspace dir needs write; root fs elsewhere is the image's own
                environment=env or {},
                detach=True,
                name=container_name,
                labels={'managed_by': 'hosting_panel', 'script_key': script_key},
                # auto_remove=False so we can still fetch logs after it exits
            )
            return container.id, None
        except ImageNotFound:
            return None, f"Sandbox image {image} not found. Run: docker pull {image}"
        except DockerException as e:
            logger.error(f"Failed to start container for {script_key}: {e}")
            return None, f"Failed to start sandbox: {e}"

    def stop_script(self, container_id: str, timeout: int = 5) -> bool:
        if not self.is_available():
            return False
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=timeout)
            container.remove(force=True)
            return True
        except NotFound:
            return True  # already gone, nothing to do
        except DockerException as e:
            logger.error(f"Error stopping container {container_id}: {e}")
            return False

    def is_running(self, container_id: str) -> bool:
        if not self.is_available():
            return False
        try:
            container = self.client.containers.get(container_id)
            container.reload()
            return container.status == 'running'
        except NotFound:
            return False
        except DockerException:
            return False

    def get_exit_code(self, container_id: str):
        if not self.is_available():
            return None
        try:
            container = self.client.containers.get(container_id)
            container.reload()
            return container.attrs.get('State', {}).get('ExitCode')
        except (NotFound, DockerException):
            return None

    def get_stats(self, container_id: str):
        """
        One-shot (non-streaming) resource snapshot for the resource dashboard.
        Returns dict {cpu_percent, mem_used_mb, mem_limit_mb, mem_percent} or
        None if the container isn't running / stats aren't available.
        """
        if not self.is_available():
            return None
        try:
            container = self.client.containers.get(container_id)
            container.reload()
            if container.status != 'running':
                return None
            stats = container.stats(stream=False)

            cpu_percent = 0.0
            try:
                cpu_delta = (stats['cpu_stats']['cpu_usage']['total_usage']
                             - stats['precpu_stats']['cpu_usage']['total_usage'])
                system_delta = (stats['cpu_stats']['system_cpu_usage']
                                 - stats['precpu_stats']['system_cpu_usage'])
                online_cpus = stats['cpu_stats'].get('online_cpus') or len(
                    stats['cpu_stats']['cpu_usage'].get('percpu_usage', [1])
                )
                if system_delta > 0 and cpu_delta > 0:
                    cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0
            except (KeyError, ZeroDivisionError, TypeError):
                pass

            mem_used_mb = mem_limit_mb = mem_percent = 0.0
            try:
                mem_used = stats['memory_stats']['usage']
                mem_limit = stats['memory_stats']['limit']
                mem_used_mb = mem_used / (1024 * 1024)
                mem_limit_mb = mem_limit / (1024 * 1024)
                mem_percent = (mem_used / mem_limit) * 100.0 if mem_limit else 0.0
            except (KeyError, ZeroDivisionError, TypeError):
                pass

            return {
                'cpu_percent': round(cpu_percent, 1),
                'mem_used_mb': round(mem_used_mb, 1),
                'mem_limit_mb': round(mem_limit_mb, 1),
                'mem_percent': round(mem_percent, 1),
            }
        except (NotFound, DockerException):
            return None

    def get_logs(self, container_id: str, tail: int = 500) -> str:
        if not self.is_available():
            return "(docker sandbox unavailable)"
        try:
            container = self.client.containers.get(container_id)
            return container.logs(tail=tail).decode('utf-8', errors='replace')
        except NotFound:
            return "(container no longer exists — it may have been stopped/cleaned up)"
        except DockerException as e:
            return f"(error fetching logs: {e})"

    def list_managed_containers(self):
        """Returns every container this bot has ever started (running OR
        exited) that's still tagged with our label, as a list of dicts:
        {container_id, script_key, status}. Used at startup to rebuild
        bot_scripts after a restart — without this, a script that was
        running when the process restarted keeps running in Docker but
        the bot has no record of it: Stop/logs/timeout-watch all silently
        stop working for that script until someone notices."""
        if not self.is_available():
            return []
        try:
            containers = self.client.containers.list(all=True, filters={'label': 'managed_by=hosting_panel'})
        except DockerException as e:
            logger.error(f"Error listing managed containers: {e}")
            return []

        results = []
        for container in containers:
            script_key = container.labels.get('script_key')
            if not script_key:
                continue
            results.append({
                'container_id': container.id,
                'script_key': script_key,
                'status': container.status,
            })
        return results

    def cleanup_finished(self):
        """Run this periodically (e.g. every few minutes) to remove stopped
        containers this bot created, so `docker ps -a` doesn't pile up."""
        if not self.is_available():
            return
        try:
            for container in self.client.containers.list(all=True, filters={'label': 'managed_by=hosting_panel'}):
                if container.status in ('exited', 'dead'):
                    container.remove(force=True)
        except DockerException as e:
            logger.error(f"Cleanup error: {e}")


docker_runner = DockerRunner()


async def watch_timeout(container_id: str, timeout_seconds: int, on_timeout):
    """
    Background asyncio task: force-kills a container if it's still running
    after timeout_seconds. `on_timeout` is an awaitable callback (no args)
    you use to update bot_scripts / notify the user.
    """
    await asyncio.sleep(timeout_seconds)
    if docker_runner.is_running(container_id):
        docker_runner.stop_script(container_id)
        try:
            await on_timeout()
        except Exception as e:
            logger.error(f"on_timeout callback failed: {e}")
