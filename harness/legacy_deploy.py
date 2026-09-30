"""Automates bringing up the legacy application's own Docker environment.

Two flows, picked automatically from the checked-out source:

* Modern GLPI (10/11): the repo ships docker-compose.yaml + .docker/, so we
  use it as-is (up -> composer -> locales -> db:install -> health check).
* GLPI 9.x (e.g. tag 9.5.5): the repo ships NO docker setup and needs an
  older PHP (7.2-8.0) plus npm-built front-end libs. We generate a small
  Dockerfile + compose file under <repo>/.harness_deploy/ and run:
  up -> composer install -> npm ci + build -> db:install -> health check.
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



# --------------------------------------------------------------------------
# GLPI 9.x flow
# --------------------------------------------------------------------------
GLPI9_PROJECT = "legacy_glpi9"
GLPI9_DIR = ".harness_deploy"

GLPI9_DOCKERFILE = """\
FROM php:7.4-apache

# php:7.4 images are Debian bullseye (end-of-life): use the archive mirror, main only.
RUN echo 'deb http://archive.debian.org/debian bullseye main' > /etc/apt/sources.list \\
    && apt-get -o Acquire::Check-Valid-Until=false update \\
    && apt-get install -y --no-install-recommends \\
        git unzip patch libpng-dev libjpeg-dev libfreetype6-dev libicu-dev \\
        libzip-dev libbz2-dev libxml2-dev libonig-dev libldap2-dev \\
    && docker-php-ext-configure gd --with-freetype --with-jpeg \\
    && docker-php-ext-install -j2 gd intl mysqli zip bz2 exif opcache ldap \\
    && a2enmod rewrite \\
    && echo "ServerName localhost" > /etc/apache2/conf-enabled/servername.conf \\
    && rm -rf /var/lib/apt/lists/*

COPY --from=composer:2 /usr/bin/composer /usr/bin/composer

RUN { echo "memory_limit=512M"; echo "max_execution_time=300"; \\
      echo "session.cookie_httponly=On"; echo "upload_max_filesize=50M"; \\
      echo "post_max_size=50M"; } > /usr/local/etc/php/conf.d/glpi.ini

WORKDIR /var/www/html
"""

GLPI9_COMPOSE = """\
services:
  app:
    build:
      context: ./.harness_deploy
    ports:
      - "{port}:80"
    volumes:
      - .:/var/www/html
    depends_on:
      - db
  db:
    image: mariadb:10.6
    environment:
      MARIADB_ROOT_PASSWORD: rootpass
      MARIADB_DATABASE: glpi
      MARIADB_USER: glpi
      MARIADB_PASSWORD: glpi
    volumes:
      - dbdata:/var/lib/mysql
  node:
    image: node:16-bullseye
    working_dir: /app
    volumes:
      - .:/app
    profiles: ["tools"]
volumes:
  dbdata:
"""


def _is_glpi9(root: Path) -> bool:
    define = root / "inc" / "define.php"
    if not define.exists() or (root / "docker-compose.yaml").exists():
        return False
    return "'GLPI_VERSION', '9." in define.read_text(encoding="utf-8", errors="ignore")


def _port_in_use(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _dc(root: Path) -> list[str]:
    """Base `docker compose` command for the generated GLPI 9 stack."""
    return ["docker", "compose", "-p", GLPI9_PROJECT,
            "--project-directory", str(root),
            "-f", str(root / GLPI9_DIR / "docker-compose.yml")]


def _deploy_glpi9(repo_path: str, web_port: int = DEFAULT_WEB_PORT) -> dict:
    root = Path(repo_path)
    dc = _dc(root)

    if _port_in_use(web_port):
        raise RuntimeError(
            f"Port {web_port} is already in use (most likely the GLPI 11 stack). "
            "Open the Legacy Deploy page for the old job, click 'Stop and reset', then deploy again."
        )

    out_dir = root / GLPI9_DIR
    out_dir.mkdir(exist_ok=True)
    (out_dir / "Dockerfile").write_text(GLPI9_DOCKERFILE, encoding="utf-8", newline="\n")
    (out_dir / "docker-compose.yml").write_text(
        GLPI9_COMPOSE.replace("{port}", str(web_port)), encoding="utf-8", newline="\n")

    def logs() -> str:
        return _run(dc + ["logs", "--tail", "100"], cwd=root).stdout

    def app_exec(args: list[str], timeout: int = 300, env: dict | None = None):
        cmd = dc + ["exec", "-T"]
        for k, v in (env or {}).items():
            cmd += ["-e", f"{k}={v}"]
        return _run(cmd + ["app"] + args, cwd=root, timeout=timeout)

    print("[legacy_deploy] GLPI 9.x detected - generating PHP 7.4 + MariaDB stack...", flush=True)
    print("[legacy_deploy] Building and starting containers (first build takes a few minutes)...", flush=True)
    up = _run(dc + ["up", "-d", "--build"], cwd=root, timeout=1800)
    if up.returncode != 0:
        raise RuntimeError(f"docker compose up failed:\n{up.stderr or up.stdout}")

    print("[legacy_deploy] Waiting for app container...", flush=True)
    for _ in range(30):
        if app_exec(["true"], timeout=30).returncode == 0:
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"App container did not become ready.\n\nContainer logs:\n{logs()}")

    # Windows safety nets: CRLF in bin/ scripts, and git refusing bind-mounted repo.
    app_exec(["sh", "-c", "sed -i 's/\\r$//' /var/www/html/bin/*"], timeout=60)
    app_exec(["git", "config", "--global", "--add", "safe.directory", "/var/www/html"], timeout=30)

    print("[legacy_deploy] Installing PHP dependencies (composer)... can take 10-30+ min on Windows bind mounts", flush=True)
    rc = _run_streaming(
        dc + ["exec", "-T",
              "-e", "COMPOSER_PROCESS_TIMEOUT=1800", "-e", "COMPOSER_ALLOW_SUPERUSER=1",
              "-e", "COMPOSER_MEMORY_LIMIT=-1",
              "app", "composer", "install", "--no-dev", "--no-interaction", "--prefer-dist"],
        cwd=root, timeout=2700,
    )
    if rc != 0 and app_exec(["test", "-f", "/var/www/html/vendor/autoload.php"], timeout=30).returncode != 0:
        raise RuntimeError(f"composer install failed (see output above).\n\nContainer logs:\n{logs()}")

    # GLPI 9.5 loads public/lib/*.css|js, which only exist after an npm build.
    print("[legacy_deploy] Building front-end libraries (npm ci + npm run build)... can take 10+ min on Windows", flush=True)
    rc = _run_streaming(
        dc + ["run", "--rm", "-T", "node", "sh", "-c",
              "(npm ci --no-audit --no-fund || npm install --no-audit --no-fund) && npm run build"],
        cwd=root, timeout=2700,
    )
    if rc != 0 or not (root / "public" / "lib" / "base.js").exists():
        raise RuntimeError("npm build failed: public/lib/base.js was not produced (see output above).")

    print("[legacy_deploy] Fixing writable directories...", flush=True)
    app_exec(["sh", "-c",
              "mkdir -p /var/www/html/files /var/www/html/config "
              "&& chown -R www-data:www-data /var/www/html/files /var/www/html/config || true"],
             timeout=300)

    print("[legacy_deploy] Running database install...", flush=True)
    install = app_exec(
        ["php", "bin/console", "db:install", "--no-interaction", "--reconfigure", "--force",
         "--db-host=db", "--db-name=glpi", "--db-user=glpi", "--db-password=glpi"],
        timeout=600,
    )
    print((install.stdout or "")[-2000:], flush=True)
    if install.returncode != 0:
        raise RuntimeError(
            f"db:install failed (exit {install.returncode}):\n"
            f"{install.stderr or install.stdout}\n\nContainer logs:\n{logs()}"
        )

    url = f"http://localhost:{web_port}"
    print(f"[legacy_deploy] Waiting for {url} to respond...", flush=True)
    if not _healthy(url):
        raise RuntimeError(f"App did not become healthy at {url}.\n\nContainer logs:\n{logs()}")
    return {"url": url}


def deploy_legacy_app(repo_path: str, web_port: int = DEFAULT_WEB_PORT) -> dict:
    """Picks the right flow for the checked-out source. Returns {"url": ...}
    on success, raises RuntimeError with the relevant logs on failure."""
    root = Path(repo_path)
    if _is_glpi9(root):
        return _deploy_glpi9(repo_path, web_port)
    return _deploy_modern(repo_path, web_port)


def _deploy_modern(repo_path: str, web_port: int = DEFAULT_WEB_PORT) -> dict:
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
    """Tears down the legacy containers (either flow). Safe to call even
    if nothing is running."""
    root = Path(repo_path)
    if (root / GLPI9_DIR / "docker-compose.yml").exists():
        cmd = _dc(root) + ["down", "-v", "--remove-orphans"]
    else:
        cmd = ["docker", "compose", "-p", "legacy_glpi", "down", "-v"]
    subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)