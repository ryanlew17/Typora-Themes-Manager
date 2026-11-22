# Typora Themes Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[English](README.md) | [简体中文](README.zh-CN.md)**

A local, zero-dependency installer for [Typora](https://typora.io/) themes from any
GitHub repository: **fetch package → security scan → baseline manifest comparison →
selective install → report**. Python standard library only.

The install package is resolved through an automatic fallback chain, so
repositories without zipped releases also work:

1. **Release archive** — a zip or tar-family asset of the latest release
   (7z/rar cannot be unpacked with the standard library and are skipped with
   a notice);
2. **Release loose files** — css/font files attached to a release directly;
3. **Source tree** — the repository's default branch, for repositories with no
   usable release assets; only install-relevant files are downloaded;
4. **Manual match** — you pick local theme files (web console file picker or
   the `match` command); they are aligned by content hash against all releases
   (newest first) and the source tree, so files from any historical version
   also match.

A manually matched repository is managed by its recorded structure: updates
overwrite only the files you have not customized, missing files are restored,
and uninstall / clean reinstall scope strictly to the recorded files. Update
checks compare the recorded hashes against the repository side — never the
local files — so your customizations are never flagged as outdated. Files
added upstream are not adopted automatically; re-run a manual match to
register them.

### Quick start

Requires Python **3.8+** (Windows / macOS / Linux).

- **Windows** — double-click `start-manager.bat`
- **macOS** — double-click `start-manager.command` (if Gatekeeper blocks it: right-click → Open)
- **Linux** — run `bash start-manager.command`

The web console starts at `http://127.0.0.1:17888/` and opens your browser.
Paste a theme repository URL — e.g. `https://github.com/sumruler/typora-theme-phycat` —
and click **Add repo**. The console and CLI are bilingual (English / 简体中文).

### Command line

```bash
python theme_manager.py                 # start the web console (default)
python theme_manager.py list            # list repos and install status (read-only)
python theme_manager.py check [ID ...]  # check latest versions (no install)
python theme_manager.py install ID      # install / update
python theme_manager.py uninstall ID    # uninstall (backs up first)
python theme_manager.py add <URL>       # register a repo
python theme_manager.py match ID FILE... # manually match local files by hash
python theme_manager.py remove ID       # unregister
```

Run `--help` for all options (`--mode clean`, `--update-manifest`,
`--allow-remote-host`, `--lang`, …). Exit codes: `0` success · `1` download/query
failure · `2` security scan failed · `3` install failed · `4` manifest mismatch ·
`5` paused: remote hosts awaiting confirmation (nothing was written).

### Safety

- Scripts inside the package are reviewed line by line before extraction;
  anything suspicious aborts the install. `.bat` / `.cmd` files are never
  executed.
- If a theme's CSS contacts remote hosts, the install pauses until you review
  and approve the host list (using such a theme exposes your IP and usage
  times).
- The package's file list is compared against a local baseline; unexpected
  changes cancel the install until you confirm them.
- Only theme CSS and assets are written; every removal is backed up first and
  never touches other repos' themes.
- Manual match writes records only (structure, hashes, timestamps) under the
  repo's own data directory — the themes directory is never touched.

### License

Released under the [MIT License](LICENSE).
