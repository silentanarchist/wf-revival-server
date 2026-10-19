#!/bin/bash
# Adds one more website on the home PC (e.g. the Weblate translation site) behind the relay, so its
# domain can point at the relay instead of the home address.
#
# Connections to port 443 are sorted by the name the visitor asks for (the TLS "server name", which
# is readable before anything is decrypted): that name goes to the extra site's port on the home PC,
# everything else goes to the game website exactly as before. Port 80 (plain http, for the extra
# site's redirect to https) is passed to the extra site's http port. Like everything else, both go
# through the tunnel unopened, starting with a PROXY line naming the visitor's address.
#
# The game (8780) is not touched, and HAProxy is reloaded, not restarted: open connections carry on.
# The new settings are checked before they are used; if the check fails nothing changes.
#
# Run as root on the relay:
#   bash relay-extra-site.sh <domain> [https port on the PC, default 8443] [http port on the PC, default 8081]
#   bash relay-extra-site.sh --remove
# The choice is kept in /etc/haproxy/extra-site, so running relay-setup.sh again keeps it.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this as root."; exit 1; }

KEEP=/etc/haproxy/extra-site
if [ "${1:-}" = "--remove" ]; then
  rm -f "$KEEP"
else
  DOMAIN="${1:-}"; HTTPS_PORT="${2:-8443}"; HTTP_PORT="${3:-8081}"
  if ! [[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
     || ! [[ "$HTTPS_PORT" =~ ^[0-9]{2,5}$ ]] || ! [[ "$HTTP_PORT" =~ ^[0-9]{2,5}$ ]]; then
    echo "Usage: bash relay-extra-site.sh <domain> [https port on the PC] [http port on the PC]"
    echo "   or: bash relay-extra-site.sh --remove"
    exit 1
  fi
  echo "$DOMAIN $HTTPS_PORT $HTTP_PORT" > "$KEEP"
fi

# the same configuration relay-setup.sh writes, plus the extra site when one is kept
write_config() {
  local out="$1" domain="" https_port="" http_port=""
  [ -s "$KEEP" ] && read -r domain https_port http_port < "$KEEP"
  {
    cat <<'CONF'
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
CONF
    if [ -n "$domain" ]; then
      cat <<CONF
    # read the name the visitor asks for (sent before encryption starts), then sort by it
    tcp-request inspect-delay 5s
    tcp-request content accept if { req.ssl_hello_type 1 }
    use_backend extra_site_home if { req.ssl_sni -i $domain }
CONF
    fi
    cat <<'CONF'
    default_backend website_home

backend website_home
    server home 10.66.0.2:443 send-proxy
CONF
    if [ -n "$domain" ]; then
      cat <<CONF

# $domain (https and its http redirect) on the home PC
backend extra_site_home
    server home 10.66.0.2:$https_port send-proxy

frontend extra_site_http
    bind 0.0.0.0:80
    bind :::80 v6only
    default_backend extra_site_http_home

backend extra_site_http_home
    server home 10.66.0.2:$http_port send-proxy
CONF
    fi
  } > "$out"
}

NEW=/etc/haproxy/haproxy.cfg.new
write_config "$NEW"
if ! haproxy -c -f "$NEW" >/dev/null; then
  echo "The new settings did not pass HAProxy's check; nothing was changed."
  haproxy -c -f "$NEW" || true
  rm -f "$NEW"
  exit 1
fi
[ -f /etc/haproxy/haproxy.cfg.before-extra-site ] || cp /etc/haproxy/haproxy.cfg /etc/haproxy/haproxy.cfg.before-extra-site
mv "$NEW" /etc/haproxy/haproxy.cfg
systemctl reload haproxy

if [ -s "$KEEP" ]; then
  ufw allow 80/tcp >/dev/null
  read -r d p1 p2 < "$KEEP"
  echo "Done: $d now goes to the home PC's port $p1 (https) and $p2 (http); everything else as before."
else
  ufw delete allow 80/tcp >/dev/null 2>&1 || true
  echo "Done: the extra site is removed; 443 goes to the game website only."
fi
