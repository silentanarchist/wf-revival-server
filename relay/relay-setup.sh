#!/bin/bash
# Sets up a small rented Linux server (Ubuntu) as the public face of a Warpforge Revival server.
#
# Players connect to this machine; it passes every connection on, untouched and still encrypted,
# through a private WireGuard tunnel to the PC that runs the real server. The PC opens the
# tunnel itself, so no ports need to be open on the home network and the home address stays
# private. Each connection is passed on with one line saying who really connected (the PROXY
# protocol), so the server's bans and limits still see each player's own address.
#
#   ports passed on:  8780 (the game: web requests, https and matches)  and  443 (the website)
#   tunnel:           this machine 10.66.0.1  <->  the home PC 10.66.0.2  (UDP port 51820)
#
# Run as root:   bash relay-setup.sh <the home PC's WireGuard public key>
# Running it again is safe: it keeps this machine's tunnel key and rewrites everything else.
set -euo pipefail

HOME_KEY="${1:-}"
if ! [[ "$HOME_KEY" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$ ]]; then
  echo "Usage: bash relay-setup.sh <the home PC's WireGuard public key>"
  echo "(the 44-character key the WireGuard app shows for the tunnel on the PC)"
  exit 1
fi
[ "$(id -u)" = 0 ] || { echo "Run this as root."; exit 1; }

echo "== installing WireGuard, HAProxy and the firewall"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q wireguard haproxy ufw unattended-upgrades

echo "== the tunnel"
umask 077
mkdir -p /etc/wireguard
[ -s /etc/wireguard/relay.key ] || wg genkey > /etc/wireguard/relay.key
RELAY_KEY="$(cat /etc/wireguard/relay.key)"
RELAY_PUB="$(printf '%s' "$RELAY_KEY" | wg pubkey)"
cat > /etc/wireguard/wg0.conf <<CONF
[Interface]
Address = 10.66.0.1/24
ListenPort = 51820
PrivateKey = $RELAY_KEY

[Peer]
# the PC that runs the Warpforge Revival server
PublicKey = $HOME_KEY
AllowedIPs = 10.66.0.2/32
CONF
umask 022
systemctl enable wg-quick@wg0 >/dev/null 2>&1
systemctl restart wg-quick@wg0

echo "== passing connections on"
[ -f /etc/haproxy/haproxy.cfg.original ] || cp /etc/haproxy/haproxy.cfg /etc/haproxy/haproxy.cfg.original
cat > /etc/haproxy/haproxy.cfg <<'CONF'
# Warpforge Revival relay: connections are passed on unopened (still encrypted) to the home PC
# through the WireGuard tunnel, each starting with a PROXY line naming the player's address.
# Nothing is logged here.
global
    maxconn 4000
    user haproxy
    group haproxy
    daemon

defaults
    mode tcp
    timeout connect 10s
    timeout client 2h
    timeout server 2h

frontend game
    bind 0.0.0.0:8780
    bind :::8780 v6only
    default_backend game_home

backend game_home
    server home 10.66.0.2:8780 send-proxy

frontend website
    bind 0.0.0.0:443
    bind :::443 v6only
    default_backend website_home

backend website_home
    server home 10.66.0.2:443 send-proxy
CONF
haproxy -c -f /etc/haproxy/haproxy.cfg >/dev/null
systemctl enable haproxy >/dev/null 2>&1
systemctl restart haproxy

echo "== firewall: only SSH, the tunnel, the game and the website"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 51820/udp >/dev/null
ufw allow 8780/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "== SSH: keys only"
if [ -s /root/.ssh/authorized_keys ]; then
  cat > /etc/ssh/sshd_config.d/10-relay.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
  systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || true
else
  echo "   (skipped: no SSH key is installed for root, so password sign-in stays on)"
fi

echo "== security updates install themselves"
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

echo
echo "Done. Put this in the [Peer] section of the tunnel on the PC:"
echo
echo "    PublicKey = $RELAY_PUB"
echo
echo "Then activate the tunnel on the PC and check here with:   wg show"
echo "(a 'latest handshake' line means the tunnel is up)"
