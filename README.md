# Typora Themes Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**[English](README.md) | [简体中文](README.zh-CN.md)**

A local, zero-dependency installer for [Typora](https://typora.io/) themes from any
GitHub repository: **download latest release → security scan → baseline manifest
comparison → selective install → report**. Python standard library only.

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
python theme_manager.py remove ID       # unregister
```

Run `--help` for all options (`--mode clean`, `--update-manifest`,
`--allow-remote-host`, `--lang`, …). Exit codes: `0` success · `1` download/query
failure · `2` security scan failed · `3` install failed · `4` manifest mismatch ·
`5` paused: remote hosts awaiting confirmation (nothing was written).

### Safety

- Scripts inside the archive are reviewed before extraction; anything suspicious
  aborts the install. `.bat` / `.cmd` files are never executed.
- If a theme's CSS contacts remote hosts, the install pauses until you review and
  approve the host list (using such a theme exposes your IP and usage times).
- The archive's file list is compared against a local baseline; unexpected
  changes cancel the install until you confirm them.
- Only theme CSS and assets are written; every removal is backed up first and
  never touches other repos' themes.

### License

Released under the [MIT License](LICENSE).
