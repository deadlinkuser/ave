# AVE — Automatic Video Encoder

AVE is a self-hosted, rootless-container video transcoding queue with a lightweight Web GUI. Drop single files or nested show folders into `watch/`, click **Start Now**, and AVE converts them into predictable, archival-friendly outputs in `output/` while safely archiving verified originals.

## Key Features

* **Zero-Config Boot:** Starts in an `IDLE` state with a ready-to-use `HEVC Balanced` preset (`MKV`, max `1080p` without upscaling, `10-bit x265`, `CRF 22`, `fast`, `AAC`, `ENG` audio/subtitles).
* **Deterministic Queue & Folder Affinity:** Processes files in natural human-numeric order (`Episode 2` before `Episode 10`) and finishes an active show folder completely before moving on to newly added folders.
* **Optional Hardware Acceleration (VAAPI):** Supports `/dev/dri` GPU pass-through (`hevc_vaapi` / `h264_vaapi`) with automatic fallback to CPU encoding if a file or GPU does not support a requested format. *(Intel 8th-Gen or newer is recommended for 10-bit HEVC; AMD VCN quality varies by generation.)*
* **Built-In Safety & QA:**
  * Verifies file stability before reading so active file transfers are never cut short.
  * Validates encoded outputs (`> 100 KB` and duration match within `±2.0s`) before moving originals to `watch/_originals/`.
  * Quarantines corrupt or unreadable sources to `watch/_failed/` alongside a plain-English `.error.txt` report.
  * Automatically backs up custom presets (`custom_presets.json.bak`) and recovers from JSON corruption on startup.
* **Courtesy Throttling & Smart RAM Scratch:** Yields CPU (`nice`) and disk I/O (`ionice`) automatically when external host workloads spike, restores full speed once load clears, and optionally encodes in `/dev/shm` when safe memory headroom exists.
* **Optional LAN Login Guard:** Binds to `127.0.0.1` by default, or exposes on your LAN with `AVE_WEB_PASSWORD` enabled to require a session login prompt.

---

## Quick Start & Lifecycle Commands

AVE works with both rootless [Podman](https://podman.io) (recommended, using systemd Quadlet) and Docker.

```bash
mkdir -p ~/DEV/github-repos
git clone https://github.com ~/DEV/github-repos/ave
cd ~/DEV/github-repos/ave
./install.sh
```

### Lifecycle Management
```bash
./install.sh --install     # Interactive setup (data path, port, LAN password, GPU, boot auto-start)
./install.sh --upgrade     # Backs up config/, pulls latest updates, and rebuilds the container
./install.sh --backup      # Saves a lightweight config/ archive to ~/backups/ave/
./install.sh --uninstall   # Removes container/service while keeping ~/containers/ave data safe
```

Once running, open **`http://127.0.0.1:8488`** in your browser.

---

## Directory Layout

* **Runtime Data (`~/containers/ave`):** Contains `watch/`, `watch/_originals/`, `watch/_failed/`, `output/`, and `config/`.
* **Config Backups (`~/backups/ave`):** Stores lightweight `.tar.gz` snapshots of your presets and settings.

> **Storage Best Practice (Local Disk vs. NAS / ZFS):**
> Copy media onto local storage (`~/containers/ave/watch`) rather than mounting remote SMB/NFS network shares directly. Keeping `watch/` and `output/` on the same local filesystem ensures atomic file moves and avoids network drop failures. If your host uses **ZFS** with automated snapshots, exclude `~/containers/ave` from frequent snapshot schedules so temporary `.part` files and archived `_originals` do not bloat snapshot blocks.

---

## Companion Projects

* **[easy-jellyfin-setup](https://github.com):** Rootless container installer and backup utility for Jellyfin Media Server—pair it with AVE to keep your media library compact, standardized, and direct-play friendly.

---

## Feedback & Bug Reports

Feedback, feature ideas, and bug reports are always welcome!
* See **[REPORTING_BUGS.txt](REPORTING_BUGS.txt)** for step-by-step instructions on exporting logs.
* Submit feedback or issues anytime at **[GitHub Issues](https://github.com)**.

## License

Licensed under the **GNU Affero General Public License v3.0 (AGPLv3)**. See [LICENSE](LICENSE) for full terms.
