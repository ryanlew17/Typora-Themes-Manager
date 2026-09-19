# Typora Themes Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[English](README.md) | [简体中文](README.zh-CN.md)**

A local, zero-dependency theme installer for [Typora](https://typora.io/). It manages any
GitHub-hosted Typora theme repository through a uniform pipeline:

**download latest release → light security scan → baseline manifest comparison →
selective install → report**

Everything runs locally with the Python standard library only — no `pip install`,
no frameworks, no bundled binaries.

### Highlights

- **Any GitHub theme repo** — register it with a URL; the installer infers the layout
  (theme CSS location, file-name pattern, font/asset directories) automatically.
- **Safety first**
  - Scripts inside the archive are reviewed line by line against a rule table
    (`BLOCK` / `WARN` / `INFO`); any `BLOCK` aborts the install before extraction.
  - Path traversal (zip-slip) and CRC integrity checks.
  - `.bat` / `.cmd` files are **never executed and never content-analyzed** (counted only).
  - The archive's file list is compared against a local **baseline manifest** — an
    unexpected structural change (renamed/added/removed files) cancels the install.
    Accepting a new manifest is always an explicit, human-confirmed action.
- **Selective install** — only theme CSS (into the themes directory root), asset
  directories and an optional bridge file are written. READMEs, install scripts and
  demo pages never enter the themes directory, so multiple repos coexist cleanly.
- **Precise uninstall / clean reinstall** — an install record (`installed.json`) drives
  removal; everything is backed up to `themes-backup-<id>-<timestamp>/` next to the
  themes directory before any file is touched.
- **Bilingual UI** — Simplified Chinese (`zh-CN`) and English (`en-US`), for both the
  web console and CLI output/reports. Switch in the page header, or use `--lang`.
- **Friendly to rate limits** — GitHub API calls use ETag conditional requests, and fall
  back to redirect-based tag resolution + asset-name probing when rate-limited.

### Requirements

- Python **3.8+** (standard library only)
- Windows / macOS / Linux

### Quick start

**Windows** — double-click `start-manager.bat`
(or run `python theme_manager.py` in a terminal).

**macOS** — double-click `start-manager.command`
(first time: if Gatekeeper blocks it, right-click → Open; or run
`bash start-manager.command` in a terminal).

**Linux** — run `bash start-manager.command`
(or make it executable, then double-click "Run in terminal" in your file manager).

The web console starts at `http://127.0.0.1:17888/` (bound to localhost only) and opens
your browser automatically. Paste a GitHub theme repository URL — for example
`https://github.com/sumruler/typora-theme-phycat` — and click **Add repo**.

### Command line

```bash
python theme_manager.py                        # start the web console (default)
python theme_manager.py list                   # list repos and install status (read-only)
python theme_manager.py check [ID ...]         # check latest versions (parallel, no install)
python theme_manager.py install ID             # install / update (writes the themes dir!)
python theme_manager.py install ID --mode clean       # clean reinstall (this repo only, backs up first)
python theme_manager.py install ID --update-manifest  # rebuild the baseline manifest after reviewing diffs
python theme_manager.py uninstall ID           # uninstall (backs up first)
python theme_manager.py add <URL>              # register a repo (restores local state if it was installed)
python theme_manager.py rescan [ID ...]        # refresh version cache + adopt existing installs (registry-only)
python theme_manager.py remove ID [--purge]    # unregister (--purge also deletes local data)
python theme_manager.py --lang en-US list      # run with English output (or set TTM_LANG)
```

Exit codes: `0` success · `1` download/query failure · `2` security scan failed ·
`3` install failed · `4` manifest mismatch.

### Where things live

| Path | Purpose |
|---|---|
| `repos.json` | Registry of managed repos (auto-created with sensible defaults) |
| `repos/<id>/zips/<tag>/` | Downloaded release archives (latest N kept, see `settings.keep_zips`) |
| `repos/<id>/manifest.json` | Baseline file list used for tamper detection |
| `repos/<id>/installed.json` | Install record driving uninstall / clean reinstall |
| `repos/<id>/latest.json` | Version-check cache (with ETag and fallback memory) |
| `repos/<id>/安装报告.md` | Latest install report (one per repo, rewritten every run) |

The real Typora themes directory is detected per platform
(`%APPDATA%\Typora\themes` on Windows,
`~/Library/Application Support/abnerworks.Typora/themes` on macOS,
`~/.config/Typora/themes` on Linux). Backups are created next to it and are never
deleted automatically.

### License

Released under the [MIT License](LICENSE).
