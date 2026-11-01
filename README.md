<p align="center"><img src="assets/icon.png" width="128" alt="Anotherr"></p>

# Anotherr

**Another \*arr.** It checks your Plex library for bad media files and fixes them through Sonarr, Radarr and Bazarr.

Anotherr runs on your Unraid server. It mirrors your Sonarr/Radarr libraries and works through them on a schedule, sampling random parts of each episode or movie. It flags files that are broken, in the wrong language, or have bad subtitles. Speech recognition runs locally on your NVIDIA GPU with faster-whisper. Claude Desktop (through MCP) is only used for the checks that need eyes, like reading title cards.

## What it checks

| Area | Checks |
|---|---|
| **Integrity** | ffprobe sanity, sampled-segment decode errors, truncated files (the last segment is always decoded), container vs. stream duration, optional full decode (deep profile) |
| **Runtime** | File length vs. the Sonarr/Radarr runtime: too short (wrong file, sample), too long, or a likely multi-episode file mapped to one episode |
| **Video** | Black video, frozen video, interlacing |
| **Audio language** | Whisper language detection on each audio track (tags are often wrong or `und`). The **original language is always preferred** unless you set a per-show/movie override. It also flags mislabeled tracks, a default track that isn't the preferred language, and silent tracks |
| **Anime** | Original audio required, **dual audio preferred** (flags a missing dub as info), full subtitles required (not signs/songs only), **"dubtitles"** detection (subs that just caption the English dub) |
| **Subtitles** | Missing, signs/songs only, **out of sync** (VAD speech vs. cue timing, reports the offset), **wrong subs entirely** (timing doesn't match the dialogue), text doesn't match speech (native-language content), image-only (PGS) warning. External `.srt`/`.ass` files from Bazarr are included |
| **Multi-episode files** | SpongeBob / Courage-style files: segment breaks found with black-frame + silence detection, then title-card frames queued for a visual check against the expected episode titles |
| **Visual (Claude or you)** | Title cards match the episode, movie identity when the length is suspicious, and in deep scans hardcoded subs, watermarks, cam rips, or a wrong aspect ratio |

### Scan profiles

| Profile | What it does | Typical time |
|---|---|---|
| `quick` | 3 × 30 s segments (start / random / end), decode, language ID, subtitle presence from stream flags | seconds |
| `standard` | 5 × 90 s segments, plus full subtitle analysis, sync, dubtitle/content checks, multi-episode breaks | ~20–60 s with GPU decode |
| `deep` | 8 × 3 min segments, plus a full decode and a visual-quality review | several min |

Every scan stores its random seed, so results are reproducible.

## Scheduling (Unraid-aware)

- **Scan windows** per weekday (overnight windows work, e.g. 23:00–06:00), with a cap on files per window.
- **Disk modes**:
  - `all`: the whole array, by priority
  - `rotate`: one disk per window, round-robin
  - `spun_up`: only disks that are already spinning (from emhttp's `disks.ini`)
  - `specific`: only the disks you pick
- Files are read straight from `/mnt/diskN` (bypassing shfs) once their disk is known.
- **Queued scans run any time** (windows don't apply). They wait during parity checks, Plex streams or a manual pause; **Process queue now** (Queue page / dashboard, or the `process_queue` MCP tool) works through the whole queue ignoring those pauses.
- **Pauses** during parity checks and while Plex has active streams (via Tautulli).
- **Priority**: never-checked files first, then recent imports, files whose release group has a bad track record, files with earlier problems, and the longest time since the last check.
- **New imports** get a quick scan straight away through Sonarr/Radarr webhooks.
- The Whisper model is unloaded after it sits idle, so Plex gets the VRAM back.

## Actions and automation

Problems create **proposals**:
- **Resub**: asks Bazarr to search for new subtitles.
- **Redownload**: marks the grab as failed in Sonarr/Radarr (blocklists the release), optionally deletes the bad file, then triggers a search.

**Automation is off by default.** Turn it on in Settings → Automation. It runs only inside its own schedule windows, with a daily cap and a list of issue types allowed to trigger a redownload. Until then, every proposal waits for your approval on the Actions page (or from Claude).

## Install on Unraid

1. Install the **Nvidia Driver** plugin (Community Apps) and copy your GPU's UUID.
2. Download the template from the Unraid terminal:
   ```bash
   wget -O /boot/config/plugins/dockerMan/templates-user/my-anotherr.xml https://raw.githubusercontent.com/adman234/anotherr/main/unraid/anotherr.xml
   ```
3. Go to **Docker → Add Container** and pick **anotherr** from the Template dropdown. Set `NVIDIA_VISIBLE_DEVICES` to your GPU UUID (or leave `all`). Keep `NVIDIA_DRIVER_CAPABILITIES` = `compute,utility,video` (`video` enables GPU video decoding, which makes scans several times faster). Then click Apply.
4. Open the web UI at `http://<unraid-ip>:9797`, then go to **Settings**:
   - Sonarr / Radarr / Bazarr / Tautulli: URL and API key (use **Test**).
   - **Path mappings**: how Sonarr/Radarr paths map into the container. If Sonarr sees `/tv` and that's `/mnt/user/media/tv` on Unraid, map `/tv` → `/mnt/user/media/tv`. The container sees Unraid's `/mnt` read-only. Click **Check paths** to test a sample of files: it suggests the right mapping and adds it with one click.
   - Whisper → **Load & test model**. The first run downloads `large-v3-turbo` (~1.6 GB) into appdata.
   - Click **Sync now** on the dashboard.
5. Optional, for instant scans of new downloads: in Sonarr and Radarr, add **Settings → Connect → Webhook** with "On Import" and "On Upgrade", URL `http://<unraid-ip>:9797/api/webhook/sonarr` (or `/radarr`). If you set an API key, append `?apikey=<key>`.

> The image is built by GitHub Actions and published to `ghcr.io/adman234/anotherr`. After the first build, check that the package is **public** (GitHub → your profile → Packages → anotherr → Package settings), or Unraid can't pull it.

### RTX 50-series (Blackwell) note

The image uses CUDA 12.8 + cuDNN 9, which RTX 50-series cards (e.g. 5060 Ti) need. If CUDA fails to load, Anotherr falls back to the CPU with a smaller model. The dashboard shows which device is in use and the error.

## Claude Desktop (MCP)

The MCP server is a thin local bridge. Claude never touches your files directly. Everything runs on the server, and Claude gets small JPEG frames only when a visual check is needed. Those frames count against your Claude plan's usage, which is why only flagged items are sent.

1. Install [uv](https://docs.astral.sh/uv/) on the machine running Claude Desktop.
2. Download the bridge from `http://<unraid-ip>:9797/mcp/anotherr_mcp.py` (or [`mcp/anotherr_mcp.py`](mcp/anotherr_mcp.py) in this repo).
3. Add it to `claude_desktop_config.json`:
   ```json
   {
     "mcpServers": {
       "anotherr": {
         "command": "uv",
         "args": ["run", "--script", "/path/to/anotherr_mcp.py"],
         "env": { "ANOTHERR_URL": "http://192.168.1.10:9797", "ANOTHERR_API_KEY": "" }
       }
     }
   }
   ```
4. Restart Claude Desktop and try:
   - "Review the pending Anotherr items"
   - "What did Anotherr find last night?"
   - "Scan 20 random anime episodes on disk3 with the standard profile"
   - "Show me frames from file 123 at 1:00, 5:00 and 10:00"

**Tools:** `status`, `coverage`, `recent_activity`, `search_library`, `item_files`, `file_report`, `set_language_override`, `process_queue`, `queue_scan`, `list_issues`, `ignore_issue`, `pending_reviews`, `get_review` (returns frames), `get_frames`, `submit_review`, `list_actions`, `propose_action`, `decide_action`.
**Prompts:** `review_pending`, `nightly_summary`.

## API

Everything the UI does goes through a REST API at `/api/*`. When `ANOTHERR_API_KEY` is set, send it as an `X-Api-Key` header or `?apikey=`. Main endpoints:
- `GET /api/status`, `GET /api/coverage`
- `GET /api/items`, `PATCH /api/items/{id}` (`language_override`, `is_anime`, `multi_segment`, `excluded`)
- `GET /api/files/{id}`, `GET /api/files/{id}/frame?t=`
- `POST /api/scan` with `{scope: file|item|disk|library|random|problems|unscanned, ids, disk, library, count, profile}`
- `GET /api/issues`, `GET /api/reviews`, `POST /api/reviews/{id}/verdict`
- `GET /api/actions`, `POST /api/actions/{id}/approve|reject`
- `GET|PUT /api/settings`
- `POST /api/webhook/{sonarr|radarr}`

## Development

```bash
pip install -r requirements.txt pytest
pytest -q
ANOTHERR_CONFIG=./config python -m anotherr   # http://localhost:9797
```

Layout:
- `anotherr/scanner.py`: the check pipeline
- `anotherr/analysis.py`: pure algorithms (subtitle sync, signs detection, dubtitles)
- `anotherr/scheduler.py`: windows, disk rotation, pauses, priority
- `anotherr/actions.py`: Bazarr / Sonarr / Radarr fixes and automation
- `anotherr/web/`: the UI (vanilla JS, no build step)
- `mcp/anotherr_mcp.py`: the Claude Desktop bridge
- `unraid/anotherr.xml`: the Docker template
