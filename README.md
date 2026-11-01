# claude-skills

A Claude Code plugin marketplace with skills I build and share. More will be added over time.

| Plugin | What it does |
|---|---|
| [`deadlock-build`](plugins/deadlock-build) | Plans Deadlock item builds from the current patch's real numbers |

## Install

```
/plugin marketplace add paul-lapczynski/claude-skills
/plugin install deadlock-build@claude-skills
```

---

## deadlock-build

The skill does not copy popular builds or lean on win rates (which mix ranks and skill). It pulls the current patch's
real ability, weapon and item numbers, finds the hero's power spikes, and builds around them.

Then ask Claude for a build, for example: `/deadlock-build Graves`.

### What you get

For a hero, one HTML report with, per build:

- the ability order with the soul count where each point unlocks (unlocks and ability points are separate; tiers cost 1/2/5),
- the power spikes and why they matter,
- a purchase order with lane stepping stones, upgrades (component price credited), sells, late-game swaps and imbue targets,
- a final inventory of exactly 12 items (9 + 3 flex) with every upgrade chain finished,
- a rough model of damage, health and lifesteal for comparing builds,
- copy buttons for in-game build import strings.

The experimental structure is **identities** (Damage, Durable, Utility, depending on the kit), each with **Fight lane / Farm lane
openings**, plus a **Perfect** build: the strongest inventory the kit allows if souls were unlimited, with an explicit
survivability decision (some heroes are best as glass cannons).

### Requirements

- Python 3 (standard library only). `pip install zstandard` once so import strings are compressed the way the game expects.
- Internet access to `api.deadlock-api.com` (a community API) for hero, item and patch data.
- Your own Deadlock account id for import strings (see below).

### Finding your account id

Your Deadlock account id is your Steam account id. Any one of these works:

1. **Steam friend code (easiest):** in Steam, Friends -> Add a Friend -> "Your Friend Code".
2. **Your profile URL:** if it looks like `steamcommunity.com/profiles/7656119...`, that long number (a SteamID64) works as is.
   If it has a custom name instead, paste your profile into a lookup site such as steamid.io to get the number.

The skill asks for it the first time you make a build. To set or change it yourself:

```
python plugins/deadlock-build/skills/deadlock-build/scripts/deadlock_build.py render plan.json --account-id 12345678
```

The script converts a SteamID64 to the 32-bit id and saves it in `~/.cache/deadlock-build/account_id.txt`, so you only give it once.
It is not secret, but it is stored in your own import strings and cache, never in this repo.

Static game data is cached per game build in `~/.cache/deadlock-build/`, so repeat runs are fast.

### Scripts

```
python plugins/deadlock-build/skills/deadlock-build/scripts/deadlock_build.py facts Graves
python plugins/deadlock-build/skills/deadlock-build/scripts/deadlock_build.py render perfect.json damage.json utility.json --out graves.md
python plugins/deadlock-build/skills/deadlock-build/scripts/deadlock_build.py Graves        # older automatic mode (Aggressive / Farming / Support)
```

### Caveats

- Unofficial and not affiliated with Valve. Game data comes from a community API and can lag or change.
- The model covers the gun, direct ability damage, damage over time and per-bullet bonuses. It ignores summons, procs, debuffs and fight context. It compares builds; it does not predict damage.
- No build here is proven stronger than another. Test in game.
- The import-string format was reverse-engineered from builds copied out of the game and may break with a game update.
