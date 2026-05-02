# Soulmask Manager

A local web app for running Soulmask 1.0 dedicated servers from a
single dashboard. Runs on the same Windows machine as the game
server itself, opens on `http://<server-ip>:5000`, and gives you
point-and-click control over starting, stopping, updating, backing
up, and watching your servers without ever needing to RDP in for
routine work.

Built for a single operator running one or two cluster nodes.

## Quick start

1. Make sure Python 3.11+ is on PATH.
2. Double-click `start_manager.bat`.
3. Open `http://<server-ip>:5000` from any browser on your LAN.

First launch creates the venv, installs dependencies, and writes a
default `data/settings.toml`. Walk through `/setup/` to install
SteamCMD, download the Soulmask server, and lay down launch scripts.

## What it does

### Setup wizard

A guided first-run flow at `/setup/`:

- Pick single-map or two-map cluster.
- Set server names, ports, password.
- Choose a GameXishu tuning preset (~90 templates available).
- Installs SteamCMD, downloads the Soulmask server, generates the
  per-instance launch `.bat` files, copies the chosen GameXishu
  template, and patches the cluster config.
- Installs PortableGit alongside SteamCMD on first run, so the
  manager can pull its own updates without you installing Git
  separately.

### Live dashboard

The main page at `/`:

- Per-instance status cards — running / stopped / shutting down,
  PID, uptime, CPU, RAM, player count, ports.
- Cluster banner showing all-running / partial / all-stopped.
- Auto-refreshes via a live event stream (no polling lag).
- Start, stop (with configurable in-game warning countdown), and
  cancel-shutdown buttons right on the dashboard.
- Per-instance and cluster-wide login lock toggles for maintenance
  windows.
- At-a-glance "update available" badges for both Steam and the
  manager itself, linking through to the Updates page.

### Server lifecycle

- **Always-graceful stop.** Shutdown goes through the in-game
  EchoPort `SaveAndExit` command with a configurable countdown
  (1 second to 15 minutes). The manager never auto-kills.
- **Cluster-aware ordering.** Two-map clusters start the main
  instance first and the child once the main is healthy; stop
  reverses the order so dependent state saves cleanly.
- **Adoption.** If the manager restarts while servers are running,
  it reattaches to the existing processes via PID scan rather than
  spawning duplicates.
- **Login lock.** Block new logins per-instance or cluster-wide for
  clean maintenance windows. State persists across manager restarts
  and re-applies to adopted servers on boot.

### Updates — game and manager

One unified Updates page at `/updates/` covers both:

- **Steam (game server).** Compares your installed buildid against
  the latest public buildid via SteamCMD. One-click "Check now"
  refresh. When an update is available, "Update + Restart" stops the
  servers gracefully (with operator-chosen warning), runs SteamCMD
  `app_update`, and brings them back in cluster order. Manual
  "Update" and "Verify" buttons cover the cases where you want to
  re-run SteamCMD without a build-id change.
- **Manager (this app).** Pulls its own updates from the project's
  git remote using a per-machine SSH deploy key. Generate the key
  through the UI, paste the public half into the deploy keys page
  on your repo, and Apply pulls + restarts.

Both update streams have background pollers so you find out about
updates without remembering to check, and the dashboard surfaces
both badges at a glance.

### Recovery and safety nets (manager updates)

Several layers of protection so a bad commit can't take the manager
offline:

- **Pre-pull connectivity probe.** Before any state-changing fetch,
  a quick `ls-remote` distinguishes network-down from auth-broken
  from wrong-URL with a clear message.
- **Pre-apply byte-compile check.** The incoming code is extracted
  to a tempdir and compiled before pulling. Syntax errors and
  missing imports are caught BEFORE the manager restarts on broken
  code.
- **Discard / Force-sync recovery buttons.** When the working tree
  is dirty (operator hand-edited a file on the server, line-ending
  drift, etc.), one-click recovery without shell access.
- **Circuit breaker.** Three failed Apply attempts in 24 hours pause
  auto-updates and surface the actual error. Manual reset on the UI
  re-arms it.
- **Auto-rollback on boot crash.** If the manager fast-crashes after
  an update, the supervisor automatically rolls back to the last
  known-good revision and re-launches.
- **Self-heal on unclean shutdown.** A forced kill or BSOD triggers
  `git fsck` on the next boot to clean up any half-written objects
  from an interrupted pull.

### Backups

Configurable automatic backups of the world database with retention,
plus on-demand backups from the `/backups/` page.

**What's saved.** Each snapshot captures the live world DB for every
running instance plus a "manager config bundle" tar of `settings.toml`,
the active `GameXishu*.json` files, and `Engine.ini`. Per-instance DBs
go into `data/backups/<instance>/`; config bundles into
`data/backups/config/`. The index lives in `data/backups.json`.

**When it runs.** Three independent triggers, each toggleable in
Settings:

- **Online cadence** (default every 2 hours, ON) — fires while at
  least one player is connected.
- **Post-logoff one-shot** (default 45 minutes after the last logoff,
  ON) — single-shot save once everyone's gone.
- **Offline cadence** (default every 6 hours, OFF) — for keeping a
  freshly-saved copy while the server idles. Off by default; turn on
  if you want it.

Plus two automatic snapshots tied to lifecycle events:

- **Pre-shutdown** (ON) — fires before any operator-triggered Stop
  countdown.
- **Pre-update** (mandatory) — fires before every Update + Restart
  so there's always a rollback target if a Steam update breaks
  something.

**In-game warnings.** Scheduled backups broadcast `say` messages at
T-3min and T-30s so players see the upcoming brief lag (manual
Backup-now skips the chatter).

**Restore.** `/backups/` row -> Restore -> prepare page -> confirm.
The flow:

1. Takes a fresh "pre-restore" snapshot of current state first.
2. Stops only the instances that need replacing (in canonical reverse
   order).
3. Decompresses the backup to a staging file, integrity-checks it,
   atomic-renames over the live world DB.
4. Optionally restores the manager config bundle (off by default;
   your current settings get saved as `settings.toml.before-restore`
   for manual merge).
5. Restarts only the instances that were running before. Re-applies
   login-lock state.

If the snapshot's build_id doesn't match the currently-installed
build, the prepare page shows a prominent cross-build warning before
you confirm.

**Retention.** Last 48 unpinned snapshots per instance (configurable);
pinned snapshots are exempt and don't count toward the limit. Each
backup is integrity-checked (`PRAGMA integrity_check`) before being
written, and again before being used in a restore. Disk-free check
warns and aborts if the manager volume drops below 2 GB before a
scheduled save.

### Mods

`/mods/` page handles Steam Workshop subscriptions:

- Paste a Workshop ID, click Add — the manager runs SteamCMD,
  installs into `WS\Mods\<MOD-folder>\`, and tracks it in
  `data/mods_manifest.json`.
- Friendly names (from `ModeInfo.json` / `.uplugin`) display
  alongside the folder ID.
- Per-row Redownload button refreshes a Workshop mod from
  SteamCMD (overwrites local files, preserves the manifest entry).
  Remove drops the folder and the manifest entry.
- Server-side auto-discovery mounts every pak in `WS\Mods\` regardless
  of subscription state. The `-mod=` launch flag advertises the IDs
  to connecting clients so their game knows to fetch the same mods.

Server must be stopped to add or remove mods (SteamCMD can't update
files while WSServer.exe holds them open).

### Logs and diagnostics

`/logs/` — live tail and search across:

- **Manager log** — the manager's own application log.
- **WS / WS_2** — game server logs (one per cluster instance).
- **Events** — the parsed event stream extracted from WS logs:
  joins, leaves, chat, deaths, knockdowns, recruits, thrall down,
  thrall lost, invasions, plus any `[HOOK] ...` lines emitted by a
  server-side mod's Log String node.

Filter, pause, auto-scroll, and SSE-streamed live updates. Tabs
remember their state when you switch away.

### Map

`/map/` — Leaflet view of CloudMist (DLC) and Shifting Sands maps:

- POI sidebar grouped by category (chests, dungeons, animals,
  resources, etc.) with proper game icons.
- Live online-player markers via 30-second `lp` polling while at
  least one game-server instance is running.
- Tile pyramid shipped with the manager (no external CDN).

Coordinate calibration is per-server. The CloudMist transform is
verified; if your Shifting Sands markers look offset, you can pin
down the transform empirically using the `dap` admin command (writes
`Saved/ACTOR_POSI_DATA.log`) against a known landmark.

### Settings

`/settings/` exposes everything tunable: poll intervals, log levels,
UI refresh rate, backup schedule, network bind host/port, theme
(11 DaisyUI themes available), etc. Changes take effect on next
poll iteration without a manager restart where possible.

- The **Discord** tab has a Test-connection button that posts a
  "connected" message to the configured webhook so you can verify
  the URL without waiting for an in-game event.
- The **Manager update detection** tab hosts the deploy-key UI
  (generate, copy, test connection, regenerate) for manager
  self-updates over SSH.

## Stopping

Ctrl+C in the cmd window where it's running. Closing the window
also kills it. Game servers, if running, are unaffected — the
manager re-adopts them on next start.

## File layout

```
SM_Manager/
  manager/         Python package (Flask app)
  data/            settings.toml, backups, deploy key, runtime state
  logs/            rotating manager.log
  steamcmd/        local SteamCMD install
  portable/git/    bundled Git for self-update (auto-installed)
  venv/            Python virtualenv
  start_manager.bat
  bootloader.py    pre-boot supervisor (handles auto-rollback)
```
