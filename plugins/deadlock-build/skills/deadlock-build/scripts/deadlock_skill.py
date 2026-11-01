#!/usr/bin/env python3
"""`facts` and `render` commands for the deadlock-build skill.

    python deadlock_build.py facts  <Hero> [--vs "A,B"] [--stats] [--out-dir DIR]
    python deadlock_build.py render <plan.json> [--out FILE.md] [--account-id N]

facts  : writes plain-language, current-patch facts (abilities with exact numbers per upgrade tier, level/soul
         timeline, weapon, patch notes, the item catalog and its upgrade tree). No scoring - Claude reasons from it.
render : takes a build plan (written by Claude), checks it against the real rules (upgrade chains, price credit,
         slots, imbue, ability order) and writes the HTML report plus in-game import strings.
"""
import argparse, difflib, json, os, re, sys, time, zlib

import deadlock_build as g
from deadlock_build import get, plain, num, mmss

NOISE = re.compile(r"Velocity|Drag|Pitch|Ceiling|Friction|Acceleration|Gravity|FallSpeed|Think|Model|Volume|Offset|Seed|Rate$", re.I)
SLOTS = ("weapon", "vitality", "spirit")


# ---------------------------------------------------------------- shared helpers

def lines_of(html):
    html = re.sub(r"<svg.*?</svg>", "", html or "", flags=re.S)
    html = re.sub(r"</(li|p|div|h\d)>|<br\s*/?>", "\n", html)
    t = re.sub(r"<[^>]+>", "", html)
    t = t.replace("&amp;", "&").replace("&quot;", '"').replace("&#039;", "'").replace("&gt;", ">").replace("&lt;", "<")
    return [re.sub(r"\s+", " ", x).strip() for x in t.split("\n") if x.strip()]


def fmt(v):
    return f"{v:g}"


def prop_line(ab, name, v):
    label = v.get("label") or name
    val = str(v.get("value")).strip()
    post = v.get("postfix") or ""
    sf = v.get("scale_function") or {}
    scale = ""
    if "tech" in str(sf.get("class_name", "")).lower() and sf.get("stat_scale"):
        scale = f" (spirit power x{fmt(num(sf['stat_scale']))})"
    return f"{label} {val}{post}{scale}"


def ability_block(ab, slot_no):
    L = []
    cd = num(ab["properties"].get("AbilityCooldown", {}).get("value", 0))
    ult = ab.get("ability_type") == "ultimate"
    L.append(f"### {'ULT ' if ult else ''}Ability {slot_no}: {ab['name']}" + (f"  (cooldown {fmt(cd)}s)" if cd else "  (passive / no cooldown)"))
    d = ab.get("description") or {}
    if plain(d.get("desc", "")):
        L.append(plain(d["desc"]))
    base = []
    for k, v in ab["properties"].items():
        if isinstance(v, dict) and num(v.get("value")) not in (0, -1) and not NOISE.search(k) and k != "AbilityCooldown":
            base.append(prop_line(ab, k, v))
    if base:
        L.append("Base numbers: " + "; ".join(base))
    ups = ab.get("upgrades") or []
    for t, u in enumerate(ups[:3], 1):
        bits = []
        for pu in u.get("property_upgrades", []):
            pv = ab["properties"].get(pu["name"], {})
            b0 = num(pv.get("value", 0)) if isinstance(pv, dict) else 0
            b = num(pu["bonus"])
            lab = (pv.get("label") if isinstance(pv, dict) else None) or pu["name"]
            post = (pv.get("postfix") if isinstance(pv, dict) else "") or ""
            if pu.get("upgrade_type") == "EAddToScale" or pu.get("scale_stat_filter"):
                bits.append(f"{lab}: spirit scaling +{fmt(b)}")
            elif b0:
                bits.append(f"{lab} {fmt(b0)} -> {fmt(b0 + b)}{post}")
            else:
                bits.append(f"{lab} +{fmt(b)}{post}")
        txt = plain(d.get(f"t{t}_desc", ""))
        L.append(f"- T{t} (costs {g.AP_COST.get(t, '?')} ability points): " + ("; ".join(bits) or "-") + (f"  [{txt}]" if txt else ""))
    return "\n".join(L)


def catalog(all_items, tree):
    parents = {}
    for it in tree["by_id"].values():
        for c in tree["comps"](it):
            parents.setdefault(c["id"], []).append(it["name"])
    shop = [i for i in all_items if i.get("type") == "upgrade" and i.get("shopable") and not i.get("disabled")
            and i.get("item_tier") in (1, 2, 3, 4) and i.get("cost") and i.get("cost") != 9999]
    L = ["# Deadlock item catalog (current patch)", "",
         "Format: Name | cost | passive/active | stats | effect | builds from -> / upgrades into. "
         "`imbue` items attach to ONE ability. Buying an upgrade consumes the component items you hold and credits their price.", ""]
    for slot in SLOTS:
        for tier in (1, 2, 3, 4):
            rows = sorted((i for i in shop if i.get("item_slot_type") == slot and i["item_tier"] == tier), key=lambda i: (i["cost"], i["name"]))
            if not rows:
                continue
            L.append(f"## {slot.upper()} tier {tier}")
            for it in rows:
                prof = g.item_profile(it)
                stats = []
                for u in (it.get("upgrades") or [])[:1]:
                    for pu in u.get("property_upgrades", []):
                        pv = it["properties"].get(pu["name"], {})
                        lab = (pv.get("label") if isinstance(pv, dict) else None) or pu["name"]
                        stats.append(f"{lab} {pu['bonus']}{(pv.get('postfix') or '') if isinstance(pv, dict) else ''}")
                eff = []
                d = it.get("description") or {}
                if plain(d.get("desc", "")):
                    eff.append(plain(d["desc"]))
                for sec in it.get("tooltip_sections") or []:
                    for at in sec.get("section_attributes", []):
                        if at.get("loc_string"):
                            eff.append(plain(at["loc_string"]))
                eff_txt = " ".join(dict.fromkeys(eff))[:260]
                cd = num(it["properties"].get("AbilityCooldown", {}).get("value", 0))
                link = []
                if tree["comps"](it):
                    link.append("builds from " + ", ".join(c["name"] for c in tree["comps"](it)))
                if parents.get(it["id"]):
                    link.append("upgrades into " + ", ".join(parents[it["id"]]))
                L.append(f"- {it['name']} | {it['cost']} | " + (f"active, {fmt(cd)}s cd" if it.get("is_active_item") or it.get("activation") not in (None, "passive") and cd else "passive")
                         + (f" | {'; '.join(stats[:8])}" if stats else "") + (f" | {eff_txt}" if eff_txt else "")
                         + (f" | IMBUE ({it['imbue']})" if it.get("imbue") else "") + (f" | {'; '.join(link)}" if link else "")
                         + (" | HERO-SPECIFIC" if it.get("heroes") else ""))
            L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------- facts

def cmd_facts(argv):
    ap = argparse.ArgumentParser(prog="deadlock_build.py facts")
    ap.add_argument("hero")
    ap.add_argument("--vs", default="")
    ap.add_argument("--stats", action="store_true", help="append OBSERVATIONAL item win-rate data (optional sanity check)")
    ap.add_argument("--min-badge", type=int, default=70)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    R = a.refresh
    heroes = get("/v1/assets/heroes", refresh=R)
    hero = get(f"/v1/assets/heroes/{g.find_hero(a.hero, heroes)['id']}", refresh=R)
    pc = g.patch_context(R)
    abilities = get(f"/v1/assets/items/by-hero-id/{hero['id']}", refresh=R)
    kit = g.ability_by_slot(hero, abilities)
    all_items = get("/v1/assets/items", refresh=R)
    tree = g.build_tree(all_items)

    L = [f"# {hero['name']} - facts for build reasoning",
         f"Game build {pc['build']} ({pc['build_date']}). Latest patch note: {pc['patch_title']}. Data pulled live; nothing here is scored or opinionated.", "",
         f"Type: {hero.get('hero_type')} | gun: {hero.get('gun_tag')} | tags: {', '.join(hero.get('tags', []))}", ""]
    ss = hero.get("starting_stats") or {}
    want = ("max_health", "max_move_speed", "sprint_speed", "base_health_regen", "stamina_regen_per_second", "light_melee_damage", "heavy_melee_damage")
    L.append("Base stats: " + "; ".join(f"{k} {fmt(num(ss[k]['value']))}" for k in want if k in ss and isinstance(ss[k], dict)))
    sc = hero.get("scaling_stats") or {}
    sc_txt = [f"{k.replace('MODIFIER_VALUE_', '').lower()} {v}" for k, v in sc.items() if v]
    if sc_txt:
        L.append("Per-level scaling: " + "; ".join(sc_txt))
    weap = next((i for i in all_items if i["class_name"] == hero["items"].get("weapon_primary")), None)
    if weap and weap.get("weapon_info"):
        w = weap["weapon_info"]
        keys = ("bullet_damage", "bullets", "clip_size", "reload_duration", "bullets_per_second", "damage_per_second", "damage_per_second_with_reload",
                "damage_falloff_start_range", "damage_falloff_end_range", "damage_falloff_end_scale", "spread", "standing_spread", "crit_bonus_start", "range")
        L += ["", "## Weapon (base, before items)", "; ".join(f"{k} {fmt(num(w[k]))}" for k in keys if k in w)]
    L += ["", "## Abilities (exact numbers; every upgrade tier shows base -> new)"]
    for n, ab in enumerate(kit, 1):
        L += ["", ability_block(ab, n)]

    unl, pts = [], []
    for lvl in sorted(hero.get("level_info") or {}, key=int):
        cur = hero["level_info"][lvl].get("bonus_currencies") or []
        row = (int(lvl), hero["level_info"][lvl].get("required_gold", 0))
        if "EAbilityUnlocks" in cur:
            unl.append(row)
        elif "EAbilityPoints" in cur:
            pts.append(row)
    L += ["", "## Level / soul timeline (net worth when each currency arrives)",
          "Two currencies: ABILITY UNLOCKS (1 per unlock) and ABILITY POINTS. Upgrade tiers cost T1=1, T2=2, T3=5 points, so a T3 needs "
          "five points saved up: its earliest soul count is the soul count of the Nth point, N = running total of points spent. "
          "Shop item prices count toward net worth.", "",
          "Unlocks: " + "; ".join(f"#{k} L{lv} @{s} souls" for k, (lv, s) in enumerate(unl, 1)),
          "Ability points (cumulative): " + "; ".join(f"#{k} L{lv} @{s}" for k, (lv, s) in enumerate(pts[:34], 1))]

    # survival read: what keeps this hero alive without items, so Claude can judge how much toughness the build needs
    tools = []
    for ab in kit:
        dd = ab.get("description") or {}
        blob = " ".join(plain(dd.get(k, "")) for k in ("desc", "t1_desc", "t2_desc", "t3_desc")).lower()
        tags = [w for w, pat in (("untargetable/invulnerable", r"untargetable|invulnerab|invincib"), ("barrier/shield", r"barrier|shield"), ("self heal", r"heal|lifesteal|regenerat"),
                                 ("dash/teleport/escape", r"dash|teleport|blink|leap|escape"), ("crowd control", r"stun|slow|immobiliz|silence|sleep|root"),
                                 ("summons that draw attention", r"summon|ghoul|zombie|minion|deadhead")) if re.search(pat, blob)]
        if tags:
            tools.append(f"- {ab['name']}: " + ", ".join(tags))
    ss0 = hero.get("starting_stats") or {}
    L += ["", "## Survival read (judge how much toughness this hero needs; glass cannon can be right)",
          f"Base health {fmt(num(ss0.get('max_health', {}).get('value', 0)))}, move speed {fmt(num(ss0.get('max_move_speed', {}).get('value', 0)))}, "
          f"health regen {fmt(num(ss0.get('base_health_regen', {}).get('value', 0)))}; gun range {fmt(num(weapon_info_of(hero, all_items).get('range', 0)))}.",
          "Survival tools in the kit (from ability text):"] + (tools or ["- none found"]) + [
          "Ask: can this hero stay out of reach (range, escape, summons that take the focus)? If yes, damage-heavy or glass-cannon items can beat toughness. "
          "If it must stand in the fight with no escape, buy health, resists, barriers or lifesteal. Say which, and why."]

    # kit math: what each stat is worth for THIS hero, from its own numbers
    winfo = weapon_info_of(hero, all_items)
    vt, base = stat_value_table(hero, kit, winfo, {ab["id"]: 3 for ab in kit})
    L += ["", "## Kit math (mechanics, relative - read the caveats)",
          "Built from this hero's weapon and ability numbers. It ignores summons, damage over time, procs, conditions and fight context, "
          "and counts conditional item bonuses in full, so it can under-rate summon heroes and over-rate conditional items. "
          "Use it to see which stats this kit multiplies, not to predict damage.", ""] + vt
    b0 = base["gun_dps"] + base["abil_dps"]
    ref = dict(ZERO, sp=100.0)
    rows = []
    for it in all_items:
        if it.get("type") == "upgrade" and it.get("shopable") and not it.get("disabled") and it.get("item_tier") in (1, 2, 3, 4) and it.get("cost") and it.get("cost") != 9999:
            stt = item_stats(it)
            e = kit_eval(hero, kit, winfo, {k: ref[k] + stt[k] for k in ref}, {ab["id"]: 3 for ab in kit})
            rows.append((e["gun_dps"] + e["abil_dps"] - b0, it))
    rows.sort(key=lambda r: -r[0])
    L += ["", "Items by model damage gain alone (stats only; effects and procs not counted):"]
    L += [f"- {it['name']} ({it['cost']}): +{gn:.1f} DPS, {gn / (it['cost'] / 1000):.1f} per 1000 souls" for gn, it in rows[:14] if gn > 0]

    flt = {"min_average_badge": a.min_badge}
    if pc["patch_ts"]:
        flt["min_unix_timestamp"] = pc["patch_ts"]
    try:
        ao = get("/v1/analytics/ability-order-stats", dict(flt, hero_id=hero["id"], min_matches=30), R)
        names = {x["id"]: x["name"] for x in kit}
        tot = sum(r["matches"] for r in ao) or 1
        good = [r for r in ao if r["matches"] >= max(40, 0.01 * tot) and len(r["abilities"]) >= 8]
        L += ["", "## Reference only: ability orders players actually used (observational - skill/rank mix unknown)"]
        for lab, key in (("most played", lambda r: r["matches"]), ("best win rate", lambda r: (r["wins"] + 100) / (r["matches"] + 200))):
            for r in sorted(good, key=key, reverse=True)[:2]:
                seq = []
                for i, aid in enumerate(r["abilities"]):
                    t = r["avg_upgrade_times_s"][i] if i < len(r["avg_upgrade_times_s"]) else None
                    seq.append(f"{names.get(aid, aid)}@{mmss(t) if t else '?'}")
                L.append(f"- {lab} ({r['matches']} games, {r['wins'] / r['matches'] * 100:.0f}% WR): " + " > ".join(seq))
    except SystemExit:
        pass

    if a.vs.strip():
        L += ["", "## Enemy lineup"]
        for nm in [x.strip() for x in a.vs.split(",") if x.strip()]:
            eh = g.find_hero(nm, heroes)
            eh = get(f"/v1/assets/heroes/{eh['id']}", refresh=R)
            ek = g.ability_by_slot(eh, get(f"/v1/assets/items/by-hero-id/{eh['id']}", refresh=R))
            L.append(f"### {eh['name']} ({eh.get('hero_type')}, {eh.get('gun_tag')}; tags {', '.join(eh.get('tags', []))})")
            for ab in ek:
                cd = num(ab["properties"].get("AbilityCooldown", {}).get("value", 0))
                L.append(f"- {'ULT ' if ab.get('ability_type') == 'ultimate' else ''}{ab['name']}" + (f" ({fmt(cd)}s)" if cd else "") + ": " + plain((ab.get('description') or {}).get('desc', ''))[:230])

    # patch notes: full notes saved earlier by Claude (cache folder) are used as-is; the feed itself is often a stub
    notes_dir = os.path.join(g.CACHE, "patch_notes")
    saved = sorted(os.listdir(notes_dir)) if os.path.isdir(notes_dir) else []
    patches = sorted(get("/v1/patches", refresh=R), key=lambda p: p["pub_date"], reverse=True)[:3]
    if saved:
        L += ["", f"## Full patch notes already saved in {notes_dir} (no need to fetch again)"]
        for fn in saved[-3:]:
            L.append(f"### {fn}")
            L += [f"- {x}" for x in open(os.path.join(notes_dir, fn), encoding="utf-8").read().splitlines() if x.strip()][:300]
    else:
        L += ["", f"(No full patch notes saved yet. After fetching one, save it as a text file in {notes_dir} so it is reused.)"]
    L += ["", "## Recent patch notes (the feed often carries only a stub: open the link with a web fetch for the real notes, "
              "and check anything touching this hero, its abilities or items you plan to use)"]
    keys = [hero["name"].lower()] + [x["name"].lower() for x in kit]
    for k, p in enumerate(patches):
        ls = lines_of(p.get("content_encoded", ""))
        L.append(f"### {p['title']} ({p['pub_date'][:10]}) {p.get('link', '')}")
        if k == 0:
            L += [f"- {x}" for x in ls][:260]
        else:
            L += [f"- {x}" for x in ls if any(key in x.lower() for key in keys)][:40]

    if a.stats:
        L += ["", "## OBSERVATIONAL item win rates (optional sanity check only)",
              "Caution: ranks and skill are mixed, rich players buy expensive items, and these numbers cannot show cause. Do not let them drive the build.", ""]
        items = {i["id"]: i for i in all_items if i.get("type") == "upgrade" and i.get("shopable") and not i.get("disabled")
                 and i.get("item_tier") in (1, 2, 3, 4) and i.get("cost") and i.get("cost") != 9999 and (not i.get("heroes") or hero["id"] in i["heroes"])}
        hm = g.roster_table(get("/v1/analytics/hero-stats", flt, R)).get(hero["id"], {})
        sa = g.fetch_item_stats(hero["id"], flt, R)
        sw = {w: g.fetch_item_stats(hero["id"], flt, R, window=rng if rng[1] else (rng[0], 9999)) for w, rng in g.WINDOWS.items()}
        met = g.score_items(items, sa, sw, hm.get("matches", 1), None)
        L.append(f"{hm.get('matches', 0)} games, hero win rate {hm.get('wr', 0) * 100:.1f}%. Lift = win-rate points vs same-tier items in the window where that tier is normally bought.")
        for t in (1, 2, 3, 4):
            rows = sorted((m for m in met.values() if m["item"]["item_tier"] == t and m["pick"] >= 3), key=lambda m: -m["lift"])
            L.append(f"- Tier {t}: " + "; ".join(f"{m['item']['name']} {m['pick']:.0f}% pick {m['lift']:+.1f}pp" for m in rows[:14]))

    out = a.out_dir
    os.makedirs(out, exist_ok=True)
    fp = os.path.join(out, f"facts_{hero['name'].lower().replace(' ', '_')}.md")
    cp = os.path.join(out, f"items_build{pc['build']}.md")
    with open(fp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    with open(cp, "w", encoding="utf-8") as fh:
        fh.write(catalog(all_items, tree) + "\n")
    print(f"wrote {fp}\nwrote {cp}")


# ---------------------------------------------------------------- render

def resolve_item(name, by_name, errors, where):
    key = str(name).strip().lower()
    if key in by_name:
        return by_name[key]
    close = difflib.get_close_matches(key, list(by_name), n=3, cutoff=0.6)
    errors.append(f"{where}: no purchasable item called '{name}'" + (f" (did you mean {', '.join(by_name[c]['name'] for c in close)}?)" if close else ""))
    return None


def render_one(plan_path, R, acct_arg):
    """Validate one plan against the live rules and build its markdown and export strings."""
    plan = json.load(open(plan_path, encoding="utf-8"))
    if "hero" not in plan or "steps" not in plan:
        sys.exit(f"{plan_path} is not a build plan (needs at least \"hero\" and \"steps\"). Plan files are never overwritten; outputs are *.md, *.html, *_import.txt, *_export.json.")
    heroes = get("/v1/assets/heroes", refresh=R)
    hero = get(f"/v1/assets/heroes/{g.find_hero(plan['hero'], heroes)['id']}", refresh=R)
    pc = g.patch_context(R)
    kit = g.ability_by_slot(hero, get(f"/v1/assets/items/by-hero-id/{hero['id']}", refresh=R))
    all_items = get("/v1/assets/items", refresh=R)
    tree = g.build_tree(all_items)
    by_id = tree["by_id"]
    shop = {i["name"].lower(): i for i in all_items if i.get("type") == "upgrade" and i.get("shopable") and not i.get("disabled")
            and i.get("item_tier") in (1, 2, 3, 4) and i.get("cost") and i.get("cost") != 9999 and (not i.get("heroes") or hero["id"] in i["heroes"])}
    ab_by_name = {x["name"].lower(): x for x in kit}
    errors, warns = [], []
    g.ICONS.update({i["name"]: i.get("shop_image_webp") or i.get("shop_image") for i in by_id.values() if i.get("shop_image_webp") or i.get("shop_image")})

    # ---- ability order
    rewards = g.reward_schedule(hero)
    order = []
    for k, entry in enumerate(plan.get("ability_order", [])):
        nm, why_ = (entry, "") if isinstance(entry, str) else (entry.get("ability", ""), entry.get("why", ""))
        ab = ab_by_name.get(nm.lower())
        if not ab:
            errors.append(f"ability_order[{k}]: '{nm}' is not one of {hero['name']}'s abilities ({', '.join(x['name'] for x in kit)})")
            continue
        order.append((ab, why_))
    cnt, picks = {}, []
    for ab, _ in order:
        cnt[ab["id"]] = cnt.get(ab["id"], 0) + 1
        picks.append((cnt[ab["id"]] == 1, g.AP_COST.get(cnt[ab["id"]] - 1, 5)))
    sched = g.pick_schedule(hero, picks)
    seen, ao_rows, changes = {}, [], []
    for k, (ab, why_) in enumerate(order):
        seen[ab["id"]] = seen.get(ab["id"], 0) + 1
        n = seen[ab["id"]]
        if n > 4:
            errors.append(f"ability_order[{k}]: {ab['name']} is picked {n} times (max: unlock + 3 upgrades)")
        lv, souls = sched[k]
        ao_rows.append((k + 1, ab["name"], "Unlock" if n == 1 else f"T{n - 1}", souls, lv, why_))
        if n == 1:
            changes.append({"ability_id": ab["id"], "currency_type": 2, "delta": -1, "annotation": None})
        elif n - 1 in g.AP_COST:
            changes.append({"ability_id": ab["id"], "currency_type": 1, "delta": -g.AP_COST[n - 1], "annotation": None})
    if order and not any(r[2] == "Unlock" and r[1] == next(x["name"] for x in kit if x.get("ability_type") == "ultimate") for r in ao_rows):
        warns.append("ability_order never unlocks the ultimate")

    # ---- simulate purchases
    owned, steps, paid, imbue_map, sell_map = [], [], 0, {}, {}
    for k, st in enumerate(plan.get("steps", []), 1):
        if "sell" in st:
            it = resolve_item(st["sell"], shop, errors, f"steps[{k}] sell")
            if not it:
                continue
            if it["id"] not in owned:
                errors.append(f"steps[{k}]: you sell {it['name']} but do not own it at that point")
                continue
            owned.remove(it["id"])
            steps.append({"kind": "sell", "id": it["id"], "pay": 0, "cum": paid, "slots": len(owned), "note": st.get("note", "")})
            continue
        it = resolve_item(st.get("item", ""), shop, errors, f"steps[{k}]")
        if not it:
            continue
        if it["id"] in owned:
            errors.append(f"steps[{k}]: {it['name']} is already in the inventory (duplicate buy)")
            continue
        comps = tree["comps"](it)
        used = [c["id"] for c in comps if c["id"] in owned]
        if comps and not used:
            warns.append(f"steps[{k}]: {it['name']} is bought at full price - it builds from {', '.join(c['name'] for c in comps)}, which you do not hold")
        credit = sum(by_id[c]["cost"] for c in used)
        for c in used:
            owned.remove(c)
        owned.append(it["id"])
        paid += it["cost"] - credit
        if len(owned) > g.BASE_SLOTS + g.FLEX_SLOTS:
            errors.append(f"steps[{k}]: {len(owned)} items held after buying {it['name']} - the inventory holds {g.BASE_SLOTS + g.FLEX_SLOTS}")
        elif len(owned) > g.BASE_SLOTS:
            warns.append(f"steps[{k}]: {it['name']} needs a flex slot (item {len(owned)} of {g.BASE_SLOTS + g.FLEX_SLOTS}); flex slots only open later in the game")
        inherited = next((imbue_map[c] for c in used if c in imbue_map), None)       # an upgrade keeps its component's imbue
        imb = st.get("imbue")
        if not imb and inherited:
            imbue_map[it["id"]] = inherited
        elif imb:
            tgt = ab_by_name.get(str(imb).lower())
            if not it.get("imbue"):
                warns.append(f"steps[{k}]: {it['name']} cannot be imbued - imbue ignored")
            elif not tgt:
                errors.append(f"steps[{k}]: imbue target '{imb}' is not one of {hero['name']}'s abilities")
            else:
                imbue_map[it["id"]] = tgt["id"]
        elif it.get("imbue"):
            tgt = g.imbue_target(it, kit)
            if tgt:
                imbue_map[it["id"]] = tgt["id"]
                warns.append(f"steps[{k}]: {it['name']} is imbuable and the plan did not say where - auto-picked {tgt['name']}; set \"imbue\" to override")
        steps.append({"kind": "upgrade" if used else "buy", "id": it["id"], "pay": it["cost"] - credit, "consumes": used, "cum": paid,
                      "slots": len(owned), "note": st.get("note", ""), "sell_priority": st.get("sell_priority")})
    for k, s2 in enumerate(steps):
        if s2["kind"] == "sell":
            s2["replace"] = next((x["id"] for x in steps[k + 1:] if x["kind"] != "sell"), None)
    swaps = []
    for k, sw in enumerate(plan.get("swaps", []), 1):
        s_it = resolve_item(sw.get("sell", ""), shop, errors, f"swaps[{k}] sell")
        b_it = resolve_item(sw.get("buy", ""), shop, errors, f"swaps[{k}] buy")
        if s_it and b_it:
            if s_it["id"] not in owned:
                warns.append(f"swaps[{k}]: {s_it['name']} is not in the final inventory")
            swaps.append({"sell": s_it["id"], "buy": b_it["id"], "note": sw.get("note", "")})
    if errors:
        print("PLAN HAS ERRORS - fix these and render again:")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    # ---- the final inventory is always full: 9 base + 3 flex slots
    full = g.BASE_SLOTS + g.FLEX_SLOTS
    if len(owned) < full:
        errors.append(f"final inventory has {len(owned)} items but must have {full} ({g.BASE_SLOTS} + {g.FLEX_SLOTS} flex). "
                      f"Add the best remaining items at the end of the plan; they will be marked luxury.")

    # ---- unfinished upgrade chains: a final item that still has a purchasable upgrade is usually a half-built item
    parents = {}
    for it in shop.values():
        for c in tree["comps"](it):
            parents.setdefault(c["id"], []).append(it)
    for iid in owned:
        ups = parents.get(iid, [])
        if ups:
            base_it = by_id[iid]
            opts = "; ".join(f"{u['name']} (T{u['item_tier']}, +{u['cost'] - base_it['cost']} souls"
                             + (f", also needs {', '.join(c['name'] for c in tree['comps'](u) if c['id'] != iid)}" if len(tree["comps"](u)) > 1 else "") + ")"
                             for u in ups)
            keep = {str(k.get("item", k) if isinstance(k, dict) else k).lower(): (k.get("why", "") if isinstance(k, dict) else "")
                    for k in plan.get("keep_unfinished", [])}
            if base_it["name"].lower() in keep:
                warns.append(f"{base_it['name']} is deliberately left unfinished (could become {opts}): {keep[base_it['name'].lower()] or 'no reason given'}")
            else:
                errors.append(f"UNFINISHED UPGRADE: {base_it['name']} is in the final inventory but can still become {opts}. "
                              f"Add the upgrade step, or list it in \"keep_unfinished\" with a reason.")
    if errors:
        print("PLAN HAS ERRORS - fix these and render again:")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    # ---- typical end-of-game net worth (budget sanity)
    try:
        flt = {"min_average_badge": 70}
        if pc["patch_ts"]:
            flt["min_unix_timestamp"] = pc["patch_ts"]
        nw = g.roster_table(get("/v1/analytics/hero-stats", flt, R))[hero["id"]]["networth"]
        if paid > nw * 1.15 and not plan.get("ideal"):
            warns.append(f"total spend {paid} souls is above a typical end-of-game net worth (~{int(nw)}); the last items only happen in long games")
    except Exception:
        nw = None

    # ---- markdown
    path = plan.get("path", "Custom")
    name = plan.get("name") or f"{hero['name']} - {path}"
    L = [f"# {hero['name']} - {path} build  (game build {pc['build']}, {pc['build_date']})"]
    if plan.get("summary"):
        L += ["", plan["summary"]]
    L += ["", "## Checks"]
    L += [f"- {w}" for w in warns] or ["- All purchases follow the upgrade tree, slot limits and ability rules."]
    if ao_rows:
        L += ["", "## Ability order", "", "| # | Ability | Step | Earliest souls (Lv) | Why |", "|---|---------|------|--------------------|-----|"]
        for n, nm, step, souls, lv, why_ in ao_rows:
            L.append(f"| {n} | {nm} | {step} | {souls if souls is not None else '-'} (L{lv or '-'}) | {why_} |")
    spikes = [s for s in plan.get("spikes", []) if isinstance(s, dict)]
    if spikes:
        L += ["", "## Power spikes (the reasoning)"]
        for s in spikes:
            when = f"~{s['souls']} souls" if s.get("souls") else s.get("when", "")
            L.append(f"- **{when}** - {s.get('text', '')}")
    openings = []
    for o in plan.get("openings", []):
        its = [resolve_item(x, shop, errors, f"openings[{o.get('name', '?')}]") for x in o.get("items", [])]
        openings.append((o.get("name", "Opening"), [x["name"] for x in its if x], o.get("why", "")))
    if errors:
        print("PLAN HAS ERRORS - fix these and render again:")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    if openings:
        L += ["", "## Opening options (first ~3000 souls)", "", "| Opening | Items | Why |", "|---------|-------|-----|"]
        L += [f"| {n} | {', '.join(i)} | {w} |" for n, i, w in openings]
    if plan.get("ideal"):
        L += ["", "*Ideal build: assumes a very long game with unlimited souls. Buy in this order and stop where the game ends.*"]
    if plan.get("experimental"):
        L += ["", "*Experimental build structure.*"]
    L += ["", "## Build", "", f"### {path}", "", "| # | Action | Item | Pay | Total | Lv | Slots | Plan note |", "|---|--------|------|-----|-------|----|-------|-----------|"]
    pend = sorted((s for s in spikes if s.get("souls")), key=lambda s: s["souls"])
    prev, n = 0, 0
    for st in steps:
        it = by_id[st["id"]]
        for sp in list(pend):
            if prev < sp["souls"] <= st["cum"]:
                L.append(f"| | >> SPIKE ~{sp['souls']} souls | | | | | | {sp.get('text', '')[:110]} |")
                pend.remove(sp)
        prev = st["cum"]
        slots = f"{st['slots']}/{g.BASE_SLOTS + g.FLEX_SLOTS if st['slots'] > g.BASE_SLOTS else g.BASE_SLOTS}"
        if st["kind"] == "sell":
            rp = f"; makes room for {by_id[st['replace']]['name']}" if st.get("replace") else ""
            L.append(f"| | SELL | {it['name']} | | {st['cum']} | {g.level_at(hero, st['cum'])} | {slots} | {st['note']}{rp} |")
            continue
        n += 1
        extra = ""
        if st.get("consumes"):
            extra += f" (consumes {', '.join(by_id[c]['name'] for c in st['consumes'])}, credit {sum(by_id[c]['cost'] for c in st['consumes'])})"
        if st["id"] in imbue_map:
            extra += f" **IMBUE on {next(x['name'] for x in kit if x['id'] == imbue_map[st['id']])}**"
        if nw and not plan.get("ideal") and st["cum"] > nw * 1.1:
            extra += " (luxury - only if the game goes long)"
        L.append(f"| {n} | {'Upgrade' if st['kind'] == 'upgrade' else 'Buy'} | {it['name']} | {st['pay']} | {st['cum']} | {g.level_at(hero, st['cum'])} | {slots} | {st['note']}{extra} |")
    L += ["", f"**Final inventory ({len(owned)} slots)**", "", "| Slot | Item | Cost |", "|------|------|------|"]
    for k, iid in enumerate(owned, 1):
        L.append(f"| {k}{' (flex)' if k > g.BASE_SLOTS else ''} | {by_id[iid]['name']} | {by_id[iid]['cost']} |")
    if swaps:
        L += ["", "**Late-game swaps**", "", "| Sell | Replace with | Cost | Why |", "|------|--------------|------|-----|"]
        for sw in swaps:
            L.append(f"| {by_id[sw['sell']]['name']} | {by_id[sw['buy']]['name']} | {by_id[sw['buy']]['cost']} | {sw['note']} |")
    ev = kit_eval(hero, kit, weapon_info_of(hero, all_items), total_stats([by_id[i] for i in owned]), tiers_from_order([ab["id"] for ab, _ in order]))
    L += ["", "**Model estimate** (relative: the hero's real numbers times this build's stats. It ignores summons, damage over time, procs and conditions, so compare builds with it; it is not in-game damage)", ""]
    L += model_lines(ev, path)
    L.append("")
    L.append("Per cast: " + "; ".join(f"{r['name']} {r['cast']:.0f} dmg every {r['cd']:.1f}s" for r in ev["abilities"] if r["modelled"]))
    text = "\n".join(L)

    # ---- export
    acct = g.resolve_account_id(acct_arg)
    if acct is None:
        print("note: no account id; pass --account-id once (it is saved)", file=sys.stderr)
    ts = int(time.time())
    ids = ((ts - 1_700_000_000) * 8 + zlib.crc32(name.encode()) % 8 + 1, acct or 0, ts)
    buys = list(dict.fromkeys(s["id"] for s in steps if s["kind"] != "sell"))
    cats = [(nm, buys[k:k + 5], False) for nm, k in (("Lane", 0), ("Core", 5), ("Mid game", 10), ("Late game", 15)) if buys[k:k + 5]]
    flex = [sw["buy"] for sw in swaps if sw["buy"] not in buys]
    if flex:
        cats.append(("Swaps / situational", flex[:5], True))
    extra, prio = {}, 0
    for iid, abid in imbue_map.items():
        extra.setdefault(iid, {})["imbue"] = abid
    sells = [(s["id"], s.get("replace"), s["note"]) for s in steps if s["kind"] == "sell"] + [(sw["sell"], sw["buy"], sw["note"]) for sw in swaps]
    for sid, rid, note in sells:
        prio += 1
        e = extra.setdefault(sid, {})
        e["sell"] = prio
        e["annotation"] = (f"Sell for {by_id[rid]['name']}" if rid else "Sell when slots are needed")[:60]
    jsn = {"hero_build": {"hero_id": hero["id"], "name": name, "description": plan.get("summary", "")[:300], "details": {
        "mod_categories": [{"name": nm, "mods": [{"ability_id": i, "annotation": extra.get(i, {}).get("annotation"), "sell_priority": extra.get(i, {}).get("sell"),
                                                  "imbue_target_ability_id": extra.get(i, {}).get("imbue")} for i in ii], "optional": opt} for nm, ii, opt in cats],
        "ability_order": {"currency_changes": changes}}}}
    desc = (plan.get("summary") or f"{path} build")[:200]
    exports = {path: {"import": g.encode_build(hero["id"], name, desc, cats, ids=ids, extra=extra),
                      "import_ao": g.encode_build(hero["id"], name, desc, cats, changes, ids=ids, extra=extra),
                      "list": "\n".join(f"{k}. {'SELL ' if s['kind'] == 'sell' else ''}{by_id[s['id']]['name']}" for k, s in enumerate(steps, 1)),
                      "json": json.dumps(jsn, indent=2)}}
    return {"hero": hero, "pc": pc, "name": name, "path": path, "lines": L, "text": text, "exports": exports}


def cmd_render(argv):
    ap = argparse.ArgumentParser(prog="deadlock_build.py render")
    ap.add_argument("plans", nargs="+", help="one or more plan files for the SAME hero; several make one combined report")
    ap.add_argument("--out", help="markdown path; an .html, *_import.txt and *_export.json are written beside it")
    ap.add_argument("--account-id")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    rs = [render_one(pth, a.refresh, a.account_id) for pth in a.plans]
    if len({r["hero"]["id"] for r in rs}) > 1:
        sys.exit("all plans in one report must be for the same hero")
    paths = [r["path"] for r in rs]
    if len(set(paths)) != len(paths):
        sys.exit("each plan in one report needs a different \"path\" (for example Aggressive, Farming, Support)")
    hero, pc = rs[0]["hero"], rs[0]["pc"]
    exports = {}
    for r in rs:
        exports.update(r["exports"])
    if len(rs) == 1:
        text, name = rs[0]["text"], rs[0]["name"]
    else:
        name = f"{hero['name']} - " + " / ".join(paths)
        L = [f"# {hero['name']} - {', '.join(paths)} builds  (game build {pc['build']}, {pc['build_date']})"]
        for r in rs:
            L += ["", f"## {r['path']}"]
            for ln in r["lines"][1:]:
                if ln.startswith("## Build"):
                    continue
                L.append(f"**{ln[3:]}**" if ln.startswith("## ") else ln)
        text = "\n".join(L)
    slug = lambda p: re.sub(r"[^a-z0-9]+", "", p.lower())
    base = os.path.splitext(a.out or f"{hero['name'].lower()}_{'_'.join(slug(p) for p in paths)}.md")[0]
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    imgs = hero.get("images") or {}
    himg = g.fetch_image(imgs.get("icon_hero_card_webp") or imgs.get("icon_hero_card"))
    with open(base + ".html", "w", encoding="utf-8") as fh:
        fh.write(g.to_html(text, name, exports, himg))
    wrote = [base + ".html", base + ".md"]
    for r in rs:
        suffix = "" if len(rs) == 1 else "_" + slug(r["path"])
        with open(f"{base}{suffix}_import.txt", "w", encoding="utf-8") as fh:
            fh.write(r["exports"][r["path"]]["import"] + "\n")
        with open(f"{base}{suffix}_export.json", "w", encoding="utf-8") as fh:
            fh.write(r["exports"][r["path"]]["json"])
        wrote.append(f"{base}{suffix}_import.txt")
    print(text)
    print("\nwrote " + ", ".join(wrote), file=sys.stderr)


# ---------------------------------------------------------------- kit math (mechanics, not win rates)
# A relative model: it multiplies the hero's real numbers by the stats a build adds. It does NOT model summons,
# damage over time, procs, conditions or fight context, so use it to compare builds and stats, not to predict damage.

STAT_KEYS = {
    "sp": ("TechPower", "SpiritPower", "BonusSpirit"), "sp_pct": ("TechPowerPercent",), "wd": ("BaseAttackDamagePercent",),
    "fr": ("BonusFireRate",), "ammo": ("BonusClipSizePercent",), "cdr": ("CooldownReduction",), "reload": ("ReloadSpeedMultipler",),
    "hp": ("BonusHealth",), "bres": ("BulletResist",), "sres": ("TechResist",), "sls": ("AbilityLifestealPercentHero",),
    "bls": ("BulletLifestealPercent",),
}
ZERO = {k: 0.0 for k in STAT_KEYS}


def item_stats(it):
    out = dict(ZERO)
    for u in (it.get("upgrades") or [])[:1]:
        for pu in u.get("property_upgrades", []):
            for k, names in STAT_KEYS.items():
                if pu["name"] in names:
                    out[k] += num(pu["bonus"])
    return out


def total_stats(items):
    tot = dict(ZERO)
    for it in items:
        for k, v in item_stats(it).items():
            tot[k] += v
    return tot


def kit_eval(hero, kit, winfo, tot, tiers):
    """tiers: {ability id: upgrade tiers bought (0-3)}. Returns relative damage and toughness numbers."""
    sp = tot["sp"] * (1 + tot["sp_pct"] / 100)
    bd, bps, clip, rl = (num(winfo.get(k, 0)) for k in ("bullet_damage", "bullets_per_second", "clip_size", "reload_duration"))
    bps_e = bps * (1 + tot["fr"] / 100)
    clip_e = clip * (1 + tot["ammo"] / 100)
    rl_e = rl * (1 + tot["reload"] / 100)
    mag = clip_e / bps_e if bps_e else 0
    up = mag / (mag + rl_e) if mag + rl_e else 0
    gun = bd * (1 + tot["wd"] / 100) * bps_e * up
    cdr = min(tot["cdr"], 60) / 100
    rows = []
    for ab in kit:
        dmg, cd = [], num(ab["properties"].get("AbilityCooldown", {}).get("value", 0))
        for k, v in ab["properties"].items():
            sf = v.get("scale_function") if isinstance(v, dict) else None
            if sf and "tech" in str(sf.get("class_name", "")).lower() and re.search(r"damage", k, re.I) and num(v.get("value")) > 0 and "Percent" not in k:
                dmg.append([k, num(v["value"]), num(sf.get("stat_scale", 0))])
        for t, u in enumerate((ab.get("upgrades") or [])[:tiers.get(ab["id"], 0)]):
            for pu in u.get("property_upgrades", []):
                b = num(pu["bonus"])
                if pu["name"] == "AbilityCooldown":
                    cd += b
                elif pu.get("scale_stat_filter") or pu.get("upgrade_type") == "EAddToScale":
                    if dmg:
                        dmg[0][2] += b
                else:
                    for d in dmg:
                        if d[0] == pu["name"]:
                            d[1] += b
        cast = sum(b + c * sp for _, b, c in dmg)
        eff_cd = cd * (1 - cdr) if cd > 0 else 0
        rows.append({"name": ab["name"], "cast": cast, "cd": eff_cd, "dps": (cast / eff_cd) if eff_cd and cast else 0.0, "modelled": bool(eff_cd and cast)})
    hp = num((hero.get("starting_stats") or {}).get("max_health", {}).get("value", 0)) + tot["hp"]
    abil = sum(r["dps"] for r in rows)
    ehp_b, ehp_s = hp / (1 - min(tot["bres"], 90) / 100), hp / (1 - min(tot["sres"], 90) / 100)
    sustain = gun * tot["bls"] / 100 + abil * tot["sls"] / 100            # health per second from lifesteal at full uptime
    return {"sustain": sustain, "power_index": (gun + abil) * (ehp_b + ehp_s) / 2 / 1000,
            "gun_dps": gun, "abil_dps": abil, "abilities": rows, "sp": sp, "hp": hp,
            "ehp_bullet": hp / (1 - min(tot["bres"], 90) / 100), "ehp_spirit": hp / (1 - min(tot["sres"], 90) / 100),
            "gun_uptime": up, "tot": tot}


def tiers_from_order(order_ids):
    cnt = {}
    for aid in order_ids:
        cnt[aid] = cnt.get(aid, 0) + 1
    return {aid: min(max(n - 1, 0), 3) for aid, n in cnt.items()}


def weapon_info_of(hero, all_items):
    w = next((i for i in all_items if i["class_name"] == hero["items"].get("weapon_primary")), None)
    return (w or {}).get("weapon_info") or {}


def model_lines(ev, label="Build"):
    L = [f"| {label} | Gun DPS | Ability DPS (modelled) | Spirit power | Health | Bullet EHP | Spirit EHP | Lifesteal HP/s | Fight power (damage x toughness) |",
         "|---|---|---|---|---|---|---|---|---|",
         f"| total | {ev['gun_dps']:.1f} | {ev['abil_dps']:.1f} | {ev['sp']:.0f} | {ev['hp']:.0f} | {ev['ehp_bullet']:.0f} | {ev['ehp_spirit']:.0f} | {ev['sustain']:.1f} | {ev['power_index']:.0f} |"]
    return L


def stat_value_table(hero, kit, winfo, tiers):
    """What each extra stat is worth in the model, at a reference of 100 spirit power and all upgrades bought."""
    ref = dict(ZERO, sp=100.0)
    base = kit_eval(hero, kit, winfo, ref, tiers)
    b = base["gun_dps"] + base["abil_dps"]
    L = [f"Reference: 100 spirit power, no other stats, all ability tiers bought -> gun {base['gun_dps']:.1f} + abilities {base['abil_dps']:.1f} = {b:.1f} DPS (relative).", ""]
    for lab, key, amt in (("+10 spirit power", "sp", 10), ("+10% fire rate", "fr", 10), ("+10% weapon damage", "wd", 10), ("+10% ability cooldown reduction", "cdr", 10), ("+10% max ammo", "ammo", 10)):
        t2 = dict(ref)
        t2[key] += amt
        e = kit_eval(hero, kit, winfo, t2, tiers)
        L.append(f"- {lab}: +{e['gun_dps'] + e['abil_dps'] - b:.2f} DPS (gun +{e['gun_dps'] - base['gun_dps']:.2f}, abilities +{e['abil_dps'] - base['abil_dps']:.2f})")
    unmod = [r["name"] for r in base["abilities"] if not r["modelled"]]
    if unmod:
        L.append(f"- NOT modelled (summons, damage over time, passives or no direct damage): {', '.join(unmod)}. These can make spirit power worth more than shown.")
    return L, base
