"""The relay's live referee for server-deal matches (2026-11-01; plan: claude/match-network-protocol.md).

Only rooms with the server deal on ("RevivalDeal" = 1: the testers' Server Deal mode and testers' server-deal
challenges) get one; every other room is relayed exactly as before.

The game has no match server: each game runs the whole match, and only the players' inputs travel between them as
PUN RPCs (event 200, {5: method index, 4: [arguments]}) through this relay. The referee reads each one as it passes
and keeps what can be known without running the game's rules:

- the phase of the match (start-up messages, then play) and whose turn it is (the server's coin for the first turn;
  every "end turn" move from the player whose turn it is hands it over - checked against 234 recorded matches: no
  real move came from the other player);
- each player's move numbers (a repeat with the same content is the game's own resend; a different move under a
  number already used is a conflict);
- the random table the room creator sends, against the server's own ("RevivalRng");
- each deck list as it is sent (the server's deck check runs on it at once, not after the match);
- resignations, desync messages, "match finished" messages, and who left the room for good.

It never changes a move. Two kinds of message are not passed on at all: "card seen" (the opponent hovers a hand card)
and "card reorder" (the opponent drags one): they only tell the other game which hand card is which. Everything else
it only notes: each finding is a "ref" line in the match record and a warning in the log, and the summary decides the
match's result together with the players' own reports (playfab_emu._ref_settle).
"""
import json
import threading
import time

# the game's RPC list, sorted by name ignoring case (playfab_emu.PUN_RPCS has the same list for after-match reading)
START_GAME, ACK_START, RNG, RNG_OK, DECK = 21, 6, 18, 11, 10
MULLIGAN, MULLIGAN_ACK, MULLIGAN_RESEND = 14, 16, 19
ACTION, RESEND, SEEN, REORDER, CHAT = 17, 20, 8, 7, 9
DESYNC, FORFEIT, FINISHED, WRONG_MODE, WRONG_ROOM = 12, 13, 15, 22, 23
NAMES = {6: "AckStartGame", 7: "CardReorder", 8: "CardSeen", 9: "ChatMessage", 10: "DeckData", 11: "ReceivedRandomNumbers",
         12: "EnemyDesync", 13: "EnemyForfeit", 14: "EnemyMulligan", 15: "MatchFinished", 16: "MulliganAck",
         17: "PlayerAction", 18: "RandomNumbers", 19: "RequestMulliganResend", 20: "ResendLostActions",
         21: "StartGame", 22: "WrongPlayMode", 23: "WrongRoom"}
START_ONLY = (START_GAME, ACK_START, RNG, RNG_OK, DECK)
NOT_PASSED_ON = (SEEN, REORDER)
# PlayerAction.typ (BattleActionType) values a player may send when it is not their turn
ANY_TURN = (54, 55)                     # resign, chat
END_TURN = 14
MOVE_KEYS = ("typ", "act", "tget", "tgtd", "pos", "num", "at")
MAX_FINDINGS = 60                       # per match, so a broken game cannot fill the record


class Referee(object):
    def __init__(self, room_name, players, first, creator, rng, mode, note, deck_check=None):
        """players: the two player ids; first: who the server's coin picked; creator: who opened the room;
        rng: the server's table as text ("12 845 ..."); mode: the server-deal mode's id (None: a challenge);
        note(kind, **fields): writes a line into the match record; deck_check(deck, mode) -> [problems]."""
        self.room = room_name
        self.players = list(players)
        self.first = first if first in self.players else None
        self.creator = None                 # who sent StartGame (the room's master client then); `creator` opened it
        self.opener = creator
        self.rng = [int(x) for x in str(rng or "").split() if x.isdigit()]
        self.mode = mode
        self._note = note
        self._deck_check = deck_check
        self.started = time.time()
        self.phase = "start"
        self.turn_of = None                 # the player whose turn it is (from the first move on)
        self.turns = 0
        self.moves = {p: {} for p in self.players}          # player -> {move number: move}
        self.rng_seen = {}                  # arrayIndex -> numbers, as the creator sent them
        self.decks = {}                     # player -> {"warlord", "cards"}
        self.passed_over = {"CardSeen": 0, "CardReorder": 0}
        self.forfeit_by = None
        self.desync_by = []
        self.finished_by = []
        self.left = []                      # players who left the room for good
        self.left_in_play = []              # ...of them, those who left before the match had ended
        self.findings = []
        self.claims = {}                    # player -> {"won": bool, "how": text} (playfab_emu fills it)
        self.settled = None                 # the result once decided (playfab_emu)
        self.closed = False
        note("ref", what="on", first=self.first, mode=mode)

    # ------------------------------------------------------------------ findings
    def _find(self, player, kind, detail):
        if len(self.findings) >= MAX_FINDINGS:
            return
        f = {"t": round(time.time() - self.started, 1), "player": player, "kind": kind, "detail": str(detail)[:300]}
        self.findings.append(f)
        try:
            self._note("ref", what="finding", p=player, kind=kind, detail=f["detail"])
        except Exception:
            pass

    # ------------------------------------------------------------------ messages
    def rpc(self, player, index, args):
        """One RPC from `player` (decoded by the relay). Returns False when it must not be passed on."""
        if player not in self.players:
            self._find(player, "stranger", "a message from someone who is not one of the two players")
            return True
        if index in NOT_PASSED_ON:
            self.passed_over[NAMES[index]] += 1
            return False
        if index == START_GAME:
            if self.phase != "start":           # (the relay starts a fresh referee for a rematch first)
                self._find(player, "phase", "StartGame after the match had begun")
            if self.creator is None:
                self.creator = player
        elif index in START_ONLY and self.phase != "start":
            self._find(player, "phase", "%s after the match had begun" % NAMES[index])
        if index == RNG:
            self._rng(player, args)
        elif index == DECK:
            self._deck(player, args)
        elif index == ACTION:
            self._action(player, args)
        elif index == FORFEIT:
            if self.forfeit_by is None and self.phase != "over":
                self.forfeit_by = player
                self._note("ref", what="resigned", p=player, turn=self.turns)
            self.phase = "over"
        elif index == DESYNC:
            if player not in self.desync_by:
                self.desync_by.append(player)
                self._note("ref", what="desync", p=player, turn=self.turns)
        elif index == FINISHED:
            if player not in self.finished_by:
                self.finished_by.append(player)
            self.phase = "over"
        elif index not in NAMES:
            self._find(player, "unknown", "RPC number %s" % index)
        return True

    def _rng(self, player, args):
        if self.creator and player != self.creator:
            self._find(player, "rng", "the random table sent by the player who did not start the match")
        if len(args) != 2 or not isinstance(args[0], int) or not isinstance(args[1], list):
            self._find(player, "rng", "a random table of the wrong shape")
            return
        self.rng_seen[args[0]] = [x for x in args[1] if isinstance(x, int)]
        sent = [n for i in sorted(self.rng_seen) for n in self.rng_seen[i]]
        if self.rng and sent != self.rng[:len(sent)]:
            self._find(player, "rng", "the random table sent is not the server's (%d numbers, first %s)" % (len(sent), sent[:5]))

    def _deck(self, player, args):
        if player in self.decks:
            self._find(player, "deck", "a second deck list")
            return
        try:
            deck = json.loads(args[0]) if args and isinstance(args[0], str) else None
        except ValueError:
            deck = None
        if not isinstance(deck, dict) or not isinstance(deck.get("cardLibrary"), list):
            self.decks[player] = {"warlord": None, "cards": 0}
            self._find(player, "deck", "a deck list the server cannot read")
            return
        w = deck.get("deckWarlord")
        self.decks[player] = {"warlord": w.get("id") if isinstance(w, dict) else None, "cards": len(deck["cardLibrary"])}
        if self._deck_check:
            # on a thread of its own: the relay holds its lock while it passes messages on, and the first check
            # after a start reads the card data from disk
            threading.Thread(target=self._check_deck, args=(player, deck), name="referee-deck", daemon=True).start()

    def _check_deck(self, player, deck):
        try:
            problems = self._deck_check(deck, self.mode) or []
        except Exception as e:
            problems = []
            self._find(player, "deck", "the deck check failed: %s" % e)
        if problems:
            self._find(player, "deck", "; ".join(str(p) for p in problems[:5]))

    def _action(self, player, args):
        if len(args) != 7 or not all(isinstance(a, (int, bool)) for a in args):
            self._find(player, "move", "a move of the wrong shape")
            return
        mv = dict(zip(MOVE_KEYS, args))
        num, typ = mv["num"], mv["typ"]
        mine = self.moves[player]
        if num in mine:
            if mine[num] != mv:
                self._find(player, "move", "move %d sent twice with different content" % num)
            return                          # the game's own resend: already counted
        last = max(mine) if mine else 0
        if num != last + 1:
            self._note("ref", what="gap", p=player, num=num, last=last)     # the other game asks for a resend
        if len(mine) < 5000:
            mine[num] = mv
        if self.phase == "start":
            self.phase = "play"
            if typ not in ANY_TURN:
                self.turn_of = self.first or player
                if self.first and player != self.first:
                    self._find(player, "turn", "the first move came from the player the server's coin did not pick")
                    self.turn_of = player
        # (a move after the match ended is not a finding: in 268 recorded matches a move already on its way, or
        # queued as the game ended it, arrived after the end messages several times)
        if typ in ANY_TURN:
            return
        if self.turn_of is None:
            self.turn_of = player
        if player != self.turn_of:
            self._find(player, "turn", "move %d (type %d) on the opponent's turn %d" % (num, typ, self.turns + 1))
            return
        if typ == END_TURN:
            self.turns += 1
            self.turn_of = next((p for p in self.players if p != player), player)

    # ------------------------------------------------------------------ the room
    def left_room(self, player):
        if player in self.players and player not in self.left:
            self.left.append(player)
            if self.phase != "over" and player not in self.left_in_play:
                self.left_in_play.append(player)
            self._note("ref", what="left", p=player, turn=self.turns, phase=self.phase)

    def back(self, player):
        if player in self.left:
            self.left.remove(player)
        if player in self.left_in_play:
            self.left_in_play.remove(player)

    def summary(self):
        return {"room": self.room, "players": list(self.players), "first": self.first, "mode": self.mode,
                "phase": self.phase, "turns": self.turns, "turnOf": self.turn_of,
                "moves": {p: len(m) for p, m in self.moves.items()}, "decks": dict(self.decks),
                "notPassedOn": dict(self.passed_over), "forfeitBy": self.forfeit_by, "desyncBy": list(self.desync_by),
                "finishedBy": list(self.finished_by), "left": list(self.left), "leftInPlay": list(self.left_in_play), "findings": list(self.findings),
                "claims": dict(self.claims), "settled": self.settled, "closed": self.closed}
