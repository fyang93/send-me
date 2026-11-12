# send-me

**English** · [中文](README.zh-CN.md)

One-time upload links for receiving files from friends, served from your own machine through Cloudflare Tunnel. No public IP, no account for the sender, no service running in the background.

```
just link   →   https://send.example.com/k3J9…   →   friend uploads   →   link dies
```

## How it works

```
 friend's browser ──HTTPS──▶ Cloudflare ──tunnel──▶ cloudflared ──▶ send-me.py ──▶ ~/Downloads/send-me/
```

- `just link` starts a small Python server (standard library only) and a `cloudflared` tunnel, then prints the link and a QR code.
- The first browser that starts uploading claims the link. Everyone else gets "link already used".
- When the upload finishes, or the link expires, the server exits and the tunnel closes. Every run gets a fresh random token, so old links never work again.

## Features

- **One-time link**: a random 24-character token per run, plus an optional expiry time and total size limit.
- **Parallel chunked upload**: 8 MiB chunks over 4 connections (configurable). This helps a lot on lossy long-distance routes, e.g. from mainland China.
- **Resumable, like `rsync --partial`**:
  - Cancel, page reload, network drop: the upload continues where it stopped.
  - Server stopped or link expired: run `just link` again and have the sender pick the same files. Chunks already received are skipped.
  - Unfinished data is kept in `~/Downloads/send-me/.incoming/` for 7 days.
- **End-to-end integrity**: the browser computes a SHA-256 for every chunk and the server verifies it. A corrupted chunk is sent again.
- **Live terminal progress**: bar, received/total bytes, speed, ETA, current file.
- **Web page**:
  - Drag & drop and multiple files.
  - Per-file progress, an overall progress ring, cancel/resume.
  - Light and dark mode, a WebGL fluid background, bilingual (中文 / English).
  - Loads no external resources, so it also works behind the Great Firewall.
- **Tolerant links**: if a chat app glues text onto the URL (`…/TOKEN，thanks`), the page still opens.

## Requirements

- Linux or macOS with [Nix](https://nixos.org) (flakes) and [direnv](https://direnv.net) (nix-direnv recommended).
  The dev shell provides `cloudflared`, `just`, `uv` and `qrencode`.
- Without Nix: install `cloudflared`, `just` and `uv` yourself (`qrencode` is optional).
- For a fixed URL: a domain whose DNS is managed by Cloudflare (the free plan is enough).

## Quick start

```sh
cd send-me
direnv allow          # first time only: builds the dev shell
just link             # prints the link + QR code, waits for the upload
```

Send the link to your friend. Received files land in `~/Downloads/send-me/<date-time>/`.
Without a domain configured, `just link` uses a random `*.trycloudflare.com` quick tunnel. Quick tunnels need no account but have no uptime guarantee.

## Using your own domain (recommended)

One-time setup. A browser window opens for the Cloudflare login, where you pick your domain:

```sh
just setup send.example.com
```

This runs `cloudflared tunnel login`, creates a tunnel named `send-me`, adds the DNS record, and writes `.env`:

```sh
SEND_ME_TUNNEL=send-me
SEND_ME_HOSTNAME=send.example.com
```

After that, `just link` prints `https://send.example.com/<token>`. The URL stays fixed but the token changes every run. When send-me is not running, nothing answers on the domain.

## Commands

| Command | Description |
|---|---|
| `just link [opts]` | Create a one-time upload link (uses `.env` if present) |
| `just quick [opts]` | Force a random trycloudflare.com link |
| `just local [opts]` | Localhost only, no tunnel (for testing) |
| `just setup <host>` | One-time named-tunnel setup on your domain |
| `just teardown` | Delete the tunnel and `.env` (remove the DNS record in the dashboard) |
| `just open` | Open the download folder |
| `just clean` | Delete unfinished uploads kept for resuming |
| `just help` | Show all options |

Options (pass after `just link`, e.g. `just link -e 2 -m 10 -j 8`):

| Option | Default | Meaning |
|---|---|---|
| `-e, --expire HOURS` | `24` | Link lifetime |
| `-m, --max-gb GB` | `50` | Max total size, `0` = unlimited |
| `-j, --parallel N` | `4` | Concurrent chunk uploads (1–16) |
| `-o, --out DIR` | `~/Downloads/send-me` | Where files are saved |
| `-p, --port PORT` | random | Local port |
| `--tunnel / --hostname` | from `.env` | Named tunnel and its public hostname |

## Resuming an interrupted upload

1. The terminal says `kept for resume: <file> (x / y)`.
2. Run `just link` again and send the new link.
3. The sender opens it and selects **the same files** (same name, size and modification time).
4. The page shows "已从断点继续 / resuming" and only the missing chunks are sent.

## Security notes

- Only someone who has the exact link can upload, and only one browser session can use it.
- File names are sanitized: no path traversal, no hidden files. Total size is capped by `--max-gb`.
- Transport is HTTPS between the browser and Cloudflare, then the encrypted tunnel to your machine. Cloudflare terminates TLS, so do not use this for data you would not trust to Cloudflare.
- The server listens on `127.0.0.1` only. Nothing is exposed when `just link` is not running.

## Troubleshooting

- **Sender gets 404**: their chat app may have changed the link, or they opened an old one. The terminal logs every 404 with IP, country, path and user agent. Send the link as its own message, or share the QR code.
- **`just setup` hangs after "you can close this window"**: press Ctrl+C and run it again. Login polling occasionally stalls.
- **`WRN Your version … is outdated`**: harmless. The nixpkgs build lags a few releases behind.
- **Slow uploads from far away**: try `-j 8`. If speed does not change, the sender's own uplink is the limit.

## Files

```
send-me.py    server, tunnel management, terminal UI (Python stdlib, run via `uv run --script`)
upload.html   the upload page (single self-contained file)
justfile      commands
flake.nix     dev shell: cloudflared, just, uv, qrencode
.envrc        `use flake` for direnv
.env          named-tunnel settings (created by `just setup`, git-ignored)
```
