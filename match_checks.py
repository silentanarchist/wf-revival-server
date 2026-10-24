"""Server-side checks of a finished match, from the relay's own record (2026-10-21).

The server has no rules engine, so these are tripwires for the crude cheats a modified game could try, worked out
from static card data and the mode's rules only. Each flag becomes an automatic report for the admins (playfab_emu
files them when a match room closes); nothing is blocked.

    flags = check_match(relay, lobby, room_name, carddata, rules, long_copies)

- relay: playfab_emu._relay_game(match id): {"players": {id: {"deck", "env", "mulligan", "moves"}}, "goesFirst"
  (the server's coin; the card and mana checks need it), ...}
- lobby: the room's lobby name ('...#RevivalClassic@r1-0.1.0'; '' for a friend challenge), room_name
- carddata: data/replay-carddata.json: {card id: {"name", "kind": unit|hero|tactic|whispers, "cost", "rarity"
  (1 Common .. 4 Legendary, 5 Special), "army", "collection": True for cards the deck editor offers}}
- rules: {event id: gameplayVariables} from GameModes.json; long_copies: {rarity number: copies} for the Long Game

Checks:
  deck  - the deck list a game sent at the start: size, cards that exist and are deck-legal, one army (the
          warlord's), copies per card and legendaries per deck by the mode's rules; a custom card (CUSTOM-NNNN)
          only in a mode whose rules have "customCards".
  card  - a card played by its in-match number that belongs to the OPPONENT's deck (the game numbers both decks
          in a known order: first player's warlord, their deck, second player's warlord, their deck).
  mana  - a turn in which the base cost of the cards played is far more than the Energy that turn could possibly
          have had (starting Energy + per-turn gains + carried over). Cost reductions and Energy gains are
          invisible to the server and real matches reach 6 over, so only a turn 10 or more over, or three turns
          5 or more over, is a flag: the "set my Energy to 99" cheat, not a subtle one. A player's turns stop being
          counted once they play a card that lowers costs or gives Energy back (COST_CHANGERS).
Friend challenges (rooms without a mode in the lobby name) play under rules the record does not carry: only the
deck check runs there, against the mode the deck size points to.
"""
import re

PLAY, END_TURN = 5, 14
MANA_SLACK = 3           # cost reductions and Energy gains the server cannot see: this much over the bound is normal...
MANA_WILD = 10           # ...this much over in one turn is not (real matches reach +6 with cost tricks; a memory edit shows +20 or more)
MANA_REPEAT = 3          # or this many turns at least 5 over

# Cards that lower other cards' cost or give Energy back (from the cards' texts, 2026-10-22). The small ones fit in
# MANA_SLACK, the big ones do not (Reclaim the Stars: draw 5, each 5 cheaper - a legal turn read as 20 Energy with 6),
# so once a player plays one, the mana check stops counting that player's turns from that turn on.
COST_CHANGERS = frozenset((
    "Fire and Fade", "Webway Gate", "Wild Host", "Reclaim the Stars", "Faeburn Vanquisher", "Minka Lesk", "Astropath",
    "Tech-Priest Enginseer", "Cadian Castellan", "Maelon Dhrost", "Full Throttle", "Munitorum Supplies",
    "Armoured Squadron", "At All Costs", "High Command", "Venomcrawler", "Iskandar Khayon", "Throne of the Heretic",
    "Company Master", "Inner Circle", "Master of Manoeuvre", "Convoke the Circle", "Obscure Ritual", "Plasma Generator",
    "Desperate Mission", "Winged Daemon Prince", "Decadent Throne", "Atalan Leader", "Metamorph Iconbearer",
    "Metamorph Leader", "Alien Idol", "Rusted Vent", "Eve of the Uprising", "Lying in Wait", "Living Icon",
    "Roaming Outriders", "Rampant Infestation", "Divination Menhir", "Implacable Conqueror", "Mekboy Gazmek",
    "Beastboss on Squigosaur", "Weirdboy", "Battlewagon", "Extractor Rig", "Da Green Horde", "Thundering Stampede",
    "Fasta Than Yooz", "Guidance of the Saints", "Imperial Creed", "Emperor's Judgement", "Njal Stormcaller",
    "Hrolf the Ironhowl", "High King of Fenris", "Trail Shaper", "Piranha", "Aun'Shi", "Emergency Dispensation",
    "Saviour Protocols", "Mawloc", "Winged Tyrant", "Digestion Pool", "Brood Progenitor", "Overwhelming Swarm",
    "Hive Fleet Leviathan", "Sergeant Telion", "Hive Factory", "Tactical Insight", "Fall Back", "Greatest Deeds",
    "Rapid Deployment", "Battlefield Supremacy", "Armoured Offensive", "Gather the Company"))
# cards that get cheaper by themselves during the match ("costs 1 less for each..."): counted as free
SELF_REDUCING = frozenset(("Fate Inescapable", "Mechanised Infantry", "Vengeance for Cadia", "Unholy Smite", "Patriarch",
                           "Fenrisian Wolfpack", "Burgeoning Empire", "Norn Emissary", "Avenging Zeal"))


def _rules_for(lobby, deck_sizes, rules):
    """(event id, rules) for the match: from the lobby name, else the mode whose deck size matches."""
    m = re.search(r"#([A-Za-z0-9_.-]+)@", lobby or "")
    if m and m.group(1) in rules:
        return m.group(1), rules[m.group(1)]
    for size in deck_sizes:
        for eid, gv in rules.items():
            if gv.get("deckSize") == size and not (gv.get("testersOnly") is True):
                return eid, gv
    return None, None


def check_deck(deck, gv, carddata, long_copies=None, long_game=False):
    """Problems with one deck list (SerializedPlayerDeckData as the game sent it), as short texts."""
    out = []
    if not isinstance(deck, dict):
        return ["deck list unreadable"]
    cards = deck.get("cardLibrary")
    warlord = (deck.get("deckWarlord") or {}).get("id") if isinstance(deck.get("deckWarlord"), dict) else None
    if not isinstance(cards, list):
        return ["deck list has no cards"]
    size = gv.get("deckSize") if gv else None
    if size and len(cards) != size:
        out.append("%d cards, the mode plays %d" % (len(cards), size))
    w = carddata.get(warlord) if isinstance(warlord, str) else None
    if not w:
        out.append("unknown warlord %r" % (warlord,))
    elif w.get("kind") != "hero" or not w.get("collection"):
        out.append("warlord %s is not a playable warlord" % w.get("name", warlord))
    army = w.get("army") if w else None
    counts, legendaries, bad = {}, 0, []
    custom_ok = bool(gv) and gv.get("customCards") is True
    for c in cards:
        cid = c.get("id") if isinstance(c, dict) else None
        if isinstance(cid, str) and cid.startswith("CUSTOM-"):
            # a custom card (the tester bundle): only a mode with "customCards" may hold one
            if not custom_ok:
                bad.append("%s is a custom card, not allowed in this mode" % cid)
                continue
        d = carddata.get(cid) if isinstance(cid, str) else None
        if not d:
            if isinstance(cid, str) and cid.startswith("CUSTOM-") and custom_ok:
                counts[cid] = counts.get(cid, 0) + 1          # not in the card data yet: counted, otherwise unchecked
                continue
            bad.append("unknown card %r" % (cid,))
            continue
        if d.get("kind") not in ("unit", "tactic") or not d.get("collection"):
            bad.append("%s cannot be in a deck" % d.get("name", cid))
        if army is not None and d.get("army") != army:
            bad.append("%s is not of the warlord's army" % d.get("name", cid))
        counts[cid] = counts.get(cid, 0) + 1
        if d.get("rarity") == 4:
            legendaries += 1
    out.extend(sorted(set(bad))[:8])
    if gv:
        for cid, n in counts.items():
            d = carddata.get(cid) or {}
            rarity = d.get("rarity")
            if long_game and long_copies:
                allowed = long_copies.get(rarity)
            else:
                allowed = gv.get("numberOfCopiesLegendary") if rarity == 4 else gv.get("numberOfCopiesOtherRarities")
            if isinstance(allowed, int) and n > allowed:
                out.append("%d copies of %s (at most %d)" % (n, d.get("name", cid), allowed))
        if isinstance(gv.get("maxLegendaries"), int) and legendaries > gv["maxLegendaries"]:
            out.append("%d legendaries (at most %d)" % (legendaries, gv["maxLegendaries"]))
    return out[:10]


def _numbering(relay):
    """In-match card number -> (owner player id, card id) for the two decks, or None when the order is unknown."""
    first = relay.get("goesFirst")                      # the server's coin (2026-10-19 on); without it the order is a guess
    players = relay.get("players") or {}
    if not first or first not in players or len(players) != 2:
        return None
    second = next(p for p in players if p != first)
    table, n = {}, 0
    for pid in (first, second):
        deck = players[pid].get("deck")
        if not isinstance(deck, dict) or not isinstance(deck.get("cardLibrary"), list):
            return None
        table[n] = (pid, (deck.get("deckWarlord") or {}).get("id") if isinstance(deck.get("deckWarlord"), dict) else None)
        n += 1
        for c in deck["cardLibrary"]:
            table[n] = (pid, c.get("id") if isinstance(c, dict) else None)
            n += 1
    return table


def check_match(relay, lobby, room_name, carddata, rules, long_copies=None):
    """[{"player": id, "kind": "deck" | "card" | "mana", "detail": text}] - one per player and kind at most."""
    flags = []
    players = relay.get("players") or {}
    decks = {pid: p.get("deck") for pid, p in players.items()}
    sizes = sorted({len(d["cardLibrary"]) for d in decks.values() if isinstance(d, dict) and isinstance(d.get("cardLibrary"), list)})
    eid, gv = _rules_for(lobby, sizes, rules)
    long_game = bool(eid) and eid.lower().find("long") >= 0
    matchmade = bool(re.search(r"#([A-Za-z0-9_.-]+)@", lobby or ""))

    for pid, deck in decks.items():
        if deck is None:
            continue
        problems = check_deck(deck, gv or {}, carddata, long_copies, long_game)
        if problems:
            flags.append({"player": pid, "kind": "deck", "detail": ("%s: " % eid if eid else "") + "; ".join(problems)})

    # a deck that is not what the mode allows makes the card numbering (and everything below) unreliable; the deck
    # flag is the evidence then
    table = _numbering(relay) if not flags else None
    if table:
        for pid, p in players.items():
            others = []
            for num, mv in sorted((p.get("moves") or {}).items(), key=lambda kv: kv[0] if isinstance(kv[0], int) else -1):
                if mv.get("typ") != PLAY:
                    continue
                act = mv.get("act")
                if isinstance(act, int) and act in table and table[act][0] != pid:
                    d = carddata.get(table[act][1]) or {}
                    others.append("move %s: %s" % (num, d.get("name", table[act][1])))
            if others:
                flags.append({"player": pid, "kind": "card", "detail": "played cards of the opponent's deck: " + "; ".join(others[:5])})

    if table and gv and matchmade:
        first = relay.get("goesFirst")
        for pid, p in players.items():
            start = gv.get("startingMana", 1) if pid == first else gv.get("startingManaSecond", gv.get("startingMana", 1))
            per_turn, carry = gv.get("manaPerTurn", 1), gv.get("manaAccumulation", 0)
            if not all(isinstance(x, int) for x in (start, per_turn, carry)):
                continue
            moves = [mv for _, mv in sorted((p.get("moves") or {}).items(), key=lambda kv: kv[0] if isinstance(kv[0], int) else -1)]
            turn, spent, over = 1, 0, []
            def bound(t):
                return start + (t - 1) * per_turn + carry + MANA_SLACK
            changed = False                 # a cost changer was played: this turn and the rest are not counted
            for mv in moves:
                if mv.get("typ") == PLAY:
                    act = mv.get("act")
                    if isinstance(act, int) and act in table and table[act][0] == pid:
                        d = carddata.get(table[act][1]) or {}
                        if d.get("name") in COST_CHANGERS:
                            changed = True
                        elif isinstance(d.get("cost"), int) and d.get("name") not in SELF_REDUCING:
                            spent += d["cost"]
                elif mv.get("typ") == END_TURN:
                    if changed:
                        break
                    if spent > bound(turn):
                        over.append((turn, spent, bound(turn) - MANA_SLACK))
                    turn, spent = turn + 1, 0
            if not changed and spent > bound(turn):
                over.append((turn, spent, bound(turn) - MANA_SLACK))
            if sum(1 for _, s, b in over if s - b >= 5) >= MANA_REPEAT or any(s - b >= MANA_WILD for _, s, b in over):
                flags.append({"player": pid, "kind": "mana",
                              "detail": "%s: " % eid + "; ".join("turn %d: cards costing %d with at most %d Energy" % o for o in over[:5])})
    return flags
