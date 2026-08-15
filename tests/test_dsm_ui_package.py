import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackageOpenWebManagementTests(unittest.TestCase):
    def test_info_declares_package_center_open_web_management_port(self):
        info = (ROOT / "spk" / "INFO").read_text(encoding="utf-8")
        self.assertIn('version="0.1.0-055"', info)
        self.assertIn('adminprotocol="http"', info)
        self.assertIn('adminport="18097"', info)
        self.assertIn('adminurl=""', info)
        self.assertNotIn('dsmuidir=', info)
        self.assertNotIn('dsmappname=', info)

    def test_package_center_icons_exist(self):
        icon64 = ROOT / "spk" / "PACKAGE_ICON.PNG"
        icon256 = ROOT / "spk" / "PACKAGE_ICON_256.PNG"
        self.assertTrue(icon64.exists())
        self.assertTrue(icon256.exists())
        self.assertGreater(icon64.stat().st_size, 50)
        self.assertGreater(icon256.stat().st_size, 50)

    def test_build_script_outputs_current_version_and_does_not_package_dsm_3rdparty_ui(self):
        script = (ROOT / "tools" / "build_spk.sh").read_text(encoding="utf-8")
        self.assertIn('OUT="$BUILD/iptv-transcoder-0.1.0-055-x86_64.spk"', script)
        self.assertIn('cp "$ROOT/spk/PACKAGE_ICON.PNG" "$SPK_STAGE/PACKAGE_ICON.PNG"', script)
        self.assertIn('cp "$ROOT/spk/PACKAGE_ICON_256.PNG" "$SPK_STAGE/PACKAGE_ICON_256.PNG"', script)
        self.assertIn('find "$PKG_STAGE" -type f -exec chmod 644 {} +', script)
        self.assertIn('find "$SPK_STAGE" -type f -exec chmod 644 {} +', script)
        self.assertIn('find "$SPK_STAGE/conf" -type f -exec chmod 644 {} +', script)
        self.assertNotIn('cp -a "$ROOT/spk/ui" "$PKG_STAGE/ui"', script)

    def test_start_script_exports_media_library_path_for_jellyfin_ffmpeg(self):
        script = (ROOT / "spk" / "scripts" / "start-stop-status").read_text(encoding="utf-8")
        self.assertIn("setup_media_library_path", script)
        self.assertIn("LD_LIBRARY_PATH", script)
        self.assertIn("/var/packages/Jellyfin/target/lib", script)
        self.assertIn("libmpg123", script)
        self.assertIn("setup_media_runtime_env", script)
        self.assertIn("OCL_ICD_VENDORS", script)
        self.assertIn("OPENCL_VENDOR_PATH", script)
        self.assertIn("LIBVA_DRIVERS_PATH", script)

    def test_start_script_performs_hardware_preflight_for_noarch_package(self):
        info = (ROOT / "spk" / "INFO").read_text(encoding="utf-8")
        script = (ROOT / "spk" / "scripts" / "start-stop-status").read_text(encoding="utf-8")
        self.assertIn('arch="noarch"', info)
        self.assertIn("preflight_hardware", script)
        self.assertIn("IPTV_TRANSCODER_QSV_DEVICE", script)

    def test_package_run_as_privilege_file_exists_for_dsm_install(self):
        privilege = ROOT / "spk" / "conf" / "privilege"
        self.assertTrue(privilege.exists())
        text = privilege.read_text(encoding="utf-8")
        self.assertIn('"run-as": "package"', text)

    def test_legacy_dsm_ui_source_avoids_inner_html(self):
        html = (ROOT / "spk" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("innerHTML", html)
        self.assertIn("replaceChildren()", html)

    def test_scripts_prefer_appdata_var_dir_for_package_user(self):
        for rel in ["spk/scripts/postinst", "spk/scripts/start-stop-status"]:
            with self.subTest(rel=rel):
                script = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("resolve_var_dir", script)
                self.assertIn("/volume1/@appdata/${PKG}", script)
                self.assertIn("$PKG_DIR/var", script)

    def test_install_and_start_scripts_repair_appdata_owner_and_modes_for_package_user(self):
        for rel in ["spk/scripts/postinst", "spk/scripts/start-stop-status"]:
            with self.subTest(rel=rel):
                script = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("repair_runtime_permissions", script)
                self.assertIn('chown -R "$PKG_USER" "$VAR_DIR"', script)
                self.assertIn('chmod 700 "$VAR_DIR" "$LOG_DIR" "$RUN_DIR" "$HLS_DIR"', script)
                self.assertIn('chmod 600 "$ENV_FILE"', script)

    def test_install_and_start_scripts_repair_missing_api_key(self):
        for rel in ["spk/scripts/postinst", "spk/scripts/start-stop-status"]:
            with self.subTest(rel=rel):
                script = (ROOT / rel).read_text(encoding="utf-8")
        self.assertIn("ensure_api_key", script)
        self.assertIn("IPTV_TRANSCODER_API_KEY", script)
        self.assertIn("grep -q '^IPTV_TRANSCODER_API_KEY='", script)
        self.assertIn("api key missing or empty", script)

    def test_start_script_exports_api_key_to_python_service(self):
        script = (ROOT / "spk" / "scripts" / "start-stop-status").read_text(encoding="utf-8")
        self.assertIn('export IPTV_TRANSCODER_API_KEY="${IPTV_TRANSCODER_API_KEY:-}"', script)

    def test_start_script_removes_stale_pid_file_when_startup_check_fails(self):
        script = (ROOT / "spk" / "scripts" / "start-stop-status").read_text(encoding="utf-8")
        self.assertIn('rm -f "$PID_FILE"', script)
        self.assertIn('echo "$PKG failed to start; see $SERVICE_LOG"', script)

    def test_smoke_api_script_exists_and_checks_core_endpoints(self):
        script = (ROOT / "tools" / "smoke_api.sh").read_text(encoding="utf-8")
        self.assertIn("GET /api/health/details", script)
        self.assertIn("GET /api/config", script)
        self.assertIn("GET /api/status", script)
        self.assertIn("POST /api/probe", script)
        self.assertIn("--exercise-transcode", script)
        self.assertIn("/api/transcode/start", script)
        self.assertIn("/api/transcode/$CHANNEL_ID/heartbeat", script)
        self.assertIn("/api/transcode/$CHANNEL_ID/stop", script)
        self.assertIn("missing_playlist", script)
        self.assertIn("Heartbeat warmup state", script)
        self.assertIn("trap cleanup_started_channel EXIT", script)
        self.assertIn("STARTED_CHANNEL_ID", script)
        self.assertIn("python3 -c", script)
        self.assertNotIn("python3 - \"$field\" <<'PY'", script)

    def test_readme_links_to_web_smoke_runbook_for_end_to_end_acceptance(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("../iptv-web/tools/README.smoke.md", readme)
        self.assertIn("Web 侧的联动验收顺序", readme)


if __name__ == "__main__":
    unittest.main()
