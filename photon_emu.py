"""
A stand-in for the Photon match service, for Warhammer 40,000: Warpforge on a revival server.

The game's own Photon client connects here over TCP (the mod only changes the address), so this
module speaks Photon's wire format: TCP framing, the handshake, pings, the "GpBinaryV16"
serialisation, and the LoadBalancing operations the game uses - rooms, matchmaking with the
game's SQL-style filters, relaying events between the players of a room (with the room's event
cache), room/player properties, joins, leaves and rejoining after a dropped connection.

It runs inside the revival server on the same port as everything else: a connection whose first
byte is 0xFB is a Photon client and is handed to serve(); anything else is ordinary HTTP.

Nothing here is shared with Photon: no code, no accounts, no servers of theirs.
"""
import base64
import hashlib
import json
import logging
import os
import random
import re
import struct
import threading
import time
import uuid


# ---- hot reload (2026-10-20)
# The game server reloads this file when it changes on disk (warpforge_server.current_services), like playfab_emu.py.
# importlib.reload runs the file again in the same module namespace, so every module-level table would start empty
# and every live connection, room and record would keep the old classes. _keep() hands the old value back for the
# tables that must survive, and _adopt() at the end of the file moves the live objects onto the new classes, so
# the code that runs from then on is the new one (a connection's read loop itself stays the old one until that
# connection ends, which is at most one match). State that must not change shape between versions: see _adopt.
def _keep(name, make):
    old = globals().get(name)
    return old if old is not None else make()

log = logging.getLogger("photon")
try:                                              # kept in logs/photon.log as well as on the console
    _dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(_dir, exist_ok=True)
    # a new file each night, two weeks kept: the log names the addresses players connect from
    from logging.handlers import TimedRotatingFileHandler
    _file = TimedRotatingFileHandler(os.path.join(_dir, "photon.log"), when="midnight", backupCount=14, encoding="utf-8")
    _file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(_file)
    log.setLevel(logging.INFO)
except OSError:
    pass

# ----------------------------------------------------------------------------- values
# Photon distinguishes byte / short / int / long and float / double; a relay must give back
# exactly what it was given. Python ints are Photon ints, str/bool/bytes/None/dict (Hashtable)
# and list (object[]) map directly, and everything else is wrapped.


class T(object):
    """A typed scalar: T('b', 255) is the byte 255, T('k', ..) short, 'l' long, 'f' float, 'd' double."""
    __slots__ = ("t", "v")

    def __init__(self, t, v):
        self.t, self.v = t, v

    def __eq__(self, o):
        return isinstance(o, T) and o.t == self.t and o.v == self.v

    def __hash__(self):
        return hash((self.t, self.v))

    def __repr__(self):
        return "%s:%r" % (self.t, self.v)


def B(v):
    return T("b", v)


class IntArr(list):       # int[]
    pass


class StrArr(list):       # string[]
    pass


class Arr(object):        # typed array: element type code + elements (or, for custom types, the custom code too)
    def __init__(self, et, items, custom=None, dict_types=None):
        self.et, self.items, self.custom, self.dict_types = et, items, custom, dict_types


class Dict(object):       # Dictionary<K,V>: declared key/value type codes (0 = object) + pairs
    def __init__(self, kt, vt, pairs):
        self.kt, self.vt, self.pairs = kt, vt, pairs


class Custom(object):     # a registered custom type (Vector3, PhotonPlayer, ...): kept as its bytes
    def __init__(self, code, data):
        self.code, self.data = code, data


class Raw(object):        # already-serialised value, type byte included: passed through untouched
    def __init__(self, data):
        self.data = data


def plain(v):
    """A value without its Photon type, for comparisons."""
    return v.v if isinstance(v, T) else v


class Reader(object):
    def __init__(self, buf, pos=0):
        self.b, self.p = buf, pos

    def take(self, n):
        if self.p + n > len(self.b):
            raise ValueError("message cut short")
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def u8(self):
        return self.take(1)[0]

    def i16(self):
        return struct.unpack(">h", self.take(2))[0]

    def i32(self):
        return struct.unpack(">i", self.take(4))[0]

    def value(self, t=None):
        if t is None:
            t = self.u8()
        if t in (42, 0):
            return None
        if t == 98:
            return T("b", self.u8())
        if t == 111:
            return self.u8() != 0
        if t == 107:
            return T("k", self.i16())
        if t == 105:
            return self.i32()
        if t == 108:
            return T("l", struct.unpack(">q", self.take(8))[0])
        if t == 102:
            return T("f", struct.unpack(">f", self.take(4))[0])
        if t == 100:
            return T("d", struct.unpack(">d", self.take(8))[0])
        if t == 115:
            return self.take(self.i16() & 0xFFFF).decode("utf-8", "replace")
        if t == 120:
            return bytes(self.take(self.i32()))
        if t == 110:
            return IntArr(self.i32() for _ in range(self.i32()))
        if t == 97:
            return StrArr(self.value(115) for _ in range(self.i16()))
        if t == 104:
            out = {}
            for _ in range(self.i16()):
                k = self.value()
                out[k] = self.value()
            return out
        if t == 122:
            return [self.value() for _ in range(self.i16())]
        if t == 99:
            code = self.u8()
            return Custom(code, bytes(self.take(self.i16() & 0xFFFF)))
        if t == 68:
            kt, vt = self.u8(), self.u8()
            if vt in (68, 121):
                raise ValueError("nested dictionary types are not supported")
            return Dict(kt, vt, self._pairs(kt, vt))
        if t == 121:
            n, et = self.i16(), self.u8()
            if et == 121:
                return Arr(et, [self.value(121) for _ in range(n)])
            if et == 120:
                return Arr(et, [bytes(self.take(self.i32())) for _ in range(n)])
            if et == 99:
                code = self.u8()
                return Arr(et, [bytes(self.take(self.i16() & 0xFFFF)) for _ in range(n)], custom=code)
            if et == 68:
                kt, vt = self.u8(), self.u8()
                if vt in (68, 121):
                    raise ValueError("nested dictionary types are not supported")
                return Arr(et, [self._pairs(kt, vt) for _ in range(n)], dict_types=(kt, vt))
            return Arr(et, [self.value(et) for _ in range(n)])
        if t in (101, 112, 113):
            raise ValueError("nested operation/event values are not supported")
        raise ValueError("unknown value type %d" % t)

    def _pairs(self, kt, vt):
        return [(self.value(kt or None), self.value(vt or None)) for _ in range(self.i16())]

    def params(self):
        """A parameter table: {code: value}; also remembers where each value sits in the message."""
        out, spans = {}, {}
        for _ in range(self.i16()):
            k = self.u8()
            start = self.p
            out[k] = self.value()
            spans[k] = (start, self.p)
        return out, spans


def write(out, v, typed=True):
    """Appends a value to the bytearray; `typed` adds its one-byte type code first."""
    def code(c):
        if typed:
            out.append(c)
    if isinstance(v, Raw):
        out += v.data
    elif v is None:
        out.append(42)
    elif isinstance(v, bool):
        code(111)
        out.append(1 if v else 0)
    elif isinstance(v, T):
        code({"b": 98, "k": 107, "l": 108, "f": 102, "d": 100}[v.t])
        out += {"b": lambda: bytes([v.v & 0xFF]), "k": lambda: struct.pack(">h", v.v), "l": lambda: struct.pack(">q", v.v),
                "f": lambda: struct.pack(">f", v.v), "d": lambda: struct.pack(">d", v.v)}[v.t]()
    elif isinstance(v, int):
        code(105)
        out += struct.pack(">i", v)
    elif isinstance(v, float):
        code(100)
        out += struct.pack(">d", v)
    elif isinstance(v, str):
        code(115)
        raw = v.encode("utf-8")
        out += struct.pack(">H", len(raw)) + raw
    elif isinstance(v, (bytes, bytearray)):
        code(120)
        out += struct.pack(">i", len(v)) + v
    elif isinstance(v, IntArr):
        code(110)
        out += struct.pack(">i", len(v))
        for i in v:
            out += struct.pack(">i", i)
    elif isinstance(v, StrArr):
        code(97)
        out += struct.pack(">h", len(v))
        for s in v:
            write(out, s, False)
    elif isinstance(v, dict):
        code(104)
        out += struct.pack(">h", len(v))
        for k, x in v.items():
            write(out, k)
            write(out, x)
    elif isinstance(v, list):
        code(122)
        out += struct.pack(">h", len(v))
        for x in v:
            write(out, x)
    elif isinstance(v, Custom):
        code(99)
        out.append(v.code)
        out += struct.pack(">H", len(v.data)) + v.data
    elif isinstance(v, Dict):
        code(68)
        out += bytes([v.kt, v.vt])
        _write_pairs(out, v.kt, v.vt, v.pairs)
    elif isinstance(v, Arr):
        code(121)
        out += struct.pack(">h", len(v.items))
        out.append(v.et)
        if v.et == 121:
            for x in v.items:
                write(out, x, False)
        elif v.et == 120:
            for x in v.items:
                out += struct.pack(">i", len(x)) + x
        elif v.et == 99:
            out.append(v.custom)
            for x in v.items:
                out += struct.pack(">H", len(x)) + x
        elif v.et == 68:
            out += bytes(v.dict_types)
            for pairs in v.items:
                _write_pairs(out, v.dict_types[0], v.dict_types[1], pairs)
        else:
            for x in v.items:
                write(out, x, False)
    else:
        raise TypeError("cannot serialise %r" % type(v))


def _write_pairs(out, kt, vt, pairs):
    out += struct.pack(">h", len(pairs))
    for k, x in pairs:
        write(out, k, kt == 0)
        write(out, x, vt == 0)


def _table(out, params):
    out += struct.pack(">h", len(params))
    for k, v in params.items():
        out.append(k)
        write(out, v)


def frame(payload):
    """A complete TCP message: 0xFB, total length, channel 0, reliable, then the payload."""
    return b"\xfb" + struct.pack(">i", len(payload) + 7) + b"\x00\x01" + bytes(payload)


def op_response(code, params=None, rc=0, debug=None, internal=False):
    out = bytearray(b"\xf3" + (b"\x07" if internal else b"\x03"))
    out.append(code)
    out += struct.pack(">h", rc)
    write(out, debug)
    _table(out, params or {})
    return frame(out)


def event(code, params):
    out = bytearray(b"\xf3\x04")
    out.append(code)
    _table(out, params)
    return frame(out)


def op_request(code, params, internal=False):          # only the test client sends these
    out = bytearray(b"\xf3" + (b"\x06" if internal else b"\x02"))
    out.append(code)
    _table(out, params)
    return frame(out)


# ----------------------------------------------------------------------------- matchmaking filter
# The game searches with filters like:  C1 < 1300 AND C1 >= 1100 AND C4 == 0 AND C5 == 14
_TOKEN = re.compile(r"""\s*(?:(?P<num>-?\d+(?:\.\d+)?)|"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<op>==|!=|<>|<=|>=|=|<|>|\(|\))|(?P<word>[A-Za-z_][A-Za-z0-9_]*))""")


def sql_match(expr, props):
    """True when a room with these (plain) custom properties passes the filter. A filter this
    cannot read matches nothing, as a broken query would on the real service."""
    if not expr or not expr.strip():
        return True
    toks, pos = [], 0
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN.match(expr, pos)
        if not m:
            return False
        pos = m.end()
        if m.group("num") is not None:
            toks.append(("v", float(m.group("num"))))
        elif m.group("dq") is not None:
            toks.append(("v", m.group("dq")))
        elif m.group("sq") is not None:
            toks.append(("v", m.group("sq")))
        elif m.group("op"):
            toks.append(("o", m.group("op")))
        else:
            w = m.group("word")
            toks.append(("k", w.upper()) if w.upper() in ("AND", "OR", "NOT") else ("p", w))
    state = {"i": 0}

    def peek():
        return toks[state["i"]] if state["i"] < len(toks) else (None, None)

    def take():
        t = peek()
        state["i"] += 1
        return t

    def operand():
        kind, v = take()
        if kind == "v":
            return v
        if kind == "p":
            x = props.get(v)
            if isinstance(x, bool):
                return 1.0 if x else 0.0
            return float(x) if isinstance(x, (int, float)) else x
        raise ValueError("expected a value")

    def comparison():
        if peek() == ("o", "("):
            take()
            v = either()
            if take() != ("o", ")"):
                raise ValueError("missing )")
            return v
        if peek() == ("k", "NOT"):
            take()
            return not comparison()
        a = operand()
        kind, op = take()
        if kind != "o" or op in ("(", ")"):
            raise ValueError("expected a comparison")
        b = operand()
        if a is None or b is None:
            return False                                   # a property the room does not have: no match (SQL NULL)
        if isinstance(a, float) != isinstance(b, float):
            try:
                a, b = float(a), float(b)
            except (TypeError, ValueError):
                a, b = str(a), str(b)
        return {"=": a == b, "==": a == b, "!=": a != b, "<>": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

    def both():
        v = comparison()
        while peek() == ("k", "AND"):
            take()
            v = comparison() and v
        return v

    def either():
        v = both()
        while peek() == ("k", "OR"):
            take()
            v = both() or v
        return v

    try:
        v = either()
        return bool(v) and state["i"] >= len(toks)
    except (ValueError, TypeError, KeyError):
        return False


# ----------------------------------------------------------------------------- rooms
K_MAX, K_VISIBLE, K_OPEN, K_LOBBY_PROPS, K_PLAYER_TTL, K_EMPTY_TTL = B(255), B(254), B(253), B(250), B(246), B(245)
A_NAME, A_INACTIVE = B(255), B(254)
ERR_FULL, ERR_CLOSED, ERR_EXISTS, ERR_NO_MATCH, ERR_NO_GAME, ERR_NO_REJOIN = 32765, 32764, 32766, 32760, 32758, 32748
ERR_AUTH = 32767                              # "invalid authentication": the client gives up and disconnects

# Limits, so no client can make the service hold rooms or memory for ever. The game itself keeps a
# dropped player's seat for 30 seconds and caches a handful of events per match.
TTL_MAX_MS = 600000                           # a seat or an empty room is kept at most 10 minutes
TICKET_GRACE = 15                             # seconds a connection may stay without a valid match ticket (when required)
ROOM_IDLE_MAX = 15 * 60                       # a room with nobody connected closes after 15 minutes regardless
CACHE_MAX_EVENTS = 1000                       # remembered events per room
CACHE_MAX_BYTES = 4 * 1024 * 1024             # and their total size
MAX_ROOMS_PER_PLAYER = 5                      # open rooms one signed-in player has created
MAX_ROOMS = 1000                              # open rooms in all
PROPS_MAX_KEYS = 200                          # properties one room, or one player in a room, can hold
PROPS_MAX_BYTES = 256 * 1024                  # and their total size as sent


def _props_fit(props):
    """Whether a room's or a player's properties stay within the limits above."""
    if len(props) > PROPS_MAX_KEYS:
        return False
    out = bytearray()
    try:
        write(out, props)
    except Exception:
        return False
    return len(out) <= PROPS_MAX_BYTES


def _ttl(v):
    """A seat or empty-room keep time from a client, held to 0 .. TTL_MAX_MS (-1, "for ever", becomes the most)."""
    return TTL_MAX_MS if v < 0 else min(v, TTL_MAX_MS)

# ---- who is connecting
# The match client's own sign-in is encrypted and says nothing this service can check, so the mod
# asks the game server for a match ticket over https (Revival/MatchTicket, with its session) and
# sends it here as operation OP_TICKET just before signing in. When the server requires accounts
# (REQUIRE, set by the game server from "requireAccount"), a connection without a valid ticket is
# refused, and a player's seat in a room belongs to the signed-in player, not to whatever name a
# client sends.
OP_TICKET = 160
TICKET_SECONDS = 4 * 3600      # from grant and from every use (2026-10-21: a 21-minute match outlived a 30-minute ticket
                               # fetched at sign-in; the game then reconnected with it and was refused once)
REQUIRE = _keep("REQUIRE", lambda: [True])   # closed until the game server has read its settings (it sets this at start-up)
# Tickets are kept by fingerprint (sha256, never the ticket itself) and, once the game server has set
# TICKET_FILE (data/match-tickets.json), on disk too, so a restart of the server does not turn away the games
# that were already open (before 2026-10-17 they were refused every 2 s until their ticket ran out).
TICKET_FILE = _keep("TICKET_FILE", lambda: [None])
MATCH_CLOSED = _keep("MATCH_CLOSED", lambda: [None])       # playfab_emu sets a function(record id) run when a recorded room closes
_tickets = _keep("_tickets", dict)            # sha256(ticket) -> (player id, expires at, unix seconds)
_tickets_lock = _keep("_tickets_lock", threading.Lock)
_tickets_loaded = _keep("_tickets_loaded", lambda: [None])      # the file the tickets were last read from

# ---- who goes first (2026-10-19)
# Every playable warlord has the same initiative, so the game decides who goes first by a coin: the room
# creator's draw from the shared random table in matchmaking, the challenger's own toss in a friend challenge.
# Friend challenges with custom rules showed the two games can take different routes and both start first,
# which breaks the match on the first move. So the server tosses the coin instead: when the second player
# joins a room it writes the room property "RevivalFirst" = the player id that goes first; the joiner gets it
# in the join reply and the creator by a properties-changed event, before either sees the other arrive, and
# the mod (0.14.17+) makes both games follow it (a challenge's own "Goes first" rule still wins there).
# Only when BOTH players run a mod that follows it: a mod that does not would keep the game's own coin while
# its opponent followed the server's, so the property is written only when both tickets say 0.14.17 or newer
# (the mod says its version when asking for its match ticket). Older pairs play exactly as before.
FIRST_KEY = "RevivalFirst"
FIRST_MIN_MOD = (0, 14, 17)
# "RevivalMod" (2026-10-20) = the LOWER of the two players' mod versions ("0.14.31"), written with the coin, so
# the mod can switch on a change both games must make together (skulls in friend matches, 0.14.31) only when
# the opponent's mod makes it too. Absent when either player's version is unknown (an older mod).
PAIR_KEY = "RevivalMod"
# The server-deal test (2026-10-20, mod 0.14.34 ServerDeal.cs; plan: claude/server-deal-plan.md). A tester's friend
# challenge with the rule "serverDeal" puts the pair in DEAL_PAIRS (playfab_emu ChatPrivate). When their match room
# seats both and both mods are new enough, the room gets "RevivalDeal" = 1 and "RevivalRng" = the server's own random
# table (100 numbers 0..999, the game's shape), which the creator's game sends instead of its own and the joiner's
# checks. Step 1 of the plan: a table nobody picked; later steps deal the cards.
DEAL_KEY = "RevivalDeal"
RNG_KEY = "RevivalRng"
DEAL_MIN_MOD = (0, 14, 34)
DEAL_PAIRS = _keep("DEAL_PAIRS", dict)        # (challenger id, friend id) -> until (time.time())
DEAL_EVENTS = _keep("DEAL_EVENTS", list)      # game modes that are server-deal tests ("serverDeal" in GameModes.json; playfab_emu fills it)
DEAL_EVENTS_REFRESH = _keep("DEAL_EVENTS_REFRESH", lambda: [None])   # playfab_emu sets a function that re-reads GameModes.json when it changed
_sysrand = random.SystemRandom()


def deal_lobby(lobby):
    """The game mode of a matchmaking lobby when it is a server-deal test, else None. The game names the lobby
    after the mode: 'Everguild.LiveOps.RankedV3EventEventRoom#RevivalClassic@r1-0.1.0'."""
    try:
        if DEAL_EVENTS_REFRESH[0]:
            DEAL_EVENTS_REFRESH[0]()
        name = lobby[0] if isinstance(lobby, tuple) and lobby else ""
        m = re.search(r"#([A-Za-z0-9_.-]+)@", str(name))
        eid = m.group(1) if m else None
        return eid if eid and eid in DEAL_EVENTS else None
    except Exception:
        log.exception("deal lobby %r", lobby)
        return None


def deal_event(room):
    """The game mode of a matchmaking room when it is a server-deal test, else None."""
    return deal_lobby(getattr(room, "lobby", None))


def deal_pair(a, b):
    """True while a server-deal challenge between the two is open (either way round)."""
    now = time.time()
    for k in [k for k, until in DEAL_PAIRS.items() if until < now]:
        DEAL_PAIRS.pop(k, None)
    return DEAL_PAIRS.get((a, b), 0) >= now or DEAL_PAIRS.get((b, a), 0) >= now
_mods = _keep("_mods", dict)                  # player id -> mod version tuple, from the match ticket request


def _version_tuple(text):
    m = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def mod_of(player_id):
    """The mod version (tuple) a signed-in player said when asking for their match ticket, or None."""
    with _tickets_lock:
        return _mods.get(player_id)


def _fingerprint(ticket):
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _tickets_sync():
    """Reads the kept tickets once per file (call with _tickets_lock held)."""
    path = TICKET_FILE[0]
    if not path or _tickets_loaded[0] == path:
        return
    _tickets_loaded[0] = path
    try:
        with open(path, "r", encoding="utf-8") as f:
            kept = json.load(f)
        now = time.time()
        for k, v in (kept.items() if isinstance(kept, dict) else []):
            if isinstance(v, list) and len(v) in (2, 3) and float(v[1]) > now:
                _tickets.setdefault(str(k), (str(v[0]), float(v[1])))
                if len(v) == 3 and _version_tuple(v[2]):           # the mod version the ticket was asked with
                    _mods.setdefault(str(v[0]), _version_tuple(v[2]))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("match tickets: could not read %s: %s", path, e)


def _tickets_save():
    """Writes the tickets still valid (call with _tickets_lock held)."""
    path = TICKET_FILE[0]
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: [who, until, ".".join(str(n) for n in _mods[who])] if who in _mods else [who, until]
                   for k, (who, until) in _tickets.items()}, f)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("match tickets: could not write %s: %s", path, e)


def grant(player_id, mod=None):
    """A match ticket for a signed-in player (valid TICKET_SECONDS, usable for every connection).

    mod: the version the mod said in its request ("0.14.17-w"); it decides whether the server picks who goes
    first for this player's matches (see FIRST_MIN_MOD). A ticket asked for without one counts as an older mod."""
    ticket = uuid.uuid4().hex + uuid.uuid4().hex
    now = time.time()
    with _tickets_lock:
        _tickets_sync()
        for k in [k for k, (_, until) in _tickets.items() if until < now]:
            del _tickets[k]
        _tickets[_fingerprint(ticket)] = (player_id, now + TICKET_SECONDS)
        version = _version_tuple(mod)
        if version:
            _mods[player_id] = version
        else:
            _mods.pop(player_id, None)
        _tickets_save()
    return ticket


def _redeem(ticket):
    if not isinstance(ticket, str) or not ticket:
        return None
    now = time.time()
    with _tickets_lock:
        _tickets_sync()
        key = _fingerprint(ticket)
        found = _tickets.get(key)
        if found and found[1] >= now:
            # a ticket in use stays valid: its life starts again at every connection, so an open game is never
            # turned away because its sign-in was long ago (saved with the next grant, which is good enough)
            _tickets[key] = (found[0], now + TICKET_SECONDS)
            return found[0]
    return None
FLAG_DELETE_NULL, FLAG_PROPS_TO_ALL = 16, 32


class Actor(object):
    def __init__(self, nr, conn, props):
        self.nr, self.conn, self.props = nr, conn, props
        self.player = conn.player if conn is not None else None    # the signed-in player, when known
        self.inactive_since = None
        self.groups = None                    # None: only group 0 (default); set of ints; "all"

    @property
    def name(self):
        return plain(self.props.get(A_NAME)) or ""


class Room(object):
    def __init__(self, name, lobby):
        self.name, self.lobby = name, lobby
        self.props = {}
        self.actors = {}                      # nr -> Actor
        self.next_nr = 1
        self.cache = []                       # [(actor nr, code, raw data or None, decoded data)]
        self.cleanup = True
        self.flags = 0
        self.player_ttl = 0
        self.empty_ttl = 0
        self.empty_since = None
        self.reserved = []                    # times at which a slot was promised to a searching player
        self.creator = None                   # the signed-in player who opened it, when known
        self.cache_full_noted = False
        self.record = None                    # its MatchRecord (see below)

    def active(self):
        return [a for a in self.actors.values() if a.conn is not None]

    @property
    def max_players(self):
        v = plain(self.props.get(K_MAX))
        return v if isinstance(v, int) and not isinstance(v, bool) else 0

    def flag(self, key, default=True):
        v = self.props.get(key)
        return v if isinstance(v, bool) else default

    def custom(self):
        return {k: plain(v) for k, v in self.props.items() if isinstance(k, str)}

    def has_space(self, now):
        self.reserved = [t for t in self.reserved if now - t < 15]
        return self.max_players <= 0 or len(self.actors) + len(self.reserved) < self.max_players


# ----------------------------------------------------------------------------- match records
# Every match is written down as the relay saw it: who joined, every event each player sent (its
# serialised data, untouched) and property changes, with the time since the room opened. The copy
# is the server's own, so a player's game cannot leave things out of it the way it could with the
# recording it uploads; admins use it to review reported matches. One file per match,
# data/matches/<YYYY-MM>/<id>.jsonl, one JSON object per line. The revival server sets MATCH_DIR at
# start-up (without it nothing is written) and deletes records after MATCH_KEEP_DAYS unless a
# replay code or a report holds them (playfab_emu). Nothing personal beyond player ids and names.
MATCH_DIR = _keep("MATCH_DIR", lambda: [None])
MATCH_MAX_LINES = 20000                       # per match; a record that reaches either cap stops,
MATCH_MAX_BYTES = 4 * 1024 * 1024             # with a last line saying so
RECENT_MATCHES = _keep("RECENT_MATCHES", list)   # newest last: {"id", "players": set, "opened", "last"}, at most 500
MATCH_ID = re.compile(r"[0-9a-f]{16}")


class MatchRecord(object):
    """The record of one room. Joins are kept in memory until the room's first event, so rooms in
    which nothing happened (a search nobody answered) leave no file."""

    def __init__(self, room):
        self.room_name = room.name
        self.lobby = str(room.lobby[0]) if isinstance(room.lobby, tuple) and room.lobby else ""
        self.id = uuid.uuid4().hex[:16]
        self.t0 = time.monotonic()
        self.opened = time.time()
        self.pending = []
        self.f = None
        self.lines = self.size = 0
        self.stopped = False
        self.players = set()
        self.info = None
        self.recent = None

    def note(self, kind, **fields):
        line = dict(t=int((time.monotonic() - self.t0) * 1000), k=kind, **{k: v for k, v in fields.items() if v is not None})
        if kind == "ev" and self.f is None and not self.stopped:
            self._open()
        if self.f is None:
            if not self.stopped and len(self.pending) < 200:
                self.pending.append(line)
            return
        self._write(line)

    def _open(self):
        base = MATCH_DIR[0]
        if not base:
            self.stopped = True
            return
        try:
            folder = os.path.join(base, time.strftime("%Y-%m", time.gmtime(self.opened)))
            os.makedirs(folder, exist_ok=True)
            self.f = open(os.path.join(folder, self.id + ".jsonl"), "a", encoding="utf-8")
        except OSError as e:
            log.warning("match record %s: cannot write (%s)", self.id, e)
            self.stopped = True
            return
        self._write({"t": 0, "k": "open", "id": self.id, "room": self.room_name[:200], "lobby": self.lobby[:200],
                     "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.opened)), "info": self.info})
        for line in self.pending:
            self._write(line)
        self.pending = []
        self.recent = {"id": self.id, "players": self.players, "opened": self.opened, "last": time.time()}
        RECENT_MATCHES.append(self.recent)
        del RECENT_MATCHES[:-500]

    def _write(self, line):
        if self.f is None:
            return
        text = json.dumps(line, separators=(",", ":"), default=str) + "\n"
        if self.lines >= MATCH_MAX_LINES or self.size + len(text) > MATCH_MAX_BYTES:
            if line.get("k") != "full":
                self._write_raw(json.dumps({"t": line.get("t"), "k": "full"}) + "\n")
            self.close()
            self.stopped = True
            return
        self._write_raw(text)
        self.lines += 1
        self.size += len(text)
        if self.recent is not None:
            self.recent["last"] = time.time()

    def _write_raw(self, text):
        try:
            self.f.write(text)
            self.f.flush()
        except (OSError, ValueError) as e:
            log.warning("match record %s: write failed (%s)", self.id, e)
            self.close()
            self.stopped = True

    def close(self):
        if self.f is not None:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None


def _b64(data):
    return base64.b64encode(bytes(data)).decode("ascii")


def find_match(player_ids, since_seconds=3 * 3600):
    """The id of the most recent recorded match that every one of `player_ids` took part in, or None."""
    want = {p for p in player_ids if p}
    now = time.time()
    for e in reversed(RECENT_MATCHES):
        if now - e["last"] > since_seconds:
            continue
        if want and want <= e["players"]:
            return e["id"]
    return None


class Service(object):
    """All rooms of this server. One instance, shared by every connection."""

    def __init__(self):
        self.lock = threading.RLock()
        self.rooms = {}
        self.conns = set()
        self.started = time.monotonic()
        threading.Thread(target=self._janitor, name="photon-janitor", daemon=True).start()

    def _record(self, room, kind, **fields):
        """Notes something in the room's match record (made on first use). Never raises."""
        try:
            if room.record is None:
                if kind == "close" or MATCH_DIR[0] is None:
                    return
                room.record = MatchRecord(room)
                room.record.info = {k: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:120])
                                    for k, v in list(room.custom().items())[:40]}
            rec = room.record
            if kind == "join" and fields.get("p"):
                rec.players.add(fields["p"])
            if kind == "close":
                rec.note("close")
                rec.close()
                if MATCH_CLOSED[0] and rec.players:
                    try:
                        MATCH_CLOSED[0](rec.id)                     # playfab_emu checks the record (its own thread)
                    except Exception:
                        log.exception("match record %s: closed hook", rec.id)
                return
            rec.note(kind, **fields)
        except Exception:
            log.exception("match record")

    def now_ms(self):
        return int((time.monotonic() - self.started) * 1000) & 0x7FFFFFFF

    def stats(self):
        with self.lock:
            return {"connections": len(self.conns), "rooms": len(self.rooms),
                    "players": sum(len(r.active()) for r in self.rooms.values())}

    # -- housekeeping: players who dropped and did not come back, rooms nobody is in
    def _janitor(self):
        while True:
            time.sleep(1.0)
            try:
                now = time.monotonic()
                with self.lock:
                    for room in list(self.rooms.values()):
                        for a in list(room.actors.values()):
                            if a.conn is None and room.player_ttl >= 0 and (now - a.inactive_since) * 1000 >= room.player_ttl:
                                self._remove_actor(room, a)
                        self._maybe_close(room, now)
            except Exception:
                log.exception("janitor")

    def _maybe_close(self, room, now):
        if room.active():
            room.empty_since = None
            return
        if room.empty_since is None:
            room.empty_since = now
        if room.actors and now - room.empty_since >= ROOM_IDLE_MAX:
            room.actors.clear()                  # only dropped players left, for far too long
            room.empty_ttl = 0
        if not room.actors and (now - room.empty_since) * 1000 >= room.empty_ttl:
            if self.rooms.get(room.name) is room:
                del self.rooms[room.name]
                log.info("room %s closed", room.name)
                self._record(room, "close")

    # -- sending
    def _send_room(self, room, data, skip=None, only=None):
        for a in room.active():
            if a.nr != skip and (only is None or a.nr in only):
                a.conn.send(data)

    def _actor_list(self, room):
        return IntArr(sorted(room.actors))

    def _remove_actor(self, room, actor, inactive=False):
        """A player leaves for good (or, with `inactive`, drops but may come back)."""
        if inactive:
            actor.conn, actor.inactive_since = None, time.monotonic()
            actor.props[A_INACTIVE] = True
            self._send_room(room, event(254, {254: actor.nr, 233: True, 252: self._actor_list(room)}))
            log.info("room %s: %s (actor %d) dropped; slot kept for %s ms", room.name, actor.name, actor.nr, room.player_ttl)
            self._record(room, "drop", a=actor.nr)
        else:
            room.actors.pop(actor.nr, None)
            actor.conn = None
            if room.cleanup:
                room.cache = [c for c in room.cache if c[0] != actor.nr]
            self._send_room(room, event(254, {254: actor.nr, 252: self._actor_list(room)}))
            log.info("room %s: %s (actor %d) left; %d still in", room.name, actor.name, actor.nr, len(room.actors))
            self._record(room, "leave", a=actor.nr)
        self._maybe_close(room, time.monotonic())

    def drop(self, conn):
        """The connection closed. In a room with a rejoin window the seat is kept."""
        with self.lock:
            self.conns.discard(conn)
            room, actor = conn.room, conn.actor
            conn.room = conn.actor = None
            if room is not None and actor is not None and room.actors.get(actor.nr) is actor and actor.conn is conn:
                self._remove_actor(room, actor, inactive=room.player_ttl != 0 and not conn.left)

    # -- operations
    def join_random(self, conn, p):
        lobby = (p.get(213) or "", plain(p.get(212)) or 0)
        want = p.get(248) or {}
        sql = p.get(245) if isinstance(p.get(245), str) else ""
        # a server-deal test mode has one pool: the game's ranked / casual split ("C4 == 0|1", which can differ
        # between two testers of a mode neither has played) would keep two testers from finding each other
        if deal_lobby(lobby):
            stripped = re.sub(r"\s*AND\s+C4\s*==\s*\d+", "", sql, flags=re.I)
            if stripped != sql:
                log.info("matchmaking: %s searches a server-deal mode; the ranked/casual filter is ignored", conn.peer)
                sql = stripped
        now = time.monotonic()
        with self.lock:
            found = []
            for room in self.rooms.values():
                if room.lobby != lobby or not room.flag(K_OPEN) or not room.flag(K_VISIBLE) or not room.has_space(now):
                    continue
                if not room.active():
                    continue                                    # only people who dropped are in it
                custom = room.custom()
                if any(isinstance(k, str) and custom.get(k) != plain(v) for k, v in want.items()):
                    continue
                if K_MAX in want and plain(want[K_MAX]) and room.max_players != plain(want[K_MAX]):
                    continue
                if lobby[1] == 2 and not sql_match(sql, custom):
                    continue
                found.append(room)
            if not found:
                return conn.reply(225, rc=ERR_NO_MATCH, debug="No match found")
            room = max(found, key=lambda r: len(r.actors))       # fill rooms up
            room.reserved.append(now)
            log.info("matchmaking: %s -> room %s", conn.peer, room.name)
            conn.reply(225, {255: room.name, 230: conn.address})

    def create_or_join(self, conn, code, p):
        name = p.get(255) if isinstance(p.get(255), str) else ""
        on_game_server = 249 in p or 248 in p or 250 in p
        mode = plain(p.get(215)) or 0                            # 1 create if missing, 2 join or rejoin, 3 rejoin only
        with self.lock:
            room = self.rooms.get(name)
            if not on_game_server:
                # The "master" step: the client only asks where to go. It reconnects and asks again.
                if code == 227:
                    if not name:
                        name = str(uuid.uuid4())
                    elif room is not None:
                        return conn.reply(code, rc=ERR_EXISTS, debug="A game with the specified id already exist.")
                elif room is None and mode != 1:
                    return conn.reply(code, rc=ERR_NO_GAME, debug="Game does not exist")
                return conn.reply(code, {255: name, 230: conn.address})

            if conn.room is not None:
                return conn.reply(code, rc=-3, debug="already in a room")
            props = p.get(249) if isinstance(p.get(249), dict) else {}
            if not _props_fit(props) or (isinstance(p.get(248), dict) and not _props_fit(p[248])):
                return conn.reply(code, rc=-3, debug="too many properties")
            nick = plain(props.get(A_NAME)) or ""
            if room is None:
                if code == 226 and mode != 1:
                    return conn.reply(code, rc=ERR_NO_GAME, debug="Game does not exist")
                # per player only when signed in: without a ticket every player behind a relay shares one address
                creator = conn.player
                if len(self.rooms) >= MAX_ROOMS or (creator is not None and
                        sum(1 for r in self.rooms.values() if r.creator == creator) >= MAX_ROOMS_PER_PLAYER):
                    log.warning("%s: room refused, too many open rooms", conn.peer)
                    return conn.reply(code, rc=ERR_FULL, debug="Too many open games")
                room = self._new_room(name or str(uuid.uuid4()), p)
                room.creator = creator
            elif code == 227:
                return conn.reply(code, rc=ERR_EXISTS, debug="A game with the specified id already exist.")

            # Someone who dropped out of this room and is coming back keeps their seat: matched by the
            # signed-in player when known (a name alone can be copied), otherwise by name.
            if conn.player:
                back = next((a for a in room.actors.values() if a.player == conn.player), None)
            else:
                back = next((a for a in room.actors.values() if nick and not a.player and a.name == nick), None)
            if back is not None and back.conn is not None:
                # Same player, new connection: the old one died without the server noticing yet.
                old = back.conn
                old.room = old.actor = None
                old.closed = True
                try:
                    old.sock.close()
                except OSError:
                    pass
                back.conn = None
            if back is None and mode == 3:
                return conn.reply(code, rc=ERR_NO_REJOIN, debug="Rejoiner not found")
            if back is None:
                if not room.flag(K_OPEN):
                    return conn.reply(code, rc=ERR_CLOSED, debug="Game closed")
                if room.max_players > 0 and len(room.actors) >= room.max_players:
                    return conn.reply(code, rc=ERR_FULL, debug="Game full")
                actor = Actor(room.next_nr, conn, dict(props))
                room.next_nr += 1
                room.actors[actor.nr] = actor
                if room.reserved:
                    room.reserved.pop(0)
            else:
                merged = dict(back.props)
                merged.update(props)
                if not _props_fit(merged):
                    return conn.reply(code, rc=-3, debug="too many properties")
                actor = back
                actor.conn, actor.inactive_since = conn, None
                actor.props.pop(A_INACTIVE, None)
                actor.props.update(props)
            conn.room, conn.actor = room, actor
            room.empty_since = None
            if back is None:
                self._pick_first(room, actor)
            others = {a.nr: dict(a.props) for a in room.actors.values() if a.nr != actor.nr}
            conn.reply(code, {254: actor.nr, 248: dict(room.props), 249: others, 252: self._actor_list(room)})
            self._send_room(room, event(255, {254: actor.nr, 249: dict(actor.props), 252: self._actor_list(room)}))
            for sender, ev_code, raw, _ in room.cache:           # what the room has been told so far
                params = {254: sender}
                if raw is not None:
                    params[245] = Raw(raw)
                conn.send(event(ev_code, params))
            log.info("room %s: %s %s as actor %d (%d in room)", room.name, actor.name or conn.peer,
                     "is back" if back else "joined", actor.nr, len(room.actors))
            self._record(room, "back" if back else "join", a=actor.nr, p=actor.player or conn.player, n=(actor.name or "")[:40])

    def _pick_first(self, room, joined):
        """A new player has just joined (call with the lock held): once two signed-in players are in, toss the
        coin for who goes first and tell the room (see FIRST_KEY above). Tossed again for every new pairing
        (a player who left and someone else took the seat); a player coming back keeps the toss."""
        try:
            seated = [a for a in room.actors.values() if a.player]
            if len(room.actors) != 2 or len(seated) != 2:
                room.props.pop(FIRST_KEY, None)
                room.props.pop(PAIR_KEY, None)
                room.props.pop(DEAL_KEY, None)
                room.props.pop(RNG_KEY, None)
                return
            players = [a.player for a in seated]
            mods = [mod_of(pl) for pl in players]
            pair = ".".join(str(n) for n in min(mods)) if all(mods) else None
            if pair:
                room.props[PAIR_KEY] = pair
            else:
                room.props.pop(PAIR_KEY, None)
            if any((m or (0,)) < FIRST_MIN_MOD for m in mods):
                if room.props.pop(FIRST_KEY, None) is not None:
                    log.info("room %s: who goes first left to the games (a mod older than %s)", room.name,
                             ".".join(str(n) for n in FIRST_MIN_MOD))
                return
            first = random.choice(players)
            room.props[FIRST_KEY] = first
            log.info("room %s: %s goes first (server's coin); the pair's mod is %s", room.name, first, pair)
            self._record(room, "first", p=first)
            # the one already in the room hears it now, before the join event; the joiner gets it in the join reply
            changed = {FIRST_KEY: first}
            if pair:
                changed[PAIR_KEY] = pair
            room.props.pop(DEAL_KEY, None)
            room.props.pop(RNG_KEY, None)
            why = "a tester's challenge" if deal_pair(players[0], players[1]) else None
            if why is None:
                ev = deal_event(room)
                if ev:
                    why = "the %s mode" % ev
            if why:
                if min(mods) >= DEAL_MIN_MOD:
                    table = " ".join(str(_sysrand.randrange(1000)) for _ in range(100))
                    room.props[DEAL_KEY] = 1
                    room.props[RNG_KEY] = table
                    changed[DEAL_KEY] = 1
                    changed[RNG_KEY] = table
                    log.info("room %s: server deal on, from %s (the server's random table %s...)", room.name, why, table[:24])
                    self._record(room, "deal", rng=table)
                else:
                    log.info("room %s: server deal asked for but a mod is older than %s; off", room.name, ".".join(str(n) for n in DEAL_MIN_MOD))
            self._send_room(room, event(253, {253: 0, 251: changed, 254: 0}), skip=joined.nr)
        except Exception:
            log.exception("room %s: who goes first", room.name)

    def _new_room(self, name, p):
        room = Room(name, (p.get(213) or "", plain(p.get(212)) or 0))
        game_props = p.get(248) if isinstance(p.get(248), dict) else {}
        room.props = dict(game_props)
        if isinstance(p.get(241), bool):
            room.cleanup = p[241]
        room.flags = plain(p.get(191)) or 0
        if isinstance(plain(p.get(235)), int) and not isinstance(plain(p.get(235)), bool):
            room.player_ttl = _ttl(plain(p[235]))
        if isinstance(plain(p.get(236)), int) and not isinstance(plain(p.get(236)), bool):
            room.empty_ttl = _ttl(plain(p[236]))
        self._ttl_from_props(room)
        self.rooms[name] = room
        log.info("room %s opened (lobby %r, max %s, custom %r)", name, room.lobby, room.max_players or "any", room.custom())
        return room

    @staticmethod
    def _ttl_from_props(room):
        for key, attr in ((K_PLAYER_TTL, "player_ttl"), (K_EMPTY_TTL, "empty_ttl")):
            v = plain(room.props.get(key))
            if isinstance(v, int) and not isinstance(v, bool):
                setattr(room, attr, _ttl(v))

    def leave(self, conn, p):
        with self.lock:
            room, actor = conn.room, conn.actor
            conn.left = True
            conn.room = conn.actor = None
            conn.reply(254, {})
            if room is not None and actor is not None and room.actors.get(actor.nr) is actor:
                self._remove_actor(room, actor, inactive=p.get(233) is True and room.player_ttl != 0)

    def raise_event(self, conn, p, raw):
        with self.lock:
            room, actor = conn.room, conn.actor
            if room is None or actor is None:
                return
            code = plain(p.get(244)) or 0
            cache = plain(p.get(247)) or 0
            targets = list(p[252]) if isinstance(p.get(252), IntArr) else None
            data = p.get(245)
            if cache in (6, 7):                                  # take things out of the room's cache; nothing is sent
                if cache == 7:
                    room.cache = [c for c in room.cache if c[0] in room.actors or c[0] == 0]
                else:
                    def doomed(c):
                        if code and c[1] != code:
                            return False
                        if targets is not None and c[0] not in targets:
                            return False
                        if isinstance(data, dict):
                            return isinstance(c[3], dict) and all(k in c[3] and _same(c[3][k], v) for k, v in data.items())
                        return True
                    room.cache = [c for c in room.cache if not doomed(c)]
                return
            params = {254: actor.nr}
            if raw is not None:
                params[245] = Raw(raw)
            msg = event(code, params)
            if cache in (1, 2):                                  # one remembered event per player and code
                room.cache = [c for c in room.cache if not (c[0] == actor.nr and c[1] == code)]
            if cache in (1, 2, 4, 5):
                if len(room.cache) < CACHE_MAX_EVENTS and \
                        sum(len(c[2]) for c in room.cache if c[2]) + len(raw or b"") <= CACHE_MAX_BYTES:
                    room.cache.append((0 if cache == 5 else actor.nr, code, raw, data))
                elif not room.cache_full_noted:
                    room.cache_full_noted = True
                    log.warning("room %s: event cache full; further events are sent but not remembered", room.name)
            elif cache == 3:
                room.cache = [c for c in room.cache if not (c[0] == actor.nr and c[1] == code)]
            group = plain(p.get(240)) or 0
            receivers = plain(p.get(246)) or 0                   # 0 everyone else, 1 everyone, 2 the master client
            if targets is not None:
                only = set(targets)
            elif receivers == 2:
                act = room.active()
                only = {min(a.nr for a in act)} if act else set()
            else:
                only = None
            for a in room.active():
                if only is not None:
                    if a.nr not in only:
                        continue
                elif a.nr == actor.nr and receivers != 1:
                    continue
                elif group and a.nr != actor.nr and not (a.groups == "all" or (a.groups and group in a.groups)):
                    continue
                a.conn.send(msg)
            self._record(room, "ev", a=actor.nr, c=code, d=_b64(raw) if raw is not None else None,
                         to=targets, r=receivers or None, g=group or None)

    def set_properties(self, conn, p):
        with self.lock:
            room, actor = conn.room, conn.actor
            props = p.get(251)
            if room is None or actor is None or not isinstance(props, dict):
                return conn.reply(252, rc=-3, debug="not in a room")
            target_nr = plain(p.get(254)) or 0
            store = room.props
            if target_nr:
                target = room.actors.get(target_nr)
                if target is None:
                    return conn.reply(252, rc=-3, debug="no such player")
                store = target.props
            expected = p.get(231)
            if isinstance(expected, dict) and any(not _same(store.get(k), v) for k, v in expected.items()):
                return conn.reply(252, rc=32742, debug="the properties changed in the meantime")
            changed = dict(store)
            for k, v in props.items():
                if v is None and room.flags & FLAG_DELETE_NULL:
                    changed.pop(k, None)
                else:
                    changed[k] = v
            if not _props_fit(changed):
                log.warning("room %s: %s refused, too many properties", room.name, conn.peer)
                return conn.reply(252, rc=-3, debug="too many properties")
            store.clear()
            store.update(changed)
            if not target_nr:
                self._ttl_from_props(room)
            conn.reply(252, {})
            try:
                packed = bytearray()
                write(packed, props)
                self._record(room, "props", a=actor.nr, of=target_nr or None, d=_b64(packed))
            except Exception:
                log.exception("match record: properties")
            msg = event(253, {253: target_nr, 251: props, 254: actor.nr})
            self._send_room(room, msg, skip=None if room.flags & FLAG_PROPS_TO_ALL else actor.nr)

    def change_groups(self, conn, p):
        with self.lock:
            actor = conn.actor
            if actor is None:
                return
            remove, add = p.get(239), p.get(238)
            cur = set() if actor.groups in (None, "all") else set(actor.groups)
            if isinstance(remove, (bytes, bytearray)):
                cur = set() if len(remove) == 0 else cur - set(remove)
                if actor.groups == "all" and len(remove):
                    cur = set(range(1, 256)) - set(remove)
            if isinstance(add, (bytes, bytearray)):
                if len(add) == 0:
                    actor.groups = "all"
                    return
                cur |= set(add)
            actor.groups = cur

    def get_properties(self, conn, p):
        with self.lock:
            room = conn.room
            if room is None:
                return conn.reply(251, rc=-3, debug="not in a room")
            conn.reply(251, {248: dict(room.props), 249: {a.nr: dict(a.props) for a in room.actors.values()}})


def _same(a, b):
    return plain(a) == plain(b)


def _brief(p):
    """Operation parameters for the log, short."""
    def one(v):
        if isinstance(v, dict):
            return "{%s}" % ", ".join("%s: %s" % (plain(k), one(x)) for k, x in list(v.items())[:12])
        if isinstance(v, (bytes, bytearray)):
            return "<%d bytes>" % len(v)
        if isinstance(v, (Arr, Dict, Custom, list)) and not isinstance(v, (IntArr, StrArr)):
            return "<%s>" % type(v).__name__
        text = repr(plain(v)) if not isinstance(v, (IntArr, StrArr)) else repr(list(v))
        return text if len(text) <= 70 else text[:67] + "..."
    return ", ".join("%d=%s" % (k, one(v)) for k, v in sorted(p.items()))


SERVICE = _keep("SERVICE", Service)
SEEN = _keep("SEEN", dict)          # player id -> when their game last asked the game server for chat (kept across reloads)
PRIVATE = _keep("PRIVATE", dict)    # private messages waiting for their reader (playfab_emu fills and drains it)

# Oakley group 1: the well-known prime the client's key exchange uses.
_PRIME = int("FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B"
             "302B0A6DF25F14374FE1356D6D51C245E485B576625E7EC6F44C42E9A63A3620FFFFFFFFFFFFFFFF", 16)


class Connection(object):
    """One Photon client. Runs on its own thread until the socket closes."""

    def __init__(self, sock, peer, service):
        self.sock, self.service = sock, service
        self.peer = "%s:%s" % (peer[0], peer[1])
        self.address = "revival:0"            # where clients are told to reconnect; the mod overrides the address anyway
        self.wlock = threading.Lock()
        self.room = self.actor = None
        self.left = False
        self.ready = False
        self.closed = False
        self.player = None                    # set by a valid match ticket

    def send(self, data):
        if self.closed:
            return
        try:
            with self.wlock:
                self.sock.sendall(data)
        except OSError:
            self.closed = True

    def reply(self, code, params=None, rc=0, debug=None):
        self.send(op_response(code, params, rc, debug))

    def _read(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("closed")
            buf += chunk
        return buf

    def run(self):
        self.sock.settimeout(30.0)            # the client pings every second; silence means it is gone
        with self.service.lock:
            self.service.conns.add(self)
        opened = time.monotonic()
        try:
            while not self.closed:
                if REQUIRE[0] and not self.player and time.monotonic() - opened > TICKET_GRACE:
                    # The game shows its ticket straight after connecting; a connection that never
                    # does is not a game and is not kept open by its pings.
                    log.info("%s: closed, no valid match ticket within %d s", self.peer, TICKET_GRACE)
                    break
                first = self._read(1)[0]
                if first == 0xF0:             # ping: answered with the server clock, which every client shares
                    stamp = self._read(4)
                    self.send(b"\xf0" + struct.pack(">i", self.service.now_ms()) + stamp)
                    continue
                if first != 0xFB:
                    raise ValueError("not a Photon message (0x%02x)" % first)
                head = self._read(6)
                length = struct.unpack(">i", head[:4])[0]
                if not 9 <= length <= 1 << 20:
                    raise ValueError("bad message length %d" % length)
                self.handle(self._read(length - 7))
        except (ConnectionError, OSError):
            pass
        except Exception as e:
            log.warning("%s: %s", self.peer, e)
        finally:
            self.closed = True
            self.service.drop(self)
            try:
                self.sock.close()
            except OSError:
                pass

    def handle(self, m):
        try:
            self._handle(m)
        except Exception as e:
            # One message this service cannot read must not end the match; keep what it looked like.
            log.warning("%s: could not handle a message (%s): %s", self.peer, e, bytes(m[:160]).hex())

    def _handle(self, m):
        if not self.ready or m[0] != 0xF3 or m[1] == 0:
            # The first message is the client's "init" (its exact shape varies); acknowledge it.
            self.ready = True
            log.info("%s connected (init %s)", self.peer, bytes(m[:48]).hex())
            return self.send(frame(b"\xf3\x01\x00"))
        kind = m[1]
        if kind & 0x80:
            # Encrypted: the game only encrypts its sign-in.
            return self.sign_in(230)
        r = Reader(m, 3)
        if kind == 6:                         # internal: key exchange, or a ping sent as an operation
            p, _ = r.params()
            if m[2] == 0:
                secret = int.from_bytes(os.urandom(32), "big")
                key = pow(22, secret, _PRIME).to_bytes(96, "big")
                return self.send(op_response(0, {1: key}, internal=True))
            if m[2] == 1:
                return self.send(op_response(1, {1: self.service.now_ms(), 2: p.get(1)}, internal=True))
            return
        if kind != 2:
            return
        code = m[2]
        p, spans = r.params()
        s = self.service
        if code not in (253, OP_TICKET):      # events are too many to log; a ticket must never be written down
            log.info("%s op %d %s", self.peer, code, _brief(p))
        if code == OP_TICKET:
            who = _redeem(p.get(1))
            if who:
                self.player = who
                log.info("%s is player %s", self.peer, who)
            else:
                log.info("%s sent a match ticket that is unknown or expired", self.peer)
            return
        if REQUIRE[0] and not self.player and code in (225, 226, 227, 229, 222):
            return self.reply(code, rc=ERR_AUTH, debug="Sign in to the game first")
        if code in (230, 231):
            self.sign_in(code)
        elif code == 229 or code == 228:      # join / leave lobby: rooms are found by searching, nothing to list
            self.reply(code, {})
        elif code == 225:
            s.join_random(self, p)
        elif code in (226, 227):
            s.create_or_join(self, code, p)
        elif code == 254:
            s.leave(self, p)
        elif code == 253:
            raw = bytes(m[spans[245][0]:spans[245][1]]) if 245 in spans else None
            s.raise_event(self, p, raw)
        elif code == 252:
            s.set_properties(self, p)
        elif code == 251:
            s.get_properties(self, p)
        elif code == 248:
            s.change_groups(self, p)
        elif code == 217:                     # room list of a lobby: not kept
            self.reply(code, {222: {}})
        elif code == 222:                     # find friends: nobody is tracked here
            names = p.get(1) if isinstance(p.get(1), StrArr) else StrArr()
            self.reply(code, {1: Arr(111, [False] * len(names)), 2: StrArr([""] * len(names))})
        elif code in (218, 221):              # settings, lobby statistics: nothing to say
            pass
        else:
            self.reply(code, rc=-2, debug="Unknown operation code")

    def sign_in(self, code):
        if REQUIRE[0] and not self.player:
            log.info("%s refused: no valid match ticket", self.peer)
            self.reply(code, rc=ERR_AUTH, debug="Sign in to the game first")
            return
        self.signed_in(code)

    def signed_in(self, code=230):
        st = self.service.stats()
        self.reply(code, {221: uuid.uuid4().hex})
        self.send(event(226, {229: st["players"], 227: max(0, st["connections"] - st["players"]), 228: st["rooms"]}))


def serve(sock, peer):
    """Entry point from the web server: this connection is a Photon client."""
    try:
        import socket
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # small messages; do not hold them back
    except OSError:
        pass
    Connection(sock, peer, SERVICE).run()


def _adopt():
    """After a reload: the live service, its rooms, actors, records and connections take the new classes."""
    svc = globals().get("SERVICE")
    if svc is None or type(svc) is Service:
        return
    moved = 0
    with svc.lock:
        svc.__class__ = Service
        for room in list(svc.rooms.values()):
            room.__class__ = Room
            for a in list(room.actors.values()):
                a.__class__ = Actor
            if room.record is not None:
                room.record.__class__ = MatchRecord
            moved += 1
        for c in list(svc.conns):
            c.__class__ = Connection
    log.info("photon_emu.py reloaded: %d room(s) and %d connection(s) carried over", moved, len(svc.conns))


_adopt()
