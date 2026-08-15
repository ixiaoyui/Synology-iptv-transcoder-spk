"""Core helpers for IPTV Transcoder Synology package."""
from __future__ import annotations

import copy
import json
import os
import re
import signal
import subprocess
import shutil
import threading
import time
from dataclasses import dataclass, InitVar, field
from pathlib import Path
from typing import Any

CHANNEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ALLOWED_OPERATIONS = {
    "qsv_h264",
    "qsv_deinterlace",
    "qsv_hevc_to_h264",
    "qsv_deinterlace_hevc_to_h264",
    "qsv_mpeg2_to_h264",
    "qsv_mpeg2_deinterlace_to_h264",
    "qsv_hevc_to_h264_1080p",
}
BROWSER_FRIENDLY_VIDEO_CODECS = {"h264", "avc1"}
BROWSER_FRIENDLY_AUDIO_CODECS = {"aac", "mp3"}
HDR_TRANSFER_VALUES = {"arib-std-b67", "smpte2084", "hlg", "pq"}
RESOLUTION_SIZES = {
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "2k": (2560, 1440),
    "4k": (3840, 2160),
}
LOW_POWER_QUALITY_PRESETS = ("low", "medium", "high")
LOW_POWER_RATE_FIELDS = ("bitrate", "maxrate", "bufsize")
DEFAULT_LOW_POWER_BITRATE_LADDER = {
    "720p": {
        "low": {"bitrate": "2500k", "maxrate": "3000k", "bufsize": "6000k"},
        "medium": {"bitrate": "3500k", "maxrate": "4000k", "bufsize": "8000k"},
        "high": {"bitrate": "5000k", "maxrate": "6000k", "bufsize": "12000k"},
    },
    "1080p": {
        "low": {"bitrate": "4500k", "maxrate": "5000k", "bufsize": "10000k"},
        "medium": {"bitrate": "6000k", "maxrate": "7000k", "bufsize": "14000k"},
        "high": {"bitrate": "8000k", "maxrate": "9000k", "bufsize": "18000k"},
    },
    "2k": {
        "low": {"bitrate": "7000k", "maxrate": "8000k", "bufsize": "16000k"},
        "medium": {"bitrate": "9000k", "maxrate": "10000k", "bufsize": "20000k"},
        "high": {"bitrate": "12000k", "maxrate": "14000k", "bufsize": "28000k"},
    },
    "4k": {
        "low": {"bitrate": "14000k", "maxrate": "16000k", "bufsize": "32000k"},
        "medium": {"bitrate": "18000k", "maxrate": "20000k", "bufsize": "40000k"},
        "high": {"bitrate": "25000k", "maxrate": "28000k", "bufsize": "56000k"},
    },
}
VIDEO_DECODER_BY_CODEC = {
    "h264": "h264_qsv",
    "hevc": "hevc_qsv",
    "h265": "hevc_qsv",
    "mpeg2video": "mpeg2_qsv",
    "mpeg2": "mpeg2_qsv",
}
CHANNELS_CACHE: dict[str, tuple[str, dict[str, dict[str, Any]]]] = {}
CHANNELS_CACHE_LOCK = threading.Lock()


def safe_channel_id(channel_id: str) -> bool:
    return bool(CHANNEL_ID_RE.fullmatch(channel_id or ""))


def operation_is_allowed(operation: str) -> bool:
    return operation in ALLOWED_OPERATIONS


@dataclass
class Config:
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    qsv_device: str = "/dev/dri/renderD128"
    vaapi_device: InitVar[str | None] = None
    hls_root: Path = Path("/var/packages/iptv-transcoder/var/hls")
    log_root: Path = Path("/var/packages/iptv-transcoder/var/logs")
    channels_file: Path = Path("/var/packages/iptv-transcoder/var/channels.json")
    public_base_url: str = "http://127.0.0.1:18096"
    api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 18096
    management_port: int = 18097
    max_transcodes: int = 5
    idle_timeout: int = 10
    hls_time: float = 2.0
    hls_list_size: int = 6
    hls_gop: int = 100
    global_quality: int = 23
    global_quality_4k: int = 24
    qsv_low_power_h264: bool = True
    qsv_low_power_ladder: dict[str, dict[str, dict[str, str]]] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_LOW_POWER_BITRATE_LADDER)
    )
    audio_bitrate: str = "128k"
    hardware_only: bool = True
    allowed_upstreams: str = ""
    hls_ttl_seconds: int = 3600
    hls_max_bytes: int = 2 * 1024 * 1024 * 1024
    startup_probe_seconds: float = 0.2
    ffmpeg_stop_timeout: float = 1.5
    ffprobe_timeout: float = 20.0
    ffprobe_analyzeduration: int = 10_000_000
    ffprobe_probesize: int = 20_000_000
    hdr_vpp_brightness: float = 8.0
    hdr_vpp_contrast: float = 1.0

    def __post_init__(self, vaapi_device: str | None) -> None:
        if vaapi_device and self.qsv_device == "/dev/dri/renderD128":
            self.qsv_device = vaapi_device

    @classmethod
    def from_env(cls) -> "Config":
        def p(name: str, default: str) -> Path:
            return Path(os.environ.get(name, default))

        def i(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name, str(default)))
            except ValueError:
                return default

        def b(name: str, default: str) -> bool:
            return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}

        def flt(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, str(default)))
            except ValueError:
                return default

        def bitrate(name: str, default: str) -> str:
            value = str(os.environ.get(name, default)).strip()
            if not value:
                return default
            return value.lower()

        ladder = copy.deepcopy(DEFAULT_LOW_POWER_BITRATE_LADDER)
        for resolution in RESOLUTION_SIZES:
            env_resolution = resolution.upper()
            for preset in LOW_POWER_QUALITY_PRESETS:
                env_preset = preset.upper()
                for rate_field in LOW_POWER_RATE_FIELDS:
                    env_name = f"IPTV_TRANSCODER_QSV_LOW_POWER_{env_resolution}_{env_preset}_{rate_field.upper()}"
                    ladder[resolution][preset][rate_field] = bitrate(
                        env_name,
                        ladder[resolution][preset][rate_field],
                    )

        port = i("IPTV_TRANSCODER_PORT", 18096)
        return cls(
            ffmpeg_bin=os.environ.get("IPTV_TRANSCODER_FFMPEG", "/var/packages/Jellyfin/target/bin/ffmpeg"),
            ffprobe_bin=os.environ.get("IPTV_TRANSCODER_FFPROBE", "/var/packages/Jellyfin/target/bin/ffprobe"),
            qsv_device=os.environ.get(
                "IPTV_TRANSCODER_QSV_DEVICE",
                os.environ.get("IPTV_TRANSCODER_VAAPI_DEVICE", "/dev/dri/renderD128"),
            ),
            hls_root=p("IPTV_TRANSCODER_HLS_ROOT", "/var/packages/iptv-transcoder/var/hls"),
            log_root=p("IPTV_TRANSCODER_LOG_ROOT", "/var/packages/iptv-transcoder/var/logs"),
            channels_file=p("IPTV_TRANSCODER_CHANNELS", "/var/packages/iptv-transcoder/var/channels.json"),
            public_base_url=os.environ.get("IPTV_TRANSCODER_PUBLIC_BASE_URL", f"http://127.0.0.1:{port}").rstrip("/"),
            api_key=os.environ.get("IPTV_TRANSCODER_API_KEY", ""),
            host=os.environ.get("IPTV_TRANSCODER_HOST", "0.0.0.0"),
            port=port,
            management_port=i("IPTV_TRANSCODER_MANAGEMENT_PORT", 18097),
            max_transcodes=i("IPTV_TRANSCODER_MAX_TRANSCODES", 5),
            idle_timeout=i("IPTV_TRANSCODER_IDLE_TIMEOUT", 10),
            hls_time=max(0.5, min(10.0, flt("IPTV_TRANSCODER_HLS_TIME", 2.0))),
            hls_list_size=i("IPTV_TRANSCODER_HLS_LIST_SIZE", 6),
            hls_gop=max(10, min(300, i("IPTV_TRANSCODER_HLS_GOP", 100))),
            global_quality=i("IPTV_TRANSCODER_GLOBAL_QUALITY", 23),
            global_quality_4k=i("IPTV_TRANSCODER_GLOBAL_QUALITY_4K", 24),
            qsv_low_power_h264=b("IPTV_TRANSCODER_QSV_LOW_POWER_H264", "1"),
            qsv_low_power_ladder=ladder,
            audio_bitrate=os.environ.get("IPTV_TRANSCODER_AUDIO_BITRATE", "128k"),
            hardware_only=b("IPTV_TRANSCODER_HARDWARE_ONLY", "1"),
            allowed_upstreams=os.environ.get("IPTV_TRANSCODER_ALLOWED_UPSTREAMS", ""),
            hls_ttl_seconds=i("IPTV_TRANSCODER_HLS_TTL_SECONDS", 3600),
            hls_max_bytes=i("IPTV_TRANSCODER_HLS_MAX_BYTES", 2 * 1024 * 1024 * 1024),
            startup_probe_seconds=max(0.0, flt("IPTV_TRANSCODER_STARTUP_PROBE_SECONDS", 0.2)),
            ffmpeg_stop_timeout=max(0.1, flt("IPTV_TRANSCODER_FFMPEG_STOP_TIMEOUT", 1.5)),
            ffprobe_timeout=max(1.0, flt("IPTV_TRANSCODER_FFPROBE_TIMEOUT", 20.0)),
            ffprobe_analyzeduration=max(100_000, i("IPTV_TRANSCODER_FFPROBE_ANALYZEDURATION", 10_000_000)),
            ffprobe_probesize=max(100_000, i("IPTV_TRANSCODER_FFPROBE_PROBESIZE", 20_000_000)),
            hdr_vpp_brightness=max(-100.0, min(100.0, flt("IPTV_TRANSCODER_HDR_VPP_BRIGHTNESS", 8.0))),
            hdr_vpp_contrast=max(0.0, min(10.0, flt("IPTV_TRANSCODER_HDR_VPP_CONTRAST", 1.0))),
        )


def cleanup_hls_root(
    hls_root: Path,
    ttl_seconds: int = 3600,
    max_bytes: int = 2 * 1024 * 1024 * 1024,
    active_channels: set[str] | None = None,
    now: float | None = None,
) -> dict[str, list[str]]:
    """Bound disk usage for HLS output without touching active channels.

    Expired inactive channel directories are removed first. If the root still
    exceeds ``max_bytes``, oldest inactive files are deleted until the quota is
    satisfied. This is intentionally conservative: active channels are skipped.
    """
    now = time.time() if now is None else now
    active_channels = active_channels or set()
    removed: dict[str, list[str]] = {"expired_dirs": [], "quota_files": []}
    if ttl_seconds < 1 or max_bytes < 1 or not hls_root.exists():
        return removed

    for child in list(hls_root.iterdir()):
        if not child.is_dir() or child.name in active_channels or not safe_channel_id(child.name):
            continue
        try:
            newest = max((p.stat().st_mtime for p in child.rglob("*") if p.exists()), default=child.stat().st_mtime)
        except OSError:
            continue
        if now - newest > ttl_seconds:
            try:
                shutil.rmtree(child)
                removed["expired_dirs"].append(child.name)
            except OSError:
                pass

    files: list[tuple[float, int, Path]] = []
    total = 0
    for p in hls_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            stat = p.stat()
        except OSError:
            continue
        total += stat.st_size
        try:
            rel = p.relative_to(hls_root)
            channel = rel.parts[0]
        except Exception:
            channel = ""
        if channel not in active_channels:
            files.append((stat.st_mtime, stat.st_size, p))

    for _, size, p in sorted(files):
        if total <= max_bytes:
            break
        try:
            p.unlink()
            total -= size
            removed["quota_files"].append(str(p))
        except OSError:
            pass

    return removed

def normalize_operation(mode: str) -> str:
    legacy = {
        "copy": "qsv_h264",
        "transcode": "qsv_h264",
        "deinterlace": "qsv_deinterlace",
        "deinterlace_transcode": "qsv_deinterlace",
        "qsv_transcode": "qsv_h264",
    }
    return legacy.get(mode, mode)


def normalize_resolution(value: str | None) -> str:
    resolution = str(value or "auto").strip().lower()
    if not resolution:
        resolution = "auto"
    if resolution == "auto" or resolution in RESOLUTION_SIZES:
        return resolution
    raise ValueError(f"unsupported resolution: {resolution}")


def load_channels(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw_text = path.read_text(encoding="utf-8")
    cache_key = str(path.resolve(strict=False)) if hasattr(path, "resolve") else str(path)
    with CHANNELS_CACHE_LOCK:
        cached = CHANNELS_CACHE.get(cache_key)
        if cached and cached[0] == raw_text:
            return copy.deepcopy(cached[1])
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("channels.json must be an object keyed by channel id")
    for channel_id, ch in data.items():
        if not safe_channel_id(channel_id):
            raise ValueError(f"unsafe channel id: {channel_id!r}")
        if not isinstance(ch, dict) or not ch.get("url"):
            raise ValueError(f"channel {channel_id!r} must contain url")
        operation = normalize_operation(str(ch.get("operation") or ch.get("mode") or "qsv_deinterlace"))
        if operation not in ALLOWED_OPERATIONS:
            raise ValueError(f"channel {channel_id!r} has invalid operation {operation!r}")
        ch["operation"] = operation
    with CHANNELS_CACHE_LOCK:
        CHANNELS_CACHE[cache_key] = (raw_text, copy.deepcopy(data))
    return copy.deepcopy(data)


def decoder_for(operation: str, video_codec: str | None = None) -> str:
    codec = (video_codec or "").strip().lower()
    if operation in {"qsv_hevc_to_h264", "qsv_deinterlace_hevc_to_h264", "qsv_hevc_to_h264_1080p"}:
        return "hevc_qsv"
    if operation in {"qsv_mpeg2_to_h264", "qsv_mpeg2_deinterlace_to_h264"}:
        return "mpeg2_qsv"
    return VIDEO_DECODER_BY_CODEC.get(codec, "h264_qsv")


def is_10bit_video(profile: str | None = None, pix_fmt: str | None = None) -> bool:
    profile_text = (profile or "").strip().lower()
    pix_fmt_text = (pix_fmt or "").strip().lower()
    return "10" in profile_text or "10" in pix_fmt_text or pix_fmt_text.startswith("p010")


def needs_nv12_for_hevc_h264(profile: str | None = None, pix_fmt: str | None = None) -> bool:
    profile_text = (profile or "").strip().lower()
    pix_fmt_text = (pix_fmt or "").strip().lower()
    # If either field is missing, choose the safe QSV surface format. Some live
    # MPEG-TS streams only expose index/rate fields with restricted ffprobe
    # output; without NV12 conversion Main10 streams can stay running forever
    # without producing HLS.
    if not profile_text or not pix_fmt_text:
        return True
    return is_10bit_video(profile_text, pix_fmt_text)


def parse_frame_rate(value: Any) -> float | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if "/" in text:
        left, _, right = text.partition("/")
        try:
            numerator = float(left)
            denominator = float(right)
        except ValueError:
            return None
        if denominator == 0:
            return None
        rate = numerator / denominator
    else:
        try:
            rate = float(text)
        except ValueError:
            return None
    if rate <= 0:
        return None
    return rate


def source_frame_rate(ch: dict[str, Any]) -> float | None:
    for key in ("fps", "avg_frame_rate", "frame_rate", "r_frame_rate"):
        rate = parse_frame_rate(ch.get(key))
        if rate is not None:
            return rate
    return None


def resolve_hls_timing(config: Config, ch: dict[str, Any]) -> tuple[float, int, int]:
    explicit_hls_time = ch.get("hls_time") not in {None, ""}
    explicit_gop = ch.get("gop") not in {None, ""}
    explicit_keyint_min = ch.get("keyint_min") not in {None, ""}
    if explicit_hls_time or explicit_gop or explicit_keyint_min:
        hls_time = float(ch.get("hls_time") or config.hls_time)
        gop = int(ch.get("gop") or config.hls_gop)
        keyint_min = int(ch.get("keyint_min") or gop)
        return hls_time, gop, keyint_min

    fps = source_frame_rate(ch)
    if fps is None:
        return float(config.hls_time), int(config.hls_gop), int(config.hls_gop)

    target_hls_time = max(0.5, float(config.hls_time))
    raw_gop = max(10, int(round(fps * target_hls_time)))
    gop = max(10, min(int(config.hls_gop), raw_gop))
    hls_time = gop / fps
    return hls_time, gop, gop


def is_hdr_video(
    color_transfer: str | None = None,
    color_primaries: str | None = None,
    color_space: str | None = None,
) -> bool:
    transfer = (color_transfer or "").strip().lower()
    return transfer in HDR_TRANSFER_VALUES


def probe_is_audio_only(video_codec: str | None = None, audio_codec: str | None = None) -> bool:
    return not str(video_codec or "").strip() and bool(str(audio_codec or "").strip())


def browser_friendly_media(
    video_codec: str | None = None,
    audio_codec: str | None = None,
    interlaced: bool = False,
    audio_only: bool = False,
) -> bool:
    normalized_video = str(video_codec or "").strip().lower()
    normalized_audio = str(audio_codec or "").strip().lower()
    if audio_only:
        return normalized_audio in BROWSER_FRIENDLY_AUDIO_CODECS
    return (
        normalized_video in BROWSER_FRIENDLY_VIDEO_CODECS
        and (not normalized_audio or normalized_audio in BROWSER_FRIENDLY_AUDIO_CODECS)
        and not interlaced
    )


def probe_transcode_decision(
    video_codec: str | None = None,
    audio_codec: str | None = None,
    interlaced: bool = False,
    audio_only: bool = False,
) -> tuple[bool, str]:
    normalized_video = str(video_codec or "").strip().lower()
    normalized_audio = str(audio_codec or "").strip().lower()
    if audio_only:
        return False, "audio_only_broadcast"
    if interlaced:
        return True, "interlaced_video"
    if normalized_video in {"hevc", "h265"}:
        return True, "video_codec_hevc"
    if normalized_video in {"mpeg2video", "mpeg2"}:
        return True, "video_codec_mpeg2"
    if normalized_video and normalized_video not in BROWSER_FRIENDLY_VIDEO_CODECS:
        return True, "video_codec_unsupported"
    if normalized_audio and normalized_audio not in BROWSER_FRIENDLY_AUDIO_CODECS:
        return True, "audio_codec_unsupported"
    if browser_friendly_media(normalized_video, normalized_audio, interlaced, audio_only):
        return False, "browser_friendly"
    return False, "unknown_media_shape"


def qsv_filter_params(
    operation: str,
    resolution: str | None = None,
    video_profile: str | None = None,
    pix_fmt: str | None = None,
    source_width: int = 0,
    source_height: int = 0,
    *,
    include_nv12: bool = True,
) -> list[str]:
    params: list[str] = []
    if operation in {"qsv_deinterlace", "qsv_deinterlace_hevc_to_h264", "qsv_mpeg2_deinterlace_to_h264"}:
        params.append("deinterlace=2")
    target_size = qsv_output_size(operation, resolution, source_width, source_height)
    if target_size is not None:
        width, height = target_size
        params.extend([f"w={width}", f"h={height}"])
    if include_nv12 and operation in {"qsv_hevc_to_h264", "qsv_deinterlace_hevc_to_h264", "qsv_hevc_to_h264_1080p"} and needs_nv12_for_hevc_h264(video_profile, pix_fmt):
        params.append("format=nv12")
    return params


def qsv_output_size(
    operation: str,
    resolution: str | None = None,
    source_width: int = 0,
    source_height: int = 0,
) -> tuple[int, int] | None:
    normalized_resolution = normalize_resolution(resolution)
    target_size = RESOLUTION_SIZES.get(normalized_resolution)
    if target_size is None and operation == "qsv_hevc_to_h264_1080p":
        return RESOLUTION_SIZES["1080p"]
    if target_size is not None:
        return target_size
    if normalized_resolution == "auto" and source_width >= 3840 and source_height >= 2160 and operation != "qsv_hevc_to_h264_1080p":
        return RESOLUTION_SIZES["4k"]
    return None


def hdr_setparams(
    color_transfer: str | None = None,
    color_primaries: str | None = None,
    color_space: str | None = None,
) -> str:
    params: list[str] = []
    if color_primaries:
        params.append(f"color_primaries={str(color_primaries).strip().lower()}")
    if color_transfer:
        params.append(f"color_trc={str(color_transfer).strip().lower()}")
    if color_space:
        params.append(f"colorspace={str(color_space).strip().lower()}")
    return "setparams=" + ":".join(params) if params else ""


def qsv_vpp_filter(operation: str, resolution: str | None, width: int, height: int) -> str:
    params = qsv_filter_params(operation, resolution, None, None, width, height, include_nv12=False)
    return "vpp_qsv" + ("=" + ":".join(params) if params else "")


def qsv_vpp_hdr_filter(
    operation: str,
    resolution: str | None,
    width: int,
    height: int,
    *,
    brightness: float = 8.0,
    contrast: float = 1.0,
) -> str:
    params = qsv_filter_params(operation, resolution, None, None, width, height, include_nv12=False)
    params.extend([
        "format=nv12",
        "out_color_matrix=bt709",
        "out_color_primaries=bt709",
        "out_color_transfer=bt709",
        "tonemap=1",
        "procamp=1",
        f"brightness={ffmpeg_number(brightness)}",
        f"contrast={ffmpeg_number(contrast)}",
    ])
    return "vpp_qsv=" + ":".join(params)


def filter_for(
    operation: str,
    resolution: str | None = None,
    video_profile: str | None = None,
    pix_fmt: str | None = None,
    width: int = 0,
    height: int = 0,
    color_transfer: str | None = None,
    color_primaries: str | None = None,
    color_space: str | None = None,
    color_range: str | None = None,
    hdr_vpp_brightness: float = 8.0,
    hdr_vpp_contrast: float = 1.0,
) -> str:
    hdr = operation in {"qsv_hevc_to_h264", "qsv_deinterlace_hevc_to_h264", "qsv_hevc_to_h264_1080p"} and is_hdr_video(
        color_transfer=color_transfer,
        color_primaries=color_primaries,
        color_space=color_space,
    )
    if not hdr:
        params = qsv_filter_params(operation, resolution, video_profile, pix_fmt, width, height, include_nv12=True)
        return "vpp_qsv" + ("=" + ":".join(params) if params else "")

    if not is_10bit_video(video_profile, pix_fmt):
        # 8-bit HLG/BT.2020 channels cannot use tonemap_opencl directly on this
        # platform; keep them on the proven QSV VPP tone-map path.
        return qsv_vpp_hdr_filter(
            operation,
            resolution,
            width,
            height,
            brightness=hdr_vpp_brightness,
            contrast=hdr_vpp_contrast,
        )

    # Match Jellyfin's HDR path closely:
    #   QSV decode -> QSV VPP scale/deinterlace -> map to OpenCL ->
    #   tonemap_opencl=bt2390 -> map back to QSV -> H.264 QSV encode.
    filters: list[str] = []
    setparams_filter = hdr_setparams(color_transfer=color_transfer, color_primaries=color_primaries, color_space=color_space)
    if setparams_filter:
        filters.append(setparams_filter)
    filters.append(qsv_vpp_filter(operation, resolution, width, height))
    filters.extend([
        "hwmap=derive_device=opencl:mode=read",
        "tonemap_opencl="
        "format=nv12:p=bt709:t=bt709:m=bt709:tonemap=bt2390:peak=100:desat=0",
        "hwmap=derive_device=qsv:mode=write:reverse=1:extra_hw_frames=16",
        "format=qsv",
    ])
    return ",".join(filters)


def choose_audio_args(audio_codec: str | None, config: Config, force_aac: bool = False) -> list[str]:
    if not force_aac and (audio_codec or "").strip().lower() == "aac":
        return ["-c:a", "copy"]
    # Live E-AC-3 / multi-channel broadcast audio can produce AAC HLS segments
    # whose later TS chunks probe as sample_rate=0/channels=0 in browsers. Clamp
    # transcoded AAC to a conservative browser-friendly shape.
    return ["-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", config.audio_bitrate]


def ffmpeg_number(value: float | int | str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return ("%.3f" % number).rstrip("0").rstrip(".")


def low_power_quality_preset(ch: dict[str, Any]) -> str:
    preset = str(ch.get("quality_preset") or "").strip().lower()
    if preset in LOW_POWER_QUALITY_PRESETS:
        return preset
    if preset == "default":
        return "medium"
    try:
        quality = int(str(ch.get("global_quality") or ""))
    except (TypeError, ValueError):
        return "medium"
    if quality <= 20:
        return "high"
    if quality >= 25:
        return "low"
    return "medium"


def low_power_resolution_bucket(
    output_size: tuple[int, int] | None,
    source_width: int = 0,
    source_height: int = 0,
) -> str:
    effective_size = output_size
    if effective_size is None and source_width > 0 and source_height > 0:
        effective_size = (source_width, source_height)
    if effective_size == RESOLUTION_SIZES["4k"]:
        return "4k"
    if effective_size == RESOLUTION_SIZES["2k"]:
        return "2k"
    if effective_size == RESOLUTION_SIZES["1080p"]:
        return "1080p"
    return "720p"


def build_ffmpeg_command(config: Config, channel_id: str, ch: dict[str, Any]) -> list[str]:
    if not safe_channel_id(channel_id):
        raise ValueError("unsafe channel id")
    operation = normalize_operation(str(ch.get("operation") or ch.get("mode") or "qsv_deinterlace"))
    if operation not in ALLOWED_OPERATIONS:
        raise ValueError(f"unsupported operation: {operation}")
    if bool(ch.get("audio_only")):
        raise ValueError("audio-only source cannot be video transcoded")
    out_dir = config.hls_root / channel_id
    out_path = out_dir / "master.m3u8"
    decoder = str(ch.get("decoder") or decoder_for(operation, ch.get("video_codec")))
    if not decoder.endswith("_qsv"):
        raise ValueError("hardware-only mode requires a QSV decoder")
    source_width = int(ch.get("width") or 0)
    source_height = int(ch.get("height") or 0)
    output_size = qsv_output_size(operation, ch.get("resolution"), source_width, source_height)
    effective_4k = output_size == RESOLUTION_SIZES["4k"]
    quality = int(ch.get("global_quality") or (config.global_quality_4k if effective_4k else config.global_quality))
    use_low_power_h264 = bool(config.qsv_low_power_h264)
    low_power_rate_args: list[str] = []
    if use_low_power_h264:
        low_power_resolution = low_power_resolution_bucket(output_size, source_width, source_height)
        low_power_preset = low_power_quality_preset(ch)
        low_power_profile = config.qsv_low_power_ladder[low_power_resolution][low_power_preset]
        low_power_rate_args = [
            "-b:v", low_power_profile["bitrate"],
            "-maxrate", low_power_profile["maxrate"],
            "-bufsize", low_power_profile["bufsize"],
        ]
    hdr_tonemap = operation in {"qsv_hevc_to_h264", "qsv_deinterlace_hevc_to_h264", "qsv_hevc_to_h264_1080p"} and is_hdr_video(
        color_transfer=str(ch.get("color_transfer") or ""),
        color_primaries=str(ch.get("color_primaries") or ""),
        color_space=str(ch.get("color_space") or ""),
    )
    hdr_opencl = hdr_tonemap and is_10bit_video(ch.get("video_profile"), ch.get("pix_fmt"))
    audio_args = choose_audio_args(ch.get("audio_codec"), config, bool(ch.get("force_aac", False)) or hdr_tonemap)
    hls_time, gop_i, keyint_min_i = resolve_hls_timing(config, ch)
    hls_time_s = ffmpeg_number(hls_time)
    gop = str(gop_i)
    keyint_min = str(keyint_min_i)

    cmd = [
        config.ffmpeg_bin,
        "-hide_banner",
        "-loglevel", str(ch.get("loglevel", "warning")),
        "-nostdin",
        *(
            [
                "-init_hw_device", f"vaapi=va:{config.qsv_device},driver=iHD",
                "-init_hw_device", "qsv=qs@va",
                "-init_hw_device", "opencl=ocl@va",
                "-filter_hw_device", "qs",
            ] if hdr_opencl else []
        ),
        "-hwaccel", "qsv",
        "-hwaccel_output_format", "qsv",
        *(["-qsv_device", config.qsv_device] if not hdr_opencl else []),
        "-c:v", decoder,
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", ch["url"],
        "-vf", filter_for(
            operation,
            ch.get("resolution"),
            ch.get("video_profile"),
            ch.get("pix_fmt"),
            source_width,
            source_height,
            ch.get("color_transfer"),
            ch.get("color_primaries"),
            ch.get("color_space"),
            ch.get("color_range"),
            float(ch.get("hdr_vpp_brightness") or config.hdr_vpp_brightness),
            float(ch.get("hdr_vpp_contrast") or config.hdr_vpp_contrast),
        ),
        "-c:v", "h264_qsv",
        *(["-low_power", "1"] if use_low_power_h264 else []),
        *(low_power_rate_args if use_low_power_h264 else ["-global_quality", str(quality)]),
        "-preset", "veryfast" if (use_low_power_h264 or effective_4k) else "medium",
        "-async_depth", "8" if effective_4k else "4",
        "-look_ahead", "0",
        *(["-bf", "0"] if hdr_tonemap else []),
        "-g", gop,
        "-keyint_min", keyint_min,
        *audio_args,
        "-f", "hls",
        "-hls_time", hls_time_s,
        "-hls_list_size", str(config.hls_list_size),
        "-hls_flags", "delete_segments+append_list+omit_endlist",
        "-hls_segment_filename", str(out_dir / "seg_%05d.ts"),
        str(out_path),
    ]
    if hdr_tonemap:
        cmd[cmd.index("-g"):cmd.index("-g")] = [
            "-color_range", "tv",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
        ]
    return cmd


def summarize_probe(ffprobe_json: dict[str, Any]) -> dict[str, Any]:
    streams = ffprobe_json.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    video_codec = str(video.get("codec_name") or "").lower()
    video_profile = str(video.get("profile") or "").lower()
    pix_fmt = str(video.get("pix_fmt") or "").lower()
    audio_codec = str(audio.get("codec_name") or "").lower()
    field_order = str(video.get("field_order") or "").lower()
    color_transfer = str(video.get("color_transfer") or "").lower()
    color_space = str(video.get("color_space") or "").lower()
    color_primaries = str(video.get("color_primaries") or "").lower()
    color_range = str(video.get("color_range") or "").lower()
    r_frame_rate = str(video.get("r_frame_rate") or "").lower()
    avg_frame_rate = str(video.get("avg_frame_rate") or "").lower()
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    fps = avg_frame_rate if parse_frame_rate(avg_frame_rate) is not None else r_frame_rate
    interlaced = field_order in {"tt", "bb", "tb", "bt"} or "interlaced" in field_order
    audio_only = probe_is_audio_only(video_codec, audio_codec)
    if audio_only:
        operation = ""
    elif video_codec in {"hevc", "h265"} and interlaced:
        operation = "qsv_deinterlace_hevc_to_h264"
    elif video_codec in {"hevc", "h265"}:
        operation = "qsv_hevc_to_h264"
    elif video_codec in {"mpeg2video", "mpeg2"} and interlaced:
        operation = "qsv_mpeg2_deinterlace_to_h264"
    elif video_codec in {"mpeg2video", "mpeg2"}:
        operation = "qsv_mpeg2_to_h264"
    elif interlaced:
        operation = "qsv_deinterlace"
    else:
        operation = "qsv_h264"
    needs_transcode, reason = probe_transcode_decision(
        video_codec=video_codec,
        audio_codec=audio_codec,
        interlaced=interlaced,
        audio_only=audio_only,
    )
    return {
        "video_codec": video_codec,
        "video_profile": video_profile,
        "pix_fmt": pix_fmt,
        "audio_codec": audio_codec,
        "color_transfer": color_transfer,
        "color_space": color_space,
        "color_primaries": color_primaries,
        "color_range": color_range,
        "r_frame_rate": r_frame_rate,
        "avg_frame_rate": avg_frame_rate,
        "fps": fps,
        "width": width,
        "height": height,
        "field_order": field_order or "unknown",
        "interlaced": interlaced,
        "audio_only": audio_only,
        "needs_transcode": needs_transcode,
        "direct_playable": not needs_transcode,
        "browser_playable": not needs_transcode,
        "reason": reason,
        "operation": operation,
        "hardware_plan": {
            "supported": operation in ALLOWED_OPERATIONS,
            "decode": decoder_for(operation, video_codec) if operation in ALLOWED_OPERATIONS else "",
            "filter": filter_for(
                operation,
                video_profile=video_profile,
                pix_fmt=pix_fmt,
                width=width,
                height=height,
                color_transfer=color_transfer,
                color_primaries=color_primaries,
                color_space=color_space,
                color_range=color_range,
            ) if operation in ALLOWED_OPERATIONS else "",
            "encode": "h264_qsv" if operation in ALLOWED_OPERATIONS else "",
            "operation": operation,
        },
    }


class TranscoderState:
    def __init__(self, idle_timeout: int = 10, stop_timeout: float = 1.5):
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen] = {}
        self.heartbeats: dict[str, float] = {}
        self.job_specs: dict[str, tuple[tuple[str, str], ...]] = {}
        self.starting: set[str] = set()
        self.start_cancelled: set[str] = set()
        self.idle_timeout = idle_timeout
        self.stop_timeout = max(0.1, float(stop_timeout))

    def cleanup_dead(self) -> list[str]:
        with self.lock:
            dead = [cid for cid, proc in self.processes.items() if proc.poll() is not None]
            for cid in dead:
                self._remove_channel_locked(cid)
            return dead

    def running_count(self) -> int:
        with self.lock:
            self.cleanup_dead()
            return sum(1 for proc in self.processes.values() if proc.poll() is None)

    def is_running(self, channel_id: str) -> bool:
        with self.lock:
            proc = self.processes.get(channel_id)
            return bool(proc and proc.poll() is None)

    def is_starting(self, channel_id: str) -> bool:
        with self.lock:
            return channel_id in self.starting

    def heartbeat(self, channel_id: str, now: float | None = None) -> bool:
        with self.lock:
            if self.is_running(channel_id):
                self.heartbeats[channel_id] = time.time() if now is None else now
                return True
            return False

    def stop(self, channel_id: str) -> bool:
        with self.lock:
            proc = self.processes.get(channel_id)
            if not proc:
                if channel_id in self.starting:
                    self.starting.discard(channel_id)
                    self.start_cancelled.add(channel_id)
                    return True
                return False
            self._remove_channel_locked(channel_id)
        self._terminate_process(proc)
        return True

    def stop_idle(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with self.lock:
            idle_channels = [
                cid for cid in list(self.processes)
                if now - self.heartbeats.get(cid, 0) > self.idle_timeout
            ]
        stopped: list[str] = []
        for cid in idle_channels:
            if self.stop(cid):
                stopped.append(cid)
        return stopped

    def stop_all(self) -> None:
        with self.lock:
            channel_ids = list(self.processes)
            starting_ids = list(self.starting)
        for cid in channel_ids:
            self.stop(cid)
        for cid in starting_ids:
            self.stop(cid)

    def active_channel_ids(self) -> set[str]:
        with self.lock:
            self.cleanup_dead()
            return {cid for cid, proc in self.processes.items() if proc.poll() is None}

    def _remove_channel_locked(self, channel_id: str) -> None:
        self.processes.pop(channel_id, None)
        self.heartbeats.pop(channel_id, None)
        self.job_specs.pop(channel_id, None)
        self.starting.discard(channel_id)
        self.start_cancelled.discard(channel_id)

    def _terminate_process(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=self.stop_timeout)
            return
        except Exception:
            pass
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=self.stop_timeout)
        except Exception:
            pass
