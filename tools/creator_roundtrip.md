# Card creator round-trip check

Every official card's creator design (from `content/creator-official.json`, game content, not in
this repo) is loaded into the creator page's editor and saved back out (`mergeAbility` then
`trimAbility`, the same code the page runs), and both versions are built with `CardCompiler`.
The editor is faithful when every card builds identically. Result on 2026-10-12: **1351 of 1351**.

How it was run: a local copy of the server with the page, a headless browser calling
`trimAbility(mergeAbility(a))` for every ability of every design, then
`CardCompiler(native).compile()` on the original and on the round-tripped design, comparing the
results. Re-run it after changing how the editor loads or saves abilities.
