# One-shot upload links via Cloudflare Tunnel.
# Named tunnel on your own domain is configured in .env (see `just setup`);
# without it, a random trycloudflare.com quick tunnel is used.

set dotenv-load

cloudflared := "cloudflared"

default:
    @just --list

# Generate a one-time upload link (extra args go to the script, e.g. `just link -e 2 -m 10 -j 8`)
link *args:
    uv run --script send-me.py {{args}}

# Force a random trycloudflare.com quick tunnel, ignoring .env
quick *args:
    uv run --script send-me.py --quick {{args}}

# Same as link, but local only (no tunnel), for testing
local *args:
    uv run --script send-me.py --local {{args}}

# One-time setup of a named tunnel on your domain, e.g. `just setup send.example.com`
setup hostname name="send-me":
    #!/usr/bin/env bash
    set -euo pipefail
    cf="{{cloudflared}}"
    if [ ! -f ~/.cloudflared/cert.pem ]; then
        echo ">> Log in in the browser and pick the zone for {{hostname}}"
        $cf tunnel login
    fi
    if ! $cf tunnel info "{{name}}" >/dev/null 2>&1; then
        $cf tunnel create "{{name}}"
    fi
    $cf tunnel route dns "{{name}}" "{{hostname}}"
    printf 'SEND_ME_TUNNEL=%s\nSEND_ME_HOSTNAME=%s\n' "{{name}}" "{{hostname}}" > .env
    echo ">> Done. Wrote .env; \`just link\` now uses https://{{hostname}}"

# Delete the named tunnel and .env (the DNS record must be removed in the dashboard)
teardown name="send-me":
    {{cloudflared}} tunnel delete -f "{{name}}"
    rm -f .env

# Open the folder with received files
open:
    xdg-open ~/Downloads/send-me

# Delete unfinished uploads kept for resuming (~/Downloads/send-me/.incoming)
clean:
    rm -rf ~/Downloads/send-me/.incoming

# Show options
help:
    uv run --script send-me.py --help
