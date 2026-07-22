#!/usr/bin/env bash
#
# One-shot VPS setup for the Pokemon Drop Notifier + wog auto-buyer.
# Tested on a fresh Ubuntu 24.04 Hetzner server, run as root.
#
# Usage (paste this whole line once you're logged into the server):
#   curl -fsSL https://raw.githubusercontent.com/iliasdecraene/PokemonPing/main/vps-setup.sh | bash
#
# It installs Python + git, clones the repo, builds a virtualenv, and registers
# a systemd service that runs 24/7 and restarts on crash or reboot. It does NOT
# start the bot — you edit .env with your secrets first, then start it.

set -euo pipefail

REPO="https://github.com/iliasdecraene/PokemonPing.git"
DIR="/root/PokemonPing"
SERVICE="pokemonping"

echo "==> Installing system packages…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null

echo "==> Fetching the code into ${DIR}…"
if [ -d "${DIR}/.git" ]; then
  git -C "${DIR}" pull --ff-only
else
  git clone --depth 1 "${REPO}" "${DIR}"
fi

echo "==> Building the Python virtualenv…"
python3 -m venv "${DIR}/.venv"
"${DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${DIR}/.venv/bin/pip" install --quiet -r "${DIR}/requirements.txt"

echo "==> Preparing .env…"
if [ ! -f "${DIR}/.env" ]; then
  cp "${DIR}/.env.example" "${DIR}/.env"
  chmod 600 "${DIR}/.env"
  echo "    created ${DIR}/.env (fill in your secrets before starting)"
else
  echo "    ${DIR}/.env already exists — left untouched"
fi

echo "==> Registering the systemd service…"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Pokemon Drop Notifier + wog auto-buyer
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${DIR}
EnvironmentFile=${DIR}/.env
ExecStart=${DIR}/.venv/bin/python ${DIR}/notifier.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE}" >/dev/null 2>&1

cat <<EOF

============================================================
 Setup done. The bot is installed but NOT running yet.

 1. Put your secrets in the config:
       nano ${DIR}/.env
    (fill WOG_USERNAME/PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS;
     leave WOG_BUY_DRYRUN=1 for now so it only *pings*, never buys)

 2. Quick login check (optional but recommended):
       set -a; . ${DIR}/.env; set +a
       ${DIR}/.venv/bin/python ${DIR}/wog_buyer.py login-test

 3. Start it (and it'll auto-restart forever after):
       systemctl start ${SERVICE}

 4. Watch it live:
       journalctl -u ${SERVICE} -f
============================================================
EOF
