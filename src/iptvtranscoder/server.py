#!/usr/bin/env python3
"""Small stdlib HTTP service for Synology IPTV transcoding."""
from __future__ import annotations

import argparse
import html
import json
import shlex
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .core import (
    ALLOWED_OPERATIONS,
    Config,
    DEFAULT_LOW_POWER_BITRATE_LADDER,
    LOW_POWER_QUALITY_PRESETS,
    LOW_POWER_RATE_FIELDS,
    TranscoderState,
    build_ffmpeg_command,
    cleanup_hls_root,
    load_channels,
    normalize_operation,
    parse_frame_rate,
    safe_channel_id,
    summarize_probe,
)

CONFIG: Config
STATE: TranscoderState
ENV_FILE: Path | None = None
CONFIG_FILE_LOCK = threading.RLock()
ENV_CONFIG_CACHE: dict[str, tuple[str, dict[str, str]]] = {}
HLS_HEALTH_CACHE: dict[str, tuple[tuple[int, int, float], dict]] = {}
HLS_HEALTH_CACHE_LOCK = threading.Lock()
HLS_HEALTH_CACHE_TTL_SECONDS = 0.5
LOG_TAIL_CACHE: dict[str, tuple[tuple[int, int, int], str]] = {}
LOG_TAIL_CACHE_LOCK = threading.Lock()

LOW_POWER_RESOLUTION_LABELS = {
    "720p": "720p",
    "1080p": "1080p",
    "2k": "2K",
    "4k": "4K",
}
LOW_POWER_QUALITY_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
}
LOW_POWER_RATE_FIELD_LABELS = {
    "bitrate": "目标码率",
    "maxrate": "最大码率",
    "bufsize": "缓冲区",
}


def low_power_env_key(resolution: str, preset: str, rate_field: str) -> str:
    return f"IPTV_TRANSCODER_QSV_LOW_POWER_{resolution.upper()}_{preset.upper()}_{rate_field.upper()}"


LOW_POWER_RATE_CONFIG_KEYS = [
    low_power_env_key(resolution, preset, rate_field)
    for resolution in DEFAULT_LOW_POWER_BITRATE_LADDER
    for preset in LOW_POWER_QUALITY_PRESETS
    for rate_field in LOW_POWER_RATE_FIELDS
]

CONFIG_KEYS = [
    "IPTV_TRANSCODER_HOST",
    "IPTV_TRANSCODER_PORT",
    "IPTV_TRANSCODER_MANAGEMENT_PORT",
    "IPTV_TRANSCODER_PUBLIC_BASE_URL",
    "IPTV_TRANSCODER_API_KEY",
    "IPTV_TRANSCODER_FFMPEG",
    "IPTV_TRANSCODER_FFPROBE",
    "IPTV_TRANSCODER_QSV_DEVICE",
    "IPTV_TRANSCODER_ALLOWED_UPSTREAMS",
    "IPTV_TRANSCODER_HARDWARE_ONLY",
    "IPTV_TRANSCODER_MAX_TRANSCODES",
    "IPTV_TRANSCODER_IDLE_TIMEOUT",
    "IPTV_TRANSCODER_GLOBAL_QUALITY",
    "IPTV_TRANSCODER_GLOBAL_QUALITY_4K",
    "IPTV_TRANSCODER_QSV_LOW_POWER_H264",
    "IPTV_TRANSCODER_AUDIO_BITRATE",
    "IPTV_TRANSCODER_HLS_TIME",
    "IPTV_TRANSCODER_HLS_GOP",
    "IPTV_TRANSCODER_HLS_TTL_SECONDS",
    "IPTV_TRANSCODER_HLS_MAX_BYTES",
    "IPTV_TRANSCODER_STARTUP_PROBE_SECONDS",
    "IPTV_TRANSCODER_FFMPEG_STOP_TIMEOUT",
    "IPTV_TRANSCODER_FFPROBE_TIMEOUT",
    "IPTV_TRANSCODER_FFPROBE_ANALYZEDURATION",
    "IPTV_TRANSCODER_FFPROBE_PROBESIZE",
    "IPTV_TRANSCODER_HDR_VPP_BRIGHTNESS",
    "IPTV_TRANSCODER_HDR_VPP_CONTRAST",
] + LOW_POWER_RATE_CONFIG_KEYS

SAFE_MEDIA_BIN_PREFIXES = (
    "/var/packages/Jellyfin/target/bin/",
    "/var/packages/ffmpeg/target/bin/",
    "/var/packages/ffmpeg7/target/bin/",
    "/usr/bin/",
    "/bin/",
    "/usr/local/bin/",
)


@dataclass
class FileResponse:
    path: Path
    content_type: str
    status: int = 200
    cache_control: str = "no-cache"

    @property
    def size(self) -> int:
        return self.path.stat().st_size



def json_bytes(obj, status=200):
    return status, b"application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "IPTVTranscoder/0.2.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

    def _common_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "X-API-Key, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send(self, status: int, ctype: bytes | str, body: bytes):
        if isinstance(ctype, bytes):
            ctype = ctype.decode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        self.send_header("Cache-Control", "no-store" if self.path.startswith("/api/") else "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, response: FileResponse) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(response.size))
        self._common_headers()
        self.send_header("Cache-Control", response.cache_control)
        self.end_headers()
        if self.command != "HEAD":
            with response.path.open("rb") as src:
                shutil.copyfileobj(src, self.wfile, length=1024 * 1024)

    def _send_response(self, response) -> None:
        if isinstance(response, FileResponse):
            self._send_file(response)
            return
        status, ctype, payload = response
        self._send(status, ctype, payload)

    def do_OPTIONS(self):
        self._send(204, "text/plain", b"")

    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        try:
            body = self.read_json_body() if method == "POST" else {}
            response = route(method, self.path, self.headers.get("X-API-Key"), body)
        except HTTPError as e:
            response = json_bytes({"ok": False, "error": e.message}, e.status)
        except Exception as e:
            traceback.print_exc()
            response = json_bytes({"ok": False, "error": "internal error", "detail": str(e)}, 500)
        self._send_response(response)

    def read_json_body(self) -> dict:
        raw_len = self.headers.get("Content-Length", "0") or "0"
        try:
            size = int(raw_len)
        except ValueError as exc:
            raise HTTPError(400, "Invalid Content-Length") from exc
        if size <= 0:
            return {}
        if size > 1024 * 1024:
            raise HTTPError(413, "Request body too large")
        raw = self.rfile.read(size)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            raise HTTPError(400, "Invalid JSON body") from exc
        if not isinstance(data, dict):
            raise HTTPError(400, "JSON body must be an object")
        return data


class ManagementHandler(Handler):
    server_version = "IPTVTranscoderManagement/0.2.0"

    def _dispatch(self, method: str):
        try:
            body = self.read_json_body() if method == "POST" else {}
            response = management_route(method, self.path, self.headers.get("X-API-Key"), body)
        except HTTPError as e:
            response = json_bytes({"ok": False, "error": e.message}, e.status)
        except Exception as e:
            traceback.print_exc()
            response = json_bytes({"ok": False, "error": "internal error", "detail": str(e)}, 500)
        self._send_response(response)


class HTTPError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def require_key(key: str | None):
    if not CONFIG.api_key.strip():
        raise HTTPError(401, "Unauthorized: API key is not configured")
    if key != CONFIG.api_key:
        raise HTTPError(401, "Unauthorized")


def current_env_file() -> Path:
    if ENV_FILE is not None:
        return ENV_FILE
    return CONFIG.log_root.parent / "env"


def parse_env_value(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if value[0] not in {"'", '"'}:
        return value
    try:
        tokens = shlex.split(f"VALUE={value}", posix=True)
    except ValueError:
        return value
    if len(tokens) != 1 or not tokens[0].startswith("VALUE="):
        return value
    return tokens[0].split("=", 1)[1]


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw_text = path.read_text(encoding="utf-8")
    cache_key = str(path.resolve(strict=False)) if hasattr(path, "resolve") else str(path)
    cached = ENV_CONFIG_CACHE.get(cache_key)
    if cached and cached[0] == raw_text:
        return dict(cached[1])
    values: dict[str, str] = {}
    for raw in raw_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in CONFIG_KEYS:
            values[key] = parse_env_value(value)
    ENV_CONFIG_CACHE[cache_key] = (raw_text, dict(values))
    return dict(values)


def config_from_runtime() -> dict[str, str]:
    return {
        "IPTV_TRANSCODER_HOST": CONFIG.host,
        "IPTV_TRANSCODER_PORT": str(CONFIG.port),
        "IPTV_TRANSCODER_MANAGEMENT_PORT": str(CONFIG.management_port),
        "IPTV_TRANSCODER_PUBLIC_BASE_URL": CONFIG.public_base_url,
        "IPTV_TRANSCODER_API_KEY": CONFIG.api_key,
        "IPTV_TRANSCODER_FFMPEG": CONFIG.ffmpeg_bin,
        "IPTV_TRANSCODER_FFPROBE": CONFIG.ffprobe_bin,
        "IPTV_TRANSCODER_QSV_DEVICE": CONFIG.qsv_device,
        "IPTV_TRANSCODER_ALLOWED_UPSTREAMS": CONFIG.allowed_upstreams,
        "IPTV_TRANSCODER_HARDWARE_ONLY": "1" if CONFIG.hardware_only else "0",
        "IPTV_TRANSCODER_MAX_TRANSCODES": str(CONFIG.max_transcodes),
        "IPTV_TRANSCODER_IDLE_TIMEOUT": str(CONFIG.idle_timeout),
        "IPTV_TRANSCODER_GLOBAL_QUALITY": str(CONFIG.global_quality),
        "IPTV_TRANSCODER_GLOBAL_QUALITY_4K": str(CONFIG.global_quality_4k),
        "IPTV_TRANSCODER_QSV_LOW_POWER_H264": "1" if CONFIG.qsv_low_power_h264 else "0",
        "IPTV_TRANSCODER_AUDIO_BITRATE": CONFIG.audio_bitrate,
        "IPTV_TRANSCODER_HLS_TIME": str(CONFIG.hls_time),
        "IPTV_TRANSCODER_HLS_GOP": str(CONFIG.hls_gop),
        "IPTV_TRANSCODER_HLS_TTL_SECONDS": str(CONFIG.hls_ttl_seconds),
        "IPTV_TRANSCODER_HLS_MAX_BYTES": str(CONFIG.hls_max_bytes),
        "IPTV_TRANSCODER_STARTUP_PROBE_SECONDS": str(CONFIG.startup_probe_seconds),
        "IPTV_TRANSCODER_FFMPEG_STOP_TIMEOUT": str(CONFIG.ffmpeg_stop_timeout),
        "IPTV_TRANSCODER_FFPROBE_TIMEOUT": str(CONFIG.ffprobe_timeout),
        "IPTV_TRANSCODER_FFPROBE_ANALYZEDURATION": str(CONFIG.ffprobe_analyzeduration),
        "IPTV_TRANSCODER_FFPROBE_PROBESIZE": str(CONFIG.ffprobe_probesize),
        **{
            low_power_env_key(resolution, preset, rate_field): CONFIG.qsv_low_power_ladder[resolution][preset][rate_field]
            for resolution in DEFAULT_LOW_POWER_BITRATE_LADDER
            for preset in LOW_POWER_QUALITY_PRESETS
            for rate_field in LOW_POWER_RATE_FIELDS
        },
    }


def is_valid_ffmpeg_rate_value(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return False
    if text[-1] in {"k", "m"}:
        return text[:-1].isdigit()
    return text.isdigit()


def shell_env_line(key: str, value: str) -> str:
    safe = str(value).replace("\n", " ").replace("\r", " ").strip()
    if not safe:
        return f"{key}=\n"
    if all(ch.isalnum() or ch in "._/:,@%+-" for ch in safe):
        return f"{key}={safe}\n"
    return f"{key}='" + safe.replace("'", "'\\''") + "'\n"


def validate_media_bin_path(key: str, value: str) -> None:
    if not value:
        raise HTTPError(400, f"{key} must not be empty")
    if not value.startswith("/"):
        raise HTTPError(400, f"{key} must be an absolute path")
    if ".." in Path(value).parts:
        raise HTTPError(400, f"{key} must not contain '..'")
    if not any(value.startswith(prefix) for prefix in SAFE_MEDIA_BIN_PREFIXES):
        raise HTTPError(400, f"{key} must be under an approved bin directory")
    if Path(value).name not in {"ffmpeg", "ffprobe"}:
        raise HTTPError(400, f"{key} must end with ffmpeg/ffprobe")


def validate_public_base_url(value: str) -> None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPError(400, "IPTV_TRANSCODER_PUBLIC_BASE_URL must be http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPError(400, "IPTV_TRANSCODER_PUBLIC_BASE_URL must not include credentials/query/fragment")


def validate_allowed_upstreams_value(value: str) -> None:
    if value.strip() == "*":
        return
    for item in [part.strip() for part in value.split(",") if part.strip()]:
        if ":" not in item or "/" in item:
            raise HTTPError(400, "IPTV_TRANSCODER_ALLOWED_UPSTREAMS must be host:port list or *")
        host, port_s = item.rsplit(":", 1)
        if not host:
            raise HTTPError(400, "IPTV_TRANSCODER_ALLOWED_UPSTREAMS host must not be empty")
        try:
            port = int(port_s)
        except ValueError as exc:
            raise HTTPError(400, "IPTV_TRANSCODER_ALLOWED_UPSTREAMS port must be integer") from exc
        if not 1 <= port <= 65535:
            raise HTTPError(400, "IPTV_TRANSCODER_ALLOWED_UPSTREAMS port must be 1-65535")


def validate_config_update(update: dict[str, str]) -> None:
    for key in update:
        if key not in CONFIG_KEYS:
            raise HTTPError(400, f"Unsupported config key: {key}")
    parsed_ports: dict[str, int] = {}
    for port_key in ["IPTV_TRANSCODER_PORT", "IPTV_TRANSCODER_MANAGEMENT_PORT"]:
        if port_key not in update:
            continue
        try:
            port = int(str(update[port_key]))
        except ValueError as exc:
            raise HTTPError(400, f"{port_key} must be an integer") from exc
        if not 1 <= port <= 65535:
            raise HTTPError(400, f"{port_key} must be 1-65535")
        parsed_ports[port_key] = port
    api_port = parsed_ports.get("IPTV_TRANSCODER_PORT", CONFIG.port)
    management_port = parsed_ports.get("IPTV_TRANSCODER_MANAGEMENT_PORT", CONFIG.management_port)
    if api_port == management_port:
        raise HTTPError(400, "IPTV_TRANSCODER_PORT and IPTV_TRANSCODER_MANAGEMENT_PORT must differ")
    ranges = {
        "IPTV_TRANSCODER_MAX_TRANSCODES": (1, 32),
        "IPTV_TRANSCODER_IDLE_TIMEOUT": (5, 86400),
        "IPTV_TRANSCODER_GLOBAL_QUALITY": (1, 51),
        "IPTV_TRANSCODER_GLOBAL_QUALITY_4K": (1, 51),
        "IPTV_TRANSCODER_HLS_GOP": (10, 300),
        "IPTV_TRANSCODER_HLS_TTL_SECONDS": (60, 604800),
        "IPTV_TRANSCODER_HLS_MAX_BYTES": (10 * 1024 * 1024, 100 * 1024 * 1024 * 1024),
        "IPTV_TRANSCODER_FFPROBE_ANALYZEDURATION": (100000, 20000000),
        "IPTV_TRANSCODER_FFPROBE_PROBESIZE": (100000, 20000000),
    }
    for int_key, (minimum, maximum) in ranges.items():
        if int_key in update and update[int_key]:
            try:
                value = int(str(update[int_key]))
            except ValueError as exc:
                raise HTTPError(400, f"{int_key} must be an integer") from exc
            if not minimum <= value <= maximum:
                raise HTTPError(400, f"{int_key} must be {minimum}-{maximum}")
    bool_keys = {"IPTV_TRANSCODER_HARDWARE_ONLY", "IPTV_TRANSCODER_QSV_LOW_POWER_H264"}
    for bool_key in bool_keys:
        if bool_key in update and str(update[bool_key]).strip() not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
            raise HTTPError(400, f"{bool_key} must be a boolean")
    for rate_key in LOW_POWER_RATE_CONFIG_KEYS:
        if rate_key in update and update[rate_key]:
            if not is_valid_ffmpeg_rate_value(str(update[rate_key])):
                raise HTTPError(400, f"{rate_key} must be an ffmpeg bitrate like 7000k or 20m")
    float_ranges = {
        "IPTV_TRANSCODER_STARTUP_PROBE_SECONDS": (0.0, 5.0),
        "IPTV_TRANSCODER_HLS_TIME": (0.5, 10.0),
        "IPTV_TRANSCODER_FFMPEG_STOP_TIMEOUT": (0.1, 10.0),
        "IPTV_TRANSCODER_FFPROBE_TIMEOUT": (1.0, 30.0),
    }
    for float_key, (minimum, maximum) in float_ranges.items():
        if float_key in update and update[float_key]:
            try:
                value = float(str(update[float_key]))
            except ValueError as exc:
                raise HTTPError(400, f"{float_key} must be a number") from exc
            if not minimum <= value <= maximum:
                raise HTTPError(400, f"{float_key} must be {minimum}-{maximum}")
    for key, value in update.items():
        if "\n" in str(value) or "\r" in str(value):
            raise HTTPError(400, f"{key} must be a single line")
        text = str(value).strip()
        if key in {"IPTV_TRANSCODER_FFMPEG", "IPTV_TRANSCODER_FFPROBE"}:
            validate_media_bin_path(key, text)
        elif key == "IPTV_TRANSCODER_PUBLIC_BASE_URL" and text:
            validate_public_base_url(text)
        elif key == "IPTV_TRANSCODER_ALLOWED_UPSTREAMS":
            validate_allowed_upstreams_value(text)


def get_config(api_key: str | None):
    require_key(api_key)
    with CONFIG_FILE_LOCK:
        env_file = current_env_file()
        config = config_from_runtime()
        config.update(parse_env_file(env_file))
    return json_bytes({"ok": True, "env_file": str(env_file), "config": config, "restart_required": False})


def update_config(api_key: str | None, body: dict):
    require_key(api_key)
    update = {key: str(value) for key, value in body.items()}
    validate_config_update(update)
    with CONFIG_FILE_LOCK:
        env_file = current_env_file()
        config = config_from_runtime()
        config.update(parse_env_file(env_file))
        config.update(update)
        env_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = env_file.with_name(f"{env_file.name}.tmp.{os.getpid()}.{threading.get_ident()}")
        content = "".join(shell_env_line(key, config.get(key, "")) for key in CONFIG_KEYS)
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(env_file)
        try:
            env_file.chmod(0o600)
        except OSError:
            pass
        cache_key = str(env_file.resolve(strict=False)) if hasattr(env_file, "resolve") else str(env_file)
        ENV_CONFIG_CACHE[cache_key] = (content, {key: str(config.get(key, "")) for key in CONFIG_KEYS})
    return json_bytes({"ok": True, "env_file": str(env_file), "config": config, "restart_required": True})


def channel_url(channel_id: str) -> str:
    return f"{CONFIG.public_base_url}/hls/{channel_id}/master.m3u8"


def playlist_segment_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if (line or '').strip() and not line.strip().startswith('#'))


def hls_health_cache_key(channel_id: str) -> str:
    return str(channel_id or "").strip()


def clear_hls_health_cache(channel_id: str) -> None:
    cache_key = hls_health_cache_key(channel_id)
    if not cache_key:
        return
    with HLS_HEALTH_CACHE_LOCK:
        HLS_HEALTH_CACHE.pop(cache_key, None)


def hls_health_status(channel_id: str, now: float | None = None) -> dict:
    now = time.time() if now is None else float(now)
    out_dir = CONFIG.hls_root / channel_id
    playlist = out_dir / "master.m3u8"
    if not playlist.exists() or not playlist.is_file():
        return {"healthy": False, "reason": "missing_playlist", "segment_count": 0, "output_age_seconds": None}
    try:
        playlist_stat = playlist.stat()
    except OSError:
        return {"healthy": False, "reason": "playlist_unreadable", "segment_count": 0, "output_age_seconds": None}
    cache_key = hls_health_cache_key(channel_id)
    cache_signature = (playlist_stat.st_mtime_ns, playlist_stat.st_size, now)
    with HLS_HEALTH_CACHE_LOCK:
        cached = HLS_HEALTH_CACHE.get(cache_key)
        if cached:
            cached_signature, cached_value = cached
            cached_playlist_mtime_ns, cached_playlist_size, cached_now = cached_signature
            if (
                cached_playlist_mtime_ns == playlist_stat.st_mtime_ns
                and cached_playlist_size == playlist_stat.st_size
                and abs(now - cached_now) <= HLS_HEALTH_CACHE_TTL_SECONDS
            ):
                return dict(cached_value)
    try:
        text = playlist.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"healthy": False, "reason": "playlist_unreadable", "segment_count": 0, "output_age_seconds": None}
    segment_count = playlist_segment_count(text)
    if segment_count <= 0:
        return {"healthy": False, "reason": "empty_playlist", "segment_count": 0, "output_age_seconds": None}
    latest_mtime = 0.0
    try:
        for p in out_dir.glob("*"):
            if p.is_file():
                latest_mtime = max(latest_mtime, p.stat().st_mtime)
    except OSError:
        pass
    if latest_mtime <= 0:
        return {"healthy": False, "reason": "missing_segments", "segment_count": segment_count, "output_age_seconds": None}
    output_age = max(0.0, now - latest_mtime)
    stale_after = max(10.0, float(CONFIG.hls_time) * max(3, int(CONFIG.hls_list_size)))
    healthy = output_age <= stale_after
    result = {
        "healthy": healthy,
        "reason": "ok" if healthy else "stale_hls_output",
        "segment_count": segment_count,
        "output_age_seconds": int(output_age),
    }
    with HLS_HEALTH_CACHE_LOCK:
        HLS_HEALTH_CACHE[cache_key] = (cache_signature, dict(result))
    return result


def allowed_upstreams() -> set[str]:
    return {item.strip().lower() for item in CONFIG.allowed_upstreams.split(",") if item.strip()}


def upstream_hostport(input_url: str) -> str:
    parsed = urlparse(input_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return ""
    return f"{parsed.hostname.lower()}:{port}"


def ensure_input_allowed(input_url: str) -> None:
    allowed = allowed_upstreams()
    hostport = upstream_hostport(input_url)
    if not hostport:
        raise HTTPError(400, "input_url must be http(s)")
    if "*" in allowed:
        return
    if not allowed:
        raise HTTPError(403, "No upstream allowlist is configured")
    if hostport not in allowed:
        raise HTTPError(403, f"Input upstream is not allowed: {hostport}")



def html_bytes(text: str, status: int = 200):
    return status, b"text/html; charset=utf-8", text.encode("utf-8")


def render_management_html() -> str:
    host = CONFIG.host if CONFIG.host not in {"0.0.0.0", "::"} else "NAS_IP"
    api_base = "http://%s:%s" % (host, CONFIG.port)
    template = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IPTV Transcoder 管理</title>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    body { margin: 0; background: #f4f6f8; color: #17202a; }
    .wrap { max-width: 980px; margin: 0 auto; padding: 24px; }
    .card { background: #fff; border-radius: 14px; box-shadow: 0 8px 24px rgba(15,23,42,.12); padding: 22px; margin-bottom: 16px; }
    h1 { margin: 0 0 8px; font-size: 24px; } h2 { font-size: 18px; margin: 0 0 14px; }
    label { display:block; font-weight:600; margin:12px 0 6px; }
    input { box-sizing:border-box; width:100%; border:1px solid #cbd5e1; border-radius:9px; padding:10px 12px; font-size:14px; background:#fff; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px 18px; }
    .subgrid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px 18px; }
    .triple { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px 18px; }
    .actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:18px; }
    button { border:0; border-radius:9px; padding:10px 14px; font-weight:700; cursor:pointer; background:#2563eb; color:#fff; }
    button.secondary { background:#475569; } button.warn { background:#b45309; }
    pre { overflow:auto; white-space:pre-wrap; background:#0f172a; color:#e2e8f0; padding:12px; border-radius:9px; }
    .hint { color:#64748b; font-size:13px; line-height:1.6; }
    .section { margin-top: 18px; padding-top: 18px; border-top: 1px solid #e2e8f0; }
    .card-title-row { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
    .task-list { display:grid; gap:10px; }
    .task-row { border:1px solid #e2e8f0; border-radius:10px; padding:12px; display:grid; gap:6px; }
    .task-head { display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; font-weight:700; }
    .pill { display:inline-flex; align-items:center; border-radius:999px; padding:3px 8px; font-size:12px; background:#dcfce7; color:#166534; }
    .pill.warn { background:#fef3c7; color:#92400e; }
    .task-meta { color:#475569; font-size:13px; display:flex; flex-wrap:wrap; gap:8px 14px; }
    .task-link { color:#2563eb; word-break:break-all; font-size:13px; }
    @media (prefers-color-scheme: dark) { body { background:#0b1120; color:#e5e7eb; } .card { background:#111827; } input { background:#0f172a; color:#e5e7eb; border-color:#334155; } .hint { color:#94a3b8; } .task-row { border-color:#334155; } .task-meta { color:#cbd5e1; } .task-link { color:#93c5fd; } }
    @media (prefers-color-scheme: dark) { .section { border-top-color:#334155; } }
    @media (max-width:720px) { .grid, .subgrid, .triple { grid-template-columns:1fr; } .wrap { padding:14px; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>IPTV Transcoder 管理</h1>
      <p class="hint">这是独立 Web 管理界面，默认监听管理端口 <b>__MGMT_PORT__</b>；转码 API/HLS 默认监听端口 <b>__API_PORT__</b>。</p>
      <div class="grid">
        <div>
          <label for="serviceBase">转码服务地址</label>
          <input id="serviceBase" value="__API_BASE__" placeholder="http://192.168.1.100:18096">
          <div class="hint">此地址用于调用 /api/health 和 /api/config。NAS_IP 请改为 NAS 局域网 IP。</div>
        </div>
        <div>
          <label for="authKey">当前 API Key（空值会 fail-closed）</label>
          <input id="authKey" type="password" autocomplete="current-password" placeholder="输入 env 中的 IPTV_TRANSCODER_API_KEY">
        </div>
      </div>
      <div class="actions"><button onclick="health()">检查服务</button><button class="secondary" onclick="loadConfig()">读取配置</button></div>
    </div>
    <div class="card">
      <h2>配置</h2>
      <div class="grid" id="form"></div>
      <div class="section">
        <h2>英特尔低电压模式硬件编码</h2>
        <p class="hint">先控制是否启用 H.264 QSV 低功耗，再按输出分辨率和质量档位调整对应的目标码率、最大码率和缓冲区。</p>
        <div class="triple">
          <div>
            <label for="IPTV_TRANSCODER_QSV_LOW_POWER_H264">H.264 QSV 低功耗</label>
            <input id="IPTV_TRANSCODER_QSV_LOW_POWER_H264">
          </div>
          <div>
            <label for="lpResolution">输出分辨率</label>
            <select id="lpResolution"></select>
          </div>
          <div>
            <label for="lpQuality">质量档位</label>
            <select id="lpQuality"></select>
          </div>
        </div>
        <div class="subgrid" id="lpRates"></div>
      </div>
      <div class="actions"><button id="saveConfigBtn" onclick="saveConfig()">保存配置</button><button class="warn" onclick="fillHost()">用当前主机填充地址</button></div><p class="hint">端口、API Key、监听地址保存后需要在 DSM 套件中心重启 IPTV Transcoder 才会生效。</p>
    </div>
    <div class="card">
      <div class="card-title-row"><h2>实时任务</h2><button class="secondary" onclick="loadTasks()">刷新任务</button></div>
      <p class="hint">展示当前正在启动或运行的实时转码任务；页面会自动刷新。</p>
      <div id="taskList" class="task-list"><p class="hint">当前没有实时转码任务</p></div>
    </div>
    <div class="card"><h2>输出</h2><pre id="out">等待操作...</pre></div>
  </div>
<script>
const keys = [
 ['IPTV_TRANSCODER_HOST','API 监听地址'], ['IPTV_TRANSCODER_PORT','API/HLS 端口'], ['IPTV_TRANSCODER_MANAGEMENT_PORT','管理界面端口'],
 ['IPTV_TRANSCODER_PUBLIC_BASE_URL','Public Base URL'], ['IPTV_TRANSCODER_API_KEY','API Key'], ['IPTV_TRANSCODER_FFMPEG','ffmpeg 路径'],
 ['IPTV_TRANSCODER_FFPROBE','ffprobe 路径'], ['IPTV_TRANSCODER_QSV_DEVICE','QSV 设备'], ['IPTV_TRANSCODER_ALLOWED_UPSTREAMS','上游白名单'],
    ['IPTV_TRANSCODER_HARDWARE_ONLY','仅硬件转码'], ['IPTV_TRANSCODER_MAX_TRANSCODES','最大并发'], ['IPTV_TRANSCODER_IDLE_TIMEOUT','空闲超时秒'],
    ['IPTV_TRANSCODER_GLOBAL_QUALITY','QSV 质量'], ['IPTV_TRANSCODER_GLOBAL_QUALITY_4K','4K QSV 质量'], ['IPTV_TRANSCODER_AUDIO_BITRATE','音频码率'],
 ['IPTV_TRANSCODER_HLS_TIME','自动 HLS 目标分片秒数'], ['IPTV_TRANSCODER_HLS_GOP','自动 HLS GOP 上限'],
 ['IPTV_TRANSCODER_HLS_TTL_SECONDS','HLS 清理 TTL 秒'], ['IPTV_TRANSCODER_HLS_MAX_BYTES','HLS 最大字节数'],
 ['IPTV_TRANSCODER_STARTUP_PROBE_SECONDS','启动确认等待秒'], ['IPTV_TRANSCODER_FFMPEG_STOP_TIMEOUT','ffmpeg 停止等待秒'], ['IPTV_TRANSCODER_FFPROBE_TIMEOUT','ffprobe 超时秒'],
 ['IPTV_TRANSCODER_FFPROBE_ANALYZEDURATION','ffprobe analyzeduration'], ['IPTV_TRANSCODER_FFPROBE_PROBESIZE','ffprobe probesize'],
 ['IPTV_TRANSCODER_HDR_VPP_BRIGHTNESS','VPP 色调映射亮度增益'], ['IPTV_TRANSCODER_HDR_VPP_CONTRAST','VPP 色调映射对比度增益']
];
const lowPowerResolutions = [['720P','720p'], ['1080P','1080p'], ['2K','2K'], ['4K','4K']];
const lowPowerQualities = [['LOW','低'], ['MEDIUM','中'], ['HIGH','高']];
const lowPowerRateFields = [['BITRATE','目标码率'], ['MAXRATE','最大码率'], ['BUFSIZE','缓冲区']];
let configLoaded = false;
let currentConfig = {};
function out(x) { document.getElementById('out').textContent = typeof x === 'string' ? x : JSON.stringify(x, null, 2); }
function base() { return document.getElementById('serviceBase').value.replace(/\/+$/, ''); }
function headers() { return {'X-API-Key': document.getElementById('authKey').value.trim(), 'Content-Type':'application/json'}; }
function setSaveEnabled(enabled) { const btn=document.getElementById('saveConfigBtn'); if(btn) btn.disabled=!enabled; }
function lowPowerConfigKey(resolution, quality, rateField) { return `IPTV_TRANSCODER_QSV_LOW_POWER_${resolution}_${quality}_${rateField}`; }
function ensureLowPowerSelectors() {
  const resolution = document.getElementById('lpResolution');
  const quality = document.getElementById('lpQuality');
  if (!resolution.options.length) {
    for (const [value, label] of lowPowerResolutions) {
      const option=document.createElement('option'); option.value=value; option.textContent=label; resolution.appendChild(option);
    }
  }
  if (!quality.options.length) {
    for (const [value, label] of lowPowerQualities) {
      const option=document.createElement('option'); option.value=value; option.textContent=label; quality.appendChild(option);
    }
  }
  resolution.onchange = renderLowPowerRateFields;
  quality.onchange = renderLowPowerRateFields;
}
function renderLowPowerRateFields() {
  const resolution = document.getElementById('lpResolution').value || '1080P';
  const quality = document.getElementById('lpQuality').value || 'MEDIUM';
  const wrap = document.getElementById('lpRates');
  wrap.replaceChildren();
  for (const [rateField, label] of lowPowerRateFields) {
    const key = lowPowerConfigKey(resolution, quality, rateField);
    const box = document.createElement('div');
    const l = document.createElement('label');
    l.textContent = label;
    l.htmlFor = key;
    const i = document.createElement('input');
    i.id = key;
    i.value = currentConfig[key] || '';
    i.oninput = () => { currentConfig[key] = i.value.trim(); };
    box.appendChild(l);
    box.appendChild(i);
    wrap.appendChild(box);
  }
}
function render(config={}) {
  currentConfig = {...config};
  const f=document.getElementById('form');
  f.replaceChildren();
  for (const [k,label] of keys) {
    const box=document.createElement('div');
    const l=document.createElement('label');
    l.textContent=label;
    l.htmlFor=k;
    const i=document.createElement('input');
    i.id=k;
    i.value=config[k]||'';
    i.oninput = () => { currentConfig[k] = i.value; };
    if(k==='IPTV_TRANSCODER_API_KEY') i.type='password';
    box.appendChild(l);
    box.appendChild(i);
    f.appendChild(box);
  }
  const lowPowerToggle = document.getElementById('IPTV_TRANSCODER_QSV_LOW_POWER_H264');
  if (lowPowerToggle) {
    lowPowerToggle.value = config.IPTV_TRANSCODER_QSV_LOW_POWER_H264 || '';
    lowPowerToggle.oninput = () => { currentConfig.IPTV_TRANSCODER_QSV_LOW_POWER_H264 = lowPowerToggle.value; };
  }
  ensureLowPowerSelectors();
  renderLowPowerRateFields();
}
async function readJsonResponse(r) { const text=await r.text(); try { return JSON.parse(text || '{}'); } catch(e) { return {ok:false,error:'响应不是 JSON',status:r.status,body:text.slice(0,500)}; } }
function node(tag, className, text) { const n=document.createElement(tag); if(className) n.className=className; if(text!==undefined) n.textContent=text; return n; }
function renderTasks(tasks=[]) { const list=document.getElementById('taskList'); list.replaceChildren(); if(!tasks.length){ list.appendChild(node('p','hint','当前没有实时转码任务')); return; } for(const task of tasks){ const row=node('div','task-row'); const head=node('div','task-head'); const title=node('span','',task.channel_id||task.job_id||'未知任务'); const state=node('span', task.idle_expired?'pill warn':'pill', task.state==='starting'?'启动中':(task.running?'运行中':'已退出')); head.appendChild(title); head.appendChild(state); const meta=node('div','task-meta'); const items=['PID '+(task.pid||'-'),'心跳 '+(task.seconds_since_heartbeat==null?'-':task.seconds_since_heartbeat+' 秒前'),'空闲超时 '+(task.idle_timeout||'-')+' 秒']; for(const item of items) meta.appendChild(node('span','',item)); row.appendChild(head); row.appendChild(meta); if(task.hls_url){ const link=node('a','task-link',task.hls_url); link.href=task.hls_url; link.target='_blank'; link.rel='noreferrer'; row.appendChild(link); } list.appendChild(row); } }
async function loadTasks() { try { const r=await fetch(base()+'/api/status',{headers:headers()}); const d=await readJsonResponse(r); if(!r.ok||!d.ok){renderTasks([]); out(Object.assign({message:explainFailure('读取实时任务',r,d)}, d)); return;} renderTasks(d.tasks||[]); } catch(e) { renderTasks([]); out('读取实时任务失败：'+e); } }
function explainFailure(action, r, d) { if (r.status===401) return action+'失败：API Key 不匹配或运行中的服务尚未加载这个 key。请确认只复制 env 里等号后的值；如果刚改过 env，需要在 DSM 套件中心重启 IPTV Transcoder。'; if (r.status===404) return action+'失败：请求地址没有这个接口。新版管理端口也会代理 /api/config、/api/status；旧版请把“转码服务地址”填 API 端口 http://NAS_IP:18096。'; return action+'失败：HTTP '+r.status+' '+(d.error||''); }
async function health() { try { const r=await fetch(base()+'/api/health/details',{headers:headers()}); const d=await readJsonResponse(r); out(r.ok&&d.ok ? d : Object.assign({message:explainFailure('服务检查',r,d)}, d)); } catch(e) { out('服务检查失败：'+e); } }
async function loadConfig() { try { const r=await fetch(base()+'/api/config',{headers:headers()}); const d=await readJsonResponse(r); if(!r.ok||!d.ok){configLoaded=false;setSaveEnabled(false);out(Object.assign({message:explainFailure('读取配置',r,d)}, d));return;} render(d.config); configLoaded=true; setSaveEnabled(true); out(d); } catch(e) { configLoaded=false; setSaveEnabled(false); out('读取配置失败：'+e); } }
async function saveConfig() { if(!configLoaded){out('请先成功读取配置，再保存，避免空表单覆盖配置');return;} const body={...currentConfig}; for(const [k] of keys) body[k]=document.getElementById(k)?.value||''; try { const r=await fetch(base()+'/api/config',{method:'POST',headers:headers(),body:JSON.stringify(body)}); const d=await readJsonResponse(r); out(r.ok&&d.ok ? d : Object.assign({message:explainFailure('保存配置',r,d)}, d)); if(d.ok&&d.config){render(d.config); configLoaded=true; setSaveEnabled(true);} if(d.ok&&body.IPTV_TRANSCODER_API_KEY) document.getElementById('authKey').value=body.IPTV_TRANSCODER_API_KEY; } catch(e) { out('保存配置失败：'+e); } }
function fillHost() { const host=location.hostname||'NAS_IP'; const apiPort=document.getElementById('IPTV_TRANSCODER_PORT')?.value||'18096'; document.getElementById('serviceBase').value='http://'+host+':'+apiPort; const pub=document.getElementById('IPTV_TRANSCODER_PUBLIC_BASE_URL'); if(pub) pub.value='http://'+host+':'+apiPort; }
render(); fillHost(); setSaveEnabled(false); loadTasks(); setInterval(loadTasks, 5000);
</script>
</body>
</html>"""
    return (template
        .replace("__API_BASE__", html.escape(api_base, quote=True))
        .replace("__API_PORT__", str(CONFIG.port))
        .replace("__MGMT_PORT__", str(CONFIG.management_port)))


def management_route(method: str, raw_path: str, api_key: str | None, body: dict):
    parsed = urlparse(raw_path)
    path = unquote(parsed.path)
    if path == "/" and method == "GET":
        return html_bytes(render_management_html())
    if path in {"/api/health", "/api/health/details", "/api/config", "/api/status"}:
        return route(method, raw_path, api_key, body)
    raise HTTPError(404, "Not found")


def current_task_status() -> dict:
    now = time.time()
    with STATE.lock:
        STATE.cleanup_dead()
        channels = {
            cid: {"pid": proc.pid, "running": proc.poll() is None, "last_heartbeat": STATE.heartbeats.get(cid)}
            for cid, proc in list(STATE.processes.items())
        }
        starting_ids = [cid for cid in sorted(STATE.starting) if cid not in channels]
        idle_timeout = STATE.idle_timeout
    tasks = []
    for cid, item in sorted(channels.items()):
        last_heartbeat = item.get("last_heartbeat")
        seconds_since_heartbeat = None
        idle_expired = False
        if last_heartbeat is not None:
            seconds_since_heartbeat = max(0, int(now - float(last_heartbeat)))
            idle_expired = seconds_since_heartbeat > idle_timeout
        hls_health = hls_health_status(cid, now=now)
        tasks.append({
            "channel_id": cid,
            "job_id": cid,
            "pid": item.get("pid"),
            "state": "running" if item.get("running") else "exited",
            "running": bool(item.get("running")),
            "last_heartbeat": last_heartbeat,
            "seconds_since_heartbeat": seconds_since_heartbeat,
            "idle_timeout": idle_timeout,
            "idle_expired": idle_expired,
            "hls_url": channel_url(cid),
            "hls_healthy": hls_health["healthy"],
            "hls_reason": hls_health["reason"],
            "hls_output_age_seconds": hls_health["output_age_seconds"],
        })
    for cid in starting_ids:
        tasks.append({
            "channel_id": cid,
            "job_id": cid,
            "pid": None,
            "state": "starting",
            "running": False,
            "last_heartbeat": None,
            "seconds_since_heartbeat": None,
            "idle_timeout": idle_timeout,
            "idle_expired": False,
            "hls_url": channel_url(cid),
            "hls_healthy": False,
            "hls_reason": "starting",
            "hls_output_age_seconds": None,
        })
    running = [cid for cid, item in sorted(channels.items()) if item.get("running")]
    return {
        "ok": True,
        "time": now,
        "running": running,
        "running_count": len(running),
        "max_transcodes": CONFIG.max_transcodes,
        "channels": channels,
        "tasks": tasks,
    }


def parse_max_bytes_param(raw_path: str, default: int = 12000, minimum: int = 256, maximum: int = 512000) -> int:
    parsed = urlparse(raw_path)
    query = parse_qs(parsed.query or "", keep_blank_values=False)
    raw_value = (query.get("max_bytes") or query.get("max") or [None])[0]
    if raw_value in {None, ""}:
        return default
    try:
        value = int(str(raw_value))
    except ValueError as exc:
        raise HTTPError(400, "max_bytes must be an integer") from exc
    if value < minimum or value > maximum:
        raise HTTPError(400, f"max_bytes must be {minimum}-{maximum}")
    return value


def route(method: str, raw_path: str, api_key: str | None, body: dict):
    parsed = urlparse(raw_path)
    path = unquote(parsed.path)

    if path == "/" and method == "GET":
        return json_bytes({
            "ok": True,
            "name": "IPTV Transcoder",
            "version": "0.2.0",
            "hardware_only": CONFIG.hardware_only,
            "operations": sorted(ALLOWED_OPERATIONS),
            "endpoints": ["/api/health", "/api/status", "/api/probe", "/api/transcode/start", "/api/config"],
        })

    if path == "/api/config" and method == "GET":
        return get_config(api_key)

    if path == "/api/config" and method == "POST":
        return update_config(api_key, body)

    if path == "/api/health" and method == "GET":
        return json_bytes({
            "ok": True,
            "time": time.time(),
            "running": STATE.running_count(),
            "max_transcodes": CONFIG.max_transcodes,
        })

    if path == "/api/health/details" and method == "GET":
        require_key(api_key)
        return json_bytes({
            "ok": True,
            "time": time.time(),
            "running": STATE.running_count(),
            "max_transcodes": CONFIG.max_transcodes,
            "hardware": {
                "mode": "qsv",
                "device": CONFIG.qsv_device,
                "device_exists": Path(CONFIG.qsv_device).exists(),
                "hardware_only": CONFIG.hardware_only,
                "encoder": "h264_qsv",
            },
            "ffmpeg": {"path": CONFIG.ffmpeg_bin, "exists": Path(CONFIG.ffmpeg_bin).exists()},
            "ffprobe": {"path": CONFIG.ffprobe_bin, "exists": Path(CONFIG.ffprobe_bin).exists()},
            "hls": {"root": str(CONFIG.hls_root), "ttl_seconds": CONFIG.hls_ttl_seconds, "max_bytes": CONFIG.hls_max_bytes},
            "operations": sorted(ALLOWED_OPERATIONS),
        })

    if path == "/api/channels" and method == "GET":
        require_key(api_key)
        channels = load_channels(CONFIG.channels_file)
        redacted = {cid: {k: v for k, v in ch.items() if k != "url"} for cid, ch in channels.items()}
        return json_bytes({"ok": True, "channels": redacted})

    if path == "/api/status" and method == "GET":
        require_key(api_key)
        return json_bytes(current_task_status())

    if path == "/api/probe" and method == "POST":
        require_key(api_key)
        return probe_dynamic(body)

    if path == "/api/transcode/start" and method == "POST":
        require_key(api_key)
        return start_dynamic(body)

    parts = [p for p in path.split("/") if p]
    if len(parts) == 4 and parts[:2] == ["api", "channels"]:
        _, _, channel_id, action = parts
        if not safe_channel_id(channel_id):
            raise HTTPError(400, "Unsafe channel id")
        if action == "start" and method == "GET":
            raise HTTPError(405, "Use POST to start transcoding")
        if action == "start" and method == "POST":
            require_key(api_key)
            channels = load_channels(CONFIG.channels_file)
            ch = channels.get(channel_id)
            if not ch:
                raise HTTPError(404, "Unknown channel")
            ensure_input_allowed(ch["url"])
            ch = enrich_channel_from_probe(ch)
            return start_job(channel_id, ch)
        if action == "heartbeat" and method == "POST":
            require_key(api_key)
            ok = STATE.heartbeat(channel_id)
            if not ok:
                if STATE.is_starting(channel_id):
                    return startup_heartbeat_response(channel_id)
                return json_bytes({"ok": False, "channel_id": channel_id, "hls_healthy": False, "reason": "process_not_running"})
            health = hls_health_status(channel_id)
            return json_bytes({"ok": health["healthy"], "channel_id": channel_id, "hls_healthy": health["healthy"], "reason": health["reason"], "segment_count": health["segment_count"], "output_age_seconds": health["output_age_seconds"]})
        if action == "stop" and method == "POST":
            require_key(api_key)
            stopped = STATE.stop(channel_id)
            if stopped:
                clear_channel_runtime_caches(channel_id)
            return json_bytes({"ok": stopped, "channel_id": channel_id})
        if action == "probe" and method == "GET":
            require_key(api_key)
            channels = load_channels(CONFIG.channels_file)
            ch = channels.get(channel_id)
            if not ch:
                raise HTTPError(404, "Unknown channel")
            ensure_input_allowed(ch["url"])
            return probe_url(channel_id, ch["url"])

    if len(parts) == 4 and parts[:2] == ["api", "transcode"]:
        _, _, channel_id, action = parts
        if not safe_channel_id(channel_id):
            raise HTTPError(400, "Unsafe channel id")
        require_key(api_key)
        if action == "heartbeat" and method == "POST":
            ok = STATE.heartbeat(channel_id)
            if not ok:
                if STATE.is_starting(channel_id):
                    return startup_heartbeat_response(channel_id)
                return json_bytes({"ok": False, "channel_id": channel_id, "hls_healthy": False, "reason": "process_not_running"})
            health = hls_health_status(channel_id)
            return json_bytes({"ok": health["healthy"], "channel_id": channel_id, "hls_healthy": health["healthy"], "reason": health["reason"], "segment_count": health["segment_count"], "output_age_seconds": health["output_age_seconds"]})
        if action == "stop" and method == "POST":
            stopped = STATE.stop(channel_id)
            if stopped:
                clear_channel_runtime_caches(channel_id)
            return json_bytes({"ok": stopped, "channel_id": channel_id})

    if len(parts) == 3 and parts[:2] == ["api", "logs"] and method == "GET":
        _, _, channel_id = parts
        require_key(api_key)
        max_bytes = parse_max_bytes_param(raw_path)
        return json_bytes({"ok": True, "channel_id": channel_id, "log_tail": read_log_tail(channel_id, max_bytes=max_bytes)})

    if path == "/api/logs/service" and method == "GET":
        require_key(api_key)
        max_bytes = parse_max_bytes_param(raw_path)
        return json_bytes({"ok": True, "channel_id": "service", "log_tail": read_service_log_tail(max_bytes=max_bytes)})

    if path.startswith("/hls/") and method in {"GET", "HEAD"}:
        return serve_hls(path)

    raise HTTPError(404, "Not found")


def validate_job_body(body: dict) -> tuple[str, dict]:
    channel_id = str(body.get("channel_id") or body.get("job_id") or "").strip()
    input_url = str(body.get("input_url") or body.get("url") or "").strip()
    operation = normalize_operation(str(body.get("operation") or body.get("mode") or "qsv_h264"))
    if not safe_channel_id(channel_id):
        raise HTTPError(400, "Unsafe channel id")
    if not input_url.startswith(("http://", "https://")):
        raise HTTPError(400, "input_url must be http(s)")
    ensure_input_allowed(input_url)
    if operation not in ALLOWED_OPERATIONS:
        raise HTTPError(400, f"Unsupported operation: {operation}")
    resolution = str(body.get("resolution") or "auto").strip().lower()
    if resolution not in {"auto", "720p", "1080p", "2k", "4k"}:
        raise HTTPError(400, "Unsupported resolution")
    quality_preset = str(body.get("quality_preset") or "default").strip().lower()
    if quality_preset not in {"default", *LOW_POWER_QUALITY_PRESETS}:
        raise HTTPError(400, "Unsupported quality_preset")
    global_quality = body.get("global_quality")
    if global_quality not in {None, ""}:
        try:
            quality = int(str(global_quality))
        except ValueError as exc:
            raise HTTPError(400, "global_quality must be an integer") from exc
        if not 1 <= quality <= 51:
            raise HTTPError(400, "global_quality must be 1-51")
    try:
        width = int(body.get("width") or 0)
        height = int(body.get("height") or 0)
    except (TypeError, ValueError) as exc:
        raise HTTPError(400, "width and height must be integers") from exc
    if width < 0 or height < 0:
        raise HTTPError(400, "width and height must be non-negative")
    return channel_id, {
        "url": input_url,
        "operation": operation,
        "video_codec": str(body.get("video_codec") or ""),
        "video_profile": str(body.get("video_profile") or body.get("profile") or ""),
        "pix_fmt": str(body.get("pix_fmt") or ""),
        "audio_codec": str(body.get("audio_codec") or ""),
        "audio_only": bool(body.get("audio_only", False)),
        "color_transfer": str(body.get("color_transfer") or ""),
        "color_space": str(body.get("color_space") or body.get("colorspace") or ""),
        "color_primaries": str(body.get("color_primaries") or ""),
        "color_range": str(body.get("color_range") or ""),
        "fps": str(body.get("fps") or ""),
        "avg_frame_rate": str(body.get("avg_frame_rate") or ""),
        "r_frame_rate": str(body.get("r_frame_rate") or ""),
        "width": width,
        "height": height,
        "resolution": resolution,
        "quality_preset": quality_preset,
        "global_quality": global_quality,
        "force_aac": bool(body.get("force_aac", False)),
        "loglevel": str(body.get("loglevel") or "warning"),
    }


def start_dynamic(body: dict):
    channel_id, ch = validate_job_body(body)
    ch = enrich_channel_from_probe(ch)
    return start_job(channel_id, ch, reason=str(body.get("reason") or "dynamic"))


def rotate_file(path: Path, max_bytes: int = 10 * 1024 * 1024) -> None:
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            rotated = path.with_suffix(path.suffix + ".1")
            if rotated.exists():
                rotated.unlink()
            path.rename(rotated)
            cache_prefix = f"{path.resolve()}:" if hasattr(path, "resolve") else f"{path}:"
            with LOG_TAIL_CACHE_LOCK:
                stale_keys = [key for key in LOG_TAIL_CACHE if key.startswith(cache_prefix)]
                for key in stale_keys:
                    LOG_TAIL_CACHE.pop(key, None)
    except OSError:
        pass


def read_log_tail(channel_id: str, max_bytes: int = 12000) -> str:
    if not safe_channel_id(channel_id):
        raise HTTPError(400, "Unsafe channel id")
    log_path = (CONFIG.log_root / f"{channel_id}.log").resolve()
    root = CONFIG.log_root.resolve()
    if root not in [log_path, *log_path.parents]:
        raise HTTPError(403, "Forbidden")
    if not log_path.exists() or not log_path.is_file():
        return ""
    try:
        stat = log_path.stat()
    except OSError:
        return ""
    cache_key = f"{log_path}:{max_bytes}"
    cache_signature = (stat.st_mtime_ns, stat.st_size, max_bytes)
    with LOG_TAIL_CACHE_LOCK:
        cached = LOG_TAIL_CACHE.get(cache_key)
        if cached and cached[0] == cache_signature:
            return cached[1]
    with log_path.open("rb") as f:
        try:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
        except OSError:
            pass
        result = f.read(max_bytes).decode("utf-8", errors="replace")
    with LOG_TAIL_CACHE_LOCK:
        LOG_TAIL_CACHE[cache_key] = (cache_signature, result)
    return result


def read_service_log_tail(max_bytes: int = 12000) -> str:
    log_path = (CONFIG.log_root / "service.log").resolve()
    root = CONFIG.log_root.resolve()
    if root not in [log_path, *log_path.parents]:
        raise HTTPError(403, "Forbidden")
    if not log_path.exists() or not log_path.is_file():
        return ""
    try:
        stat = log_path.stat()
    except OSError:
        return ""
    cache_key = f"{log_path}:{max_bytes}"
    cache_signature = (stat.st_mtime_ns, stat.st_size, max_bytes)
    with LOG_TAIL_CACHE_LOCK:
        cached = LOG_TAIL_CACHE.get(cache_key)
        if cached and cached[0] == cache_signature:
            return cached[1]
    with log_path.open("rb") as f:
        try:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
        except OSError:
            pass
        result = f.read(max_bytes).decode("utf-8", errors="replace")
    with LOG_TAIL_CACHE_LOCK:
        LOG_TAIL_CACHE[cache_key] = (cache_signature, result)
    return result


def clear_log_tail_cache(channel_id: str) -> None:
    if not safe_channel_id(channel_id):
        return
    try:
        log_path = (CONFIG.log_root / f"{channel_id}.log").resolve()
    except Exception:
        return
    cache_prefix = f"{log_path}:"
    with LOG_TAIL_CACHE_LOCK:
        stale_keys = [key for key in LOG_TAIL_CACHE if key.startswith(cache_prefix)]
        for key in stale_keys:
            LOG_TAIL_CACHE.pop(key, None)


def clear_channel_runtime_caches(channel_id: str) -> None:
    clear_hls_health_cache(channel_id)
    clear_log_tail_cache(channel_id)


def clear_hls_caches_for_removed_paths(paths: list[str]) -> None:
    if not paths:
        return
    channel_ids = set()
    for raw_path in paths:
        try:
            rel = Path(raw_path).resolve().relative_to(CONFIG.hls_root.resolve())
        except Exception:
            continue
        if rel.parts:
            channel_id = rel.parts[0]
            if safe_channel_id(channel_id):
                channel_ids.add(channel_id)
    for channel_id in channel_ids:
        clear_hls_health_cache(channel_id)



JOB_SPEC_KEYS = (
    "url",
    "operation",
    "resolution",
    "video_codec",
    "video_profile",
    "pix_fmt",
    "audio_codec",
    "audio_only",
    "color_transfer",
    "color_space",
    "color_primaries",
    "color_range",
    "width",
    "height",
    "fps",
    "avg_frame_rate",
    "r_frame_rate",
    "quality_preset",
    "global_quality",
    "force_aac",
    "gop",
    "keyint_min",
    "hls_time",
)


def job_spec(ch: dict) -> tuple[tuple[str, str], ...]:
    return tuple((key, str(ch.get(key) or "")) for key in JOB_SPEC_KEYS)


def probe_stream_summary(input_url: str) -> tuple[object, dict, dict]:
    cmd = [
        CONFIG.ffprobe_bin, "-hide_banner", "-v", "error",
        "-analyzeduration", str(CONFIG.ffprobe_analyzeduration), "-probesize", str(CONFIG.ffprobe_probesize),
        "-print_format", "json",
        "-show_streams",
        "-show_programs",
        input_url,
    ]
    best: tuple[int, object, dict, dict] | None = None
    attempts = 3
    for attempt in range(attempts):
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=CONFIG.ffprobe_timeout)
        except subprocess.TimeoutExpired:
            raise HTTPError(504, "ffprobe timed out")
        except OSError as exc:
            raise HTTPError(502, f"ffprobe start failed: {exc}") from exc
        try:
            parsed = json.loads(res.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPError(502, f"invalid ffprobe json: {exc}") from exc
        if not isinstance(parsed, dict):
            raise HTTPError(502, "ffprobe JSON root is not an object")
        summary = summarize_probe(parsed)
        score = probe_summary_score(summary)
        if best is None or probe_summary_should_replace_best(best[0], best[3], score, summary):
            best = (score, res, parsed, summary)
        if probe_summary_is_complete(summary) and not probe_summary_is_live_tv_interlace_ambiguous(summary):
            break
        if attempt + 1 < attempts:
            time.sleep(0.15)
    assert best is not None
    return best[1], best[2], best[3]


def channel_needs_probe_metadata(ch: dict) -> bool:
    if parse_frame_rate(ch.get("fps")) is None and parse_frame_rate(ch.get("avg_frame_rate")) is None and parse_frame_rate(ch.get("r_frame_rate")) is None:
        return True
    required = ("video_codec", "video_profile", "pix_fmt", "audio_codec", "width", "height")
    return any(not ch.get(key) for key in required)


def enrich_channel_from_probe(ch: dict) -> dict:
    if not ch.get("url") or not channel_needs_probe_metadata(ch):
        return ch
    res, _parsed, summary = probe_stream_summary(str(ch["url"]))
    if getattr(res, "returncode", 1) != 0:
        return ch
    enriched = dict(ch)
    for key in (
        "video_codec",
        "video_profile",
        "pix_fmt",
        "audio_codec",
        "color_transfer",
        "color_space",
        "color_primaries",
        "color_range",
        "width",
        "height",
        "field_order",
        "interlaced",
        "audio_only",
        "fps",
        "avg_frame_rate",
        "r_frame_rate",
    ):
        if not enriched.get(key) and summary.get(key) not in {None, ""}:
            enriched[key] = summary.get(key)
    if not enriched.get("operation") and summary.get("operation"):
        enriched["operation"] = summary["operation"]
    return enriched


def running_job_response(channel_id: str, ch: dict) -> tuple[int, bytes, bytes]:
    return json_bytes({
        "ok": True,
        "channel_id": channel_id,
        "job_id": channel_id,
        "status": "running",
        "operation": ch.get("operation"),
        "hls_url": channel_url(channel_id),
        "hls": channel_url(channel_id),
    })


def startup_heartbeat_response(channel_id: str) -> tuple[int, bytes, bytes]:
    return json_bytes({
        "ok": True,
        "channel_id": channel_id,
        "hls_healthy": False,
        "reason": "starting",
        "state": "starting",
        "segment_count": 0,
        "output_age_seconds": None,
    })


def prepare_start_job(channel_id: str, spec: tuple[tuple[str, str], ...], ch: dict) -> None:
    should_stop_existing = False
    with STATE.lock:
        STATE.cleanup_dead()
        STATE.start_cancelled.discard(channel_id)
        if STATE.is_running(channel_id):
            current_spec = getattr(STATE, "job_specs", {}).get(channel_id)
            if current_spec == spec:
                STATE.heartbeat(channel_id)
                raise RuntimeError("already_running_same_spec")
            should_stop_existing = True
        if channel_id in STATE.starting:
            raise HTTPError(409, "Channel is already starting")
        running_count = sum(1 for proc in STATE.processes.values() if proc.poll() is None)
        effective_running = running_count - (1 if should_stop_existing else 0)
        if effective_running + len(STATE.starting) >= CONFIG.max_transcodes:
            raise HTTPError(429, "Transcode limit reached")

    if should_stop_existing:
        STATE.stop(channel_id)

    with STATE.lock:
        if channel_id in STATE.starting:
            raise HTTPError(409, "Channel is already starting")
        STATE.starting.add(channel_id)


def spawn_job_process(channel_id: str, ch: dict, reason: str) -> subprocess.Popen:
    out_dir = CONFIG.hls_root / channel_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*"):
        if old.is_file():
            try:
                old.unlink()
            except OSError:
                pass
    CONFIG.log_root.mkdir(parents=True, exist_ok=True)
    log_file = CONFIG.log_root / f"{channel_id}.log"
    rotate_file(log_file)
    cmd = build_ffmpeg_command(CONFIG, channel_id, ch)
    with log_file.open("ab", buffering=0) as log:
        log.write(("\n--- start %s reason=%s ---\n" % (time.strftime("%F %T"), reason)).encode())
        safe_cmd = [part if part != CONFIG.api_key else "***" for part in cmd]
        log.write(("cmd: %s\n" % " ".join(safe_cmd)).encode())
        return subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)


def finalize_started_job(channel_id: str, spec: tuple[tuple[str, str], ...], ch: dict, proc: subprocess.Popen) -> tuple[int, bytes, bytes]:
    with STATE.lock:
        STATE.starting.discard(channel_id)
        if channel_id in STATE.start_cancelled:
            STATE.start_cancelled.discard(channel_id)
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            return json_bytes({
                "ok": False,
                "channel_id": channel_id,
                "job_id": channel_id,
                "status": "stopped_during_startup",
                "pid": proc.pid,
                "operation": ch.get("operation"),
                "error": "channel stopped during startup",
            }, status=409)
        if STATE.is_running(channel_id):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            STATE.heartbeat(channel_id)
            return running_job_response(channel_id, ch)
        STATE.processes[channel_id] = proc
        STATE.job_specs[channel_id] = spec
        STATE.heartbeat(channel_id)
    return None


def startup_probe_result(channel_id: str, ch: dict, proc: subprocess.Popen) -> tuple[int, bytes, bytes]:
    time.sleep(float(ch.get("startup_probe_seconds", CONFIG.startup_probe_seconds)))
    with STATE.lock:
        still_current = STATE.processes.get(channel_id) is proc
    returncode = proc.poll()
    if not still_current:
        clear_channel_runtime_caches(channel_id)
        return json_bytes({
            "ok": False,
            "channel_id": channel_id,
            "job_id": channel_id,
            "status": "stopped_during_startup",
            "pid": proc.pid,
            "operation": ch.get("operation"),
            "error": "channel stopped during startup",
        }, status=409)
    if returncode is not None:
        with STATE.lock:
            if STATE.processes.get(channel_id) is proc:
                STATE.processes.pop(channel_id, None)
                STATE.heartbeats.pop(channel_id, None)
                STATE.job_specs.pop(channel_id, None)
        clear_channel_runtime_caches(channel_id)
        return json_bytes({
            "ok": False,
            "channel_id": channel_id,
            "job_id": channel_id,
            "status": "exited_immediately",
            "pid": proc.pid,
            "returncode": returncode,
            "operation": ch.get("operation"),
            "hardware": {
                "mode": "qsv",
                "device": CONFIG.qsv_device,
                "encode": "h264_qsv",
                "hardware_only": CONFIG.hardware_only,
            },
            "hls_url": channel_url(channel_id),
            "hls": channel_url(channel_id),
            "log_tail": read_log_tail(channel_id),
        }, status=502)
    return json_bytes({
        "ok": True,
        "channel_id": channel_id,
        "job_id": channel_id,
        "status": "started",
        "pid": proc.pid,
        "operation": ch.get("operation"),
        "hardware": {
            "mode": "qsv",
            "device": CONFIG.qsv_device,
            "encode": "h264_qsv",
            "hardware_only": CONFIG.hardware_only,
        },
        "hls_url": channel_url(channel_id),
        "hls": channel_url(channel_id),
        "log_url": f"/api/logs/{channel_id}",
    })


def start_job(channel_id: str, ch: dict, reason: str = "configured"):
    spec = job_spec(ch)
    try:
        prepare_start_job(channel_id, spec, ch)
    except RuntimeError as exc:
        if str(exc) == "already_running_same_spec":
            return running_job_response(channel_id, ch)
        raise

    try:
        proc = spawn_job_process(channel_id, ch, reason)
    except OSError as exc:
        with STATE.lock:
            STATE.starting.discard(channel_id)
        return json_bytes({"ok": False, "channel_id": channel_id, "status": "start_failed", "error": str(exc)}, status=502)
    except ValueError as exc:
        with STATE.lock:
            STATE.starting.discard(channel_id)
        raise HTTPError(400, str(exc)) from exc

    running_response = finalize_started_job(channel_id, spec, ch, proc)
    if running_response is not None:
        return running_response
    return startup_probe_result(channel_id, ch, proc)

def probe_dynamic(body: dict):
    channel_id = str(body.get("channel_id") or body.get("job_id") or "probe").strip()
    input_url = str(body.get("input_url") or body.get("url") or "").strip()
    if not safe_channel_id(channel_id):
        raise HTTPError(400, "Unsafe channel id")
    if not input_url.startswith(("http://", "https://")):
        raise HTTPError(400, "input_url must be http(s)")
    ensure_input_allowed(input_url)
    return probe_url(channel_id, input_url)


def probe_summary_score(summary: dict) -> int:
    score = 0
    if summary.get("video_codec"):
        score += 2
    if summary.get("video_profile"):
        score += 1
    if summary.get("pix_fmt"):
        score += 1
    if int(summary.get("width") or 0) > 0 and int(summary.get("height") or 0) > 0:
        score += 2
    if summary.get("audio_codec"):
        score += 1
    return score


def probe_summary_is_complete(summary: dict) -> bool:
    if bool(summary.get("audio_only")) and summary.get("audio_codec"):
        return True
    return bool(
        summary.get("video_codec")
        and summary.get("video_profile")
        and summary.get("pix_fmt")
        and int(summary.get("width") or 0) > 0
        and int(summary.get("height") or 0) > 0
    )


def probe_summary_is_live_tv_interlace_ambiguous(summary: dict) -> bool:
    video_codec = str(summary.get("video_codec") or "").strip().lower()
    audio_codec = str(summary.get("audio_codec") or "").strip().lower()
    field_order = str(summary.get("field_order") or "").strip().lower()
    interlaced = bool(summary.get("interlaced"))
    return (
        probe_summary_is_complete(summary)
        and video_codec in {"h264", "avc1"}
        and audio_codec == "mp2"
        and not interlaced
        and field_order in {"", "unknown"}
    )


def probe_summary_should_replace_best(best_score: int, best_summary: dict, score: int, summary: dict) -> bool:
    if score > best_score:
        return True
    if score < best_score:
        return False
    if summary.get("interlaced") and not best_summary.get("interlaced"):
        return True
    return False


def probe_url(channel_id: str, input_url: str):
    try:
        res, parsed, summary = probe_stream_summary(input_url)
    except HTTPError as exc:
        message = str(exc.message)
        code = "ffprobe_failed"
        if "timed out" in message:
            code = "ffprobe_timed_out"
        elif "start failed" in message:
            code = "ffprobe_start_failed"
        elif "invalid ffprobe json" in message or "JSON root" in message:
            code = "invalid_ffprobe_json"
        return json_bytes({
            "ok": False,
            "channel_id": channel_id,
            "returncode": None,
            "error": {"code": code, "message": message},
            "stderr": "",
        }, status=exc.status)
    return json_bytes({
        "ok": res.returncode == 0,
        "channel_id": channel_id,
        "returncode": res.returncode,
        "input": {k: summary[k] for k in ["video_codec", "video_profile", "pix_fmt", "audio_codec", "color_transfer", "color_space", "color_primaries", "color_range", "width", "height", "field_order", "interlaced", "audio_only", "fps", "avg_frame_rate", "r_frame_rate"]},
        "hardware_plan": summary["hardware_plan"],
        "suggested_operation": summary["operation"],
        "needs_transcode": summary["needs_transcode"],
        "direct_playable": summary["direct_playable"],
        "browser_playable": summary["browser_playable"],
        "audio_only": summary["audio_only"],
        "reason": summary["reason"],
        "stdout": parsed,
        "stderr": res.stderr[-4000:],
    })


def serve_hls(path: str):
    rel = path[len("/hls/"):]
    parts = rel.split("/")
    if len(parts) < 2 or not safe_channel_id(parts[0]) or any(p in {"", ".", ".."} for p in parts):
        raise HTTPError(400, "Unsafe path")
    file_path = (CONFIG.hls_root / Path(*parts)).resolve()
    root = CONFIG.hls_root.resolve()
    if root not in [file_path, *file_path.parents]:
        raise HTTPError(403, "Forbidden")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPError(404, "HLS file not found")
    ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    if file_path.suffix == ".m3u8":
        ctype = "application/vnd.apple.mpegurl"
    elif file_path.suffix == ".ts":
        ctype = "video/mp2t"
    return FileResponse(path=file_path, content_type=ctype)


def reaper_loop():
    while True:
        try:
            stopped = STATE.stop_idle()
            for cid in stopped:
                clear_channel_runtime_caches(cid)
                print(f"stopped idle channel {cid}", file=sys.stderr)
            cleanup = cleanup_hls_root(
                CONFIG.hls_root,
                ttl_seconds=CONFIG.hls_ttl_seconds,
                max_bytes=CONFIG.hls_max_bytes,
                active_channels=STATE.active_channel_ids(),
            )
            for cid in cleanup["expired_dirs"]:
                clear_hls_health_cache(cid)
                print(f"removed expired hls channel {cid}", file=sys.stderr)
            clear_hls_caches_for_removed_paths(cleanup["quota_files"])
        except Exception:
            traceback.print_exc()
        time.sleep(1)


def make_shutdown_handler(httpds):
    shutting_down = False

    def _shutdown_worker():
        try:
            STATE.stop_all()
        except Exception:
            traceback.print_exc()
        for httpd in httpds:
            try:
                httpd.shutdown()
            except Exception:
                traceback.print_exc()

    def _handler(signum, frame):
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        # HTTPServer.shutdown() must run from a different thread than
        # serve_forever(); signal handlers run on Python's main thread, which is
        # also where api_httpd.serve_forever() is running in main(). Calling it
        # inline can deadlock DSM package stop until start-stop-status kills us.
        threading.Thread(target=_shutdown_worker, daemon=True).start()

    return _handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="reserved for future use", default=None)
    parser.parse_args(argv)
    global CONFIG, STATE
    CONFIG = Config.from_env()
    STATE = TranscoderState(CONFIG.idle_timeout, stop_timeout=CONFIG.ffmpeg_stop_timeout)
    CONFIG.hls_root.mkdir(parents=True, exist_ok=True)
    CONFIG.log_root.mkdir(parents=True, exist_ok=True)
    print(f"IPTV Transcoder API/HLS listening on {CONFIG.host}:{CONFIG.port}", file=sys.stderr)
    print(f"IPTV Transcoder management listening on {CONFIG.host}:{CONFIG.management_port}", file=sys.stderr)
    print(f"channels={CONFIG.channels_file} hls={CONFIG.hls_root} qsv_device={CONFIG.qsv_device}", file=sys.stderr)
    threading.Thread(target=reaper_loop, daemon=True).start()
    api_httpd = ThreadingHTTPServer((CONFIG.host, CONFIG.port), Handler)
    management_httpd = ThreadingHTTPServer((CONFIG.host, CONFIG.management_port), ManagementHandler)
    shutdown_handler = make_shutdown_handler([api_httpd, management_httpd])
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    management_thread = threading.Thread(target=management_httpd.serve_forever, daemon=True)
    management_thread.start()
    try:
        api_httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STATE.stop_all()
        management_httpd.shutdown()
        management_httpd.server_close()
        api_httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
