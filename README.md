# Warpforge Revival server

A self-hosted server that lets *Warhammer 40,000: Warpforge* be played again now that the game's
own servers are closed. Players connect with the
[Warpforge Revival mod](https://github.com/silentanarchist/warpforge-revival-mod).

This is an unofficial fan project. It is not affiliated with or endorsed by Everguild or
Games Workshop. It contains no game files.

**About AI use.** The code and documentation in this repository were written largely by an AI
assistant (Anthropic's Claude), working under the maintainer's direction; the maintainer decided
what to build and tested it with the game. It has had no independent security review. It is meant
for a small community of people who know each other: read the code before you put it on the open
internet, and please report what you find.

Python 3.9 or newer, standard library only. One process, one port.

## What is in it

| File | What it does |
|---|---|
| `warpforge_server.py` | The web server: card pack, files for the mod, mod updates, the card creator page. Start this one. |
| `playfab_emu.py` | Stands in for the game's backend: sign-in, player data, decks, game modes, friends, chat, battle log, win/loss record, and the card creator's accounts and review flow. Reloads itself when the file changes. |
| `photon_emu.py` | Stands in for the match service: rooms, matchmaking, relaying each player's actions, rejoining after a dropped connection. Shares the web server's port. |
| `content/files/creator.html` | The card creator site. Each tab has its own address (`/creator`, `/my-cards`, `/review`, `/people`, `/examples`, `/keywords`, `/artwork`, `/rules`, `/profile`), sign-in is at `/login`, and the site root sends visitors there. Server status moved from `/` to `/api/v1/status` only. |
| `content/files/revival-settings.example.json` | Settings every player's mod reads from the server. |
| `content/liveops/GameModes.json` | The game modes and their rules (deck size, energy, turn limits). |
| `content/titledata/` | Configuration the game asks for at start-up. |
| `content/files/text-terms.json` | Wording for game texts that are missing. |
| `tools/extract_card_art.py`, `tools/extract_card_art.bat` | Extract the official card pictures from your copy of the game for the card creator's preview (see below). |

## Running it

```
python warpforge_server.py --port 8780
```

On Windows, `start_server.bat` does the same. Open TCP port 8780 on your firewall and router.
Check it is up at `http://your.server.address:8780/api/v1/status`.

Copy `content/files/revival-settings.example.json` to `content/files/revival-settings.json` and
edit it. Players then set `ServerUrl` in their `UserData\WarpforgeRevival.cfg` to your address.

Folders the server creates as it runs, none of which belong in a repository:
`data/` (players, accounts, chat), `logs/`, and `mod/`.

### Mod updates for players

Put the current mod builds in `mod/` and players are offered them as an update the next time
they start the game. There is one slot per kind of device:

| File in `mod/` | Offered to |
|---|---|
| `WarpforgeRevival.dll` | Windows players |
| `WarpforgeRevival.Android.dll` | Android players |

The server reads the version out of the file itself, so replacing a file is all it takes; no
restart. Versions are numbered together across devices: `x.y.z-w` for Windows, `x.y.z-a` for
Android ([Android mod and patcher](https://github.com/silentanarchist/warpforge-revival-mod-android)).

### Phones

Android players are served the Android build of the content
files from `content/bundles-android/` (the game's content files differ per platform; like
`content/bundles/` they are yours to supply).

### Match service

Matches run on this server (`photon_emu.py`), on the same port as everything else. It needs a
direct connection to that port, so it does not go through an https proxy (the server's own https
on the game port carries it, below). If the match service
should be reached on a different port, set `"matchPort"` in the settings file.

### https

**The card creator** takes passwords, so put it behind https. Any reverse proxy works; for
example Caddy with `reverse_proxy 127.0.0.1:8780` for your domain.

**The game** (sign-in, decks, chat, card and mod downloads) is encrypted on the game port itself
when the server can find a certificate for your domain. The port then answers three things at
once: https, plain http, and match connections. Players change nothing: from mod 0.11.18 a
server address written without `http://` is tried over https first.

The server does not obtain certificates; it reads the files something else keeps up to date and
checks every few minutes for renewed ones:

- With Caddy running for the creator site on the same machine and under the same user, nothing
  needs setting up: Caddy's certificate folder is found by itself.
- Otherwise start the server with `--certs FOLDER` (any `name.crt` with `name.key` beside it,
  in PEM form) or `--cert FILE --key FILE`.

The server's first lines say whether encryption is on, and `/api/v1/status` reports
`"encrypted": true` when asked over https. Three things to know:

- Once a player's mod has reached a server over https it refuses to drop back to plain http
  for that server. If your certificate expires, players cannot connect until it is renewed.
- A server reached by number address (a home network) has no certificate and stays on plain http.
- Match connections are encrypted too, from mod 0.11.27. The game's match client only speaks
  plain TCP, so the mod points it at a listener inside the game (127.0.0.1) and carries each
  connection on to the game port inside https; the server unwraps it for the match service.
  `/api/v1/status` reports `"encryptedMatches": true` when the server can do this, and the mod
  only does it then. Older mods, and servers without a certificate, keep plain match connections.

Once your players are on mod 0.11.18 or newer, set `"requireHttps": true` in the settings file:
plain http from outside this machine and its home network is then refused, except what an
older mod needs to find and download its update. Match connections are not affected by it:
once your players are on mod 0.11.27 or newer, `"requireEncryptedMatches": true` refuses
unencrypted match connections from outside this machine and its home network.

### Keeping your home address private (relay)

`relay/` sets up a small rented Linux server as the public address: it passes connections on,
unopened, through a WireGuard tunnel to the PC running the server, so the home network needs
no open ports. See `relay/README.txt`. The relay starts each connection with a PROXY protocol
line naming the player's address; list the relay's tunnel address in the settings file so the
server believes it (`"relays": ["10.66.0.1"]`), and let Caddy read it too:

```
{
	servers {
		listener_wrappers {
			proxy_protocol {
				allow 10.66.0.1/32
			}
			tls
		}
	}
}
```

## What is not here, and why

**Game content is not included.** The card data, asset bundles, and the files generated from
them (the card list and effect list the creator page uses) are the publisher's, so they are left
out and listed in `.gitignore`. Without them:

- the server runs, and players can sign in, chat and be matched;
- the card creator page has no menus to show until `content/files/creator-schema.json` and
  `content/files/creator-cards.json` are generated from your own copy of the game;
- cards the mod should add or change come from `content/cards/*.json`, which you supply.
- the creator's card preview shows an official card's picture when a design borrows its art only
  if `content/files/card-art.zip` is there (see "Card art for the creator" below). Any folder
  under `content/files/` can be kept as a zip of the same name this way.

### Card art for the creator

When a design borrows an official card's art, the card creator's preview shows that card's
picture if the server has it. The pictures come from your own copy of the game:

1. Have the Windows game installed (Steam) and the server's card data in place
   (`content/bundles/allcards_assets_all.bundle`).
2. Install [Python](https://www.python.org/) 3.9 or newer.
3. Double-click `tools/extract_card_art.bat`. It finds the game in Steam's usual folder (or asks
   where it is), installs UnityPy and Pillow the first time, and writes
   `content/files/card-art.zip`: one 384 x 384 JPEG per card, named by card id, about 25 MB.

Or run it yourself: `pip install UnityPy Pillow`, then
`python tools/extract_card_art.py --game "path\to\Warhammer 40k Warpforge"`. Options:
`--cards` (another card data bundle), `--out` (another zip), `--size` and `--quality`.

The server serves `files/card-art/<id>.jpg` straight out of the zip; no restart is needed when
it is replaced. Run it again after the card data changes (new cards). The pictures are the
game's own art: the zip is listed in `.gitignore`; never publish it.

The card creator turns a design into the game's own card layout. Which parts of a card's data
each effect uses was taken from the game's code, and the result is checked by rebuilding every
official card; the lists it works from (`content/creator-native.json` and the two files above)
are generated from the game and so are not in this repository either.

## Keeping old and new versions apart, and other mods out

Three optional settings in `revival-settings.json`:

- `"minModVersion": "latest"` - sign-in is refused to any mod older than the build this server
  is handing out from `mod/` (so publishing a build makes everyone update before they connect).
  A fixed number such as `"0.11.17"` works too. Windows and Android builds share version numbers.
  Leave it out to accept every version.
- `"onlyRevivalMod": true` - sign-in is refused when the game reports any other mod or plugin
  loaded, and when it runs the version this server hands out but its mod file is not that file.
  Needs mod 0.11.17 or newer; older mods are refused while this is on.
- `"rulesVersion": "1"` - your own label for the cards and rules this server serves. Players are
  only matched with players on the same label and the same mod rules number. Change it when you
  change cards or rules.

What a game says about itself can be faked by someone who rewrites the mod. These settings keep
honest players on one set of rules and stop casual tampering; they are not proof against cheats.

## Accounts and safety

- Keep `data/` and `logs/` private: they hold your players' accounts, decks and chat. Both are in
  `.gitignore`; never publish or share them.
- Use https for the card creator and for the game (above).
- The logs are kept short and free of sign-in details. `logs/playfab.jsonl` (every game request)
  never records passwords, sessions or uploaded pictures, records a device id or Steam ticket
  only as a short fingerprint, and drops entries older than `"logDays"` in
  `revival-settings.json` (14 if left out). `logs/photon.log` (match connections, with the
  address each came from) starts a new file each night and keeps 14. An older log is cleaned the
  first time the server runs with this version.
- **Back up `data/`.** `maintenance/setup-nightly-backup.bat`, run once on the server PC, makes
  a first backup and schedules one every night at 4:12: a dated zip of `data/` (and the live
  settings file) in `C:\WarpforgeRevival-backups`, checked after writing, with a line per night
  in `backup-log.txt`. Old backups are kept. Pick a different drive than the server's if you can:
  `python maintenance/backup-data.py D:\somewhere` makes one by hand. To restore, stop the
  server, unzip a backup's `data` folder over `data/`, and start it again.
- Game sessions are kept in `data/sessions.json` as fingerprints, so a restart does not sign
  players out; a session not used for 30 days stops working.
- Requests are limited per address (size, time to arrive, requests per minute, wrong passwords,
  new accounts), and chat needs a signed-in player. Behind a proxy on the same machine the
  address is taken from `X-Forwarded-For`.
- **Requiring an account.** With `"requireAccount": true` in `revival-settings.json`, the game
  can only sign in with an account from the creator site. From mod 0.11.19 the game shows a
  sign-in window (name and password, or create an account) and keeps a game login in
  `UserData/WarpforgeRevival/game-login.txt`, so it only asks once per computer or phone. An
  account's first game sign-in gets a new game player of its own, the same on every computer and
  phone it signs in from. A player from before accounts were required keeps its progress only if
  the owner links it to the account (set the account's `"player"` in `data/creator_users.json`).
  The account and its game player are then one thing: the game's account page offers **Sign
  out** (that computer or phone only) instead of unlinking, an admin's button on the People page
  signs the account out of the game everywhere, and neither takes the player away from the
  account. If an account ever loses its link, its next game sign-in finds the player made for it
  before it would make a new one. Changing the password or banning the account cancels every
  saved game login. Recommended for any server open to people you do not know.
- **Forgotten passwords.** There is no email. Each player can make a recovery code on their
  Profile page (their password is asked again); it is shown once and downloaded as a text file
  for them to keep. "Forgot your password?" on the sign-in page takes the username, the code and a
  new password. A code works once, making a new one cancels the old one, the server keeps only a
  fingerprint of it, and using it signs the account out of every browser, computer and phone.
  Wrong codes are limited per address and per account name.
- **Bans.** Banning an account on the People page also bans its linked game player. Players
  without a site account can be banned on their own on the same page (kept in `data/bans.json`).
  A banned player cannot sign in, and a running game stops working at its next request.
- **Word filter.** Blocked words are starred out in chat and refused in player and account
  names. The starting list is `content/word-filter.txt`; once an admin saves the list on the
  People page the server uses that copy (`data/word_filter.json`). Chat messages also carry the
  sender's real name, whatever the game wrote in them.
- The game only signs in the ways it actually uses; the other sign-in methods of the original
  service are refused.
- This is a hobby server for a small community of people who know each other. Do not rely on it
  to keep strangers out, and do not reuse a password from anywhere else on it.

## Licence

MIT - see [LICENSE](LICENSE). The licence covers this server's code only, not the game.
