"""Automates bringing up the legacy application's own Docker
environment (GLPI ships docker-compose.yaml + .docker/), instead of
generating a custom compose file. Mirrors what `make install` does in
GLPI's Makefile, without requiring `make` (not available on this
Windows/Git Bash setup).

Flow: docker compose up -d --build -> wait for the app container to be
ready -> fix git ownership -> install PHP deps -> compile locales ->
run the CLI installer -> health-check the web port -> return the URL.
"""

from __future__ import annotations

import subprocess
import time
import urllib.request
from pathlib import Path

DEFAULT_WEB_PORT = 8080  # GLPI's own default (see .docker/README.md)


def _run(cmd: list[str], cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


def _run_streaming(cmd: list[str], cwd: Path, timeout: int) -> int:
    """Runs a command with output streamed live to the console instead
    of buffered silently - so a long install shows real progress and
    isn't indistinguishable from a hang."""
    proc = subprocess.Popen(
        cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    start = time.time()
    for line in proc.stdout:
        print(line, end="", flush=True)
        if time.time() - start > timeout:
            proc.kill()
            raise subprocess.TimeoutExpired(cmd, timeout)
    proc.wait()
    return proc.returncode


def _healthy(url: str, timeout: int = 180) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            status = urllib.request.urlopen(url, timeout=5).status
            if status < 400:
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def deploy_legacy_app(repo_path: str, web_port: int = DEFAULT_WEB_PORT) -> dict:
    """Brings up GLPI's own docker-compose.yaml and installs the DB.
    Returns {"url": ...} on success, raises RuntimeError with the
    relevant logs on failure."""
    root = Path(repo_path)
    compose_file = root / "docker-compose.yaml"
    if not compose_file.exists():
        raise RuntimeError(f"No docker-compose.yaml found at {root}")

    project = "legacy_glpi"

    # Start the stack. Without this, every `exec` below only works when
    # the containers happen to be running already.
    print("[legacy_deploy] Starting containers (docker compose up -d --build)...", flush=True)
    up = _run(
        ["docker", "compose", "-p", project, "up", "-d", "--build"],
        cwd=root, timeout=1800,
    )
    if up.returncode != 0:
        raise RuntimeError(f"docker compose up failed:\n{up.stderr or up.stdout}")

    # Wait until the app container accepts exec commands.
    print("[legacy_deploy] Waiting for app container...", flush=True)
    ready = False
    for _ in range(30):
        probe = _run(
            ["docker", "compose", "-p", project, "exec", "-T", "app", "true"],
            cwd=root, timeout=30,
        )
        if probe.returncode == 0:
            ready = True
            break
        time.sleep(2)
    if not ready:
        logs = _run(["docker", "compose", "-p", project, "logs", "--tail", "100"], cwd=root).stdout
        raise RuntimeError(f"App container did not become ready.\n\nContainer logs:\n{logs}")

    # Safety net for Windows checkouts: CRLF in bin/ scripts breaks their
    # shebang ("env: 'php\r': No such file or directory").
    _run(
        ["docker", "compose", "-p", project, "exec", "-T", "app",
         "sh", "-c", "sed -i 's/\\r$//' /var/www/glpi/bin/*"],
        cwd=root, timeout=60,
    )

    # Bind-mounted repo has different ownership inside the container
    # than the container user, which makes git refuse to operate on it
    # (composer's own scripts shell out to git under the hood).
    print("[legacy_deploy] Configuring git safe.directory...", flush=True)
    _run(
        ["docker", "compose", "-p", project, "exec", "-T", "app",
         "git", "config", "--global", "--add", "safe.directory", "/var/www/glpi"],
        cwd=root, timeout=30,
    )

    print("[legacy_deploy] Installing PHP dependencies (composer)... this can take 10-30+ min on Windows bind mounts", flush=True)
    returncode = _run_streaming(
        ["docker", "compose", "-p", project, "exec", "-T",
         "-e", "COMPOSER_PROCESS_TIMEOUT=1200",
         "app", "php", "bin/console", "dependencies", "install",
         "--no-interaction", "--no-dev"],
        cwd=root, timeout=2700,  # 45 min ceiling
    )
    if returncode != 0:
        check = _run(
            ["docker", "compose", "-p", project, "exec", "-T", "app",
             "test", "-f", "/var/www/glpi/vendor/autoload.php"],
            cwd=root, timeout=30,
        )
        if check.returncode != 0:
            logs = _run(["docker", "compose", "-p", project, "logs", "--tail", "100"], cwd=root).stdout
            raise RuntimeError(f"dependencies install failed (see streamed output above).\n\nContainer logs:\n{logs}")
        print("[legacy_deploy] Dependencies installed (a harmless post-install script warning was ignored).", flush=True)

    # Give the app container a moment to finish its entrypoint (composer
    # install / cache warmup) before we try to run console commands in it.
    print("[legacy_deploy] Waiting for app container to settle...", flush=True)
    time.sleep(15)

    # GLPI 11 refuses to run db:install until the .po translation files
    # are compiled to .mo ("Application locales have to be compiled").
    print("[legacy_deploy] Compiling locales...", flush=True)
    locales = _run(
        ["docker", "compose", "-p", project, "exec", "-T", "app",
         "php", "bin/console", "tools:locales:compile", "--no-interaction"],
        cwd=root, timeout=600,
    )
    if locales.returncode != 0:
        raise RuntimeError(
            f"tools:locales:compile failed:\n{locales.stderr or locales.stdout}"
        )

    print("[legacy_deploy] Running database install...", flush=True)
    install = _run(
        ["docker", "compose", "-p", project, "exec", "-T", "app",
         "php", "bin/console", "db:install", "--no-interaction",
         "--reconfigure", "--force",
         "--db-host=db", "--db-name=glpi", "--db-user=glpi", "--db-password=glpi"],
        cwd=root, timeout=600,
    )
    print((install.stdout or "")[-2000:], flush=True)
    if install.returncode != 0:
        logs = _run(["docker", "compose", "-p", project, "logs", "--tail", "100"], cwd=root).stdout
        raise RuntimeError(
            f"db:install failed (exit {install.returncode}):\n"
            f"{install.stderr or install.stdout}\n\nContainer logs:\n{logs}"
        )

    url = f"http://localhost:{web_port}"
    print(f"[legacy_deploy] Waiting for {url} to respond...", flush=True)
    if not _healthy(url):
        logs = _run(["docker", "compose", "-p", project, "logs", "--tail", "100"], cwd=root).stdout
        raise RuntimeError(f"App did not become healthy at {url}.\n\nContainer logs:\n{logs}")

    return {"url": url}


def stop_legacy_app(repo_path: str) -> None:
    """Tears down the legacy containers. Safe to call even if nothing
    is running."""
    root = Path(repo_path)
    subprocess.run(
        ["docker", "compose", "-p", "legacy_glpi", "down", "-v"],
        cwd=str(root), capture_output=True, text=True,
    )