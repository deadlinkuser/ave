# AVE — Automatic Video Encoder

AVE is a self-hosted, rootless-container video transcoding queue with a lightweight Web GUI. Drop single files or nested show folders into `watch/`, click **Start Now**, and AVE converts them into predictable, archival-friendly outputs in `output/` while safely archiving verified originals.

## Key Features

* **Zero-Config Boot:** Starts in an `IDLE` state with a ready-to-use `HEVC Balanced` preset (`MKV`, max `1080p` without upscaling, `10-bit x265`, `CRF 22`, `fast`, `AAC`, `ENG` audio/subtitles).
* **Deterministic Queue & Folder Affinity:** Processes files in natural human-numeric order (`Episode 2` before `Episode 10`) and finishes an active show folder completely before moving on to newly added folders.
* **Built-In Safety & QA:**
  * Verifies file stability before reading so active file transfers are never cut short.
  * Validates encoded outputs (`> 100 KB` and duration match within `±2.0s`) before moving originals to `watch/_originals/`.
  * Quarantines corrupt or unreadable sources to `watch/_failed/` alongside a plain-English `.error.txt` report.
  * Automatically backs up custom presets (`custom_presets.json.bak`) and recovers from JSON corruption on startup.
* **Courtesy Throttling & Smart RAM Scratch:** Yields CPU (`nice`) and disk I/O (`ionice`) automatically when host workloads rise, and optionally encodes in `/dev/shm` only when safe memory headroom exists.
* **Optional LAN Login Guard:** Bind to `127.0.0.1` by default, or expose on your LAN with `AVE_WEB_PASSWORD` enabled to require a session login prompt.

---

## Quick Start (Interactive Installer)

AVE works with both rootless [Podman](https://podman.io) (recommended) and Docker.

```bash
git clone https://github.com ~/containers/ave/source
cd ~/containers/ave/source
./install.sh
```

The interactive installer prompts you for:
1. **Host data path** (defaults to `~/containers/ave`)
2. **Web GUI port** (defaults to `8488`)
3. **Local-only vs. LAN access** (with optional session username and password)
4. **Automatic RAM scratch sizing** (`50%` of host memory) and optional **systemd auto-start on boot** (Linux + Podman)

Once finished, open **`http://127.0.0.1:8488`** in your browser.

---

## Directory Layout (`~/containers/ave`)

All runtime folders live under `~/containers/ave` on the host (mounted to `/data` inside the container):

```text
~/containers/ave/
├── source/          # Cloned repository (Containerfile, ave/, entrypoint.py, install.sh)
├── watch/           # Drop incoming videos or nested Show/Season folders here
│   ├── _originals/  # Verified source files archived here after QA passes
│   └── _failed/     # Corrupt/unreadable files + .error.txt reports
├── output/          # Finished encodes mirroring watch/ folder structure
└── config/          # config.json, custom_presets.json, .bak backup, status.json
```

> **Storage Best Practice (Local Disk vs. NAS / ZFS):**
> Copy media onto local storage (`~/containers/ave/watch`) rather than mounting remote SMB/NFS network shares directly. Keeping `watch/` and `output/` on the same local filesystem ensures atomic file moves and avoids network drop failures. If your host uses **ZFS** with automated snapshots, exclude `~/containers/ave` from frequent snapshot schedules so temporary `.part` files and archived `_originals` do not bloat snapshot blocks.

---

## Manual Setup Options

### Podman CLI
```bash
mkdir -p ~/containers/ave/{watch,output,config}
cd ~/containers/ave/source
podman build -t localhost/ave:latest -f Containerfile .

SHM_GB=\$(awk '/MemTotal/ {g=int(\$2/1024/1024/2); print (g>0?g:2)"g"}' /proc/meminfo)
podman run -d --name ave \
  -p 127.0.0.1:8488:8488 \
  -v ~/containers/ave:/data:Z \
  --shm-size="\$SHM_GB" \
  localhost/ave:latest
```

### Podman Quadlet (Systemd Auto-Start)
```bash
mkdir -p ~/.config/containers/systemd
cp ~/containers/ave/source/ave.container ~/.config/containers/systemd/
systemctl --user daemon-reload
systemctl --user enable --now ave.service
loginctl enable-linger "\$USER"
```

### Docker / Podman Compose
```bash
cd ~/containers/ave/source
docker compose up -d --build
# Or: podman compose up -d --build
```

---

## Running Unit Tests

```bash
cd ~/containers/ave/source
python3 -m unittest discover -s tests -v
```

## License

Licensed under the **GNU Affero General Public License v3.0 (AGPLv3)**. See [LICENSE](LICENSE) for full terms.
