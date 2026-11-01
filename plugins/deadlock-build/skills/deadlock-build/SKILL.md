---
name: deadlock-build
description: Build a Deadlock item build for a hero by reasoning about the current patch's real abilities, power spikes, items, upgrade chains and slot limits, then render an HTML report and in-game import strings. Use when asked for a Deadlock build, item order, power spikes, or to compare spirit / gun / mixed paths for a hero.
---

# Deadlock build skill

You reason; the scripts fetch live facts and check your plan. Do not hand the decision to a formula, and do not
lean on win-rate statistics: they mix ranks and skill and cannot show cause. Power spikes (exact ability and item
numbers) drive the build. Stats are an optional sanity check only.

Scripts live in `scripts/` next to this file (Python 3, standard library; `pip install zstandard` once so import
strings are game-compatible). Run from any working folder you can write to.

## What to deliver by default (experimental structure)

One report per hero with these builds, each its own plan (`path`) and import string, rendered together:

1. **Identities** - what the build IS at the end. Give the identities the hero's kit credibly supports (usually two, at most
   three), each a balanced build:
   - **Damage** - the hero's damage sources (gun, abilities, summons) multiplied together.
   - **Durable** - frontline: health, resists, barriers, lifesteal. Only for heroes that fight in the middle of the other team.
   - **Utility** - buffing the user and the team: heals, barriers, debuffs, auras, crowd control, cooldowns.
   Say which identity the kit favours (or that it is flexible) and why, from the numbers.
2. **Openings** - how the first ~3000 souls differ. Put them on each identity as `openings`: a **Fight lane** (damage and
   sustain stones, aimed at the first kill spike) and a **Farm lane** (clear-speed and economy stones). Aggressive and
   Farming are openings, not identities: late inventories converge, so do not make them separate builds unless asked.
3. **Perfect** - the ideal build, ignoring cost: the strongest possible inventory for this kit, as if souls were unlimited.
   Multiply EVERY damage source and the debuffs, finish every chain, set every imbue, fill 12 slots, and make an explicit
   survivability decision (some heroes are best as glass cannons). Set `"ideal": true`. Use the facts file's kit math and
   the renderer's model table to compare it with the other builds, then explain what it still cannot show. Never call it
   proven: it is the best we can build from the mechanics.

Mark the plans `"experimental": true`. If the user asks for the old categories, deliver **Aggressive / Farming / Support**
instead (Aggressive = fighting power; Farming = economy and sustain early; Support = buff the user and team, and the
hero's own spikes), each a balanced build.

## Account id (ask once)

Import strings need the user's Deadlock account id, which is their Steam account id. Before rendering, check whether
`~/.cache/deadlock-build/account_id.txt` exists. If it does not, ask the user for it once, and tell them where to find it:
Steam -> Friends -> Add a Friend -> "Your Friend Code" (that number is the account id), or the number from their profile
URL (`steamcommunity.com/profiles/7656119...`). Pass it with `--account-id`; the script accepts the friend code or the
long SteamID64, converts it, and saves it for next time. Never put the id in files meant to be shared.

## Workflow

1. **Get the facts** (current patch, nothing scored):
   `python <skill>/scripts/deadlock_build.py facts <Hero> [--vs "Enemy1,Enemy2"] [--out-dir DIR]`
   It writes `facts_<hero>.md` (weapon, every ability tier with base -> new numbers, the level/soul timeline,
   enemy abilities, recent patch notes) and `items_build<N>.md` (every purchasable item with stats, effect text,
   upgrade chain and imbue flag). Read the facts file fully. Grep the item catalog; read whole slot sections
   when choosing. Add `--stats` only if the user wants the observational sanity check.
2. **Check the patch notes, once per patch.** The facts file says whether full notes are already saved in
   `~/.cache/deadlock-build/patch_notes/`; if so, read those and do not fetch again. If not, the feed is only a stub:
   open the link in the newest entry with a web fetch, save the useful text there as `<patch title>.txt`, and look
   for changes to this hero, its abilities, or items you plan to use. Say what you found. Static game data (heroes,
   abilities, items) is already cached per game build by the scripts, so reruns are fast; use `--refresh` only if
   you suspect stale data.
3. **Reason about the kit** (write this down for the user, briefly):
   - What each ability tier actually changes in numbers (cooldown cut as extra uptime, damage doubling, a new
     charge, scaling coefficient). Name the real power spikes and the soul/level where each lands.
   - Where damage comes from: spirit scaling (the "spirit power xN" in the numbers), the gun (weapon DPS, falloff,
     spread, clip, abilities that buff or steal weapon damage), or both. Decide the split from the mechanics,
     but default to a balanced (mixed) build: use the gun and the abilities together, leaning on whichever the
     numbers favour. The user tried a 100% gun Graves build and found it too much, so do not offer pure single-path
     builds (all gun or all spirit) unless they ask for one. If two balanced leanings are credible, give two plans.
   - **Survivability.** Damage that dies instantly is worth nothing, and how much toughness a hero needs depends on the
     hero. Read the facts file's survival read (base health, escape, barriers, heals, crowd control, summons that take
     the focus, gun range). If the hero can stay out of reach, glass-cannon items (for example Glass Cannon) may be
     the best build; if it must stand in the fight with no escape, buy health, resists, barriers or lifesteal. State
     your survivability judgement for every plan and say why. The renderer's model shows health, effective health,
     lifesteal and a damage-times-toughness figure to compare plans; it cannot judge fights.
   - Which items multiply those specific numbers (e.g. cooldown reduction on a long-cooldown ultimate, range on a
     ranged line, fire rate for an on-hit effect) versus items that are generic stat sticks.
4. **Plan purchases around the spikes.** Rules that the renderer enforces:
   - Inventory is 12 slots: 9 base + 3 flex (flex only opens late). Categories are unrestricted.
   - **Fill all 12 slots.** The final inventory must have exactly 12 items (the renderer rejects fewer). Put the best
     remaining items last; whatever lands beyond a typical game's souls is marked luxury.
   - Upgrades consume the components you hold and credit their price. Never list a component and its upgrade
     as two final items. Buy cheap components as lane items when you want their upgrade later; if a lane item has
     no useful upgrade, plan to sell it. A cheap item late in the game does nothing, so plan the sell/replacement.
   - **Finish the chains.** For every item in the final inventory, look at its "upgrades into" in the catalog. A
     leftover half-built item (for example Improved Spirit when Boundless Spirit is affordable) wastes a slot late
     in the game: upgrade it. The renderer REFUSES a plan whose final inventory still has an item that can
     upgrade. Only if the cheaper item is truly better, list it under `"keep_unfinished": [{"item": "...", "why": "..."}]`.
   - Imbuable items (`IMBUE` in the catalog) attach to one ability: choose the ability that gains most.
   - A typical game ends around 35-40k souls; items beyond that are luxury.
   Also plan: ability order (unlock/upgrade picks, 1/2/5 points for T1/T2/T3, which tier to rush and why),
   lane stepping stones, sells with the item that replaces each, and late-game swaps.
5. **Write the plan file** (JSON, schema below) and render:
   `python <skill>/scripts/deadlock_build.py render plan.json --out <name>.md`
   For the default three builds, pass all three plan files at once (same hero, different `path`):
   `... render aggressive.json farming.json support.json --out <hero>.md` makes ONE report with each build and its own copy buttons.
   If it prints errors (unknown item name, duplicate buy, over 12 items, bad ability name), fix the plan and rerun.
   Warnings are advice (full-price buy of an upgrade, flex-slot use, auto-picked imbue): address or explain them.
6. **Tell the user** what you chose and why in plain language: the spikes, the path, the sells, any trade-off, and
   what you are unsure about (for example untested mechanics). Give the HTML report and `_import.txt` paths.
   Never claim a build is proven stronger than another.

## Plan schema

```json
{
  "hero": "Graves",
  "path": "Gun-heavy",
  "name": "Graves - Gun-heavy (optional)",
  "summary": "One or two sentences on the idea.",
  "ability_order": [ {"ability": "Jar of Dead", "why": "unlock"}, "Jar of Dead", "Grasping Hands" ],
  "spikes": [ {"souls": 2600, "text": "Ultimate online: ..."}, {"when": "late", "text": "..."} ],
  "steps": [
    {"item": "Extra Stamina", "note": "lane; upgrades into Stamina Mastery"},
    {"item": "Mystic Expansion", "imbue": "Grasping Hands", "note": "range on the wall"},
    {"sell": "Extra Regen", "note": "dead weight now; slot goes to X"},
    {"item": "Ricochet", "note": "..."}
  ],
  "swaps": [ {"sell": "Spirit Sap", "buy": "Cheat Death", "note": "once rich with full slots"} ]
}
```

- Worked plans for the shape (not the choices) are in `examples/` (`graves_perfect_plan.json`, `graves_damage_plan.json`, `graves_utility_plan.json`).
- `ability_order` lists names in pick order (first mention = unlock; later mentions = T1, T2, T3). Use exact ability names from the facts file.
- `steps` are in purchase order and include lane stepping stones, upgrades and sells. Item names must match the catalog.
- `imbue` names an ability for imbuable items; omit it to let the script auto-pick (it will warn).
- `keep_unfinished` is optional: items deliberately left un-upgraded, each with a reason.
- Optional: `openings` (`[{"name", "items": [...], "why"}]`), `experimental`, `ideal` (no budget warnings; for Perfect).
- `spikes` with `souls` appear in the purchase table at that soul count; others are listed as reasoning.

## Honesty rules

- You cannot test builds in game. Say which parts are mechanics you verified in the numbers and which are judgment.
- If the numbers in the facts contradict a belief the user states (for example gun-heavy play), say so, but defer to
  the user's play experience on feel (aim, ricochet and split-shot behaviour) that data cannot show.
- The standalone auto mode (`deadlock_build.py <Hero>`) is the older heuristic generator. It works as a quick
  starting point but misreads how much a hero relies on the gun, so check its read against the facts.
