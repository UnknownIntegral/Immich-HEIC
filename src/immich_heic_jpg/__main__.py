from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

import requests


LOGGER = logging.getLogger("immich-heic-jpg")
HEIC_EXTENSIONS = {".heic", ".heif"}
HEIC_MIME_TYPES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence"}
MAX_EVENTS = 200
MAX_RUNS = 30


@dataclass(frozen=True)
class Config:
    api_url: str
    api_key: str
    dry_run: bool
    daily_scan_enabled: bool
    daily_scan_time: str
    run_on_start: bool
    jpeg_quality: int
    page_size: int
    max_assets: int
    state_path: Path
    tmp_dir: Path
    remote_duplicate_check: bool
    preserve_metadata: bool
    web_host: str
    web_port: int


@dataclass
class RunSummary:
    id: str
    dry_run: bool
    trigger: str
    started_at: str
    finished_at: str | None = None
    scanned: int = 0
    matched_heic: int = 0
    planned: int = 0
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    status: str = "running"
    error: str | None = None


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = load_config()
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 2

    app = App(config)
    return app.serve()


def load_config() -> Config:
    api_url = os.getenv("IMMICH_API_URL", "").strip().rstrip("/")
    api_key = os.getenv("IMMICH_API_KEY", "").strip()
    daily_scan_time = os.getenv("DAILY_SCAN_TIME", "02:30").strip()

    if not api_url:
        raise ValueError("IMMICH_API_URL is required, for example http://immich-server:2283/api")
    if not api_key or api_key == "replace-with-your-api-key":
        raise ValueError("IMMICH_API_KEY is required")
    parse_daily_time(daily_scan_time)

    return Config(
        api_url=api_url,
        api_key=api_key,
        dry_run=parse_bool("DRY_RUN", default=True),
        daily_scan_enabled=parse_bool("DAILY_SCAN_ENABLED", default=True),
        daily_scan_time=daily_scan_time,
        run_on_start=parse_bool("RUN_ON_START", default=False),
        jpeg_quality=parse_int("JPEG_QUALITY", 92, minimum=1, maximum=100),
        page_size=parse_int("PAGE_SIZE", 250, minimum=1, maximum=1000),
        max_assets=parse_int("MAX_ASSETS", 0, minimum=0),
        state_path=Path(os.getenv("STATE_PATH", "/state/state.json")),
        tmp_dir=Path(os.getenv("TMP_DIR", "/tmp/immich-heic-to-jpg")),
        remote_duplicate_check=parse_bool("REMOTE_DUPLICATE_CHECK", default=True),
        preserve_metadata=parse_bool("PRESERVE_METADATA", default=True),
        web_host=os.getenv("WEB_HOST", "0.0.0.0"),
        web_port=parse_int("WEB_PORT", 8080, minimum=1, maximum=65535),
    )


def parse_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_int(name: str, default: int, minimum: int, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    value = default if raw is None or raw.strip() == "" else int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = ImmichClient(config.api_url, config.api_key)
        self.state = State(config.state_path)
        self.status = StatusStore(config, self.state)
        self.converter = Converter(config.jpeg_quality, config.preserve_metadata)
        self.runner = Runner(config, self.client, self.state, self.status.event)
        self.run_lock = threading.Lock()
        self.stop_event = threading.Event()

    def serve(self) -> int:
        self.status.event("ready", "Dashboard is ready. Originals are never deleted or modified.")
        if self.config.run_on_start:
            self.start_run(trigger="startup", dry_run=self.config.dry_run)

        scheduler = threading.Thread(target=self._schedule_loop, name="daily-scheduler", daemon=True)
        scheduler.start()

        handler = make_handler(self)
        server = ThreadingHTTPServer((self.config.web_host, self.config.web_port), handler)
        LOGGER.info("Dashboard listening on http://%s:%s", self.config.web_host, self.config.web_port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOGGER.info("Interrupted")
            return 130
        finally:
            self.stop_event.set()
            server.server_close()
        return 0

    def start_run(self, trigger: str, dry_run: bool) -> tuple[bool, str]:
        if not self.run_lock.acquire(blocking=False):
            return False, "A scan is already running."

        summary = RunSummary(id=str(int(time.time())), dry_run=dry_run, trigger=trigger, started_at=utc_now())
        self.status.start_run(summary)
        thread = threading.Thread(target=self._run_worker, args=(summary,), name=f"scan-{summary.id}", daemon=True)
        thread.start()
        return True, f"Started {'dry scan' if dry_run else 'conversion scan'}."

    def _run_worker(self, summary: RunSummary) -> None:
        try:
            self.runner.run_once(summary)
            summary.status = "completed"
        except Exception as exc:
            summary.status = "failed"
            summary.error = str(exc)
            LOGGER.exception("Scan failed")
            self.status.event("error", f"Scan failed: {exc}")
        finally:
            summary.finished_at = utc_now()
            self.status.finish_run(summary)
            self.run_lock.release()

    def _schedule_loop(self) -> None:
        while not self.stop_event.is_set():
            next_run = next_daily_run(self.config.daily_scan_time)
            self.status.set_next_run(next_run if self.config.daily_scan_enabled else None)

            if not self.config.daily_scan_enabled:
                self.stop_event.wait(60)
                continue

            wait_seconds = max(1, int(next_run - time.time()))
            if self.stop_event.wait(wait_seconds):
                return
            self.start_run(trigger="schedule", dry_run=self.config.dry_run)


class ImmichClient:
    def __init__(self, api_url: str, api_key: str) -> None:
        self.api_url = api_url
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key})

    def iter_image_assets(self, page_size: int) -> Iterable[dict[str, Any]]:
        page = 1
        while True:
            payload = {
                "type": "IMAGE",
                "withDeleted": False,
                "withExif": False,
                "size": page_size,
                "page": page,
            }
            data = self._request("POST", "/search/metadata", json=payload).json()
            assets = data.get("assets", {})
            items = assets.get("items", [])
            if not items:
                return

            yield from items

            next_page = assets.get("nextPage")
            if next_page is None:
                return
            page = int(next_page)

    def has_asset_with_filename(self, filename: str) -> bool:
        payload = {
            "type": "IMAGE",
            "withDeleted": False,
            "withExif": False,
            "size": 10,
            "page": 1,
            "originalFileName": filename,
        }
        data = self._request("POST", "/search/metadata", json=payload).json()
        items = data.get("assets", {}).get("items", [])
        return any(item.get("originalFileName") == filename for item in items)

    def download_original(self, asset_id: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        response = self._request("GET", f"/assets/{asset_id}/original", stream=True)
        with output_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

    def upload_jpg(self, jpg_path: Path, asset: dict[str, Any], filename: str) -> dict[str, Any]:
        checksum = sha1_base64(jpg_path)
        fields = {
            "fileCreatedAt": asset["fileCreatedAt"],
            "fileModifiedAt": asset["fileModifiedAt"],
            "filename": filename,
            "isFavorite": str(bool(asset.get("isFavorite", False))).lower(),
        }
        if asset.get("visibility"):
            fields["visibility"] = asset["visibility"]

        headers = {"x-immich-checksum": checksum}
        with jpg_path.open("rb") as handle:
            files = {"assetData": (filename, handle, "image/jpeg")}
            return self._request("POST", "/assets", data=fields, files=files, headers=headers).json()

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(method, f"{self.api_url}{path}", timeout=300, **kwargs)
        if response.status_code >= 400:
            detail = response.text[:1000]
            raise RuntimeError(f"{method} {path} failed with HTTP {response.status_code}: {detail}")
        return response


class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()

    def converted(self, asset: dict[str, Any], output_filename: str) -> bool:
        with self.lock:
            item = self.data.get("assets", {}).get(asset["id"])
            return bool(
                item
                and item.get("sourceChecksum") == asset.get("checksum")
                and item.get("outputFilename") == output_filename
                and item.get("status") in {"created", "duplicate"}
            )

    def record(self, asset: dict[str, Any], output_filename: str, status: str, upload_id: str | None = None) -> None:
        with self.lock:
            self.data.setdefault("assets", {})[asset["id"]] = {
                "sourceChecksum": asset.get("checksum"),
                "sourceFilename": asset.get("originalFileName"),
                "outputFilename": output_filename,
                "status": status,
                "uploadId": upload_id,
                "updatedAt": utc_now(),
            }
            self.save_locked()

    def stats(self) -> dict[str, int]:
        with self.lock:
            assets = self.data.get("assets", {})
            created = sum(1 for item in assets.values() if item.get("status") == "created")
            duplicate = sum(1 for item in assets.values() if item.get("status") == "duplicate")
            return {"tracked": len(assets), "created": created, "duplicates": duplicate}

    def save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(self.data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "assets": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))


class StatusStore:
    def __init__(self, config: Config, state: State) -> None:
        self.config = config
        self.state = state
        self.lock = threading.Lock()
        self.started_at = utc_now()
        self.current_run: RunSummary | None = None
        self.last_run: RunSummary | None = None
        self.recent_runs: list[RunSummary] = []
        self.events: list[dict[str, str]] = []
        self.next_run_at: float | None = None

    def start_run(self, summary: RunSummary) -> None:
        with self.lock:
            self.current_run = summary
        self.event("scan", f"Started {'dry scan' if summary.dry_run else 'conversion scan'} from {summary.trigger}.")

    def finish_run(self, summary: RunSummary) -> None:
        with self.lock:
            self.current_run = None
            self.last_run = summary
            self.recent_runs.insert(0, summary)
            del self.recent_runs[MAX_RUNS:]
        label = "completed" if summary.status == "completed" else summary.status
        self.event(
            "scan",
            f"Scan {label}: {summary.matched_heic} HEIC matched, {summary.converted} converted, {summary.skipped} skipped, {summary.failed} failed.",
        )

    def update_run(self, summary: RunSummary) -> None:
        with self.lock:
            self.current_run = summary

    def set_next_run(self, timestamp: float | None) -> None:
        with self.lock:
            self.next_run_at = timestamp

    def event(self, kind: str, message: str) -> None:
        LOGGER.info("%s", message)
        with self.lock:
            self.events.insert(0, {"at": utc_now(), "kind": kind, "message": message})
            del self.events[MAX_EVENTS:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "startedAt": self.started_at,
                "config": public_config(self.config),
                "state": self.state.stats(),
                "currentRun": summary_dict(self.current_run),
                "lastRun": summary_dict(self.last_run),
                "recentRuns": [summary_dict(run) for run in self.recent_runs],
                "events": list(self.events),
                "nextRunAt": local_datetime(self.next_run_at) if self.next_run_at else None,
                "nextRunEpoch": self.next_run_at,
                "safety": [
                    "Original HEIC/HEIF files are never deleted.",
                    "Immich database is never edited directly.",
                    "Converted JPGs are uploaded as new Immich assets.",
                    "Dry scans can be run any time before enabling conversion.",
                    "The API key is read from environment variables and is never shown in the dashboard.",
                ],
            }


class Converter:
    def __init__(self, jpeg_quality: int, preserve_metadata: bool) -> None:
        self.jpeg_quality = jpeg_quality
        self.preserve_metadata = preserve_metadata

    def convert(self, input_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []

        for command in self._conversion_commands(input_path, output_path):
            try:
                run(command)
                break
            except RuntimeError as exc:
                errors.append(str(exc))
        else:
            joined = "\n".join(errors)
            raise RuntimeError(f"All HEIC conversion commands failed:\n{joined}")

        if self.preserve_metadata and shutil.which("exiftool"):
            try:
                run(
                    [
                        "exiftool",
                        "-overwrite_original",
                        "-TagsFromFile",
                        str(input_path),
                        "-all:all",
                        "-unsafe",
                        "-icc_profile",
                        str(output_path),
                    ]
                )
            except RuntimeError as exc:
                LOGGER.warning("Metadata copy failed for %s: %s", input_path.name, exc)

    def _conversion_commands(self, input_path: Path, output_path: Path) -> list[list[str]]:
        commands: list[list[str]] = []
        if shutil.which("magick"):
            commands.append(
                [
                    "magick",
                    str(input_path),
                    "-auto-orient",
                    "-colorspace",
                    "sRGB",
                    "-quality",
                    str(self.jpeg_quality),
                    str(output_path),
                ]
            )
        if shutil.which("convert"):
            commands.append(
                [
                    "convert",
                    str(input_path),
                    "-auto-orient",
                    "-colorspace",
                    "sRGB",
                    "-quality",
                    str(self.jpeg_quality),
                    str(output_path),
                ]
            )
        if shutil.which("heif-convert"):
            commands.append(["heif-convert", "-q", str(self.jpeg_quality), str(input_path), str(output_path)])
        if not commands:
            raise RuntimeError("No converter found. Install ImageMagick or libheif-examples/heif-convert.")
        return commands


class Runner:
    def __init__(
        self,
        config: Config,
        client: ImmichClient,
        state: State,
        event: Callable[[str, str], None],
    ) -> None:
        self.config = config
        self.client = client
        self.state = state
        self.event = event

    def run_once(self, summary: RunSummary) -> None:
        converter = Converter(self.config.jpeg_quality, self.config.preserve_metadata)
        self.event("scan", "Scanning Immich assets through the public API.")

        for asset in self.client.iter_image_assets(self.config.page_size):
            summary.scanned += 1
            if not is_heic_asset(asset):
                continue

            summary.matched_heic += 1
            if self.config.max_assets and summary.matched_heic > self.config.max_assets:
                break

            output_filename = converted_filename(asset)
            if self.state.converted(asset, output_filename):
                summary.skipped += 1
                self.event("skip", f"Already converted: {safe_display(asset_label(asset))}")
                continue

            if self.config.remote_duplicate_check and self.client.has_asset_with_filename(output_filename):
                summary.skipped += 1
                self.state.record(asset, output_filename, "duplicate")
                self.event("skip", f"Already present in Immich: {safe_display(output_filename)}")
                continue

            if summary.dry_run:
                summary.planned += 1
                self.event("plan", f"Would convert {safe_display(asset_label(asset))} to {safe_display(output_filename)}")
                continue

            try:
                self._convert_and_upload(asset, output_filename, converter)
                summary.converted += 1
            except Exception as exc:
                summary.failed += 1
                self.event("error", f"Failed {safe_display(asset_label(asset))}: {exc}")

    def _convert_and_upload(self, asset: dict[str, Any], output_filename: str, converter: Converter) -> None:
        self.config.tmp_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.config.tmp_dir) as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / safe_filename(asset.get("originalFileName") or f"{asset['id']}.heic")
            jpg_path = tmp_path / output_filename

            self.event("download", f"Downloading {safe_display(asset_label(asset))}")
            self.client.download_original(asset["id"], source_path)

            self.event("convert", f"Converting {safe_display(source_path.name)}")
            converter.convert(source_path, jpg_path)

            self.event("upload", f"Uploading {safe_display(output_filename)}")
            response = self.client.upload_jpg(jpg_path, asset, output_filename)
            status = str(response.get("status", "created")).lower()
            upload_id = response.get("id")
            self.state.record(asset, output_filename, status, upload_id)
            self.event("done", f"Uploaded {safe_display(output_filename)} with Immich status {status}.")


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ImmichHeicJpg/1.0"

        def do_GET(self) -> None:
            if self.path == "/" or self.path.startswith("/?"):
                self.send_html(render_dashboard())
                return
            if self.path == "/api/status":
                self.send_json(app.status.snapshot())
                return
            if self.path == "/icon.png":
                icon_path = Path(__file__).resolve().parents[2] / "assets" / "icon.png"
                if icon_path.exists():
                    self.send_bytes(icon_path.read_bytes(), "image/png")
                    return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path == "/api/run-dry":
                started, message = app.start_run(trigger="manual", dry_run=True)
                self.send_json({"ok": started, "message": message}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            if self.path == "/api/run":
                started, message = app.start_run(trigger="manual", dry_run=app.config.dry_run)
                self.send_json({"ok": started, "message": message}, HTTPStatus.ACCEPTED if started else HTTPStatus.CONFLICT)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def send_html(self, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def send_bytes(self, payload: bytes, content_type: str) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: Any) -> None:
            LOGGER.debug("HTTP %s", fmt % args)

    return Handler


def render_dashboard() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Immich HEIC to JPG</title>
  <style>
    :root { color-scheme: light dark; --bg: #f6f7f4; --panel: #ffffff; --text: #17201c; --muted: #61706a; --line: #dce4df; --accent: #0f766e; --warn: #b45309; --bad: #b42318; --good: #137333; }
    @media (prefers-color-scheme: dark) { :root { --bg: #101412; --panel: #19201d; --text: #ecf3ef; --muted: #a7b4ae; --line: #2d3934; --accent: #5eead4; --warn: #fbbf24; --bad: #fb7185; --good: #86efac; } }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    header { padding: 22px clamp(16px, 4vw, 48px); border-bottom: 1px solid var(--line); background: var(--panel); display: flex; gap: 16px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
    h1 { margin: 0; font-size: clamp(22px, 3vw, 32px); letter-spacing: 0; }
    h2 { margin: 0 0 14px; font-size: 16px; }
    main { padding: 24px clamp(16px, 4vw, 48px) 40px; display: grid; gap: 18px; }
    .subtle { color: var(--muted); }
    .grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
    .panel, .metric { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    .metric strong { display: block; font-size: 30px; line-height: 1.1; }
    .metric span { color: var(--muted); }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; }
    button { appearance: none; border: 1px solid var(--accent); background: var(--accent); color: #ffffff; border-radius: 6px; padding: 10px 14px; font-weight: 650; cursor: pointer; }
    button.secondary { background: transparent; color: var(--accent); }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .status { display: inline-flex; align-items: center; gap: 8px; border: 1px solid var(--line); border-radius: 999px; padding: 6px 10px; color: var(--muted); }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--good); }
    .dot.running { background: var(--warn); }
    .dot.failed { background: var(--bad); }
    ul { margin: 0; padding-left: 20px; }
    li { margin: 7px 0; }
    table { width: 100%; border-collapse: collapse; }
    th, td { text-align: left; padding: 9px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; }
    .events { max-height: 360px; overflow: auto; }
    .event { display: grid; grid-template-columns: 150px 90px 1fr; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--line); }
    .kind { color: var(--accent); font-weight: 650; }
    @media (max-width: 720px) { .event { grid-template-columns: 1fr; gap: 2px; } }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Immich HEIC to JPG</h1>
      <div class="subtle">A cautious converter for photos that matter.</div>
    </div>
    <div class="status"><span id="statusDot" class="dot"></span><span id="statusText">Loading</span></div>
  </header>
  <main>
    <section class="grid">
      <div class="metric"><strong id="matched">0</strong><span>HEIC matched in last run</span></div>
      <div class="metric"><strong id="converted">0</strong><span>JPGs converted in last run</span></div>
      <div class="metric"><strong id="planned">0</strong><span>Dry-run conversions planned</span></div>
      <div class="metric"><strong id="tracked">0</strong><span>Conversions tracked in state</span></div>
    </section>
    <section class="panel">
      <h2>Controls</h2>
      <p class="subtle" id="schedule">Checking schedule...</p>
      <div class="actions">
        <button class="secondary" id="dryButton" onclick="postRun('/api/run-dry')">Run Dry Scan Now</button>
        <button id="runButton" onclick="postRun('/api/run')">Run Configured Scan Now</button>
      </div>
      <p class="subtle" id="mode"></p>
    </section>
    <section class="panel">
      <h2>Safety Checks</h2>
      <ul id="safety"></ul>
    </section>
    <section class="panel">
      <h2>Recent Runs</h2>
      <div style="overflow:auto">
        <table>
          <thead><tr><th>Started</th><th>Mode</th><th>Status</th><th>Scanned</th><th>Matched</th><th>Converted</th><th>Skipped</th><th>Failed</th></tr></thead>
          <tbody id="runs"></tbody>
        </table>
      </div>
    </section>
    <section class="panel">
      <h2>Activity</h2>
      <div class="events" id="events"></div>
    </section>
  </main>
  <script>
    async function refresh() {
      const res = await fetch('/api/status', {cache: 'no-store'});
      const data = await res.json();
      const current = data.currentRun;
      const last = data.lastRun || {};
      const source = current || last;
      document.getElementById('matched').textContent = source?.matched_heic ?? 0;
      document.getElementById('converted').textContent = source?.converted ?? 0;
      document.getElementById('planned').textContent = source?.planned ?? 0;
      document.getElementById('tracked').textContent = data.state.tracked ?? 0;
      document.getElementById('statusText').textContent = current ? 'Scan running' : 'Idle';
      document.getElementById('statusDot').className = 'dot' + (current ? ' running' : '');
      document.getElementById('schedule').textContent = data.nextRunAt ? `Next automatic scan: ${data.nextRunAt}` : 'Automatic daily scan is disabled.';
      document.getElementById('mode').textContent = `Configured mode: ${data.config.dryRun ? 'dry-run only' : 'convert and upload'} at ${data.config.dailyScanTime}.`;
      document.getElementById('dryButton').disabled = !!current;
      document.getElementById('runButton').disabled = !!current;
      document.getElementById('safety').innerHTML = data.safety.map(item => `<li>${escapeHtml(item)}</li>`).join('');
      document.getElementById('runs').innerHTML = data.recentRuns.map(run => `<tr><td>${escapeHtml(run.started_at)}</td><td>${run.dry_run ? 'Dry' : 'Convert'}</td><td>${escapeHtml(run.status)}</td><td>${run.scanned}</td><td>${run.matched_heic}</td><td>${run.converted}</td><td>${run.skipped}</td><td>${run.failed}</td></tr>`).join('');
      document.getElementById('events').innerHTML = data.events.map(event => `<div class="event"><code>${escapeHtml(event.at)}</code><span class="kind">${escapeHtml(event.kind)}</span><span>${escapeHtml(event.message)}</span></div>`).join('');
    }
    async function postRun(path) {
      await fetch(path, {method: 'POST'});
      await refresh();
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""


def is_heic_asset(asset: dict[str, Any]) -> bool:
    filename = str(asset.get("originalFileName") or asset.get("originalPath") or "")
    mime_type = str(asset.get("originalMimeType") or "").lower()
    return Path(filename).suffix.lower() in HEIC_EXTENSIONS or mime_type in HEIC_MIME_TYPES


def converted_filename(asset: dict[str, Any]) -> str:
    original = safe_filename(asset.get("originalFileName") or f"{asset['id']}.heic")
    stem = Path(original).stem
    short_id = str(asset["id"]).split("-")[0]
    return safe_filename(f"{stem}__immich_{short_id}.jpg")


def asset_label(asset: dict[str, Any]) -> str:
    return f"{asset.get('originalFileName', 'unknown')} ({asset['id']})"


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    return cleaned or "asset"


def safe_display(value: str) -> str:
    if len(value) <= 160:
        return value
    return value[:157] + "..."


def sha1_base64(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def run(command: list[str]) -> None:
    LOGGER.debug("Running command: %s", command)
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        output = "\n".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
        raise RuntimeError(f"{command[0]} exited {completed.returncode}: {output}")


def public_config(config: Config) -> dict[str, Any]:
    return {
        "apiUrl": config.api_url,
        "apiKey": "set" if config.api_key else "missing",
        "dryRun": config.dry_run,
        "dailyScanEnabled": config.daily_scan_enabled,
        "dailyScanTime": config.daily_scan_time,
        "runOnStart": config.run_on_start,
        "jpegQuality": config.jpeg_quality,
        "pageSize": config.page_size,
        "maxAssets": config.max_assets,
        "remoteDuplicateCheck": config.remote_duplicate_check,
        "preserveMetadata": config.preserve_metadata,
    }


def summary_dict(summary: RunSummary | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    return asdict(summary)


def parse_daily_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value)
    if not match:
        raise ValueError("DAILY_SCAN_TIME must use 24-hour HH:MM format, for example 02:30")
    return int(match.group(1)), int(match.group(2))


def next_daily_run(value: str) -> float:
    hour, minute = parse_daily_time(value)
    now = time.time()
    local = time.localtime(now)
    candidate = time.mktime(
        (
            local.tm_year,
            local.tm_mon,
            local.tm_mday,
            hour,
            minute,
            0,
            local.tm_wday,
            local.tm_yday,
            local.tm_isdst,
        )
    )
    if candidate <= now:
        candidate += 24 * 60 * 60
    return candidate


def local_datetime(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(timestamp))


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit(main())
