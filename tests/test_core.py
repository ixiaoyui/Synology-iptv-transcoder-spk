import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from iptvtranscoder.core import CHANNELS_CACHE, Config, TranscoderState, build_ffmpeg_command, cleanup_hls_root, load_channels, normalize_resolution, parse_frame_rate, safe_channel_id
from iptvtranscoder import server as server_module


class CoreTests(unittest.TestCase):
    def setUp(self):
        CHANNELS_CACHE.clear()

    def test_stop_clears_state_before_waiting_for_process_exit(self):
        state = TranscoderState(stop_timeout=0.1)
        test_case = self

        class FakeProcess:
            pid = 1234
            def __init__(self):
                self.wait_calls = 0
            def poll(self):
                return None
            def terminate(self):
                pass
            def kill(self):
                pass
            def wait(self, timeout=None):
                self.wait_calls += 1
                test_case.assertNotIn("chan", state.processes)
                raise TimeoutError("still exiting")

        proc = FakeProcess()
        state.processes["chan"] = proc
        state.heartbeats["chan"] = 1.0
        state.job_specs["chan"] = (("url", "http://example.test/stream"),)
        with unittest.mock.patch("iptvtranscoder.core.os.killpg", side_effect=ProcessLookupError):
            self.assertTrue(state.stop("chan"))
        self.assertNotIn("chan", state.processes)
        self.assertNotIn("chan", state.heartbeats)
        self.assertNotIn("chan", state.job_specs)
        self.assertGreaterEqual(proc.wait_calls, 1)

    def test_stop_all_cancels_starting_channels_without_registered_process(self):
        state = TranscoderState(stop_timeout=0.1)
        state.starting.add("pending")
        state.stop_all()
        self.assertNotIn("pending", state.starting)
        self.assertIn("pending", state.start_cancelled)

    def test_is_starting_checks_state_under_lock(self):
        state = TranscoderState(stop_timeout=0.1)
        state.starting.add("pending")
        self.assertTrue(state.is_starting("pending"))
        self.assertFalse(state.is_starting("missing"))

    def test_safe_channel_id_rejects_path_traversal(self):
        for value in ["../x", "a/b", "", ".", "中文", "x y"]:
            with self.subTest(value=value):
                self.assertFalse(safe_channel_id(value))
        self.assertTrue(safe_channel_id("sdzy-hd_1"))

    def test_deinterlace_qsv_command_uses_vpp_and_h264_qsv(self):
        cfg = Config(
            ffmpeg_bin="ffmpeg",
            qsv_device="/dev/dri/renderD128",
            hls_root=Path("/tmp/hls"),
        )
        ch = {
            "url": "http://192.168.1.1:7088/rtp/239.x.x.x:5002",
            "operation": "qsv_deinterlace",
            "video_codec": "h264",
            "audio_codec": "aac",
        }
        cmd = build_ffmpeg_command(cfg, "sdzy-hd", ch)
        joined = " ".join(cmd)
        self.assertIn("-hwaccel qsv", joined)
        self.assertIn("-hwaccel_output_format qsv", joined)
        self.assertIn("-qsv_device /dev/dri/renderD128", joined)
        self.assertIn("vpp_qsv=deinterlace=2", joined)
        self.assertIn("h264_qsv", cmd)
        self.assertIn("-low_power 1", joined)
        self.assertIn("-b:v 3500k", joined)
        self.assertIn("-maxrate 4000k", joined)
        self.assertIn("-bufsize 8000k", joined)
        self.assertNotIn("-global_quality", joined)
        self.assertIn("/tmp/hls/sdzy-hd/master.m3u8", cmd[-1])

    def test_qsv_low_power_h264_can_be_disabled(self):
        cfg = Config(hls_root=Path("/tmp/hls"), qsv_low_power_h264=False)
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "video_codec": "h264",
            "audio_codec": "aac",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "no-low-power", ch))
        self.assertIn("-c:v h264_qsv", joined)
        self.assertNotIn("-low_power 1", joined)
        self.assertIn("-global_quality 23", joined)

    def test_qsv_low_power_h264_uses_quality_preset_ladder_for_selected_resolution(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
            "resolution": "2k",
            "quality_preset": "high",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "lp-2k-high", ch))
        self.assertIn("-b:v 12000k", joined)
        self.assertIn("-maxrate 14000k", joined)
        self.assertIn("-bufsize 28000k", joined)
        self.assertIn("-preset veryfast", joined)
        self.assertNotIn("-global_quality", joined)

    def test_qsv_low_power_h264_infers_quality_preset_from_global_quality_override(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
            "resolution": "1080p",
            "global_quality": 27,
        }
        joined = " ".join(build_ffmpeg_command(cfg, "lp-gq-low", ch))
        self.assertIn("-b:v 4500k", joined)
        self.assertIn("-maxrate 5000k", joined)
        self.assertIn("-bufsize 10000k", joined)

    def test_qsv_low_power_h264_uses_source_resolution_bucket_when_auto_preserves_1080p(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_deinterlace",
            "video_codec": "h264",
            "audio_codec": "aac",
            "width": 1920,
            "height": 1080,
        }
        joined = " ".join(build_ffmpeg_command(cfg, "lp-auto-1080", ch))
        self.assertIn("-b:v 6000k", joined)
        self.assertIn("-maxrate 7000k", joined)
        self.assertIn("-bufsize 14000k", joined)

    def test_hls_defaults_use_conservative_qsv_compatible_segments(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://192.168.1.1:7088/rtp/239.x.x.x:5002",
            "operation": "qsv_h264",
            "video_codec": "h264",
            "audio_codec": "aac",
        }
        cmd = build_ffmpeg_command(cfg, "safe-hls", ch)
        joined = " ".join(cmd)
        self.assertIn("-hls_time 2", joined)
        self.assertIn("-g 100", joined)
        self.assertIn("-keyint_min 100", joined)
        self.assertNotIn("-force_key_frames", joined)

    def test_hls_auto_timing_uses_source_frame_rate_for_25fps(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
            "fps": "25/1",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "auto-25fps", ch))
        self.assertIn("-hls_time 2", joined)
        self.assertIn("-g 50", joined)
        self.assertIn("-keyint_min 50", joined)

    def test_hls_auto_timing_uses_source_frame_rate_for_50fps(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
            "fps": "50/1",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "auto-50fps", ch))
        self.assertIn("-hls_time 2", joined)
        self.assertIn("-g 100", joined)
        self.assertIn("-keyint_min 100", joined)

    def test_hls_auto_timing_prefers_avg_frame_rate_when_present(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
            "avg_frame_rate": "25/1",
            "r_frame_rate": "50/1",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "auto-avg-rate", ch))
        self.assertIn("-hls_time 2", joined)
        self.assertIn("-g 50", joined)
        self.assertIn("-keyint_min 50", joined)

    def test_hls_time_and_gop_can_be_overridden_per_config_and_channel(self):
        cfg = Config(hls_root=Path("/tmp/hls"), hls_time=1.5, hls_gop=30)
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
            "gop": "45",
            "keyint_min": "45",
        }
        cmd = build_ffmpeg_command(cfg, "custom-hls", ch)
        joined = " ".join(cmd)
        self.assertIn("-hls_time 1.5", joined)
        self.assertIn("-g 45", joined)
        self.assertIn("-keyint_min 45", joined)
        self.assertNotIn("-force_key_frames", joined)

    def test_parse_frame_rate_supports_fractional_and_plain_values(self):
        self.assertEqual(parse_frame_rate("25/1"), 25.0)
        self.assertAlmostEqual(parse_frame_rate("30000/1001"), 30000 / 1001, places=5)
        self.assertEqual(parse_frame_rate("50"), 50.0)
        self.assertIsNone(parse_frame_rate("0/0"))
        self.assertIsNone(parse_frame_rate(""))

    def test_hevc_transcode_mode_has_no_deinterlace_filter(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {"url": "http://example.test/stream", "operation": "qsv_hevc_to_h264", "audio_codec": "mp2"}
        cmd = build_ffmpeg_command(cfg, "hevc", ch)
        joined = " ".join(cmd)
        self.assertNotIn("deinterlace=2", joined)
        self.assertIn("-c:v hevc_qsv", joined)
        self.assertIn("-c:v h264_qsv", joined)
        self.assertIn("-c:a aac", joined)

    def test_hevc_transcode_converts_to_nv12_even_when_probe_metadata_is_missing(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {"url": "http://example.test/stream", "operation": "qsv_hevc_to_h264", "audio_codec": "eac3"}
        joined = " ".join(build_ffmpeg_command(cfg, "hevc-missing-metadata", ch))
        self.assertIn("-vf vpp_qsv=format=nv12", joined)
        self.assertIn("-c:v hevc_qsv", joined)
        self.assertIn("-c:a aac", joined)
        self.assertIn("-ac 2", joined)
        self.assertIn("-ar 48000", joined)

    def test_hevc_yuv420p_8bit_transcode_does_not_force_nv12_format(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main",
            "pix_fmt": "yuv420p",
            "audio_codec": "aac",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "hevc-yuv420p", ch))
        self.assertIn("-vf vpp_qsv", joined)
        self.assertNotIn("format=nv12", joined)
        self.assertIn("-c:v hevc_qsv", joined)
        self.assertIn("-c:a copy", joined)
        self.assertNotIn("-ac 2", joined)
        self.assertNotIn("-ar 48000", joined)

    def test_hevc_main10_transcode_converts_to_nv12_for_h264_qsv_encoder(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264_1080p",
            "video_codec": "hevc",
            "video_profile": "Main 10",
            "pix_fmt": "yuv420p10le",
            "audio_codec": "eac3",
            "resolution": "1080p",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "hevc-main10", ch))
        self.assertIn("-vf vpp_qsv=w=1920:h=1080:format=nv12", joined)
        self.assertIn("-c:v hevc_qsv", joined)
        self.assertIn("-c:v h264_qsv", joined)
        self.assertIn("-c:a aac", joined)

    def test_probe_summary_preserves_profile_and_pix_fmt_for_main10_detection(self):
        summary = server_module.summarize_probe({
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "profile": "Main 10",
                    "pix_fmt": "yuv420p10le",
                    "width": 1920,
                    "height": 1080,
                    "field_order": "progressive",
                },
                {"codec_type": "audio", "codec_name": "eac3"},
            ]
        })
        self.assertEqual(summary["video_profile"], "main 10")
        self.assertEqual(summary["pix_fmt"], "yuv420p10le")
        self.assertEqual(summary["operation"], "qsv_hevc_to_h264")
        self.assertIn("format=nv12", summary["hardware_plan"]["filter"])

    def test_probe_summary_keeps_native_resolution_operation_for_4k_hevc_by_default(self):
        summary = server_module.summarize_probe({
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "profile": "Main 10",
                    "pix_fmt": "yuv420p10le",
                    "width": 3840,
                    "height": 2160,
                    "field_order": "progressive",
                },
                {"codec_type": "audio", "codec_name": "eac3"},
            ]
        })
        self.assertEqual(summary["operation"], "qsv_hevc_to_h264")
        self.assertEqual(summary["hardware_plan"]["filter"], "vpp_qsv=w=3840:h=2160:format=nv12")

    def test_probe_summary_preserves_hdr_color_metadata_and_marks_tonemap_filter(self):
        summary = server_module.summarize_probe({
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
                    "avg_frame_rate": "50/1",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        })
        self.assertEqual(summary["color_transfer"], "arib-std-b67")
        self.assertEqual(summary["color_space"], "bt2020nc")
        self.assertEqual(summary["color_primaries"], "bt2020")
        self.assertEqual(summary["color_range"], "tv")
        self.assertEqual(summary["fps"], "50/1")
        self.assertEqual(summary["operation"], "qsv_hevc_to_h264")
        self.assertIn("setparams=color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc", summary["hardware_plan"]["filter"])
        self.assertIn("vpp_qsv=", summary["hardware_plan"]["filter"])
        self.assertIn("hwmap=derive_device=opencl:mode=read", summary["hardware_plan"]["filter"])
        self.assertIn("tonemap_opencl=", summary["hardware_plan"]["filter"])
        self.assertIn("tonemap=bt2390", summary["hardware_plan"]["filter"])
        self.assertIn("peak=100", summary["hardware_plan"]["filter"])
        self.assertIn("desat=0", summary["hardware_plan"]["filter"])
        self.assertIn("hwmap=derive_device=qsv:mode=write:reverse=1:extra_hw_frames=16", summary["hardware_plan"]["filter"])
        self.assertNotIn("hwdownload", summary["hardware_plan"]["filter"])

    def test_probe_summary_uses_qsv_vpp_tonemap_for_8bit_hdr_hevc(self):
        summary = server_module.summarize_probe({
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "profile": "Main",
                    "pix_fmt": "yuv420p",
                    "width": 3840,
                    "height": 2160,
                    "field_order": "progressive",
                    "color_transfer": "arib-std-b67",
                    "color_space": "bt2020nc",
                    "color_primaries": "bt2020",
                    "color_range": "tv",
                },
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        })
        self.assertIn("vpp_qsv=", summary["hardware_plan"]["filter"])
        self.assertIn("tonemap=1", summary["hardware_plan"]["filter"])
        self.assertIn("brightness=8", summary["hardware_plan"]["filter"])
        self.assertIn("contrast=1", summary["hardware_plan"]["filter"])
        self.assertNotIn("tonemap_opencl=", summary["hardware_plan"]["filter"])

    def test_probe_summary_treats_audio_only_broadcast_as_direct_playable(self):
        summary = server_module.summarize_probe({
            "streams": [
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        })
        self.assertTrue(summary["audio_only"])
        self.assertFalse(summary["needs_transcode"])
        self.assertTrue(summary["direct_playable"])
        self.assertTrue(summary["browser_playable"])
        self.assertEqual(summary["reason"], "audio_only_broadcast")
        self.assertEqual(summary["operation"], "")
        self.assertEqual(summary["hardware_plan"]["operation"], "")
        self.assertEqual(summary["hardware_plan"]["filter"], "")

    def test_bt2020_reserved_hevc_does_not_enable_hdr_tonemap_without_hdr_transfer(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main",
            "pix_fmt": "yuv420p",
            "audio_codec": "aac",
            "color_transfer": "reserved",
            "color_space": "bt2020nc",
            "color_primaries": "bt2020",
            "color_range": "tv",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "bt2020-reserved", ch))
        self.assertIn("-vf vpp_qsv", joined)
        self.assertNotIn("tonemap=1", joined)
        self.assertNotIn("out_color_transfer=bt709", joined)
        self.assertIn("-c:a copy", joined)

    def test_hdr_hevc_8bit_transcode_uses_qsv_vpp_tonemap(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main",
            "pix_fmt": "yuv420p",
            "audio_codec": "aac",
            "color_transfer": "arib-std-b67",
            "color_space": "bt2020nc",
            "color_primaries": "bt2020",
            "color_range": "tv",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "hdr-hevc", ch))
        self.assertNotIn("-init_hw_device opencl=ocl@va", joined)
        self.assertNotIn("tonemap_opencl=", joined)
        self.assertIn("vpp_qsv=", joined)
        self.assertIn("out_color_matrix=bt709", joined)
        self.assertIn("out_color_primaries=bt709", joined)
        self.assertIn("out_color_transfer=bt709", joined)
        self.assertIn("tonemap=1", joined)
        self.assertIn("brightness=8", joined)
        self.assertIn("contrast=1", joined)
        self.assertIn("-c:a aac", joined)
        self.assertNotIn("-c:a copy", joined)

    def test_hdr_hevc_8bit_transcode_uses_configurable_qsv_vpp_tonemap_values(self):
        cfg = Config(hls_root=Path("/tmp/hls"), hdr_vpp_brightness=12, hdr_vpp_contrast=1.25)
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main",
            "pix_fmt": "yuv420p",
            "audio_codec": "aac",
            "color_transfer": "arib-std-b67",
            "color_space": "bt2020nc",
            "color_primaries": "bt2020",
            "color_range": "tv",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "hdr-hevc-vpp-tunable", ch))
        self.assertIn("brightness=12", joined)
        self.assertIn("contrast=1.25", joined)

    def test_hdr_main10_hevc_transcode_uses_qsv_opencl_tonemap_without_cpu_download(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main 10",
            "pix_fmt": "yuv420p10le",
            "audio_codec": "aac",
            "color_transfer": "arib-std-b67",
            "color_space": "bt2020nc",
            "color_primaries": "bt2020",
            "color_range": "tv",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "hdr-main10", ch))
        self.assertIn("vpp_qsv", joined)
        self.assertIn("tonemap_opencl=", joined)
        self.assertIn("tonemap=bt2390", joined)
        self.assertIn("peak=100", joined)
        self.assertIn("desat=0", joined)
        self.assertNotIn("hwdownload", joined)
        self.assertNotIn("format=p010le", joined)

    def test_hdr_4k_hevc_transcode_disables_b_frames_for_browser_hardware_decode_stability(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main",
            "pix_fmt": "yuv420p",
            "audio_codec": "aac",
            "width": 3840,
            "height": 2160,
            "color_transfer": "arib-std-b67",
            "color_space": "bt2020nc",
            "color_primaries": "bt2020",
            "color_range": "tv",
        }
        cmd = build_ffmpeg_command(cfg, "hdr-hevc-4k-browser", ch)
        joined = " ".join(cmd)
        self.assertIn("-bf 0", joined)
        self.assertLess(cmd.index("-bf"), cmd.index("-g"))
        self.assertIn("-color_trc bt709", joined)

    def test_default_hevc_transcode_preserves_source_resolution_and_framerate(self):
        cfg = Config(hls_root=Path("/tmp/hls"), global_quality=23, global_quality_4k=27, qsv_low_power_h264=False)
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_hevc_to_h264",
            "video_codec": "hevc",
            "video_profile": "Main 10",
            "pix_fmt": "yuv420p10le",
            "audio_codec": "eac3",
            "width": 3840,
            "height": 2160,
        }
        joined = " ".join(build_ffmpeg_command(cfg, "hevc-native", ch))
        self.assertIn("-vf vpp_qsv=w=3840:h=2160:format=nv12", joined)
        self.assertIn("-global_quality 27", joined)
        self.assertNotIn("w=1920:h=1080", joined)
        self.assertNotIn(" -r ", f" {joined} ")

    def test_explicit_4k_output_uses_4k_quality_even_for_non_4k_source(self):
        cfg = Config(hls_root=Path("/tmp/hls"), global_quality=23, global_quality_4k=27, qsv_low_power_h264=False)
        ch = {
            "url": "http://example.test/stream",
            "operation": "qsv_h264",
            "audio_codec": "aac",
            "width": 1920,
            "height": 1080,
            "resolution": "4k",
        }
        joined = " ".join(build_ffmpeg_command(cfg, "upscale-4k", ch))
        self.assertIn("-vf vpp_qsv=w=3840:h=2160", joined)
        self.assertIn("-global_quality 27", joined)

    def test_supported_resolution_values_normalize_for_api_payloads(self):
        for value in ["", None, "auto", "AUTO"]:
            with self.subTest(value=value):
                self.assertEqual(normalize_resolution(value), "auto")
        for value in ["720p", "1080p", "2k", "2K", "4k", "4K"]:
            with self.subTest(value=value):
                self.assertEqual(normalize_resolution(value), str(value).lower())
        with self.assertRaisesRegex(ValueError, "resolution"):
            normalize_resolution("1440p")

    def test_resolution_selection_adds_qsv_scaling_filter(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        expected_filters = {
            "auto": "vpp_qsv=format=nv12",
            "720p": "vpp_qsv=w=1280:h=720:format=nv12",
            "1080p": "vpp_qsv=w=1920:h=1080:format=nv12",
            "2k": "vpp_qsv=w=2560:h=1440:format=nv12",
            "4k": "vpp_qsv=w=3840:h=2160:format=nv12",
        }
        for resolution, expected_filter in expected_filters.items():
            with self.subTest(resolution=resolution):
                ch = {"url": "http://example.test/stream", "operation": "qsv_hevc_to_h264", "audio_codec": "mp2", "resolution": resolution}
                joined = " ".join(build_ffmpeg_command(cfg, f"hevc-{resolution}", ch))
                self.assertIn(f"-vf {expected_filter}", joined)

    def test_deinterlace_and_resolution_selection_are_combined_in_qsv_filter(self):
        cfg = Config(hls_root=Path("/tmp/hls"))
        ch = {"url": "http://example.test/stream", "operation": "qsv_mpeg2_deinterlace_to_h264", "audio_codec": "mp2", "resolution": "720p"}
        joined = " ".join(build_ffmpeg_command(cfg, "mpeg2-720p", ch))
        self.assertIn("-vf vpp_qsv=deinterlace=2:w=1280:h=720", joined)

    def test_allowed_upstreams_blocks_untrusted_input_url(self):
        server_module.CONFIG = Config(allowed_upstreams="192.168.1.1:7088")
        server_module.ensure_input_allowed("http://192.168.1.1:7088/rtp/239.1.1.1:5002")
        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.ensure_input_allowed("http://127.0.0.1:5000/rtp/239.1.1.1:5002")
        self.assertEqual(ctx.exception.status, 403)

    def test_allowed_upstreams_empty_is_fail_closed_and_star_is_explicit_allow_all(self):
        server_module.CONFIG = Config(allowed_upstreams="")
        with self.assertRaises(server_module.HTTPError) as ctx:
            server_module.ensure_input_allowed("http://192.168.1.1:7088/rtp/239.1.1.1:5002")
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("No upstream", ctx.exception.message)

        server_module.CONFIG = Config(allowed_upstreams="*")
        server_module.ensure_input_allowed("http://127.0.0.1:5000/stream")

    def test_load_channels_uses_cache_until_file_changes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "channels.json"
            path.write_text('{"a":{"url":"http://example.test/a","operation":"qsv_h264"}}', encoding="utf-8")
            first = load_channels(path)
            second = load_channels(path)
            self.assertEqual(sorted(first), ["a"])
            self.assertEqual(sorted(second), ["a"])
            path.write_text('{"b":{"url":"http://example.test/b","operation":"qsv_h264"}}', encoding="utf-8")
            third = load_channels(path)
            self.assertEqual(sorted(third), ["b"])

    def test_hls_cleanup_removes_expired_channel_dirs_and_enforces_quota(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stale = root / "stale"
            stale.mkdir()
            (stale / "seg_00001.ts").write_bytes(b"old")
            active = root / "active"
            active.mkdir()
            (active / "seg_00001.ts").write_bytes(b"active")
            quota = root / "quota"
            quota.mkdir()
            f1 = quota / "seg_00001.ts"
            f2 = quota / "seg_00002.ts"
            f1.write_bytes(b"1" * 8)
            f2.write_bytes(b"2" * 8)
            old = 1000.0
            for p in [stale, stale / "seg_00001.ts", f1, quota]:
                os.utime(p, (old, old))
            removed = cleanup_hls_root(root, ttl_seconds=1, max_bytes=10, active_channels={"active"}, now=old + 10)
            self.assertIn("stale", removed["expired_dirs"])
            self.assertFalse(stale.exists())
            self.assertTrue(active.exists())
            self.assertIn(str(f1), removed["quota_files"])
            self.assertFalse(f1.exists())

    def test_transcoder_state_exposes_reentrant_lock_for_concurrent_access(self):
        state = TranscoderState(idle_timeout=1)
        with state.lock:
            state.cleanup_dead()
            self.assertEqual(state.running_count(), 0)

    def test_state_stops_idle_process(self):
        class DummyProcess:
            pid = 999999  # 高位 PID，确保 os.getpgid 抛 ProcessLookupError，避免误杀真实进程（曾用 123 撞上内核线程 irq/46-aerdrv）
            killed = False
            def poll(self): return None
            def terminate(self): self.killed = True
            def wait(self, timeout=None): return 0
        state = TranscoderState(idle_timeout=1)
        proc = DummyProcess()
        state.processes["a"] = proc
        state.heartbeats["a"] = 1
        stopped = state.stop_idle(now=3)
        self.assertEqual(stopped, ["a"])
        self.assertTrue(proc.killed)
        self.assertNotIn("a", state.processes)


if __name__ == "__main__":
    unittest.main()
