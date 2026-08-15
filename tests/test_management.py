import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from iptvtranscoder.core import Config, TranscoderState
from iptvtranscoder import server as server_module


class ManagementApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = root / "env"
        self.env.write_text(
            "IPTV_TRANSCODER_HOST=0.0.0.0\n"
            "IPTV_TRANSCODER_PORT=18096\n"
            "IPTV_TRANSCODER_MANAGEMENT_PORT=18097\n"
            "IPTV_TRANSCODER_PUBLIC_BASE_URL=http://NAS_IP:18096\n"
            "IPTV_TRANSCODER_API_KEY=old-key\n"
            "IPTV_TRANSCODER_FFMPEG=/var/packages/Jellyfin/target/bin/ffmpeg\n"
            "IPTV_TRANSCODER_FFPROBE=/var/packages/Jellyfin/target/bin/ffprobe\n"
            "IPTV_TRANSCODER_QSV_DEVICE=/dev/dri/renderD128\n"
            "IPTV_TRANSCODER_ALLOWED_UPSTREAMS=192.168.1.1:7088\n"
            "IPTV_TRANSCODER_HARDWARE_ONLY=1\n"
            "IPTV_TRANSCODER_MAX_TRANSCODES=1\n"
            "IPTV_TRANSCODER_IDLE_TIMEOUT=90\n"
            "IPTV_TRANSCODER_GLOBAL_QUALITY=23\n"
            "IPTV_TRANSCODER_GLOBAL_QUALITY_4K=24\n"
            "IPTV_TRANSCODER_QSV_LOW_POWER_H264=1\n"
            "IPTV_TRANSCODER_AUDIO_BITRATE=128k\n",
            encoding="utf-8",
        )
        server_module.CONFIG = Config(
            api_key="old-key",
            port=18096,
            management_port=18097,
            public_base_url="http://NAS_IP:18096",
            ffmpeg_bin="/var/packages/Jellyfin/target/bin/ffmpeg",
            ffprobe_bin="/var/packages/Jellyfin/target/bin/ffprobe",
            qsv_device="/dev/dri/renderD128",
            hls_root=root / "hls",
            log_root=root / "logs",
            channels_file=root / "channels.json",
            allowed_upstreams="192.168.1.1:7088",
            max_transcodes=1,
            idle_timeout=90,
            global_quality=23,
            global_quality_4k=24,
            qsv_low_power_h264=True,
            audio_bitrate="128k",
        )
        server_module.STATE = TranscoderState()
        server_module.ENV_FILE = self.env
        server_module.ENV_CONFIG_CACHE.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def test_api_root_remains_json_health_not_management_html(self):
        status, ctype, body = server_module.route("GET", "/", None, {})
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype.decode() if isinstance(ctype, bytes) else ctype)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["name"], "IPTV Transcoder")

    def test_management_root_serves_html_on_separate_management_port(self):
        status, ctype, body = server_module.management_route("GET", "/", None, {})
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype.decode() if isinstance(ctype, bytes) else ctype)
        text = body.decode("utf-8")
        self.assertIn("IPTV Transcoder 管理", text)
        self.assertIn("18096", text)
        self.assertIn("/api/config", text)
        self.assertIn("转码服务地址", text)
        self.assertIn("H.264 QSV 低功耗", text)
        self.assertIn("英特尔低电压模式硬件编码", text)
        self.assertIn("lpResolution", text)
        self.assertIn("lpQuality", text)
        self.assertIn("renderLowPowerRateFields", text)
        self.assertIn("render(); fillHost();", text)
        self.assertNotIn("innerHTML", text)
        self.assertIn("replaceChildren()", text)

    def test_management_page_blocks_saving_before_config_is_loaded(self):
        status, ctype, body = server_module.management_route("GET", "/", None, {})
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("let configLoaded = false;", text)
        self.assertIn("setSaveEnabled(false);", text)
        self.assertIn("if(!configLoaded){out('请先成功读取配置，再保存，避免空表单覆盖配置');return;}", text)
        self.assertIn("setSaveEnabled(true);", text)

    def test_management_page_shows_realtime_task_list_from_status_endpoint(self):
        status, ctype, body = server_module.management_route("GET", "/", None, {})
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("实时任务", text)
        self.assertIn("当前没有实时转码任务", text)
        self.assertIn("taskList", text)
        self.assertIn("loadTasks()", text)
        self.assertIn("fetch(base()+'/api/status'", text)
        self.assertIn("renderTasks", text)
        self.assertNotIn("innerHTML", text)

    def test_status_endpoint_returns_task_metadata_for_management_list(self):
        fake_proc = mock.Mock()
        fake_proc.pid = 4321
        fake_proc.poll.return_value = None
        server_module.STATE.processes["cctv1"] = fake_proc
        server_module.STATE.heartbeats["cctv1"] = 123.0
        with mock.patch.object(server_module.time, "time", return_value=130.0):
            status, ctype, payload = server_module.route("GET", "/api/status", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["running"], ["cctv1"])
        self.assertEqual(data["tasks"][0]["channel_id"], "cctv1")
        self.assertEqual(data["tasks"][0]["pid"], 4321)
        self.assertEqual(data["tasks"][0]["state"], "running")
        self.assertEqual(data["tasks"][0]["seconds_since_heartbeat"], 7)
        self.assertFalse(data["tasks"][0]["idle_expired"])

    def test_transcode_heartbeat_reports_failure_when_hls_output_is_stale(self):
        fake_proc = mock.Mock()
        fake_proc.pid = 9876
        fake_proc.poll.return_value = None
        server_module.STATE.processes["cctv1"] = fake_proc
        server_module.STATE.heartbeats["cctv1"] = 123.0
        hls_dir = server_module.CONFIG.hls_root / "cctv1"
        hls_dir.mkdir(parents=True, exist_ok=True)
        playlist = hls_dir / "master.m3u8"
        segment = hls_dir / "seg_00001.ts"
        playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nseg_00001.ts\n", encoding="utf-8")
        segment.write_bytes(b"0123456789")
        server_module.os.utime(playlist, (100.0, 100.0))
        server_module.os.utime(segment, (100.0, 100.0))
        with mock.patch.object(server_module.time, "time", return_value=200.0):
            status, ctype, payload = server_module.route("POST", "/api/transcode/cctv1/heartbeat", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertFalse(data["hls_healthy"])
        self.assertIn("stale", data["reason"])

    def test_api_config_reads_current_env_for_management_ui(self):
        status, ctype, body = server_module.route("GET", "/api/config", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["config"]["IPTV_TRANSCODER_PORT"], "18096")
        self.assertEqual(data["config"]["IPTV_TRANSCODER_MANAGEMENT_PORT"], "18097")
        self.assertEqual(data["config"]["IPTV_TRANSCODER_API_KEY"], "old-key")
        self.assertEqual(data["config"]["IPTV_TRANSCODER_QSV_LOW_POWER_H264"], "1")
        self.assertEqual(data["config"]["IPTV_TRANSCODER_QSV_LOW_POWER_1080P_MEDIUM_BITRATE"], "6000k")
        self.assertEqual(data["env_file"], str(self.env))

    def test_management_port_proxies_config_and_details_endpoints(self):
        for path in ["/api/config", "/api/health/details"]:
            with self.subTest(path=path):
                status, ctype, body = server_module.management_route("GET", path, "old-key", {})
                self.assertEqual(status, 200)
                data = json.loads(body.decode("utf-8"))
                self.assertTrue(data["ok"])

    def test_management_page_trims_key_and_explains_401_and_404(self):
        status, ctype, body = server_module.management_route("GET", "/", None, {})
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("document.getElementById('authKey').value.trim()", text)
        self.assertIn("API Key 不匹配", text)
        self.assertIn("新版管理端口也会代理 /api/config", text)

    def test_api_config_post_writes_env_and_requires_restart(self):
        body = {
            "IPTV_TRANSCODER_PORT": "18123",
            "IPTV_TRANSCODER_MANAGEMENT_PORT": "18124",
            "IPTV_TRANSCODER_PUBLIC_BASE_URL": "http://192.168.1.100:18123",
            "IPTV_TRANSCODER_API_KEY": "new-key",
            "IPTV_TRANSCODER_ALLOWED_UPSTREAMS": "192.168.1.1:7088,192.168.1.2:7088",
            "IPTV_TRANSCODER_QSV_LOW_POWER_H264": "0",
            "IPTV_TRANSCODER_QSV_LOW_POWER_1080P_MEDIUM_BITRATE": "6500k",
        }
        status, ctype, payload = server_module.route("POST", "/api/config", "old-key", body)
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(data["restart_required"])
        written = self.env.read_text(encoding="utf-8")
        self.assertIn("IPTV_TRANSCODER_PORT=18123\n", written)
        self.assertIn("IPTV_TRANSCODER_MANAGEMENT_PORT=18124\n", written)
        self.assertIn("IPTV_TRANSCODER_API_KEY=new-key\n", written)
        self.assertIn("IPTV_TRANSCODER_ALLOWED_UPSTREAMS=192.168.1.1:7088,192.168.1.2:7088\n", written)
        self.assertIn("IPTV_TRANSCODER_QSV_LOW_POWER_H264=0\n", written)
        self.assertIn("IPTV_TRANSCODER_QSV_LOW_POWER_1080P_MEDIUM_BITRATE=6500k\n", written)

    def test_api_config_roundtrip_preserves_shell_quoted_values(self):
        self.env.write_text(
            self.env.read_text(encoding="utf-8") +
            "IPTV_TRANSCODER_PUBLIC_BASE_URL='http://NAS IP:18096/path value'\n"
            "IPTV_TRANSCODER_API_KEY='key with spaces and '\\''quotes'\\'''"
            "\n",
            encoding="utf-8",
        )
        status, ctype, body = server_module.route("GET", "/api/config", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["config"]["IPTV_TRANSCODER_PUBLIC_BASE_URL"], "http://NAS IP:18096/path value")
        self.assertEqual(data["config"]["IPTV_TRANSCODER_API_KEY"], "key with spaces and 'quotes'")

    def test_api_config_reads_updated_env_after_file_changes(self):
        status, ctype, body = server_module.route("GET", "/api/config", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["config"]["IPTV_TRANSCODER_PUBLIC_BASE_URL"], "http://NAS_IP:18096")
        self.env.write_text(
            self.env.read_text(encoding="utf-8").replace("IPTV_TRANSCODER_PUBLIC_BASE_URL=http://NAS_IP:18096", "IPTV_TRANSCODER_PUBLIC_BASE_URL=http://192.168.1.100:18096"),
            encoding="utf-8",
        )
        status, ctype, body = server_module.route("GET", "/api/config", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(data["config"]["IPTV_TRANSCODER_PUBLIC_BASE_URL"], "http://192.168.1.100:18096")

    def test_api_config_rejects_invalid_ports(self):
        for key in ["IPTV_TRANSCODER_PORT", "IPTV_TRANSCODER_MANAGEMENT_PORT"]:
            with self.subTest(key=key):
                with self.assertRaises(server_module.HTTPError) as ctx:
                    server_module.route("POST", "/api/config", "old-key", {key: "abc"})
                self.assertEqual(ctx.exception.status, 400)
                self.assertIn("PORT", ctx.exception.message)

    def test_api_key_empty_fails_closed_for_protected_endpoints(self):
        server_module.CONFIG.api_key = ""
        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.route("GET", "/api/config", None, {})
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("API key is not configured", ctx.exception.message)

    def test_health_is_public_minimal_and_details_require_api_key(self):
        status, ctype, payload = server_module.route("GET", "/api/health", None, {})
        self.assertEqual(status, 200)
        public = json.loads(payload.decode("utf-8"))
        self.assertTrue(public["ok"])
        self.assertNotIn("ffmpeg", public)
        self.assertNotIn("hardware", public)

        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.route("GET", "/api/health/details", None, {})
        self.assertEqual(ctx.exception.status, 401)

        status, ctype, payload = server_module.route("GET", "/api/health/details", "old-key", {})
        self.assertEqual(status, 200)
        details = json.loads(payload.decode("utf-8"))
        self.assertIn("ffmpeg", details)
        self.assertIn("hardware", details)

    def test_api_config_rejects_unsafe_paths_ranges_urls_and_upstreams(self):
        bad_updates = [
            {"IPTV_TRANSCODER_FFMPEG": "ffmpeg"},
            {"IPTV_TRANSCODER_FFPROBE": "/tmp/ffprobe"},
            {"IPTV_TRANSCODER_MAX_TRANSCODES": "0"},
            {"IPTV_TRANSCODER_IDLE_TIMEOUT": "2"},
            {"IPTV_TRANSCODER_GLOBAL_QUALITY": "99"},
            {"IPTV_TRANSCODER_QSV_LOW_POWER_H264": "maybe"},
            {"IPTV_TRANSCODER_QSV_LOW_POWER_1080P_MEDIUM_BITRATE": "fast"},
            {"IPTV_TRANSCODER_PUBLIC_BASE_URL": "javascript:alert(1)"},
            {"IPTV_TRANSCODER_ALLOWED_UPSTREAMS": "http://192.168.1.1:7088"},
        ]
        for update in bad_updates:
            with self.subTest(update=update):
                with self.assertRaises(server_module.HTTPError) as ctx:
                    server_module.route("POST", "/api/config", "old-key", update)
                self.assertEqual(ctx.exception.status, 400)

    def test_probe_uses_low_latency_ffprobe_config(self):
        server_module.CONFIG.allowed_upstreams = "*"
        server_module.CONFIG.ffprobe_timeout = 3.5
        server_module.CONFIG.ffprobe_analyzeduration = 1_000_000
        server_module.CONFIG.ffprobe_probesize = 500_000
        fake = mock.Mock(returncode=0, stdout='{"streams": [], "format": {}}', stderr="")
        with mock.patch.object(server_module.subprocess, "run", return_value=fake) as run:
            status, ctype, payload = server_module.probe_url("probe", "http://example.test/stream")
        self.assertEqual(status, 200)
        cmd = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs["timeout"], 3.5)
        self.assertIn("1000000", cmd)
        self.assertIn("500000", cmd)
        self.assertIn("-show_streams", cmd)
        self.assertIn("-show_programs", cmd)
        self.assertNotIn("-show_entries", cmd)

    def test_probe_retries_incomplete_live_ts_results_and_returns_best_summary(self):
        server_module.CONFIG.allowed_upstreams = "*"
        incomplete = mock.Mock(returncode=0, stdout='{"streams": [{"index": 0}], "programs": []}', stderr="")
        complete = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "programs": [],
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "profile": "Main 10",
                        "pix_fmt": "yuv420p10le",
                        "width": 3840,
                        "height": 2160,
                    },
                    {"codec_type": "audio", "codec_name": "eac3"},
                ],
            }),
            stderr="[hevc] PPS id out of range",
        )
        with mock.patch.object(server_module.subprocess, "run", side_effect=[incomplete, complete]) as run:
            status, ctype, payload = server_module.probe_url("probe", "http://example.test/stream")
        self.assertEqual(status, 200)
        self.assertEqual(run.call_count, 2)
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["input"]["video_codec"], "hevc")
        self.assertEqual(data["input"]["video_profile"], "main 10")
        self.assertEqual(data["input"]["pix_fmt"], "yuv420p10le")
        self.assertEqual(data["suggested_operation"], "qsv_hevc_to_h264")

    def test_probe_prefers_later_interlaced_result_over_earlier_complete_unknown_field_order_for_live_tv(self):
        server_module.CONFIG.allowed_upstreams = "*"
        ambiguous = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "programs": [],
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "profile": "High",
                        "pix_fmt": "yuv420p",
                        "width": 1920,
                        "height": 1080,
                        "field_order": "unknown",
                    },
                    {"codec_type": "audio", "codec_name": "mp2"},
                ],
            }),
            stderr="",
        )
        interlaced = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "programs": [],
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "profile": "High",
                        "pix_fmt": "yuv420p",
                        "width": 1920,
                        "height": 1080,
                        "field_order": "tt",
                    },
                    {"codec_type": "audio", "codec_name": "mp2"},
                ],
            }),
            stderr="",
        )
        with mock.patch.object(server_module.subprocess, "run", side_effect=[ambiguous, interlaced]) as run:
            status, ctype, payload = server_module.probe_url("probe", "http://example.test/stream")
        self.assertEqual(status, 200)
        self.assertEqual(run.call_count, 2)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["input"]["interlaced"])
        self.assertEqual(data["input"]["field_order"], "tt")
        self.assertEqual(data["suggested_operation"], "qsv_deinterlace")

    def test_probe_returns_direct_playable_audio_only_broadcast_without_transcode_operation(self):
        server_module.CONFIG.allowed_upstreams = "*"
        audio_only = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "programs": [],
                "streams": [
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
            }),
            stderr="",
        )
        with mock.patch.object(server_module.subprocess, "run", return_value=audio_only) as run:
            status, ctype, payload = server_module.probe_url("probe", "http://example.test/stream")
        self.assertEqual(status, 200)
        self.assertEqual(run.call_count, 1)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["audio_only"])
        self.assertFalse(data["needs_transcode"])
        self.assertTrue(data["direct_playable"])
        self.assertEqual(data["reason"], "audio_only_broadcast")
        self.assertEqual(data["suggested_operation"], "")

    def test_probe_returns_frame_rate_fields(self):
        server_module.CONFIG.allowed_upstreams = "*"
        complete = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "programs": [],
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "profile": "Main 10",
                        "pix_fmt": "yuv420p10le",
                        "width": 3840,
                        "height": 2160,
                        "field_order": "progressive",
                        "avg_frame_rate": "25/1",
                        "r_frame_rate": "50/1",
                    },
                    {"codec_type": "audio", "codec_name": "eac3"},
                ],
            }),
            stderr="",
        )
        with mock.patch.object(server_module.subprocess, "run", return_value=complete):
            status, ctype, payload = server_module.probe_url("probe", "http://example.test/stream")
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["input"]["fps"], "25/1")
        self.assertEqual(data["input"]["avg_frame_rate"], "25/1")
        self.assertEqual(data["input"]["r_frame_rate"], "50/1")

    def test_channel_start_enriches_probe_metadata_for_auto_hls_timing(self):
        server_module.CONFIG.allowed_upstreams = "*"
        channels = {
            "beijing4k": {
                "url": "http://example.test/stream",
                "operation": "qsv_hevc_to_h264",
            }
        }

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
                self.terminated = False
            def poll(self):
                return 0 if self.terminated else None
            def terminate(self):
                self.terminated = True
            def kill(self):
                self.terminated = True
            def wait(self, timeout=None):
                self.terminated = True
                return 0

        probe = mock.Mock(
            returncode=0,
            stdout=json.dumps({
                "programs": [],
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "hevc",
                        "profile": "Main 10",
                        "pix_fmt": "yuv420p10le",
                        "width": 3840,
                        "height": 2160,
                        "field_order": "progressive",
                        "color_transfer": "arib-std-b67",
                        "color_space": "bt2020nc",
                        "color_primaries": "bt2020",
                        "color_range": "tv",
                        "avg_frame_rate": "25/1",
                        "r_frame_rate": "50/1",
                    },
                    {"codec_type": "audio", "codec_name": "eac3"},
                ],
            }),
            stderr="",
        )
        with mock.patch.object(server_module, "load_channels", return_value=channels), \
             mock.patch.object(server_module.subprocess, "run", return_value=probe), \
             mock.patch.object(server_module.subprocess, "Popen", return_value=FakeProcess(444444)), \
             mock.patch.object(server_module.time, "sleep", return_value=None):
            status, ctype, payload = server_module.route("POST", "/api/channels/beijing4k/start", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["status"], "started")
        log_text = (server_module.CONFIG.log_root / "beijing4k.log").read_text(encoding="utf-8")
        self.assertIn("-hls_time 2", log_text)
        self.assertIn("-g 50", log_text)
        self.assertIn("-keyint_min 50", log_text)

    def test_start_job_restarts_running_shared_job_when_transcode_parameters_change(self):
        server_module.CONFIG.allowed_upstreams = "*"
        server_module.CONFIG.max_transcodes = 1

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
                self.terminated = False
                self.killed = False
            def poll(self):
                return 0 if self.terminated or self.killed else None
            def terminate(self):
                self.terminated = True
            def kill(self):
                self.killed = True
            def wait(self, timeout=None):
                self.terminated = True
                return 0

        first_proc = FakeProcess(111111)
        second_proc = FakeProcess(222222)
        base_ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main 10",
            "pix_fmt": "yuv420p10le",
            "audio_codec": "eac3",
        }
        changed_ch = {**base_ch, "operation": "qsv_hevc_to_h264_1080p", "resolution": "1080p"}
        with mock.patch.object(server_module.subprocess, "Popen", side_effect=[first_proc, second_proc]) as popen, \
             mock.patch.object(server_module.time, "sleep", return_value=None):
            status, ctype, payload = server_module.start_job("shared", base_ch, reason="first")
            self.assertEqual(status, 200)
            status, ctype, payload = server_module.start_job("shared", changed_ch, reason="changed")
        self.assertEqual(popen.call_count, 2)
        self.assertTrue(first_proc.terminated or first_proc.killed)
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["status"], "started")
        self.assertEqual(data["operation"], "qsv_hevc_to_h264_1080p")
        log_text = (server_module.CONFIG.log_root / "shared.log").read_text(encoding="utf-8")
        self.assertIn("-vf vpp_qsv=w=1920:h=1080:format=nv12", log_text)

    def test_start_job_does_not_report_started_if_stopped_during_startup_probe_window(self):
        server_module.CONFIG.allowed_upstreams = "*"

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
                self.terminated = False
            def poll(self):
                return 0 if self.terminated else None
            def terminate(self):
                self.terminated = True
            def kill(self):
                self.terminated = True
            def wait(self, timeout=None):
                self.terminated = True
                return 0

        proc = FakeProcess(333333)

        def fake_sleep(_seconds):
            server_module.STATE.stop("startup-stop")

        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
        }
        with mock.patch.object(server_module.subprocess, "Popen", return_value=proc), \
             mock.patch.object(server_module.time, "sleep", side_effect=fake_sleep):
            status, ctype, payload = server_module.start_job("startup-stop", ch, reason="test")
        self.assertEqual(status, 409)
        data = json.loads(payload.decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "stopped_during_startup")

    def test_stop_during_starting_window_cancels_pending_start_before_process_registration(self):
        server_module.CONFIG.allowed_upstreams = "*"

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
                self.terminated = False
            def poll(self):
                return 0 if self.terminated else None
            def terminate(self):
                self.terminated = True
            def kill(self):
                self.terminated = True
            def wait(self, timeout=None):
                self.terminated = True
                return 0

        proc = FakeProcess(444444)
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
        }

        def fake_prepare(channel_id, spec, body):
            server_module.STATE.starting.add(channel_id)
            stopped = server_module.STATE.stop(channel_id)
            self.assertTrue(stopped)
            server_module.STATE.starting.add(channel_id)

        with mock.patch.object(server_module, "prepare_start_job", side_effect=fake_prepare), \
             mock.patch.object(server_module, "spawn_job_process", return_value=proc), \
             mock.patch.object(server_module.time, "sleep", return_value=None):
            status, ctype, payload = server_module.start_job("cancel-before-register", ch, reason="test")
        self.assertEqual(status, 409)
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["status"], "stopped_during_startup")

    def test_heartbeat_reports_starting_state_during_startup_window(self):
        server_module.STATE.starting.add("warmup")
        status, ctype, payload = server_module.route("POST", "/api/transcode/warmup/heartbeat", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["state"], "starting")
        self.assertEqual(data["reason"], "starting")
        self.assertFalse(data["hls_healthy"])

    def test_hls_health_status_uses_short_cache_for_unchanged_playlist(self):
        hls_dir = server_module.CONFIG.hls_root / "cached"
        hls_dir.mkdir(parents=True, exist_ok=True)
        playlist = hls_dir / "master.m3u8"
        segment = hls_dir / "seg_00001.ts"
        playlist.write_text("#EXTM3U\n#EXTINF:2.0,\nseg_00001.ts\n", encoding="utf-8")
        segment.write_bytes(b"0123456789")
        server_module.HLS_HEALTH_CACHE.clear()
        calls = {"read_text": 0}
        original_read_text = Path.read_text

        def counting_read_text(path_obj, *args, **kwargs):
            calls["read_text"] += 1
            return original_read_text(path_obj, *args, **kwargs)

        with mock.patch("pathlib.Path.read_text", new=counting_read_text):
            first = server_module.hls_health_status("cached", now=100.0)
            second = server_module.hls_health_status("cached", now=100.2)
        self.assertEqual(first["reason"], "ok")
        self.assertEqual(second["reason"], "ok")
        self.assertEqual(calls["read_text"], 1)

    def test_read_log_tail_uses_cache_for_unchanged_log(self):
        server_module.CONFIG.log_root.mkdir(parents=True, exist_ok=True)
        log_path = server_module.CONFIG.log_root / "cached-tail.log"
        log_path.write_text("hello\nffmpeg failed\n", encoding="utf-8")
        server_module.LOG_TAIL_CACHE.clear()
        calls = {"open": 0}
        original_open = Path.open

        def counting_open(path_obj, *args, **kwargs):
            if path_obj == log_path:
                calls["open"] += 1
            return original_open(path_obj, *args, **kwargs)

        with mock.patch("pathlib.Path.open", new=counting_open):
            first = server_module.read_log_tail("cached-tail")
            second = server_module.read_log_tail("cached-tail")
        self.assertIn("ffmpeg failed", first)
        self.assertEqual(first, second)
        self.assertEqual(calls["open"], 1)

    def test_clear_hls_caches_for_removed_paths_clears_channel_health_cache(self):
        server_module.HLS_HEALTH_CACHE["demo"] = ((1.0, 1, 1.0), {"healthy": True})
        removed_path = str((server_module.CONFIG.hls_root / "demo" / "seg_00001.ts").resolve())
        server_module.clear_hls_caches_for_removed_paths([removed_path])
        self.assertNotIn("demo", server_module.HLS_HEALTH_CACHE)

    def test_stop_endpoint_clears_runtime_caches_for_channel(self):
        server_module.HLS_HEALTH_CACHE["demo"] = ((1.0, 1, 1.0), {"ok": True})
        log_path = (server_module.CONFIG.log_root / "demo.log").resolve()
        server_module.LOG_TAIL_CACHE[f"{log_path}:12000"] = ((1.0, 1, 12000), "tail")
        fake_proc = mock.Mock()
        fake_proc.poll.return_value = 0
        server_module.STATE.processes["demo"] = fake_proc
        status, ctype, payload = server_module.route("POST", "/api/transcode/demo/stop", "old-key", {})
        self.assertEqual(status, 200)
        self.assertNotIn("demo", server_module.HLS_HEALTH_CACHE)
        self.assertNotIn(f"{log_path}:12000", server_module.LOG_TAIL_CACHE)

    def test_startup_probe_failure_clears_runtime_caches(self):
        server_module.HLS_HEALTH_CACHE["demo"] = ((1.0, 1, 1.0), {"ok": True})
        log_path = (server_module.CONFIG.log_root / "demo.log").resolve()
        server_module.LOG_TAIL_CACHE[f"{log_path}:12000"] = ((1.0, 1, 12000), "tail")

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
            def poll(self):
                return 1

        proc = FakeProcess(666666)
        server_module.STATE.processes["demo"] = proc
        server_module.STATE.heartbeats["demo"] = 1.0
        server_module.STATE.job_specs["demo"] = (("url", "x"),)
        with mock.patch.object(server_module.time, "sleep", return_value=None):
            status, ctype, payload = server_module.startup_probe_result("demo", {"operation": "qsv_h264"}, proc)
        self.assertEqual(status, 502)
        self.assertNotIn("demo", server_module.HLS_HEALTH_CACHE)
        self.assertNotIn(f"{log_path}:12000", server_module.LOG_TAIL_CACHE)

    def test_stale_start_cancelled_flag_does_not_cancel_next_clean_start(self):
        server_module.CONFIG.allowed_upstreams = "*"
        server_module.STATE.start_cancelled.add("clean-start")

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid
            def poll(self):
                return None
            def terminate(self):
                pass
            def kill(self):
                pass
            def wait(self, timeout=None):
                return 0

        proc = FakeProcess(555555)
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
        }
        with mock.patch.object(server_module.subprocess, "Popen", return_value=proc), \
             mock.patch.object(server_module.time, "sleep", return_value=None):
            status, ctype, payload = server_module.start_job("clean-start", ch, reason="test")
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "started")

    def test_probe_invalid_ffprobe_json_is_structured_error_not_exception(self):
        server_module.CONFIG.allowed_upstreams = "*"
        fake = mock.Mock(returncode=0, stdout="not-json", stderr="ffprobe said nope")
        with mock.patch.object(server_module.subprocess, "run", return_value=fake):
            status, ctype, payload = server_module.probe_url("probe", "http://example.test/stream")
        self.assertEqual(status, 502)
        data = json.loads(payload.decode("utf-8"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"]["code"], "invalid_ffprobe_json")

    def test_hls_ts_response_is_file_response_for_streaming(self):
        ts_dir = server_module.CONFIG.hls_root / "chan"
        ts_dir.mkdir(parents=True, exist_ok=True)
        ts_file = ts_dir / "seg_00001.ts"
        ts_file.write_bytes(b"0123456789")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes must not be used")):
            response = server_module.serve_hls("/hls/chan/seg_00001.ts")
        self.assertIsInstance(response, server_module.FileResponse)
        self.assertEqual(response.content_type, "video/mp2t")

    def test_signal_shutdown_handler_stops_state_and_http_servers(self):
        stopped = []
        shutdowns = []
        class DummyState:
            def stop_all(self):
                stopped.append(True)
        class DummyHTTPD:
            def shutdown(self):
                shutdowns.append("shutdown")
        server_module.STATE = DummyState()
        handler = server_module.make_shutdown_handler([DummyHTTPD(), DummyHTTPD()])
        handler(15, None)
        self.assertEqual(stopped, [True])
        self.assertEqual(shutdowns, ["shutdown", "shutdown"])

    def test_signal_shutdown_handler_runs_http_shutdown_outside_signal_thread(self):
        workers = []
        class DummyState:
            def stop_all(self):
                pass
        class DummyHTTPD:
            def shutdown(self):
                pass
        class ImmediateThread:
            def __init__(self, target, daemon=False):
                self.target = target
                self.daemon = daemon
                workers.append(self)
            def start(self):
                self.target()
        server_module.STATE = DummyState()
        with mock.patch.object(server_module.threading, "Thread", ImmediateThread):
            handler = server_module.make_shutdown_handler([DummyHTTPD()])
            handler(15, None)
        self.assertEqual(len(workers), 1)
        self.assertTrue(workers[0].daemon)

    def test_log_endpoint_returns_channel_log_tail(self):
        server_module.CONFIG.log_root.mkdir(parents=True, exist_ok=True)
        (server_module.CONFIG.log_root / "diag.log").write_text("hello\nffmpeg failed\n", encoding="utf-8")
        status, ctype, payload = server_module.route("GET", "/api/logs/diag", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["channel_id"], "diag")
        self.assertIn("ffmpeg failed", data["log_tail"])

    def test_log_endpoint_supports_max_bytes_query(self):
        server_module.CONFIG.log_root.mkdir(parents=True, exist_ok=True)
        content = "0123456789abcdef" * 20
        (server_module.CONFIG.log_root / "diag.log").write_text(content, encoding="utf-8")
        status, ctype, payload = server_module.route("GET", "/api/logs/diag?max_bytes=256", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertEqual(data["log_tail"], content[-256:])

    def test_service_log_endpoint_returns_tail(self):
        server_module.CONFIG.log_root.mkdir(parents=True, exist_ok=True)
        content = ("boot ok\nopencl ok\n" * 30)
        (server_module.CONFIG.log_root / "service.log").write_text(content, encoding="utf-8")
        status, ctype, payload = server_module.route("GET", "/api/logs/service?max_bytes=256", "old-key", {})
        self.assertEqual(status, 200)
        data = json.loads(payload.decode("utf-8"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["channel_id"], "service")
        self.assertEqual(data["log_tail"], content[-256:])

    def test_log_endpoint_rejects_invalid_max_bytes(self):
        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.route("GET", "/api/logs/diag?max_bytes=abc", "old-key", {})
        self.assertEqual(ctx.exception.status, 400)

    def test_log_endpoint_rejects_unsafe_channel_id(self):
        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.route("GET", "/api/logs/../x", "old-key", {})
        self.assertEqual(ctx.exception.status, 404)

    def test_dynamic_transcode_rejects_invalid_dimensions_as_bad_request(self):
        server_module.CONFIG.allowed_upstreams = "*"
        body = {
            "channel_id": "bad-dim",
            "input_url": "http://example.test/stream",
            "operation": "qsv_h264",
            "width": "wide",
            "height": 1080,
        }
        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.validate_job_body(body)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("width and height", ctx.exception.message)

        body["width"] = 1920
        body["height"] = -1
        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.validate_job_body(body)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("non-negative", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
