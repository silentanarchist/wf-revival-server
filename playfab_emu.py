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
import threading
import time
import unicodedata

import photon_emu
if not hasattr(photon_emu, "SEEN"):        # an older photon_emu (before a restart) has no table of its own
    photon_emu.SEEN = {}
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

    def default_name(self, pfid):
        """Name given to a new player until they pick one: "Commander-" plus part of their id,
        lengthened if someone already took that exact name."""
        taken = set()
        for name in os.listdir(self.players_dir):
            if name.endswith(".json") and name[:-5] != pfid:
                other = self.load(name[:-5])
                if other and other.get("DisplayName"):
                    taken.add(other["DisplayName"].strip().lower())
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

    def __init__(self, native):
        self.n = native
        self.e = native["enums"]
        self.reads = (native.get("reads") or {})
        self.trig = (native.get("triggers") or {})
        self.notes = []
        self.explicit_choice = False       # the design names what the player may pick ("choose")

    def _new(self, what):
        return copy.deepcopy(self.n[what])

    def _enum(self, table, name, what):
        if name in (None, "", 0):
            return 0
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
        duration = b.get("duration")
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
        when = ab.get("when") or {}
        turn = when.get("turn") or ab.get("turn") or info.get("turn")
        if turn:
            a["boardCriteria"]["playerTurn"] = self.PLAYERS.get(turn, 0)
        if when.get("count") is not None or when.get("is"):
            a["boardCriteria"]["countCriteria"] = self.targets(when.get("count") or {}, (when.get("count") or {}).get("pool") or "allFiltered")
            a["boardCriteria"]["triggerIf"] = self._enum("StatFilterOptions", when.get("is") or "EqualOrHigher", "comparison")
            a["boardCriteria"]["referenceValue"] = self._int(when.get("value"))
            if when.get("minusAllies"):
                a["boardCriteria"]["countOptions"] = self.e["CountOptions"]["EnemiesMinusAllies"]
        elif when.get("chance"):
            a["boardCriteria"]["triggerIf"] = self.e["StatFilterOptions"]["RandomChance"]
            a["boardCriteria"]["referenceValue"] = self._int(when["chance"])
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
                 manaCost=card["cost"], attackDamageType=4, initiative=20, showInCollectionOptions=5,
                 useOverDraw=0 if kind == "tactic" else 1)
        tag = self.n["armyTags"].get(card.get("army") or "", card.get("army") or "Neutral")
        d["m_Name"] = "%s %s-%s" % ("war" if kind == "hero" else ("T" if kind == "tactic" else "") + "C", tag, card["name"])
        if card.get("token"):
            # how the game marks cards created by other cards (seen on every such official card):
            # not shown in the collection, not part of the inventory, never picked by "random card" effects
            d.update(showInCollectionOptions=0, inventoryOptions=10, cantBeRandomlyCreated=1)
            d["m_Name"] = "Z-NC " + d["m_Name"].split(" ", 1)[1]
        d["deckCriteria"]["triggerIf"] = 1
        d["normalAbilityBoardCriteria"]["triggerIf"] = 1
        d["attackScoringCriteria"] = copy.deepcopy(self.n["attackScoring"])
        if kind == "tactic":
            d["spellType"] = self._enum("SpellType", card.get("stratagemType") or "Normal", "stratagem type")
            if d["spellType"]:
                d["cantBeRandomlyCreated"] = 1
        else:
            d.update(meleeAttack=card.get("melee", 0), rangeAttack=card.get("ranged", 0), maxHealth=card.get("health", 1),
                     armour=card.get("armour", 0), keyword=self._enum("Keyword", card.get("keyword"), "unit type"))
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
                         _traitValue=int(t.get("value") or 0))
            d["CardTraits"].append(entry)
        own = {t.get("name") for t in card.get("traits") or []}
        given = set()
        chosen = None
        self.explicit_choice = "choose" in card      # the design says itself what the player picks (or that nothing is picked)
        for i, ab in enumerate(card.get("abilities") or [], 1):
            a, wants, trigger = self.ability(ab, i)
            if wants is not None:
                if not (self.trig.get(trigger) or {}).get("chooses", trigger == "ThisCardPlayed"):
                    self.notes.append(f"ability {i} asks the player to choose a target, but official cards only do that on "
                                      "'when played' style abilities and actions - use a random or 'all' target group instead")
                elif chosen is None:
                    chosen = wants
            if ab.get("keyword") and ab["keyword"] not in own:      # e.g. Rally labelling a "when played" ability
                own.add(ab["keyword"])
                d["CardTraits"].append(dict(self.n["trait"], name=ab["keyword"],
                                            _trait=self._enum("DefinedTrait", ab["keyword"], "keyword")))
            for fx in ab.get("effects") or []:
                if (fx.get("buff") or {}).get("trait"):
                    given.add(fx["buff"]["trait"])
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
                self.explicit_choice = "choose" in action
                logics, wants = self.effects(action.get("effects"), "the action")
                d["hasActiveAbility"] = 1
                d["activeAbility"]["activeAbilityLogic"] = logics
                d["activeAbility"]["manaCost"] = int(action.get("cost") or 0)
                if self.explicit_choice:
                    wants = action["choose"] or None
                if wants is not None:
                    d["activeAbility"]["targeted"] = 1
                    d["activeAbility"]["targetCriteria"] = self.targets(wants, wants.get("pool") if self.explicit_choice and wants.get("pool") else "allFiltered")
                if action.get("keyword") and action["keyword"] not in own:
                    d["CardTraits"].append(dict(self.n["trait"], name=action["keyword"],
                                                _trait=self._enum("DefinedTrait", action["keyword"], "keyword")))
                if action.get("keyword") == "ferocity":
                    self.notes.append("Ferocity: written like the rebuilt Space Wolves cards, which could not be activated in a match yet")
                if action.get("note"):
                    self.notes.append(f"action limit not written into the game data yet: {action['note']}")
                for fx in action.get("effects") or []:
                    if (fx.get("buff") or {}).get("trait"):
                        given.add(fx["buff"]["trait"])
        for name in sorted(given - own):          # keywords the card hands out get a hint on the card
            d["ShowTraits"].append(dict(self.n["trait"], name=name, _trait=self._enum("DefinedTrait", name, "keyword")))
        return d, list(dict.fromkeys(self.notes))


class PlayFabEmulator:
    def __init__(self, data_root, content_root, log_dir):
        self.content_root = content_root
        self.store = Store(data_root, content_root)
        self.store.banned = self._banned_ids
        # the match service asks for a match ticket whenever accounts are required - from the
        # start, not only once someone has signed in (re-read on every reload of this file)
        photon_emu.REQUIRE[0] = self._settings().get("requireAccount") is True
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
            "saveMatchRecording": self.cs_battle_log_current,       # recordings themselves are not kept
            "pinMatchRecording": self.cs_pinMatchRecording,
            "recoverMatchRecording": lambda player, params: {},     # no recording: the game says the replay cannot be loaded
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
        player = self.store.load(pfid) or {}
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
        for f in self._friends(player):
            fid = f.get("id") if isinstance(f, dict) else None
            if not isinstance(fid, str):
                continue
            other = self.store.load(fid) or {}
            if not any(isinstance(g, dict) and g.get("id") == pfid for g in self._friends(other)):
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
        return ok({"messages": out, "last": max(last, since, 0), "history": since < 0,
                   "friends": self._friend_statuses(me)})

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

    def api_Revival_CreatorReports(self, req, headers):
        """Admins: reported chat messages, newest first (closed ones only with "closed": true)."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see reports")
        users = self._creator_users()
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

    @staticmethod
    def _creator_hash(passphrase, salt):
        return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), bytes.fromhex(salt), 200000).hex()

    def _creator_user(self, req):
        """Display name of the signed-in author, or None."""
        token = str(req.get("session") or "")
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for user in self._creator_users().values():
            if digest in user.get("sessions", []):
                return None if self._creator_perms(user)["access"] in ("banned", "requested") else user["name"]
        return None

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
                return err("InvalidParams", "account names are 5 to 30 letters, digits, . ' - or _, without spaces")
            if self._word_filter().found(name):
                return err("InvalidParams", "that name contains a word that is not allowed here")
            if not 6 <= len(passphrase) <= 200:
                return err("InvalidParams", "the password must be at least 6 characters")
            if any(ch.isspace() for ch in passphrase):
                return err("InvalidParams", "the password cannot contain spaces")
        elif not name or not passphrase:
            return err("InvalidParams", "enter a name and a password")
        where = self._address(headers)
        slow = err("TooManyAttempts", "too many attempts - wait a few minutes and try again", 1199, 429)
        if not self._allow("signin-fail", where, 20, 900, count=False):
            return slow
        with self.store.lock:
            users = self._creator_users()
            user = users.get(name.lower())
            if self._name_locked(name, where, user):
                return slow
            if user is None:
                if not req.get("create"):
                    self._allow("signin-fail", where, 20, 900)
                    return err("AccountNotFound", "no account with that name yet - use 'Create an account' below")
                if not self._allow("signup", where, 5, 3600):
                    return err("TooManyAttempts", "too many new accounts from this connection - try again in an hour", 1199, 429)
                salt = os.urandom(16).hex()
                user = users[name.lower()] = {"name": name, "salt": salt, "hash": self._creator_hash(passphrase, salt),
                                              "sessions": [], "created": now_iso(),
                                              "access": "approved" if self._creator_auto_approve() else "requested"}
            elif req.get("create"):
                return err("NameNotAvailable", "that name is taken - sign in instead, or pick another name")
            elif not hmac.compare_digest(user["hash"], self._creator_hash(passphrase, user["salt"])):
                self._count_wrong_password(name, where)
                return err("InvalidPassword", "wrong password")
            if self._creator_perms(user)["access"] == "banned":
                return err("AccountBanned", "this account has been banned")
            self._remember_address(user, where)
            if self._creator_perms(user)["access"] == "requested":
                # A new account that an admin has not approved yet: nothing on the site or in the
                # game until then (it is saved, so it appears on the People page).
                self._creator_users_save(users)
                return err("AccountNotApproved", "your account is waiting for an admin to approve it - try again once it is approved", 1002, 403)
            token = uuid.uuid4().hex + uuid.uuid4().hex
            user["sessions"] = (user.get("sessions", []) + [hashlib.sha256(token.encode()).hexdigest()])[-5:]
            self._creator_users_save(users)
        return ok({"name": user["name"], "session": token, "account": self._creator_perms(user)})

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

    # Permissions belong to accounts and only admins can change them:
    #   access    "requested" (asked to join), "approved" or "banned". New accounts are approved
    #             straight away unless revival-settings.json has {"accounts": {"autoApprove": false}}.
    #   reviewer  approves and rejects cards waiting for review.
    #   tester    in-game tester (will see custom cards in the game before they are released).
    #   admin     everything: reviewer and tester rights, changing or un-approving approved cards,
    #             and editing accounts.
    # Founding admins are always admins and cannot be changed from the page (so the server can
    # never be left without one).
    ACCESS = ("requested", "approved", "banned")

    def _creator_founders(self):
        try:
            with open(os.path.join(self.store.root, "creator_admins.json"), encoding="utf-8") as f:
                return {str(n).lower() for n in json.load(f)}
        except (OSError, ValueError, TypeError):
            return set()

    def _creator_auto_approve(self):
        try:
            with open(os.path.join(self.content_root, "files", "revival-settings.json"), encoding="utf-8") as f:
                return (json.load(f).get("accounts") or {}).get("autoApprove", True) is not False
        except (OSError, ValueError, AttributeError):
            return True

    def _creator_perms(self, user):
        """{"access", "reviewer", "tester", "admin", "fixed"} for an account."""
        fixed = user["name"].lower() in self._creator_founders()
        admin = fixed or bool(user.get("admin")) or user.get("role") == "admin"        # "role" is the older form
        access = user.get("access")
        if access not in self.ACCESS:
            access = "approved"                                 # accounts made before access existed
        if fixed:
            access = "approved"
        return {"access": access, "admin": admin, "fixed": fixed, "player": user.get("player") or "",
                "reviewer": admin or bool(user.get("reviewer")) or user.get("role") == "reviewer",
                "tester": admin or bool(user.get("tester"))}

    def _creator_account(self, req):
        """The signed-in account, or None."""
        token = str(req.get("session") or "")
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for user in self._creator_users().values():
            if digest in user.get("sessions", []):
                # an account set back to "requested" by an admin is signed out of the site meanwhile
                return None if self._creator_perms(user)["access"] == "requested" else user
        return None

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
        if rarity not in ("Common", "Rare", "Epic", "Legendary"):
            raise ValueError("unknown rarity")
        out = {"name": name, "kind": kind, "rarity": rarity, "army": str(card.get("army") or "")[:40],
               "cost": self._creator_int(card.get("cost", 0), 0, 12, "cost"),
               "text": str(card.get("text") or "")[:300], "art": str(card.get("art") or "")[:40],
               "designNotes": str(card.get("designNotes") or "")[:1000],
               # a card that only exists when another card creates it: not in the collection, not for decks
               "token": card.get("token") is True and card.get("kind") != "hero"}
        if kind != "tactic":
            out["melee"] = self._creator_int(card.get("melee", 0), 0, 30, "melee")
            out["ranged"] = self._creator_int(card.get("ranged", 0), 0, 30, "ranged")
            out["health"] = self._creator_int(card.get("health", 1), 1, 60, "health")
            out["armour"] = self._creator_int(card.get("armour", 0), 0, 30, "armour")
            out["keyword"] = str(card.get("keyword") or "")[:30]
        else:
            out["stratagemType"] = str(card.get("stratagemType") or "Normal")[:30]
        for field, limit in (("traits", 8), ("abilities", 6)):
            value = card.get(field) or []
            if not isinstance(value, list) or len(value) > limit or not all(isinstance(x, dict) for x in value):
                raise ValueError(f"{field} must be a list of at most {limit} entries")
            out[field] = value
        action = card.get("action")
        if isinstance(action, dict):          # an ability the player activates by hand (Duty, Ferocity)
            effects = action.get("effects") or []
            if not isinstance(effects, list) or len(effects) > 6 or not all(isinstance(x, dict) for x in effects):
                raise ValueError("the action must have at most 6 effects")
            out["action"] = {"keyword": str(action.get("keyword") or "")[:30],
                             "cost": self._creator_int(action.get("cost", 0), 0, 12, "action cost"),
                             "note": str(action.get("note") or "")[:200], "effects": effects}
            if isinstance(action.get("choose"), dict) or action.get("choose") is False:
                out["action"]["choose"] = action["choose"]
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
        return ok({"image": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"), "stamp": card["artUpload"]})

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
            card.pop("artUpload", None)
            card.pop("artMask", None)
            self._creator_store(card, path)
        return ok({"card": card})

    # Admins answer for every picture kept on the server, drafts included, so they can list all
    # uploads and delete one for good (the author's own "remove" only sets a picture aside).
    def api_Revival_CreatorArtList(self, req, headers):
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see every upload")
        with self.store.lock:
            out = [{"id": c["id"], "name": c.get("name", ""), "author": c.get("author", ""), "status": c.get("status", ""),
                    "kind": c.get("kind", ""), "artUpload": c["artUpload"], "artMask": c.get("artMask") or ""}
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
            for tail in (".png", ".mask.png", ".removed.png", ".mask.removed.png"):
                if os.path.isfile(base + tail):
                    os.remove(base + tail)
                    gone += 1
            card.pop("artUpload", None)
            card.pop("artMask", None)
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
        key, user = self._creator_account_of_player(self._creator_users(), pfid)
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
        for user in self._creator_users().values():
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
        users = self._creator_users()

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
            for p in self._all_players():
                pfid = p["PlayFabId"]
                if (query in (p.get("DisplayName") or "").lower() or pfid.startswith(key)) and len(found) < 30:
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
            _, account = self._creator_account_of_player(self._creator_users(), pfid)
            if account and account["name"].lower() == me["name"].lower():
                return err("InvalidParams", "that is your own game account")
            data = self._bans()
            if ban:
                data["players"][pfid] = {"reason": str(req.get("reason") or "")[:200], "by": me["name"], "at": now_iso()}
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

    def api_Revival_CreatorRename(self, req, headers):
        """Changes the account's username (the current passphrase is asked again). Cards follow the new name."""
        new = " ".join(str(req.get("name") or "").split())
        if not re.fullmatch(r"[\w.'-]{5,30}", new):
            return err("InvalidParams", "account names are 5 to 30 letters, digits, . ' - or _, without spaces")
        if self._word_filter().found(new):
            return err("InvalidParams", "that name contains a word that is not allowed here")
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_perms(me)["access"] == "banned":
                return err("NotAuthorized", "sign in first")
            if not self._creator_check_passphrase(me, req.get("passphrase")):
                return err("InvalidPassword", "wrong password")
            users = self._creator_users()
            old_key, new_key = me["name"].lower(), new.lower()
            if new_key != old_key and new_key in users:
                return err("NameNotAvailable", "that name is taken")
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
            if was_founder:                                   # a founding admin stays one under the new name
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

    def api_Revival_CreatorRecover(self, req, headers):
        """Forgotten password: name + recovery code + new password. Signs in on success."""
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
            if user is None or not digest or not user.get("recoveryHash") or not hmac.compare_digest(user["recoveryHash"], digest):
                self._allow("recover-fail", where, 10, 900)
                self._allow("recover-fail-name", name.lower(), 5, 900)
                return err("InvalidPassword", "that name and recovery code do not match")
            if self._creator_perms(user)["access"] == "banned":
                return err("AccountBanned", "this account has been banned")
            token = uuid.uuid4().hex + uuid.uuid4().hex
            user["salt"] = os.urandom(16).hex()
            user["hash"] = self._creator_hash(new, user["salt"])
            user["sessions"] = [hashlib.sha256(token.encode("utf-8")).hexdigest()]
            user["passphraseChangedAt"] = user["recoveredAt"] = now_iso()
            user.pop("recoveryHash", None)                  # used up
            user.pop("recoveryAt", None)
            user.pop("gameLoginHash", None)                 # every computer and phone signs in again
            user.pop("gameLogins", None)
            self._end_play(user.get("player"), "The password of this account was reset. Start the game again and sign in.")
            self._creator_users_save(users)
        log.info("account %s: password reset with its recovery code", user["name"])
        return ok({"name": user["name"], "session": token, "account": self._creator_perms(user)})

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
        return ok({"name": user["name"], "token": token})

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
            return ok({"keyword": keyword, "examples": self._examples.get("keywords", {}).get(keyword) or {"count": 0, "cards": []}})
        return ok({"keywords": {k: v["count"] for k, v in self._examples.get("keywords", {}).items()}})

    def api_Revival_CreatorPeople(self, req, headers):
        """Every account with its permissions and number of cards (admins only)."""
        if self._creator_role(req) < 2:
            return err("NotAuthorized", "only admins can see the accounts")
        with self.store.lock:
            users, cards = self._creator_users(), self._creator_load()
        counts = {}
        for c in cards:
            counts[c.get("author", "").lower()] = counts.get(c.get("author", "").lower(), 0) + 1
        def game_name(pfid):
            p = self.store.load(pfid) if pfid else None
            return (p.get("DisplayName") or pfid) if p else ""
        people = [dict(self._creator_perms(u), name=u["name"], created=u.get("created", ""), cards=counts.get(key, 0),
                       playerName=game_name(u.get("player"))) for key, u in users.items()]
        people.sort(key=lambda p: (p["access"] != "requested", not p["admin"], p["name"].lower()))
        return ok({"people": people, "autoApprove": self._creator_auto_approve(),
                   "requireAccount": self._settings().get("requireAccount") is True})

    def api_Revival_CreatorSetAccount(self, req, headers):
        """An admin changes one account: any of access, reviewer, tester, admin."""
        name = str(req.get("name") or "").lower()
        access = req.get("access")
        if access is not None and access not in self.ACCESS:
            return err("InvalidParams", "unknown access value")
        unlink = req.get("unlink") is True
        flags = {k: req[k] for k in ("reviewer", "tester", "admin") if k in req}
        if not all(isinstance(v, bool) for v in flags.values()):
            return err("InvalidParams", "reviewer, tester and admin are on/off values")
        with self.store.lock:
            me = self._creator_account(req)
            if not me or self._creator_role(req) < 2:
                return err("NotAuthorized", "only admins can edit accounts")
            users = self._creator_users()
            user = users.get(name)
            if user is None:
                return err("InvalidParams", "no account with that name")
            if name in self._creator_founders():
                return err("NotAuthorized", f"{user['name']} is a founding admin and cannot be changed here")
            if name == me["name"].lower() and (flags.get("admin") is False or access in ("banned", "requested")):
                return err("InvalidParams", "you cannot remove your own admin rights - ask another admin")
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
            user["changedBy"], user["changedAt"] = me["name"], now_iso()
            self._creator_users_save(users)
            perms = self._creator_perms(user)
        return ok(dict(perms, name=user["name"]))

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
        """The owner's revival-settings.json ({} when there is none)."""
        try:
            with open(os.path.join(self.content_root, "files", "revival-settings.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

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
            # the Revival project's own optional pieces are not "other mods"
            # (the graphics plugin is retired; logged so the owner can tell when no phone has it any more)
            allowed = {"warpforge revival graphics"}
            names = [n for n in (x.strip() for x in others.split(",")) if n]
            retired = any(n.lower() in allowed for n in names)
            others = ", ".join(n for n in names if n.lower() not in allowed)
            if others:
                return (err("OtherModsLoaded", "This server only allows the Warpforge Revival mod. Remove these and start the game again: " + others, 1001, 401),
                        "other mods loaded: " + others)
            sent = str(headers.get("X-Revival-Hash") or "").strip().lower()
            # ("unknown": the mod could not read its own file - let it in, since refusing would lock out honest players)
            if sent != "unknown" and served.get("h") and has and has == self._version_number(served.get("v")) and sent != str(served["h"]).lower():
                return (err("ModChanged", "This copy of the Warpforge Revival mod is not the one this server hands out. Let the game update the mod, or download it again.", 1001, 401),
                        "mod file of %s differs from the one served" % mine)
            if retired:
                return None, "still has the retired Warpforge Revival graphics plugin"
        return None, ""

    def handle(self, path, headers, body_bytes):
        api = path[len("/playfab/"):].split("?")[0]
        try:
            req = json.loads(body_bytes or b"{}")
        except ValueError:
            req = {}
        handler = getattr(self, "api_" + api.replace("/", "_"), None)
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
                note = why     # a sign-in let through can still carry a note (the retired graphics plugin)
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
        if api.startswith("Revival/Creator") or api.startswith("Revival/Account"):
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
        for p in self._all_players():
            if (p.get("DisplayName") or "").strip().lower() == q.lower() or p["PlayFabId"] == key:
                return p
            if len(key) == 8 and p["PlayFabId"].startswith(key):
                by_code = p
        return by_code

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
                     "replaysVersion": text("ReplaysVersion") or "", "pinned": False,
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
            kept, free = [], self.BATTLE_LOG_KEEP
            for e in entries:
                if e.get("pinned"):
                    kept.append(e)
                elif free > 0:
                    kept.append(e)
                    free -= 1
            self._battle_log_save(player, kept)
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
    # won or lost -> collect. The game does the picking and random offers itself; the server
    # only has to remember the run ("save") between steps and hand it back.

    DRAFT_FRESH = {"rewardsCollected": False, "allianceRewardsCollected": False, "refunded": False,
                   "won": 0.0, "payState": 0, "selectedWarlord": None, "selectedPacks": [], "selectedCards": [],
                   "selectableWarlords": [], "cardbackId": "", "currentReRolls": 0,
                   "joinState": 0, "matchStarted": False, "numberOfMatchesWon": 0, "numberOfMatchesLost": 0,
                   "numberOfMatchesDraw": 0, "lastOpponentInfo": None}

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
        picked at random from the warlords in this player's collection, limited to the event's
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
        return random.sample(pool, count)

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

    def cs_PickDraftWarlord(self, player, params):
        def pick(save, data):
            save["selectedWarlord"] = data.get("warlord") or None
            save["currentReRolls"] = 0
        _, save = self._draft(player, params, pick)
        return {"Result": True, "Save": save}

    def cs_PickDraftPack(self, player, params):
        def pick(save, data):
            save.setdefault("selectedPacks", []).append(data.get("pack"))
            cards = data.get("cards")
            if isinstance(cards, list):
                save.setdefault("selectedCards", []).extend(cards)     # the game sends this pack's cards only
            save["currentReRolls"] = 0
        _, save = self._draft(player, params, pick)
        return {"Result": True, "Save": save}

    def cs_SaveSelectedDraftCards(self, player, params):
        def keep(save, data):
            if isinstance(data.get("cards"), list):
                save["selectedCards"] = data["cards"]
        _, save = self._draft(player, params, keep)
        return {"Result": True, "Save": save}

    def cs_RerollDraft(self, player, params):
        def again(save, data):
            save["currentReRolls"] = int(save.get("currentReRolls", 0)) + 1
        _, save = self._draft(player, params, again)
        return {"cost": {"Currency": 40, "Amount": 0}, "currentReRolls": save.get("currentReRolls", 0)}

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
