"""Minimal PlayFab Client API stand-in for Warpforge Revival.

The game's PlayFab SDK is redirected here by the mod (POST /playfab/<Api>/<Call>).
Every request is appended to logs/playfab.jsonl so missing calls are easy to spot.

Player state lives in data/players/<PlayFabId>.json; title-wide data in
content/titledata/*.json (one JSON object of key -> value per file; non-string
values are serialised to JSON strings, which is how PlayFab stores them).
"""
import copy
import binascii
import base64
import hashlib
import re
import hmac
import json
import logging
import os
import random
import shutil
import threading
import time
import unicodedata

import photon_emu
if not hasattr(photon_emu, "SEEN"):        # an older photon_emu (before a restart) has no table of its own
    photon_emu.SEEN = {}
if not hasattr(photon_emu, "PRIVATE"):     # private messages waiting for their reader (kept across reloads of this file)
    photon_emu.PRIVATE = {}
import uuid
from datetime import datetime, timezone

log = logging.getLogger("warpforge.playfab")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def ok(data):
    return 200, {"code": 200, "status": "OK", "data": data}


def err(code_name, message, error_code=1000, http=400):
    return http, {"code": http, "status": "BadRequest", "error": code_name,
                  "errorCode": error_code, "errorMessage": message}


def steam_id_from_ticket(ticket_hex):
    """The Steam id a sign-in was made for, or None."""
    try:
        b = bytes.fromhex(ticket_hex)
        if len(b) >= 20 and int.from_bytes(b[0:4], "little") == 20:
            return str(int.from_bytes(b[12:20], "little"))
    except ValueError:
        pass
    return None


class WordFilter:
    """Finds blocked words in a piece of text (see content/word-filter.txt for the rules)."""
    LOOKALIKE = str.maketrans({"0": "o", "1": "i", "!": "i", "|": "i", "3": "e", "4": "a", "@": "a",
                               "5": "s", "$": "s", "7": "t", "+": "t", "8": "b", "9": "g"})

    def __init__(self, entries):
        self.whole_forms, self.prefix, self.inside = [], [], []
        for raw in entries:
            e = str(raw).strip().lower()
            if not e or e.startswith("#"):
                continue
            inside, prefix = e.startswith("*"), e.endswith("*")
            word = re.sub(r"[^a-z0-9]", "", self._plain(e.strip("*")))
            if len(word) < 2:
                continue
            # each entry as written, and with doubled letters made single ("fuuuck" -> "fuck")
            for form in {word, self._squash(word)}:
                (self.inside if inside else self.prefix if prefix else self.whole_forms).append((form, form != word))

    @classmethod
    def _plain(cls, text):
        """Same length as text: lower case, accents dropped, look-alike characters turned into letters."""
        out = []
        for ch in text.lower():
            base = unicodedata.normalize("NFKD", ch)[:1] or ch
            out.append(base.translate(cls.LOOKALIKE) if len(base.translate(cls.LOOKALIKE)) == 1 else base)
        return "".join(out)

    @staticmethod
    def _squash(word):
        return re.sub(r"(.)\1+", r"\1", word)

    def _bad(self, word):
        """A squashed entry ("ass" -> "as") only counts against a word that had repeated letters
        itself ("asss"), so ordinary words like "as" are left alone."""
        if not word:
            return False
        squashed = self._squash(word)
        tries = [(word, False)] + ([(squashed, True)] if squashed != word else [])
        for w, was_squashed in tries:
            for form, form_squashed in self.whole_forms:
                if w == form and (was_squashed or not form_squashed):
                    return True
            for form, form_squashed in self.prefix:
                if w.startswith(form) and (was_squashed or not form_squashed):
                    return True
            for form, form_squashed in self.inside:
                if form in w and (was_squashed or not form_squashed):
                    return True
        return False

    def _spans(self, text):
        plain = self._plain(text)
        spans = []
        for m in re.finditer(r"[a-z0-9]+", plain):
            if self._bad(m.group()):
                spans.append((m.start(), m.end()))
        # letters split up: "f u c k", "f.u.c.k", "f-u-c-k"
        for m in re.finditer(r"(?<![a-z0-9])[a-z0-9](?:[^a-z0-9\n]{1,2}[a-z0-9](?![a-z0-9])){2,}", plain):
            if self._bad(re.sub(r"[^a-z0-9]", "", m.group())):
                spans.append((m.start(), m.end()))
        return spans

    def found(self, text):
        return bool(text) and bool(self._spans(str(text)))

    def censor(self, text):
        """(text with blocked words starred out, whether anything was)."""
        spans = self._spans(text or "")
        if not spans:
            return text, False
        chars = list(text)
        for a, b in spans:
            for i in range(a, b):
                if not chars[i].isspace():
                    chars[i] = "*"
        return "".join(chars), True


class NotSignedIn(Exception):
    """The request has no valid session (raised by _player; reported to the game as NotAuthenticated)."""


def _retry_locked(action, tries=20, wait=0.025):
    """Run a file action, trying again for a moment when Windows refuses it because another request
    is reading or replacing the same file at that instant (PermissionError, "access is denied")."""
    for i in range(tries):
        try:
            return action()
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(wait)


class Store:
    def __init__(self, root, content):
        self.root = root
        self.players_dir = os.path.join(root, "players")
        self.titledata_dir = os.path.join(content, "titledata")
        os.makedirs(self.players_dir, exist_ok=True)
        os.makedirs(self.titledata_dir, exist_ok=True)
        self.lock = threading.Lock()
        # Each player's display name, kept with the file's modified time and size (efficiency E3):
        # finding a player by name or friend code, or picking a free name, reads only files that changed.
        self._names = {}
        self._names_lock = threading.Lock()
        # Game sessions. A ticket is known only to the game it was given to; the server keeps a
        # fingerprint of it (never the ticket) in data/sessions.json, so a session survives a
        # restart without anything having to be taken on trust.
        self._sessions_file = os.path.join(root, "sessions.json")
        self._sessions_lock = threading.Lock()
        self._sessions = {}      # sha256 of ticket -> {"p": player id, "t": last used (unix seconds)}
        try:
            with open(self._sessions_file, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._sessions = {k: v for k, v in d.items() if isinstance(v, dict) and v.get("p")}
        except (OSError, ValueError):
            pass

    SESSION_DAYS = 30         # a ticket not used for this long stops working
    SESSIONS_PER_PLAYER = 5   # the newest few are kept (one per device a player uses)

    def _sessions_save(self):
        tmp = self._sessions_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._sessions, f)
        _retry_locked(lambda: os.replace(tmp, self._sessions_file))

    def issue(self, pfid):
        """A new session ticket for a player who has just signed in."""
        ticket = f"{pfid}-{uuid.uuid4().hex}{uuid.uuid4().hex}"
        now = int(time.time())
        with self._sessions_lock:
            oldest = now - self.SESSION_DAYS * 86400
            self._sessions = {k: v for k, v in self._sessions.items() if v.get("t", 0) >= oldest}
            mine = sorted((k for k, v in self._sessions.items() if v["p"] == pfid), key=lambda k: self._sessions[k].get("t", 0))
            for k in mine[:max(0, len(mine) - (self.SESSIONS_PER_PLAYER - 1))]:
                del self._sessions[k]
            self._sessions[hashlib.sha256(ticket.encode("utf-8")).hexdigest()] = {"p": pfid, "t": now}
            try:
                self._sessions_save()
            except OSError:
                log.exception("could not save sessions.json")
        return ticket

    def session_player_id(self, headers):
        """The id of the signed-in player a request comes from, or "" when its ticket is unknown or expired."""
        ticket = headers.get("X-Authorization") or headers.get("X-EntityToken") or ""
        if not ticket:
            return ""
        now = int(time.time())
        with self._sessions_lock:
            entry = self._sessions.get(hashlib.sha256(ticket.encode("utf-8")).hexdigest())
            if entry is None or entry.get("t", 0) < now - self.SESSION_DAYS * 86400:
                return ""
            if entry.get("out"):                     # signed out by the server (see sign_out)
                return ""
            if now - entry.get("t", 0) > 3600:      # remembered roughly; written out with the next sign-in
                entry["t"] = now
            pid = entry["p"]
        banned = getattr(self, "banned", None)            # set by the emulator: a banned player's sessions stop working
        if banned is not None and pid in banned():
            return ""
        return pid

    def sign_out(self, pfid, reason):
        """Ends every session of a player; a game still using one is told `reason` (signed_out_reason)."""
        with self._sessions_lock:
            hit = 0
            for entry in self._sessions.values():
                if entry.get("p") == pfid and not entry.get("out"):
                    entry["out"] = reason
                    hit += 1
            if hit:
                try:
                    self._sessions_save()
                except OSError:
                    log.exception("could not save sessions.json")
        return hit

    def sign_out_session(self, headers, reason):
        """Ends only the session this request was made with. Returns True when there was one."""
        ticket = headers.get("X-Authorization") or headers.get("X-EntityToken") or ""
        if not ticket:
            return False
        with self._sessions_lock:
            entry = self._sessions.get(hashlib.sha256(ticket.encode("utf-8")).hexdigest())
            if entry is None or entry.get("out"):
                return False
            entry["out"] = reason
            try:
                self._sessions_save()
            except OSError:
                log.exception("could not save sessions.json")
        return True

    def signed_out_reason(self, headers):
        """Why a session that used to work no longer does ("" when it is simply unknown or old)."""
        ticket = headers.get("X-Authorization") or headers.get("X-EntityToken") or ""
        if not ticket:
            return ""
        with self._sessions_lock:
            entry = self._sessions.get(hashlib.sha256(ticket.encode("utf-8")).hexdigest())
        if entry is None:
            return ""
        if entry.get("out"):
            return entry["out"]
        banned = getattr(self, "banned", None)
        if banned is not None and entry.get("p") in banned():
            return "This account has been banned from this server."
        return ""

    # ---- players
    # Player ids are 16 hex characters (see player_id_for). Some arrive from the game (another
    # player's profile, a friend, an opponent), so nothing else may become a file name here.
    PLAYER_ID = re.compile(r"[0-9A-F]{16}")

    def _path(self, pfid):
        if not isinstance(pfid, str) or not self.PLAYER_ID.fullmatch(pfid):
            raise ValueError("not a player id")
        return os.path.join(self.players_dir, pfid + ".json")

    def load(self, pfid):
        if not isinstance(pfid, str) or not self.PLAYER_ID.fullmatch(pfid):
            return None
        p = self._path(pfid)
        if os.path.exists(p):
            def read():
                with open(p, encoding="utf-8") as f:
                    return json.load(f)
            return _retry_locked(read)
        return None

    MAX_SAVE_BYTES = 1024 * 1024        # last line of defence; the request handlers stop well before this

    def save(self, player):
        text = json.dumps(player, indent=1, ensure_ascii=False)
        if len(text.encode("utf-8")) > self.MAX_SAVE_BYTES:
            raise ValueError("player %s is too large to save" % player.get("PlayFabId"))
        tmp = self._path(player["PlayFabId"]) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        _retry_locked(lambda: os.replace(tmp, self._path(player["PlayFabId"])))

    @staticmethod
    def player_id_for(kind, ext_id):
        return hashlib.sha1(f"{kind}:{ext_id}".encode()).hexdigest()[:16].upper()

    def get_or_create(self, kind, ext_id, display=None):
        pfid = self.player_id_for(kind, ext_id)
        with self.lock:
            p = self.load(pfid)
            created = p is None
            if created:
                p = {"PlayFabId": pfid, "Created": now_iso(), "Login": {kind: ext_id},
                     "DisplayName": display, "UserData": {}, "ReadOnlyData": {},
                     "Inventory": [], "VirtualCurrency": {}, "Statistics": {}, "DataVersion": 0}
            if not p.get("DisplayName"):
                p["DisplayName"] = self.default_name(pfid)
            p["LastLogin"] = now_iso()
            self.save(p)
        ticket = self.issue(pfid)
        return p, ticket, created

    def login(self, pfid):
        """Signs an existing player in by id (used by the game login file). Returns (player, ticket) or (None, None)."""
        with self.lock:
            p = self.load(pfid)
            if p is None:
                return None, None
            p["LastLogin"] = now_iso()
            self.save(p)
        ticket = self.issue(pfid)
        return p, ticket

    def names(self):
        """[(player id, display name stripped and lower-cased)] for every player file, in folder order.
        The folder is listed each time (cheap); a file is read again only when its modified time or
        size changed since it was last read, and files that are gone drop out (efficiency E3)."""
        out, seen = [], set()
        with self._names_lock:
            cache = self._names
            with os.scandir(self.players_dir) as entries:
                for e in entries:
                    if not e.name.endswith(".json"):
                        continue
                    pfid = e.name[:-5]
                    if not self.PLAYER_ID.fullmatch(pfid):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    stamp = (st.st_mtime_ns, st.st_size)
                    hit = cache.get(pfid)
                    if hit is None or hit[0] != stamp:
                        try:
                            p = self.load(pfid)
                        except (OSError, ValueError):
                            p = None
                        if not p:
                            cache.pop(pfid, None)
                            continue
                        hit = (stamp, (p.get("DisplayName") or "").strip().lower())
                        cache[pfid] = hit
                    seen.add(pfid)
                    out.append((pfid, hit[1]))
            for k in [k for k in cache if k not in seen]:
                del cache[k]
        return out

    def default_name(self, pfid):
        """Name given to a new player until they pick one: "Commander-" plus part of their id,
        lengthened if someone already took that exact name."""
        taken = {name for other, name in self.names() if other != pfid and name}
        for n in range(4, len(pfid) + 1):
            candidate = "Commander-" + pfid[:n]
            if candidate.lower() not in taken:
                return candidate[:20]
        return ("Commander-" + pfid)[:20]

    def from_session(self, headers):
        pfid = self.session_player_id(headers)
        return self.load(pfid) if pfid else None

    # ---- title data
    def title_data(self):
        data = {}
        for name in sorted(os.listdir(self.titledata_dir)):
            if name.endswith(".json"):
                with open(os.path.join(self.titledata_dir, name), encoding="utf-8") as f:
                    for k, v in json.load(f).items():
                        data[k] = v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))
        return data


def _kv(d, keys=None):
    out = {}
    for k, v in d.items():
        if keys and k not in keys:
            continue
        out[k] = {"Value": v, "LastUpdated": now_iso(), "Permission": "Private"}
    return out



class CardCompiler:
    """Writes a creator design in the game's own card layout (the form cards have inside the card
    file). The blank structures and enum numbers come from content/creator-native.json, which is
    taken from the game. Two things are left as placeholders for the build step, which knows the
    final card file: {"$ref": card id} for another card and {"$art": card id} for borrowed art.
    Everything the layout cannot express is returned as plain-language notes.

    What each effect needs is not guessed: native["reads"] lists, per effect, the parts of an
    effect's data the game's code actually reads (tools/effect_reads.json, taken from
    AbilityLogic.PlayAbility). An effect only gets the parts it reads; anything else a design
    supplies for it is reported instead of being written where the game would ignore it."""
    # triggers where the player picks nothing but "the target" is still a unit (the one the event is about)
    EVENT_UNIT_TRIGGERS = ("UnitAttack", "Strike", "UnitStunned", "ThisUnitDeath", "OtherUnitDeath", "TriggeredSwarm")

    SELF, ENEMY = 10, 20
    KINDS = {"troop": 0, "hero": 10, "tactic": 20}

    # design name -> the game's field, for a "who/what" filter (TargetCriteria)
    CRIT_FLAGS = {"friendly": "friendlyUnits", "enemy": "enemyUnits", "warlords": "includeHero", "troops": "includeMinions",
                  "stratagems": "includeSpells", "whispers": "includeWhispers", "weapons": "includeWeapons",
                  "remnants": "includeRemnants", "warlordParts": "includeHeroParts", "self": "includeSelf",
                  "onlySelf": "excludeNotSelf", "excludeSimilar": "excludeSimilarCards", "excludeTargeted": "excludeTargetted",
                  "excludeFriendlyWarlord": "excludeFriendlyHero", "excludeEnemyWarlord": "excludeEnemyHero",
                  "excludeTrait": "excludeTrait", "excludeCondition": "excludeCondition", "excludeKeyword": "excludeRace",
                  "fromChoice": "selectFromChooseResolution"}
    CRIT_ENUMS = {"pool": ("targetsAffected", "TargetsAffected", "target group"), "trait": ("traitsFilter", "DefinedTrait", "keyword"),
                  "keyword": ("keywordFilter", "Keyword", "unit type"), "condition": ("conditionsFilter", "UnitConditions", "condition"),
                  "select": ("targetSelectMethod", "TargetSelectMethod", "pick method"),
                  "army": ("chosenArmy", "CardArmy", "faction"), "armyMode": ("armyFilter", "ArmyFilterOptions", "faction rule"),
                  "stratagemType": ("spellType", "SpellType", "stratagem type")}
    CRIT_STATS = {"attack": "attackFilter", "health": "currentHealthFilter", "cost": "manaCostFilter", "rarity": "rarityFilter"}
    CRIT_REFS = {"cards": "filterSpecificCards", "notEnchantedBy": "excludeEnchantments"}

    # a buff (CardEffect)
    BUFF_STATS = {"melee": "meleeAttack", "ranged": "rangeAttack", "health": "health", "armour": "armour", "cost": "cost"}
    BUFF_DURATIONS = {"turn": "untilEndOfTurn", "nextTurn": "untilPlayerTurnStart", "enemyTurn": "untilEnemyTurnStart",
                      "followingTurn": "untilFollowingPlayerTurnStart", "nextAttack": "untilNextAttack",
                      "auraFixed": "extrinsic", "auraCounted": "extrinsicVariable"}
    BUFF_FLAGS = {"permanent": "permanentBuff", "doNotStack": "doNotStack", "enchantAnim": "triggerEnchantAnim"}
    BUFF_ENUMS = {"mark": ("markOfChaosType", "MarkOfChaosType", "mark of chaos"), "daemonHost": ("daemonHostType", "DaemonhostType", "daemonhost gift"),
                  "mutation": ("mutationType", "MutationType", "mutation"), "race": ("newRace", "Keyword", "unit type")}
    # buff types whose numbers the game recalculates from "value from" options (AbilityLogic.AdjustCardEffect)
    BUFF_SCALED = ("addStats", "addTrait", "addTraitRemnant", "changeCost", "changeAbilityCost", "setMeleeAttack", "setRangeAttack", "setCost")

    MANA_RESOURCES = {"energy": None, "psychic": "psychicMana", "plasma": "plasmaMana", "spiritStone": "spiritStoneMana",
                      "faith": "faithMana", "questPoints": "questPoints"}
    SUMMON_FLAGS = {"forEnemy": "isForEnemy", "allTargets": "summonAllTargets", "chooseInPlay": "chooseCardInPlay",
                    "keepEffects": "keepEffects", "removeFromCemetery": "shouldRemoveFromCemetery"}
    PLAYERS = {"both": 0, "self": 10, "enemy": 20}
    INVENTORY = {"InInventory": 0, "CantAddToDeck": 5, "NotInInventory": 10}     # CardInventoryOptions
    # the Pray ability every official Pray card carries (same on all 23): the praying state
    PRAY_ABILITY = {"trigger": "Pray", "context": "Play", "priority": "Instant", "playIcon": True,
                    "effects": [{"effect": "buffSelf", "targets": {"pool": "self"},
                                 "buff": {"type": "singleUseEnchantment", "trait": "pray",
                                          "enchantment": "9218576fd3e26554db5b4c239a36dbc7"}}]}

    def __init__(self, native):
        self.n = native
        self.e = native["enums"]
        self.reads = (native.get("reads") or {})
        self.trig = (native.get("triggers") or {})
        self.notes = []
        self.explicit_choice = False       # the design names what the player may pick ("choose")

    def _new(self, what):
        return copy.deepcopy(self.n[what])

    # value options official cards use that the game's list has no name for
    EXTRA_ENUMS = {("ChooseValueOptions", "unmultiplied"): 17}     # 4 official buffs: the number as written, never multiplied

    def _enum(self, table, name, what):
        if name in (None, "", 0):
            return 0
        if (table, name) in self.EXTRA_ENUMS:
            return self.EXTRA_ENUMS[(table, name)]
        value = self.e.get(table, {}).get(name)
        if value is None:
            self.notes.append(f"{what} '{name}' is not known to the game - left empty")
            return 0
        return value

    @staticmethod
    def _int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------ who / what filters
    def targets(self, t, pool=None):
        tc = self._new("targets")
        t = t or {}
        tc["targetsAffected"] = self._enum("TargetsAffected", pool or t.get("pool") or "any", "target group")
        for flag, field in self.CRIT_FLAGS.items():
            if t.get(flag):
                tc[field] = 1
        for key, (field, table, what) in self.CRIT_ENUMS.items():
            if key != "pool" and t.get(key):
                tc[field] = self._enum(table, t[key], what)
        if t.get("army") and not t.get("armyMode"):
            tc["armyFilter"] = self.e["ArmyFilterOptions"]["OnlyChosenArmy"]
        for key, field in self.CRIT_STATS.items():
            f = t.get(key)
            if isinstance(f, dict) and f.get("is"):
                tc[field] = self._int(f.get("value"))
                tc[field + "Option"] = self._enum("StatFilterOptions", f["is"], "comparison")
        for key, field in self.CRIT_REFS.items():
            if t.get(key):
                tc[field] = [{"$ref": c} for c in t[key]]
        if t.get("max"):
            tc["maxTargets"] = self._int(t["max"])
        return tc

    # ------------------------------------------------------------------ buffs
    def buff(self, b, trigger=None):
        """A buff as the fields of the game's CardEffect."""
        b = b or {}
        out = {}
        stats = {k: self._int(b.get(k)) for k in self.BUFF_STATS}
        kind = b.get("type")
        if not kind:
            parts = [bool(b.get("trait")), bool(stats["cost"]), any(stats[k] for k in ("melee", "ranged", "health", "armour"))]
            if sum(parts) > 1:
                self.notes.append("one effect can give stats, a keyword or a cost change, not several - "
                                  "only the first was written; add a second effect for the others")
            kind = "addTrait" if b.get("trait") else "changeCost" if stats["cost"] else "addStats"
        out["buffType"] = self._enum("BuffType", kind, "buff type")
        if kind in ("addTrait", "removeTrait", "addTraitRemnant") or b.get("trait"):
            out["trait"] = self._enum("DefinedTrait", b.get("trait"), "keyword")
            out["traitValue"] = self._int(b.get("traitValue"))
        elif b.get("traitValue"):
            # a value without a keyword: an enchantment's number (Hunters of Heretics attaches "Vulnerable 4")
            out["traitValue"] = self._int(b["traitValue"])
        if kind in ("changeCost", "setCost", "changeAbilityCost") or (b.get("type") and stats["cost"]):
            out["costChange"] = stats["cost"]
        if kind not in ("addTrait", "removeTrait", "changeCost", "setCost", "changeAbilityCost") or b.get("type"):
            for k in ("melee", "ranged", "health", "armour"):
                if stats[k]:
                    out[self.BUFF_STATS[k] + "Change"] = stats[k]
        for k, field in self.BUFF_STATS.items():       # "value from": the number comes from the game state
            src = b.get(k + "From")
            if src:
                out[field + "ValueOptions"] = self._enum("ChooseValueOptions", src, "value source")
                if kind not in self.BUFF_SCALED:
                    self.notes.append(f"buff type '{kind}' does not recalculate its numbers, so '{k} from {src}' has no effect")
        if b.get("random"):
            out["randomValueChange"] = self._int(b["random"])
        durations = b.get("duration")
        # one duration, or several at once (official cards: "this turn" and "until its next attack";
        # both "while in play" flags)
        for duration in (durations if isinstance(durations, list) else [durations]):
            if duration == "aura":
                # how official cards mark "only while the source is in play": the plain flag for
                # "while in play", the counted one where the size follows a count
                out["extrinsicVariable" if trigger == "WhileInPlayVariable" else "extrinsic"] = 1
            elif duration:
                field = self.BUFF_DURATIONS.get(duration)
                if field:
                    out[field] = 1
                else:
                    self.notes.append(f"buff duration '{duration}' is not known - left permanent")
        for k, field in self.BUFF_FLAGS.items():
            if b.get(k):
                out[field] = 1
        for k, (field, table, what) in self.BUFF_ENUMS.items():
            if b.get(k):
                out[field] = self._enum(table, b[k], what)
        if b.get("enchantment"):
            out["enchantment"] = {"$ref": b["enchantment"]}
        for k in ("effectName", "effectText"):
            if b.get(k):
                out[k] = str(b[k])
        return out

    def _set(self, logic, part, values):
        block = logic
        for p in part.split("."):
            block = block[p]
        for k, v in values.items():
            if k not in block:
                self.notes.append(f"the game's layout has no '{part}.{k}' field - skipped")
            else:
                block[k] = v

    # ------------------------------------------------------------------ one effect
    def effect(self, fx, trigger=None):
        """One effect -> one entry of the game's ability logic. Returns (logic, the targets block if it needs a chosen target)."""
        name = fx.get("effect") or ""
        logic = self._new("logic")
        logic["chosenAbility"] = self._enum("AbilityEffect", name, "effect")
        reads = (self.reads.get(name) or {}).get("blocks")
        known = reads is not None
        reads = set(reads or [])
        chosen = None
        t = fx.get("targets")
        if t is not None:
            if t.get("pool") == "target" and not self.explicit_choice:
                # official cards put the "who can be chosen" filter on the card, and a bare "target" here.
                # (Designs that say what can be chosen themselves - "choose" - are written as given.)
                logic["targetCriteria"] = self.targets({k: v for k, v in t.items() if k in ("select", "max")}, "target")
                chosen = t
            else:
                logic["targetCriteria"] = self.targets(t)
        amount, count = self._int(fx.get("amount")), self._int(fx.get("count")) or 1
        used = {"effect", "targets", "note"}

        # every effect: how many times / for each ..., and a chance
        per = fx.get("per")
        if per:
            logic["multiplier"] = self.targets(per)
            used.add("per")
        if fx.get("times"):
            logic["furtherMultiplier"] = self._int(fx["times"])
            used.add("times")
        if fx.get("chance"):
            logic["chanceOfHappening"] = max(0, min(100, self._int(fx["chance"])))
            used.add("chance")
        if fx.get("casterMustBeInPlay"):
            logic["checkIfCasterInPlay"] = 1
            used.add("casterMustBeInPlay")

        if "damage" in reads:
            tc = logic["targetCriteria"]
            if (t and tc["maxTargets"] and not tc["conditionsFilter"] and not t.get("anyState")
                    and tc["targetsAffected"] == self.e["TargetsAffected"]["allFiltered"]):
                # damage to a few units picked at random: official cards (84 of 114) leave out units
                # that are already dying, so the damage is not wasted on them
                tc["conditionsFilter"] = self.e["UnitConditions"]["dealtKillDmg"]
                tc["excludeCondition"] = 1
            hi = self._int(fx.get("amountMax"))
            crit = {"minDamage": amount, "maxDamage": hi or amount}
            if fx.get("amountFrom"):
                crit["valueOptions"] = self._enum("ChooseValueOptions", fx["amountFrom"], "value source")
            elif hi and hi != amount:
                crit.update(valueOptions=self.e["ChooseValueOptions"]["RandomizedOnResolution"],
                            damageType=self.e["DamageType"]["Random"])        # rolled when it resolves
            if fx.get("damageType"):
                crit["damageType"] = self._enum("DamageType", fx["damageType"], "damage type")
            self._set(logic, "damageCriteria", crit)
            used.update(("amount", "amountMax", "amountFrom", "damageType"))
            if name == "doDamageMultiRandom":
                # the game deals the damage "multiplier" times, each to a fresh random target
                if count > 1 and not fx.get("times"):
                    logic["furtherMultiplier"] = count
                used.add("count")
        if "heal" in reads:
            hi = self._int(fx.get("amountMax"))
            crit = {"minValue": amount, "maxValue": hi or amount}
            if fx.get("amountFrom"):
                crit["valueOptions"] = self._enum("ChooseValueOptions", fx["amountFrom"], "value source")
            self._set(logic, "healCriteria", crit)
            used.update(("amount", "amountMax", "amountFrom"))
        if "buff" in reads:
            self._set(logic, "buffEffect", self.buff(fx.get("buff"), trigger))
            used.add("buff")
            if name == "buffSelf" and t is None:
                logic["targetCriteria"] = self.targets({}, "self")     # 198 of 206 official uses
        if "enchantment" in reads:
            b = fx.get("buff") or {}
            vals = {}
            if b.get("enchantment") or fx.get("card"):
                vals["enchantment"] = {"$ref": b.get("enchantment") or fx.get("card")}
            if b.get("type"):
                vals["buffType"] = self._enum("BuffType", b["type"], "buff type")
            if vals:
                self._set(logic, "buffEffect", vals)
            else:
                self.notes.append(f"'{name}' names another card's effect, and none was chosen")
            used.update(("buff", "card"))
        if "hand" in reads:
            h = fx.get("hand") or {}
            self._set(logic, "handBuff.cardEffect", self.buff(h.get("buff") or fx.get("buff"), trigger))
            ht = h.get("targets") or t or {"friendly": True, "troops": True, "stratagems": True}
            if not h.get("targets"):
                # one target box on the page: it describes the cards in hand, and the effect itself needs none
                logic["targetCriteria"] = self._new("targets")
            logic["handBuff"]["targetCriteria"] = self.targets(dict(ht, pool=ht.get("pool") if ht.get("pool") not in (None, "any") else "inHandFiltered"))
            # whose hand: official cards follow the filter (the enemy's cards -> the enemy's hand)
            whose = h.get("players") or ("enemy" if ht.get("enemy") and not ht.get("friendly") else "self")
            logic["handBuff"]["playersAffected"] = self.PLAYERS.get(whose, 0)
            if h.get("uses"):
                logic["handBuff"].update(limitedUses=1, numberOfUses=self._int(h["uses"]))
            used.update(("hand", "buff"))
        if "mana" in reads:
            m = fx.get("mana") or {}
            who = m.get("player") or "self"
            res = m.get("resource") or "energy"
            vals = {"manaAmount": self._int(m.get("amount")) or amount or 1,
                    "friendlyPlayer": int(who in ("self", "both")), "enemyPlayer": int(who in ("enemy", "both"))}
            if res not in self.MANA_RESOURCES:
                self.notes.append(f"resource '{res}' is not known - written as energy")
                res = "energy"
            if self.MANA_RESOURCES[res]:
                vals[self.MANA_RESOURCES[res]] = 1
            mode = m.get("mode") or ("gain" if res in ("energy", "faith") else "")
            # gain = a new point (the bar grows), refill = fill empty points; quest points take neither
            if mode in ("gain", "both"):
                vals["createMana"] = 1
            if mode in ("refill", "both"):
                vals["refillMana"] = 1
            if m.get("untilEndOfTurn"):
                vals["untilEndOfTurn"] = 1
            self._set(logic, "manaBuff", vals)
            used.update(("mana", "amount"))
        if "summon" in reads:
            s = fx.get("summon") or {}
            cards = s.get("cards") or ([fx["card"]] if fx.get("card") else [])
            vals = {"unitsToSummon": [{"$ref": c} for c in cards]}
            for k, field in self.SUMMON_FLAGS.items():
                if s.get(k):
                    vals[field] = 1
            for k, field in (("createAt", "createAtPos"), ("moveTo", "moveToPos")):
                if s.get(k):
                    vals[field] = self._enum("PositionOption", s[k], "position")
            self._set(logic, "summonCriteria", vals)
            if not cards and not (s.get("allTargets") or s.get("chooseInPlay")) and t is None:
                self.notes.append(f"'{name}' has no card chosen")
            if count > 1 and not fx.get("times"):
                logic["furtherMultiplier"] = count
            used.update(("summon", "card", "count"))
        if "draw" in reads:
            if count > 1 and not fx.get("times"):
                logic["furtherMultiplier"] = count      # cards drawn = the multiplier
            used.add("count")
        if "players" in reads:
            logic["playersAffected"] = self.PLAYERS.get(fx.get("players") or "self", self.SELF)
            used.add("players")
        elif fx.get("players"):
            logic["playersAffected"] = self.PLAYERS.get(fx["players"], 0)
            used.add("players")

        if not known:
            self.notes.append(f"effect '{name}' is not one the game has - only its name and targets were written")
        ignored = sorted(k for k, v in fx.items() if k not in used and v not in (None, "", 0, [], {}, 1) and k not in ("count", "amount"))
        if ignored:
            self.notes.append(f"effect '{name}' does not use: {', '.join(ignored)} (the game ignores them for this effect)")
        return logic, chosen

    def effects(self, effects, what, trigger=None):
        logics, chosen = [], None
        for fx in effects or []:
            logic, wants = self.effect(fx, trigger)
            logics.append(logic)
            if wants is not None:
                if chosen is not None and chosen != wants:
                    self.notes.append(f"{what}: effects ask for different chosen targets - the game allows one choice, the first was used")
                else:
                    chosen = wants
        return logics, chosen

    def board(self, bc, when, turn=None):
        """A condition on the state of the game (BoardCriteria): whose turn, and/or a count compared
        with a number, or a random roll. Used by abilities ("when") and by the alternative action."""
        turn = when.get("turn") or turn
        if turn:
            bc["playerTurn"] = self.PLAYERS.get(turn, 0)
        if when.get("count") is not None or when.get("is"):
            bc["countCriteria"] = self.targets(when.get("count") or {}, (when.get("count") or {}).get("pool") or "allFiltered")
            bc["triggerIf"] = self._enum("StatFilterOptions", when.get("is") or "EqualOrHigher", "comparison")
            bc["referenceValue"] = self._int(when.get("value"))
            if when.get("minusAllies"):
                bc["countOptions"] = self.e["CountOptions"]["EnemiesMinusAllies"]
        elif when.get("chance"):
            bc["triggerIf"] = self.e["StatFilterOptions"]["RandomChance"]
            bc["referenceValue"] = self._int(when["chance"])

    def active(self, aa, action, kw=""):
        """An action the player uses by hand (ActiveAbility): its effects, cost and what the player picks.
        Used by the card's action and by its alternative action."""
        self.explicit_choice = "choose" in action
        effects = list(action.get("effects") or [])
        if kw == "pray" and not any(fx.get("effect") == "triggerPray" for fx in effects):
            # every official Pray action ends by setting off Pray (what cards that react to a prayer listen for)
            effects.append({"effect": "triggerPray", "targets": {"pool": "self"}})
        logics, wants = self.effects(effects, "the action")
        aa["activeAbilityLogic"] = logics
        aa["manaCost"] = int(action.get("cost") or 0)
        if action.get("playAnim"):
            aa["playAnim"] = 1
        if self.explicit_choice:
            wants = action["choose"] or None
        if wants is not None:
            aa["targeted"] = 1
            aa["targetCriteria"] = self.targets(wants, wants.get("pool") if self.explicit_choice and wants.get("pool") else "allFiltered")

    # ------------------------------------------------------------------ one ability
    def ability(self, ab, i):
        """One triggered ability. Returns (the game's ability, the chosen-target filter or None)."""
        a = self._new("ability")
        trigger = ab.get("trigger") or "ThisCardPlayed"
        a["trigger"] = self._enum("AbilityTrigger", trigger, "trigger")
        info = self.trig.get(trigger) or {}
        # how quickly it resolves; every official card but a handful uses "Instant"
        a["priority"] = self._enum("ActionPriority", ab.get("priority") or "Instant", "priority")
        # where the card has to be for the ability to count (in play, in hand, dying ...)
        a["abilityContext"] = self._enum("AbilityContext", ab.get("context") or info.get("context") or "Play", "context")
        if ab.get("playIcon") or (ab.get("playIcon") is None and info.get("playIcon")):
            a["playIcon"] = 1
        if ab.get("triggerValue") or info.get("value"):
            a["triggerValue"] = self._int(ab.get("triggerValue")) or 1
        if ab.get("random"):
            a["randomAbilityChoice"] = 1

        # the card that caused the event must match this (the game checks it for every trigger)
        if ab.get("subject"):
            a["actingCardCriteria"] = self.targets(ab["subject"], ab["subject"].get("pool") or "allFiltered")
        elif info.get("subject"):
            a["actingCardCriteria"] = self.targets(info["subject"])
            self.notes.append(f"ability {i}: '{trigger}' needs to know which cards set it off - none was given, so the usual "
                              "official setting was used (" + info.get("subjectText", "any card") + ")")
        # ... and so must the event's target
        if ab.get("victim"):
            a["targetCriteria"] = self.targets(ab["victim"], ab["victim"].get("pool") or "allFiltered")

        # a condition on the state of the game: whose turn, and/or a count compared with a number
        self.board(a["boardCriteria"], ab.get("when") or {}, ab.get("turn") or info.get("turn"))
        if ab.get("countEach"):
            a["multiCountCriteria"] = self.targets(ab["countEach"], ab["countEach"].get("pool") or "allFiltered")
        if ab.get("condition"):
            self.notes.append(f"ability {i} has a free-text condition, which the game cannot read - use the 'when' settings: {ab['condition']}")

        logics, wants = self.effects(ab.get("effects"), f"ability {i}", trigger)
        a["abilityLogic"] = logics
        return a, wants, trigger

    def compile(self, card):
        self.notes = []
        d = self._new("card")
        kind = card["kind"]
        d.update(m_Enabled=1, m_Script=copy.deepcopy(self.n["script"]), uniqueId=card.get("id") or "", cardName=card["name"],
                 cardArmy=self._enum("CardArmy", card.get("army") or "Neutral", "faction"),
                 cardRarity=self._enum("CardRarity", card["rarity"], "rarity"), cardType=self.KINDS[kind],
                 # attacks deal "Attack" damage; 56 of 66 official warlords say "Normal" instead
                 manaCost=card["cost"], attackDamageType=0 if kind == "hero" else 4, initiative=20, showInCollectionOptions=5,
                 useOverDraw=0 if kind == "tactic" else 1)
        tag = self.n["armyTags"].get(card.get("army") or "", card.get("army") or "Neutral")
        d["m_Name"] = "%s %s-%s" % ("war" if kind == "hero" else ("T" if kind == "tactic" else "") + "C", tag, card["name"])
        if card.get("attackDamageType"):
            d["attackDamageType"] = self._enum("DamageType", card["attackDamageType"], "attack damage type")
        if card.get("token") or card.get("enchantment"):
            # how the game marks cards created by other cards (seen on every such official card):
            # not shown in the collection, not part of the inventory, never picked by "random card" effects
            d.update(showInCollectionOptions=0, inventoryOptions=10, cantBeRandomlyCreated=1)
            d["m_Name"] = "Z-NC " + d["m_Name"].split(" ", 1)[1]
        if card.get("enchantment"):
            # an enchantment: a hidden card other cards attach with a buff of type enchantment (the 80 official
            # ones are named "<card>_enchantment"); its abilities run as the host card's
            d["m_Name"] += "_enchantment"
            if not card.get("abilities"):
                self.notes.append("an enchantment does nothing without abilities - add what it does to the card it is attached to")
        d["deckCriteria"]["triggerIf"] = 1
        d["normalAbilityBoardCriteria"]["triggerIf"] = 1
        d["attackScoringCriteria"] = copy.deepcopy(self.n["attackScoring"])
        if kind == "tactic":
            d["spellType"] = self._enum("SpellType", card.get("stratagemType") or "Normal", "stratagem type")
            if d["spellType"]:
                d["cantBeRandomlyCreated"] = 1
        if card.get("randomCreation") is not None:
            # whether "a random card" effects may create this card, when it differs from the usual rule
            # (35 official cards: tokens that may, or ordinary cards that may not)
            d["cantBeRandomlyCreated"] = 0 if card["randomCreation"] else 1
        if card.get("inventory") in self.INVENTORY:
            # collection and decks: in the inventory, owned but never in a deck, or not in the inventory
            d["inventoryOptions"] = self.INVENTORY[card["inventory"]]
        if card.get("blockActions"):
            d["blockActionsWhileResolving"] = 1      # the player cannot act until the card has resolved
        for k, field in (("startingCard", "startingCardInHand"), ("goSecondCard", "goSecondCardInHand")):
            if card.get(k):
                # a warlord's own cards: in the starting hand, and given to the player who goes second
                d[field] = {"$ref": card[k]}
        for n, rid in enumerate((card.get("related") or [])[:4], 1):
            if rid:
                d["relatedCard%d" % n] = {"$ref": rid}          # shown next to the card when it is looked at
        if card.get("choices"):
            # the "choose one of these" window: where the options come from and how many are offered
            ch = card["choices"]
            d["choicesDefinition"] = self.targets(ch, ch.get("pool") or "choiceResolution")
        if card.get("targetsCounter"):
            # the number shown on the card in hand: how many targets it has right now
            tcn = card["targetsCounter"]
            d["targetsAvailableCounter"] = self.targets(tcn, tcn.get("pool") or "any")
        if kind != "tactic" or any(card.get(k) for k in ("melee", "ranged", "health", "armour", "keyword")):
            # units always; a few official stratagems carry unit stats too (Crushing Claws, Deffkopta ...)
            d.update(meleeAttack=card.get("melee", 0), rangeAttack=card.get("ranged", 0), maxHealth=card.get("health", 0 if kind == "tactic" else 1),
                     armour=card.get("armour", 0), keyword=self._enum("Keyword", card.get("keyword"), "unit type"))
        if kind != "tactic":
            if kind == "troop" and card.get("ranged", 0) > 0:
                d["customRangedAttackParticles"] = {"m_AssetGUID": self.n["rangedParticles"].get(card.get("keyword") or "", self.n["rangedParticles"][""]),
                                                    "m_SubObjectName": "", "m_SubObjectType": ""}
        # uploaded artwork wins over art borrowed from another card
        art = "upload:" + (card.get("id") or "") if card.get("artUpload") else card.get("art") or ""
        d["cardImageReference"] = {"$art": art}
        if not art:
            self.notes.append("no artwork chosen")
        for t in card.get("traits") or []:
            entry = dict(self.n["trait"], name=t.get("name") or "", _trait=self._enum("DefinedTrait", t.get("name"), "keyword"),
                         _traitValue=int(t.get("value") or 0), _hide=1 if t.get("hidden") else 0)
            d["CardTraits"].append(entry)
        own = {t.get("name") for t in card.get("traits") or []}
        given = set()
        chosen = None
        self.explicit_choice = "choose" in card      # the design says itself what the player picks (or that nothing is picked)
        for i, ab in enumerate(card.get("abilities") or [], 1):
            a, wants, trigger = self.ability(ab, i)
            if wants is not None:
                if not (self.trig.get(trigger) or {}).get("chooses", trigger == "ThisCardPlayed"):
                    # the player picks nothing here: "the target" is the unit the event is about, which official
                    # cards only use on these triggers (Venomthrope, Jain Zar, Tyranid Prime ...) and on enchantments
                    if trigger not in self.EVENT_UNIT_TRIGGERS and not card.get("enchantment"):
                        self.notes.append(f"ability {i} aims at 'the target', but the player picks nothing on this trigger and official "
                                          "cards only do that where the event is about a unit (attacks, Strike, stuns, deaths, Swarm) - "
                                          "use a random or 'all' target group instead")
                elif chosen is None:
                    chosen = wants
            if ab.get("keyword") and ab["keyword"] not in own:      # e.g. Rally labelling a "when played" ability
                own.add(ab["keyword"])
                d["CardTraits"].append(dict(self.n["trait"], name=ab["keyword"],
                                            _trait=self._enum("DefinedTrait", ab["keyword"], "keyword")))
            for fx in ab.get("effects") or []:
                b = fx.get("buff") or (fx.get("hand") or {}).get("buff") or {}      # a hand buff counts too
                if b.get("trait"):
                    given.add(b["trait"])
            d["cardAbilities"].append(a)
        if self.explicit_choice:
            chosen = card["choose"] or None
        if chosen is not None:
            d["targeted"] = 1
            d["cardTargetCriteria"] = self.targets(chosen, chosen.get("pool") if self.explicit_choice and chosen.get("pool") else "allFiltered")
            d["targetingTextReferenceId"] = "chooseTarget"
        action = card.get("action")
        if action:
            if kind == "tactic":
                self.notes.append("stratagems cannot have an action - it was left out")
            else:
                kw = action.get("keyword") or ""
                d["hasActiveAbility"] = 1
                self.active(d["activeAbility"], action, kw)
                if kw == "oath":
                    # "Oath N": official cards keep N on the keyword and leave the action's own cost at 0
                    d["activeAbility"]["manaCost"] = 0
                    oath = next((t for t in d["CardTraits"] if t.get("name") == "oath"), None)
                    if oath is None:
                        oath = dict(self.n["trait"], name="oath", _trait=self._enum("DefinedTrait", "oath", "keyword"))
                        d["CardTraits"].append(oath)
                        own.add("oath")
                    oath["_traitValue"] = int(action.get("cost") or 0)
                if kw == "pray" and not any(a.get("trigger") == self._enum("AbilityTrigger", "Pray", "trigger") for a in d["cardAbilities"]):
                    # ...and comes with the same Pray ability on every official card: the unit takes the praying state
                    a, _, _ = self.ability(self.PRAY_ABILITY, len(d["cardAbilities"]) + 1)
                    d["cardAbilities"].insert(0, a)
                if action.get("keyword") and action["keyword"] not in own:
                    d["CardTraits"].append(dict(self.n["trait"], name=action["keyword"],
                                                _trait=self._enum("DefinedTrait", action["keyword"], "keyword")))
                    own.add(action["keyword"])     # the card has it, so it needs no hint (official Pray cards have none)
                if action.get("keyword") == "ferocity":
                    self.notes.append("Ferocity: written like the rebuilt Space Wolves cards, which could not be activated in a match yet")
                if action.get("note"):
                    self.notes.append(f"action limit not written into the game data yet: {action['note']}")
                for fx in action.get("effects") or []:
                    if (fx.get("buff") or {}).get("trait"):
                        given.add(fx["buff"]["trait"])
        alt = card.get("alternative")
        if alt and kind != "tactic":
            # a second action used instead of the first while the condition is NOT met (Wulfen Pack Leader)
            d["hasAlternativeAbility"] = 1
            self.board(d["normalAbilityBoardCriteria"], alt.get("when") or {})
            self.active(d["alternativeAbility"], alt)
        hints = card.get("showTraits")
        if hints is None:                         # keywords the card hands out get a hint on the card
            hints = sorted(given - own)
        for t in hints:                           # ... or exactly the hints the design lists
            name = t.get("name") if isinstance(t, dict) else t
            d["ShowTraits"].append(dict(self.n["trait"], name=name, _trait=self._enum("DefinedTrait", name, "keyword"),
                                        _traitValue=int((t.get("value") if isinstance(t, dict) else 0) or 0)))
        return d, list(dict.fromkeys(self.notes))



# The website's pages: address -> file in content/files. warpforge_server.py asks for this on every
# page request, and this file reloads itself when it changes, so a page added or moved here goes live
# without restarting the server (matches in progress are not dropped). The site's single page
# (creator.html) shows the tab named in the address.
SITE_PAGES = {
    "/": "creator.html",
    "/terms": "terms.html",                       # Terms and Privacy: open to everyone, no account needed
    **{p: "creator.html" for p in (
        "/login", "/creator", "/my-cards", "/review", "/people", "/examples", "/cards", "/keywords",
        "/artwork", "/rules", "/profile", "/server", "/bugs", "/requests", "/moderation", "/uploads", "/unreleased",
        "/guides", "/guides/pc", "/guides/phone", "/guides/android-pc", "/guides/mumu", "/guides/creator", "/guides/card-art")},
    "/robots.txt": "robots.txt",                  # asks search engines and crawlers to keep out (all but /terms)
}
# Files in content/files that are not handed out as plain downloads (/api/v1/content/files/...):
# the guides are for signed-in accounts and reach the site through Revival/Guides; the card creator's
# data (the publisher's card list and rules data) through Revival/CreatorData (security review 27).
CREATOR_DATA_FILES = ("creator-cards.json", "creator-schema.json", "creator-help.json", "keywords.json", "stratagem-types.json")
SITE_PRIVATE_FILES = {"guides.html", *CREATOR_DATA_FILES}
# Folders in content/files (or their .zip stand-ins) never handed out to anyone: the game's card art,
# pop-out masks and frames. The site draws official cards only from the player's own export now.
SITE_PRIVATE_FOLDERS = {"card-art", "card-masks", "card-frames"}


# Official card names, rules text and lore are not handed out by the server: players see them from
# their own game's export (Settings > Mod > Export card art, card-text.json), read in their browser.
# The Card viewer, Examples and the creator's card list get ids, numbers and game data only.
OFFICIAL_TEXT_KEYS = ("name", "text", "lore")          # on a card entry, its detail and its design
OFFICIAL_DATA_TEXT_KEYS = ("m_Name", "cardName")       # inside the game data
_NAMED_REF = re.compile(r"^(\w+ \S+) \((?!\d+\)$).*\)$")


def _data_without_names(obj):
    """The game data without the card's own names, and references like "card AM60 (Timely
    Reinforcements)" as "card AM60". Enum values like "Rare (2)" are kept."""
    if isinstance(obj, dict):
        return {k: _data_without_names(v) for k, v in obj.items() if k not in OFFICIAL_DATA_TEXT_KEYS}
    if isinstance(obj, list):
        return [_data_without_names(v) for v in obj]
    if isinstance(obj, str):
        m = _NAMED_REF.match(obj)
        if m:
            return m.group(1)
    return obj


def _card_without_text(card):
    """A copy of an official card entry (list entry, viewer detail or example) without its name, rules
    text and lore, in itself, its "design" and its "data"."""
    if not isinstance(card, dict):
        return card
    out = {k: v for k, v in card.items() if k not in OFFICIAL_TEXT_KEYS}
    if isinstance(out.get("design"), dict):
        out["design"] = {k: v for k, v in out["design"].items() if k not in OFFICIAL_TEXT_KEYS}
    if "data" in out:
        out["data"] = _data_without_names(out["data"])
    return out


def _site_file_refused(rel):
    """True for a /api/v1/content/files/ path that must not be served: one inside SITE_PRIVATE_FOLDERS,
    or one spelled in a way no file the site serves is (see the security notes)."""
    rel = (rel or "").replace("\\", "/")
    if "~" in rel:
        return True
    parts = [p.lower().rstrip(". ") for p in os.path.normpath(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    return bool(parts) and parts[0] in SITE_PRIVATE_FOLDERS


def _site_block_files():
    """Puts _site_file_refused in front of the running server's page handler (once per process; the
    lists are read from this file's newest copy on every request, so no restart is needed)."""
    import sys
    from urllib.parse import urlparse, unquote
    handler = getattr(sys.modules.get("__main__"), "Handler", None)
    if handler is None or not hasattr(handler, "do_GET") or getattr(handler, "_site_files_wrapped", False):
        return
    original = handler.do_GET

    def do_GET(self):
        try:
            path = urlparse(self.path).path
            lists = sys.modules.get(__name__)
            if path.startswith("/api/v1/content/files/") and getattr(lists, "_site_file_refused", _site_file_refused)(unquote(path[len("/api/v1/content/files/"):])):
                return self._json(404, {"error": "not found"})
        except Exception:
            log.exception("site files: could not check the address")
            return self._json(404, {"error": "not found"})
        return original(self)

    handler.do_GET = do_GET
    handler._site_files_wrapped = True
    log.info("site files: card pictures and short-name paths are refused")


def _site_patch_running_server():
    """Brings the website's page list to a server process started from an older warpforge_server.py,
    so new pages work without a restart (a restart drops matches in progress). This file is reloaded
    inside the running server whenever it changes; on load it wraps the server's page handler once:
    addresses in SITE_PAGES and files in SITE_PRIVATE_FILES are answered from this file's current
    lists, everything else (the game's downloads, the mod, the card pack) goes to the server's own code
    unchanged. A warpforge_server.py that already reads SITE_PAGES itself (it has _site_page) is left alone."""
    import sys
    from urllib.parse import urlparse, unquote
    handler = getattr(sys.modules.get("__main__"), "Handler", None)
    if handler is None or hasattr(handler, "_site_page") or not hasattr(handler, "_serve_file"):
        return
    if getattr(handler, "_site_original_do_GET", None) is None:
        handler._site_original_do_GET = handler.do_GET
        original = handler.do_GET

        def do_GET(self):
            page, private = None, False
            try:
                lists = sys.modules.get(__name__)                     # the newest copy of this file
                path = urlparse(self.path).path.rstrip("/") or "/"
                page = getattr(lists, "SITE_PAGES", {}).get(path)
                if path.startswith("/api/v1/content/files/"):
                    name = os.path.basename(unquote(path[len("/api/v1/content/files/"):]).replace("\\", "/")).lower().rstrip(". ")
                    private = name in getattr(lists, "SITE_PRIVATE_FILES", ())
            except Exception:
                log.exception("site pages: could not read the address")
                page, private = None, False
            if not (page or private):
                return original(self)
            if hasattr(self, "_plain_refused") and self._plain_refused(path):
                return self._refuse_plain()
            if hasattr(self, "_too_many") and self._too_many(self._client()):
                return self._json(429, {"error": "too many requests - wait a minute and try again"})
            if private:
                return self._json(404, {"error": "not found"})
            return self._serve_file(page)

        handler.do_GET = do_GET
        log.info("site pages: the running server now takes its page list from playfab_emu.py (no restart needed)")


def _cards_version(handler):
    """One version for the cards the game plays with, the same on PC and Android: from the card bundles
    the server hands out (allcards*, both platforms). Replays record it, so a replay is only played
    with the cards it was recorded with."""
    shas = []
    for store in (getattr(handler, "bundles", None), getattr(handler, "bundles_android", None)):
        if store is None:
            continue
        shas += [b["sha256"] for b in store.list() if b.get("name", "").startswith("allcards")]
    return hashlib.sha256("|".join(sorted(shas)).encode()).hexdigest()[:16] if shas else ""


# The content bundles (the game's card file, alternate art and text) are only for signed-in players.
# Mods from 0.13.21 send their saved game sign-in with each bundle download (X-Revival-Login).
# Switched on 2026-10-14: a download without a valid sign-in gets 403 (the mod fetches it again after
# signing in). The mod's own update (/api/v1/mod/...) stays open, so old mods can always update.
BUNDLES_NEED_SIGN_IN = True
BUNDLE_PATHS = ("/api/v1/content/bundles/", "/api/v1/content/bundles-android/")


def _bundle_signed_in(handler):
    """True when the request carries the saved game sign-in of an approved, unbanned account."""
    token = str(handler.headers.get("X-Revival-Login") or "").strip()
    if not token or len(token) > 200:
        return False
    api = handler.current_playfab()
    _, user = api._game_login_user_cached(token)
    return user is not None and api._creator_perms(user)["access"] == "approved"


_offence_cache = [None, {}]


def _offence_modes(handler):
    """{eventId: true/false} for every game mode in content/liveops/GameModes.json: whether its
    Offence card pick is on ("offenseCards" in its gameplayVariables; a mode without it: off).
    Re-read when the file changes."""
    api = handler.current_playfab()
    path = os.path.join(api.content_root, "liveops", "GameModes.json")
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return {}
    if _offence_cache[0] != stamp:
        modes = {}
        with open(path, encoding="utf-8-sig") as f:
            for ev in json.load(f).get("RawEvents", []):
                d = (ev or {}).get("Data") or {}
                if isinstance(d.get("eventId"), str):
                    modes[d["eventId"]] = (d.get("gameplayVariables") or {}).get("offenseCards") is True
        _offence_cache[0], _offence_cache[1] = stamp, modes
    return _offence_cache[1]


def _site_content_get(handler, path):
    """Game content answers changed without a restart. Returns True when the request was answered.
    - /api/v1/content/files/revival-settings.json (what the mod reads) gains "offenseCardModes",
      each game mode's Offence card switch (mod 0.13.24 on; older mods read "offenseCards").
    - /api/v1/content/pack is no longer handed out: the mod never used its cards (only its version,
      now "cardsVersion" in the manifest); mods before 0.13.20 still ask and keep their cached copy.
    - /api/v1/content/manifest gains "cardsVersion"."""
    if path == "/api/v1/content/pack":
        handler._json(404, {"error": "not found"})
        return True
    if path.startswith(BUNDLE_PATHS):
        try:
            signed = _bundle_signed_in(handler)
        except Exception:
            log.exception("bundles: could not check the sign-in")
            signed = False
        log.info("bundles: %s %s", path.rsplit("/", 1)[-1], "with a game sign-in" if signed else "WITHOUT a game sign-in")
        if BUNDLES_NEED_SIGN_IN and not signed:
            handler._json(403, {"error": "sign in to the game first"})
            return True
        return False
    if path == "/api/v1/content/files/revival-settings.json":
        try:
            modes = _offence_modes(handler)
        except Exception:
            log.exception("site content: could not read the game modes' Offence switches")
            modes = {}
        plain = handler._send

        def with_modes(code, body, ctype="application/json", *a, **k):
            if code == 200 and ctype == "application/json" and not a and not k.get("gz_body"):
                try:
                    obj = json.loads(body)
                    if isinstance(obj, dict):
                        body = json.dumps(dict(obj, offenseCardModes=modes), ensure_ascii=False).encode("utf-8")
                except ValueError:
                    pass
            return plain(code, body, ctype, *a, **k)
        handler._send = with_modes
        handler._site_content_started = True
        try:
            handler._site_content_original(handler)
        finally:
            del handler._send
        return True
    if path == "/api/v1/content/manifest":
        try:
            cv = _cards_version(handler)
        except Exception:
            log.exception("site content: could not work out cardsVersion")
            cv = ""
        plain = handler._json

        def with_version(code, obj, *a, **k):
            if code == 200 and isinstance(obj, dict):
                obj = dict(obj, cardsVersion=cv)
            return plain(code, obj, *a, **k)
        handler._json = with_version
        handler._site_content_started = True             # from here on the request is the original's
        try:
            handler._site_content_original(handler)
        finally:
            del handler._json
        return True
    return False


def _site_content_patch():
    """Puts _site_content_get (newest copy, read on every request) in front of the running server's
    page handler, once per process."""
    import sys
    from urllib.parse import urlparse
    handler = getattr(sys.modules.get("__main__"), "Handler", None)
    if handler is None or not hasattr(handler, "do_GET") or getattr(handler, "_site_content_wrapped", False):
        return
    original = handler.do_GET

    def do_GET(self):
        try:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path.startswith("/api/v1/content/") and "~" not in path:
                hook = getattr(sys.modules.get(__name__), "_site_content_get", None)
                self._site_content_original = original
                if hook is not None and hook(self, path):
                    return
        except Exception:
            log.exception("site content: could not answer %s", getattr(self, "path", "?"))
            if getattr(self, "_site_content_started", False):
                return
        return original(self)

    handler.do_GET = do_GET
    handler._site_content_wrapped = True
    log.info("site content: the card pack is no longer handed out; the manifest carries cardsVersion; settings carry each mode's Offence switch")


_site_patch_running_server()
try:
    _site_block_files()
except Exception:
    log.exception("site files: could not set up the refusals")
try:
    _site_content_patch()
except Exception:
    log.exception("site content: could not set up")


def _server_log_setup():
    """Keeps everything the server prints in logs/server.log as well (a new file each night, two weeks
    kept, like photon.log, whose own lines stay out of it), so errors can be read after the window has
    scrolled. Also answers the request errors Python prints in the window by itself: a client that hangs
    up mid-request (an https connection dropped, often a scanner sending something that is not a web
    request) is noted in one line; anything else goes to the log with its full traceback. Done in this
    file, which reloads itself, so no restart is needed; each step is done once per server process."""
    import socketserver
    import ssl
    import sys
    from logging.handlers import TimedRotatingFileHandler
    root = logging.getLogger()
    if not root.handlers:
        # loaded before warpforge_server.py set up its console log: set it up the same way now,
        # since its basicConfig does nothing once the root logger has a handler
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not any(getattr(h, "_revival_server_log", False) for h in root.handlers):
        try:
            main = sys.modules.get("__main__")
            base = os.path.dirname(os.path.abspath(getattr(main, "__file__", None) or __file__))
            folder = os.path.join(base, "logs")
            os.makedirs(folder, exist_ok=True)
            handler = TimedRotatingFileHandler(os.path.join(folder, "server.log"), when="midnight", backupCount=14, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            handler.addFilter(lambda record: not record.name.startswith("photon"))
            handler._revival_server_log = True
            root.addHandler(handler)
            log.info("server log: also written to %s", os.path.join(folder, "server.log"))
        except OSError as e:
            log.warning("server log: could not open logs/server.log: %s", e)
    if not getattr(socketserver.BaseServer, "_revival_quiet_errors", False):
        def handle_error(self, request, client_address):
            kind, value = sys.exc_info()[:2]
            where = client_address[0] if isinstance(client_address, tuple) and client_address else "?"
            if kind is not None and issubclass(kind, (ssl.SSLError, ConnectionError, TimeoutError)):
                logging.getLogger("warpforge").info("%s hung up mid-request (%s: %s)", where, kind.__name__, value)
            else:
                logging.getLogger("warpforge").error("request from %s failed", where, exc_info=True)
        socketserver.BaseServer.handle_error = handle_error
        socketserver.BaseServer._revival_quiet_errors = True


try:
    _server_log_setup()
except Exception:                         # never let the log setup stop the game API from loading
    log.exception("server log setup failed")


class PlayFabEmulator:
    def __init__(self, data_root, content_root, log_dir):
        self.content_root = content_root
        self.store = Store(data_root, content_root)
        self.store.banned = self._banned_ids
        # the match service asks for a match ticket whenever accounts are required - from the
        # start, not only once someone has signed in (re-read on every reload of this file)
        photon_emu.REQUIRE[0] = self._settings().get("requireAccount") is True
        if hasattr(photon_emu, "MATCH_DIR"):          # an older match service (before a restart) keeps no records
            photon_emu.MATCH_DIR[0] = self._matches_dir()
        if not photon_emu.REQUIRE[0]:
            # Steam and phone sign-ins carry no proof the server can check, so without accounts
            # anyone who knows a player's Steam id or device id can play as that player.
            log.warning("requireAccount is off: game sign-ins are taken on trust (see README, 'Accounts and safety')")
        os.makedirs(log_dir, exist_ok=True)
        self.log_path = os.path.join(log_dir, "playfab.jsonl")
        self.log_lock = threading.Lock()
        self._log_tidied = ""
        try:
            self._log_tidy()
        except Exception:
            log.exception("could not tidy the request log")
        self._chat_file = os.path.join(data_root, "chat.jsonl")
        self._chat_lock = threading.Lock()
        self._chat = []          # newest last: {"id", "channel", "sender", "message", "cid", "t"}
        try:
            with open(self._chat_file, encoding="utf-8") as f:
                for line in f:
                    try:
                        self._chat.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        self._chat = self._chat[-self.CHAT_KEEP:]
        for e in self._chat:
            e["message"] = self._chat_stamp(e["message"], e["id"])
        self.cloudscript = {  # FunctionName -> handler(player, params) -> result object
            "InitializeLiveOps": self.cs_InitializeLiveOps,
            "FetchTwitchDrops": lambda player, params: [],
            "FetchGroupInvitations": lambda player, params: [],  # alliance invites: none
            "updatePlayerDecks": self.cs_updatePlayerDecks,
            "deletePlayerDeck": self.cs_deletePlayerDeck,
            "FreeDraftEntrance": self.cs_FreeDraftEntrance,
            "PayDraftEntrance": self.cs_PayDraftEntrance,
            "PickDraftWarlord": self.cs_PickDraftWarlord,
            "PickDraftPack": self.cs_PickDraftPack,
            "SaveSelectedDraftCards": self.cs_SaveSelectedDraftCards,
            "RerollDraft": self.cs_RerollDraft,
            "DraftMatchFound": self.cs_DraftMatchFound,
            "DraftMatchFinished": self.cs_DraftMatchFinished,
            "FinishDraftRun": self.cs_FinishDraftRun,
            "CollectDraftRewards": self.cs_CollectDraftRewards,
            "RefundDraftRun": self.cs_RefundDraftRun,
            "ResetCollectedDraft": self.cs_ResetCollectedDraft,
            "ExpireDraftEvent": self.cs_ExpireDraftEvent,
            "validatePlayerDeck": self.cs_validatePlayerDeck,
            "SetGamemodeDefaultDeck": self.cs_SetGamemodeDefaultDeck,
            "OrderPlayerDecks": self.cs_OrderPlayerDecks,
            "onPracticeMatchFinished": self.cs_match_finished,
            "saveMatchRecording": self.cs_saveMatchRecording,
            "pinMatchRecording": self.cs_pinMatchRecording,
            "recoverMatchRecording": self.cs_recoverMatchRecording,
            "removeNotSavedBattleRecord": lambda player, params: {},
            "AddFriend": self.cs_AddFriend,
            "changeCosmetics": self.cs_changeCosmetics,
            "GetLeaderboard": self.cs_GetLeaderboard,
            "GetPlayerProfile": self.cs_GetPlayerProfile,
            "ChangePlayerName": self.cs_ChangePlayerName,
        }

    # ------------------------------------------------------------------ chat
    # The mod sends the game's chat messages here instead of to Photon and asks for new ones every
    # few seconds. Messages are kept in data/chat.jsonl so the chat has a history again.
    CHAT_KEEP = 500       # messages remembered in total
    CHAT_HISTORY = 50     # messages per channel handed to a player who just started the game

    @staticmethod
    def _chat_stamp(message, when_ms):
        """The chat window shows "x ago" from the message's own timestamp (Unix milliseconds).
        Give it the server's time when the game did not write a real one."""
        try:
            data = json.loads(message)
            if isinstance(data, dict) and not (data.get("timestamp") or 0) > 10 ** 12:
                data["timestamp"] = int(when_ms)
                return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (ValueError, TypeError):
            pass
        return message

    def _chat_clean(self, message, sender):
        """The sender's real name on the message (not the one the game wrote), and blocked words starred out."""
        try:
            data = json.loads(message)
        except ValueError:
            return self._word_filter().censor(message)[0]
        if not isinstance(data, dict):
            return message
        player = self.store.load(sender) or {}
        if "playerName" in data and player.get("DisplayName"):
            data["playerName"] = player["DisplayName"]
        if isinstance(data.get("msg"), str):
            data["msg"] = self._word_filter().censor(data["msg"])[0]
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    def api_Revival_ChatSend(self, req, headers):
        channel = str(req.get("channel") or "")
        message = req.get("message")
        if not channel or not isinstance(message, str) or not message or len(message) > 8000:
            return err("InvalidParams", "channel and message are required")
        # The sender is whoever is signed in - never what the request says about itself.
        sender = self.store.session_player_id(headers)
        if not sender:
            return err("NotAuthenticated", "sign in to use the chat", 1074, 401)
        try:
            inside = json.loads(message)
            claimed = str(inside.get("playerId") or inside.get("playFabId") or "") if isinstance(inside, dict) else ""
        except ValueError:
            claimed = ""
        if claimed and claimed != sender:
            return err("NotAuthorized", "a chat message can only be sent as yourself", 1089, 403)
        if not self._allow("chat", sender, 8, 10):
            return err("TooManyAttempts", "slow down - too many messages", 1199, 429)
        message = self._chat_clean(message, sender)
        with self._chat_lock:
            last = self._chat[-1]["id"] if self._chat else 0
            stamp = max(last + 1, int(time.time() * 1000))
            entry = {"id": stamp, "channel": channel, "sender": sender,
                     "message": self._chat_stamp(message, stamp), "cid": str(req.get("cid") or ""), "t": now_iso()}
            self._chat.append(entry)
            if len(self._chat) > self.CHAT_KEEP * 2:      # trim the file now and then
                self._chat = self._chat[-self.CHAT_KEEP:]
                with open(self._chat_file, "w", encoding="utf-8") as f:
                    for e in self._chat:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
            else:
                with open(self._chat_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return ok({"id": entry["id"]})

    # A game that is running asks for chat every few seconds, so that is also how the server knows
    # who is online: seen within this many seconds counts as online.
    ONLINE_SECONDS = 45

    def _friend_statuses(self, pfid):
        """{friend id: "online" | "playing" | "offline"} for this player's friends: playing while
        connected to a match room, online while their game asks for chat, offline otherwise. Only
        for friends who have added this player back - adding someone does not show you when they
        are online until they add you too."""
        now = time.time()
        playing = set()
        try:
            with photon_emu.SERVICE.lock:
                for room in photon_emu.SERVICE.rooms.values():
                    for a in room.actors.values():
                        if a.conn is not None and a.player:
                            playing.add(a.player)
        except Exception:
            pass
        out = {}
        for fid in self._friend_ids(pfid):
            if not re.fullmatch(r"[0-9A-F]{16}", fid) or pfid not in self._friend_ids(fid):
                continue
            if fid in playing:
                out[fid] = "playing"
            elif now - photon_emu.SEEN.get(fid, 0) <= self.ONLINE_SECONDS:
                out[fid] = "online"
            else:
                out[fid] = "offline"
        return out

    def api_Revival_ChatPoll(self, req, headers):
        me = self.store.session_player_id(headers)
        if not me:
            return err("NotAuthenticated", "sign in to use the chat", 1074, 401)
        photon_emu.SEEN[me] = time.time()
        try:
            since = int(req.get("since", -1))
        except (TypeError, ValueError):
            since = -1
        with self._chat_lock:
            if since < 0:                                   # first request: recent history per channel
                per, out = {}, []
                for e in reversed(self._chat):
                    n = per.get(e["channel"], 0)
                    if n < self.CHAT_HISTORY:
                        per[e["channel"]] = n + 1
                        out.append(e)
                out.reverse()
            else:
                out = [e for e in self._chat if e["id"] > since]
            last = self._chat[-1]["id"] if self._chat else 0
            private = [{"from": m["from"], "data": m["data"]} for m in photon_emu.PRIVATE.pop(me, [])
                       if m["t"] > time.time() - self.PRIVATE_SECONDS]
        return ok({"messages": out, "last": max(last, since, 0), "history": since < 0,
                   "friends": self._friend_statuses(me), "private": private})

    # ---- private messages between two players (challenging a friend to a match)
    # The game sent these through the publisher's chat service, which the revival does not use.
    # The mod hands them to the server instead; the reader gets them with its next chat poll.
    PRIVATE_SECONDS = 120          # an invitation nobody collected in this time is dropped
    PRIVATE_MAX_BYTES = 2048
    PRIVATE_WAITING = 50           # per reader

    def api_Revival_ChatPrivate(self, req, headers):
        """{"to": player id, "data": the game's message (text)}: passed on to that player."""
        me = self.store.session_player_id(headers)
        if not me:
            return err("NotAuthenticated", "sign in to use the chat", 1074, 401)
        to, data = str(req.get("to") or ""), req.get("data")
        if not re.fullmatch(r"[0-9A-F]{16}", to) or not isinstance(data, str) or not data or len(data) > self.PRIVATE_MAX_BYTES:
            return err("InvalidParams", "not a private message")
        if to == me:
            return ok({"sent": False})              # the game's messages to itself are not passed back
        if not self._allow("private", me, 30, 60):
            return err("TooManyRequests", "too many messages; wait a moment", 1199, 429)
        if not self._private_rules_ok(data):
            log.info("private message from %s dropped: its challenge rules are not in the expected form", me)
            return err("InvalidParams", "that challenge's rules are not in the expected form")
        if me not in self._friend_ids(to):
            # only players who have the sender on their friend list hear from them, and the sender is
            # not told either way (review #17): no messages from strangers, no "online?" probing
            return ok({"sent": True})
        # The game asks "are you online?" before a challenge and gives up after 4 seconds, too
        # short for a question and its answer to each wait for a chat poll. When the other game
        # asked for chat just now, the server answers for it, in the game's own wording.
        ack = self._online_ack(data) if time.time() - photon_emu.SEEN.get(to, 0) <= self.ACK_SECONDS else None
        if ack is not None:
            return ok({"sent": True, "private": [{"from": to, "data": ack}]})
        with self._chat_lock:
            waiting = photon_emu.PRIVATE.setdefault(to, [])
            waiting.append({"from": me, "data": data, "t": time.time()})
            del waiting[:-self.PRIVATE_WAITING]
        return ok({"sent": True})

    # A friend challenge with custom rules carries them as "revivalRules" in the game's challenge message
    # (mod ChallengeRules.cs). The receiving mod already refuses anything not in the exact form; the server
    # checks the same form so a modified client cannot pass odd values on at all (review #22).
    # key: (min, max, step) - the same table as ChallengeRules.Fields; keep the two in step.
    CHALLENGE_RULE_FIELDS = {"warlordHealthMultiplier": (10, 30, 2), "warlordLifeChange": (-20, 40, 1),
                             "startingHand": (1, 10, 1), "secondPlayerExtraCards": (0, 3, 1),
                             "drawCardsPerTurn": (0, 5, 1), "handLimit": (3, 20, 1), "startingMana": (0, 10, 1),
                             "startingManaSecond": (0, 10, 1), "manaPerTurn": (0, 5, 1), "overtimeTurn": (5, 60, 1),
                             "turnSeconds": (30, 300, 5)}
    # from mod 0.13.24: the Offence card pick (0 off, 1 on) and who goes first (0 the game decides,
    # 1 the challenger, 2 the friend challenged); messages from older mods carry neither
    CHALLENGE_RULE_FIELDS_NEW = dict(CHALLENGE_RULE_FIELDS, offenseCards=(0, 1, 1), firstPlayer=(0, 2, 1))
    # from mod 0.13.33: unspent mana carried over to the next turn (at most this much)
    CHALLENGE_RULE_FIELDS_0133 = dict(CHALLENGE_RULE_FIELDS_NEW, manaAccumulation=(0, 5, 1))
    CHALLENGE_RULE_EVENT = re.compile(r"[A-Za-z0-9_.-]{1,64}")

    def _private_rules_ok(self, data):
        """False only for a message that carries "revivalRules" not in the exact form the mod sends."""
        if "revivalRules" not in data:
            return True
        try:
            msg = json.loads(data)
        except ValueError:
            return True                                  # not JSON: not a challenge, passed on as before
        if not isinstance(msg, dict) or "revivalRules" not in msg:
            return True
        rr = msg["revivalRules"]
        whole = lambda x: isinstance(x, int) and not isinstance(x, bool)
        if not isinstance(rr, dict) or set(rr) != {"event", "v"}:
            return False
        if not isinstance(rr["event"], str) or not self.CHALLENGE_RULE_EVENT.fullmatch(rr["event"]):
            return False
        v = rr["v"]
        if not isinstance(v, dict):
            return False
        fields = next((f for f in (self.CHALLENGE_RULE_FIELDS_0133, self.CHALLENGE_RULE_FIELDS_NEW, self.CHALLENGE_RULE_FIELDS)
                       if set(v) == set(f)), None)
        if fields is None:
            return False
        for k, (lo, hi, step) in fields.items():
            if not whole(v[k]) or not lo <= v[k] <= hi or (v[k] - lo) % step:
                return False
        return v["startingHand"] + v["secondPlayerExtraCards"] <= v["handLimit"]

    ACK_SECONDS = 15               # chat polls come every 3 s in front, every 12 s in the background

    @staticmethod
    def _online_ack(data):
        """The answer to an online check (the same message with the AckUserOnline type), or None."""
        try:
            msg = json.loads(data)
        except ValueError:
            return None
        if not isinstance(msg, dict):
            return None
        kind = msg.get("messageType")
        if kind == 4:
            msg["messageType"] = 5
        elif kind == "CheckUserOnline":
            msg["messageType"] = "AckUserOnline"
        else:
            return None
        return json.dumps(msg, separators=(",", ":"))

    # ---- reports: "Report message" on a player in the global chat
    # The game itself only sent these to its analytics service. The mod sends them here, they are
    # kept in data/chat_reports.json and admins see them on the creator site's People page.
    REPORTS_KEEP = 500

    def _reports_path(self):
        return os.path.join(self.store.root, "chat_reports.json")

    def _reports(self):
        try:
            with open(self._reports_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _reports_save(self, reports):
        path = self._reports_path()
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(reports[-self.REPORTS_KEEP:], f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    def api_Revival_ChatReport(self, req, headers):
        """{"player": id of whoever wrote the message, "msg": its text, "timestamp": its time}."""
        reporter = self.store.session_player_id(headers)
        if not reporter:
            return err("NotAuthenticated", "sign in to report a message", 1074, 401)
        reported = str(req.get("player") or "").strip().upper()
        text = req.get("msg") if isinstance(req.get("msg"), str) else ""
        if not re.fullmatch(r"[0-9A-F]{16}", reported) or self.store.load(reported) is None:
            return err("InvalidParams", "no such player")
        if reported == reporter:
            return ok({"reported": False})
        if not self._allow("report", reporter, 10, 3600):
            return err("TooManyAttempts", "too many reports - try again later", 1199, 429)
        # Use the server's own copy of the message when it still has it, so a report cannot put
        # words in someone's mouth; otherwise keep what the reporter's game showed, marked as such.
        found = None
        with self._chat_lock:
            for e in reversed(self._chat):
                if e["sender"] != reported:
                    continue
                try:
                    data = json.loads(e["message"])
                except ValueError:
                    data = None
                said = data.get("msg") if isinstance(data, dict) else e["message"]
                stamp = data.get("timestamp") if isinstance(data, dict) else None
                if said == text or (stamp and stamp == req.get("timestamp")):
                    found = (e, said if isinstance(said, str) else "")
                    break
        with self.store.lock:
            reports = self._reports()
            entry = {"id": uuid.uuid4().hex[:12], "at": now_iso(), "reporter": reporter, "player": reported,
                     "msg": (found[1] if found else text)[:1000], "verified": found is not None,
                     "channel": found[0]["channel"] if found else "", "sent": found[0]["t"] if found else "",
                     "closed": False}
            # one open report per reporter and message is enough
            if not any(r["reporter"] == reporter and r["player"] == reported and r["msg"] == entry["msg"]
                       and not r.get("closed") for r in reports):
                reports.append(entry)
                self._reports_save(reports)
                log.info("chat report %s: %s reported %s", entry["id"], reporter, reported)
        return ok({"reported": True})

    # ---------------------------------------------------------------- server settings (founding admins)
    # The site's "Server" page edits the game modes (content/liveops/GameModes.json) and the server
    # settings (content/files/revival-settings.json). Only founding admins (data/creator_admins.json)
    # may read or change them. Every save keeps the previous file in data/config-history/ and is
    # refused if the file changed since the page loaded it (someone edited it by hand meanwhile).
    CONFIG_FILES = {"modes": ("liveops", "GameModes.json"), "settings": ("files", "revival-settings.json")}
    CONFIG_MAX_BYTES = 512 * 1024
    CONFIG_HISTORY_KEEP = 30
    _config_lock = threading.Lock()

    # gameplayVariables: whole numbers in a sane range, and switches
    MODE_NUMBERS = {"maxLegendaries": (0, 200), "numberOfCopiesOtherRarities": (1, 60), "numberOfCopiesLegendary": (1, 60),
                    "slotsPerSide": (1, 8), "warlordLifeChange": (-100, 100), "deckSize": (1, 200), "overtimeTurn": (1, 100),
                    "startingMana": (0, 50), "startingManaSecond": (0, 50), "addedManaSecond": (0, 50), "manaPerTurn": (0, 50),
                    "drawCardsPerTurn": (0, 20), "manaAccumulation": (0, 50), "handLimit": (1, 30),
                    "startingHand": (0, 10), "secondPlayerExtraCards": (0, 10)}
    # "offenseCards" is the revival's own: the Offence card pick in that mode (mod 0.13.24 on; a mode
    # without it has no pick)
    MODE_SWITCHES = ("shouldReshuffle", "showMulligan", "assignDefensiveCardsInEditMode", "alternativeTraitLogic", "offenseCards")
    SETTING_SWITCHES = ("offenseCards", "onlyRevivalMod", "requireAccount", "requireHttps", "requireEncryptedMatches")
    # every top-level key the server or the mod reads; keys starting with "_" are notes (review #21)
    SETTING_KEYS = SETTING_SWITCHES + ("turnSeconds", "practiceClassicWarlordLife", "rulesVersion", "minModVersion", "creatorUrl",
                                       "relays", "accounts", "longGame", "photon", "logDays", "matchPort", "startingHands",
                                       "bugReports", "bugStorageMB")

    def _config_unknown(self, which, data):
        """Top-level settings nothing reads (most likely a typo) - warned about, not refused."""
        if which != "settings" or not isinstance(data, dict):
            return []
        return sorted(k for k in data if not str(k).startswith("_") and k not in self.SETTING_KEYS)

    def _config_founder(self, req):
        me = self._creator_account(req)
        if not me:
            return None
        perms = self._creator_perms(me)
        return me if perms["fixed"] else None

    def _config_path(self, which):
        return os.path.join(self.content_root, *self.CONFIG_FILES[which])

    def _config_read(self, which):
        try:
            with open(self._config_path(which), "rb") as f:
                return f.read()
        except OSError:
            return b""

    @staticmethod
    def _config_stamp(raw):
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def _whole(v):
        return isinstance(v, int) and not isinstance(v, bool)

    def _config_check(self, which, data):
        """None when the new content may be saved, else what is wrong with it."""
        if not isinstance(data, dict):
            return "the file must be a JSON object"
        if which == "modes":
            events = data.get("RawEvents")
            if not isinstance(events, list) or not events:
                return '"RawEvents" must be a list of game modes'
            seen = set()
            for e in events:
                d = e.get("Data") if isinstance(e, dict) else None
                if not isinstance(d, dict) or not isinstance(d.get("eventId"), str) or not d["eventId"].strip():
                    return "every game mode needs Data.eventId"
                eid = d["eventId"]
                if eid in seen:
                    return "two game modes are called " + eid
                seen.add(eid)
                if not isinstance(d.get("$type"), str):
                    return eid + ": Data.$type is missing"
                gv = d.get("gameplayVariables")
                if gv is None:
                    continue
                if not isinstance(gv, dict):
                    return eid + ": gameplayVariables must be an object"
                for k, (lo, hi) in self.MODE_NUMBERS.items():
                    if k in gv and (not self._whole(gv[k]) or not lo <= gv[k] <= hi):
                        return f"{eid}: {k} must be a whole number from {lo} to {hi}"
                for k in self.MODE_SWITCHES:
                    if k in gv and not isinstance(gv[k], bool):
                        return f"{eid}: {k} must be true or false"
                if self._whole(gv.get("startingHand")) and self._whole(gv.get("handLimit")) and gv["startingHand"] > gv["handLimit"]:
                    return f"{eid}: the starting hand is bigger than the hand limit"
            return None
        for k in self.SETTING_SWITCHES:
            if k in data and not isinstance(data[k], bool):
                return k + " must be true or false"
        if "turnSeconds" in data and (not self._whole(data["turnSeconds"]) or not 10 <= data["turnSeconds"] <= 3600):
            return "turnSeconds must be a whole number from 10 to 3600"
        if "practiceClassicWarlordLife" in data and (not self._whole(data["practiceClassicWarlordLife"]) or not 1 <= data["practiceClassicWarlordLife"] <= 1000):
            return "practiceClassicWarlordLife must be a whole number from 1 to 1000"
        for k in ("rulesVersion", "minModVersion", "creatorUrl"):
            if k in data and not (isinstance(data[k], str) and len(data[k]) <= 200 or self._whole(data[k])):
                return k + " must be a short text"
        if "relays" in data and not (isinstance(data["relays"], list) and all(isinstance(r, str) for r in data["relays"])):
            return "relays must be a list of addresses"
        for k in ("accounts", "longGame", "photon"):
            if k in data and not isinstance(data[k], dict):
                return k + " must be an object"
        if "autoApprove" in (data.get("accounts") or {}) and not isinstance(data["accounts"]["autoApprove"], bool):
            return "accounts.autoApprove must be true or false"
        return None

    def api_Revival_CreatorServerConfig(self, req, headers):
        """Founding admins: the game modes and server settings files, each with a stamp to save against."""
        me = self._config_founder(req)
        if not me:
            return err("NotAuthorized", "only founding admins can see the server settings")
        out = {}
        for which in self.CONFIG_FILES:
            raw = self._config_read(which)
            try:
                data = json.loads(raw.decode("utf-8-sig")) if raw else {}
                problem = None
            except ValueError as e:
                data, problem = None, "the file on the server is not valid JSON (%s); fix it by hand" % e
            out[which] = {"data": data, "stamp": self._config_stamp(raw), "problem": problem}
        return ok({"files": out})

    def api_Revival_CreatorServerConfigSave(self, req, headers):
        """Founding admins: {"file": "modes" | "settings", "data": {...}, "stamp"} replaces that file."""
        me = self._config_founder(req)
        if not me:
            return err("NotAuthorized", "only founding admins can change the server settings")
        # the password again, so a session token lifted from a browser cannot change the server (review #15)
        if not self._allow("config-pass-fail", me["name"].lower(), 5, 900, count=False):
            return err("TooManyAttempts", "too many wrong passwords - wait a few minutes and try again", 1199, 429)
        if not self._creator_check_passphrase(me, req.get("passphrase")):
            self._allow("config-pass-fail", me["name"].lower(), 5, 900)
            return err("InvalidPassword", "enter your password to save server settings")
        which = req.get("file")
        if which not in self.CONFIG_FILES:
            return err("InvalidParams", "unknown settings file")
        data = req.get("data")
        problem = self._config_check(which, data)
        if problem:
            return err("InvalidParams", problem)
        body = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        if len(body) > self.CONFIG_MAX_BYTES:
            return err("InvalidParams", "that is too big for a settings file")
        with self._config_lock:
            raw = self._config_read(which)
            if self._config_stamp(raw) != str(req.get("stamp") or ""):
                return err("Conflict", "the file changed on the server since this page loaded it; reload the page and make the change again")
            history = os.path.join(self.store.root, "config-history")
            os.makedirs(history, exist_ok=True)
            base = os.path.splitext(self.CONFIG_FILES[which][1])[0]
            if raw:
                with open(os.path.join(history, "%s-%s.json" % (base, time.strftime("%Y%m%d-%H%M%S", time.gmtime()))), "wb") as f:
                    f.write(raw)
            old = sorted(n for n in os.listdir(history) if n.startswith(base + "-"))
            for n in old[:-self.CONFIG_HISTORY_KEEP]:
                try:
                    os.remove(os.path.join(history, n))
                except OSError:
                    pass
            path = self._config_path(which)
            with open(path + ".tmp", "wb") as f:
                f.write(body)
            _retry_locked(lambda: os.replace(path + ".tmp", path))
        log.info("server settings: %s saved %s", me["name"], self.CONFIG_FILES[which][1])
        return ok({"stamp": self._config_stamp(body), "unknown": self._config_unknown(which, data)})

    def api_Revival_CreatorReports(self, req, headers):
        """Admins: reported chat messages, newest first (closed ones only with "closed": true)."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see reports")
        users = self._creator_users_view()
        banned = self._banned_ids()

        def who(pfid):
            p = self.store.load(pfid) or {}
            _, account = self._creator_account_of_player(users, pfid)
            return {"player": pfid, "code": self.friend_code(pfid), "name": p.get("DisplayName") or "",
                    "account": account["name"] if account else "", "banned": pfid in banned}

        show_closed = req.get("closed") is True
        out = []
        for r in reversed(self._reports()):
            if r.get("closed") and not show_closed:
                continue
            out.append(dict(r, reporter=who(r["reporter"]), player=who(r["player"])))
        return ok({"reports": out[:200]})

    def api_Revival_CreatorReportClose(self, req, headers):
        """Admins: mark a report dealt with ({"id", "closed": true/false})."""
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_role(req) < 2:
                return err("NotAuthorized", "only admins can close reports")
            reports = self._reports()
            entry = next((r for r in reports if r.get("id") == req.get("id")), None)
            if entry is None:
                return err("InvalidParams", "no such report")
            closed = req.get("closed") is not False
            entry.update(closed=closed, closedBy=me["name"] if closed else "", closedAt=now_iso() if closed else "")
            self._reports_save(reports)
        return ok({"id": entry["id"], "closed": closed})

    # ------------------------------------------------------------------ card creator
    # The creator page (content/files/creator.html) stores card designs here. A design is only a
    # description kept in data/custom_cards/<id>.json; nothing reaches the game until approved
    # designs are built into the card files and the custom mode is switched on.
    # draft (private to the author) -> pending (waiting for review) -> approved; rejected goes back
    # to the author with a note. Approved cards are what the first custom build will contain.
    CREATOR_STATUSES = ("pending", "approved", "rejected")
    CREATOR_KINDS = ("troop", "tactic", "hero")

    def _creator_dir(self):
        d = os.path.join(self.store.root, "custom_cards")
        os.makedirs(d, exist_ok=True)
        return d

    # Creator accounts: a name and a passphrase, so drafts and rejected cards stay with their author.
    def _creator_users(self):
        """The accounts file, freshly read: a private copy for code that changes accounts and saves."""
        try:
            with open(os.path.join(self.store.root, "creator_users.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _creator_users_save(self, users):
        path = os.path.join(self.store.root, "creator_users.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
        self._users_cache = None

    # Efficiency E1: the accounts file read once and kept until it changes on disk (its modified time
    # and size; a save here also drops it), with lookups from site session, game login and game player
    # to the account. Read-only: code that changes accounts reads its own copy (_creator_users) and
    # saves it; accounts handed out from here are copies.
    def _users_cached(self):
        path = os.path.join(self.store.root, "creator_users.json")
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        cached = getattr(self, "_users_cache", None)
        if cached is not None and cached[0] == stamp:
            return cached
        users = self._creator_users() if stamp is not None else {}
        if not isinstance(users, dict):
            users = {}
        sessions, logins, players = {}, {}, {}
        for key, user in users.items():
            if not isinstance(user, dict):
                continue
            for d in user.get("sessions") or []:
                sessions.setdefault(d, key)               # the first account holding it, as a scan in file order found
            for h in self._game_login_hashes(user):
                logins.setdefault(h, key)
            if user.get("player"):
                players.setdefault(user["player"], key)
        cached = (stamp, users, sessions, logins, players)
        self._users_cache = cached
        return cached

    def _creator_users_view(self):
        """The accounts, from memory. Do not change what this returns."""
        return self._users_cached()[1]

    @staticmethod
    def _creator_hash(passphrase, salt):
        return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), bytes.fromhex(salt), 200000).hex()

    # Site sessions stop working after SITE_SESSION_DAYS without use (security review #15). When each was
    # last used is kept in its own small file with its own lock, so a lookup can note the time even
    # while the caller holds the store lock. Sessions from before this existed count from first use.
    SITE_SESSION_DAYS = 30

    def _site_seen(self):
        if getattr(self, "_site_seen_cache", None) is None:
            try:
                with open(os.path.join(self.store.root, "creator_sessions_seen.json"), encoding="utf-8") as f:
                    self._site_seen_cache = {k: float(v) for k, v in json.load(f).items()}
            except (OSError, ValueError, TypeError, AttributeError):
                self._site_seen_cache = {}
        return self._site_seen_cache

    def _site_seen_save(self):
        now = time.time()
        seen = self._site_seen()
        # An expired session is kept as 0 (still refused), never deleted: a session with no time at all
        # counts as one from before this existed and would be accepted again (review #15a). Entries go
        # only once the session is gone from every account (signed out, password changed, replaced).
        live = {d for u in self._creator_users_view().values() for d in (u.get("sessions") or [])}
        for k in list(seen):
            if k not in live:
                del seen[k]
            elif seen[k] < now - self.SITE_SESSION_DAYS * 86400:
                seen[k] = 0
        for k in live - set(seen):
            seen[k] = now                             # sessions from before this existed: their 30 days start now
        path = os.path.join(self.store.root, "creator_sessions_seen.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(path + ".tmp", path)

    def _site_session_fresh(self, digest, new=False):
        """True if this site session may be used now; notes the time of use (saved at most hourly per session)."""
        if not hasattr(self, "_site_seen_lock"):
            self._site_seen_lock = threading.Lock()
        now = time.time()
        with self._site_seen_lock:
            seen = self._site_seen()
            last = seen.get(digest)
            if last is not None and not new and last < now - self.SITE_SESSION_DAYS * 86400:
                return False
            if new or last is None or last < now - 3600:
                seen[digest] = now
                try:
                    self._site_seen_save()
                except OSError:
                    log.warning("could not save when site sessions were last used")
        return True

    def _creator_session_owner(self, req):
        """The account a site session token belongs to (expired sessions belong to nobody), or None."""
        token = str(req.get("session") or "")
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._users_cached()
        key = cached[2].get(digest)
        if key is None:
            return None
        return copy.deepcopy(cached[1][key]) if self._site_session_fresh(digest) else None

    def _creator_user(self, req):
        """Display name of the signed-in author, or None."""
        user = self._creator_session_owner(req)
        if user is None:
            return None
        return None if self._creator_perms(user)["access"] in ("banned", "requested") else user["name"]

    # Wrong passwords are limited per address (20 in 15 minutes) and per account name (10). The
    # per-name limit alone would let anyone lock any account - an admin's included - by guessing
    # badly on purpose, so it does not apply to the addresses that account has signed in from before.
    KNOWN_ADDRESSES_KEPT = 10

    @staticmethod
    def _address_mark(user, where):
        """A fingerprint of an address, salted per account (the address itself is never stored)."""
        return hashlib.sha256((str(user.get("salt") or "") + "|addr|" + where).encode("utf-8")).hexdigest()[:32]

    def _name_locked(self, name, where, user):
        """True when too many wrong passwords were tried for this name and `where` is not one of
        the addresses the account has signed in from."""
        if self._allow("signin-fail-name", name.lower(), 10, 900, count=False):
            return False
        return not (user and where != "unknown" and self._address_mark(user, where) in (user.get("knownAddresses") or []))

    def _remember_address(self, user, where):
        if where == "unknown":
            return
        mark = self._address_mark(user, where)
        known = [m for m in user.get("knownAddresses") or [] if m != mark]
        user["knownAddresses"] = (known + [mark])[-self.KNOWN_ADDRESSES_KEPT:]

    def _count_wrong_password(self, name, where):
        self._allow("signin-fail", where, 20, 900)
        if name:
            self._allow("signin-fail-name", name.lower(), 10, 900)

    def api_Revival_CreatorSignIn(self, req, headers):
        """Signs in, or creates the account when the name is new and "create" is set."""
        name = " ".join(str(req.get("name") or "").split())[:30]
        passphrase = str(req.get("passphrase") or "")
        if req.get("create"):       # the rules apply to new accounts; existing ones sign in as they were made
            if not re.fullmatch(r"[\w.'-]{5,30}", name):
                return err("InvalidParams", "usernames are 5 to 30 letters, digits, . ' - or _, without spaces")
            if self._word_filter().found(name):
                return err("InvalidParams", "that username contains a word that is not allowed here")
            if not 6 <= len(passphrase) <= 200:
                return err("InvalidParams", "the password must be at least 6 characters")
            if any(ch.isspace() for ch in passphrase):
                return err("InvalidParams", "the password cannot contain spaces")
            if self._terms_version() and req.get("agreeTerms") is not True:
                return err("TermsNotAgreed", "tick the box to agree to the Terms and Privacy notice first", 1002, 403)
        elif not name or not passphrase:
            return err("InvalidParams", "enter a username and a password")
        where = self._address(headers)
        slow = err("TooManyAttempts", "too many attempts - wait a few minutes and try again", 1199, 429)
        if not self._allow("signin-fail", where, 20, 900, count=False):
            return slow
        with self.store.lock:
            users = self._creator_users()
            user = users.get(name.lower())
            if self._name_locked(name, where, user):
                return slow
            new_request = False
            if user is None:
                if not req.get("create"):
                    self._allow("signin-fail", where, 20, 900)
                    return err("AccountNotFound", "no account with that name yet - use 'Create an account' below")
                if not self._allow("signup", where, 5, 3600):
                    return err("TooManyAttempts", "too many new accounts from this connection - try again in an hour", 1199, 429)
                salt = os.urandom(16).hex()
                new_request = not self._creator_auto_approve()
                user = users[name.lower()] = {"name": name, "salt": salt, "hash": self._creator_hash(passphrase, salt),
                                              "sessions": [], "created": now_iso(),
                                              "access": "approved" if self._creator_auto_approve() else "requested"}
                self._terms_agree(user)
            elif req.get("create"):
                return err("NameNotAvailable", "that username is taken - sign in instead, or pick another username")
            elif not hmac.compare_digest(user["hash"], self._creator_hash(passphrase, user["salt"])):
                self._count_wrong_password(name, where)
                return err("InvalidPassword", "wrong password")
            if self._creator_perms(user)["access"] == "banned":
                return err("AccountBanned", "this account has been banned")
            if user.get("deleteSticks") and user.get("deleteAt"):
                return err("AccountBeingDeleted", "this account was banned and is being deleted", 1002, 403)
            if self._terms_missing(user):
                if req.get("agreeTerms") is not True:
                    return err("TermsNotAgreed", "the Terms and Privacy notice have changed - read them and agree to carry on", 1002, 403)
                self._terms_agree(user)
            cancelled = self._account_delete_cancel(user)
            if cancelled:
                log.info("account %s signed in again: its deletion is cancelled", user["name"])
            self._remember_address(user, where)
            if self._creator_perms(user)["access"] == "requested":
                # A new account that an admin has not approved yet: nothing on the site or in the
                # game until then (it is saved, so it appears on the People page).
                self._creator_users_save(users)
                if new_request:
                    self._discord_request(user["name"])
                return err("AccountNotApproved", "your account is waiting for an admin to approve it - try again once it is approved", 1002, 403)
            token = uuid.uuid4().hex + uuid.uuid4().hex
            user["sessions"] = (user.get("sessions", []) + [hashlib.sha256(token.encode()).hexdigest()])[-5:]
            self._creator_users_save(users)
        self._site_session_fresh(hashlib.sha256(token.encode()).hexdigest(), new=True)
        return ok({"name": user["name"], "session": token, "account": self._creator_perms(user), "deletionCancelled": cancelled})

    # New account requests to a Discord channel. data/discord-webhook.json holds
    #   {"url": "<the channel's webhook address>", "pingRole": "<role id to @, optional>"}
    # (Discord: channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL; the role id
    # from Server settings > Roles > right-click the role > Copy Role ID, with Developer Mode on).
    # The webhook address lets anyone post in that channel, so it lives in data/: never served, never
    # in the repo, never logged. No file (or no valid address) = off. The note only gives the
    # username and a link to the site's Requests page; approving is done on the site.
    # Requests close together go out as one message, at most one a minute, so a burst of sign-ups
    # cannot flood the channel; the posting runs on its own thread and never holds up a sign-in.
    DISCORD_FILE = "discord-webhook.json"
    DISCORD_HOOK = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/webhooks/\d{5,25}/[\w-]{20,100}")
    DISCORD_GAP = 60
    _discord_state = {"lock": threading.Lock(), "names": [], "busy": False, "last": 0.0}

    def _discord_settings(self):
        try:
            with open(os.path.join(self.store.root, self.DISCORD_FILE), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        url = str(d.get("url") or "").strip() if isinstance(d, dict) else ""
        if not self.DISCORD_HOOK.fullmatch(url):
            return None
        role = str(d.get("pingRole") or "").strip()
        return url, (role if re.fullmatch(r"\d{5,25}", role) else "")

    def _discord_request(self, name):
        if self._discord_settings() is None:
            return
        st = self._discord_state
        with st["lock"]:
            st["names"].append(name)
            if st["busy"]:
                return                      # the running sender picks this one up
            st["busy"] = True
        threading.Thread(target=self._discord_send, name="discord-requests", daemon=True).start()

    def _discord_send(self):
        st = self._discord_state
        while True:
            wait = st["last"] + self.DISCORD_GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            with st["lock"]:
                names, st["names"] = st["names"], []
                if not names:
                    st["busy"] = False
                    return
            try:
                self._discord_post(names)
            except Exception as e:          # the error only, never the address (it is a secret)
                log.warning("discord: could not post %d account request(s): %s", len(names), type(e).__name__)
            st["last"] = time.time()

    def _discord_post(self, names):
        import urllib.request
        got = self._discord_settings()
        if got is None:
            return
        url, role = got
        site = str(self._settings().get("creatorUrl") or "").rstrip("/")
        base = site.split("/creator")[0] if site else ""
        with self.store.lock:
            waiting = sum(1 for u in self._creator_users_view().values() if self._creator_perms(u)["access"] == "requested")
        shown = ", ".join("`" + n + "`" for n in names[:20]) + (f" and {len(names) - 20} more" if len(names) > 20 else "")
        text = (f"<@&{role}> " if role else "") + \
            ("New account request: " if len(names) == 1 else f"{len(names)} new account requests: ") + shown + \
            f"\n{waiting} waiting in all. " + (f"Review them at {base}/requests" if base else "Review them on the site's Requests page.")
        body = json.dumps({"content": text[:1900], "username": "Warpforge Revival",
                           "allowed_mentions": {"parse": [], "roles": [role] if role else []}}).encode()
        rq = urllib.request.Request(url, data=body, method="POST",
                                    headers={"Content-Type": "application/json", "User-Agent": "WarpforgeRevival (server, 1.0)"})
        with urllib.request.urlopen(rq, timeout=15) as resp:
            log.info("discord: posted %d account request(s) (HTTP %s)", len(names), resp.status)

    def api_Revival_CreatorSignOut(self, req, headers):
        token = str(req.get("session") or "")
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with self.store.lock:
            users = self._creator_users()
            for user in users.values():
                if digest in user.get("sessions", []):
                    user["sessions"].remove(digest)
                    self._creator_users_save(users)
        return ok({})

    # ---- terms and privacy notice; deleting an account
    # content/files/terms.html (served at /terms, no account needed) carries its version in
    # <meta name="terms-version" content="...">. An account records the version it agreed to
    # ("termsAgreed", "termsAgreedAt"). New accounts must agree ("agreeTerms": true). An existing
    # account that has not agreed to the current version is asked at its next password sign-in on the
    # site or in the game (answer "TermsNotAgreed"; the same request again with agreeTerms goes
    # through). Sessions and saved game logins made before keep working.
    #
    # Deleting: the account owner (Profile, password and name typed again) or someone ranked above
    # it (People) sets "deleteAt" 7 days ahead. The account is signed out of the site and the game at
    # once; signing in with the password before then cancels it. After that the sweep (run at most
    # hourly, on any request) erases it - see _account_erase for exactly what goes and what stays.
    DELETE_DAYS = 7
    DELETED_NAME = "deleted account"
    TERMS_META = re.compile(r'<meta\s+name="terms-version"\s+content="([^"]{1,40})"', re.I)

    def _terms_version(self):
        """The version of terms.html, or "" when the server has no terms page (nothing is asked then)."""
        path = os.path.join(self.content_root, "files", "terms.html")
        try:
            stamp = os.path.getmtime(path)
            if getattr(self, "_terms_stamp", None) != stamp:
                with open(path, encoding="utf-8") as f:
                    m = self.TERMS_META.search(f.read(8192))
                self._terms_cached, self._terms_stamp = (m.group(1) if m else ""), stamp
            return self._terms_cached
        except OSError:
            return ""

    def _terms_missing(self, user):
        """True when this account has not agreed to the current terms."""
        version = self._terms_version()
        return bool(version) and user.get("termsAgreed") != version

    def _terms_agree(self, user):
        user["termsAgreed"], user["termsAgreedAt"] = self._terms_version(), now_iso()

    @staticmethod
    def _iso_in(seconds):
        return datetime.fromtimestamp(time.time() + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"

    def _account_delete_start(self, user, by):
        """Marks the account for deletion and signs it out everywhere (the caller saves)."""
        user["deleteAt"] = self._iso_in(self.DELETE_DAYS * 86400)
        user["deleteAsked"], user["deleteBy"] = now_iso(), by
        # owner's rule (2026-10-11, in the Terms): deleting an account that is banned at the time
        # cannot be undone by the account itself, not even after an unban; only staff ("Keep account")
        if self._creator_perms(user)["access"] == "banned":
            user["deleteSticks"] = True
        user["sessions"] = []
        for k in ("gameLoginHash", "gameLoginAt", "gameLogins"):
            user.pop(k, None)
        for pfid in self._account_players(user):
            self.store.sign_out(pfid, "This account is being deleted. Sign in again with your password within %d days to keep it." % self.DELETE_DAYS)

    @staticmethod
    def _account_delete_cancel(user, by_staff=False):
        """True when a deletion was waiting and is now cancelled. A sticking deletion (the account was
        banned when it was asked for) is cancelled only by staff."""
        if not user.get("deleteAt") or (user.get("deleteSticks") and not by_staff):
            return False
        for k in ("deleteAt", "deleteAsked", "deleteBy", "deleteSticks"):
            user.pop(k, None)
        return True

    def _account_players(self, user):
        """Ids of the game players that belong to an account: the linked one, and any made for it by name."""
        out = [user["player"]] if user.get("player") else []
        names = {str(user.get("name") or "").lower(), str(user.get("renamedFrom") or "").lower()} - {""}
        try:
            files = os.listdir(self.store.players_dir)
        except OSError:
            files = []
        for f in files:
            if f.endswith(".json") and f[:-5] not in out:
                p = self.store.load(f[:-5])
                login = (p or {}).get("Login")
                if isinstance(login, dict) and str(login.get("account") or "").lower() in names:
                    out.append(f[:-5])
        return out

    def api_Revival_CreatorDeleteAccount(self, req, headers):
        """The signed-in account asks to be deleted: {"passphrase", "confirm": its own name}."""
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_perms(me)["access"] == "banned":
                return err("NotAuthorized", "sign in first")
            if not self._creator_check_passphrase(me, req.get("passphrase")):
                return err("InvalidPassword", "wrong password")
            if " ".join(str(req.get("confirm") or "").split()).lower() != me["name"].lower():
                return err("InvalidParams", "type your account name exactly to confirm")
            if me["name"].lower() in self._creator_founders():
                return err("NotAuthorized", "the founding owner's account cannot be deleted from the site")
            users = self._creator_users()
            user = users[me["name"].lower()]
            self._account_delete_start(user, user["name"])
            self._creator_users_save(users)
        log.info("account %s asked to be deleted (on %s)", user["name"], user["deleteAt"])
        return ok({"deleteAt": user["deleteAt"], "days": self.DELETE_DAYS})

    def _account_sweep(self, force=False):
        """Erases the accounts whose deletion time has passed (at most hourly unless forced)."""
        now = time.time()
        if not force and now - getattr(self, "_sweep_at", 0) < 3600:
            return []
        self._sweep_at = now
        done = []
        with self.store.lock:
            users = self._creator_users()
            due = [k for k, u in users.items() if isinstance(u, dict) and u.get("deleteAt") and str(u["deleteAt"]) <= now_iso()
                   and k not in self._creator_founders()]
            for key in due:
                try:
                    self._account_erase(users, key)
                    done.append(key)
                except Exception:
                    log.exception("could not finish deleting the account %s", key)
            if done:
                self._creator_users_save(users)
            try:
                self._bugs_prune_closed()
            except Exception:
                log.exception("could not tidy the attachments of closed bug reports")
        return done

    def _account_erase(self, users, key):
        """Deletes one account for good (the caller holds the store lock and saves `users`):
          gone     the account, its game player(s) with decks, progress, friends list and recordings,
                   the player on other players' friend lists, its drafts / pending / rejected /
                   discarded cards and their artwork;
          changed  its approved cards stay, credited to "deleted account"; its chat messages still
                   kept are emptied and shown as from "deleted account";
          kept     other players' match history, chat reports and bans (moderation records)."""
        user = users[key]
        name = str(user.get("name") or key)
        names = {key, name.lower(), str(user.get("renamedFrom") or "").lower()} - {""}
        players = self._account_players(user)
        for pfid in players:
            self.store.sign_out(pfid, "This account has been deleted.")
            try:
                os.remove(self.store._path(pfid))
            except (OSError, ValueError):
                pass
            shutil.rmtree(self._replay_dir(pfid), ignore_errors=True)
        if players:
            for code, d in self._codes_all().items():          # their replay codes (copies of their recordings)
                if d.get("owner") in players:
                    try:
                        os.remove(os.path.join(self._codes_dir(), code + ".json"))
                    except OSError:
                        pass
            gone = set(players)
            for other in list(self._all_players()):
                friends = self._friends(other)
                kept = [f for f in friends if not (isinstance(f, dict) and f.get("id") in gone)]
                if len(kept) != len(friends):
                    self._save_friends(other, kept)
            self._chat_forget(gone)
        d = self._creator_dir()
        cards = changed = 0
        for f in sorted(os.listdir(d)):
            if not re.fullmatch(r"CUSTOM-\d{4}\.(json|discarded)", f):
                continue
            path = os.path.join(d, f)
            try:
                with open(path, encoding="utf-8") as fh:
                    card = json.load(fh)
            except (OSError, ValueError):
                continue
            if str(card.get("author") or "").lower() not in names:
                continue
            if f.endswith(".json") and card.get("status") == "approved":
                card["author"] = self.DELETED_NAME
                if isinstance(card.get("rulesAccepted"), dict):
                    card["rulesAccepted"]["by"] = self.DELETED_NAME
                with open(path + ".tmp", "w", encoding="utf-8") as fh:
                    json.dump(card, fh, ensure_ascii=False, indent=1)
                os.replace(path + ".tmp", path)
                changed += 1
            else:
                base = self._creator_art_path(f[:-5] if f.endswith(".json") else f[:-10])[:-4]
                for tail in self.ART_TAILS:
                    try:
                        os.remove(base + tail)
                    except OSError:
                        pass
                os.remove(path)
                cards += 1
        self._bugs_forget_author(names)
        del users[key]
        log.info("account %s deleted: %d game player(s), %d card(s) removed, %d approved card(s) now by '%s'",
                 name, len(players), cards, changed, self.DELETED_NAME)

    def _chat_forget(self, pfids):
        """Empties the kept chat messages of these players and shows them as from "deleted account"."""
        with self._chat_lock:
            hit = False
            for e in self._chat:
                if e.get("sender") not in pfids:
                    continue
                hit = True
                e["sender"] = ""
                try:
                    data = json.loads(e["message"])
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    for k in ("msg", "playerName", "playerId", "playFabId"):
                        if k in data:
                            data[k] = "" if k != "playerName" else self.DELETED_NAME
                    e["message"] = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
                else:
                    e["message"] = ""
            if hit:
                with open(self._chat_file + ".tmp", "w", encoding="utf-8") as f:
                    for e in self._chat:
                        f.write(json.dumps(e, ensure_ascii=False) + "\n")
                os.replace(self._chat_file + ".tmp", self._chat_file)

    # ------------------------------------------------------------------ bug reports (testers)
    # Plan: claude/tester-bug-reports.md (project docs). The game sends a report over its own session
    # (Revival/BugSubmit): a title and a few fixed choices typed by the tester, an automatic context
    # block, the mod's recent log lines, and optionally a screenshot (BMP) and, after a crash, Unity's
    # crash dump. Reports live in data/bugs/<id>/ with a summary list in data/bugs/index.json and are
    # read and triaged on the creator site's Bugs page (Revival/BugList, BugGet, BugFile, BugUpdate).
    # Who may send: revival-settings.json "bugReports": "testers" (default) | "everyone" | "off".
    # Logs and screenshots are seen by the report's author and admins only, crash dumps by admins only;
    # other testers see the title, the text and the context summary.
    BUG_CATEGORIES = ("crash", "picture", "card", "rules", "ui", "connection", "replay", "text", "other")
    BUG_SEVERITIES = ("blocker", "match", "annoying", "cosmetic")
    BUG_KINDS = ("manual", "crash", "freeze")
    BUG_STATUSES = ("new", "confirmed", "progress", "fixed", "verified", "cantrepro", "wontfix", "duplicate")
    BUG_CONTEXT_KEYS = {"mod": 40, "platform": 20, "game": 40, "os": 120, "device": 120, "gpu": 120, "api": 60,
                        "screen": 20, "loadingVideo": 20, "scene": 80, "screenName": 80, "mode": 64, "match": 80,
                        "rules": 2000, "cards": 80, "frozenSeconds": 10, "closedAt": 40, "runStarted": 40}
    BUG_MAX_LOG = 256 * 1024
    BUG_MAX_SCREEN = 3 * 1024 * 1024
    BUG_MAX_DUMP = 4 * 1024 * 1024
    # All of data/bugs/ together (review #24): new reports are refused past this, so testers cannot fill
    # the disk. revival-settings.json "bugStorageMB" changes it (default 2048).
    BUG_STORAGE_MB = 2048
    BUG_SECRET_PATTERNS = [
        re.compile(r"wfr-login-[0-9a-f]{8,}", re.I),                    # game logins
        re.compile(r"\b[0-9A-F]{16}-[0-9a-f]{64}\b"),                   # game session tickets
        re.compile(r"\b[0-9a-f]{64}\b", re.I),                          # site sessions and other 32-byte secrets
        re.compile(r"\b[A-Z0-9]{4}(?:-[A-Z0-9]{4}){4}\b"),              # recovery codes
        re.compile(r"(X-Authorization|X-EntityToken|SessionTicket|passphrase)\W{0,4}\S+", re.I),
    ]

    def _bugs_storage_full(self, incoming):
        """True when storing `incoming` more bytes would take data/bugs/ past its cap."""
        try:
            cap = int(self._settings().get("bugStorageMB", self.BUG_STORAGE_MB)) * 1024 * 1024
        except (TypeError, ValueError):
            cap = self.BUG_STORAGE_MB * 1024 * 1024
        used = 0
        for root, _dirs, files in os.walk(self._bugs_dir()):
            for name in files:
                try:
                    used += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return used + incoming > cap

    def _bugs_dir(self):
        d = os.path.join(self.store.root, "bugs")
        os.makedirs(d, exist_ok=True)
        return d

    def _bugs_index(self):
        try:
            with open(os.path.join(self._bugs_dir(), "index.json"), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _bugs_index_save(self, index):
        path = os.path.join(self._bugs_dir(), "index.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    def _bug_path(self, bug_id):
        if not isinstance(bug_id, str) or not re.fullmatch(r"B-\d{4,6}", bug_id):
            return None
        return os.path.join(self._bugs_dir(), bug_id)

    def _bug_load(self, bug_id):
        d = self._bug_path(bug_id)
        if not d:
            return None
        try:
            with open(os.path.join(d, "report.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _bug_save(self, report):
        d = self._bug_path(report["id"])
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "report.json")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)
        index = [e for e in self._bugs_index() if e.get("id") != report["id"]]
        index.append(self._bug_summary(report))
        index.sort(key=lambda e: e["id"])
        self._bugs_index_save(index)

    @staticmethod
    def _bug_summary(r):
        ctx = r.get("context") or {}
        return {"id": r["id"], "title": r["title"], "status": r["status"], "category": r["category"], "severity": r["severity"],
                "kind": r.get("kind", "manual"), "platform": ctx.get("platform", ""), "mod": r.get("signedInMod") or ctx.get("mod", ""),
                "author": r.get("author", ""), "created": r["created"], "updated": r["updated"], "fixedIn": r.get("fixedIn", ""),
                "duplicateOf": r.get("duplicateOf", "")}

    def _bug_scrub(self, text, headers):
        """The log with secrets removed, and the exact addresses this server knows (no IP pattern:
        version numbers look like addresses)."""
        for p in self.BUG_SECRET_PATTERNS:
            text = p.sub("<removed>", text)
        for addr in {self._address(headers), str(self._settings().get("publicAddress") or "")} - {"", "unknown"}:
            text = text.replace(addr, "<address>")
        return text

    @staticmethod
    def _bug_bytes(value, limit, signatures, what):
        """(bytes or None, error) for an optional base64 attachment."""
        if value in (None, ""):
            return None, None
        if not isinstance(value, str) or len(value) > limit * 4 // 3 + 8:
            return None, err("InvalidParams", f"the {what} is too large")
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            return None, err("InvalidParams", f"the {what} could not be read")
        if len(raw) > limit or not any(raw.startswith(s) for s in signatures):
            return None, err("InvalidParams", f"the {what} is not in a format the server accepts")
        return raw, None

    def _bug_who(self, pfid):
        """(account perms with name, None) for a player allowed to send reports, or (None, error)."""
        mode = str(self._settings().get("bugReports") or "testers").lower()
        if mode == "off":
            return None, err("NotAuthorized", "bug reports are switched off on this server", 1089, 403)
        perms = self.creator_perms_for_player(pfid)
        if perms is None or perms["access"] != "approved":
            return None, err("NotAuthorized", "link this game to an approved site account to send bug reports", 1089, 403)
        if mode != "everyone" and not perms["tester"]:
            return None, err("NotAuthorized", "bug reports are for testers", 1089, 403)
        return perms, None

    def api_Revival_BugSubmit(self, req, headers):
        """A report from the game (see the section comment). Refused whole if anything is not as expected."""
        pfid = self.store.session_player_id(headers)
        if not pfid:
            return err("NotAuthenticated", "sign in to send bug reports", 1074, 401)
        allowed = {"title", "category", "severity", "description", "kind", "context", "log", "screenshot", "dump"}
        if not isinstance(req, dict) or not set(req) <= allowed:
            return err("InvalidParams", "that is not a bug report")
        perms, problem = self._bug_who(pfid)
        if problem:
            return problem
        title = " ".join(str(req.get("title") or "").split())
        if not isinstance(req.get("title"), str) or not 3 <= len(title) <= 120:
            return err("InvalidParams", "the title must be 3 to 120 characters")
        category, severity, kind = req.get("category"), req.get("severity"), req.get("kind") or "manual"
        if category not in self.BUG_CATEGORIES or severity not in self.BUG_SEVERITIES or kind not in self.BUG_KINDS:
            return err("InvalidParams", "unknown category, severity or kind")
        description = req.get("description") or ""
        if not isinstance(description, str) or len(description.encode("utf-8")) > 4096:
            return err("InvalidParams", "the description is limited to 4 KB")
        ctx = req.get("context") or {}
        if not isinstance(ctx, dict) or not set(ctx) <= set(self.BUG_CONTEXT_KEYS):
            return err("InvalidParams", "the report's context is not as expected")
        for k, v in ctx.items():
            if not isinstance(v, str) or len(v) > self.BUG_CONTEXT_KEYS[k]:
                return err("InvalidParams", "the report's context is not as expected")
        log_text = req.get("log") or ""
        if not isinstance(log_text, str) or len(log_text.encode("utf-8")) > self.BUG_MAX_LOG:
            return err("InvalidParams", "the log is limited to 256 KB")
        screen, problem = self._bug_bytes(req.get("screenshot"), self.BUG_MAX_SCREEN, (b"BM", b"\xff\xd8\xff", b"\x89PNG"), "screenshot")
        if problem:
            return problem
        dump, problem = self._bug_bytes(req.get("dump"), self.BUG_MAX_DUMP, (b"MDMP",), "crash dump")
        if problem:
            return problem
        if not self._allow("bug-hour", pfid, 10, 3600, count=False) or not self._allow("bug-day", pfid, 30, 86400, count=False):
            return err("TooManyAttempts", "too many bug reports - try again later", 1199, 429)
        incoming = len(log_text.encode("utf-8")) + len(screen or b"") + len(dump or b"") + len(description.encode("utf-8"))
        if self._bugs_storage_full(incoming):
            log.warning("bug report from %s refused: data/bugs is at its storage cap (bugStorageMB)", perms["name"])
            return err("StorageFull", "the server's bug report storage is full - please tell us at wf-revival@silentanarchist.com", 1199, 507)
        self._allow("bug-hour", pfid, 10, 3600)
        self._allow("bug-day", pfid, 30, 86400)
        now = now_iso()
        with self.store.lock:
            index = self._bugs_index()
            n = max([int(e["id"][2:]) for e in index if re.fullmatch(r"B-\d+", str(e.get("id")))] + [0]) + 1
            bug_id = "B-%04d" % n
            d = self._bug_path(bug_id)
            os.makedirs(d, exist_ok=True)
            report = {"id": bug_id, "title": title, "category": category, "severity": severity, "kind": kind,
                      "description": self._bug_scrub(description, headers), "status": "new", "author": perms["name"],
                      "player": pfid, "signedInMod": str(headers.get("X-Revival-Mod") or "")[:40],
                      "context": ctx, "created": now, "updated": now, "files": {}, "history": [{"at": now, "by": perms["name"], "status": "new"}]}
            if log_text:
                with open(os.path.join(d, "log.txt"), "w", encoding="utf-8") as f:
                    f.write(self._bug_scrub(log_text, headers))
                report["files"]["log"] = len(log_text)
            if screen:
                ext = "bmp" if screen.startswith(b"BM") else "jpg" if screen.startswith(b"\xff\xd8") else "png"
                with open(os.path.join(d, "screen." + ext), "wb") as f:
                    f.write(screen)
                report["files"]["screen"] = ext
            if dump:
                with open(os.path.join(d, "crash.dmp"), "wb") as f:
                    f.write(dump)
                report["files"]["dump"] = len(dump)
            self._bug_save(report)
        log.info("bug report %s from %s (%s, %s): %s", bug_id, perms["name"], kind, ctx.get("platform", "?"), title)
        site = str(self._settings().get("creatorUrl") or "").rstrip("/")
        base = site.split("/creator")[0] if site else ""
        return ok({"id": bug_id, "url": (base + "/bugs?id=" + bug_id) if base else ""})

    def api_Revival_Guides(self, req, headers):
        """The install guides for the site's Guides page: any signed-in account that has been let in
        (not one still waiting for approval, not a banned one). Text from content/files/guides.html."""
        me = self._creator_account(req)
        if not me or self._creator_perms(me)["access"] != "approved":
            return err("NotAuthorized", "sign in first")
        try:
            with open(os.path.join(self.content_root, "files", "guides.html"), encoding="utf-8") as f:
                page = f.read()
        except OSError:
            return err("NotFound", "the guides are not on the server")
        m = re.search(r"<main>(.*)</main>", page, re.S)
        return ok({"html": m.group(1) if m else ""})

    def _bug_reader(self, req):
        """(account, perms) for a signed-in tester or admin on the site, or (None, error)."""
        me = self._creator_account(req)
        if not me:
            return None, err("NotAuthorized", "sign in first")
        perms = self._creator_perms(me)
        # testers and admins ("tester" is on for every admin); reviewers are for the card creator
        if perms["access"] != "approved" or not (perms["tester"] or perms["admin"]):
            return None, err("NotAuthorized", "the bug list is for testers")
        return me, perms

    def api_Revival_BugList(self, req, headers):
        me, perms = self._bug_reader(req)
        if not me:
            return perms
        with self.store.lock:
            index = self._bugs_index()
        index.sort(key=lambda e: e.get("updated", ""), reverse=True)
        return ok({"bugs": index, "admin": perms["admin"], "me": me["name"]})

    def api_Revival_BugGet(self, req, headers):
        me, perms = self._bug_reader(req)
        if not me:
            return perms
        with self.store.lock:
            r = self._bug_load(str(req.get("id") or ""))
        if not r:
            return err("NotFound", "no bug report with that number")
        mine = r.get("author", "").lower() == me["name"].lower()
        out = {k: v for k, v in r.items() if k not in ("player",)}
        files = dict(r.get("files") or {})
        if not (mine or perms["admin"]):
            files.pop("log", None)
            files.pop("screen", None)
        if not perms["admin"]:
            files.pop("dump", None)
        out["files"] = files
        out["mine"], out["admin"] = mine, perms["admin"]
        return ok(out)

    def api_Revival_BugFile(self, req, headers):
        """One attachment: {"id", "file": "log" | "screen" | "dump"} (author and admins; dumps admins only)."""
        me, perms = self._bug_reader(req)
        if not me:
            return perms
        which = req.get("file")
        with self.store.lock:
            r = self._bug_load(str(req.get("id") or ""))
        if not r or which not in ("log", "screen", "dump") or which not in (r.get("files") or {}):
            return err("NotFound", "that report has no such attachment")
        mine = r.get("author", "").lower() == me["name"].lower()
        if not perms["admin"] and (which == "dump" or not mine):
            return err("NotAuthorized", "only the reporter and admins can see this")
        d = self._bug_path(r["id"])
        if which == "log":
            with open(os.path.join(d, "log.txt"), encoding="utf-8") as f:
                return ok({"log": f.read()})
        name = "crash.dmp" if which == "dump" else "screen." + r["files"]["screen"]
        with open(os.path.join(d, name), "rb") as f:
            raw = f.read()
        kind = "application/octet-stream" if which == "dump" else {"bmp": "image/bmp", "jpg": "image/jpeg", "png": "image/png"}[r["files"]["screen"]]
        return ok({"name": "%s-%s" % (r["id"], name), "type": kind, "data": base64.b64encode(raw).decode("ascii")})

    def api_Revival_BugUpdate(self, req, headers):
        """Admins: status, severity, category, fixedIn, duplicateOf, note. The author: title and
        description while the report is still new."""
        me, perms = self._bug_reader(req)
        if not me:
            return perms
        with self.store.lock:
            r = self._bug_load(str(req.get("id") or ""))
            if not r:
                return err("NotFound", "no bug report with that number")
            mine = r.get("author", "").lower() == me["name"].lower()
            changed = []
            if perms["admin"]:
                if "status" in req:
                    if req["status"] not in self.BUG_STATUSES:
                        return err("InvalidParams", "unknown status")
                    r["status"] = req["status"]
                    changed.append("status")
                if "severity" in req:
                    if req["severity"] not in self.BUG_SEVERITIES:
                        return err("InvalidParams", "unknown severity")
                    r["severity"] = req["severity"]
                    changed.append("severity")
                if "category" in req:
                    if req["category"] not in self.BUG_CATEGORIES:
                        return err("InvalidParams", "unknown category")
                    r["category"] = req["category"]
                    changed.append("category")
                if "fixedIn" in req:
                    v = str(req["fixedIn"] or "").strip()
                    if v and not re.fullmatch(r"\d+(\.\d+){1,3}(-[aw])?", v):
                        return err("InvalidParams", "fixed in: a mod version like 0.13.3")
                    r["fixedIn"] = v
                    changed.append("fixedIn")
                if "duplicateOf" in req:
                    v = str(req["duplicateOf"] or "").strip().upper()
                    if v and (not self._bug_load(v) or v == r["id"]):
                        return err("InvalidParams", "duplicate of: the number of another report")
                    r["duplicateOf"] = v
                    changed.append("duplicateOf")
                if "note" in req:
                    r["adminNote"] = str(req["note"] or "")[:2000]
                    changed.append("note")
            if (mine or perms["admin"]) and r["status"] == "new":
                if "title" in req:
                    t = " ".join(str(req["title"] or "").split())
                    if not 3 <= len(t) <= 120:
                        return err("InvalidParams", "the title must be 3 to 120 characters")
                    r["title"] = t
                    changed.append("title")
                if "description" in req:
                    t = str(req["description"] or "")
                    if len(t.encode("utf-8")) > 4096:
                        return err("InvalidParams", "the description is limited to 4 KB")
                    r["description"] = self._bug_scrub(t, headers)
                    changed.append("description")
            if not changed:
                return err("NotAuthorized", "nothing you can change was sent")
            r["updated"] = now_iso()
            r.setdefault("history", []).append({"at": r["updated"], "by": me["name"], "changed": changed,
                                                "status": r["status"]})
            r["history"] = r["history"][-100:]
            self._bug_save(r)
        return ok({"id": r["id"], "status": r["status"]})

    # Attachments of closed reports (log, screenshot, crash dump) are removed 30 days after the report
    # was closed; the report itself stays. Owner's decision 2026-10-11 (security review #24), in the Terms.
    BUG_CLOSED = ("verified", "cantrepro", "wontfix", "duplicate")
    BUG_ATTACHMENT_DAYS = 30

    def _bug_closed_since(self, r):
        """When the report entered its current closed state (ISO time), or None while it is open."""
        if r.get("status") not in self.BUG_CLOSED:
            return None
        since = None
        for h in r.get("history") or []:
            st = h.get("status")
            if st in self.BUG_CLOSED:
                since = since or h.get("at")
            elif st:
                since = None
        return since or r.get("updated")

    def _bugs_prune_closed(self):
        """Removes the attachments of reports closed for 30 days or more (the caller holds the store lock)."""
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - self.BUG_ATTACHMENT_DAYS * 86400))
        d = self._bugs_dir()
        for e in self._bugs_index():
            r = self._bug_load(str(e.get("id") or ""))
            if not r or not r.get("files"):
                continue
            since = self._bug_closed_since(r)
            if not since or str(since)[:19] > cutoff:
                continue
            for name in os.listdir(os.path.join(d, r["id"])):
                if name != "report.json":
                    try:
                        os.remove(os.path.join(d, r["id"], name))
                    except OSError:
                        pass
            r["files"], r["attachmentsRemoved"] = {}, now_iso()
            self._bug_save(r)
            log.info("bug report %s: attachments removed (closed since %s)", r["id"], since)

    def _bugs_forget_author(self, names):
        """Account deletion: the author's attachments go, the reports stay as from "deleted account"."""
        d = self._bugs_dir()
        for e in self._bugs_index():
            if str(e.get("author") or "").lower() not in names:
                continue
            r = self._bug_load(e["id"])
            if not r:
                continue
            for name in os.listdir(os.path.join(d, r["id"])):
                if name != "report.json":
                    try:
                        os.remove(os.path.join(d, r["id"], name))
                    except OSError:
                        pass
            r["files"], r["author"], r["player"] = {}, self.DELETED_NAME, ""
            for h in r.get("history") or []:
                if str(h.get("by") or "").lower() in names:
                    h["by"] = self.DELETED_NAME
            self._bug_save(r)

    # Permissions belong to accounts and only admins can change them:
    #   access    "requested" (asked to join), "approved" or "banned". New accounts are approved
    #             straight away unless revival-settings.json has {"accounts": {"autoApprove": false}}.
    #   reviewer  approves and rejects cards waiting for review.
    #   tester    in-game tester (will see custom cards in the game before they are released).
    #   admin     everything: reviewer and tester rights, changing or un-approving approved cards,
    #             and editing the accounts (access, reviewer, tester) of players who are not admins.
    #   owner     admin rights, plus giving and taking admin and editing admins' accounts.
    #   founding owner  the accounts listed in data/creator_admins.json: always owners, cannot be
    #             changed from the page by anyone (so the server can never be left without one), the
    #             only ones who give or take owner or edit an owner's account, and the only ones who
    #             see the Server tab.
    # Rule of thumb: an account can only be edited (or its game player banned) by someone ranked above it.
    # A ban - of the account, or of its game player - takes every role away for good.
    ACCESS = ("requested", "approved", "banned")

    def _file_cached(self, slot, path, read):
        """Efficiency E4: read(path) kept until the file's modified time or size changes (None when missing)."""
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        cache = self.__dict__.setdefault("_files_cache", {})
        hit = cache.get(slot)
        if hit is not None and hit[0] == stamp and hit[1] == path:
            return hit[2]
        value = read(path) if stamp is not None else None
        cache[slot] = (stamp, path, value)
        return value

    def _creator_founders(self):
        def read(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return frozenset(str(n).lower() for n in json.load(f))
            except (OSError, ValueError, TypeError):
                return frozenset()
        return self._file_cached("founders", os.path.join(self.store.root, "creator_admins.json"), read) or frozenset()

    def _creator_auto_approve(self):
        try:
            return (self._settings().get("accounts") or {}).get("autoApprove", True) is not False
        except AttributeError:
            return True

    def _creator_perms(self, user):
        """{"access", "reviewer", "tester", "admin", "fixed"} for an account."""
        fixed = user["name"].lower() in self._creator_founders()
        owner = fixed or bool(user.get("owner"))
        admin = owner or bool(user.get("admin")) or user.get("role") == "admin"        # "role" is the older form
        access = user.get("access")
        if access not in self.ACCESS:
            access = "approved"                                 # accounts made before access existed
        if fixed:
            access = "approved"
        if access == "banned":                                  # a banned account has no roles, whatever the file says
            return {"access": access, "admin": False, "fixed": False, "owner": False, "player": user.get("player") or "",
                    "reviewer": False, "tester": False}
        return {"access": access, "admin": admin, "fixed": fixed, "owner": owner, "player": user.get("player") or "",
                "reviewer": admin or bool(user.get("reviewer")) or user.get("role") == "reviewer",
                "tester": admin or bool(user.get("tester"))}

    ROLE_KEYS = ("owner", "admin", "reviewer", "tester", "role")

    def _creator_rank(self, perms):
        """3 founding owner, 2 owner, 1 admin, 0 anyone else."""
        return 3 if perms["fixed"] else 2 if perms["owner"] else 1 if perms["admin"] else 0

    def _creator_strip_roles(self, user):
        """A ban takes every role away for good: unbanning later does not bring them back."""
        for k in self.ROLE_KEYS:
            user.pop(k, None)

    def _creator_account(self, req):
        """The signed-in account, or None."""
        user = self._creator_session_owner(req)
        # an account set back to "requested" by an admin is signed out of the site meanwhile
        return None if user is None or self._creator_perms(user)["access"] == "requested" else user

    def _creator_role(self, req):
        """2 admin, 1 reviewer, 0 anyone else or not signed in."""
        user = self._creator_account(req)
        if not user:
            return 0
        perms = self._creator_perms(user)
        if perms["access"] == "banned":
            return 0
        return 2 if perms["admin"] else 1 if perms["reviewer"] else 0

    def _creator_load(self):
        cards = []
        d = self._creator_dir()
        for name in sorted(os.listdir(d)):
            if re.fullmatch(r"CUSTOM-\d{4}\.json", name):
                try:
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        cards.append(json.load(f))
                except (OSError, ValueError):
                    log.warning("custom card %s is unreadable", name)
        return cards

    def _creator_compile(self, card):
        """The design in the game's card layout, plus notes on what could not be written (None when unavailable)."""
        try:
            path = os.path.join(self.content_root, "creator-native.json")
            stamp = os.path.getmtime(path)
            if getattr(self, "_native_stamp", None) != stamp:
                with open(path, encoding="utf-8") as f:
                    self._native = json.load(f)
                self._native_stamp = stamp
            native, notes = CardCompiler(self._native).compile(card)
            return native, notes + self._creator_faction_notes(card)
        except Exception as e:
            log.exception("could not write %s in the game layout", card.get("id"))
            return None, [f"could not be written in the game's layout: {type(e).__name__}: {e}"]

    def _creator_faction_notes(self, card):
        """Mechanics on the card that belong to another faction (from the creator's menu file)."""
        try:
            with open(os.path.join(self.content_root, "files", "creator-schema.json"), encoding="utf-8") as f:
                schema = json.load(f)
        except (OSError, ValueError):
            return []
        home = {kind: {e["id"]: e["faction"] for e in schema.get(kind, []) if e.get("faction")}
                for kind in ("traits", "triggers", "effects")}
        used = [("traits", t.get("name")) for t in card.get("traits") or []]
        groups = list(card.get("abilities") or []) + ([card["action"]] if card.get("action") else [])
        for g in groups:
            if g.get("trigger"):
                used.append(("triggers", g["trigger"]))
            for fx in g.get("effects") or []:
                used.append(("effects", fx.get("effect")))
                if (fx.get("buff") or {}).get("trait"):
                    used.append(("traits", fx["buff"]["trait"]))
        army = card.get("army") or ""
        out = []
        for kind, name in used:
            faction = home[kind].get(name)
            if faction and faction != army:
                note = f"{name} is a {faction} mechanic on a {army or 'Neutral'} card - it may not work outside its faction"
                if note not in out:
                    out.append(note)
        return out

    def _creator_rules_version(self):
        """Short fingerprint of the rules text, recorded when an author confirms the rules ("" if there are none)."""
        try:
            with open(os.path.join(self.content_root, "files", "creator-rules.txt"), "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:10]
        except OSError:
            return ""

    def _creator_released(self):
        try:
            with open(os.path.join(self.content_root, "files", "revival-settings.json"), encoding="utf-8") as f:
                return bool((json.load(f).get("customCards") or {}).get("released"))
        except (OSError, ValueError, AttributeError):
            return False

    @staticmethod
    def _creator_int(v, lo, hi, what):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"{what} must be a whole number from {lo} to {hi}")
        return v

    def _creator_clean(self, card):
        """Checks a submitted design and returns only the fields the creator defines."""
        if not isinstance(card, dict):
            raise ValueError("card is missing")
        if len(json.dumps(card)) > 30000:
            raise ValueError("the card is too large")
        name = str(card.get("name") or "").strip()
        if not 1 <= len(name) <= 40:
            raise ValueError("the name must be 1 to 40 characters")
        kind = card.get("kind")
        if kind not in self.CREATOR_KINDS:
            raise ValueError("unknown card kind")
        rarity = card.get("rarity")
        if rarity not in ("Common", "Rare", "Epic", "Legendary", "Special", "None"):      # Special/None: tokens and created cards
            raise ValueError("unknown rarity")
        out = {"name": name, "kind": kind, "rarity": rarity, "army": str(card.get("army") or "")[:40],
               # -1: official token stratagems ("no cost"); 14: the most an official card costs
               "cost": self._creator_int(card.get("cost", 0), -1, 14, "cost"),
               "text": str(card.get("text") or "")[:300], "art": str(card.get("art") or "")[:40],
               "lore": str(card.get("lore") or "").strip()[:300],      # flavour text (story, not rules)
               "designNotes": str(card.get("designNotes") or "")[:1000],
               # a card that only exists when another card creates it: not in the collection, not for decks
               # (warlords too: 10 official ones are only created by other cards)
               "token": card.get("token") is True}
        if card.get("enchantment") is True and kind != "hero":
            # a hidden card other cards attach with an enchantment buff (always a token)
            out["enchantment"] = True
            out["token"] = True
        if kind != "tactic" or any(k in card for k in ("melee", "ranged", "health", "armour", "keyword")):
            # units always; a stratagem only when it carries unit stats (as 5 official ones)
            out["melee"] = self._creator_int(card.get("melee", 0), 0, 30, "melee")
            out["ranged"] = self._creator_int(card.get("ranged", 0), 0, 30, "ranged")
            out["health"] = self._creator_int(card.get("health", 1), 0 if kind == "tactic" else 1, 60, "health")
            out["armour"] = self._creator_int(card.get("armour", 0), 0, 30, "armour")
            out["keyword"] = str(card.get("keyword") or "")[:30]
        if kind == "tactic":
            out["stratagemType"] = str(card.get("stratagemType") or "Normal")[:30]
        for field, limit in (("traits", 8), ("abilities", 6)):
            value = card.get(field) or []
            if not isinstance(value, list) or len(value) > limit or not all(isinstance(x, dict) for x in value):
                raise ValueError(f"{field} must be a list of at most {limit} entries")
            out[field] = value
        for ab in out["abilities"]:
            effects = ab.get("effects") or []
            if not isinstance(effects, list) or len(effects) > 8:      # 8: Tyrannofex
                raise ValueError("an ability can have at most 8 effects")
        for key in ("action", "alternative"):
            action = card.get(key)
            if not isinstance(action, dict):
                continue
            # action: an ability the player activates by hand (Duty, Ferocity); alternative: a second action used
            # instead while a condition is not met (Wulfen Pack Leader)
            effects = action.get("effects") or []
            if not isinstance(effects, list) or len(effects) > 8 or not all(isinstance(x, dict) for x in effects):
                raise ValueError(f"the {key} must have at most 8 effects")
            out[key] = {"cost": self._creator_int(action.get("cost", 0), 0, 12, key + " cost"), "effects": effects}
            if key == "action":
                out[key].update(keyword=str(action.get("keyword") or "")[:30], note=str(action.get("note") or "")[:200])
            if isinstance(action.get("choose"), dict) or action.get("choose") is False:
                out[key]["choose"] = action["choose"]
            if action.get("playAnim") is True:
                out[key]["playAnim"] = True
            if key == "alternative" and isinstance(action.get("when"), dict):
                out[key]["when"] = action["when"]
        # the card's own settings beyond its abilities (all optional; the compiler checks the names)
        for k in ("choices", "targetsCounter"):
            if isinstance(card.get(k), dict):
                out[k] = card[k]
        for k in ("inventory", "attackDamageType", "startingCard", "goSecondCard"):
            if card.get(k):
                out[k] = str(card[k])[:40]
        for k in ("randomCreation", "blockActions"):
            if isinstance(card.get(k), bool):
                out[k] = card[k]
        related = card.get("related")
        if isinstance(related, list):
            out["related"] = [str(x)[:40] for x in related[:4] if isinstance(x, str) and x]
        hints = card.get("showTraits")
        if isinstance(hints, list):
            out["showTraits"] = [h[:40] if isinstance(h, str) else {"name": str(h.get("name") or "")[:40], "value": self._creator_int(h.get("value", 0), 0, 99, "hint value")}
                                 for h in hints[:8] if isinstance(h, str) or isinstance(h, dict)]
        if card.get("needsTarget") is not None:
            out["needsTarget"] = card.get("needsTarget")
        # what the player picks when playing the card (a filter), or False for "nothing is picked";
        # left out by designs that describe the pick on the effect itself
        if isinstance(card.get("choose"), dict) or card.get("choose") is False:
            out["choose"] = card["choose"]
        return out

    def api_Revival_CreatorList(self, req, headers):
        with self.store.lock:
            cards = self._creator_load()
            me = self._creator_user(req)
        role = self._creator_role(req)
        mine = lambda c: bool(me) and c.get("author", "").lower() == me.lower()
        # Authors see only their own cards; reviewers and admins also see everything that was
        # submitted. Drafts stay private to their author either way.
        cards = [c for c in cards if mine(c) or (role and c.get("status") != "draft")]
        account = self._creator_account(req)
        return ok({"cards": cards, "role": role, "released": self._creator_released(), "name": me,
                   "account": dict(self._creator_perms(account), gameLoginAt=max([account.get("gameLoginAt") or ""] + [g.get("at") or "" for g in account.get("gameLogins") or [] if isinstance(g, dict)]),
                                   gameLogins=len(self._game_login_hashes(account)),
                                   recoveryAt=account.get("recoveryAt") or "" if account.get("recoveryHash") else "",
                                   playerName=((self.store.load(account["player"]) or {}).get("DisplayName") or account["player"])
                                   if account.get("player") else "") if account and me else None,
                   "rulesVersion": self._creator_rules_version()})

    def api_Revival_CreatorSave(self, req, headers):
        """Saves a design. "submit" sends it to the reviewer; without it the design stays a private draft."""
        try:
            card = self._creator_clean(req.get("card"))
        except ValueError as e:
            return err("InvalidParams", str(e))
        role = self._creator_role(req)
        submit = bool(req.get("submit"))
        rules = self._creator_rules_version()
        if submit and rules and not req.get("acceptRules"):
            return err("InvalidParams", "confirm that the card follows the rules before submitting")
        card_id = str((req.get("card") or {}).get("id") or "")
        with self.store.lock:
            author = self._creator_user(req)
            if not author and not role:
                return err("NotAuthorized", "sign in before saving")
            d = self._creator_dir()
            existing = None
            if card_id:
                if not re.fullmatch(r"CUSTOM-\d{4}", card_id) or not os.path.isfile(os.path.join(d, card_id + ".json")):
                    return err("InvalidParams", "that card no longer exists")
                with open(os.path.join(d, card_id + ".json"), encoding="utf-8") as f:
                    existing = json.load(f)
                mine = bool(author) and existing.get("author", "").lower() == author.lower()
                locked = existing.get("status")
                if locked == "approved" and role < 2:
                    return err("NotAuthorized", "approved cards can only be changed by an admin")
                if not role and not mine:
                    return err("NotAuthorized", "only the card's author or a reviewer can change it")
            else:
                if not author:
                    return err("NotAuthorized", "sign in as an author to create a card")
                used = [int(n[7:11]) for n in os.listdir(d) if re.fullmatch(r"CUSTOM-\d{4}\.json", n)]
                card_id = "CUSTOM-%04d" % (max(used, default=0) + 1)
            was = (existing or {}).get("status")
            if existing and role and (was == "approved" or not mine):
                status = was                      # staff corrected a card: it stays where it is in the review
            else:
                status = "pending" if submit else "draft"
            card.update(id=card_id, author=(existing or {}).get("author") or author,
                        created=(existing or {}).get("created") or now_iso(), updated=now_iso(), status=status,
                        # the reviewer's note stays visible while the author reworks a rejected card
                        reviewNote=(existing or {}).get("reviewNote", ""),
                        returned=bool((existing or {}).get("returned") or was == "rejected") and status == "draft")
            if (existing or {}).get("artUpload") and not card.get("art") and os.path.isfile(self._creator_art_path(card_id)):
                card["artUpload"] = existing["artUpload"]      # the uploaded artwork stays unless art is borrowed instead
                if existing.get("artMask") and card.get("kind") != "tactic":
                    card["artMask"] = existing["artMask"]
                if existing.get("artCredit"):
                    card["artCredit"] = existing["artCredit"]
            card["native"], card["buildNotes"] = self._creator_compile(card)
            if (existing or {}).get("rulesAccepted"):
                card["rulesAccepted"] = existing["rulesAccepted"]
            if status == "pending":
                card["submitted"] = now_iso()
                if submit and req.get("acceptRules"):      # who confirmed which version of the rules, and when
                    card["rulesAccepted"] = {"by": author, "at": now_iso(), "version": rules}
            tmp = os.path.join(d, card_id + ".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=1)
            os.replace(tmp, os.path.join(d, card_id + ".json"))
        return ok({"card": card})

    # Uploaded artwork. The game's card art is a 1024x1024 texture (measured from its art files).
    # Stratagems fill the whole square. Troops, vehicles and warlords are a full painting in the
    # middle 670 pixels across, top to bottom; the texture's alpha is not transparency but a mask
    # of the parts drawn over the card frame ("pop-out"). The creator page prepares the picture to
    # that size, and the mask is an optional second picture (white = pops out), kept next to it.
    # The server only accepts PNGs of exactly that size.
    ART_SIZE = 1024
    ART_MAX_BYTES = 4 * 1024 * 1024

    def _creator_art_path(self, card_id):
        d = os.path.join(self._creator_dir(), "art")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, card_id + ".png")

    def _creator_png(self, image, what):
        """(bytes, error) for a 1024 x 1024 PNG sent as a data URL."""
        if not str(image).startswith("data:image/png;base64,"):
            return None, err("InvalidParams", f"the {what} must be a PNG image")
        try:
            raw = base64.b64decode(image.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error):
            return None, err("InvalidParams", f"the {what} could not be read")
        if len(raw) > self.ART_MAX_BYTES:
            return None, err("InvalidParams", f"the {what} is too large (4 MB at most)")
        if raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
            return None, err("InvalidParams", f"the {what} is not a valid PNG image")
        width, height = int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
        if (width, height) != (self.ART_SIZE, self.ART_SIZE):
            return None, err("InvalidParams", f"the {what} must be {self.ART_SIZE} x {self.ART_SIZE} pixels, not {width} x {height}")
        return raw, None

    def _creator_card_for(self, req, change, admin_any=False):
        """(card, path, error) for an art request, checking who may see or change the card. Call with the lock held.
        admin_any: admins may also reach drafts (they answer for every picture stored on the server)."""
        card_id = str(req.get("id") or "")
        path = os.path.join(self._creator_dir(), card_id + ".json")
        if not re.fullmatch(r"CUSTOM-\d{4}", card_id) or not os.path.isfile(path):
            return None, None, err("InvalidParams", "save the card first")
        with open(path, encoding="utf-8") as f:
            card = json.load(f)
        role, author = self._creator_role(req), self._creator_user(req)
        mine = bool(author) and card.get("author", "").lower() == author.lower()
        if not mine and not (role and card.get("status") != "draft") and not (admin_any and role == 2):
            return None, None, err("NotAuthorized", "that is not your card")
        if change and card.get("status") == "approved" and role < 2:
            return None, None, err("NotAuthorized", "approved cards can only be changed by an admin")
        return card, path, None

    def _creator_store(self, card, path):
        card["updated"] = now_iso()
        card["native"], card["buildNotes"] = self._creator_compile(card)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(card, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    def api_Revival_CreatorArtSave(self, req, headers):
        raw, problem = self._creator_png(str(req.get("image") or ""), "artwork")
        if problem:
            return problem
        with self.store.lock:
            card, path, problem = self._creator_card_for(req, change=True)
            if problem:
                return problem
            art = self._creator_art_path(card["id"])
            with open(art + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(art + ".tmp", art)
            card.update(artUpload=now_iso(), art="")
            self._creator_store(card, path)
        return ok({"card": card})

    def api_Revival_CreatorArtMask(self, req, headers):
        """Sets ("image") or removes ("remove") the pop-out mask of a card's uploaded artwork."""
        raw = None
        if not req.get("remove"):
            raw, problem = self._creator_png(str(req.get("image") or ""), "mask")
            if problem:
                return problem
        with self.store.lock:
            card, path, problem = self._creator_card_for(req, change=True)
            if problem:
                return problem
            if not card.get("artUpload"):
                return err("InvalidParams", "upload the artwork first")
            if card.get("kind") == "tactic":
                return err("InvalidParams", "stratagem art has no pop-out mask")
            mask = self._creator_art_path(card["id"])[:-4] + ".mask.png"
            if raw is None:
                if os.path.isfile(mask):
                    os.replace(mask, mask[:-4] + ".removed.png")      # kept on disk, no longer used
                card.pop("artMask", None)
            else:
                with open(mask + ".tmp", "wb") as f:
                    f.write(raw)
                os.replace(mask + ".tmp", mask)
                card["artMask"] = now_iso()
            self._creator_store(card, path)
        return ok({"card": card})

    # Who made an uploaded picture. Kept in a file next to the picture (art/<id>.credit.json, so it stays with
    # the art file when the picture is handed on or packed) and copied onto the card for display.
    ART_TAILS = (".png", ".mask.png", ".removed.png", ".mask.removed.png", ".credit.json", ".credit.removed.json")
    CREDIT_ARTIST_MAX = 80
    CREDIT_LINK_MAX = 300

    def _creator_credit_path(self, card_id):
        return self._creator_art_path(card_id)[:-4] + ".credit.json"

    def api_Revival_CreatorArtCredit(self, req, headers):
        """Sets the credit of a card's uploaded artwork: the artist's name and an optional link (http or https
        only, since the page shows it as a link). Both empty removes it."""
        artist = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", "", str(req.get("artist") or ""))).strip()
        link = str(req.get("link") or "").strip()
        if len(artist) > self.CREDIT_ARTIST_MAX:
            return err("InvalidParams", "the artist's name is too long (%d characters at most)" % self.CREDIT_ARTIST_MAX)
        if link and (len(link) > self.CREDIT_LINK_MAX or not re.fullmatch(r"https?://[^\s<>\"'`\\]+", link)):
            return err("InvalidParams", "the link must be a web address starting with http:// or https://")
        if link and not artist:
            return err("InvalidParams", "give the artist's name with the link")
        with self.store.lock:
            card, path, problem = self._creator_card_for(req, change=True)
            if problem:
                return problem
            if not card.get("artUpload"):
                return err("InvalidParams", "upload the artwork first")
            side = self._creator_credit_path(card["id"])
            if not artist:
                card.pop("artCredit", None)
                if os.path.isfile(side):
                    os.remove(side)
            else:
                credit = {"artist": artist, "link": link, "by": self._creator_user(req), "at": now_iso()}
                with open(side + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(dict(credit, card=card["id"], picture=card["artUpload"]), f, ensure_ascii=False, indent=1)
                os.replace(side + ".tmp", side)
                card["artCredit"] = credit
            self._creator_store(card, path)
        return ok({"card": card})

    def api_Revival_CreatorArtGet(self, req, headers):
        with self.store.lock:
            card, path, problem = self._creator_card_for(req, change=False, admin_any=True)
            if problem:
                return problem
            art = self._creator_art_path(card["id"])
            if not card.get("artUpload") or not os.path.isfile(art):
                return err("InvalidParams", "this card has no uploaded artwork")
            with open(art, "rb") as f:
                raw = f.read()
            mask = None
            if card.get("artMask") and os.path.isfile(art[:-4] + ".mask.png"):     # white = drawn over the frame
                with open(art[:-4] + ".mask.png", "rb") as f:
                    mask = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
        return ok({"image": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"), "stamp": card["artUpload"], "mask": mask,
                   "credit": card.get("artCredit")})

    def api_Revival_CreatorArtRemove(self, req, headers):
        with self.store.lock:
            card, path, problem = self._creator_card_for(req, change=True)
            if problem:
                return problem
            art = self._creator_art_path(card["id"])
            if os.path.isfile(art):
                os.replace(art, art[:-4] + ".removed.png")      # kept on disk, no longer used
            mask = art[:-4] + ".mask.png"
            if os.path.isfile(mask):
                os.replace(mask, mask[:-4] + ".removed.png")
            credit = self._creator_credit_path(card["id"])
            if os.path.isfile(credit):
                os.replace(credit, credit[:-5] + ".removed.json")
            card.pop("artUpload", None)
            card.pop("artMask", None)
            card.pop("artCredit", None)
            self._creator_store(card, path)
        return ok({"card": card})

    # Admins answer for every picture kept on the server, drafts included, so they can list all
    # uploads and delete one for good (the author's own "remove" only sets a picture aside).
    def api_Revival_CreatorArtList(self, req, headers):
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see every upload")
        with self.store.lock:
            out = [{"id": c["id"], "name": c.get("name", ""), "author": c.get("author", ""), "status": c.get("status", ""),
                    "kind": c.get("kind", ""), "artUpload": c["artUpload"], "artMask": c.get("artMask") or "",
                    "artCredit": c.get("artCredit")}
                   for c in self._creator_load()
                   if c.get("artUpload") and os.path.isfile(self._creator_art_path(c["id"]))]
        out.sort(key=lambda c: c["artUpload"], reverse=True)
        return ok({"uploads": out})

    def api_Revival_CreatorArtDelete(self, req, headers):
        """An admin deletes a card's uploaded artwork and mask for good, including copies set aside earlier."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can delete an upload")
        reason = str(req.get("reason") or "").strip()[:300]
        with self.store.lock:
            card, path, problem = self._creator_card_for(req, change=True, admin_any=True)
            if problem:
                return problem
            base = self._creator_art_path(card["id"])[:-4]
            gone = 0
            for tail in self.ART_TAILS:
                if os.path.isfile(base + tail):
                    os.remove(base + tail)
                    gone += 1
            card.pop("artUpload", None)
            card.pop("artMask", None)
            card.pop("artCredit", None)
            card["artDeleted"] = {"by": self._creator_user(req), "at": now_iso(), "reason": reason}
            if card.get("status") in ("pending", "approved"):      # a card cannot stay in review or in the game without its picture
                card["status"] = "rejected"
                card["reviewNote"] = "The uploaded artwork was removed by an admin" + (": " + reason if reason else ".")
            self._creator_store(card, path)
        log.info("creator: artwork of %s deleted by %s (%d file(s))%s", card["id"], card["artDeleted"]["by"], gone, " - " + reason if reason else "")
        return ok({"card": card, "files": gone})

    # ---- linking a game player to a site account (Settings > Account in the game)
    # Optional: nobody needs a site account to play. A link tells the server which game player
    # belongs to which account, so in-game permissions (tester) can follow the People tab.
    # One account <-> one game player; the link lives on the account as "player".
    def _creator_account_of_player(self, users, pfid):
        for key, user in users.items():
            if user.get("player") == pfid:
                return key, user
        return None, None

    def creator_perms_for_player(self, pfid):
        """Permissions of the site account linked to a game player, or None when not linked."""
        cached = self._users_cached()
        key = cached[4].get(pfid) if pfid else None
        user = cached[1].get(key) if key is not None else None
        return dict(self._creator_perms(user), name=user["name"]) if user else None

    def _end_play(self, pfid, reason):
        """When the server requires an account, a game player losing its account is signed out
        everywhere at once. Returns True when that happened."""
        if not pfid or self._settings().get("requireAccount") is not True:
            return False
        return self.store.sign_out(pfid, reason) > 0

    def api_Revival_AccountLink(self, req, headers):
        player = self.store.from_session(headers)
        if player is None:
            return err("NotAuthorized", "sign in to the game first")
        pfid = player["PlayFabId"]
        op = req.get("op") or "status"
        signed_out = False
        with self.store.lock:
            users = self._creator_users()
            key, linked = self._creator_account_of_player(users, pfid)
            if op == "link":
                if linked:
                    return err("InvalidParams", f"this game account is already linked to {linked['name']} - unlink it first")
                fails = getattr(self, "_link_fails", {})
                recent = [t for t in fails.get(pfid, []) if time.time() - t < 600]
                if len(recent) >= 5:
                    return err("TooManyAttempts", "too many wrong attempts - wait ten minutes")
                name = " ".join(str(req.get("name") or "").split())
                user = users.get(name.lower())
                passphrase = str(req.get("passphrase") or "")
                # the same wrong-password limits as the sign-in page, so linking is no way around them
                where = self._address(headers)
                if not self._allow("signin-fail", where, 20, 900, count=False) or (name and self._name_locked(name, where, user)):
                    return err("TooManyAttempts", "too many attempts - wait a few minutes and try again", 1199, 429)
                if user is None or not passphrase or not hmac.compare_digest(user["hash"], self._creator_hash(passphrase, user["salt"])):
                    fails[pfid] = recent + [time.time()]
                    self._link_fails = fails
                    self._count_wrong_password(name, where)
                    return err("InvalidPassword", "wrong username or password")
                if self._creator_perms(user)["access"] == "banned":
                    return err("AccountBanned", "this account has been banned")
                if user.get("deleteSticks") and user.get("deleteAt"):
                    return err("AccountBeingDeleted", "this account was banned and is being deleted", 1002, 403)
                if user.get("player") and user["player"] != pfid:
                    return err("InvalidParams", f"{user['name']} is already linked to another game account")
                user["player"], user["playerLinkedAt"] = pfid, now_iso()
                self._creator_users_save(users)
                linked = user
            elif op == "unlink" and self._settings().get("requireAccount") is True:
                # The site account and its game player are one thing: "unlink" only signs this
                # computer or phone out (its saved game login stops working); the account keeps
                # its player, and signing in again on any device brings it back.
                if linked:
                    self._forget_game_login(linked, headers.get("X-Revival-Login"))
                    self._creator_users_save(users)
                signed_out = self.store.sign_out_session(headers, "This game was signed out of its account. Start the game again and sign in.")
            elif op == "unlink":
                if linked:
                    for k in ("player", "playerLinkedAt", "gameLoginHash", "gameLoginAt", "gameLogins"):
                        linked.pop(k, None)
                    self._creator_users_save(users)
                    signed_out = self._end_play(pfid, "This game was unlinked from its account. Start the game again and sign in.")
                linked = None
            elif op != "status":
                return err("InvalidParams", "unknown request")
            perms = dict(self._creator_perms(linked), name=linked["name"]) if linked else None
        return ok({"linked": perms["name"] if perms else "", "account": perms, "signedOut": signed_out})

    # ---- bans
    # A player is banned when the site account linked to them is banned (People page), or when an
    # admin bans the game player directly (for players without a site account): data/bans.json.
    # A banned player cannot sign in, and their running game stops working at its next request.
    def _bans_path(self):
        return os.path.join(self.store.root, "bans.json")

    def _bans(self):
        try:
            with open(self._bans_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) and isinstance(data.get("players"), dict) else {"players": {}}
        except (OSError, ValueError):
            return {"players": {}}

    def _bans_save(self, data):
        path = self._bans_path()
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(path + ".tmp", path)

    def _banned_ids(self):
        """Ids of every banned game player (cached until either file changes)."""
        stamp = []
        for path in (self._bans_path(), os.path.join(self.store.root, "creator_users.json")):
            try:
                stamp.append(os.path.getmtime(path))
            except OSError:
                stamp.append(0)
        cached = getattr(self, "_banned_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        ids = set(self._bans()["players"])
        for user in self._creator_users_view().values():
            if user.get("player") and self._creator_perms(user)["access"] == "banned":
                ids.add(user["player"])
        self._banned_cache = (stamp, frozenset(ids))
        return self._banned_cache[1]

    @staticmethod
    def _banned_error():
        return err("AccountBanned", "This account has been banned from this server.", 1002, 403)

    def api_Revival_CreatorGameBans(self, req, headers):
        """Admins: the banned game players, and players matching "query" (name, friend code or id)."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see bans")
        banned = self._banned_ids()
        direct = self._bans()["players"]
        users = self._creator_users_view()

        def describe(pfid):
            p = self.store.load(pfid) or {}
            _, account = self._creator_account_of_player(users, pfid)
            entry = direct.get(pfid) or {}
            return {"player": pfid, "code": self.friend_code(pfid), "name": p.get("DisplayName") or "",
                    "account": account["name"] if account else "", "banned": pfid in banned,
                    "bannedHere": pfid in direct, "reason": entry.get("reason", ""), "by": entry.get("by", ""),
                    "at": entry.get("at", ""), "lastLogin": p.get("LastLogin", "")}

        found = []
        query = str(req.get("query") or "").strip().lower()
        if query:
            key = query.replace("-", "").replace(" ", "").upper()
            for pfid, name in self.store.names():
                if (query in name or pfid.startswith(key)) and len(found) < 30:
                    found.append(describe(pfid))
        return ok({"bans": [describe(pfid) for pfid in sorted(banned)], "found": found,
                   "requireAccount": self._settings().get("requireAccount") is True})

    def api_Revival_CreatorGameBan(self, req, headers):
        """Admins: ban or unban one game player ({"player": id, "ban": true/false, "reason": ...})."""
        pfid = str(req.get("player") or "").strip().upper()
        ban = req.get("ban") is not False
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_role(req) < 2:
                return err("NotAuthorized", "only admins can ban players")
            if not re.fullmatch(r"[0-9A-F]{16}", pfid) or self.store.load(pfid) is None:
                return err("InvalidParams", "no player with that id")
            users = self._creator_users()
            _, account = self._creator_account_of_player(users, pfid)
            if account and account["name"].lower() == me["name"].lower():
                return err("InvalidParams", "that is your own game account")
            if account and self._creator_rank(self._creator_perms(account)) >= self._creator_rank(self._creator_perms(me)):
                return err("NotAuthorized", f"that player belongs to {account['name']} - only someone ranked above can ban it")
            data = self._bans()
            if ban:
                data["players"][pfid] = {"reason": str(req.get("reason") or "")[:200], "by": me["name"], "at": now_iso()}
                if account and any(account.get(k) for k in self.ROLE_KEYS):
                    self._creator_strip_roles(account)      # a ban takes every role away
                    account["changedBy"], account["changedAt"] = me["name"], now_iso()
                    self._creator_users_save(users)
            else:
                data["players"].pop(pfid, None)
            self._bans_save(data)
        note = ""
        if not ban and account and self._creator_perms(account)["access"] == "banned":
            note = f"still banned through the site account {account['name']} (People page)"
        return ok({"player": pfid, "banned": pfid in self._banned_ids(), "note": note})

    # ---- word filter (chat messages, player names, account names)
    def _word_filter_path(self):
        return os.path.join(self.store.root, "word_filter.json")

    def _word_list(self):
        """(entries, saved by an admin?) - the admin's list, or the starting list in content/word-filter.txt."""
        try:
            with open(self._word_filter_path(), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("words"), list):
                return [str(w) for w in data["words"]], True
        except (OSError, ValueError):
            pass
        try:
            with open(os.path.join(self.content_root, "word-filter.txt"), encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")], False
        except OSError:
            return [], False

    def _word_filter(self):
        stamp = []
        for path in (self._word_filter_path(), os.path.join(self.content_root, "word-filter.txt")):
            try:
                stamp.append(os.path.getmtime(path))
            except OSError:
                stamp.append(0)
        cached = getattr(self, "_filter_cache", None)
        if not cached or cached[0] != stamp:
            cached = self._filter_cache = (stamp, WordFilter(self._word_list()[0]))
        return cached[1]

    def api_Revival_CreatorWordFilter(self, req, headers):
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see the word filter")
        words, saved = self._word_list()
        test = str(req.get("test") or "")
        return ok({"words": words, "saved": saved, "test": self._word_filter().censor(test)[0] if test else ""})

    def api_Revival_CreatorWordFilterSave(self, req, headers):
        words = req.get("words")
        if not isinstance(words, list) or len(words) > 2000:
            return err("InvalidParams", "send the list of words")
        clean = []
        for w in words:
            w = " ".join(str(w).split()).lower()[:60]
            if w and not w.startswith("#") and w not in clean:
                clean.append(w)
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_role(req) < 2:
                return err("NotAuthorized", "only admins can change the word filter")
            path = self._word_filter_path()
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"words": clean, "by": me["name"], "at": now_iso()}, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
        return ok({"words": clean, "saved": True})

    # ---- profile: rename, change passphrase, game login file
    def _creator_check_passphrase(self, user, passphrase):
        return bool(passphrase) and hmac.compare_digest(user["hash"], self._creator_hash(str(passphrase), user["salt"]))

    def _staff_password(self, me, req):
        """The acting admin's own password, asked again before deleting an account or making a reset
        code. None when right; otherwise the error to send (5 wrong tries per admin per 15 minutes)."""
        key = me["name"].lower()
        if not self._allow("staff-pass-fail", key, 5, 900, count=False):
            return err("TooManyAttempts", "too many wrong passwords - wait a few minutes and try again", 1199, 429)
        if not self._creator_check_passphrase(me, req.get("passphrase")):
            self._allow("staff-pass-fail", key, 5, 900)
            return err("InvalidPassword", "your password is wrong" if req.get("passphrase") else "enter your password to confirm")
        return None

    def api_Revival_CreatorRename(self, req, headers):
        """Changes the account's username (the current passphrase is asked again). Cards follow the new name."""
        new = " ".join(str(req.get("name") or "").split())
        if not re.fullmatch(r"[\w.'-]{5,30}", new):
            return err("InvalidParams", "usernames are 5 to 30 letters, digits, . ' - or _, without spaces")
        if self._word_filter().found(new):
            return err("InvalidParams", "that username contains a word that is not allowed here")
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_perms(me)["access"] == "banned":
                return err("NotAuthorized", "sign in first")
            if not self._creator_check_passphrase(me, req.get("passphrase")):
                return err("InvalidPassword", "wrong password")
            users = self._creator_users()
            old_key, new_key = me["name"].lower(), new.lower()
            if new_key != old_key and new_key in users:
                return err("NameNotAvailable", "that username is taken")
            if new == me["name"]:
                return err("InvalidParams", "that is already your name")
            was_founder = old_key in self._creator_founders()
            user = users.pop(old_key)
            old_name, user["name"] = user["name"], new
            user["renamedFrom"], user["renamedAt"] = old_name, now_iso()
            users[new_key] = user
            self._creator_users_save(users)
            if user.get("player"):                            # the account's player carries its name
                p = self.store.load(user["player"])
                if p is not None and isinstance(p.get("Login"), dict) and "account" in p["Login"]:
                    p["Login"]["account"] = new
                    self.store.save(p)
            if was_founder:                                   # the founding owner stays one under the new name
                path = os.path.join(self.store.root, "creator_admins.json")
                try:
                    with open(path, encoding="utf-8") as f:
                        names = json.load(f)
                    names = [new if str(n).lower() == old_key else n for n in names]
                    with open(path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(names, f)
                    os.replace(path + ".tmp", path)
                except (OSError, ValueError):
                    log.exception("could not update creator_admins.json for the rename %s -> %s", old_name, new)
            d = self._creator_dir()
            for card in self._creator_load():                  # the author's cards carry the name
                if card.get("author", "").lower() == old_key:
                    card["author"] = new
                    path = os.path.join(d, card["id"] + ".json")
                    with open(path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(card, f, ensure_ascii=False, indent=1)
                    os.replace(path + ".tmp", path)
        return ok({"name": new})

    def api_Revival_CreatorPassphrase(self, req, headers):
        """Changes the passphrase; every other browser signed in to the account is signed out."""
        new = str(req.get("newPassphrase") or "")
        if not 6 <= len(new) <= 200:
            return err("InvalidParams", "the password must be at least 6 characters")
        if any(ch.isspace() for ch in new):
            return err("InvalidParams", "the password cannot contain spaces")
        token = str(req.get("session") or "")
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_perms(me)["access"] == "banned":
                return err("NotAuthorized", "sign in first")
            if not self._creator_check_passphrase(me, req.get("passphrase")):
                return err("InvalidPassword", "the current password is wrong")
            users = self._creator_users()
            user = users[me["name"].lower()]
            user["salt"] = os.urandom(16).hex()
            user["hash"] = self._creator_hash(new, user["salt"])
            user["sessions"] = [hashlib.sha256(token.encode("utf-8")).hexdigest()]
            user["passphraseChangedAt"] = now_iso()
            user.pop("gameLoginHash", None)                 # every computer and phone signs in again
            user.pop("gameLogins", None)
            self._end_play(user.get("player"), "The password of this account was changed. Start the game again and sign in.")
            self._creator_users_save(users)
        return ok({})

    # ---- recovery code: the way back in after a forgotten password
    # Made on the profile page (the password is asked again) and shown once, for the player to
    # download or write down; the server keeps only a fingerprint. Using it sets a new password,
    # signs out every browser, computer and phone, and is used up (a new one can be made after).
    RECOVERY_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"     # no I, L, O or U: easy to read back

    @classmethod
    def _recovery_new(cls):
        raw = "".join(cls.RECOVERY_ALPHABET[b % 32] for b in os.urandom(20))   # 100 bits
        return "-".join(raw[i:i + 4] for i in range(0, 20, 4))

    @staticmethod
    def _recovery_hash(code):
        text = re.sub(r"[^0-9A-Z]", "", str(code or "").upper())
        text = text.replace("O", "0").replace("I", "1").replace("L", "1")
        if len(text) != 20:
            return None
        return hashlib.sha256(("warpforge-recovery:" + text).encode("utf-8")).hexdigest()

    def api_Revival_CreatorRecoveryCode(self, req, headers):
        """Makes a new recovery code for the signed-in account (replacing any earlier one), or with
        "remove" set, removes the account's code without making another."""
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_perms(me)["access"] == "banned":
                return err("NotAuthorized", "sign in first")
            if not self._creator_check_passphrase(me, req.get("passphrase")):
                return err("InvalidPassword", "wrong password")
            users = self._creator_users()
            user = users[me["name"].lower()]
            if req.get("remove"):
                had = bool(user.pop("recoveryHash", None))
                user.pop("recoveryAt", None)
                self._creator_users_save(users)
                return ok({"removed": had})
            code = self._recovery_new()
            user["recoveryHash"], user["recoveryAt"] = self._recovery_hash(code), now_iso()
            self._creator_users_save(users)
        return ok({"recoveryCode": code, "name": user["name"], "at": user["recoveryAt"]})

    def api_Revival_GameRecoveryCode(self, req, headers):
        """The game's Settings > Account (mod 0.13.35): a recovery code for the site account this game player
        is linked to, made exactly like the Profile page's (it replaces any earlier one). Needs the account's
        password, with the same wrong-password limits as signing in; the code goes back once and is never logged."""
        player = self.store.from_session(headers)
        if player is None:
            return err("NotAuthorized", "sign in to the game first")
        pfid = player["PlayFabId"]
        where = self._address(headers)
        with self.store.lock:
            users = self._creator_users()
            key, user = self._creator_account_of_player(users, pfid)
            if not user:
                return err("InvalidParams", "this game is not linked to a site account")
            if self._creator_perms(user)["access"] == "banned":
                return err("AccountBanned", "this account has been banned")
            if not self._allow("signin-fail", where, 20, 900, count=False) or self._name_locked(user["name"], where, user):
                return err("TooManyAttempts", "too many attempts - wait a few minutes and try again", 1199, 429)
            if not self._creator_check_passphrase(user, req.get("passphrase")):
                self._count_wrong_password(user["name"], where)
                return err("InvalidPassword", "wrong password" if req.get("passphrase") else "enter your password first")
            code = self._recovery_new()
            user["recoveryHash"], user["recoveryAt"] = self._recovery_hash(code), now_iso()
            self._creator_users_save(users)
            log.info("recovery code made in the game for account %s", user["name"])
        return ok({"recoveryCode": code, "name": user["name"], "at": user["recoveryAt"]})

    # ---- reset code from an admin: for someone who forgot the password and has no recovery code
    # Made on Admin > People by someone ranked above the account (never for one's own account),
    # shown once to that admin to pass on privately. Same form as a recovery code and used the same
    # way (sign-in page > Forgot your password?), but it works once and only for RESET_CODE_HOURS;
    # the server keeps only a fingerprint and who made it. The account's own recovery code, if it
    # has one, is left alone. Making another replaces the waiting one; "remove" cancels it.
    RESET_CODE_HOURS = 24

    def _reset_code_live(self, user):
        """The account's admin reset code fingerprint while it is still valid, else None (an expired one is dropped)."""
        if not user.get("adminResetHash"):
            return None
        try:
            until = datetime.fromisoformat(str(user.get("adminResetUntil") or "").replace("Z", "+00:00"))
        except ValueError:
            until = None
        if until is None or until.tzinfo is None or until <= datetime.now(timezone.utc):
            for k in ("adminResetHash", "adminResetUntil", "adminResetBy", "adminResetAt"):
                user.pop(k, None)
            return None
        return user["adminResetHash"]

    def api_Revival_CreatorResetCode(self, req, headers):
        """Admins: a one-time password reset code for another account ({"name", "passphrase": the admin's
        own password}), or {"name", "remove": true} to cancel one."""
        name = " ".join(str(req.get("name") or "").split())[:30].lower()
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_role(req) < 2:
                return err("NotAuthorized", "only admins can make reset codes")
            users = self._creator_users()
            user = users.get(name)
            if user is None:
                return err("InvalidParams", "no account with that name")
            if name == me["name"].lower():
                return err("InvalidParams", "for your own account, make a recovery code on your Profile page")
            if self._creator_rank(self._creator_perms(user)) >= self._creator_rank(self._creator_perms(me)):
                return err("NotAuthorized", f"{user['name']} can only be given a reset code by someone ranked above")
            if req.get("remove"):
                had = bool(self._reset_code_live(user))
                for k in ("adminResetHash", "adminResetUntil", "adminResetBy", "adminResetAt"):
                    user.pop(k, None)
                self._creator_users_save(users)
                log.info("account %s: reset code cancelled by %s", user["name"], me["name"])
                return ok({"removed": had})
            if self._creator_perms(user)["access"] == "banned":
                return err("AccountBanned", f"{user['name']} is banned")
            if user.get("deleteSticks") and user.get("deleteAt"):
                return err("AccountBeingDeleted", f"{user['name']} was banned and is being deleted")
            bad = self._staff_password(me, req)
            if bad:
                return bad
            code = self._recovery_new()
            until = datetime.fromtimestamp(time.time() + self.RESET_CODE_HOURS * 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            user["adminResetHash"], user["adminResetUntil"] = self._recovery_hash(code), until
            user["adminResetBy"], user["adminResetAt"] = me["name"], now_iso()
            self._creator_users_save(users)
        log.info("account %s: reset code made by %s (valid until %s)", user["name"], me["name"], until)
        # "recoveryCode" is a key the request log never writes out
        return ok({"name": user["name"], "recoveryCode": code, "until": until, "hours": self.RESET_CODE_HOURS})

    def api_Revival_CreatorRecover(self, req, headers):
        """Forgotten password: name + recovery code (the account's own, or a reset code from an admin)
        + new password. Signs in on success."""
        name = " ".join(str(req.get("name") or "").split())[:30]
        new = str(req.get("newPassphrase") or "")
        digest = self._recovery_hash(req.get("recoveryCode"))
        if not name or not req.get("recoveryCode"):
            return err("InvalidParams", "enter your name and your recovery code")
        if not 6 <= len(new) <= 200:
            return err("InvalidParams", "the new password must be at least 6 characters")
        if any(ch.isspace() for ch in new):
            return err("InvalidParams", "the password cannot contain spaces")
        where = self._address(headers)
        if not self._allow("recover-fail", where, 10, 900, count=False) or not self._allow("recover-fail-name", name.lower(), 5, 900, count=False):
            return err("TooManyAttempts", "too many attempts - wait a few minutes and try again", 1199, 429)
        with self.store.lock:
            users = self._creator_users()
            user = users.get(name.lower())
            own = bool(user and digest and user.get("recoveryHash") and hmac.compare_digest(user["recoveryHash"], digest))
            given = user and digest and not own and self._reset_code_live(user)
            given = bool(given and hmac.compare_digest(given, digest))
            if not own and not given:
                self._allow("recover-fail", where, 10, 900)
                self._allow("recover-fail-name", name.lower(), 5, 900)
                return err("InvalidPassword", "that username and code do not match")
            if self._creator_perms(user)["access"] == "banned":
                return err("AccountBanned", "this account has been banned")
            if user.get("deleteSticks") and user.get("deleteAt"):
                return err("AccountBeingDeleted", "this account was banned and is being deleted", 1002, 403)
            token = uuid.uuid4().hex + uuid.uuid4().hex
            user["salt"] = os.urandom(16).hex()
            user["hash"] = self._creator_hash(new, user["salt"])
            user["sessions"] = [hashlib.sha256(token.encode("utf-8")).hexdigest()]
            cancelled = self._account_delete_cancel(user)     # a reset is a sign-in too: a waiting deletion stops
            user["passphraseChangedAt"] = user["recoveredAt"] = now_iso()
            by = user.get("adminResetBy")
            if own:
                user.pop("recoveryHash", None)              # used up
                user.pop("recoveryAt", None)
            for k in ("adminResetHash", "adminResetUntil", "adminResetBy", "adminResetAt"):
                user.pop(k, None)                           # an admin's reset code goes too, used or not
            user.pop("gameLoginHash", None)                 # every computer and phone signs in again
            user.pop("gameLogins", None)
            self._end_play(user.get("player"), "The password of this account was reset. Start the game again and sign in.")
            self._creator_users_save(users)
        log.info("account %s: password reset with %s", user["name"], "its recovery code" if own else f"a reset code from {by}")
        return ok({"name": user["name"], "session": token, "account": self._creator_perms(user), "deletionCancelled": cancelled,
                   "usedResetCode": not own})

    # A game login lets the game sign in as the account's game player, with or without Steam. It
    # is a long secret kept by the mod in UserData/WarpforgeRevival/game-login.txt: written there
    # by the in-game sign-in window, or downloaded from the profile page. An account can have
    # several (one per computer or phone); unlinking, banning or changing the password cancels them all.
    GAME_LOGIN_PREFIX = "wfr-login-"
    GAME_LOGINS_KEPT = 10

    @staticmethod
    def _game_login_hashes(user):
        hashes = [g.get("h") for g in user.get("gameLogins") or [] if isinstance(g, dict) and g.get("h")]
        if user.get("gameLoginHash"):
            hashes.append(user["gameLoginHash"])
        return hashes

    def _game_login_user_cached(self, token):
        """(key, copy of the account) a game login belongs to, or (None, None), from memory (E1)."""
        token = str(token or "").strip()
        if token.startswith(self.GAME_LOGIN_PREFIX):
            token = token[len(self.GAME_LOGIN_PREFIX):]
        if len(token) < 32:
            return None, None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        cached = self._users_cached()
        key = cached[3].get(digest)
        user = cached[1].get(key) if key is not None else None
        if user is None or not any(hmac.compare_digest(h, digest) for h in self._game_login_hashes(user)):
            return None, None
        return key, copy.deepcopy(user)

    def _game_login_user(self, users, token):
        """(key, account) a game login belongs to, or (None, None)."""
        token = str(token or "").strip()
        if token.startswith(self.GAME_LOGIN_PREFIX):
            token = token[len(self.GAME_LOGIN_PREFIX):]
        if len(token) < 32:
            return None, None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for key, user in users.items():
            if any(hmac.compare_digest(h, digest) for h in self._game_login_hashes(user)):
                return key, user
        return None, None

    def _forget_game_login(self, user, token):
        """Cancels one saved game login of an account (the one a device sent), if it is known."""
        token = str(token or "").strip()
        if token.startswith(self.GAME_LOGIN_PREFIX):
            token = token[len(self.GAME_LOGIN_PREFIX):]
        if len(token) < 32:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        before = len(self._game_login_hashes(user))
        user["gameLogins"] = [g for g in user.get("gameLogins") or [] if isinstance(g, dict) and not hmac.compare_digest(str(g.get("h") or ""), digest)]
        if user.get("gameLoginHash") and hmac.compare_digest(user["gameLoginHash"], digest):
            user.pop("gameLoginHash", None)
            user.pop("gameLoginAt", None)
        return len(self._game_login_hashes(user)) < before

    def _account_player(self, users, key, user):
        """An existing game player that belongs to this account although the account has lost its
        link to it (made for it, Login {"account": name}), and that no other account uses. The
        newest one wins. None when there is none."""
        names = {key, str(user.get("name") or "").lower(), str(user.get("renamedFrom") or "").lower()} - {""}
        taken = {u.get("player") for k, u in users.items() if k != key and u.get("player")}
        best, best_time = None, -1.0
        try:
            files = os.listdir(self.store.players_dir)
        except OSError:
            return None
        for name in files:
            if not name.endswith(".json") or name[:-5] in taken:
                continue
            p = self.store.load(name[:-5])
            login = (p or {}).get("Login")
            if isinstance(login, dict) and str(login.get("account") or "").lower() in names:
                try:
                    when = os.path.getmtime(os.path.join(self.store.players_dir, name))
                except OSError:
                    when = 0.0
                if when > best_time:
                    best, best_time = name[:-5], when
        return best

    def _new_game_login(self, user, via):
        secret = uuid.uuid4().hex + uuid.uuid4().hex
        logins = [g for g in user.get("gameLogins") or [] if isinstance(g, dict)]
        if user.get("gameLoginHash"):                                   # the older single file
            logins.append({"h": user.pop("gameLoginHash"), "at": user.pop("gameLoginAt", ""), "via": "site"})
        logins.append({"h": hashlib.sha256(secret.encode("utf-8")).hexdigest(), "at": now_iso(), "via": via})
        user["gameLogins"] = logins[-self.GAME_LOGINS_KEPT:]
        return self.GAME_LOGIN_PREFIX + secret

    def api_Revival_CreatorGameLogin(self, req, headers):
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_perms(me)["access"] == "banned":
                return err("NotAuthorized", "sign in first")
            if not self._creator_check_passphrase(me, req.get("passphrase")):
                return err("InvalidPassword", "wrong password")
            users = self._creator_users()
            user = users[me["name"].lower()]
            if not user.get("player"):
                return err("InvalidParams", "link your game account first (in the game: Settings > Account)")
            token = self._new_game_login(user, "site")
            self._creator_users_save(users)
        return ok({"file": "game-login.txt", "token": token})

    def api_Revival_GameAccountSignIn(self, req, headers):
        """The game's sign-in window: name and password of a site account (or a new one, with
        "create"). Answers a game login for the mod to keep; no game session is needed."""
        status, resp = self.api_Revival_CreatorSignIn(req, headers)
        if status != 200:
            if resp.get("error") == "TermsNotAgreed" and req.get("termsBox") is not True:
                # a mod from before the sign-in window had the tick box: agreeing happens on the website
                site = str(self._settings().get("creatorUrl") or "").rstrip("/")
                return err("TermsNotAgreed", "please agree to the Terms and Privacy notice first: sign in once on the website"
                           + (" (" + site + ")" if site else "") + ", or update the mod", 1002, 403)
            return status, resp
        site_session = resp["data"]["session"]
        with self.store.lock:
            users = self._creator_users()
            user = users.get(resp["data"]["name"].lower())
            if user is None:
                return err("AccountNotFound", "no account with that name")
            digest = hashlib.sha256(site_session.encode("utf-8")).hexdigest()
            user["sessions"] = [d for d in user.get("sessions", []) if d != digest]   # the game does not need a site session
            if self._creator_perms(user)["access"] == "requested":
                self._creator_users_save(users)
                return err("AccountNotApproved", "your account is waiting for an admin to approve it", 1002, 403)
            token = self._new_game_login(user, "game")
            self._creator_users_save(users)
        return ok({"name": user["name"], "token": token, "deletionCancelled": resp["data"].get("deletionCancelled", False)})

    # ---- decks of the linked game account: view, export (done in the page) and import ----
    DECK_KEYS = ("PlayerDecks", "PlayerDecksFast")      # the game keeps Skirmish decks (gameMode 13) in the second list
    DECK_MODES = {0: "Classic", 13: "Skirmish", 14: "Custom Test"}
    MAX_DECKS = 100

    def _creator_deck_player(self, req):
        """(account, player) for the signed-in account's linked game player, or (None, error)."""
        me = self._creator_account(req)
        if not me or self._creator_perms(me)["access"] == "banned":
            return None, err("NotAuthorized", "sign in first")
        if not me.get("player"):
            return None, err("InvalidParams", "link your game account first (in the game: Settings > Account)")
        player = self.store.load(me["player"])
        if not player:
            return None, err("NotFound", "the linked game account has no saved data yet")
        return me, player

    @staticmethod
    def _decks_of(player, key):
        try:
            decks = json.loads(player.get("UserData", {}).get(key) or "[]")
        except ValueError:
            decks = []
        if isinstance(decks, dict):
            decks = list(decks.values())
        return [d for d in decks if isinstance(d, dict)]

    def _creator_known_cards(self):
        path = os.path.join(self.content_root, "files", "creator-cards.json")
        stamp = os.path.getmtime(path)
        if getattr(self, "_known_stamp", None) != stamp:
            with open(path, encoding="utf-8") as f:
                self._known = {c["id"]: c for c in json.load(f)["cards"]}
            self._known_stamp = stamp
        return self._known

    def api_Revival_CreatorDecks(self, req, headers):
        with self.store.lock:
            me, player = self._creator_deck_player(req)
            if not me:
                return player
            out = []
            for key in self.DECK_KEYS:
                for d in self._decks_of(player, key):
                    mode = d.get("gameMode") if d.get("gameMode") in self.DECK_MODES else (13 if key == "PlayerDecksFast" else 0)
                    out.append({"id": d.get("id"), "name": d.get("name") or "", "gameMode": mode, "mode": self.DECK_MODES[mode], "hero": d.get("hero") or "",
                                "defenseCard": d.get("defenseCard") or "", "cards": d.get("cards") or {},
                                "cardback": d.get("cardback") or "", "deckTag": d.get("deckTag") or 0})
        return ok({"decks": out, "max": self.MAX_DECKS})

    def api_Revival_CreatorDeckImport(self, req, headers):
        """Add one deck to the linked game account. Nothing is replaced: an import is always a new deck."""
        deck = req.get("deck")
        if not isinstance(deck, dict):
            return err("InvalidParams", "that is not a deck")
        try:
            known = self._creator_known_cards()
        except (OSError, ValueError, KeyError):
            return err("NotFound", "this server has no card list to check the deck against")
        name = re.sub(r"\s+", " ", str(deck.get("name") or "")).strip()[:40] or "Imported deck"
        hero, defense = deck.get("hero"), deck.get("defenseCard") or ""
        # Cards missing from the list (Space Wolves, whose card file this server does not have) are
        # let through when they at least look like a card id; cards on the list are checked properly.
        looks = lambda cid: isinstance(cid, str) and re.fullmatch(r"[A-Z]{2,4}\d{1,3}", cid) is not None
        if not looks(hero) or known.get(hero, {"kind": "hero"})["kind"] != "hero":
            return err("InvalidParams", "the deck's warlord is not a warlord")
        if defense and not looks(defense):
            return err("InvalidParams", "the deck's defence card is not a card")
        cards = deck.get("cards")
        if not isinstance(cards, dict) or not cards or len(cards) > 100:
            return err("InvalidParams", "the deck has no card list")
        clean = {}
        for cid, n in cards.items():
            c = known.get(cid)
            if not looks(cid) or (c and (c["kind"] == "hero" or c.get("token"))):
                return err("InvalidParams", "the deck contains a card that cannot be put in a deck: %s" % str(cid)[:20])
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 4:
                return err("InvalidParams", "the deck has an impossible number of copies of %s" % (c["name"] if c else cid))
            clean[cid] = n
        mode = deck.get("gameMode")
        if isinstance(mode, bool) or mode not in self.DECK_MODES:
            mode = 0
        key = "PlayerDecksFast" if mode == 13 else "PlayerDecks"
        cardback = deck.get("cardback") if isinstance(deck.get("cardback"), str) and re.fullmatch(r"[0-9a-f]{32}", deck.get("cardback")) else ""
        with self.store.lock:
            me, player = self._creator_deck_player(req)
            if not me:
                return player
            decks = self._decks_of(player, key)
            if sum(len(self._decks_of(player, k)) for k in self.DECK_KEYS) >= self.MAX_DECKS:
                return err("InvalidParams", "this game account already has %d decks" % self.MAX_DECKS)
            if not cardback:
                cardback = next((d.get("cardback") for d in decks if d.get("cardback")), "10a1e3f4c77c0491196b655ad4d7d72d")
            taken = {d.get("name") for d in decks}
            shown, n = name, 2
            while shown in taken:
                shown, n = "%s (%d)" % (name[:36], n), n + 1
            new = {"cardback": cardback, "cards": clean, "deckTag": 0, "hero": hero, "defenseCard": defense,
                   "id": str(uuid.uuid4()), "lastEditTime": 0, "name": shown, "gameMode": mode}
            decks.append(new)
            player.setdefault("UserData", {})[key] = json.dumps(decks, separators=(",", ":"))
            player["DataVersion"] = player.get("DataVersion", 0) + 1
            self.store.save(player)
        return ok({"name": shown, "id": new["id"], "mode": self.DECK_MODES[mode]})

    def api_Revival_CreatorExamples(self, req, headers):
        """Official example cards per keyword, as the game's card file stores them (signed-in accounts only)."""
        if not self._creator_user(req):
            return err("NotAuthorized", "sign in first")
        path = os.path.join(self.content_root, "creator-examples.json")
        try:
            stamp = os.path.getmtime(path)
            if getattr(self, "_examples_stamp", None) != stamp:
                with open(path, encoding="utf-8") as f:
                    self._examples = json.load(f)
                self._examples_stamp = stamp
        except (OSError, ValueError):
            return err("NotFound", "no examples are available on this server")
        keyword = req.get("keyword")
        if keyword:
            ex = self._examples.get("keywords", {}).get(keyword) or {"count": 0, "cards": []}
            ids = self._official_ids_by_name()
            cards = [dict(c, id=c.get("id") or ids.get((c.get("name"), c.get("army")), "")) for c in ex.get("cards") or []]
            return ok({"keyword": keyword, "examples": dict(ex, cards=[_card_without_text(c) for c in cards])})
        return ok({"keywords": {k: v["count"] for k, v in self._examples.get("keywords", {}).items()}})

    def _official_ids_by_name(self):
        """(name, army) -> card id from creator-cards.json, for examples that carry only a name (cards made
        by other cards have no short id there), so the player's export can name them."""
        path = os.path.join(self.content_root, "files", "creator-cards.json")
        try:
            stamp = os.path.getmtime(path)
            if getattr(self, "_ids_by_name_stamp", None) != stamp:
                with open(path, encoding="utf-8") as f:
                    cards = json.load(f).get("cards") or []
                self._ids_by_name = {(c.get("name"), c.get("army")): c.get("id") for c in cards if c.get("id")}
                self._ids_by_name_stamp = stamp
        except (OSError, ValueError, AttributeError):
            return {}
        return self._ids_by_name

    def api_Revival_CreatorData(self, req, headers):
        """The card creator's menus and lists for signed-in, approved accounts (not banned, not still
        waiting): the official card list, the effect schema, our help texts, keywords and stratagem
        types. They are not plain downloads (security review 27). {file} -> {file, data}."""
        if not self._creator_user(req):
            return err("NotAuthorized", "sign in first")
        name = req.get("file")
        if name not in CREATOR_DATA_FILES:
            return err("InvalidParams", "unknown file")
        try:
            with open(os.path.join(self.content_root, "files", name), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return err("NotFound", "that file is not on the server", 1001, 404)
        if name == "creator-cards.json" and isinstance(data, dict):
            data = dict(data, cards=[_card_without_text(c) for c in data.get("cards") or []])
        return ok({"file": name, "data": data})

    def api_Revival_CreatorOfficial(self, req, headers):
        """The card viewer: every official card (the list), or one card's creator design and game data (signed-in accounts only)."""
        if not self._creator_user(req):
            return err("NotAuthorized", "sign in first")
        path = os.path.join(self.content_root, "creator-official.json")
        try:
            stamp = os.path.getmtime(path)
            if getattr(self, "_official_stamp", None) != stamp:
                with open(path, encoding="utf-8") as f:
                    self._official = json.load(f)
                self._official_stamp = stamp
        except (OSError, ValueError):
            return err("NotFound", "the card viewer's data is not on this server")
        card_id = req.get("id")
        if card_id:
            one = (self._official.get("detail") or {}).get(str(card_id))
            if not one:
                return err("NotFound", "no official card has that id")
            return ok(dict(_card_without_text(one), id=str(card_id)))
        if getattr(self, "_official_list_stamp", None) != self._official_stamp:
            self._official_list = [_card_without_text(c) for c in self._official.get("cards") or []]
            self._official_list_stamp = self._official_stamp
        return ok({"cards": self._official_list})

    def api_Revival_CreatorUnreleased(self, req, headers):
        """Admin > Unreleased cards: the cards that only exist as text in the game (names and rules text
        Everguild wrote but never turned into card data), from content/unreleased-cards.json. Admins only."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see the unreleased cards")
        path = os.path.join(self.content_root, "unreleased-cards.json")
        try:
            stamp = os.path.getmtime(path)
            if getattr(self, "_unreleased_stamp", None) != stamp:
                with open(path, encoding="utf-8") as f:
                    self._unreleased = json.load(f)
                self._unreleased_stamp = stamp
        except (OSError, ValueError):
            return err("NotFound", "the unreleased card list is not on this server")
        return ok({"cards": self._unreleased.get("cards") or [], "keywords": self._unreleased.get("keywords") or {}})

    def api_Revival_CreatorPeople(self, req, headers):
        """Every account with its permissions and number of cards (admins only)."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see the accounts")
        with self.store.lock:
            users, cards = self._creator_users_view(), self._creator_load()
        counts = {}
        for c in cards:
            counts[c.get("author", "").lower()] = counts.get(c.get("author", "").lower(), 0) + 1
        def game_name(pfid):
            p = self.store.load(pfid) if pfid else None
            return (p.get("DisplayName") or pfid) if p else ""
        # the Admin > People page leaves out accounts still waiting for access: Admin > Requests lists those
        skip = req.get("skipRequested") is True
        people = [dict(self._creator_perms(u), name=u["name"], created=u.get("created", ""), cards=counts.get(key, 0),
                       playerName=game_name(u.get("player")), deleteAt=u.get("deleteAt") or "",
                       terms=u.get("termsAgreed") or "",
                       resetUntil=u.get("adminResetUntil") or "" if self._reset_code_live(u) else "",
                       resetBy=u.get("adminResetBy") or "" if self._reset_code_live(u) else "") for key, u in users.items()
                  if not (skip and self._creator_perms(u)["access"] == "requested")]
        people.sort(key=lambda p: (p["access"] != "requested", not p["fixed"], not p["owner"], not p["admin"], p["name"].lower()))
        # who is on now: in a match room ("playing"), or their game asked for chat lately ("online")
        now = time.time()
        playing = set()
        try:
            with photon_emu.SERVICE.lock:
                for room in photon_emu.SERVICE.rooms.values():
                    for a in room.actors.values():
                        if a.conn is not None and a.player:
                            playing.add(a.player)
        except Exception:
            pass
        online = {pid for pid, t in list(photon_emu.SEEN.items()) if now - t <= self.ONLINE_SECONDS} | playing
        for p in people:
            p["online"] = "playing" if p["player"] in playing else "online" if p["player"] in online else ""
        totals = {"accounts": len(users),
                  "waiting": sum(1 for u in users.values() if self._creator_perms(u)["access"] == "requested"),
                  "online": len(online), "playing": len(playing)}
        me = self._creator_account(req)
        mine = self._creator_perms(me) if me else {"owner": False, "fixed": False}
        return ok({"people": people, "totals": totals, "owner": mine["owner"], "founder": mine["fixed"], "autoApprove": self._creator_auto_approve(),
                   "requireAccount": self._settings().get("requireAccount") is True})

    def api_Revival_CreatorRequests(self, req, headers):
        """Accounts waiting for access, oldest first, a page at a time (admins only). Kept small on purpose
        (name, when it was made, whether it is set to be deleted) so a long queue stays quick to load."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see the requests")
        try:
            offset = max(0, int(req.get("offset") or 0))
            limit = min(100, max(0, int(req.get("limit") if req.get("limit") is not None else 50)))
        except (TypeError, ValueError):
            return err("InvalidParams", "offset and limit are numbers")
        query = " ".join(str(req.get("query") or "").split()).lower()[:30]
        with self.store.lock:
            users = self._creator_users_view()
        waiting = [u for u in users.values() if self._creator_perms(u)["access"] == "requested"]
        total = len(waiting)
        if query:
            waiting = [u for u in waiting if query in u["name"].lower()]
        waiting.sort(key=lambda u: (u.get("created", ""), u["name"].lower()))
        page = [{"name": u["name"], "created": u.get("created", ""), "deleteAt": u.get("deleteAt") or ""}
                for u in waiting[offset:offset + limit]]
        return ok({"requests": page, "total": total, "matching": len(waiting), "offset": offset,
                   "autoApprove": self._creator_auto_approve()})

    def api_Revival_CreatorSetAccount(self, req, headers):
        """An admin changes one account: any of access, reviewer, tester, admin, owner (see the ranks above)."""
        name = str(req.get("name") or "").lower()
        access = req.get("access")
        if access is not None and access not in self.ACCESS:
            return err("InvalidParams", "unknown access value")
        unlink = req.get("unlink") is True
        delete = req.get("delete")
        if delete is not None and not isinstance(delete, bool):
            return err("InvalidParams", "delete is an on/off value")
        flags = {k: req[k] for k in ("reviewer", "tester", "admin", "owner") if k in req}
        if not all(isinstance(v, bool) for v in flags.values()):
            return err("InvalidParams", "reviewer, tester, admin and owner are on/off values")
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_role(req) < 2:
                return err("NotAuthorized", "only admins can edit accounts")
            users = self._creator_users()
            user = users.get(name)
            if user is None:
                return err("InvalidParams", "no account with that name")
            if name in self._creator_founders():
                return err("NotAuthorized", f"{user['name']} is the founding owner and cannot be changed here")
            mine, theirs = self._creator_perms(me), self._creator_perms(user)
            rank = self._creator_rank
            if "owner" in flags and not mine["fixed"]:
                return err("NotAuthorized", "only the founding owner can give or take owner")
            if "admin" in flags and rank(mine) < 2:
                return err("NotAuthorized", "only an owner can give or take admin")
            if name != me["name"].lower() and rank(theirs) >= rank(mine):
                who = "an owner" if theirs["owner"] else "an admin"
                return err("NotAuthorized", f"{user['name']} is {who} - only someone ranked above can change that account")
            if name == me["name"].lower() and (flags.get("admin") is False or flags.get("owner") is False or access in ("banned", "requested")):
                return err("InvalidParams", "you cannot remove your own rights - ask someone ranked above you")
            if delete is not None and name == me["name"].lower():
                return err("InvalidParams", "delete your own account from your Profile")
            if delete is True and theirs["access"] != "requested":     # declining a request (Admin > Requests) does not ask
                bad = self._staff_password(me, req)
                if bad:
                    return bad
            if "role" in user:                      # move the older single role to the separate switches
                old = user.pop("role")
                user.setdefault("admin", old == "admin")
                user.setdefault("reviewer", old == "reviewer")
            user.update(flags)
            if unlink and self._settings().get("requireAccount") is True:
                # accounts and players are one: sign the game out everywhere, keep the player
                self._end_play(user.get("player"), "An admin signed this account out of the game. Start the game again and sign in.")
                for k in ("gameLoginHash", "gameLoginAt", "gameLogins"):
                    user.pop(k, None)
            elif unlink:
                self._end_play(user.get("player"), "An admin unlinked this game from its account. Start the game again and sign in.")
                for k in ("player", "playerLinkedAt", "gameLoginHash", "gameLoginAt", "gameLogins"):
                    user.pop(k, None)
            if access is not None:
                user["access"] = access
                if access == "banned":
                    user["sessions"] = []           # signed out everywhere
                    self._creator_strip_roles(user)
            if delete is True and not user.get("deleteAt"):
                self._account_delete_start(user, me["name"])
                log.info("account %s will be deleted on %s (asked by %s)", user["name"], user["deleteAt"], me["name"])
            elif delete is False and self._account_delete_cancel(user, by_staff=True):
                log.info("deletion of the account %s cancelled by %s", user["name"], me["name"])
            user["changedBy"], user["changedAt"] = me["name"], now_iso()
            self._creator_users_save(users)
            perms = self._creator_perms(user)
        return ok(dict(perms, name=user["name"], deleteAt=user.get("deleteAt") or ""))

    def api_Revival_CreatorDiscard(self, req, headers):
        """An author throws away one of their own drafts (kept on disk as <id>.discarded)."""
        card_id = str(req.get("id") or "")
        with self.store.lock:
            author = self._creator_user(req)
            path = os.path.join(self._creator_dir(), card_id + ".json")
            if not author or not re.fullmatch(r"CUSTOM-\d{4}", card_id) or not os.path.isfile(path):
                return err("InvalidParams", "unknown card")
            with open(path, encoding="utf-8") as f:
                card = json.load(f)
            if card.get("author", "").lower() != author.lower() or card.get("status") not in ("draft", "rejected"):
                return err("NotAuthorized", "only your own drafts and rejected cards can be discarded")
            os.replace(path, path[:-5] + ".discarded")
        return ok({})

    def api_Revival_CreatorReview(self, req, headers):
        role = self._creator_role(req)
        if not role:
            return err("NotAuthorized", "only reviewers and admins can review cards")
        card_id, status = str(req.get("id") or ""), req.get("status")
        if not re.fullmatch(r"CUSTOM-\d{4}", card_id) or status not in self.CREATOR_STATUSES:
            return err("InvalidParams", "unknown card or status")
        with self.store.lock:
            path = os.path.join(self._creator_dir(), card_id + ".json")
            if not os.path.isfile(path):
                return err("InvalidParams", "that card no longer exists")
            with open(path, encoding="utf-8") as f:
                card = json.load(f)
            if role < 2 and card.get("status") == "approved":
                return err("NotAuthorized", "only an admin can un-approve a card")
            if card.get("status") == "draft":
                return err("InvalidParams", "that card is still a draft")
            # a reviewer's own card needs someone else's approval (admins answer for the whole set and may)
            if status == "approved" and role < 2 and card.get("author", "").lower() == (self._creator_user(req) or "").lower():
                return err("NotAuthorized", "you cannot approve your own card - another reviewer or an admin has to")
            card.update(status=status, reviewNote=str(req.get("note") or "")[:500], reviewed=now_iso(), returned=False,
                        reviewedAs="admin" if role == 2 else "reviewer")
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=1)
            os.replace(path + ".tmp", path)
        return ok({"card": card})

    # ------------------------------------------------------------------ limits
    # How often one address (or one name, or one player) may do something in a stretch of time.
    # Kept in memory: a restart forgets it, which is fine for slowing down guessing and flooding.
    _limits = {}
    _limits_lock = threading.Lock()

    @classmethod
    def _allow(cls, what, who, times, seconds, count=True):
        """True while `who` has done `what` fewer than `times` in the last `seconds`. With count=False it only asks."""
        now = time.time()
        with cls._limits_lock:
            if len(cls._limits) > 20000:
                # Never let the table itself become the problem - but only forget what has run out,
                # so many addresses cannot wipe everyone's counts.
                for k in [k for k, (span, ts) in cls._limits.items() if not ts or ts[-1] <= now - span]:
                    del cls._limits[k]
            hits = [t for t in cls._limits.get((what, who), (seconds, ()))[1] if t > now - seconds]
            ok_now = len(hits) < times
            if ok_now and count:
                hits.append(now)
            if hits:
                cls._limits[(what, who)] = (seconds, hits)
            else:
                cls._limits.pop((what, who), None)
            return ok_now

    @staticmethod
    def _address(headers):
        return str(headers.get("X-Revival-Client-IP") or "unknown")

    # ------------------------------------------------------------------ plumbing
    def _settings(self):
        """The owner's revival-settings.json ({} when there is none). Kept in memory until the file
        changes (E4); each caller gets its own copy."""
        def read(path):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else {}
            except (OSError, ValueError):
                return {}
        d = self._file_cached("settings", os.path.join(self.content_root, "files", "revival-settings.json"), read)
        return copy.deepcopy(d) if d else {}

    @staticmethod
    def _version_number(text):
        """(0, 11, 12) for "0.11.12-w"; () when it is not a version."""
        m = re.match(r"\s*(\d+(?:\.\d+){0,3})", str(text or ""))
        return tuple(int(x) for x in m.group(1).split(".")) if m else ()

    def _mod_refused(self, headers):
        """(error reply, note for the log) when this game may not sign in, else (None, "").

        Three checks, each switched on by the owner in revival-settings.json:
          "minModVersion": "0.11.17", or "latest" for whatever build this server is handing out -
              older mods (and ones that do not say their version) are refused;
          "onlyRevivalMod": true - the game must report that no other mod or plugin is loaded, and,
              when it runs the very version this server hands out, that its mod file is that file.
        Windows and Android builds share version numbers. What the game reports about itself can
        be faked by someone who rewrites the mod, so this keeps honest players on one set of rules
        and stops casual tampering; it is not proof.
        """
        settings = self._settings()
        mine = str(headers.get("X-Revival-Mod") or "").strip()
        android = mine.endswith("-a")
        try:
            served = json.loads(headers.get("X-Revival-Served") or "{}").get("a" if android else "w") or {}
        except (ValueError, AttributeError):
            served = {}
        has = self._version_number(mine)

        wanted = str(settings.get("minModVersion") or "").strip()
        if wanted.lower() == "latest":
            wanted = str(served.get("v") or "")
        need = self._version_number(wanted)
        if need:
            width = max(len(need), len(has))
            if not has or has + (0,) * (width - len(has)) < need + (0,) * (width - len(need)):
                number = ".".join(str(n) for n in need)
                return (err("ModOutOfDate", f"This server needs Warpforge Revival {number} or newer. Update the mod, then start the game again.", 1001, 401),
                        "mod %s is older than %s" % (mine or "(not given)", number))

        if settings.get("onlyRevivalMod") is True:
            others = headers.get("X-Revival-Others")
            if others is None:
                return (err("ModOutOfDate", "This server needs a newer Warpforge Revival mod. Update the mod, then start the game again.", 1001, 401),
                        "mod %s does not report what else is loaded" % (mine or "(not given)"))
            others = re.sub(r"[^\w .,'+()-]", "", str(others))[:200].strip()
            if others.lower() == "none":
                others = ""
            others = ", ".join(n for n in (x.strip() for x in others.split(",")) if n)
            if others:
                return (err("OtherModsLoaded", "This server only allows the Warpforge Revival mod. Remove these and start the game again: " + others, 1001, 401),
                        "other mods loaded: " + others)
            sent = str(headers.get("X-Revival-Hash") or "").strip().lower()
            # ("unknown": the mod could not read its own file - let it in, since refusing would lock out honest players)
            if sent != "unknown" and served.get("h") and has and has == self._version_number(served.get("v")) and sent != str(served["h"]).lower():
                return (err("ModChanged", "This copy of the Warpforge Revival mod is not the one this server hands out. Let the game update the mod, or download it again.", 1001, 401),
                        "mod file of %s differs from the one served" % mine)
        return None, ""

    def handle(self, path, headers, body_bytes):
        api = path[len("/playfab/"):].split("?")[0]
        try:
            req = json.loads(body_bytes or b"{}")
        except ValueError:
            req = {}
        handler = getattr(self, "api_" + api.replace("/", "_"), None)
        replay = self._replay_slot(api, headers)
        if replay is not None and replay.get("resp") is not None:
            self._log(api, req, replay["status"], replay["resp"], "REPLAYED: the same request sent again")
            return replay["status"], replay["resp"]
        try:
            return self._handle(api, req, headers, handler, replay)
        finally:
            if replay is not None:
                replay["done"].set()

    # ---- the same request sent twice
    # The mod tries a request once more when its connection failed, and the first try may have
    # reached the server after all. Both tries carry the same X-Revival-Request id; the second gets
    # the first one's answer instead of being carried out again (a draft pick would otherwise be
    # refused as a pick of a pack no longer offered, or worse, taken twice).
    REPLAY_SECONDS = 120

    def _replay_slot(self, api, headers):
        rid = str(headers.get("X-Revival-Request") or "")
        if api != "Client/ExecuteCloudScript" or not re.fullmatch(r"[0-9a-f]{32}", rid):
            return None
        try:
            me = self.store.session_player_id(headers)
        except Exception:
            me = None
        if not me:
            return None
        now = time.time()
        lock = self.__dict__.setdefault("_replay_lock", threading.Lock())      # also after a reload of this file
        replays = self.__dict__.setdefault("_request_replays", {})
        with lock:
            for k in [k for k, v in replays.items() if v["t"] < now - self.REPLAY_SECONDS]:
                del replays[k]
            slot = replays.get((me, rid))
            if slot is None:
                if len(replays) > 5000:
                    return None
                slot = replays[(me, rid)] = {"t": now, "resp": None, "status": None, "done": threading.Event(), "first": True}
                return slot
        slot["done"].wait(10)                  # the first try is still being carried out (10 s: review #18)
        return dict(slot, first=False) if slot["resp"] is not None else None

    def _handle(self, api, req, headers, handler, replay):
        status, resp = self._handle_inner(api, req, headers, handler)
        if replay is not None and replay.get("first"):
            replay["status"], replay["resp"] = status, resp
        return status, resp

    def _handle_inner(self, api, req, headers, handler):
        refused, why = self._mod_refused(headers) if api.startswith("Client/Login") else (None, "")
        if refused is not None:
            status, resp = refused
            note = "REFUSED: " + why
        elif handler is None:
            status, resp = ok({})
            note = "UNIMPLEMENTED (returned empty success)"
        else:
            try:
                status, resp = handler(req, headers)
                note = ""
            except NotSignedIn:
                why = self.store.signed_out_reason(headers)
                if why:     # the mod closes the game with this message (the next start asks to sign in)
                    status, resp = err("SignedOut", why, 1074, 401)
                    note = "REFUSED: signed out"
                else:
                    status, resp = err("NotAuthenticated", "This session is no longer valid. Restart the game to sign in again.", 1074, 401)
                    note = "REFUSED: no valid session"
            except Exception as e:  # report to the game as a PlayFab error, keep serving
                log.exception("PlayFab %s failed", api)
                status, resp = err("InternalServerError", f"{type(e).__name__}: {e}", 1123, 500)
                note = "EXCEPTION"
        if api in ("Revival/ChatPoll", "Revival/CreatorList", "Revival/CreatorExamples", "Revival/CreatorDecks", "Revival/PlayerStats") and not note:      # asked every few seconds; keep it out of the logs
            return status, resp
        log_req, log_resp = req, resp
        if api == "Client/LoginWithSteam" and str(req.get("SteamTicket", "")).startswith(self.GAME_LOGIN_PREFIX):
            log_req = dict(req, SteamTicket=self.GAME_LOGIN_PREFIX + "<not logged>")
        if api.startswith("Revival/Bug"):
            # bug reports: never the screenshot, crash dump, log text or description (they can hold chat)
            log_req = {k: (v if k in ("id", "status", "file", "category", "severity", "kind", "fixedIn", "duplicateOf")
                           else "<%d chars>" % len(v) if isinstance(v, str) else "<not logged>") for k, v in req.items() if k != "session"}
            if isinstance(resp.get("data"), dict) and any(k in resp["data"] for k in ("data", "log", "description", "bugs")):
                log_resp = dict(resp, data="<not logged>")
        elif api.startswith("Revival/Creator") or api.startswith("Revival/Account"):
            # never write secrets or whole pictures to the log: passphrases, sign-in tokens, staff keys, images
            log_req = {k: v for k, v in req.items() if k not in ("image", "session", "key", "passphrase", "newPassphrase")}
            if isinstance(resp.get("data"), dict) and any(k in resp["data"] for k in ("image", "session", "token")):
                log_resp = dict(resp, data="<not logged>")
        self._log(api, log_req, status, log_resp, note)
        if note:
            log.warning("PlayFab %s %s", api, note)
        else:
            log.info("PlayFab %s -> %d", api, status)
        return status, resp

    # ---- the request log (logs/playfab.jsonl)
    # Kept for finding problems, so it must not turn into a store of secrets or grow for ever:
    #  - anything that signs someone in is never written (passwords, sessions, tickets, pictures);
    #  - a device id or Steam ticket is written as a short fingerprint: enough to tell two devices
    #    apart in the log, useless for signing in as one;
    #  - entries older than "logDays" in revival-settings.json (default 14) are dropped once a day,
    #    and the file is cut back if it still passes 50 MB.
    LOG_SECRET = {"passphrase", "newpassphrase", "password", "session", "key", "token", "ticket", "image", "mask",
                  "sessionticket", "entitytoken", "devicetoken", "x-authorization", "secret", "recoverycode"}
    LOG_IDENTITY = {"steamticket", "androiddeviceid", "androiddevice", "customid", "iosdeviceid", "deviceid", "nintendoswitchdeviceid"}
    LOG_MAX_BYTES = 50 * 1024 * 1024

    @classmethod
    def _log_safe(cls, value, depth=0):
        """A copy of a request or reply that is safe to write to the log."""
        if depth > 12:
            return "<...>"
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                name = str(k).lower()
                if name in cls.LOG_SECRET and v not in ("", None, False):
                    out[k] = "<not logged>"
                elif name in cls.LOG_IDENTITY and isinstance(v, (str, int)) and v != "":
                    text = str(v)
                    out[k] = text if text.startswith("#") and len(text) == 11 else "#" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]
                else:
                    out[k] = cls._log_safe(v, depth + 1)
            return out
        if isinstance(value, list):
            return [cls._log_safe(v, depth + 1) for v in value]
        return value

    def _log_days(self):
        try:
            return max(1, min(3650, int(self._settings().get("logDays", 14))))
        except (TypeError, ValueError):
            return 14

    def _log_tidy(self):
        """Rewrites the log without secrets and without entries past their keeping time (see above)."""
        with self.log_lock:
            self._log_tidied = now_iso()[:10]
            if not os.path.isfile(self.log_path):
                return
            oldest = datetime.fromtimestamp(time.time() - self._log_days() * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            kept, dropped, cleaned = [], 0, 0
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        dropped += 1
                        continue
                    if str(entry.get("t") or "") < oldest:
                        dropped += 1
                        continue
                    safe = self._log_safe(entry)
                    if safe != entry:
                        cleaned += 1
                    kept.append(json.dumps(safe, ensure_ascii=False))
            size = sum(len(k) + 1 for k in kept)
            while kept and size > self.LOG_MAX_BYTES:               # still too big: the oldest go first
                cut = max(1, len(kept) // 10)
                size -= sum(len(k) + 1 for k in kept[:cut])
                dropped += cut
                kept = kept[cut:]
            if not dropped and not cleaned:
                return
            tmp = self.log_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for k in kept:
                    f.write(k + "\n")
            os.replace(tmp, self.log_path)
        log.info("request log tidied: %d entr(ies) dropped, %d cleaned of sign-in details, %d kept", dropped, cleaned, len(kept))

    def _log(self, api, req, status, resp, note):
        if time.time() - getattr(self, "_sweep_at", 0) >= 3600:
            try:
                self._account_sweep()
            except Exception:
                log.exception("could not finish deleting accounts")
        if time.time() - getattr(self, "_matches_pruned_at", 0) >= 3600:
            self._matches_pruned_at = time.time()
            try:
                self._matches_prune()
            except Exception:
                log.exception("could not tidy the match records")
        if getattr(self, '_log_tidied', '') != now_iso()[:10]:          # once a day
            try:
                self._log_tidy()
            except Exception:
                log.exception("could not tidy the request log")
        entry = {"t": now_iso(), "api": api, "status": status, "note": note, "request": self._log_safe(req),
                 "response": self._log_safe(resp) if len(json.dumps(resp)) < 4000 else "<large>"}
        with self.log_lock, open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _player(self, headers):
        p = self.store.from_session(headers)
        if p is None:
            raise NotSignedIn("not logged in")
        return p

    # ------------------------------------------------------------------ login
    def _login_result(self, player, ticket, created, info_params):
        info_params = info_params or {}
        pfid = player["PlayFabId"]
        name = player.get("DisplayName") or f"Commander-{pfid[:4]}"
        payload = {}
        if info_params.get("GetUserAccountInfo"):
            payload["AccountInfo"] = {
                "PlayFabId": pfid, "Created": player["Created"],
                "TitleInfo": {"DisplayName": name, "Origination": "Steam", "Created": player["Created"],
                              "AvatarUrl": player.get("AvatarUrl") or "",
                              "LastLogin": player["LastLogin"], "FirstLogin": player["Created"], "isBanned": False,
                              "TitlePlayerAccount": {"Id": pfid, "Type": "title_player_account",
                                                     "TypeString": "title_player_account"}},
                "PrivateInfo": {},
                "SteamInfo": {"SteamId": player["Login"].get("steam", ""), "SteamName": name},
            }
        if info_params.get("GetUserInventory"):
            payload["UserInventory"] = self._inventory(player)
        if info_params.get("GetUserVirtualCurrency"):
            payload["UserVirtualCurrency"] = player["VirtualCurrency"]
            payload["UserVirtualCurrencyRechargeTimes"] = {}
        if info_params.get("GetUserData"):
            payload["UserData"] = _kv(self._userdata(player), info_params.get("UserDataKeys"))
            payload["UserDataVersion"] = player["DataVersion"]
        if info_params.get("GetUserReadOnlyData"):
            payload["UserReadOnlyData"] = _kv(self._readonly(player), info_params.get("UserReadOnlyDataKeys"))
            payload["UserReadOnlyDataVersion"] = player["DataVersion"]
        if info_params.get("GetCharacterList"):
            payload["CharacterList"] = []
        if info_params.get("GetCharacterInventories"):
            payload["CharacterInventories"] = []
        if info_params.get("GetPlayerStatistics"):
            payload["PlayerStatistics"] = [{"StatisticName": k, "Value": v, "Version": 0}
                                           for k, v in player["Statistics"].items()]
        if info_params.get("GetTitleData"):
            td = self.store.title_data()
            keys = info_params.get("TitleDataKeys")
            payload["TitleData"] = {k: v for k, v in td.items() if not keys or k in keys}
        if info_params.get("GetPlayerProfile"):
            payload["PlayerProfile"] = self._profile(player)
        return {
            "SessionTicket": ticket, "PlayFabId": pfid, "NewlyCreated": created,
            "SettingsForUser": {"NeedsAttribution": False, "GatherDeviceInfo": False, "GatherFocusInfo": False},
            "LastLoginTime": player["LastLogin"],
            "InfoResultPayload": payload,
            "EntityToken": {"EntityToken": ticket, "TokenExpiration": "2099-01-01T00:00:00Z",
                            "Entity": {"Id": pfid, "Type": "title_player_account",
                                       "TypeString": "title_player_account"}},
            "TreatmentAssignment": {"Variants": [], "Variables": []},
        }

    def _profile(self, player):
        pfid = player["PlayFabId"]
        return {"PublisherId": pfid, "TitleId": "REVIVAL", "PlayerId": pfid,
                "DisplayName": player.get("DisplayName") or f"Commander-{pfid[:4]}",
                "AvatarUrl": player.get("AvatarUrl") or "",
                "Created": player["Created"], "LastLogin": player["LastLogin"]}

    def api_Client_LoginWithSteam(self, req, headers):
        given = str(req.get("SteamTicket", ""))
        sid = steam_id_from_ticket(given) or given[:32]
        token = given if given.startswith(self.GAME_LOGIN_PREFIX) else None    # a login file read by the mod's no-Steam part
        return self._sign_in("steam", sid, req, headers, token)

    def api_Client_LoginWithAndroidDeviceID(self, req, headers):
        # the Android game signs in with the phone's device id (one player per phone)
        device = str(req.get("AndroidDeviceId", "")).strip()
        if not device:
            return err("InvalidParams", "AndroidDeviceId is missing", 1000, 400)
        return self._sign_in("android", device, req, headers)

    def _sign_in(self, kind, ext_id, req, headers, token=None):
        """Every game sign-in ends here. With a game login (sent by the mod in X-Revival-Login) the
        player is the one linked to that site account; without one, the player this Steam account,
        phone or computer has always been - unless the server requires a site account
        ("requireAccount": true in revival-settings.json)."""
        token = headers.get("X-Revival-Login") or token
        info = req.get("InfoRequestParameters")
        photon_emu.REQUIRE[0] = self._settings().get("requireAccount") is True
        if token:
            return self._sign_in_account(token, kind, ext_id, headers, info)
        if self._settings().get("requireAccount") is True:
            return err("AccountRequired", "This server needs you to sign in with an account from its website.", 1001, 401)
        if self.store.player_id_for(kind, ext_id) in self._banned_ids():
            return self._banned_error()
        if not self._may_create(kind, ext_id, headers):
            return self._too_many_new()
        p, t, c = self.store.get_or_create(kind, ext_id)
        return ok(self._login_result(p, t, c, info))

    def _sign_in_account(self, token, kind, ext_id, headers, info):
        users = self._creator_users()
        key, user = self._game_login_user(users, token)
        if user is None:
            return err("InvalidGameLoginFile", "Your saved sign-in is no longer valid. Sign in again.", 1001, 401)
        access = self._creator_perms(user)["access"]
        if access == "banned":
            return self._banned_error()
        if access == "requested":
            return err("AccountNotApproved", "Your account is waiting for an admin to approve it.", 1002, 403)
        pfid = user.get("player")
        created = False
        if not pfid or self.store.load(pfid) is None:
            # The account lost its link (an older unlink, or an admin): its own player comes back.
            with self.store.lock:
                users = self._creator_users()
                found = self._account_player(users, key, users.get(key, user))
                if found and key in users:
                    users[key]["player"], users[key]["playerLinkedAt"] = found, now_iso()
                    self._creator_users_save(users)
                    pfid = found
                    log.info("site account %s plays as its own player %s again", user["name"], pfid)
        if not pfid or self.store.load(pfid) is None:
            # First game sign-in for this account: it gets a game player of its own, made from the
            # account alone - never from the Steam account, phone or computer it signs in from.
            if not self._allow("new-account", self._address(headers), self.NEW_ACCOUNTS_PER_HOUR, 3600):
                return self._too_many_new()
            p, _, created = self.store.get_or_create("account", uuid.uuid4().hex)
            pfid = p["PlayFabId"]
            with self.store.lock:
                users = self._creator_users()
                if key in users:
                    users[key]["player"], users[key]["playerLinkedAt"] = pfid, now_iso()
                    self._creator_users_save(users)
                p = self.store.load(pfid) or p
                p["Login"] = {"account": users[key]["name"] if key in users else user["name"]}
                self.store.save(p)
            log.info("site account %s now plays as %s", user["name"], pfid)
        if pfid in self._banned_ids():
            return self._banned_error()
        p, t = self.store.login(pfid)
        if p is None:
            return err("AccountNotFound", "The game account for this sign-in is missing.", 1001, 404)
        return ok(self._login_result(p, t, created, info))

    # New game accounts: a player makes one once, so more than this from one address in an hour is
    # someone making accounts in bulk. Signing in to an account that already exists is never limited.
    NEW_ACCOUNTS_PER_HOUR = 5

    def _may_create(self, kind, ext_id, headers):
        if self.store.load(self.store.player_id_for(kind, ext_id)) is not None:
            return True
        return self._allow("new-account", self._address(headers), self.NEW_ACCOUNTS_PER_HOUR, 3600)

    @staticmethod
    def _too_many_new():
        return err("AccountCreationLimit", "Too many new accounts from your address. Try again in an hour.", 1189, 429)

    # The game signs in only with Steam (or the mod's stand-in for it) and with the Android device id.
    # PlayFab's other ways in (email, username, custom id) are refused: nothing uses them, and here
    # they would have made an account for any name sent, with no password.
    def _unused_login(self, req, headers):
        return err("NotSupported", "This server only signs in through the game.", 1000, 400)

    api_Client_LoginWithCustomID = _unused_login
    api_Client_LoginWithEmailAddress = _unused_login
    api_Client_LoginWithPlayFab = _unused_login

    # ------------------------------------------------------------------ data
    def api_Client_GetTitleData(self, req, headers):
        td = self.store.title_data()
        keys = req.get("Keys")
        return ok({"Data": {k: v for k, v in td.items() if not keys or k in keys}})

    def api_Client_GetUserData(self, req, headers):
        p = self._player(headers)
        return ok({"Data": _kv(self._userdata(p), req.get("Keys")), "DataVersion": p["DataVersion"]})

    def api_Client_GetUserReadOnlyData(self, req, headers):
        p = self._player(headers)
        return ok({"Data": _kv(self._readonly(p), req.get("Keys")), "DataVersion": p["DataVersion"]})

    # What one player may keep on the server. A normal player is about 15 KB; the limits only stop
    # a client from filling the disk (and slowing every lookup that reads all players).
    MAX_PLAYER_BYTES = 512 * 1024
    MAX_USER_DATA_KEYS = 300
    MAX_OBJECTS = 100

    def _too_big(self, p):
        """An error to answer with when a changed player would be over the limits (None when fine)."""
        if len(p.get("UserData") or {}) > self.MAX_USER_DATA_KEYS or len(p.get("Objects") or {}) > self.MAX_OBJECTS:
            return err("InvalidParams", "too much saved data for one player", 1000, 400)
        if len(json.dumps(p, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > self.MAX_PLAYER_BYTES:
            return err("InvalidParams", "too much saved data for one player", 1000, 400)
        return None

    def api_Client_UpdateUserData(self, req, headers):
        p = self._player(headers)
        with self.store.lock:
            p.update(self.store.load(p["PlayFabId"]) or {})
            for k, v in (req.get("Data") or {}).items():
                if v is None:
                    p["UserData"].pop(k, None)
                else:
                    p["UserData"][k] = v
            for k in req.get("KeysToRemove") or []:
                p["UserData"].pop(k, None)
            refused = self._too_big(p)
            if refused:
                log.warning("UpdateUserData refused for %s: over the size limits", p["PlayFabId"])
                return refused
            p["DataVersion"] += 1
            self.store.save(p)
        return ok({"DataVersion": p["DataVersion"]})

    def _inventory(self, player):
        """Player's own items plus, when content/collection.json says grantAll, the deck limit
        of every collectible card (the revival server gives everyone the full collection)."""
        items = list(player["Inventory"])
        path = os.path.join(self.content_root, "collection.json")
        try:
            mtime = os.path.getmtime(path)
            if getattr(self, "_coll_mtime", None) != mtime:
                with open(path, encoding="utf-8") as f:
                    self._coll = json.load(f)
                self._coll_mtime = mtime
        except OSError:
            self._coll = None
        if self._coll and self._coll.get("grantAll"):
            owned = {i.get("ItemId") for i in items}
            for c in self._coll["items"]:
                if c["id"] in owned:
                    continue
                items.append({"ItemId": c["id"], "ItemInstanceId": "RV-" + c["id"], "ItemClass": c["class"],
                              "CatalogVersion": "Warpforge", "DisplayName": c.get("name", c["id"]),
                              "RemainingUses": c["copies"], "UnitPrice": 0,
                              "PurchaseDate": player.get("Created")})
        return items

    def api_Client_GetUserInventory(self, req, headers):
        p = self._player(headers)
        return ok({"Inventory": self._inventory(p), "VirtualCurrency": p["VirtualCurrency"],
                   "VirtualCurrencyRechargeTimes": {}})

    def api_Client_GetPlayerProfile(self, req, headers):
        return ok({"PlayerProfile": self._profile(self._player(headers))})

    def api_Client_UpdateUserTitleDisplayName(self, req, headers):
        p = self._player(headers)
        problem = self._set_display_name(p, req.get("DisplayName"))
        if problem:
            return err(*problem)
        return ok({"DisplayName": p["DisplayName"]})

    # ------------------------------------------------------------------ names, friend codes, friends
    @staticmethod
    def friend_code(pfid):
        """Short code a player can give to others: the first 8 characters of their id, as XXXX-XXXX."""
        return f"{pfid[:4]}-{pfid[4:8]}".upper()

    def _all_players(self):
        for name in os.listdir(self.store.players_dir):
            if name.endswith(".json"):
                p = self.store.load(name[:-5])
                if p:
                    yield p

    def _find_player(self, query):
        """Find a player by display name (any capitalisation), friend code or id."""
        q = (query or "").strip()
        if not q:
            return None
        key = q.replace("-", "").replace(" ", "").upper()
        by_code = None
        # from the kept names (E3): only the player found is read in full
        for pfid, name in self.store.names():
            if name == q.lower() or pfid == key:
                p = self.store.load(pfid)
                if p:
                    return p
            if len(key) == 8 and pfid.startswith(key):
                by_code = pfid
        return self.store.load(by_code) if by_code else None

    def _set_display_name(self, player, name):
        """Returns None on success, or (error, message, code) for a PlayFab-style error."""
        name = " ".join((name or "").split())
        if not 3 <= len(name) <= 20:
            return ("InvalidParams", "Display name must be 3-20 characters long", 1000)
        if self._word_filter().found(name):
            return ("ProfaneDisplayName", "That name contains a word that is not allowed here", 1135)
        other = self._find_player(name)
        if other and other["PlayFabId"] != player["PlayFabId"]:
            return ("NameNotAvailable", "That name is already taken", 1058)
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            player["DisplayName"] = name
            self.store.save(player)
        return None

    def api_Client_GetAccountInfo(self, req, headers):
        me = self._player(headers)
        query = req.get("TitleDisplayName") or req.get("PlayFabId") or req.get("Username") or req.get("Email")
        p = self._find_player(query) if query else me
        if p is None:
            return err("AccountNotFound", "No player with that name or friend code", 1001)
        name = p.get("DisplayName") or f"Commander-{p['PlayFabId'][:4]}"
        return ok({"AccountInfo": {"PlayFabId": p["PlayFabId"], "Created": p["Created"], "Username": name,
                                   "TitleInfo": {"DisplayName": name, "Origination": "Steam",
                                                 "Created": p["Created"], "LastLogin": p.get("LastLogin"),
                                                 "isBanned": False}}})

    def _friend_ids(self, pfid):
        """The ids on a player's friend list, cached until their player file changes (review #19:
        every chat poll asks for each friend's list, which would otherwise read every file every 3 s)."""
        try:
            stamp = os.stat(self.store._path(pfid)).st_mtime_ns
        except (OSError, ValueError):
            return frozenset()
        cache = self.__dict__.setdefault("_friend_ids_cache", {})
        hit = cache.get(pfid)
        if hit and hit[0] == stamp:
            return hit[1]
        ids = frozenset(f.get("id") for f in self._friends(self.store.load(pfid) or {}) if isinstance(f, dict) and isinstance(f.get("id"), str))
        if len(cache) > 5000:
            cache.clear()
        cache[pfid] = (stamp, ids)
        return ids

    def _friends(self, player):
        try:
            return (json.loads(player.get("UserData", {}).get("FriendsData") or "{}").get("friendList")) or []
        except ValueError:
            return []

    def _save_friends(self, player, friends):
        player.setdefault("UserData", {})["FriendsData"] = json.dumps({"friendList": friends}, separators=(",", ":"))
        player["DataVersion"] = player.get("DataVersion", 0) + 1
        self.store.save(player)

    def _fresh_friends(self, player):
        """The saved friend list with each friend's current name (names can change)."""
        out = []
        for f in self._friends(player):
            other = self.store.load(f.get("id") or "")
            if other:
                out.append({"name": other.get("DisplayName") or f.get("name") or "", "id": other["PlayFabId"]})
        return out

    @staticmethod
    def _cs_args(params):
        """The game wraps what it sends as {SessionId, Version, Override, Data}; the arguments are in Data."""
        if isinstance(params, dict) and "Data" in params and "SessionId" in params:
            return params.get("Data")
        return params

    def cs_AddFriend(self, player, params):
        """Adds a friend by id (the game looks the id up from a name or friend code first). Only to
        this player's own list: the other player sees them only once they add them back."""
        params = self._cs_args(params)
        wanted = [v for v in (params or {}).values() if isinstance(v, str)] if isinstance(params, dict) else [str(params or "")]
        other = next((p for p in (self._find_player(w) for w in wanted) if p), None)
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            mine = self._fresh_friends(player)
            before = json.dumps(self._friends(player), sort_keys=True)
            if other and other["PlayFabId"] != player["PlayFabId"]:
                if all(f["id"] != other["PlayFabId"] for f in mine):
                    mine.append({"name": other.get("DisplayName") or "", "id": other["PlayFabId"]})
            if json.dumps(mine, sort_keys=True) != before:      # the game also polls this to refresh its list
                self._save_friends(player, mine)
        return mine

    def cs_ChangePlayerName(self, player, params):
        params = self._cs_args(params)
        name = next((v for v in (params or {}).values() if isinstance(v, str)), "") if isinstance(params, dict) else str(params or "")
        problem = self._set_display_name(player, name)
        if problem:
            return {"error": problem[0], "errorMessage": problem[1]}
        return {"DisplayName": player["DisplayName"], "newName": player["DisplayName"]}

    def api_Client_UpdateAvatarUrl(self, req, headers):
        """The game packs the chosen avatar and border ids into this field."""
        p = self._player(headers)
        with self.store.lock:
            p.update(self.store.load(p["PlayFabId"]) or {})
            p["AvatarUrl"] = req.get("ImageUrl") or ""
            self.store.save(p)
        return ok({})

    def cs_GetPlayerProfile(self, player, params):
        """Another player's profile window: {"playerId", "botIndex"} -> the game's PlayerInfo."""
        data = self._cs_args(params) or {}
        wanted = data.get("playerId") if isinstance(data, dict) else None
        other = self.store.load(wanted) if wanted else None
        if not other:
            other = self._find_player(wanted or "") or player
        avatar, _, border = (other.get("AvatarUrl") or "").partition("#")
        try:
            title = json.loads(other.get("UserData", {}).get("DemoData") or "{}").get("playerTitle") or ""
        except ValueError:
            title = ""
        return {"IsPlayer": other["PlayFabId"] == player["PlayFabId"], "IsBot": False,
                "PlayerName": other.get("DisplayName") or "", "PlayfabId": other["PlayFabId"],
                "PlayerAvatarID": avatar, "PlayerAvatarBorderID": border, "PlayerTitleID": title,
                "CurrentEloRating": 0, "LegendaryEloRating": 0, "HighestEloRating": 0,
                "HighestEloSeason": 0, "HighestEloSeasonFast": 0,
                "Logs": self._battle_log(other), "VictoryCounters": self._victory_counters(other),
                "SeasonPositions": {}, "SeasonPositionsFast": {},
                "HasBeenInvited": False, "CurrentSeason": 1, "PlayerLevel": 1}

    def cs_GetLeaderboard(self, player, params):
        """No rankings are kept yet: every board is empty and the player's own score is 0."""
        name = player.get("DisplayName") or ""
        return {"total": 0, "totalArmyScore": {},
                "currentEntry": {"entryId": player["PlayFabId"], "value": 0, "position": 0, "name": name,
                                 "avatarId": "", "groupName": "", "groupId": "", "armyScores": {}, "highestScores": {}},
                "entries": [], "needUpdate": False, "fetchTime": int(time.time() * 1000)}

    def cs_changeCosmetics(self, player, params):
        """Chosen alliance badge / card back / warcry. Kept in the player's read-only data."""
        params = params if isinstance(params, dict) else {}
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            ro = player.setdefault("ReadOnlyData", {})
            try:
                data = json.loads(ro.get("CosmeticData") or "{}")
            except ValueError:
                data = {}
            for k, v in params.items():
                kl = k.lower()
                for field, word in (("currentBadge", "badge"), ("currentCardback", "cardback"), ("currentWarcry", "warcry")):
                    if word in kl and isinstance(v, str):
                        data[field] = v
            ro["CosmeticData"] = json.dumps(data, separators=(",", ":"))
            player["DataVersion"] = player.get("DataVersion", 0) + 1
            self.store.save(player)
        return {"CosmeticData": data}

    def api_Client_GetPlayerStatistics(self, req, headers):
        p = self._player(headers)
        return ok({"Statistics": [{"StatisticName": k, "Value": v, "Version": 0} for k, v in p["Statistics"].items()]})

    def api_Client_GetTime(self, req, headers):
        return ok({"Time": now_iso()})

    def api_Client_GetCatalogItems(self, req, headers):
        return ok({"Catalog": []})

    def api_Client_GetStoreItems(self, req, headers):
        return ok({"Store": [], "StoreId": req.get("StoreId"), "CatalogVersion": req.get("CatalogVersion")})

    def api_Client_GetFriendsList(self, req, headers):
        return ok({"Friends": []})

    def api_Revival_MatchTicket(self, req, headers):
        """A ticket the mod hands to the match service, which cannot see the game's own sign-in."""
        p = self._player(headers)
        photon_emu.REQUIRE[0] = self._settings().get("requireAccount") is True
        grant = getattr(photon_emu, "grant", None)
        if grant is None:                     # the match service part is older (restart the server)
            return err("NotAvailable", "restart the server to use match tickets", 1000, 503)
        return ok({"ticket": grant(p["PlayFabId"]), "seconds": getattr(photon_emu, "TICKET_SECONDS", 1800)})

    def api_Client_GetPhotonAuthenticationToken(self, req, headers):
        return ok({"PhotonCustomAuthenticationToken": uuid.uuid4().hex})

    def api_Client_WritePlayerEvent(self, req, headers):
        return ok({"EventId": uuid.uuid4().hex})

    def api_Client_WriteTitleEvent(self, req, headers):
        return ok({"EventId": uuid.uuid4().hex})

    def api_Event_WriteEvents(self, req, headers):
        return ok({"AssignedEventIds": [uuid.uuid4().hex for _ in req.get("Events", [])]})

    def api_Event_WriteTelemetryEvents(self, req, headers):
        return self.api_Event_WriteEvents(req, headers)

    # ------------------------------------------------------------------ entity objects
    def api_Object_GetObjects(self, req, headers):
        p = self._player(headers)
        objs = p.get("Objects", {})
        return ok({"Entity": req.get("Entity"), "ProfileVersion": p["DataVersion"],
                   "Objects": {k: {"ObjectName": k, "DataObject": v} for k, v in objs.items()}})

    def api_Object_SetObjects(self, req, headers):
        p = self._player(headers)
        with self.store.lock:
            p.update(self.store.load(p["PlayFabId"]) or {})
            objs = p.setdefault("Objects", {})
            for o in req.get("Objects") or []:
                if o.get("DeleteObject"):
                    objs.pop(o.get("ObjectName"), None)
                else:
                    objs[o.get("ObjectName")] = o.get("DataObject")
            refused = self._too_big(p)
            if refused:
                log.warning("SetObjects refused for %s: over the size limits", p["PlayFabId"])
                return refused
            p["DataVersion"] += 1
            self.store.save(p)
        return ok({"ProfileVersion": p["DataVersion"], "SetResults": []})

    # ------------------------------------------------------------------ cloud script
    def _fn_data(self, params):
        return (params or {}).get("Data") or {}

    # A LiveOps handler name arrives from the game and becomes a file name under content/liveops,
    # so only a plain name is accepted: no path separators, no "..", no drive or network path.
    HANDLER_NAME = re.compile(r"[A-Za-z0-9_-]{1,40}")
    MAX_HANDLERS = 64

    def _liveops_handler(self, player, handler):
        """Events for one LiveOps handler: content/liveops/<Handler>.json holds {"SharedData", "RawEvents":[{"Data"}]};
        each player's progress for an event ("Persistence") is kept in their save."""
        if not isinstance(handler, str) or not self.HANDLER_NAME.fullmatch(handler):
            return {"SharedData": {}, "RawEvents": []}
        path = os.path.join(self.content_root, "liveops", handler + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except OSError:
            return {"SharedData": {}, "RawEvents": []}
        except ValueError as e:
            log.warning("liveops/%s.json is not valid JSON: %s", handler, e)
            return {"SharedData": {}, "RawEvents": []}
        saves = player.setdefault("LiveOps", {}).setdefault(handler, {})
        events = []
        for ev in cfg.get("RawEvents", []):
            eid = ev["Data"].get("eventId", "")
            pers = dict({"eventId": eid, "IsNew": True, "WasSeen": False, "IsActive": True,
                         "triggerTime": 0, "expiredTime": 0}, **ev.get("Persistence", {}))
            pers.update(saves.get(eid, {}))
            events.append({"Data": ev["Data"], "Persistence": pers})
        return {"SharedData": cfg.get("SharedData", {}), "RawEvents": events}

    SERVER_OWNED_KEYS = ("PlayerDecks", "PlayerDecksFast", "BattleLogData")

    def _readonly(self, player):
        """Read-only player data as the game sees it. Data written by server-side functions (decks)
        is read-only data on the original backend, so expose those keys there."""
        out = dict(player.get("ReadOnlyData") or {})
        for k in self.SERVER_OWNED_KEYS:
            if k in player.get("UserData", {}) and k not in out:
                out[k] = self._deck_list(player["UserData"][k])
        if "VictoryCounters" in player.get("UserData", {}) and "VictoryCounters" not in out:
            out["VictoryCounters"] = player["UserData"]["VictoryCounters"]
        return out

    def _userdata(self, player):
        out = dict(player.get("UserData") or {})
        if "FriendsData" in out:
            out["FriendsData"] = json.dumps({"friendList": self._fresh_friends(player)}, separators=(",", ":"))
        for k in self.SERVER_OWNED_KEYS:
            if k in out:
                out[k] = self._deck_list(out[k])
        return out

    @staticmethod
    def _deck_list(raw):
        """Saved decks as the JSON list the game expects (older saves stored {deckId: deck})."""
        try:
            decks = json.loads(raw or "[]")
        except ValueError:
            return "[]"
        if isinstance(decks, dict):
            decks = list(decks.values())
        return json.dumps(decks, separators=(",", ":"))

    def cs_match_finished(self, player, params):
        """End of a match. The game needs a result block back before it shows the result screen.
        Wins / losses per event are remembered in the player's save; no ratings or rewards yet."""
        data = self._fn_data(params)
        event_id = data.get("eventId") or "unknown"
        won = bool(data.get("hasWon"))
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            rec = player.setdefault("MatchRecord", {}).setdefault(event_id, {"Won": 0, "Loss": 0, "Draw": 0})
            rec["Won" if won else "Loss"] += 1
            self.store.save(player)
        return {
            "EndMatchData": {"XP": 0, "NewRating": 0, "EarnedRating": 0, "EarnedSeals": 0, "NewOpponentRating": 0,
                             "ArmyScores": {}, "HighestScores": {}, "TotalScores": {}, "ArmyStats": {},
                             "Rewards": [], "RatingBoostState": []},
            "BattleLogData": [],
            "VictoryCounters": {},
        }

    # ---- battle log: the match history list in the game's profile ----
    # Kept in the player's data under BattleLogData as the list of entries the game reads at login;
    # match results and pin changes send the fresh list back so the screen updates without a restart.
    BATTLE_LOG_KEEP = 30                              # unpinned matches kept; pinned ones are never dropped
    MATCH_TYPES = (("unranked", 170, 180), ("fastmode", 200, 210), ("ranked", 10, 20), ("draft", 110, 120),
                   ("event", 80, 80), ("practice", 90, 90))      # word in the function name -> type vs player, vs AI

    @staticmethod
    def _battle_log(player):
        try:
            entries = json.loads(player.get("UserData", {}).get("BattleLogData") or "[]")
        except ValueError:
            entries = []
        return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []

    @staticmethod
    def _victory_counters(player):
        """{warlord card id: matches won with it}."""
        try:
            wins = json.loads(player.get("UserData", {}).get("VictoryCounters") or "{}")
        except ValueError:
            wins = {}
        return {k: v for k, v in wins.items() if isinstance(v, int) and not isinstance(v, bool)} if isinstance(wins, dict) else {}

    def _battle_log_save(self, player, entries):
        player.setdefault("UserData", {})["BattleLogData"] = json.dumps(entries, separators=(",", ":"))
        player["DataVersion"] = player.get("DataVersion", 0) + 1
        self.store.save(player)

    def _battle_log_add(self, player, fn, data):
        """Add the match the game just reported. Returns the new list, or None when the report
        does not say who was fought (nothing worth listing)."""
        text = lambda *keys: next((str(data[k]) for k in keys if isinstance(data.get(k), (str, int)) and str(data[k]).strip()), "")
        num = lambda *keys: next((int(data[k]) for k in keys if isinstance(data.get(k), (int, float)) and not isinstance(data.get(k), bool)), 0)
        own_hero = text("playerHeroId", "ownHeroId", "heroId")
        enemy_hero = text("opponentHeroId", "enemyHeroId") or own_hero
        if not own_hero:
            return None
        enemy_id = text("opponentID", "opponentId", "enemyId") or "AI"
        won = data.get("hasWon")
        draw = data.get("isDraw") is True or won in (-1, 2)
        lower = fn.lower()
        versus_ai = enemy_id == "AI"
        match_type = next((ai if versus_ai else human for word, human, ai in self.MATCH_TYPES if word in lower), 90)
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            enemy_name = text("opponentName", "enemyName")
            if not versus_ai:
                enemy_name = (self.store.load(enemy_id) or {}).get("DisplayName") or enemy_name
            entry = {"season": 0, "enemyId": enemy_id, "matchWin": bool(won) and not draw, "matchLose": not bool(won) and not draw,
                     "ownExp": 0, "enemyExp": 0, "ownTotalExp": 0, "enemyTotalExp": 0, "ownGlobalRating": 0, "enemyGlobalRating": 0,
                     "ownHeroName": own_hero, "enemyHeroName": enemy_hero,
                     "ownName": player.get("DisplayName") or text("ownName"), "enemyName": enemy_name,
                     "ownSkulls": max(0, num("milestone")), "enemySkulls": max(0, num("enemymilestone", "enemyMilestone")),
                     # a unique id so a match can be pinned; there is no recording behind it
                     "recordingIndex": uuid.uuid4().hex[:16], "matchType": match_type,
                     "replaysVersion": text("ReplaysVersion") or self._replays_version(), "pinned": False,
                     "playerClan": "", "enemyClan": "", "botAltid": ""}
            # skulls taken, per mode (the profile's record panel adds them up)
            rec = player.setdefault("MatchRecord", {}).setdefault(text("eventId") or "unknown", {"Won": 0, "Loss": 0, "Draw": 0})
            rec["Skulls"] = rec.get("Skulls", 0) + entry["ownSkulls"]
            if entry["matchWin"]:
                # wins per warlord: the profile's warlord mastery counts these
                wins = self._victory_counters(player)
                wins[own_hero] = wins.get(own_hero, 0) + 1
                player.setdefault("UserData", {})["VictoryCounters"] = json.dumps(wins, separators=(",", ":"))
            entries = [entry] + self._battle_log(player)              # newest first
            # the relay's own record of this match (vs players only), for reviews and replay codes
            if not versus_ai and hasattr(photon_emu, "find_match"):
                found = photon_emu.find_match([player["PlayFabId"], enemy_id])
                if found:
                    player.setdefault("MatchLinks", {})[entry["recordingIndex"]] = found
            kept, free = [], self.BATTLE_LOG_KEEP
            for e in entries:
                if e.get("pinned"):
                    kept.append(e)
                elif free > 0:
                    kept.append(e)
                    free -= 1
            links = player.get("MatchLinks")
            if isinstance(links, dict):
                live = {str(e.get("recordingIndex")) for e in kept}
                player["MatchLinks"] = {k: v for k, v in links.items() if k in live}
            self._battle_log_save(player, kept)
            self._replays_prune(player["PlayFabId"], kept)
        return kept

    STAT_MODES = (("Classic", ("RevivalClassic",)), ("Skirmish", ("RevivalSkirmish",)), ("Draft", ("RevivalDraft",)),
                  ("Custom Test", ("RevivalLongGame",)), ("Practice", ("PracticeEvent", "PracticeEventClassic")))

    def api_Revival_PlayerStats(self, req, headers):
        """Win / loss record and skulls for the profile page: {"playerId"} (default: the caller)."""
        me = self.store.from_session(headers)
        if me is None:
            return err("NotAuthorized", "sign in to the game first")
        wanted = str(req.get("playerId") or me["PlayFabId"])
        with self.store.lock:
            player = self.store.load(wanted) if re.fullmatch(r"[0-9A-Fa-f]{16}", wanted) else None
        if not player:
            return err("NotFound", "no such player")
        record = {k: v for k, v in (player.get("MatchRecord") or {}).items() if isinstance(v, dict)}
        num = lambda rec, k: rec[k] if isinstance(rec.get(k), int) and not isinstance(rec.get(k), bool) else 0
        row = lambda name, recs: {"name": name, "won": sum(num(r, "Won") for r in recs), "lost": sum(num(r, "Loss") for r in recs),
                                  "draw": sum(num(r, "Draw") for r in recs), "skulls": sum(num(r, "Skulls") for r in recs)}
        named = {e for _, ids in self.STAT_MODES for e in ids}
        modes = [row(name, [record[e] for e in ids if e in record]) for name, ids in self.STAT_MODES]
        modes.append(row("Other", [r for e, r in record.items() if e not in named]))
        modes = [m for m in modes if m["won"] + m["lost"] + m["draw"] > 0]
        return ok({"playerId": player["PlayFabId"], "total": row("Overall", list(record.values())), "modes": modes})

    # ---------------------------------------------------------------- replays
    # After a match the game sends its recording (shuffle values, both decks, mulligans and every
    # action) under the recordingIndex of the match-history entry the server just made for it.
    # Recordings are kept one file each in data/replays/<player id>/, only for matches in that
    # player's history, and are deleted with the entry when it drops off the list. Each is stamped
    # with revival-settings.json "rulesVersion": a replay only re-runs the moves, so after a rules
    # change old recordings would play out wrongly - bumping rulesVersion retires them.
    # Since mod 0.12.43 the game also sends the rules the match was played under ("revivalRules":
    # the mode, its gameplay values, starting hands, warlord health multiplier, turn timer, plus the
    # mod's rules number and the card pack version). A recording kept with those rules is played
    # back under them, so it no longer depends on rulesVersion; the mod itself refuses it when its
    # rules number or card pack differ (cards are not part of the rules).
    REPLAY_MAX_BYTES = 256 * 1024
    PINNED_MAX = 20                                   # pinned matches per player (review #16)
    REPLAY_PLAYER_MAX_BYTES = 10 * 1024 * 1024        # all of a player's recordings together

    def _replay_room(self, pfid, name, size, entries):
        """Make room for a new recording of `size` bytes within the player's byte cap, dropping the oldest
        recordings of unpinned matches first. False if it still does not fit."""
        d = self._replay_dir(pfid)
        pinned = {str(e.get("recordingIndex")) for e in entries if e.get("pinned")}
        try:
            files = [(n, os.stat(os.path.join(d, n))) for n in os.listdir(d) if n.endswith(".json") and n[:-5] != name]
        except OSError:
            return True
        used = sum(st.st_size for _, st in files)
        for n, st in sorted(files, key=lambda f: f[1].st_mtime):
            if used + size <= self.REPLAY_PLAYER_MAX_BYTES:
                break
            if n[:-5] in pinned:
                continue
            try:
                os.remove(os.path.join(d, n))
                used -= st.st_size
            except OSError:
                pass
        return used + size <= self.REPLAY_PLAYER_MAX_BYTES
    RECORD_NAME = re.compile(r"[0-9a-f]{16}")

    # The rules sent with a recording, in exactly the shape the mod writes them; anything else is dropped
    # (the recording is still kept, without them). Ranges are wide enough for every mode's own values.
    REPLAY_RULE_RANGES = {"warlordHealthMultiplier": (10, 30), "warlordLifeChange": (-100, 100), "startingHand": (0, 20),
                          "secondPlayerExtraCards": (0, 10), "drawCardsPerTurn": (0, 10), "handLimit": (1, 30),
                          "startingMana": (0, 20), "startingManaSecond": (0, 20), "manaPerTurn": (0, 10),
                          "overtimeTurn": (0, 200), "turnSeconds": (0, 600)}
    # from mod 0.13.24, sent together: whether the Offence card pick ran, and whether a challenge's rules
    # forced who went first (replays from older mods have neither)
    # and from 0.13.33 the unspent mana carried over to the next turn; any of these may be missing
    REPLAY_RULE_OPTIONAL = {"offenseCards": (0, 1), "forcedFirst": (0, 1), "manaAccumulation": (0, 10)}
    REPLAY_RULE_EVENT = re.compile(r"[A-Za-z0-9_.-]{1,64}")
    REPLAY_RULE_CARDS = re.compile(r"[A-Za-z0-9._-]{1,80}")

    def _replay_rules_snapshot(self, raw):
        """{"v": 1, "event", "mod", "cards", "rules": {...}} as sent with a recording, or None."""
        whole = lambda x: isinstance(x, int) and not isinstance(x, bool)
        if not isinstance(raw, dict) or set(raw) != {"v", "event", "mod", "cards", "rules"} or raw["v"] != 1 or not whole(raw["v"]):
            return None
        if not isinstance(raw["event"], str) or not self.REPLAY_RULE_EVENT.fullmatch(raw["event"]):
            return None
        if not whole(raw["mod"]) or not 0 <= raw["mod"] <= 10000:
            return None
        if not isinstance(raw["cards"], str) or not self.REPLAY_RULE_CARDS.fullmatch(raw["cards"]):
            return None
        rules = raw["rules"]
        ranges = dict(self.REPLAY_RULE_RANGES, **self.REPLAY_RULE_OPTIONAL)
        if not isinstance(rules, dict) or not set(self.REPLAY_RULE_RANGES) <= set(rules) <= set(ranges):
            return None
        for k, (lo, hi) in ranges.items():
            if k in rules and (not whole(rules[k]) or not lo <= rules[k] <= hi):
                return None
        return {"v": 1, "event": raw["event"], "mod": raw["mod"], "cards": raw["cards"],
                "rules": {k: rules[k] for k in ranges if k in rules}}

    def _replay_dir(self, pfid):
        return os.path.join(self.store.root, "replays", pfid)

    def _replay_rules(self):
        return str(self._settings().get("rulesVersion", ""))

    def _replays_prune(self, pfid, entries):
        """Delete recordings whose match is no longer in the player's history."""
        keep = {str(e.get("recordingIndex")) for e in entries}
        d = self._replay_dir(pfid)
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            if name.endswith(".json") and name[:-5] not in keep:
                try:
                    os.remove(os.path.join(d, name))
                except OSError:
                    pass

    def cs_saveMatchRecording(self, player, params):
        """{"recordName", "battleRecord", "cancelled"}: keep the recording of a match in this
        player's history. Answers with the history, as before."""
        data = self._fn_data(params)
        name = str(data.get("recordName") or "")
        record = data.get("battleRecord")
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            entries = self._battle_log(player)
            if self.RECORD_NAME.fullmatch(name) and isinstance(record, dict) \
                    and any(str(e.get("recordingIndex")) == name for e in entries):
                kept = {"rules": self._replay_rules(), "record": record}
                if "revivalRules" in data:
                    snapshot = self._replay_rules_snapshot(data.get("revivalRules"))
                    if snapshot is None:
                        log.warning("replay %s of %s: the rules sent with it are not in the expected form; kept without them",
                                    name, player["PlayFabId"])
                    else:
                        kept["revivalRules"] = snapshot
                body = json.dumps(kept, separators=(",", ":"))
                if len(body) > self.REPLAY_MAX_BYTES:
                    log.warning("replay %s of %s not kept: %d bytes", name, player["PlayFabId"], len(body))
                elif not self._replay_room(player["PlayFabId"], name, len(body), entries):
                    log.warning("replay %s of %s not kept: the player's recordings are full", name, player["PlayFabId"])
                else:
                    d = self._replay_dir(player["PlayFabId"])
                    os.makedirs(d, exist_ok=True)
                    path = os.path.join(d, name + ".json")
                    with open(path + ".tmp", "w", encoding="utf-8") as f:
                        f.write(body)
                    os.replace(path + ".tmp", path)
            self._replays_prune(player["PlayFabId"], entries)
        return {"BattleLogData": entries}

    def cs_recoverMatchRecording(self, player, params):
        """{"recordName", "targetPlayerId"}: a recording for the replay screen, from this player's
        history or (from a profile) another player's. Empty when there is none, which the game
        shows as "replay cannot be loaded"."""
        data = self._fn_data(params)
        name = str(data.get("recordName") or "")
        if not self.RECORD_NAME.fullmatch(name):
            # a replay code (mod 0.13.35): the copy kept with the code, whoever's history it came from
            code = self._code_norm(name)
            if not code or not self._allow("replaylookup", player["PlayFabId"], 120, 3600):
                return {}
            shared = self._code_load(code)
            kept = shared.get("kept") if shared else None
            if not isinstance(kept, dict):
                return {}
            return self._recording_reply(kept)
        owner = str(data.get("targetPlayerId") or data.get("targetPlayerID") or player["PlayFabId"])
        if not re.fullmatch(r"[0-9A-F]{16}", owner):
            return {}
        try:
            with open(os.path.join(self._replay_dir(owner), name + ".json"), encoding="utf-8") as f:
                kept = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(kept, dict):
            return {}
        return self._recording_reply(kept)

    def _recording_reply(self, kept):
        """What recoverMatchRecording answers for a kept recording."""
        snapshot = self._replay_rules_snapshot(kept.get("revivalRules")) if "revivalRules" in kept else None
        if snapshot is not None:
            # played back under its own rules; the mod checks its rules number and card pack
            return {"recordingData": kept.get("record"), "revivalRules": snapshot}
        if str(kept.get("rules", "")) != self._replay_rules():
            return {}
        return {"recordingData": kept.get("record")}

    # ---------------------------------------------------------------- match records and replay codes
    # The match service writes every match it relays to data/matches/<YYYY-MM>/<id>.jsonl (see
    # photon_emu, "match records"). They are deleted after MATCH_KEEP_DAYS unless something holds them:
    # a replay code made from that match. A history entry knows its record through the player's
    # MatchLinks ({recordingIndex: record id}).
    #
    # A replay code (mod 0.13.35) shares one recording: the owner asks for one in the battle log, gets
    # e.g. "K7M2-QX9P", and anyone can type it in Social > Challenge to watch. The recording is copied
    # aside with the code (data/replay_codes/<CODE>.json), so it stays when the match drops off the
    # owner's history. The game fetches it through its own recoverMatchRecording with the code as the
    # record name. Codes use Crockford's base 32 (no I, L, O, U); typing is forgiving (dashes, spaces,
    # lower case, O for 0, I or L for 1).
    MATCH_KEEP_DAYS = 30
    CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    REPLAY_CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{8}")
    CODES_PER_PLAYER = 200

    def _matches_dir(self):
        return os.path.join(self.store.root, "matches")

    def _codes_dir(self):
        return os.path.join(self.store.root, "replay_codes")

    @classmethod
    def _code_norm(cls, text):
        """The code in its stored form (8 characters), or "" when the text is not one."""
        t = re.sub(r"[\s-]", "", str(text or "")).upper()[:16]
        t = t.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))
        return t if cls.REPLAY_CODE.fullmatch(t) else ""

    @staticmethod
    def _code_show(code):
        return code[:4] + "-" + code[4:]

    def _code_load(self, code):
        if not self.REPLAY_CODE.fullmatch(code or ""):
            return None
        try:
            with open(os.path.join(self._codes_dir(), code + ".json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _codes_all(self):
        """{code: its file's contents} for every replay code."""
        out = {}
        try:
            names = os.listdir(self._codes_dir())
        except OSError:
            return out
        for n in names:
            if n.endswith(".json") and self.REPLAY_CODE.fullmatch(n[:-5]):
                d = self._code_load(n[:-5])
                if d:
                    out[n[:-5]] = d
        return out

    def _code_public(self, code, d):
        """What anyone may see about a code: the match, not the recording."""
        info = d.get("info") if isinstance(d.get("info"), dict) else {}
        return {"code": self._code_show(code), "created": d.get("created") or "", "played": d.get("played") or "",
                "owner": d.get("ownerName") or "", **{k: info.get(k) for k in
                ("ownName", "enemyName", "ownHero", "enemyHero", "result", "matchType")}}

    def _matches_held(self):
        return {d.get("match") for d in self._codes_all().values() if isinstance(d.get("match"), str)}

    def _matches_prune(self):
        """Deletes match records past MATCH_KEEP_DAYS that nothing holds."""
        root = self._matches_dir()
        try:
            months = os.listdir(root)
        except OSError:
            return
        oldest = time.time() - self.MATCH_KEEP_DAYS * 86400
        held = None
        gone = 0
        for m in months:
            folder = os.path.join(root, m)
            if not re.fullmatch(r"\d{4}-\d{2}", m) or not os.path.isdir(folder):
                continue
            for n in os.listdir(folder):
                path = os.path.join(folder, n)
                if not n.endswith(".jsonl"):
                    continue
                try:
                    if os.stat(path).st_mtime >= oldest:
                        continue
                except OSError:
                    continue
                if held is None:
                    held = self._matches_held()
                if n[:-6] in held:
                    continue
                try:
                    os.remove(path)
                    gone += 1
                except OSError:
                    pass
            try:
                os.rmdir(folder)                      # only when empty
            except OSError:
                pass
        if gone:
            log.info("match records: %d older than %d days deleted", gone, self.MATCH_KEEP_DAYS)

    def api_Revival_ReplayCode(self, req, headers):
        """{"recordingIndex"} of a match in the caller's history -> its replay code (the same code each time)."""
        p = self.store.from_session(headers)
        if p is None:
            return err("NotAuthorized", "sign in to the game first")
        pfid = p["PlayFabId"]
        index = str(req.get("recordingIndex") or "")
        if not self.RECORD_NAME.fullmatch(index):
            return err("InvalidParams", "no such match")
        with self.store.lock:
            player = self.store.load(pfid) or p
            entry = next((e for e in self._battle_log(player) if str(e.get("recordingIndex")) == index), None)
            if entry is None:
                return err("NotFound", "that match is no longer in your history")
            mine = [(c, d) for c, d in self._codes_all().items() if d.get("owner") == pfid]
            for c, d in mine:
                if d.get("recordingIndex") == index:
                    return ok(self._code_public(c, d))
            path = os.path.join(self._replay_dir(pfid), index + ".json")
            try:
                with open(path, encoding="utf-8") as f:
                    kept = json.load(f)
                played = datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except (OSError, ValueError):
                return err("NotFound", "this match has no recording on the server to share")
            if not isinstance(kept, dict):
                return err("NotFound", "this match has no recording to share")
            if len(mine) >= self.CODES_PER_PLAYER:
                return err("TooMany", f"you have {self.CODES_PER_PLAYER} replay codes; remove some on the website's Profile page first", 1199, 429)
            if not self._allow("replaycode", pfid, 30, 3600):
                return err("TooManyAttempts", "too many replay codes - try again later", 1199, 429)
            won, lost = entry.get("matchWin") is True, entry.get("matchLose") is True
            info = {"ownName": str(entry.get("ownName") or "")[:40], "enemyName": str(entry.get("enemyName") or "")[:40],
                    "ownHero": str(entry.get("ownHeroName") or "")[:20], "enemyHero": str(entry.get("enemyHeroName") or "")[:20],
                    "result": "won" if won else "lost" if lost else "draw", "matchType": entry.get("matchType")}
            links = player.get("MatchLinks") if isinstance(player.get("MatchLinks"), dict) else {}
            os.makedirs(self._codes_dir(), exist_ok=True)
            for _ in range(20):
                code = "".join(self.CODE_ALPHABET[b % 32] for b in os.urandom(8))
                target = os.path.join(self._codes_dir(), code + ".json")
                if not os.path.exists(target):
                    break
            else:
                return err("ServiceUnavailable", "could not make a code, try again", 1123, 503)
            d = {"owner": pfid, "ownerName": str(player.get("DisplayName") or "")[:40], "recordingIndex": index,
                 "created": now_iso(), "played": played, "match": links.get(index), "info": info, "kept": kept}
            with open(target + ".tmp", "w", encoding="utf-8") as f:
                json.dump(d, f, separators=(",", ":"))
            os.replace(target + ".tmp", target)
            log.info("replay code %s made by %s for match %s", code, pfid, index)
        return ok(self._code_public(code, d))

    def api_Revival_CreatorReplayCodes(self, req, headers):
        """The site's Profile page: the replay codes of the signed-in account's game player."""
        me = self._creator_account(req)
        if not me or self._creator_perms(me)["access"] == "banned":
            return err("NotAuthorized", "sign in first")
        pfid = me.get("player")
        if not pfid:
            return ok({"codes": [], "linked": False})
        codes = [self._code_public(c, d) for c, d in self._codes_all().items() if d.get("owner") == pfid]
        codes.sort(key=lambda c: c["created"], reverse=True)
        return ok({"codes": codes, "linked": True, "max": self.CODES_PER_PLAYER})

    def api_Revival_CreatorReplayCodeLookup(self, req, headers):
        """{"code"} -> what the match was (for anyone signed in). Watching happens in the game."""
        me = self._creator_account(req)
        if not me or self._creator_perms(me)["access"] == "banned":
            return err("NotAuthorized", "sign in first")
        if not self._allow("codelookup", me.get("name") or "?", 120, 3600):
            return err("TooManyAttempts", "too many look-ups - try again later", 1199, 429)
        code = self._code_norm(req.get("code"))
        d = self._code_load(code) if code else None
        if not d:
            return err("NotFound", "no replay has that code")
        return ok(self._code_public(code, d))

    def api_Revival_CreatorReplayCodeRemove(self, req, headers):
        """{"code"}: the owner (through the linked game player) or an admin takes a code back."""
        me = self._creator_account(req)
        if not me:
            return err("NotAuthorized", "sign in first")
        perms = self._creator_perms(me)
        code = self._code_norm(req.get("code"))
        d = self._code_load(code) if code else None
        if not d:
            return err("NotFound", "no replay has that code")
        if not ((me.get("player") and d.get("owner") == me.get("player") and perms["access"] != "banned") or perms["admin"]):
            return err("NotAuthorized", "that is not your replay code")
        try:
            os.remove(os.path.join(self._codes_dir(), code + ".json"))
        except OSError:
            return err("ServiceUnavailable", "could not remove it, try again", 1123, 503)
        log.info("replay code %s removed by %s", code, me.get("name"))
        return ok({"removed": self._code_show(code)})

    def _replays_version(self):
        try:
            with open(os.path.join(self.content_root, "titledata", "core.json"), encoding="utf-8") as f:
                sv = json.load(f).get("ServerVersion") or {}
            if isinstance(sv, str):
                sv = json.loads(sv)
            return str(sv.get("replaysVersion") or "")
        except (OSError, ValueError, AttributeError):
            return ""

    def cs_battle_log_current(self, player, params):
        with self.store.lock:
            return {"BattleLogData": self._battle_log(self.store.load(player["PlayFabId"]) or player)}

    def cs_pinMatchRecording(self, player, params):
        """{"recordIndex", "pinned"}: keep a match in the list (or let it go again)."""
        data = self._fn_data(params)
        index, pinned = str(data.get("recordIndex") or ""), bool(data.get("pinned"))
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            entries = self._battle_log(player)
            changed = False
            if pinned and sum(1 for e in entries if e.get("pinned")) >= self.PINNED_MAX:
                return {"BattleLogData": entries}     # at most PINNED_MAX pinned matches; unpin one first
            for e in entries:
                if index and str(e.get("recordingIndex")) == index and bool(e.get("pinned")) != pinned:
                    e["pinned"], changed = pinned, True
            if changed:
                self._battle_log_save(player, entries)
        return {"BattleLogData": entries}

    def cs_updatePlayerDecks(self, player, params):
        """Save one deck. The game sends {"Deck": "<deck json>", "DeckId", "GameMode"}; decks are kept in the
        player's data under PlayerDecks (Classic) / PlayerDecksFast (Skirmish) as {deckId: deck}."""
        data = self._fn_data(params)
        deck_id = data.get("DeckId")
        deck = data.get("Deck")
        if isinstance(deck, str):
            try:
                deck = json.loads(deck)
            except ValueError:
                deck = None
        key = "PlayerDecksFast" if data.get("GameMode") == 13 else "PlayerDecks"
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            try:
                decks = json.loads(player["UserData"].get(key) or "[]")
            except ValueError:
                decks = []
            if isinstance(decks, dict):          # older saves kept {deckId: deck}
                decks = list(decks.values())
            decks = [d for d in decks if isinstance(d, dict) and d.get("id") != deck_id]
            if deck_id and deck:
                decks.append(deck)
            # the game reads this key as a list of decks
            player["UserData"][key] = json.dumps(decks, separators=(",", ":"))
            player["DataVersion"] = player.get("DataVersion", 0) + 1
            self.store.save(player)
        return {"DeckId": deck_id, "saved": bool(deck)}

    def cs_deletePlayerDeck(self, player, params):
        """Delete one deck. The game sends {"DeckId", "GameMode": "Classic" | "Skirmish"} and expects {"Result": true}."""
        data = self._fn_data(params)
        deck_id = data.get("DeckId")
        removed = False
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            for key in ("PlayerDecks", "PlayerDecksFast"):      # a deck id is unique, so look in both lists
                try:
                    decks = json.loads(player.get("UserData", {}).get(key) or "[]")
                except ValueError:
                    continue
                if isinstance(decks, dict):
                    decks = list(decks.values())
                kept = [d for d in decks if not (isinstance(d, dict) and d.get("id") == deck_id)]
                if len(kept) != len(decks):
                    player["UserData"][key] = json.dumps(kept, separators=(",", ":"))
                    removed = True
            if removed:
                player["DataVersion"] = player.get("DataVersion", 0) + 1
                self.store.save(player)
        return {"Result": True}      # already gone counts as deleted

    def cs_OrderPlayerDecks(self, player, params):
        """Save the order of the deck list. The game sends {"DeckIds": [...], "GameMode": "Classic" | "Skirmish"}."""
        data = self._fn_data(params)
        order = [i for i in (data.get("DeckIds") or []) if isinstance(i, str)]
        key = "PlayerDecksFast" if data.get("GameMode") in ("Skirmish", 13) else "PlayerDecks"
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            try:
                decks = json.loads(player.get("UserData", {}).get(key) or "[]")
            except ValueError:
                decks = []
            if isinstance(decks, dict):
                decks = list(decks.values())
            decks = [d for d in decks if isinstance(d, dict)]
            rank = {deck_id: n for n, deck_id in enumerate(order)}
            ordered = sorted(decks, key=lambda d: rank.get(d.get("id"), len(order)))   # unknown decks keep their place at the end
            if ordered != decks:
                player.setdefault("UserData", {})[key] = json.dumps(ordered, separators=(",", ":"))
                player["DataVersion"] = player.get("DataVersion", 0) + 1
                self.store.save(player)
        return {"Result": True}

    def cs_validatePlayerDeck(self, player, params):
        """Asked before searching for a match. Everyone owns every card here, so any deck the game
        itself let the player build is accepted."""
        return {"Success": True, "Result": True}

    def cs_SetGamemodeDefaultDeck(self, player, params):
        """Remember the deck picked on a mode's page: {"eventId", "deckId"}. It is the deck used for
        that mode's matches and for friend challenges, and it is sent back with the event at login."""
        data = self._fn_data(params)
        event_id, deck_id = data.get("eventId"), data.get("deckId")
        if not event_id:
            return {"Result": False}
        handler = None
        folder = os.path.join(self.content_root, "liveops")
        try:
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    cfg = json.load(f)
                if any(ev.get("Data", {}).get("eventId") == event_id for ev in cfg.get("RawEvents", [])):
                    handler = name[:-5]
                    break
        except (OSError, ValueError) as e:
            log.warning("SetGamemodeDefaultDeck: could not look up event %s: %s", event_id, e)
        if handler is None or not isinstance(deck_id, (str, type(None))):
            return {"Result": False}                         # not an event of this server
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            save = player.setdefault("LiveOps", {}).setdefault(handler, {}).setdefault(event_id, {})
            save["defaultDeckId"] = deck_id or ""
            self.store.save(player)
        return {"Result": True}

    # ---- Draft -----------------------------------------------------------------------------
    # A draft run: enter -> pick a warlord -> pick card packs -> play matches until the run is
    # won or lost -> collect. The server remembers the run ("save") between steps and makes the
    # random choices: the warlords offered (from the player's collection) and, each round, the
    # packs offered (from content/draft/DraftPacks.json, the game's own ready-made packs). The
    # pack offer is kept in the save as "revivalDraftOffer"; the mod shows exactly those packs,
    # and only a pack from the offer is accepted, with the server's own list of its cards.

    DRAFT_FRESH = {"rewardsCollected": False, "allianceRewardsCollected": False, "refunded": False,
                   "won": 0.0, "payState": 0, "selectedWarlord": None, "selectedPacks": [], "selectedCards": [],
                   "selectableWarlords": [], "cardbackId": "", "currentReRolls": 0,
                   "joinState": 0, "matchStarted": False, "numberOfMatchesWon": 0, "numberOfMatchesLost": 0,
                   "numberOfMatchesDraw": 0, "lastOpponentInfo": None, "revivalDraftOffer": []}

    def _event_file(self, event_id):
        """(handler name, event entry) for an event id, looked up in content/liveops/*.json."""
        folder = os.path.join(self.content_root, "liveops")
        try:
            for name in sorted(os.listdir(folder)):
                if not name.endswith(".json"):
                    continue
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    cfg = json.load(f)
                for ev in cfg.get("RawEvents", []):
                    if ev.get("Data", {}).get("eventId") == event_id:
                        return name[:-5], ev
        except (OSError, ValueError) as e:
            log.warning("could not look up event %s: %s", event_id, e)
        return "GameModes", {}

    def _draft(self, player, params, change=None):
        """Load the player's run for the event named in the request, apply `change(save, data)`,
        store it, and return (data, the complete save as the game expects it)."""
        data = self._cs_args(params) or {}
        event_id = data.get("eventID") or data.get("eventId") or ""
        handler, ev = self._event_file(event_id)
        with self.store.lock:
            player.update(self.store.load(player["PlayFabId"]) or {})
            save = player.setdefault("LiveOps", {}).setdefault(handler, {}).setdefault(event_id, {})
            if change:
                change(save, data)
                self.store.save(player)
            full = dict({"eventId": event_id, "IsNew": False, "WasSeen": True, "IsActive": True,
                         "triggerTime": 0, "expiredTime": 0}, **ev.get("Persistence", {}))
            full.update(save)
        return data, full

    # The game's army numbers (CardArmy), by the army names the card list uses.
    ARMY_NUMBERS = {"Ultramarines": 10, "Goff": 20, "SaimHann": 30, "Sautekh": 40, "BlackLegion": 50,
                    "Leviathan": 60, "TauEmpire": 70, "Sororitas": 80, "Genestealers": 90,
                    "AstraMilitarum": 100, "DarkAngels": 110, "EmperorsChildren": 120, "SpaceWolves": 130}

    def _draft_warlords(self, player, data):
        """The warlords a new draft run offers: `warlordsPerDraft` (the event's setting, default 3)
        picked at random from the warlords in this player's collection, each from a different army, limited to the event's
        armies ("specificArmies") and warlords ("specificWarlords") when it names any, without its
        "blacklistedWarlords". With every card given to everyone that is every warlord of those
        armies; if cards are ever unlocked one by one, each player draws from their own. What the
        game sends with the request is not used for this."""
        event_id = data.get("eventID") or data.get("eventId") or ""
        _, ev = self._event_file(event_id)
        evd = ev.get("Data", {}) if isinstance(ev, dict) else {}
        armies = {a for a in evd.get("specificArmies") or [] if isinstance(a, int)}
        only = {w for w in evd.get("specificWarlords") or [] if isinstance(w, str)}
        never = {w for w in evd.get("blacklistedWarlords") or [] if isinstance(w, str)}
        try:
            known = self._creator_known_cards()
        except (OSError, ValueError, KeyError):
            known = None    # no card list on this server: go by the inventory's own "Hero" class alone
        pool = []
        for item in self._inventory(player):
            wid = item.get("ItemId")
            card = (known or {}).get(wid) or {}
            if item.get("ItemClass") != "Hero" or wid in pool or (known is not None and card.get("kind") != "hero"):
                continue
            if armies and known is not None and self.ARMY_NUMBERS.get(card.get("army")) not in armies:
                continue
            if (only and wid not in only) or wid in never:
                continue
            pool.append(wid)
        try:
            count = int(evd.get("warlordsPerDraft") or 3)
        except (TypeError, ValueError):
            count = 3
        if not pool:
            log.warning("draft %s: no warlord in %s's collection fits the event; nothing offered", event_id, player.get("PlayFabId"))
        if count <= 0 or len(pool) <= count:
            return pool
        if known is None:
            return random.sample(pool, count)
        # one warlord per army: armies drawn at random, then a warlord from each; only when there are
        # fewer armies than warlords to offer does an army appear twice
        by_army = {}
        for wid in pool:
            by_army.setdefault(known.get(wid, {}).get("army") or "", []).append(wid)
        armies_drawn = random.sample(list(by_army), min(count, len(by_army)))
        picked = [random.choice(by_army[a]) for a in armies_drawn]
        rest = [w for w in pool if w not in picked]
        picked += random.sample(rest, count - len(picked))
        random.shuffle(picked)
        return picked

    def _draft_enter(self, player, params, pay_state):
        now = int(time.time() * 1000)

        def enter(save, data):
            last_free = save.get("lastTimeEnteredFree", 0)
            save.clear()
            save.update(json.loads(json.dumps(self.DRAFT_FRESH)))
            save["payState"] = pay_state
            save["joinState"] = 1                   # Joined: the game only offers warlords for a joined run
            save["selectableWarlords"] = self._draft_warlords(player, data)
            save["lastTimeEnteredFree"] = now if pay_state == 1 else last_free
        _, full = self._draft(player, params, enter)
        return now, full

    def cs_FreeDraftEntrance(self, player, params):
        now, save = self._draft_enter(player, params, 1)
        return {"LastTimeEntered": now, "Save": save}

    def cs_PayDraftEntrance(self, player, params):
        data = self._cs_args(params) or {}
        _, save = self._draft_enter(player, params, 3 if data.get("isTen") else 2)
        return {"AmountRemoved": 0, "Save": save}      # entry is free on a revival server

    def _draft_packs(self):
        """({pack id: pack}, {card id: army number}) from content/draft/DraftPacks.json, re-read when it changes."""
        path = os.path.join(self.content_root, "draft", "DraftPacks.json")
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return {}, {}
        if getattr(self, "_packs_stamp", None) != stamp:
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
                packs = {p["id"]: p for p in doc.get("packs", []) if isinstance(p, dict) and isinstance(p.get("id"), str)
                         and isinstance(p.get("cards"), list) and p["cards"]}     # a pack without cards is skipped (review #20)
                armies = {k: v for k, v in (doc.get("cardArmies") or {}).items() if isinstance(v, int)}
            except (OSError, ValueError, KeyError, AttributeError) as e:
                log.warning("draft packs: %s could not be read: %s", path, e)
                return {}, {}
            self._packs, self._packs_stamp = (packs, armies), stamp
        return self._packs

    def _draft_settings(self, event_id):
        evd = (self._event_file(event_id)[1] or {}).get("Data", {})
        num = lambda k, d: evd[k] if isinstance(evd.get(k), int) and not isinstance(evd.get(k), bool) and evd[k] > 0 else d
        free = 0
        for step in evd.get("reRollCosts") or []:            # rerolls priced at nothing, before the first paid one
            cost = (step.get("costs") or {}).get("Amount") if isinstance(step, dict) else None
            if cost:
                break
            free += step.get("numberOfReRolls") or 0 if isinstance(step, dict) else 0
        return num("draftRounds", 10), num("packsPerDraft", 3), free

    def _draft_new_offer(self, save, data, avoid_offer=False):
        """A fresh random offer for the run's next pack pick ([] once every round is picked)."""
        rounds, per_round, _ = self._draft_settings(data.get("eventID") or data.get("eventId") or "")
        packs, armies = self._draft_packs()
        army = armies.get(save.get("selectedWarlord") or "")
        if army is None or len(save.get("selectedPacks") or []) >= rounds:
            save["revivalDraftOffer"] = []
            return
        pool = [pid for pid, p in packs.items() if p.get("enabled") and p.get("army") == army]
        if not pool:
            log.warning("draft: no packs for army %s (warlord %s)", army, save.get("selectedWarlord"))
        # Packs already picked this run are not offered again, nor (on a reroll) the offer being
        # replaced: a pack offered again right after it was picked looked like the screen had not
        # changed, and a quick second tap picked it twice. Only when too few packs are left does
        # the offer fall back to the whole pool.
        used = set(save.get("selectedPacks") or [])
        if avoid_offer:
            used |= set(save.get("revivalDraftOffer") or [])
        fresh = [pid for pid in pool if pid not in used]
        if len(fresh) < per_round:
            fresh = [pid for pid in pool if pid not in set(save.get("selectedPacks") or [])]
        if len(fresh) < per_round:
            last = (save.get("selectedPacks") or [None])[-1]
            fresh = [pid for pid in pool if pid != last] or pool     # never the pack just picked
        save["revivalDraftOffer"] = random.sample(fresh, min(per_round, len(fresh)))

    def cs_PickDraftWarlord(self, player, params):
        result = {}

        def pick(save, data):
            wanted = data.get("warlord") or None
            offered = save.get("selectableWarlords") or []
            if wanted and save.get("selectedWarlord") == wanted:
                return                                  # the same pick sent again (a retry): nothing changes
            if save.get("selectedWarlord") or (offered and wanted not in offered):
                result["refused"] = True                # not one of the warlords this run offered
                return
            save["selectedWarlord"] = wanted
            save["currentReRolls"] = 0
            self._draft_new_offer(save, data)
        _, save = self._draft(player, params, pick)
        if result.get("refused"):
            log.warning("draft: %s picked a warlord that was not offered", player.get("PlayFabId"))
        return {"Result": not result.get("refused"), "Save": save}

    def cs_PickDraftPack(self, player, params):
        result = {}

        def pick(save, data):
            wanted = data.get("pack")
            offer = save.get("revivalDraftOffer")
            packs, armies = self._draft_packs()
            pack = packs.get(wanted) if isinstance(wanted, str) else None
            picked = save.get("selectedPacks") or []
            if offer is not None and wanted not in offer and picked and picked[-1] == wanted:
                # The last pick sent again. The game counts every accepted answer as one more
                # pack in the deck, so this is refused (a retry of a lost request is answered
                # with the first answer before it gets here; see _replay_slot).
                result["refused"] = result["again"] = True
                return
            if offer is None:
                # a run started before the server made the offers: any pack of the warlord's army
                ok_pick = bool(pack and pack.get("enabled") and pack.get("army") == armies.get(save.get("selectedWarlord") or ""))
            else:
                ok_pick = bool(pack) and wanted in offer
            if not ok_pick:
                result["refused"] = True
                return
            save.setdefault("selectedPacks", []).append(wanted)
            save.setdefault("selectedCards", []).extend(pack.get("cards") or [])     # the server's list, not the game's
            save["currentReRolls"] = 0
            self._draft_new_offer(save, data)
        _, save = self._draft(player, params, pick)
        if result.get("again"):
            log.warning("draft: %s picked the same pack twice; the second pick refused", player.get("PlayFabId"))
        elif result.get("refused"):
            log.warning("draft: %s picked a pack that was not offered", player.get("PlayFabId"))
        return {"Result": not result.get("refused"), "Save": save}

    def cs_SaveSelectedDraftCards(self, player, params):
        def keep(save, data):
            if isinstance(data.get("cards"), list):
                save["selectedCards"] = data["cards"]
        _, save = self._draft(player, params, keep)
        return {"Result": True, "Save": save}

    def cs_RerollDraft(self, player, params):
        def again(save, data):
            if int(save.get("currentReRolls", 0)) >= self._draft_settings(data.get("eventID") or data.get("eventId") or "")[2]:
                return                                  # no free reroll left this round: the offer stays
            save["currentReRolls"] = int(save.get("currentReRolls", 0)) + 1
            self._draft_new_offer(save, data, avoid_offer=True)
        _, save = self._draft(player, params, again)
        return {"cost": {"Currency": 40, "Amount": 0}, "currentReRolls": save.get("currentReRolls", 0),
                "revivalDraftOffer": save.get("revivalDraftOffer") or []}

    def cs_DraftMatchFound(self, player, params):
        def found(save, data):
            save["lastOpponentInfo"] = data.get("opponentInfo")
            save["matchStarted"] = True
        self._draft(player, params, found)
        return {"Result": True}

    def cs_DraftMatchFinished(self, player, params):
        def done(save, data):
            try:
                won = float(data.get("matchWon") or 0)
            except (TypeError, ValueError):
                won = 0.0
            save["won"] = float(save.get("won", 0)) + won
            key = "numberOfMatchesWon" if won >= 1 else "numberOfMatchesLost" if won <= 0 else "numberOfMatchesDraw"
            save[key] = int(save.get(key, 0)) + 1
            save["matchStarted"] = False
            data_ev = self._event_file(data.get("eventID") or "")[1].get("Data", {})
            if (save.get("numberOfMatchesLost", 0) >= int(data_ev.get("maxTries") or 3)
                    or save.get("numberOfMatchesWon", 0) >= len(data_ev.get("matches") or [])):
                save["joinState"] = 2               # run over: out of tries, or every match won
        _, save = self._draft(player, params, done)
        return {"Result": True, "Save": save}

    def cs_FinishDraftRun(self, player, params):
        # Resigning, or closing a run that is over: mark it Finished so the game moves to the results.
        _, save = self._draft(player, params, lambda save, data: save.update(matchStarted=False, joinState=2))
        return save

    def cs_CollectDraftRewards(self, player, params):
        _, save = self._draft(player, params, lambda save, data: save.update(rewardsCollected=True, selectableWarlords=[]))
        return {"Rewards": [], "Save": save}

    def cs_RefundDraftRun(self, player, params):
        _, save = self._draft(player, params, lambda save, data: save.update(refunded=True, rewardsCollected=True))
        return {"Rewards": [], "Save": save}

    def cs_ResetCollectedDraft(self, player, params):
        def reset(save, data):
            last_free = save.get("lastTimeEnteredFree", 0)
            save.clear()
            save.update(json.loads(json.dumps(self.DRAFT_FRESH)))
            save["lastTimeEnteredFree"] = last_free
        _, save = self._draft(player, params, reset)
        return {"Result": True, "Save": save}

    def cs_ExpireDraftEvent(self, player, params):
        return self.cs_ResetCollectedDraft(player, params)

    def _handler_names(self, names):
        """The game's list of LiveOps handler names, keeping only plain names (see HANDLER_NAME), once each."""
        out = []
        for h in names if isinstance(names, list) else []:
            if isinstance(h, str) and self.HANDLER_NAME.fullmatch(h) and h not in out:
                out.append(h)
                if len(out) >= self.MAX_HANDLERS:
                    break
        return out

    def cs_InitializeLiveOps(self, player, params):
        data = self._fn_data(params)
        td = self.store.title_data()
        return {
            "HandlerData": {h: self._liveops_handler(player, h) for h in self._handler_names(data.get("Handlers"))},
            "TitleData": {k: td[k] for k in (data.get("Configs") if isinstance(data.get("Configs"), list) else [])
                          if isinstance(k, str) and k in td},
            "SharedEventData": {"energyData": {"energy": 0, "lastUpdated": int(time.time() * 1000)}},
            "grantedItems": [],
        }

    def api_Client_ExecuteCloudScript(self, req, headers):
        p = self._player(headers)
        fn = req.get("FunctionName", "")
        h = self.cloudscript.get(fn)
        if not h and fn.endswith("MatchFinished"):
            h = self.cs_match_finished          # ranked / skirmish / draft results share one shape
        result = h(p, req.get("FunctionParameter")) if h else {}
        if not h:
            log.warning("CloudScript %s not implemented (returned {})", fn)
        if fn.endswith("MatchFinished") and p:
            try:
                entries = self._battle_log_add(p, fn, self._fn_data(req.get("FunctionParameter")))
                if isinstance(result, dict) and entries is not None and "BattleLogData" in result:
                    result["BattleLogData"] = entries
                    if "VictoryCounters" in result:
                        result["VictoryCounters"] = self._victory_counters(self.store.load(p["PlayFabId"]) or p)
            except Exception:                       # the result screen matters more than the log
                log.exception("battle log: could not record %s", fn)
        return ok({"FunctionName": fn, "Revision": 1, "FunctionResult": result, "Logs": [],
                   "ExecutionTimeSeconds": 0.001, "ProcessorTimeSeconds": 0.001, "MemoryConsumedBytes": 0,
                   "APIRequestsIssued": 0, "HttpRequestsIssued": 0})

    def api_CloudScript_ExecuteFunction(self, req, headers):
        p = self._player(headers)
        fn = req.get("FunctionName", "")
        h = self.cloudscript.get(fn)
        result = h(p, req.get("FunctionParameter")) if h else {}
        if not h:
            log.warning("Azure function %s not implemented (returned {})", fn)
        return ok({"FunctionName": fn, "FunctionResult": result, "ExecutionTimeMilliseconds": 1,
                   "FunctionResultTooLarge": False})
