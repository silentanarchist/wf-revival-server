# Card creator checks

The creator page's editor must be able to build every official card from a blank form, and the server's
compiler (`CardCompiler` in `playfab_emu.py`) must build each such design exactly as the game's own card.
The check scripts live in the card-creator kit (`card-creator-kit/roundtrip`, with its README), because
they read the game's own card file, which never goes to this repo. Result on 2026-10-12 (commit 18ba01c):

- **Compiler:** official card -> creator design -> `CardCompiler`, compared with the real card on
  everything the game reads (card-level fields, abilities, effects by the parts each one reads, the
  action, the alternative action, targeting): **1351 of 1351**. Three differences were checked in the
  game's code and left on purpose:
  - Beautiful Death's heal maximum: TargetExcessDamage reads only the minimum.
  - Two tutorial-only warlords' initiative.

  Not compared: art, sounds, animations, and AI scoring.
- **Editor:** every official design is loaded into the page and saved back, then put through the
  server's save check (`_creator_clean`) and built: **1350 of 1351**. Tomb World's official name ends in
  a space, which the editor and server trim. No official value is missing from its menu.
- **Nothing hidden:** every value the editor saves was drawn as a control on screen, so a value is never
  kept from a loaded card without the author being able to see and set it.

Re-run them after any change to how the editor loads or saves, or to the compiler.
