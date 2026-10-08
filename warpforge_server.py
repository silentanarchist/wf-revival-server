#!/usr/bin/env python3
"""Warpforge Revival content server.

Serves the card pack (and later: custom art, accounts, friends, relay info)
to the Warpforge Revival mod. Standard library only - runs anywhere with
Python 3.9+.

Content layout (default ./content):
    content/cards/*.json      one JSON array of card objects per file
                              (e.g. one file per faction, plus customs)
    content/files/...         any extra files the mod may download
                              (custom card art etc.), served as-is

The pack version is the SHA-256 of the combined, canonicalised card data, so
editing any card file automatically produces a new version; clients compare
it with their cached copy and re-download only when it changed. Players in a
match must have the same pack version.

Usage:
    python3 warpforge_server.py --port 8780 --content ./content
"""
import argparse
import gzip
import io
import ipaddress
import hashlib
import json
import logging
import os
import re
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs

import importlib

import select
import shutil
import socket
import ssl

import photon_emu
import playfab_emu
from playfab_emu import PlayFabEmulator

API_VERSION = 1
SERVER_NAME = "Warpforge Revival"
log = logging.getLogger("warpforge")


class CardPack:
    """Combined card data built from content/cards/*.json, rebuilt when files change."""

    def __init__(self, cards_dir):
        self.cards_dir = cards_dir
        self.lock = threading.Lock()
        self._stamp = None
        self.version = None
        self.body = b""
        self.body_gz = b""
        self.card_count = 0
        self.errors = []

    def _current_stamp(self):
        stamp = []
        if os.path.isdir(self.cards_dir):
            for name in sorted(os.listdir(self.cards_dir)):
                if name.lower().endswith(".json"):
                    st = os.stat(os.path.join(self.cards_dir, name))
                    stamp.append((name, st.st_mtime_ns, st.st_size))
        return tuple(stamp)

    def refresh(self):
        with self.lock:
            stamp = self._current_stamp()
            if stamp == self._stamp:
                return
            cards, errors, seen = [], [], {}
            for name, _, _ in stamp:
                path = os.path.join(self.cards_dir, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        data = data.get("cards", [])
                    for i, card in enumerate(data):
                        problem = validate_card(card)
                        if problem:
                            errors.append(f"{name}[{i}]: {problem}")
                            continue
                        cid = card["id"]
                        if cid in seen:
                            errors.append(f"{name}[{i}]: duplicate id '{cid}' (also in {seen[cid]})")
                            continue
                        seen[cid] = name
                        card = dict(card)
                        card.setdefault("source", name)
                        cards.append(card)
                except Exception as e:  # keep serving the rest of the pack
                    errors.append(f"{name}: {e}")
            cards.sort(key=lambda c: c["id"])
            canonical = json.dumps(cards, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            version = hashlib.sha256(canonical).hexdigest()[:16]
            pack = {"format": 1, "version": version, "builtAt": int(time.time()), "cards": cards}
            self.body = json.dumps(pack, ensure_ascii=False, separators=(",", ":")).encode()
            self.body_gz = gzip.compress(self.body, 6)
            self.version, self.card_count, self.errors, self._stamp = version, len(cards), errors, stamp
            log.info("card pack %s: %d cards from %d files%s", version, len(cards), len(stamp),
                     f", {len(errors)} problems" if errors else "")
            for e in errors:
                log.warning("  %s", e)


ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
REQUIRED = ("id", "name", "army", "cardType")


def validate_card(card):
    if not isinstance(card, dict):
        return "not an object"
    for k in REQUIRED:
        if k not in card or card[k] in (None, ""):
            return f"missing '{k}'"
    if not ID_RE.match(str(card["id"])):
        return f"bad id '{card['id']}' (letters, digits, _ . - only)"
    return None


class BundleStore:
    """Replacement Addressables bundles in content/bundles/ (served to the mod, which hands
    them to the game in place of the dead cloud downloads)."""

    def __init__(self, d, url="/api/v1/content/bundles/"):
        self.dir = d
        self.url = url
        self._cache = {}

    def list(self):
        out = []
        if not os.path.isdir(self.dir):
            return out
        for name in sorted(os.listdir(self.dir)):
            if not name.endswith(".bundle"):
                continue
            path = os.path.join(self.dir, name)
            st = os.stat(path)
            key = (name, st.st_mtime_ns, st.st_size)
            if key not in self._cache:
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
                self._cache[key] = h.hexdigest()
            out.append({"name": name, "size": st.st_size, "sha256": self._cache[key],
                        "url": self.url + name})
        return out


class ModStore:
    """The current mod builds in mod/ (next to this script): WarpforgeRevival.dll for Windows and
    WarpforgeRevival.Android.dll for phones. Clients compare the version with their own and offer
    to update. Replace a file to publish a new build for that kind of device."""

    NAME = "WarpforgeRevival.dll"
    ANDROID_NAME = "WarpforgeRevival.Android.dll"

    def __init__(self, d):
        self.dir = d
        self._cache = {}

    def path(self, android=False):
        return os.path.join(self.dir, self.ANDROID_NAME if android else self.NAME)

    def info(self, android=False):
        path = self.path(android)
        if not os.path.isfile(path):
            return None
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
        cached = self._cache.get(android)
        if cached is None or cached[0] != key:
            with open(path, "rb") as f:
                data = f.read()
            # the version is the string after the mod name in the MelonInfo attribute: 0.11.0-w for
            # Windows, 0.11.0-a for Android. "label" is that name; "version" is the same number in the
            # form builds from before 0.11.0 can compare (plain for Windows, -android0 for phones).
            m = re.search(rb"Warpforge Revival[\s\S](\d+(?:\.\d+){1,3})(-[a-z][a-z0-9]*)?", data)
            number = m.group(1).decode() if m else "0.0.0"
            label = number + (m.group(2).decode() if m and m.group(2) else "")
            cached = (key, {"version": number + ("-android0" if android else ""), "label": label,
                            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "url": "/api/v1/mod/download" + ("?platform=android" if android else "")})
            self._cache[android] = cached
        return cached[1]


_settings_cache = [None, {}]


def settings():
    """content/files/revival-settings.json, re-read when the file changes ({} when missing)."""
    path = os.path.join(Handler.files_dir or "", "revival-settings.json")
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return {}
    if _settings_cache[0] != stamp:
        data = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            pass
        _settings_cache[0], _settings_cache[1] = stamp, data if isinstance(data, dict) else {}
    return _settings_cache[1]


def relays():
    """Addresses of the server's own relays ("relays" in the settings file). Only these may say
    where a connection really comes from."""
    listed = settings().get("relays") or []
    return tuple(str(a) for a in listed if isinstance(a, str)) if isinstance(listed, list) else ()


class _Prefixed(io.RawIOBase):
    """A connection with bytes already read put back in front of it."""
    def __init__(self, prefix, sock):
        self.prefix, self.sock = prefix, sock

    def readable(self):
        return True

    def readinto(self, buf):
        if self.prefix:
            n = min(len(buf), len(self.prefix))
            buf[:n] = self.prefix[:n]
            self.prefix = self.prefix[n:]
            return n
        return self.sock.recv_into(buf)


class Certificates:
    """Certificates for encrypted (https) connections on the game port.

    The server does not obtain certificates itself: it reads the files another program keeps up
    to date - by default the ones the Caddy web server stores for the https site - and looks again
    every few minutes, so a renewed certificate is picked up without a restart. With several
    certificates the one matching the name the client asked for is used. With none, the server
    simply runs without https."""
    CHECK_EVERY = 300           # seconds between looks at the files

    def __init__(self, folders=(), cert=None, key=None):
        self.folders = [f for f in folders if f]
        self.pair = (cert, key) if cert and key else None
        self.lock = threading.Lock()
        self.seen = None        # the files and their times at the last look
        self.checked = 0
        self.contexts = {}      # name -> SSLContext
        self.default = None

    def _pairs(self):
        found = {}
        if self.pair and all(os.path.isfile(f) for f in self.pair):
            found[os.path.splitext(os.path.basename(self.pair[0]))[0].lower()] = self.pair
        for folder in self.folders:
            for where, _, names in os.walk(folder):
                for name in names:
                    if name.lower().endswith(".crt"):
                        key = os.path.join(where, name[:-4] + ".key")
                        if os.path.isfile(key):
                            host, cert = name[:-4].lower().replace("wildcard_", "*"), os.path.join(where, name)
                            # the same name from two issuers: the more recently written one is the live one
                            if host not in found or os.path.getmtime(cert) > os.path.getmtime(found[host][0]):
                                found[host] = (cert, key)
        return found

    def _look(self):
        try:
            pairs = self._pairs()
            stamp = sorted((n, c, os.path.getmtime(c), os.path.getmtime(k)) for n, (c, k) in pairs.items())
        except OSError:
            return
        if stamp == self.seen:
            return
        contexts = {}
        for name, (cert, key) in sorted(pairs.items()):
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.minimum_version = ssl.TLSVersion.TLSv1_2
                ctx.load_cert_chain(cert, key)
                ctx.sni_callback = self._choose
                contexts[name] = ctx
            except (ssl.SSLError, OSError, ValueError) as e:
                log.warning("https: could not use the certificate %s (%s)", cert, e)
        self.seen = stamp
        self.contexts = contexts
        self.default = contexts[sorted(contexts)[0]] if contexts else None
        if contexts:
            log.info("https: encrypted connections are on, with certificate(s) for %s", ", ".join(sorted(contexts)))
        else:
            log.info("https: no certificate found, so connections on this port are not encrypted (see README)")

    def _choose(self, sock, name, _context):
        name = (name or "").lower()
        ctx = self.contexts.get(name) or self.contexts.get("*" + name[name.find("."):] if "." in name else "")
        if ctx is not None:
            sock.context = ctx

    def context(self):
        now = time.time()
        if now - self.checked > self.CHECK_EVERY:
            with self.lock:
                if now - self.checked > self.CHECK_EVERY:
                    self._look()
                    self.checked = now
        return self.default


class Handler(BaseHTTPRequestHandler):
    server_version = "WarpforgeRevival/1"
    certificates: Certificates = None
    pack: CardPack = None
    files_dir: str = None
    playfab: PlayFabEmulator = None
    bundles: BundleStore = None
    bundles_android: BundleStore = None
    mods: ModStore = None
    playfab_args: dict = None
    playfab_mtime: float = 0
    reload_lock = threading.Lock()

    @classmethod
    def current_playfab(cls):
        """Reload playfab_emu.py automatically when it changes on disk (no server restart needed)."""
        mtime = os.path.getmtime(playfab_emu.__file__)
        if mtime != cls.playfab_mtime:
            with cls.reload_lock:
                if mtime != cls.playfab_mtime:
                    try:
                        mod = importlib.reload(playfab_emu)
                        cls.playfab = mod.PlayFabEmulator(**cls.playfab_args)
                        log.info("loaded playfab_emu.py")
                    except Exception:
                        log.exception("playfab_emu.py failed to load; keeping the previous version")
                    cls.playfab_mtime = mtime
        return cls.playfab

    # Limits, so one connection cannot tie the server up:
    timeout = 30                        # seconds a web request may take to arrive (not match connections)
    MAX_BODY = 8 * 1024 * 1024          # largest request body accepted (an uploaded picture is about 5.5 MB)
    REQUESTS_PER_MINUTE = 900           # per address; a game start is a burst of about 60 requests
    _hits = {}
    _hits_lock = threading.Lock()

    def setup(self):
        # Three kinds of connection share this port, told apart by their first byte:
        #   0xFB  the game's match client (handled in handle below);
        #   0x16  the start of an encrypted (TLS) connection: https from the mod or a browser;
        #   else  a plain web request.
        self.secure = False
        self.first = b""
        try:
            self.request.settimeout(self.timeout)
            if self.client_address[0] in relays():
                self._proxy_header()
            self.first = self.request.recv(1, socket.MSG_PEEK)
            ctx = self.certificates.context() if self.first == b"\x16" and self.certificates else None
            if ctx is not None:
                self.request = ctx.wrap_socket(self.request, server_side=True)
                self.secure = True
                # Inside the encryption it is again either a match connection (first byte 0xFB,
                # from the mod's match tunnel) or a web request.
                self.first = self.request.recv(1)
                if not self.first:
                    self.first = None
        except (OSError, ssl.SSLError):
            self.first = None       # nothing arrived, or the encrypted connection could not be set up
        try:
            super().setup()
            if self.secure and self.first and self.first != b"\xfb":
                # the byte just read belongs to the web request: put it back in front
                self.rfile = io.BufferedReader(_Prefixed(self.first, self.request), self.rbufsize if self.rbufsize > 0 else io.DEFAULT_BUFFER_SIZE)
        except OSError:             # the connection is already gone
            self.first = None
            self.rfile = self.wfile = io.BytesIO()

    def _proxy_header(self):
        """A connection passed on by the server's relay (see relay/ and "relays" in the settings
        file) starts with one line saying who really connected, in the PROXY protocol (version 1):
        "PROXY TCP4 <player address> <relay address> <player port> <relay port>". Read it, and
        treat the connection as coming from that player. Only trusted relays may send it."""
        data = b""
        deadline = time.time() + 5
        while b"\r\n" not in data and len(data) < 108 and time.time() < deadline:
            more = self.request.recv(108, socket.MSG_PEEK)
            if not more or len(more) == len(data):
                if not more:
                    return
                time.sleep(0.02)
            data = more
        if not data.startswith(b"PROXY ") or b"\r\n" not in data:
            return
        line = self.request.recv(data.index(b"\r\n") + 2).decode("ascii", "replace").split()
        if len(line) >= 6 and line[1] in ("TCP4", "TCP6"):
            try:
                self.client_address = (line[2], int(line[4]))
            except ValueError:
                pass

    def finish(self):
        try:
            super().finish()
        except (OSError, ssl.SSLError):
            pass
        if self.secure:
            try:
                self.request.close()
            except OSError:
                pass

    def handle(self):
        # The match service shares this port: a Photon client's first byte is 0xFB, which no HTTP
        # request starts with. Such a connection stays open for the whole session.
        first = self.first
        if first is None:
            return
        if first == b"\xfb":
            if self.secure:
                return self._encrypted_match()
            if settings().get("requireEncryptedMatches") is True and self._outside():
                photon_emu.log.info("%s: unencrypted match connection refused", self.client_address[0])
                return
            self.connection.settimeout(None)        # a match lasts as long as it lasts
            photon_emu.serve(self.connection, self.client_address)
            return
        super().handle()

    def _encrypted_match(self):
        """A match connection inside https (from the mod's match tunnel). The match service is handed
        a plain local socket pair; one pump here moves the bytes between it and the encrypted
        connection, waiting on both and reading only from the side that has something, so the
        encrypted connection is only ever used by this one thread."""
        tls = self.request
        mine, theirs = socket.socketpair()
        service = threading.Thread(target=photon_emu.serve, args=(theirs, self.client_address), daemon=True)
        service.start()
        try:
            tls.settimeout(30)                      # a write or the rest of a message that stalls this long: gone
            mine.sendall(self.first)                # the 0xFB already read
            while True:
                if tls.pending():
                    ready = [tls]
                else:
                    ready, _, _ = select.select([tls, mine], [], [], 60)
                    if not ready:
                        continue                    # the match service ends silent connections itself
                if tls in ready:
                    data = tls.recv(65536)
                    if not data:
                        break
                    mine.sendall(data)
                if mine in ready:
                    data = mine.recv(65536)
                    if not data:
                        break
                    tls.sendall(data)
        except (OSError, ssl.SSLError):
            pass
        finally:
            for s in (mine,):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            service.join(35)
            for s in (mine, theirs):
                try:
                    s.close()
                except OSError:
                    pass

    def _client(self):
        """The address a request really comes from: the caller's own, or - only when the caller is
        a web proxy on this same machine - the one the proxy says it is passing on."""
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            passed = (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()
            if passed:
                return passed[:64]
        return ip

    def _too_many(self, ip):
        now = time.time()
        with Handler._hits_lock:
            if len(Handler._hits) > 20000:
                Handler._hits.clear()
            first, count = Handler._hits.get(ip, (now, 0))
            if now - first >= 60:
                first, count = now, 0
            Handler._hits[ip] = (first, count + 1)
            return count + 1 > self.REQUESTS_PER_MINUTE

    def log_message(self, fmt, *args):
        if "/playfab/Revival/ChatPoll" in (getattr(self, "path", "") or ""):
            return                                  # asked every few seconds by every player
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json", gz_body=None, extra=None):
        use_gz = gz_body is not None and "gzip" in self.headers.get("Accept-Encoding", "")
        payload = gz_body if use_gz else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        if use_gz:
            self.send_header("Content-Encoding", "gzip")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_file(self, full, ctype):
        """A file from disk, sent in 1 MB pieces rather than read into memory whole."""
        try:
            f = open(full, "rb")
        except OSError:
            return self._json(404, {"error": "not found"})
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            if self.command != "HEAD":
                shutil.copyfileobj(f, self.wfile, 1 << 20)

    def _is_android(self):
        """Phones say what they are with ?platform=android."""
        return (parse_qs(urlparse(self.path).query).get("platform") or [""])[0].lower() == "android"

    def _bundles_for_request(self):
        """The game on a phone needs its own build of the content files (content/bundles-android);
        the mod says which it is with ?platform=android. Everyone else gets content/bundles."""
        if self._is_android() and self.bundles_android is not None:
            return self.bundles_android
        return self.bundles

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    # Plain http that may still be used when "requireHttps" is on: an older mod has to be able to
    # see that there is an update and download it, and nothing else.
    PLAIN_ALLOWED = ("/api/v1/status", "/api/v1/mod/manifest", "/api/v1/mod/download")

    def _outside(self):
        """True for a connection from outside this machine and its home network."""
        try:
            peer = ipaddress.ip_address(str(self.client_address[0]).split("%")[0])
            return not (peer.is_loopback or peer.is_private or peer.is_link_local)
        except ValueError:
            return True

    def _plain_refused(self, path):
        """With "requireHttps": true in the settings file, plain (unencrypted) http is refused to
        anyone outside this machine and its home network. Caddy, on this machine, still passes
        the website on over plain http. Match connections have their own setting,
        "requireEncryptedMatches"."""
        if self.secure or settings().get("requireHttps") is not True or path in self.PLAIN_ALLOWED:
            return False
        return self._outside()

    def _refuse_plain(self):
        self.close_connection = True
        return self._json(403, {"code": 403, "status": "Forbidden", "error": "EncryptionRequired", "errorCode": 1001,
                                "errorMessage": "This server only accepts encrypted connections. Update the Warpforge Revival mod "
                                                "(0.11.18 or newer) and start the game again."})

    def do_POST(self):
        path = urlparse(self.path).path
        if self._plain_refused(path):
            return self._refuse_plain()
        ip = self._client()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > self.MAX_BODY:
            self.close_connection = True            # the body is not read, so the connection cannot be reused
            return self._json(413, {"code": 413, "status": "PayloadTooLarge", "error": "RequestTooLarge",
                                    "errorCode": 1000, "errorMessage": "that request is too large"})
        body = self.rfile.read(length) if length else b""
        if self._too_many(ip):
            return self._json(429, {"code": 429, "status": "TooManyRequests", "error": "TooManyRequests",
                                    "errorCode": 1199, "errorMessage": "too many requests - wait a minute and try again"})
        if path.startswith("/playfab/"):
            del self.headers["X-Revival-Client-IP"]       # only this server says where a request came from
            self.headers["X-Revival-Client-IP"] = ip
            served = {}
            for key, android in (("w", False), ("a", True)):      # the builds this server hands out, for the version and file checks
                info = self.mods.info(android) if self.mods is not None else None
                if info:
                    served[key] = {"v": info["label"], "h": info["sha256"]}
            del self.headers["X-Revival-Served"]
            self.headers["X-Revival-Served"] = json.dumps(served)
            status, resp = Handler.current_playfab().handle(path, self.headers, body)
            return self._json(status, resp)
        return self._json(404, {"error": "not found"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if self._plain_refused(path):
            return self._refuse_plain()
        if self._too_many(self._client()):
            return self._json(429, {"error": "too many requests - wait a minute and try again"})
        pack = self.pack
        pack.refresh()
        if path == "/":                            # the site's front door: the page itself sends signed-in visitors on
            return self._send(302, b"", "text/plain", extra={"Location": "/login", "Cache-Control": "no-store"})
        if path == "/api/v1/status":
            return self._json(200, {"name": SERVER_NAME, "apiVersion": API_VERSION,
                                    "packVersion": pack.version, "cards": pack.card_count,
                                    "matchService": photon_emu.SERVICE.stats(), "encrypted": bool(self.secure),
                                    "encryptedMatches": self.certificates is not None and self.certificates.default is not None})
        if path == "/api/v1/content/manifest":
            return self._json(200, {"apiVersion": API_VERSION, "packVersion": pack.version,
                                    "cards": pack.card_count, "size": len(pack.body),
                                    "sha256": hashlib.sha256(pack.body).hexdigest(),
                                    "packUrl": "/api/v1/content/pack",
                                    "bundles": self._bundles_for_request().list()})
        if path == "/api/v1/content/pack":
            etag = f'"{pack.version}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            return self._send(200, pack.body, gz_body=pack.body_gz, extra={"ETag": etag})
        if path == "/api/v1/content/problems":
            return self._json(200, {"packVersion": pack.version, "problems": pack.errors})
        for store in (self.bundles_android, self.bundles):
            if store is None or not path.startswith(store.url):
                continue
            name = unquote(path[len(store.url):])
            if "/" in name or "\\" in name or not name.endswith(".bundle"):
                return self._json(404, {"error": "not found"})
            full = os.path.join(store.dir, name)
            if not os.path.isfile(full):
                return self._json(404, {"error": "not found"})
            return self._send_file(full, "application/octet-stream")
        if path == "/api/v1/mod/manifest":
            info = self.mods.info(self._is_android())
            return self._json(200, dict(info, available=True) if info else {"available": False})
        if path == "/api/v1/mod/download":
            android = self._is_android()
            if self.mods.info(android) is None:
                return self._json(404, {"error": "not found"})
            return self._send_file(self.mods.path(android), "application/octet-stream")
        if path.rstrip("/") in CREATOR_PAGES:        # one page serves every tab; it shows the one in the address
            return self._serve_file("creator.html")
        if path.startswith("/api/v1/content/files/"):
            return self._serve_file(unquote(path[len("/api/v1/content/files/"):]))
        return self._json(404, {"error": "not found"})

    FILE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".json": "application/json", ".txt": "text/plain",
                  ".html": "text/html; charset=utf-8"}

    def _serve_file(self, rel):
        root = os.path.realpath(self.files_dir)
        full = os.path.realpath(os.path.join(root, rel))
        if not full.startswith(root + os.sep):
            return self._json(404, {"error": "not found"})
        ctype = self.FILE_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        if os.path.isfile(full):
            return self._send_file(full, ctype)
        # A folder of many small files can be kept as one zip instead: files/card-art/AM3.jpg is
        # also looked for inside files/card-art.zip.
        data = zipped_file(root, os.path.relpath(full, root))
        if data is None:
            return self._json(404, {"error": "not found"})
        self._send(200, data, ctype)


_zips = {}
_zips_lock = threading.Lock()


def zipped_file(root, rel):
    """The bytes of files/<folder>/<name> from files/<folder>.zip, or None. The zip is opened once
    and opened again when it changes."""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) != 2 or not parts[0] or not parts[1] or parts[0] in (".", ".."):
        return None
    path = os.path.join(root, parts[0] + ".zip")
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    with _zips_lock:
        held = _zips.get(path)
        if held is None or held[0] != stamp:
            try:
                held = (stamp, zipfile.ZipFile(path))
            except (OSError, zipfile.BadZipFile):
                return None
            _zips[path] = held
        try:
            return held[1].read(parts[1])
        except KeyError:
            return None


CREATOR_PAGES = {"/login", "/creator", "/my-cards", "/review", "/people", "/examples", "/keywords", "/artwork", "/rules", "/profile"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default all)")
    ap.add_argument("--port", type=int, default=8780, help="TCP port (default 8780)")
    ap.add_argument("--content", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "content"),
                    help="content directory (default ./content next to this script)")
    ap.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                    help="player data directory (default ./data next to this script)")
    ap.add_argument("--certs", default="",
                    help="folder searched for https certificates (name.crt with name.key beside it); "
                         "default: where the Caddy web server keeps its own, and ./https/certs")
    ap.add_argument("--cert", default="", help="one certificate file (with --key), instead of or besides --certs")
    ap.add_argument("--key", default="", help="the private key file for --cert")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    Handler.pack = CardPack(os.path.join(args.content, "cards"))
    Handler.files_dir = os.path.join(args.content, "files")
    Handler.bundles = BundleStore(os.path.join(args.content, "bundles"))
    os.makedirs(Handler.bundles.dir, exist_ok=True)
    Handler.bundles_android = BundleStore(os.path.join(args.content, "bundles-android"),
                                          "/api/v1/content/bundles-android/")
    os.makedirs(Handler.bundles_android.dir, exist_ok=True)
    Handler.mods = ModStore(os.path.join(os.path.dirname(os.path.abspath(__file__)), "mod"))
    os.makedirs(Handler.mods.dir, exist_ok=True)
    os.makedirs(Handler.files_dir, exist_ok=True)
    base = os.path.dirname(os.path.abspath(__file__))
    Handler.playfab_args = dict(data_root=args.data, content_root=args.content, log_dir=os.path.join(base, "logs"))
    Handler.current_playfab()
    Handler.pack.refresh()
    folders = [args.certs] if args.certs else [
        os.path.join(os.environ.get("APPDATA") or "", "Caddy", "certificates") if os.environ.get("APPDATA") else "",
        os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"), "caddy", "certificates"),
        os.path.join(base, "https", "certs")]
    Handler.certificates = Certificates(folders, args.cert, args.key)
    Handler.certificates.context()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("%s listening on %s:%d (content: %s)", SERVER_NAME, args.host, args.port, args.content)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
