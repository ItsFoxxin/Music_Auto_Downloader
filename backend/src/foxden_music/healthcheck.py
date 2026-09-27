from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Sequence


DEFAULT_HEARTBEAT_PATH = Path("/tmp/foxden-music-worker-heartbeat.json")
MAX_SECRET_BYTES = 64 * 1024
SUPPORTED_FILE_SECRETS = ("CSRF_SECRET", "JELLYFIN_API_KEY")


class HealthcheckError(RuntimeError):
    """A concise, non-sensitive health or runtime configuration failure."""


def _heartbeat_path() -> Path:
    return Path(os.environ.get("WORKER_HEARTBEAT_PATH", str(DEFAULT_HEARTBEAT_PATH)))


def write_worker_heartbeat(path: Path | None = None) -> None:
    """Atomically record worker liveness for the container health check.

    The worker should call this at startup and periodically while polling or
    processing. The file belongs in the per-container tmpfs, not persistent
    configuration, so a restarted worker cannot inherit a stale healthy state.
    """

    destination = path or _heartbeat_path()
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        separators=(",", ":"),
    ).encode("utf-8")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        # mkstemp creates the file mode 0600 and avoids a name-race.
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def check_worker_heartbeat() -> None:
    heartbeat = _heartbeat_path()
    try:
        maximum_age = float(os.environ.get("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "120"))
    except ValueError as exc:
        raise HealthcheckError("WORKER_HEARTBEAT_MAX_AGE_SECONDS must be numeric") from exc
    if maximum_age <= 0:
        raise HealthcheckError("WORKER_HEARTBEAT_MAX_AGE_SECONDS must be positive")

    try:
        stat = heartbeat.stat()
    except FileNotFoundError as exc:
        raise HealthcheckError("worker heartbeat has not been created") from exc
    if not heartbeat.is_file():
        raise HealthcheckError("worker heartbeat path is not a regular file")

    age = time.time() - stat.st_mtime
    if age < -5:
        raise HealthcheckError("worker heartbeat timestamp is in the future")
    if age > maximum_age:
        raise HealthcheckError(f"worker heartbeat is stale ({age:.1f}s old)")


def check_web() -> None:
    url = os.environ.get("HEALTHCHECK_WEB_URL", "http://127.0.0.1:8000/health")
    request = urllib.request.Request(url, headers={"User-Agent": "FoxDenMusic-Healthcheck/1"})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 200:
                raise HealthcheckError(f"web health endpoint returned HTTP {response.status}")
            response.read(4096)
    except (OSError, urllib.error.URLError) as exc:
        raise HealthcheckError("web health endpoint is unavailable") from exc


def _read_secret(path: Path, variable: str) -> str:
    try:
        stat = path.stat()
    except OSError as exc:
        raise HealthcheckError(f"cannot read configured {variable}_FILE") from exc
    if not path.is_file():
        raise HealthcheckError(f"configured {variable}_FILE is not a regular file")
    if stat.st_size > MAX_SECRET_BYTES:
        raise HealthcheckError(f"configured {variable}_FILE is unexpectedly large")
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except (OSError, UnicodeError) as exc:
        raise HealthcheckError(f"cannot read configured {variable}_FILE") from exc
    return value


def import_file_secrets() -> None:
    """Load supported secret files without printing their paths or values."""

    for variable in SUPPORTED_FILE_SECRETS:
        if os.environ.get(variable):
            continue
        file_value = os.environ.get(f"{variable}_FILE")
        if not file_value:
            continue
        secret = _read_secret(Path(file_value), variable)
        if secret:
            os.environ[variable] = secret


def _apply_umask() -> None:
    raw_value = os.environ.get("FILE_UMASK", "0027")
    try:
        value = int(raw_value, 8)
    except ValueError as exc:
        raise HealthcheckError("FILE_UMASK must be an octal value") from exc
    if not 0 <= value <= 0o777:
        raise HealthcheckError("FILE_UMASK must be between 0000 and 0777")
    os.umask(value)


def _prepare_temp_directory() -> None:
    """Create an explicitly configured private temp directory before exec."""

    raw_path = os.environ.get("TMPDIR")
    if not raw_path:
        return
    directory = Path(raw_path)
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise OSError("configured temp path is not a directory")
        directory.chmod(0o700)
    except OSError as exc:
        raise HealthcheckError("unable to prepare the configured temp directory") from exc


def _require_non_root_runtime() -> None:
    """Fail closed if a Compose override tries to run an application as root."""

    get_effective_uid = getattr(os, "geteuid", None)
    get_effective_gid = getattr(os, "getegid", None)
    if callable(get_effective_uid) and get_effective_uid() == 0:
        raise HealthcheckError("Fox Den Music refuses to run with effective UID 0")
    if callable(get_effective_gid) and get_effective_gid() == 0:
        raise HealthcheckError("Fox Den Music refuses to run with effective GID 0")


def exec_service(command: Sequence[str]) -> NoReturn:
    selected = list(command)
    if selected and selected[0] == "--":
        selected.pop(0)
    if not selected:
        raise HealthcheckError("runtime wrapper received no service command")
    _require_non_root_runtime()
    import_file_secrets()
    _apply_umask()
    _prepare_temp_directory()
    try:
        os.execvp(selected[0], selected)
    except OSError as exc:
        raise HealthcheckError("unable to start the configured service command") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fox Den Music runtime and container health helpers")
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("web", help="check the local web health endpoint")
    subparsers.add_parser("worker", help="check the worker heartbeat")
    exec_parser = subparsers.add_parser("exec", help="load file secrets and exec a service")
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.action == "web":
            check_web()
        elif arguments.action == "worker":
            check_worker_heartbeat()
        else:
            exec_service(arguments.command)
    except HealthcheckError as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
