# X Downloader

[中文](README.md) | [English](README.en.md)

Save images and videos from your X bookmarks to your own computer, then download only new and failed items on later runs.

Bookmarks are easy to collect and tedious to organize. This tool is for people who already have a collection of media they want to revisit: it reads bookmarks through a signed-in browser, downloads their media in batches, and remembers progress. Files stay on your computer by default. NAS copying is optional.

**Currently intended for macOS. Menus and runtime messages are mainly in Chinese.** The tool requires `ego-browser` from ego lite; it cannot use a regular Chrome or Safari session directly. Normal operation needs no language model and no X developer API key.

## When to use it

- Keep tutorials, demonstrations, and reference images for offline viewing.
- Periodically collect new bookmarks without downloading the same files again.
- Download to a laptop first and copy files to an SMB NAS later.
- Run on your own Mac daily or every few days, inspect logs, and retry failures.

It saves media, not complete post text, and is not a full account backup. It cannot recover deleted content that was never successfully downloaded.

## What it does

| Need | Behavior |
| --- | --- |
| Multiple images or videos in one bookmark | Keeps the media found by the scraper; requests original images and selects video up to 1080p by default |
| Repeated runs | Skips completed items using local state and retains failures for retry |
| Interrupted downloads | Keeps completed files; yt-dlp resumes videos, while incomplete images are downloaded again |
| Review before downloading | Collects a preview list for confirmation |
| Run without an open terminal | Supports background tasks and macOS launchd scheduling |
| Multiple X accounts | Checks the active account when collecting; separates failure queues and can reuse media records across accounts |
| Optional NAS | Copies into monthly folders and marks delivery only after SHA-256 verification; keeps local copies |
| Optional phone alerts | Sends failed-run notifications through Bark, disabled by default |

Newly processed bookmarks receive increasing filename prefixes, author handles, and post identifiers. Existing files keep their names. Progress distinguishes bookmarks from media files.

## Install and start

Requirements:

- macOS and Python 3.9 or newer.
- [ego lite](https://lite.ego.app/), with onboarding completed, `ego-browser` available in your terminal, and your X account signed in manually in that browser.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [FFmpeg](https://ffmpeg.org/). With Homebrew, run `brew install python yt-dlp ffmpeg`.

You handle login and CAPTCHAs yourself. The program uses the active browser session and does not ask you to export a Cookie file.

```bash
git clone https://github.com/amorelove99/x-downloader.git
cd x-downloader
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/xdl doctor
.venv/bin/xdl menu
```

Alternatively, double-click `启动 X Download.command` in Finder. On first launch, choose `1` to create a project virtual environment and install Python dependencies. The launcher does not install ego lite, yt-dlp, FFmpeg, or Homebrew.

Choose menu item `1` to download new bookmarks. Defaults: up to 40 recent bookmarks, images and videos, saved to `downloads/` inside the project. A NAS is not required. Menu `8` changes the count, media type, video quality, and local destination.

## Common commands

Run these from the cloned project directory:

```bash
# Collect and review a list without downloading media
.venv/bin/xdl preview

# Process media from up to 200 recent bookmarks
.venv/bin/xdl run --max 200

# Download images only to a local directory
.venv/bin/xdl run --media images --out "$HOME/Downloads/X-media"

# Start in the background, inspect progress and logs, then stop safely
.venv/bin/xdl run --background
.venv/bin/xdl status --watch
.venv/bin/xdl logs --follow
.venv/bin/xdl stop

# Retry download failures for the current account
.venv/bin/xdl retry --download-only
```

`run --dry-run` collects and simulates processing without downloading, delivering, or updating business completion state. `preview` saves a local preview list. Both can access X.

`run --skip-scrape` reuses the last confirmed account's saved list without checking whether the browser account changed. A normal run checks the current account again. Changing media mode lets later runs fill in missing images or videos from the same bookmark.

The legacy entry point `python3 -I xdl.py --max 40` delegates to the current download command. See `.venv/bin/xdl --help` and `.venv/bin/xdl run --help` for all options.

## Optional: copy to a NAS

**Skip this section if you do not use a NAS.** This feature supports mounted SMB shares on macOS. It does not mount shares or store SMB passwords.

1. Mount your SMB share in Finder.
2. Copy the example to a private local configuration and edit its mount point:

   ```bash
   mkdir -p data
   cp examples/nas.example.json data/nas.json
   ```

   ```json
   {
     "mount_root": "/Volumes/MyMediaShare",
     "delivery_folder": "Backups"
   }
   ```

   `mount_root` must be the absolute SMB share mount point. `delivery_folder` is relative to the share and cannot contain `..`.

3. Run once with NAS delivery:

   ```bash
   .venv/bin/xdl run --delivery nas
   ```

   To persist this choice, use menu `8`, then `7`, and select NAS. Command-line options apply only to that invocation.

Files go to `<mount_root>/<delivery_folder>/X download/YYYY-MM/`, using the local file modification month. Copies are SHA-256 verified. An identical existing file can be reused; a different file with the same name causes an error and is not overwritten. When the share is unavailable, local files are retained. Retry delivery later with:

```bash
.venv/bin/xdl retry --delivery-only
```

This explicitly requests NAS delivery even when the saved preference is local. A copy with no progress for 120 seconds stops waiting and leaves pending files available for retry. Locked-screen behavior and actual blocked SMB calls have not been fully tested; timely exit from every system call is not guaranteed.

Existing `~/.config/nas-delivery.json` configurations remain supported. Project-local `data/nas.json` takes precedence. Normal downloads and current NAS copying do not require the `nas-deliver` command or any AI skill.

## Optional: scheduling and failure alerts

Scheduling starts disabled. Complete a manual run before enabling it:

```bash
.venv/bin/xdl schedule enable --time 09:30 --every-days 3
.venv/bin/xdl schedule status
.venv/bin/xdl schedule disable
```

The schedule uses your Mac's time zone and the last successful date for the interval. Overlapping jobs are skipped. It cannot run while the Mac sleeps or the user is logged out; locked-screen operation remains unverified. The configuration contains absolute project and virtual environment paths, so reconfigure after moving the project.

Bark alerts start disabled. Enter a device key through menu `8`, then `8`, or use:

```bash
.venv/bin/xdl notify set --key YOUR_DEVICE_KEY
.venv/bin/xdl notify test
.venv/bin/xdl notify off
```

Prefer entering the key through the menu to avoid shell history. Add `notify set --server https://your-bark-server.example` for a self-hosted server. The key is stored only locally in `data/bark.json` with mode `600`. Once enabled, failure summaries are sent to that server and may include account handles, filenames, or errors. Manual interruption does not send an alert. Falling back to local storage when a NAS is unavailable is not treated as failure.

## Local data and privacy

- `downloads/` contains media and is retained after NAS copying.
- `data/` contains account identifiers, bookmark snapshots, state, preferences, notification settings, and logs. Logs may contain post URLs and local paths.
- `data/state.json` and `data/archive.txt` work together for deduplication and recovery. Back them up together; do not delete just one to force a download.
- `data/runs/` and `data/task-output/` each retain the latest 30 records. The cumulative `runs.log` is not automatically rotated.

A source checkout stores data inside the project. A regular non-editable installation defaults to `~/.local/share/xdownload/`. Set `XDL_HOME` before starting to choose a separate data root containing `data/` and the default `downloads/`. Command-line options override saved preferences, which override defaults.

A Git allowlist excludes local state, downloads, build caches, personal launchd files, and internal development notes. Inspect staged files before publication as described in [Contributing](CONTRIBUTING.md). These guards do not replace manual review. Download only content you have permission to save and follow the applicable service rules.

## Limits and verification

- Collection depends on X's internal web GraphQL API and page structure and can break when X changes. This is not an official X product.
- A collection makes at most 200 page requests and stops after 20 consecutive processed bookmarks. Increasing `--max` does not guarantee a complete historical export.
- Deleted, inaccessible, restricted, or unusual card media may not download. NSFW, long-video, and deleted-post edge cases have not been systematically tested.
- Collection times out after 300 seconds; each yt-dlp call is limited to 900 seconds. Login expiry, CAPTCHAs, and rate limits need manual attention.
- Linux, Windows, and an English interactive interface are not supported yet. Tool versions, browser state, and network conditions affect results.
- Offline tests use synthetic data without real X accounts or a NAS. Passing tests does not establish live download or locked-screen behavior.

```bash
.venv/bin/python -m unittest discover -v
```

Exit codes: `0` success, `1` partial download or delivery failure, `2` argument or configuration error, `3` another task is running, `4` login/environment/runtime conditions not met, `130` user interruption.

## Open source

[MIT licensed](LICENSE). Reproducible bug reports and improvements are welcome. Remove account details, Cookies, device keys, and private paths before posting; see [Security](SECURITY.md).
