<p align="center"><img src="assets/icon.png" width="128" alt="Anotherr"></p>

# Anotherr

**Another \*arr.** It checks your Plex library for bad media files and fixes them through Sonarr, Radarr and Bazarr.

Anotherr runs on your Unraid server. It mirrors your Sonarr/Radarr libraries and works through them on a schedule, sampling random parts of each episode or movie. It flags files that are broken, in the wrong language, or have bad subtitles. Speech recognition runs locally on your NVIDIA GPU with faster-whisper. Claude Desktop (through MCP) is only used for the checks that need eyes, like reading title cards.

## What it checks

| Area | Checks |
|---|---|
| **Integrity** | ffprobe sanity, sampled-segment decode errors, truncated files (the last segment is always decoded), container vs. stream duration, optional full decode (deep profile) |
| **Runtime** | File length vs. two opinions: the per-episode length **learned from the same season's files** (or the whole series when a season has fewer than 5), and Sonarr/TVDB's per-episode runtime. A file is only flagged when **both** say it's off in the same direction, so segment-numbered shows and seasons with shorter episodes pass. Flags too short (wrong file, sample), much too long, and double-length single episodes (season finales are info only) |
| **Video** | Black video, interlacing, and frozen video: keyframes nominate candidates, which are only reported after a continuous decode confirms the picture really stopped (≥10 s, black and the first/last 2 min excluded). One spot = info, two separate spots = warning |
| **Audio language** | Whisper language detection on each audio track, run separately on several windows across the file (tags are often wrong or `und`). A tag is only overridden when ≥3 windows agree at ≥90% on average. The **original language is always preferred** unless you set a per-show/movie override. It also flags mislabeled tracks, a default track that isn't the preferred language, and silent tracks |
| **Anime** | Original audio required, **dual audio preferred** (flags a missing dub as info), full subtitles required (not signs/songs only), **"dubtitles"** detection (subs that just caption the English dub) |
| **Subtitles** | Missing, signs/songs only, **out of sync** (speech vs. cue timing against the *original-language* track, reports the offset), **mismatch** (timing or text doesn't match). Text is compared after shifting by the detected offset, so each track gets one verdict. Image-only (PGS) info. Skipped for films with no dialogue. External `.srt`/`.ass` files from Bazarr are included and re-checked hourly |
| **Multi-episode files** | SpongeBob / Courage-style files: segment breaks found with black-frame + silence detection, then title-card frames queued for a visual check against the expected episode titles |
| **Wrong content** | Title cards are read automatically (OCR) and matched against the series' episode titles: a match passes, another episode's title raises **wrong content**. Subtitles failing *plus* another anomaly (length, every track failing) is also reported as wrong content, with a redownload proposed rather than a resub. Once a file is truncated or wrong, checks that can't be valid on it are skipped and listed as such |
| **Visual (Claude or you)** | Title cards OCR couldn't read, movie identity when language or runtime is off, and in deep scans hardcoded subs, watermarks, cam rips, or a wrong aspect ratio |

### Scan profiles

| Profile | What it does | Typical time |
|---|---|---|
| `quick` | 3 × 30 s segments (start / random / end), decode, language ID, subtitle presence from stream flags | seconds |
| `standard` | 5 × 90 s segments, plus full subtitle analysis, sync, dubtitle/content checks, multi-episode breaks | ~20–60 s with GPU decode |
| `deep` | 8 × 3 min segments, plus a full decode and a visual-quality review | several min |

Every scan stores its random seed, so results are reproducible.

## Scheduling (Unraid-aware)

Scanning runs **around the clock** in slots:

| Slot | Takes, in order |
|---|---|
| **Cache/SSD** (one extra slot, on by default) | queued files on cache/pools, then scheduled files there. No spin-up and no playback contention, and these are mostly fresh downloads waiting for the mover |
| **Hard drives** (`max_disks`, default 1, each slot on a different disk) | queued files on a disk that's already spinning → scheduled files on spinning disks (staying on the same disk while it has work) → queued files on an idle disk → wakes the next idle disk in rotation |

- **Spin up idle disks:** `anytime` (default), only inside the spin-up `windows`, or `never`. Queued scans you asked for may always wake a disk.
- **Quiet windows:** no new scans start, e.g. 02:30–04:30 while scheduled updates or backups run. Running scans finish first.
- **Pauses:** manual pause, parity check, and mover. Plex streams don't pause scanning by default: a stream reads a few MB/s, and scans run at the lowest disk priority. `stream_pause` can be set to `same_disk` (skip the disk Plex is reading) or `any`.
- The dashboard shows each slot's disk and why it was picked, and when the next scan will run.
- Files are read straight from `/mnt/diskN` (bypassing shfs) once their disk is known.
- **Queued scans** use the same slots and pauses. **Process queue now** (Queue page / dashboard, or the `process_queue` MCP tool) works through the whole queue, ignoring pauses.
- **Priority**: never-checked files first, then recent imports, files whose release group has a bad track record, files with earlier problems, and the longest time since the last check.
- **New imports** get a quick scan straight away: through Sonarr/Radarr webhooks, a history check every 15 minutes, or the next library sync, so webhooks are optional.
- The Whisper model is unloaded after it sits idle, so Plex gets the VRAM back.
- **Memory:** frames for reviews are rendered during the scan and cached in appdata (capped at 1 GB). At most 2 live frame extractions run at once, so a page full of frames can't start dozens of ffmpeg processes. The dashboard status reports memory use, and if the container is ever killed (e.g. out of memory), the next start logs what happened.

## Retagging audio languages (optional, off by default)

Many releases ship with untagged or wrongly tagged audio tracks (e.g. a Korean show tagged English). Plex,
Sonarr and Bazarr all go by the tag. For example, Bazarr's Whisper provider only translates when it knows
the audio language. When language detection is unanimous, Anotherr proposes a **Retag audio language**
action that writes the detected language into the file.

- **Untagged tracks:** ≥2 sampled windows, all agreeing, ≥0.90 average confidence. A 1/1 or 2/3 result
  qualifies only if ≥2 other files of the same release agree. A language that differs from the series'
  language goes to a separate *Manual review* batch.
- **Wrong tags:** ≥3 windows, all agreeing, ≥0.95, the detected language must be the series' own
  language, and ≥2 sibling files must agree. These are never automatic.
- **Excluded:** files with wrong-content, truncation or length issues; no-dialogue files; commentary,
  forced and hearing-impaired tracks; anything whose issue you've ignored.
- **One proposal per series and season,** with a dry-run preview of each file's command. Approving runs
  it; **Undo** restores the old tags from the rollback log (`/config/retag_log.jsonl`).
- **Editing:** MKV/WebM are edited in place with `mkvpropedit` (header only). Other formats are
  stream-copied with ffmpeg and atomically replace the original, keeping owner and mode. Before each
  edit Anotherr checks the file is unchanged since the scan, writable, under an allowed path and not
  playing in Plex. Afterwards it verifies with ffprobe (same streams, same duration, new tag reads back).
- **Hardlinked files** (seeding copies) are skipped unless `retag.break_hardlinks` is on, in which case
  they get a new copy.
- **Afterwards:** Anotherr asks Sonarr/Radarr to rescan and Bazarr to scan the series from disk, and
  rescans each file to read the tag back.

To enable without giving Anotherr write access to all of `/mnt`:

1. Keep the **Unraid /mnt** path **Read Only - Slave**.
2. Add a **Path** for each media folder you want editable, with the *same* host and container path,
   e.g. host `/mnt/user/media/tv` → container `/mnt/user/media/tv`, access **Read/Write - Slave**.
   Docker mounts it on top of the read-only `/mnt`, so only those folders are writable.
3. In **Settings → Retag audio**, turn it on and list the same folders as allowed paths.

Anotherr reads from the physical disk paths but writes through these share paths. Start with one small
batch and check the result in Sonarr, Bazarr and Plex.

## Default audio and subtitle tracks

Uses the same switch, allowed paths and safety checks as retagging (Settings → Retag audio).

- **Set default audio** (from `default_audio_not_preferred`): makes the track in your preferred language
  (the series override, else its original language) the default. That track must have been analysed,
  with every window agreeing and a confident result. Commentary and description tracks are skipped. If
  several tracks qualify, the one with the most channels wins, then the lowest index. A track is never
  promoted on its tag alone. Without such a track, the issue says a flag change can't fix it.
- **Set default subtitle** (from `default_subs_signs`): this is judged against the default audio *after*
  any audio fix.
  - If that audio is in your subtitle language, nothing changes (no subtitle, or the forced signs track,
    is right).
  - Otherwise the full embedded track in your subtitle language becomes the default. Text beats image,
    non-SDH beats SDH, then the lowest index wins.
  - External subtitle files can't carry flags, so they only get a note.
- A file that needs both gets **one** proposal and one header write. Proposals are grouped by series,
  season and identical change, show a dry-run preview, and are always approved by hand.
- **Executing:** the target track gets the default flag and every other track of the same type loses it.
  Forced flags are never touched. MKV is edited in place. MP4 is stream-copied with dispositions,
  verified to read back. The full flag snapshot goes to the rollback log, so **Undo** restores exactly
  what was there. A rescan reads the change back and closes the issue. Only Sonarr/Radarr are refreshed
  (Bazarr isn't involved).
- Plex can remember a per-user track choice that overrides the file's default flags.

**Reliability:** every track-edit action ends *done* or *failed*.
- Each file has a 60 s limit (a stuck file is skipped and the batch goes on).
- Progress shows as "n of N files done", with one activity-log line per file.
- An action with no progress for 10 minutes is marked *failed (stalled)* and its lock is released.
- Crashes are written to the activity log.

## Actions and automation

Problems create **proposals**:
- **Resub**: asks Bazarr to search for new subtitles. Every open issue has a **Propose resub** button.
  After a search fails, automatic resubs for that series pause. They re-arm after 7 days, after the
  file's audio is retagged (Bazarr picks subtitles by the audio tag), or after a Whisper subtitle fetch.
- **Whisper subtitles** (manual): asks Bazarr's Whisper provider to transcribe and translate one
  episode. The Whisper provider must be enabled in Bazarr, and the audio tag must be known and not your
  subtitle language. It can take several minutes.
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

**Tools:** `status`, `coverage`, `recent_activity`, `search_library`, `item_files`, `file_report`, `set_language_override`, `process_queue`, `queue_scan`, `list_issues`, `issue_evidence` (the measurements and frames behind an issue), `file_streams` (every track in a file), `ignore_issue`, `pending_reviews`, `get_review` (returns frames), `get_frames`, `submit_review`, `list_actions`, `propose_action`, `decide_action`.
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
