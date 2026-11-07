"""Turn a Warhammer 40k Warpforge match recording (BattleRecordData JSON) into a readable move list.

Pure Python, no game needed. Used by the Discord bot's "Moves" button on shared replays (playfab_emu.py).
What is exact: the deck cards' numbers (the game numbers both decks in order before shuffling), who went first, the
turns, and every card in recordings made with mod 0.13.39 or later (they carry the game's own number table,
"revivalCards"). Older recordings: talent cards and cards made before turn 1 are worked out by counting, other cards
made during the match (tokens) are only described. Researched 2026-10-18 against 8 real recordings.

    lines = moves(record, names)                     # names: card id -> card name
    lines = moves(file_json, names, carddata=...)    # whole replay-code file also accepted

Card numbers (PlayerAction.act / .tget) are the game's in-match card numbers
(CardScript.uniqueInGameID, handed out 0,1,2,... by BattleCardManager.RegisterCardInPlay as cards are created).
"""
import json

# BattleActionType (game enum; PlayerAction.typ uses it)
TYP = {1: 'attack', 5: 'playCardFromHand', 12: 'playHeroPower', 14: 'endTurn', 30: 'playActiveAbility',
       54: 'resign', 55: 'chatMessage', 76: 'useWaystone'}
CHAT = ['Intro', 'MirrorMatch', 'Bored', "I can't do that", 'Concede', 'Greetings', 'Threat', 'Well played',
        'Taunt', 'Sorry', 'Oops']
SPELL_SLOT = 4          # tactics (spells) are always recorded at slot 4 (pos % 10)


def _record(x):
    if 'kept' in x or 'info' in x:
        r = (x.get('kept') or {}).get('record') or x.get('record')
        return r, x.get('info') or {}, (x.get('kept') or {}).get('revivalCards') or x.get('revivalCards')
    return x, {}, None


def _first_side(r):
    """'P' or 'E': who took turn 1.  forceFirst 1/2 is explicit; otherwise the side whose log starts earlier."""
    if r.get('forceFirst') == 1:
        return 'P'
    if r.get('forceFirst') == 2:
        return 'E'
    ts = {s: min([a['ts'] for a in r.get(k) or [] if a['typ'] not in (54, 55)] or [10 ** 9])
          for s, k in (('P', 'playerLog'), ('E', 'enemyLog'))}
    return 'P' if ts['P'] <= ts['E'] else 'E'


def _turns(r, first):
    """Each log split at its endTurn actions; turns alternate, first player first.  Chat/resign are kept aside."""
    def segs(log):
        out, side = [[]], []
        for a in log or []:
            if a['typ'] in (54, 55):
                side.append(a)
                continue
            out[-1].append(a)
            if a['typ'] == 14:
                out.append([])
        if not out[-1]:
            out.pop()
        return out, side
    P, pside = segs(r.get('playerLog'))
    E, eside = segs(r.get('enemyLog'))
    A, B = (P, E) if first == 'P' else (E, P)
    second = 'E' if first == 'P' else 'P'
    turns = []
    for k in range(max(len(A), len(B))):
        if k < len(A):
            turns.append((first, A[k]))
        if k < len(B):
            turns.append((second, B[k]))
    extras = [('P', a) for a in pside] + [('E', a) for a in eside]
    return turns, extras


class Cards:
    """In-match card number -> (card id or None, owner 'P'/'E'/None, kind, note)."""

    def __init__(self, r, first, carddata, revival_cards):
        self.t = {}
        self.exact = False
        data = {'P': r['playerData'], 'E': r['oppData']}
        second = 'E' if first == 'P' else 'P'
        self.first, self.second = first, second
        cd = carddata or {}
        # 1) decks: first player's warlord, then its cardLibrary in order; then the same for the second player
        #    (BattleManager.SetupBoardPhase -> SetupPlayerDeckAndInitialTroops(goesFirst) then (!goesFirst);
        #     CreateDeck creates the cards in list order, the shuffle happens after the numbers are given)
        n = 0
        for s in (first, second):
            self.t[n] = (data[s]['deckWarlord']['id'], s, 'warlord', None)
            n += 1
            for c in data[s]['cardLibrary']:
                self.t[n] = (c['id'], s, 'deck', None)
                n += 1
        self.n = n
        self.cd = cd
        self.candidates = {}
        self.stale = False
        if revival_cards and revival_cards.get('cards'):
            # mod >= 0.13.39 records the game's own table: [number, card id, 1 = recording player's card]
            table = {num: (cid, own) for num, cid, own in revival_cards['cards']}
            # Mods up to 0.15.19 did not empty the table between matches when the previous match was not saved,
            # so a number this match reused kept the previous match's card (2026-11-06: an Astra Militarum vs
            # Tyranids replay named Space Wolves cards). The table's deck numbers then disagree with this
            # recording's own decks: trust none of it blindly; its later numbers are only candidates, accepted
            # in infer_turns when the player who used the card plays that card's faction.
            deck = [num for num in range(n) if num in table]
            stale = bool(deck) and sum(table[num][0] != self.t[num][0] for num in deck) * 2 > len(deck)
            if not stale:
                for num, (cid, own) in table.items():
                    if num not in self.t:
                        self.t[num] = (cid, 'P' if own else 'E', 'table', None)
                self.exact = True
                self.counter = max(self.t) + 1
                return
            self.candidates = {num: cid for num, (cid, own) in table.items() if num >= n}
            self.stale = True
        # 2) cards made before turn 1 (inferred, see README):
        envP, envE = r.get('playerEnviromentalCard'), r.get('enemyEnviromentalCard')
        pre = []
        if envP or envE:
            own_env, other_env = (envP, envE) if second == 'P' else (envE, envP)
            if own_env:
                pre.append((own_env, second, 'defence card'))
            if other_env and not (cd.get(other_env, {}).get('name', '').startswith('Normal Conditions')):
                pre.append((other_env, second, 'environment card'))
        else:
            d = data[second].get('defensiveCard') or cd.get(data[second]['deckWarlord']['id'], {}).get('goSecond')
            if d:
                pre.append((d, second, 'defence card'))
        for s in (first, second):
            st = cd.get(data[s]['deckWarlord']['id'], {}).get('start')
            if st:
                pre.append((st, s, 'starting card'))
        for cid, s, kind in pre:
            self.t[n] = (cid, s, kind, None)
            n += 1
        self.counter = n
        self.talents = {s: cd.get(data[s]['deckWarlord']['id'], {}).get('talent') or [] for s in 'PE'}
        self.warlord = {s: data[s]['deckWarlord']['id'] for s in 'PE'}

    def infer_turns(self, turns):
        """Talent cards: the active player gets a fresh (ephemeral) talent card at the start of every turn.
        Other numbers above that are cards made during play (tokens, created cards) - not named without
        simulating the game."""
        if self.exact:
            return
        refs = {}          # number -> list of (turn index, side, role)
        for ti, (side, acts) in enumerate(turns):
            for a in acts:
                if a['typ'] == 14:
                    continue
                if a['act'] >= self.n or a['typ'] in (1, 5, 76):
                    role = 'spell' if (a['typ'] == 5 and a['pos'] % 10 == SPELL_SLOT) else 'unit'
                    refs.setdefault(a['act'], []).append((ti, side, 'act-' + role))
                if a['tgtd']:
                    refs.setdefault(a['tget'], []).append((ti, side, 'target'))
        unknown = {k: v for k, v in refs.items() if k not in self.t}
        if self.candidates:
            army = lambda cid: (self.cd.get(cid) or {}).get('army')
            sides = {s: army(self.warlord[s]) for s in 'PE'}
            for k, v in list(unknown.items()):
                cid = self.candidates.get(k)
                a = army(cid) if cid else None
                if a is None:
                    continue
                users = {side for _, side, role in v if role.startswith('act')}
                if (users and all(sides[u] == a for u in users)) or (not users and a in sides.values()):
                    owner = next(iter(users)) if users else ('P' if sides['P'] == a else 'E')
                    self.t[k] = (cid, owner, 'table', None)
                    del unknown[k]
        # a talent is only ever played as a tactic, in the turn it was made, by the player whose turn it is
        talentish = {k for k, v in unknown.items()
                     if all(role == 'act-spell' for _, _, role in v) and len({ti for ti, _, _ in v}) == 1
                     and v[0][1] == turns[v[0][0]][0]}
        fixed_created = set(unknown) - talentish
        c = self.counter
        for ti, (side, acts) in enumerate(turns):
            cand = c
            while cand in fixed_created or cand in self.t:
                cand += 1
            played = sorted(k for k in talentish if unknown[k][0][0] == ti and k >= cand)
            tal = played[0] if played and played[0] - cand <= 3 else cand
            opts = self.talents.get(side) or []
            self.t[tal] = (opts[0] if len(opts) == 1 else None, side, 'talent', opts)
            c = tal + 1
            for k, v in unknown.items():
                if k >= c and any(t == ti for t, _, _ in v):
                    c = max(c, k + 1)
        for k, v in unknown.items():
            if k in self.t:
                continue
            owner = None
            for ti, side, role in v:
                if role.startswith('act'):
                    owner = side
                    break
                owner = 'E' if side == 'P' else 'P'
            kind = 'token' if any(role != 'act-spell' for _, _, role in v) else 'created card'
            self.t[k] = (None, owner, kind, None)

    def label(self, num, names, who, show_ids=False):
        e = self.t.get(num)
        nm = lambda cid: names.get(cid, cid)
        if e is None:
            s = f'card #{num}'
        else:
            cid, owner, kind, note = e
            if cid:
                s = nm(cid)
                if kind == 'talent':
                    s += ' (talent)'
            elif kind == 'talent':
                opts = note or []
                s = (f"{who.get(owner, owner)}'s talent card" +
                     (f" ({' / '.join(nm(o) for o in opts)})" if opts else ''))
            else:
                s = f"{kind} #{num}" + (f" ({who.get(owner, owner)}'s)" if owner else '')
        if show_ids:
            s += f' [#{num}]'
        return s


def moves(record, names, carddata=None, show_ids=False, player_names=None):
    """record: BattleRecordData dict (or the whole replay-code file).  names: card id -> card name.
    carddata (optional): card id -> {'talent': [ids], 'start': id, 'goSecond': id, 'choices': [ids]}
    Returns a list of text lines."""
    r, info, rc = _record(record)
    first = _first_side(r)
    who = dict(player_names or {})
    who.setdefault('P', info.get('ownName') or 'Player')
    who.setdefault('E', info.get('enemyName') or ('AI' if r.get('oppId') == 'AI' else 'Opponent'))
    turns, extras = _turns(r, first)
    cards = Cards(r, first, carddata, rc)
    cards.infer_turns(turns)
    L = lambda n: cards.label(n, names, who, show_ids)
    cd = carddata or {}

    out = [f"{who['P']} ({names.get(r['playerData']['deckWarlord']['id'], r['playerData']['deckWarlord']['id'])}) vs "
           f"{who['E']} ({names.get(r['oppData']['deckWarlord']['id'], r['oppData']['deckWarlord']['id'])}); "
           f"{who[first]} goes first"]
    if cards.stale:
        out.append("(This recording's card list is partly from an earlier match: cards made during the match, such as "
                   "tokens, may not be named.)")
    elif not cards.exact:
        out.append("(An older recording: cards made during the match, such as tokens, may not be named.)")
    for s, key in (('P', 'playerMulligan'), ('E', 'enemyMulligan')):
        if r.get(key):
            out.append(f"Mulligan – {who[s]} sent back " + ', '.join(L(x) for x in r[key]))

    # chat/resign go in by timestamp
    seq = []
    for ti, (side, acts) in enumerate(turns):
        for a in acts:
            seq.append((a['ts'], ti, side, a))
    for side, a in extras:
        ti = max([t for ts, t, _, _ in seq if ts <= a['ts']] or [0])
        seq.append((a['ts'], ti, side, a))
    seq.sort(key=lambda x: (x[1], x[0]))
    for ts, ti, side, a in seq:
        typ = a['typ']
        p = f"Turn {ti + 1} – "
        if typ == 5:
            slot, choice = a['pos'] % 10, a['pos'] // 10
            line = f"{who[side]} played {L(a['act'])}"
            if a['tgtd']:
                line += f" targeting {L(a['tget'])}"
            if slot != SPELL_SLOT or (cards.t.get(a['act']) and cd.get(cards.t[a['act']][0] or '', {}).get('type') == 'unit'):
                line += f" (slot {slot})"
            opts = cd.get((cards.t.get(a['act']) or (None,))[0] or '', {}).get('choices')
            if choice or opts:
                line += f" (choice {choice}" + (f": probably {names.get(opts[choice], opts[choice])}" if opts and choice < len(opts) else '') + ')'
            out.append(p + line)
        elif typ == 1:
            out.append(p + f"{L(a['act'])} attacked {L(a['tget'])}" + {1: ' (melee)', 2: ' (ranged)'}.get(a['at'], ''))
        elif typ == 14:
            out.append(p + f"{who[side]} ended the turn")
        elif typ == 54:
            out.append(p + f"{who[side]} conceded")
        elif typ == 55:
            m = a['tget']
            out.append(p + f"{who[side]} said \"{CHAT[m] if 0 <= m < len(CHAT) else m}\"")
        elif typ == 76:
            out.append(p + f"{who[side]} used a Waystone on {L(a['act'])}")
        elif typ in (12, 30):
            out.append(p + f"{who[side]} used the ability of {L(a['act'])}" + (f" on {L(a['tget'])}" if a['tgtd'] else ''))
        else:
            out.append(p + f"{who[side]} {TYP.get(typ, 'action %d' % typ)} act={L(a['act'])}"
                       + (f" target={L(a['tget'])}" if a['tgtd'] else '') + f" pos={a['pos']}")
    return out


if __name__ == '__main__':
    # python replay_moves.py <folder of replay-code files> <card data json>
    import glob, os, sys
    src, carddata = sys.argv[1], json.load(open(sys.argv[2], encoding='utf-8'))
    names = {k: v['name'] for k, v in carddata.items() if v.get('name')}
    for f in sorted(glob.glob(os.path.join(src, '*.json'))):
        d = json.load(open(f))
        print('=' * 20, os.path.basename(f))
        for line in moves(d, names, carddata):
            print(line)
        print()
