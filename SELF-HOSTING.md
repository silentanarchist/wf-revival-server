# Running your own WF Revival server

This guide takes you from an empty machine to a server your friends can play on. It is honest about
the rough parts: the code is all here, but the game's own content is not, and some of the files
you need have no ready-made tool yet. Those are marked **No tool yet** below, with what is known
about making them.

The [README](README.md) explains what each part of the server does and covers https, the relay,
backups and safety in more detail. This guide is the order to do things in.

> **You need your own copy of the game.** The server contains no game files, and nothing here
> gives you any. Every game file you put on your server comes from your own installed copy.

## What you need

- A PC or small server that stays on, Windows or Linux. Python **3.9 or newer**. The server itself
  uses only Python's standard library; a few of the content tools need `pip install UnityPy Pillow`.
- Open TCP ports on the firewall and router: **8780** for the game (the default), and **443** plus
  usually **80** for the website through Caddy (80 is how Caddy gets its certificate). Or use the
  relay in `relay/` if you don't want to open ports at home; it passes 8780 and 443 on.
- A domain name and a certificate if anyone outside your home network will play (strongly
  recommended: the website takes passwords). [Caddy](https://caddyserver.com/) does this for free.
- Warhammer 40,000: Warpforge on Windows (Steam), **version 1.35.0, with its downloaded content
  still in the cache** (see step 3).

## 1. Get the code

```
git clone https://github.com/silentanarchist/wf-revival-server
cd wf-revival-server
```

## 2. Settings

Copy `content/files/revival-settings.example.json` to `content/files/revival-settings.json` and
edit it. The ones to set on day one:

| Setting | Set it to | Why |
|---|---|---|
| `creatorUrl` | `https://your.domain/creator` | The website's address; the game links to it (bug reports, Discord linking). |
| `requireAccount` | `true` | Without it the server trusts whatever the game says about who you are. Leave it `false` only on a home network of people you know. |
| `accounts` | `{"autoApprove": false}` | Optional: every new account waits until an admin approves it on the People page. |
| `minModVersion` | `"latest"` | Players need at least the version you serve in `mod/` (step 4). No effect while `mod/` is empty. |
| `onlyRevivalMod` | `true` | Refuses games with other mods loaded. |
| `rulesVersion` | `"1"` | Your own label for your cards and rules; change it when you change them. |
| `requireHttps`, `requireEncryptedMatches` | `true`, once https works | Refuse unencrypted connections from outside your home network. |

The README's "Keeping old and new versions apart" and "Accounts and safety" sections explain the
rest. Once you are the founding owner (step 7), the website's **Server** page edits this file and
the game modes (`content/liveops/GameModes.json`) for you (it asks for your password again).

## 3. Game content

The server starts without any of these. Players can't play until the three PC bundles and
`collection.json` are in place, and don't open the server to players before `replay-carddata.json`
is there too. Put them where the table says (create a folder that doesn't exist yet, such as
`content/draft/`); none of them needs a restart.

### Needed to play

| File | What it is | How to get it | Tool |
|---|---|---|---|
| `content/bundles/allcards_assets_all.bundle` | Every card's data | Your PC game's download cache (below) | Copy by hand. **No tool yet** to find and rename it. |
| `content/bundles/localization_assets_all.bundle` | The game's texts | Same | Same |
| `content/bundles/alternateartstyles_assets_all.bundle` | Alternate card art styles | Same | Same |
| `content/bundles-android/` (the same 3 names) | The same files built for Android | From an Android device's cache, or converted from the PC files | **No tool yet in this repo** (a converter exists, see "Tools that don't exist yet") |
| `content/collection.json` | The cards every player owns (all of them) | Made from `allcards_assets_all.bundle` | **No tool yet in this repo** (see below) |
| `data/replay-carddata.json` | Each card's kind, cost, rarity and army, for the server's match checks and the Discord replay viewer | Made from `allcards_assets_all.bundle` | **No tool yet** |
| `content/draft/DraftPacks.json` | The game's ready-made draft packs | Made from the game's own draft packs data | **No tool yet** |

What happens without them:
- **No bundles:** the game can't load its cards. These files used to come from the game's own
  servers, which are gone, so every player gets them from yours (signed-in, approved accounts only).
- **No `collection.json`:** players own no cards and can't build decks.
- **No `replay-carddata.json`:** the match checks don't know any card, so **every finished match is
  reported as a suspicious deck** for both players on the Moderation page. Don't open the server to
  players without it.
- **No `DraftPacks.json`:** Draft mode offers no packs and can't be played.

**Finding the bundles in your cache.** The game downloaded these files once and keeps them under

```
%USERPROFILE%\AppData\LocalLow\Unity\<a folder with Everguild or Warpforge in its name>\
```

with one folder per file (`allcards_assets_all...`, `localization_assets_all...`,
`alternateartstyles_assets_all...`), a version folder inside each, and in that a file called
`__data`. That `__data` file is the bundle: copy it to `content/bundles/` under the name in the
table. The mod also keeps a second copy of the card and text files in the game folder's
`UserData\WarpforgeRevival\original-cache\`, in case the cache was cleared.

It must be the game's **final** version (1.35.0). Its card file holds **1,351 cards**, Space Wolves
included; an older cache holds fewer, and players on your server would then miss cards.

**Android bundles.** Phones need their own build of the same three files. They come either from an
Android device that ran the game before the shutdown (`Android/data/com.Everguild.WarhammerWarpforge/`
on the device; newer Android versions block reading it without a PC and adb), or converted from the
PC files: the converter marks the file as built for Android and re-encodes its pictures. Without them
the server works for PC players only.

**`collection.json`** looks like this. One entry per collectible card, each with `id`, `class` and
`copies`: warlords need `"class": "Hero"` (Draft picks its warlords by it), warlords and
Legendaries 1 copy, everything else 2. The file must be valid JSON: a broken one makes every
inventory request fail, rather than leaving players without cards.

```json
{"grantAll": true, "items": [{"id": "AM3", "class": "Card", "copies": 2, "name": "..."},
                             {"id": "AM1", "class": "Hero", "copies": 1, "name": "..."}]}
```

**`data/replay-carddata.json`** is `{card id: {"name", "kind" (unit, hero, tactic or whispers),
"cost", "rarity" (1-5), "army", "collection" (true for collectible cards), "talent", "start",
"goSecond", "choices"}}` for every card in the card file.

**`content/draft/DraftPacks.json`** is `{"packs": [{"id", "cards": [card ids], "army", "tier"
(Common, Epic or Legendary), "enabled": true}], "cardArmies": {card id: army number}}`. A pack is
only offered with `"enabled": true` (leaving it out switches the pack off), and its `army` is the
same number `cardArmies` uses. Draft also needs `content/files/creator-cards.json` (next table)
to offer one warlord per army.

### Needed for the website's Card Creator (optional)

Without these the Card Creator has no menus and saved designs can't be turned into the game's
card layout. `creator-cards.json` is also used outside the creator: by deck import and by Draft's
warlord choice.

| File | What it is | Tool |
|---|---|---|
| `content/files/creator-schema.json` | Every menu in the editor (triggers, effects, keywords...) | Card-creator kit, not published yet |
| `content/files/creator-cards.json` | The official card list for pickers and deck import | Same |
| `content/creator-native.json` | The game's card structures and enum numbers the card compiler fills in | Same |
| `content/creator-examples.json` | Official example cards per keyword (Examples page) | Same |
| `content/creator-official.json` | Admin > Card viewer, and card names in Discord replay posts | Same, plus a text file nothing makes yet |
| `content/unreleased-cards.json` | Admin > Unreleased cards | Made by hand from the game's texts; can be left out |

### Other optional files

| File | What it does | How to get it |
|---|---|---|
| `content/files/localization/` | The website, the guides, the mod and the Patcher app in 13 languages | Copy the [mod repository's](https://github.com/silentanarchist/wf-revival-mod) `localization/` folder as it is. Without it everything shows in English. |
| `content/files/news.json` | News on the game's Social > Community tab | Write it yourself: `{"discord": "https://discord.gg/...", "items": [{"date": "2026-10-22", "title": "...", "text": "...", "link": "..."}]}`, newest first |
| `content/files/art-example.jpg`, `content/files/art-kit.zip` | The example sheet and download kit on the website's Artwork page | Make your own: the originals show official card art and can't be shared. Without them that page shows a broken picture and a dead download link. |
| `content/custom/` | The custom-card package for testers (work in progress) | Built with the mod repository's `tools/custom-cards/build_custom.py` |
| `data/discord-webhook.json`, `data/discord-app.json`, `data/discord-bot.json` | Discord notices, account linking and the bot | See the comments at `_discord_settings`, `_discord_app` and `_discord_bot_read` in `playfab_emu.py` |

### Not needed any more

`content/cards/*.json` (an early hand-made card pack; only its version number is still reported),
`content/files/card-art.zip`, `card-masks.zip`, `card-frames/` (the website now draws official cards
from each player's own export; the server refuses to hand these out) and `content/files/card-text.json`.

## 4. Mod builds for players

Download the newest **`v…`** release (not a `patcher-v…` one, which is the Android Patcher app)
from the [mod's releases page](https://github.com/silentanarchist/wf-revival-mod/releases) and put
the DLLs in `mod/` next to `warpforge_server.py` (the server creates the folder on its first start):

| File in `mod/` | From | Offered to |
|---|---|---|
| `WarpforgeRevival.dll` | the `WarpforgeRevival\` folder inside the `-w.zip` | Windows players |
| `WarpforgeRevival.Android.dll` | the `-a.zip` | Android players |

From mod 0.15.0 on the files are called `WFRevival.dll` and `WFRevival.Android.dll`; the server
takes either name.

Players' games update themselves from this folder, but from mod 0.14.0 on they only install builds
that are **signed by the WF Revival project and newer** than the one they run. Serve the released
files unchanged. **Never put an unsigned build in `mod/` while `minModVersion` is `"latest"`:**
the server would then refuse every player whose game has not got it, and their games would refuse
to install it, so nobody could sign in.

## 5. Start it

```
python warpforge_server.py --port 8780
```

(On Windows, `start_server.bat`.) The first lines say whether it found a certificate. Check
`http://your.server.address:8780/api/v1/status`; `/api/v1/content/manifest` lists the bundles it
will hand out. Options: `--host`, `--port`, `--content FOLDER`, `--data FOLDER`, `--certs FOLDER`,
`--cert FILE --key FILE`.

The server creates `data/` (accounts, players, chat: **keep it private and back it up**), `logs/`
and `mod/` as it runs; `mod/` and `logs/` are always next to `warpforge_server.py`, whatever
`--content` and `--data` say. `maintenance/` has Windows scripts for starting it with the PC and
scheduling a nightly backup (see the README). On Linux use a systemd service, and run
`python3 maintenance/backup-data.py /backup/folder` from cron (it backs up the `data/` next to the
script, not one given with `--data`).

## 6. https

Run Caddy (or another reverse proxy) for your domain with `reverse_proxy 127.0.0.1:8780`. The game
port then encrypts the game's connections too, using Caddy's certificate, which the server looks
for in `%APPDATA%\Caddy\certificates` (Windows) and `~/.local/share/caddy/certificates` (Linux, the
user the server runs as). Anywhere else, start the server with `--certs FOLDER`. Caddy installed as
a Linux package keeps them in `/var/lib/caddy/.local/share/caddy/certificates`: pass that folder,
and let the server's user read the `.key` files there. Details, and what
changes for players, are in the README's "https" section.

## 7. Your account and owner rights

1. Open `https://your.domain/login` and create your account (or `http://localhost:8780/login` on
   the server machine itself, if https isn't set up yet).
2. Put its name in `data/creator_admins.json`: `["YourName"]`.

The server notices the file without a restart, and that account becomes the **founding owner**:
it can give admin and owner rights on the People page and edit the settings on the Server page.
Create the account first, then the file. A name listed before it exists goes to whoever registers
it first.

## 8. Point players at your server

Players install the mod the usual way (the [install guides](https://github.com/silentanarchist/wf-revival-mod)
of the mod repository: MelonLoader and the mod on PC, the Warpforge Patcher app on Android). The mod
connects to the WF Revival community server unless told otherwise:

- **PC:** close the game fully (the mod writes the file when the game starts), then in the game
  folder's `UserData\WarpforgeRevival.cfg` set `ServerUrl = "your.domain"`. Port 8780 is assumed;
  write `your.domain:1234` for another port.
- **Android:** Warpforge Patcher app, **Tools > Change the server**, with the game closed.

Then:
- They sign in with an account from your website, or make one in the game's sign-in window. The
  game's content is only handed to **approved** accounts, whatever `requireAccount` says: with
  `"autoApprove": false`, approve each new player on the People page first.
- **They restart the game once after their first sign-in.** The game's content downloads after
  signing in, and the game only uses it from its next start.

## 9. Make it yours

Some pages carry the WF Revival community's own details, which you should replace before you open
your server to anyone:

- `content/files/terms.html`: the Terms and Privacy notice, written for the community server and
  its operator. Write your own; you are the one keeping your players' data.
  Keep its `<meta name="terms-version" content="...">` tag: changing the value asks every account
  to agree again, and without the tag nobody is asked to agree at all.
- The contact address `wf-revival@silentanarchist.com` in `terms.html`, `creator.html` and one
  server message in `playfab_emu.py`.
- The Discord invite `discord.gg/f4N6RU2sYy` in `terms.html`, `guides.html` and `creator.html`.
- `content/files/guides.html`: the player guides, written for the community server. It and
  `creator.html` also link to the WF Revival mod repository, which is fine to keep.

## Tools that don't exist yet

What would make this setup simple, for whoever wants to help. "Exists privately" means a script
was used for the community server but isn't in a public repository yet.

| Tool | Makes | Status |
|---|---|---|
| Cache collector | `content/bundles/*.bundle` from the PC cache, renamed, and checked (1,351 cards, built for Windows) | **No tool yet** |
| Android converter | `content/bundles-android/` from the PC bundles | Exists privately (`port_to_android.py`, UnityPy) |
| Collection exporter | `content/collection.json` | Exists privately (`export_collection.py`, a few lines of UnityPy) |
| Card data exporter | `data/replay-carddata.json` | **No tool yet** (was made once by hand) |
| Draft packs exporter | `content/draft/DraftPacks.json` | **No tool yet** (was made once by hand from the game's draft packs data) |
| Card-creator kit | the creator files above | Exists privately, but needs work first: hard-coded paths, and it reads the card file pre-parsed into Python pickles that no script in the kit makes |
| Card viewer data | `content/creator-official.json` | Exists privately; needs the game's English texts pre-parsed, which no script makes |
| Art kit without official art | `art-kit.zip` and an example sheet anyone can share | **No tool yet** |
| Content check | one command that lists what is missing and what is wrong | **No tool yet** (`/api/v1/content/problems` covers the old card pack only) |
| Community details as settings | the Discord invite, contact address and operator name in one place instead of in the HTML | **No tool yet** |
| Linux service files | start with the machine; the nightly backup schedule | **No tool yet** (the autostart and scheduling scripts in `maintenance/` are Windows only; `backup-data.py` itself runs anywhere) |
