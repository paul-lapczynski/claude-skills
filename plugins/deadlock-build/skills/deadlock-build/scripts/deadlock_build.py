#!/usr/bin/env python3
"""Deadlock item-build generator.

Everything is pulled live from api.deadlock-api.com (hero/ability/item definitions,
patch notes, and match statistics), so the output follows the current patch:

    python deadlock_build.py Mina
    python deadlock_build.py Mina --vs "Haze,Seven,Lady Geist" --min-badge 80

What it does
  1. Reads the hero's real abilities and scores every ability upgrade (power spikes).
  2. Reads the hero's kit + roster-relative stats to say which archetype it favours
     (Aggressive / Farming / Support).
  3. Scores every shop item per hero: win-rate lift vs same-tier items, per time window,
     blended with how well the item's stats fit this kit and the archetype.
  4. Orders 12 items per archetype around the soul thresholds where the spikes land,
     flagging "save up" buys, and lists situational/counter picks.
"""
import argparse, base64, hashlib, json, math, os, re, struct, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.deadlock-api.com"
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "deadlock-build")
CACHE_TTL = 2 * 3600
MIN_HERO_MATCHES = 3000          # below this the patch window is too thin -> widen
FALLBACK_DAYS = 21
WINDOWS = {"early": (0, 600), "mid": (600, 1500), "late": (1500, None)}
SHRINK_K = 300                   # pseudo-matches pulling item win rates toward tier peers
NICHE_PICK_PCT = 10.0             # below this an item is "niche": its lift is often survivorship
MAX_NICHE = 2                    # niche items allowed per build
MIN_PICK_PCT = 3.0               # items bought in fewer games than this are "unproven" - never core
# Win rate depends heavily on WHEN an item is bought (late T1 buyers are losing), so each tier is
# judged in the windows where it is normally bought.
NATURAL_WINDOWS = {1: ["early"], 2: ["early", "mid"], 3: ["mid", "late"], 4: ["mid", "late"]}

# ---------------------------------------------------------------- HTTP / cache

STATIC = re.compile(r"^/v1/(assets/|patches)")     # changes only with a game patch -> cached per game build, no expiry
PATCH_TTL = 24 * 3600                               # patch-note list can grow without a new build
BUILD_TTL = 600


def _fetch(url):
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deadlock-build-gen/1.0"})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            time.sleep(2 * (attempt + 1) if e.code == 429 else 1)
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # network blips
            last = e
            time.sleep(1 + attempt)
    raise SystemExit(f"API request failed: {url} ({last})")


def _read(f):
    with open(f, encoding="utf-8") as fh:
        return json.load(fh)


def _write(f, data):
    os.makedirs(os.path.dirname(f), exist_ok=True)
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def game_build(refresh=False):
    """Current client build, re-checked at most every 10 minutes."""
    f = os.path.join(CACHE, "current_build.json")
    if not refresh and os.path.exists(f) and time.time() - os.path.getmtime(f) < BUILD_TTL:
        return _read(f)["build"]
    b = _fetch(f"{API}/v1/assets/steam-info").get("client_version")
    _write(f, {"build": b})
    return b


def get(path, params=None, refresh=False):
    """Static game data (heroes, items, abilities, patch list) is cached forever per game build in
    ~/.cache/deadlock-build/build_<N>/. Match statistics expire after 2 hours."""
    q = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{API}{path}" + (f"?{q}" if q else "")
    name = hashlib.sha1(url.encode()).hexdigest() + ".json"
    if path != "/v1/assets/steam-info" and STATIC.match(path):
        f = os.path.join(CACHE, f"build_{game_build(refresh)}", name)
        ttl = PATCH_TTL if path.startswith("/v1/patches") else None
    else:
        f = os.path.join(CACHE, name)
        ttl = BUILD_TTL if path == "/v1/assets/steam-info" else CACHE_TTL
    if not refresh and os.path.exists(f) and (ttl is None or time.time() - os.path.getmtime(f) < ttl):
        return _read(f)
    data = _fetch(url)
    _write(f, data)
    return data


# ---------------------------------------------------------------- small helpers

def fetch_image(url):
    """Download an image (cached) -> (bytes, mime) or None if unavailable."""
    if not url:
        return None
    os.makedirs(CACHE, exist_ok=True)
    f = os.path.join(CACHE, hashlib.sha1(url.encode()).hexdigest() + os.path.splitext(url)[1])
    if not os.path.exists(f):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "deadlock-build-gen/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r, open(f, "wb") as fh:
                fh.write(r.read())
        except Exception as e:
            print(f"note: could not download hero image ({e})", file=sys.stderr)
            return None
    ext = os.path.splitext(f)[1].lower()
    with open(f, "rb") as fh:
        return fh.read(), {".webp": "image/webp", ".png": "image/png"}.get(ext, "application/octet-stream")


def num(x, default=0.0):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(x)) or default)
    except ValueError:
        return default


def plain(html):
    html = re.sub(r"<svg.*?</svg>", "", html or "", flags=re.S)
    html = re.sub(r"<br\s*/?>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def pct_rank(values):
    """Percentile (0..1) of each value within the list; ties share the average rank."""
    n = len(values)
    if n <= 1:
        return [0.5] * n
    order = sorted(range(n), key=lambda i: values[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 / (n - 1)
        i = j + 1
    return out


def mmss(s):
    return f"{int(s // 60)}:{int(s % 60):02d}"


def zscores(vals):
    m = sum(vals) / len(vals)
    sd = math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals)) or 1.0
    return [(v - m) / sd for v in vals]


# ---------------------------------------------------------------- game context

def find_hero(name, heroes):
    pool = [h for h in heroes if h.get("player_selectable") and not h.get("disabled")]
    n = name.lower().strip()
    for key in ("name", "class_name"):
        for h in pool:
            if h[key].lower() == n:
                return h
    hits = [h for h in pool if n in h["name"].lower()]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"Hero '{name}' not found/ambiguous. Available: " + ", ".join(sorted(h["name"] for h in pool)))


def patch_context(refresh):
    steam = get("/v1/assets/steam-info", refresh=refresh)
    patches = get("/v1/patches", refresh=refresh)
    patches = sorted(patches, key=lambda p: p["pub_date"], reverse=True)
    latest = patches[0] if patches else None
    ts = int(datetime.fromisoformat(latest["pub_date"].replace("Z", "+00:00")).timestamp()) if latest else None
    return {"build": steam.get("client_version"), "build_date": steam.get("version_date"),
            "patch_title": latest["title"] if latest else "?", "patch_ts": ts}


# ---------------------------------------------------------------- ability / spike analysis

def ability_by_slot(hero, abilities):
    by_class = {a["class_name"]: a for a in abilities if a.get("type") == "ability"}
    out = []
    for slot in ("signature1", "signature2", "signature3", "signature4"):
        a = by_class.get(hero["items"].get(slot))
        if a:
            out.append(a)
    return out


def upgrade_gain(ab, pu):
    """Rough 'how much stronger does this ability get' (1.0 = +100% effective power)."""
    name, n = pu["name"], pu["name"].lower()
    b = num(pu["bonus"])
    base = num(ab["properties"].get(name, {}).get("value", 0))
    if pu.get("upgrade_type") == "EAddToScale" or pu.get("scale_stat_filter"):
        return 0.25, "scaling"
    if "cooldown" in n and "between" not in n:
        if base > 0 and b < 0:
            g = base / max(base + b, 0.1 * base) - 1          # extra uptime
            return g * (0.6 if "pertarget" in n else 0.9), "cooldown"
        return 0.2, "cooldown"
    if re.search(r"damage|heal|barrier|dps|dot|bats|lifesteal", n) and "resist" not in n:
        return (min(b / base, 1.5) if base > 0 else 0.4), "damage"
    if re.search(r"recast|charge|chain|count|bullets|stack|fire", n):
        return (min(b / base, 1.5) * 0.6 if base > 0 else 0.2), "mechanic"
    if re.search(r"duration|range|radius|slow|stun|silence|width", n):
        return (min(b / base, 1.0) * 0.5 if base > 0 else 0.2), "utility"
    return 0.15, "other"


def base_stats(ab):
    out = []
    for k, v in ab["properties"].items():
        if isinstance(v, dict) and v.get("label") and num(v.get("value")) > 0:
            out.append(f"{v['label']} {v['value']}{v.get('postfix') or ''}")
    return ", ".join(out[:3])


def tier_summary(ab, tier):
    d = ab.get("description") or {}
    t = plain(d.get(f"t{tier}_desc", ""))
    if t:
        return t
    parts = []
    for pu in (ab.get("upgrades") or [{}] * 3)[tier - 1].get("property_upgrades", []) if tier <= len(ab.get("upgrades") or []) else []:
        parts.append(f"{pu['name']} {pu['bonus']}")
    return ", ".join(parts)


def damage_type(ab):
    """spirit / weapon / none, from how the ability's damage properties scale."""
    spirit = weapon = 0
    for k, v in ab["properties"].items():
        if not isinstance(v, dict) or num(v.get("value")) == 0:
            continue
        sf = json.dumps(v.get("scale_function") or {}).lower()
        css = str(v.get("css_class", "")).lower()
        if "damage" in k.lower() or "tech_damage" in css:
            if "tech" in sf or "tech" in css:
                spirit += 1
            elif "weapon" in sf or "bullet" in sf:
                weapon += 1
    return spirit, weapon


def build_spikes(hero, kit, order_row, rewards):
    """One entry per ability point in the chosen ability order, with a power rating."""
    abs_by_id = {a["id"]: a for a in kit}
    cnt, picks = {}, []
    for aid in order_row["abilities"]:
        if aid in abs_by_id:
            cnt[aid] = cnt.get(aid, 0) + 1
            picks.append((cnt[aid] == 1, AP_COST.get(cnt[aid] - 1, 5)))
    sched = pick_schedule(hero, picks)
    seen, steps = {}, []
    for i, aid in enumerate(order_row["abilities"]):
        ab = abs_by_id.get(aid)
        if not ab:
            continue
        seen[aid] = seen.get(aid, 0) + 1
        n = seen[aid]
        ult = ab.get("ability_type") == "ultimate"
        k = len(steps)
        lvl, souls = sched[k] if k < len(sched) else (None, None)
        t = order_row["avg_upgrade_times_s"][i] if i < len(order_row["avg_upgrade_times_s"]) else None
        if n == 1:
            gain, kind = (3.0, "ULT ONLINE") if ult else (0.5, "new ability")
            text = plain((ab.get("description") or {}).get("desc", "")) or base_stats(ab)
            text = text[:140]
            cats = {"spirit", "cooldown"} if ult else {"spirit"}
        else:
            tier = n - 1
            ups = (ab.get("upgrades") or [])
            pus = ups[tier - 1].get("property_upgrades", []) if tier - 1 < len(ups) else []
            gl = [upgrade_gain(ab, pu) for pu in pus]
            gain = min(sum(g for g, _ in gl), 2.5) * (1.25 if ult else 1.0)
            kind = f"T{tier}"
            text = tier_summary(ab, tier)[:140]
            cats = set()
            for _, kd in gl:
                cats |= {"damage": {"spirit"}, "cooldown": {"cooldown"}, "mechanic": {"weapon", "cooldown"},
                         "utility": {"spirit"}, "scaling": {"spirit"}}.get(kd, set())
        steps.append({"step": k + 1, "ability": ab["name"], "kind": kind, "gain": gain, "major": gain >= 1.0 or kind == "ULT ONLINE",
                      "text": text, "time": t, "level": lvl, "souls": souls, "cats": cats, "ult": ult})
    return steps


def reward_schedule(hero):
    li = hero.get("level_info") or {}
    out = []
    for lvl in sorted(li, key=int):
        cur = li[lvl].get("bonus_currencies") or []
        if "EAbilityPoints" in cur or "EAbilityUnlocks" in cur:
            out.append((int(lvl), li[lvl].get("required_gold", 0)))
    return out


def pick_schedule(hero, picks):
    """Souls/level at which each ability pick becomes available. picks = [(is_unlock, cost)].
    Unlocks and upgrade points are separate currencies: an unlock costs 1 unlock, tiers T1/T2/T3 cost 1/2/5 points,
    and each level grants one of them (hero level_info says which)."""
    li = hero.get("level_info") or {}
    unl, pts = [], []
    for lvl in sorted(li, key=int):
        cur = li[lvl].get("bonus_currencies") or []
        row = (int(lvl), li[lvl].get("required_gold", 0))
        if "EAbilityUnlocks" in cur:
            unl.append(row)
        elif "EAbilityPoints" in cur:
            pts.append(row)
    out, cu, cp = [], 0, 0
    for is_unlock, cost in picks:
        if is_unlock:
            cu += 1
            out.append(unl[cu - 1] if cu <= len(unl) else (None, None))
        else:
            cp += cost
            out.append(pts[cp - 1] if cp <= len(pts) else (None, None))
    return out


def level_at(hero, souls):
    li = hero.get("level_info") or {}
    lv = 1
    for k in sorted(li, key=int):
        if li[k].get("required_gold", 0) <= souls:
            lv = int(k)
    return lv


# ---------------------------------------------------------------- kit / archetype analysis

KIT_PATTERNS = {
    "ally": r"\ballies\b|\bally\b|teammate|friendly|\bteam\b",
    "heal": r"heal|lifesteal|regenerat",
    "cc": r"stun|silence|slow|freeze|root|lift|knock|sleep|immobil|disarm|pull",
    "aoe": r"radius|area|nearby|cone|wave",
}


def kit_profile(hero, kit, roster_stats):
    sig = [a for a in kit]
    spirit = weapon = 0
    f = {k: 0 for k in KIT_PATTERNS}
    uses_per_min = 0.0
    for ab in sig:
        s, w = damage_type(ab)
        spirit += s
        weapon += w
        blob = (json.dumps(ab["properties"]) + " " + plain(json.dumps(ab.get("description") or {}))).lower()
        for k, pat in KIT_PATTERNS.items():
            if re.search(pat, blob):
                f[k] += 1
        cd = num(ab["properties"].get("AbilityCooldown", {}).get("value", 0))
        if cd > 0 and ab.get("ability_type") != "ultimate":
            uses_per_min += 60 / cd
    total = spirit + weapon
    spirit_w = spirit / total if total else 0.6 if hero.get("hero_type") == "mystic" else 0.4
    weapon_w = max(0.2, 1 - spirit_w * 0.9)
    if hero.get("hero_type") == "marksman":
        weapon_w = max(weapon_w, 0.85)
    tags = " ".join(hero.get("tags", [])).lower()

    # roster-relative stats (z-scores of per-match averages)
    rs = roster_stats
    me = rs.get(hero["id"])
    zs = {}
    if me and len(rs) > 5:
        ids = list(rs)
        for metric in ("damage", "kills", "assists", "last_hits", "creep", "networth", "deaths"):
            vals = [rs[i][metric] for i in ids]
            zs[metric] = zscores(vals)[ids.index(hero["id"])]
    z = lambda m: zs.get(m, 0.0)

    kit_agg = 1.0 * (len(sig) - f["heal"] * 0.3) / len(sig) + 0.25 * f["cc"] + (0.5 if re.search(r"burst|assassin|harass|duel|lethal|one shot|bloodthirst|cruel|merciless", tags) else 0)
    kit_farm = 0.35 * f["aoe"] + 0.15 * uses_per_min + (0.5 if re.search(r"farm|wave|summon|necromancer|inventor|explosive|arson|area denial", tags) else 0)
    kit_sup = 0.7 * f["ally"] + 0.15 * f["heal"] + 0.15 * f["cc"] + (0.8 if re.search(r"support|helpful|protector|savior|teamplay|team-up|tag-team|bodyguard|strategist|tactician", tags) else 0)
    stat_agg = 0.6 * z("damage") + 0.4 * z("kills")
    stat_farm = 0.5 * z("last_hits") + 0.3 * z("creep") + 0.2 * z("networth")
    stat_sup = 0.6 * z("assists") - 0.2 * z("kills") - 0.2 * z("damage")
    raw = {"Aggressive": 0.6 * kit_agg + 0.4 * stat_agg, "Farming": 0.6 * kit_farm + 0.4 * stat_farm, "Support": 0.6 * kit_sup + 0.4 * stat_sup}
    ex = {k: math.exp(1.5 * v) for k, v in raw.items()}
    scores = {k: round(100 * v / sum(ex.values())) for k, v in ex.items()}
    return {"spirit_w": spirit_w, "weapon_w": weapon_w, "flags": f, "scores": scores, "z": zs, "tags": hero.get("tags", []),
            "uses_per_min": uses_per_min}


def roster_table(hs):
    out = {}
    for r in hs:
        m = r["matches"] or 1
        out[r["hero_id"]] = {"damage": r["total_player_damage"] / m, "kills": r["total_kills"] / m,
                             "assists": r["total_assists"] / m, "last_hits": r["total_last_hits"] / m,
                             "creep": r["total_creep_damage"] / m, "networth": r["total_net_worth"] / m,
                             "deaths": r["total_deaths"] / m, "wr": r["wins"] / m, "matches": r["matches"]}
    return out


# ---------------------------------------------------------------- item model

CAT_PROPS = {
    "spirit":   r"^(TechPower|SpiritPower|BonusSpirit\w*|ImbuedTechPower|TechRangeMultiplier|TechRadiusMultiplier|MagicResistReduction|SpiritDamage)$",
    "cooldown": r"^(CooldownReduction|CooldownBetweenChargeReduction|BonusAbilityCharges|BonusAbilityDurationPercent|CooldownReductionOnChargedAbilities)$",
    "weapon":   r"^(BaseAttackDamagePercent\w*|BonusFireRate|BonusClipSizePercent|BonusBulletSpeedPercent|BulletArmorReduction|BulletResistReduction|HeadShotBonusDamage|CloseRangeBonusWeaponPower|LongRangeBonusWeaponPower|WeaponPowerPerStack|BonusAttackRangePercent|AmmoReloadPercent|BulletLifestealPercent)$",
    "sustain":  r"^(AbilityLifestealPercentHero|BulletLifestealPercent|OutOfCombatHealthRegen|BonusHealthRegen|HealPercentAmount|Regeneration|TotalHealthRegen|HealAmpRegenPercent|HealAmpCastPercent|LifestrikeHeal)$",
    "health":   r"^(BonusHealth|CombatBarrier|VexBarrierCombatBarrier)$",
    "resist":   r"^(BulletResist|TechResist|MeleeResistPercent|TechArmorDamageReduction|TechDamageReduction|StatusResistancePercent|SlowResistancePercent)$",
    "mobility": r"^(BonusMoveSpeed|BonusSprintSpeed|ActiveBonusMoveSpeed|Stamina|GroundDashReductionPercent|AirControlPercent|StaminaCooldownReduction)$",
    "cc":       r"^(SlowPercent|StunDuration|MovementSpeedSlow|FireRateSlow|FreezeDuration|OutgoingDamagePenaltyPercent|MaxSlowPercent)$",
    "farm":     r"^(BonusSoulsPct|NPCDamageMult|NonPlayerBonusWeaponPower|NonHeroHealPct|NonHeroMult)$",
    "team":     r"^(CooldownReductionPctOnOthers|AuraRadius)$",
}
TEAM_TEXT = re.compile(r"\ballies\b|\bally\b|teammate|friendly")

COUNTERS = [  # (rule name, item property regex, enemy trait that triggers it, why)
    ("Anti-heal", r"^HealAmpReceivePenaltyPercent$", "heal", "enemy heals/lifesteals a lot"),
    ("Spirit resist", r"^TechResist$", "spirit", "enemy team is spirit-damage heavy"),
    ("Bullet resist", r"^BulletResist$", "gun", "enemy team is gun-damage heavy"),
    ("Status resist / CC protection", r"^(StatusResistancePercent|SlowResistancePercent|StunDuration)$", "cc", "enemy has lots of stuns/slows/silences"),
    ("Burst protection (barrier)", r"^(CombatBarrier|VexBarrierCombatBarrier|DamageThreshold)$", "burst", "enemy can delete you from full health"),
    ("Armor shred", r"^(BulletResistReduction|BulletArmorReduction|MagicResistReduction)$", "tank", "enemy stacks resists / tanks"),
]


def item_profile(it):
    props = {k: v for k, v in it["properties"].items() if isinstance(v, dict) and num(v.get("value")) not in (0, -1)}
    cats = {c for c, pat in CAT_PROPS.items() for k in props if re.match(pat, k)}
    if TEAM_TEXT.search(plain(json.dumps(it.get("description") or {})).lower()):
        cats.add("team")
    shown = []
    for k, v in props.items():
        if v.get("label") and k not in ("AbilityUnitTargetLimit",):
            shown.append(f"{v['label']} {v['value']}{v.get('postfix', '') or ''}".replace("  ", " "))
    return {"props": set(props), "cats": cats, "stats": shown[:4], "active": bool(it.get("is_active_item"))}


ARCH = {
    "Aggressive": {"cats": {"spirit": "S", "weapon": "W", "cooldown": 0.6, "cc": 0.3, "mobility": 0.3, "health": 0.3, "sustain": 0.3, "resist": 0.1},
                   "tiers": {1: 0, 2: 3, 3: 5, 4: 4}},
    "Farming":    {"cats": {"farm": 1.0, "sustain": 0.8, "spirit": "S6", "weapon": "W6", "cooldown": 0.6, "mobility": 0.5, "health": 0.4, "resist": 0.1},
                   "tiers": {1: 0, 2: 4, 3: 4, 4: 4}},
    "Support":    {"cats": {"team": 1.0, "health": 0.7, "resist": 0.6, "cc": 0.6, "cooldown": 0.8, "sustain": 0.6, "mobility": 0.4, "spirit": "S4", "weapon": 0.1},
                   "tiers": {1: 0, 2: 4, 3: 5, 4: 3}},
}
MIN_CUM_BEFORE_TIER = {1: 0, 2: 0, 3: 2400, 4: 7000}     # sanity gate so T3/T4 aren't bought on 1 item
TIER_COST = {1: 800, 2: 1600, 3: 3200, 4: 6400}
BASE_SLOTS, FLEX_SLOTS = 9, 3      # inventory: 9 slots + 3 flex slots that unlock later in the game; categories unrestricted
KEEP_UPGRADES = 2                  # lane items that win early AND have a solid upgrade are upgraded and kept
STALE_T1 = 9600                    # souls spent after which a leftover 800-soul item is dead weight -> sell
EARLY_TARGETS = 7                  # only lane items whose upgrade lands within the first N purchases are bought early
LANE_ITEMS = 3                     # stepping-stone items bought for the lane phase


def arch_weights(arch, kit):
    w = {}
    for c, v in ARCH[arch]["cats"].items():
        if isinstance(v, str):                      # kit-dependent weight
            base = kit["spirit_w"] if v[0] == "S" else kit["weapon_w"]
            w[c] = base * (float(v[1:]) / 10 if len(v) > 1 else 1.0)
        else:
            w[c] = v
    return w


def fit(cats, weights):
    top = sum(sorted(weights.values(), reverse=True)[:3]) or 1
    return min(sum(weights.get(c, 0) for c in cats) / top, 1.0)


def fetch_item_stats(hero_id, flt, refresh, enemy=None, window=None):
    p = dict(flt, hero_id=hero_id, min_matches=30)
    if window:
        p["min_bought_at_s"], p["max_bought_at_s"] = window
    if enemy:
        p["enemy_hero_ids"] = ",".join(map(str, enemy))
    return {r["item_id"]: r for r in get("/v1/analytics/item-stats", p, refresh)}


def score_items(items, stats_all, stats_win, hero_matches, kit):
    """Return per-item metrics: shrunk lift vs same-tier peers (overall and per window)."""
    def peers(stats):
        agg = {}
        for iid, r in stats.items():
            it = items.get(iid)
            if it:
                a = agg.setdefault(it["item_tier"], [0.0, 0.0])
                wt = math.sqrt(r["matches"])           # dampen very popular items
                a[0] += wt * r["wins"] / r["matches"]
                a[1] += wt
        return {t: (w / m if m else 0.5) for t, (w, m) in agg.items()}

    def lift(r, p0):
        n = r["matches"]
        return ((r["wins"] + p0 * SHRINK_K) / (n + SHRINK_K) - p0) * 100

    p_all = peers(stats_all)
    p_win = {w: peers(s) for w, s in stats_win.items()}
    out = {}
    for iid, r in stats_all.items():
        it = items.get(iid)
        if not it or r["matches"] < max(100, 0.004 * hero_matches):
            continue
        t = it["item_tier"]
        m = {"item": it, "n": r["matches"], "wr": r["wins"] / r["matches"] * 100, "pick": r["matches"] / hero_matches * 100,
             "buy_s": r["avg_buy_time_s"], "lift_all": lift(r, p_all[t]), "win": {}}
        for w, s in stats_win.items():
            rw = s.get(iid)
            if rw and rw["matches"] >= 60:
                m["win"][w] = (lift(rw, p_win[w].get(t, 0.5)), rw["matches"])
        nat = [(m["win"][w][0], m["win"][w][1], w) for w in NATURAL_WINDOWS[t] if w in m["win"]]
        if nat:
            tot = sum(n for _, n, _ in nat)
            m["lift"] = sum(l * n for l, n, _ in nat) / tot
            m["wlabel"] = max(nat, key=lambda x: x[1])[2] if len(nat) == 1 else "+".join(w for _, _, w in nat)
        else:
            m["lift"], m["wlabel"] = m["lift_all"], "overall"
        out[iid] = m
    return out


def composite(metrics, arch, kit):
    """Blend data lift, kit fit and archetype fit -> 0..1 per item, ranked within its tier."""
    weights = arch_weights(arch, kit)
    kitw = {"spirit": kit["spirit_w"], "weapon": kit["weapon_w"], "cooldown": 0.5 * kit["spirit_w"] + 0.1,
            "sustain": 0.4, "health": 0.3, "resist": 0.2, "mobility": 0.3, "cc": 0.2 + 0.1 * kit["flags"]["cc"], "farm": 0.2, "team": 0.1}
    res = {}
    for t in (1, 2, 3, 4):
        ids = [i for i, m in metrics.items() if m["item"]["item_tier"] == t]
        if not ids:
            continue
        lifts = pct_rank([metrics[i]["lift"] for i in ids])
        kf = pct_rank([fit(metrics[i]["prof"]["cats"], kitw) for i in ids])
        af = pct_rank([fit(metrics[i]["prof"]["cats"], weights) for i in ids])
        for j, i in enumerate(ids):
            res[i] = 0.5 * lifts[j] + 0.2 * kf[j] + 0.3 * af[j]
    return res


# ---------------------------------------------------------------- build assembly

def build_tree(all_items):
    """Upgrade tree from the live item data: component_items lists the items consumed by an upgrade."""
    by_cn = {x["class_name"]: x for x in all_items if x.get("type") == "upgrade"}
    by_id = {x["id"]: x for x in by_cn.values()}

    def comps(it):
        return [by_cn[c] for c in it.get("component_items", []) if c in by_cn and by_cn[c].get("cost")]

    def closure(it):
        out, stack = {}, list(comps(it))
        while stack:
            c = stack.pop()
            if c["id"] not in out:
                out[c["id"]] = c
                stack += comps(c)
        return out

    return {"by_id": by_id, "comps": comps, "closure": closure}


def pick_build(arch, metrics, comp, kit, tree):
    """Pick the FINAL inventory. Highest tier first; an item is skipped if it is a component of (or built
    from) something already chosen - the upgrade would consume it."""
    chosen, covered, niche = [], set(), 0

    def conflicts(i):
        cl = set(tree["closure"](metrics[i]["item"]))
        return i in covered or bool(cl & set(chosen)) or bool(cl & covered)

    def take(i):
        chosen.append(i)
        covered.update(tree["closure"](metrics[i]["item"]))

    for tier in (4, 3, 2, 1):
        want = ARCH[arch]["tiers"][tier]
        if not want:
            continue
        cands = [i for i in metrics if metrics[i]["item"]["item_tier"] == tier and metrics[i]["lift"] > (-0.5 if tier < 4 else 0.0)
                 and metrics[i]["pick"] >= MIN_PICK_PCT]
        cands.sort(key=lambda i: -comp[i])
        got = 0
        for i in cands:
            if conflicts(i):
                continue
            if metrics[i]["pick"] < NICHE_PICK_PCT:
                if niche >= MAX_NICHE:
                    continue
                niche += 1
            take(i)
            got += 1
            if got == want:
                break
    if len(chosen) < BASE_SLOTS + FLEX_SLOTS:               # tiers couldn't fill -> best remaining overall
        rest = sorted((i for i in metrics if i not in chosen and metrics[i]["item"]["item_tier"] >= 2
                       and metrics[i]["pick"] >= MIN_PICK_PCT and metrics[i]["lift"] > 0),
                      key=lambda i: -comp[i])
        for i in rest:
            if len(chosen) >= BASE_SLOTS + FLEX_SLOTS:
                break
            if not conflicts(i):
                take(i)
    if len(chosen) < BASE_SLOTS + FLEX_SLOTS:               # still short (thin data): relax the bars, never leave a slot empty
        rest = sorted((i for i in metrics if i not in chosen and metrics[i]["item"]["item_tier"] >= 2 and metrics[i]["pick"] >= 1.0),
                      key=lambda i: -comp[i])
        for i in rest:
            if len(chosen) >= BASE_SLOTS + FLEX_SLOTS:
                break
            if not conflicts(i):
                take(i)
    # upgrade-and-keep: a popular lane item that wins early (e.g. a stamina or health line) should be upgraded to
    # its solid parent and kept, not left as an 800-soul item or dropped
    t1s = sorted((i for i in metrics if metrics[i]["item"]["item_tier"] == 1 and "early" in metrics[i]["win"]
                  and metrics[i]["win"]["early"][0] >= 1.0 and metrics[i]["pick"] >= 20), key=lambda i: -metrics[i]["win"]["early"][0])
    keeps = 0
    for i in t1s:
        if keeps >= KEEP_UPGRADES:
            break
        cn = metrics[i]["item"]["class_name"]
        parents = [q for q in metrics if cn in (metrics[q]["item"].get("component_items") or []) and metrics[q]["item"]["item_tier"] in (2, 3)
                   and metrics[q]["lift"] > -1.2 and metrics[q]["pick"] >= 15 and q not in chosen]
        if not parents or i in covered:
            continue
        q = max(parents, key=lambda x: metrics[x]["lift"])
        if conflicts(q):
            continue
        if len(chosen) >= BASE_SLOTS + FLEX_SLOTS:
            chosen.remove(min(chosen, key=lambda x: comp[x]))
            covered.clear()
            for c in chosen:
                covered.update(tree["closure"](metrics[c]["item"]))
        take(q)
        keeps += 1
    return chosen


def late_swaps(final, metrics, comp, tree, limit=3):
    """When rich with full slots: sell the weakest late-game finals for stronger unbought items."""
    owned_cover = set(final)
    for f in final:
        owned_cover |= set(tree["closure"](tree["by_id"][f]))
    weak = sorted((f for f in final if f in metrics), key=lambda f: (metrics[f]["item"]["item_tier"], comp.get(f, 0)))
    pool = sorted((i for i in metrics if i not in owned_cover and metrics[i]["item"]["item_tier"] >= 3
                   and metrics[i]["lift"] > 0.5 and metrics[i]["pick"] >= MIN_PICK_PCT), key=lambda i: -comp.get(i, 0))
    out = []
    for f in weak:
        if len(out) >= limit:
            break
        for i in pool:
            if metrics[i]["lift"] >= metrics[f]["lift"] + 0.5 and metrics[i]["item"]["item_tier"] > metrics[f]["item"]["item_tier"] and not any(i == o["buy"] for o in out)                     and not (set(tree["closure"](metrics[i]["item"])) & (set(final) - {f})):
                out.append({"sell": f, "buy": i})
                break
    return out


def make_clock(spikes, end_souls, duration):
    """souls -> typical game time (s). Early part from real ability-point timings (reliable while
    players spend points on time); later part interpolated to the average end-of-game net worth."""
    pts = sorted({(s["souls"], s["time"]) for s in spikes if s["souls"] is not None and s["time"] and s["time"] <= 900})
    if end_souls and duration:
        pts.append((end_souls, duration))
    def t(souls):
        if not pts:
            return 0
        if souls <= pts[0][0]:
            return pts[0][1]
        for (s0, t0), (s1, t1) in zip(pts, pts[1:]):
            if s0 <= souls <= s1:
                return t0 + (t1 - t0) * (souls - s0) / max(s1 - s0, 1)
        return pts[-1][1]
    return t


def sequence(chosen, metrics, comp, spikes, clock):
    """Greedy: value-per-cost, with a bonus for items that amplify the next major spike."""
    remaining, order, cum = list(chosen), [], 0
    majors = [s for s in spikes if s["major"] and s["souls"] is not None]
    while remaining:
        nxt = next((s for s in majors if s["souls"] > cum), None)
        best, best_d = None, -1
        for i in remaining:
            m = metrics[i]
            t = m["item"]["item_tier"]
            if cum < MIN_CUM_BEFORE_TIER[t]:
                continue
            v = comp[i] + (0.2 if nxt and (m["prof"]["cats"] & nxt["cats"]) else 0)
            # real purchase timing is a prior: don't buy far ahead of when winning players do
            v -= 0.15 * max(-1.0, min(1.0, (m["buy_s"] - clock(cum + m["item"]["cost"])) / 900))
            if t == 1:                                      # laning items first, leftovers late
                v += 0.3 if cum < 3200 else -0.3
            d = v / (m["item"]["cost"] / 800) ** 0.4
            if d > best_d:
                best, best_d = i, d
        if best is None:                                    # nothing allowed yet -> cheapest
            best = min(remaining, key=lambda i: metrics[i]["item"]["cost"])
        cheaper_left = [i for i in remaining if metrics[i]["item"]["cost"] < metrics[best]["item"]["cost"] / 2 and i != best]
        saveup = bool(cheaper_left) and metrics[best]["item"]["cost"] >= 3200
        amp = bool(nxt and (metrics[best]["prof"]["cats"] & nxt["cats"]))
        remaining.remove(best)
        cum += metrics[best]["item"]["cost"]
        order.append({"id": best, "cum": cum, "eta": clock(cum), "saveup": saveup, "amp": amp, "spike_for": nxt})
    return order


def plan_purchases(order, comp, tree, clock, metrics):
    """Turn the ordered final items into real purchase steps: lane stepping stones, upgrades that consume
    their components (price is credited), and sells when the inventory is full (9 slots, 12 once flex unlock)."""
    by_id, comps, closure = tree["by_id"], tree["comps"], tree["closure"]
    targets = [st["id"] for st in order]
    meta = {st["id"]: st for st in order}
    owned, steps, paid = [], [], 0

    def needed(done):                               # items still useful to hold: unbought targets and their components
        out = set()
        for t in targets:
            if t not in done:
                out.add(t)
                out |= set(closure(by_id[t]))
        return out

    done = set()

    def step(kind, iid, consumes=(), credit=0, **kw):
        nonlocal paid
        it = by_id[iid]
        pay = 0 if kind == "sell" else it["cost"] - credit
        paid += pay
        steps.append({"kind": kind, "id": iid, "pay": pay, "consumes": list(consumes), "slots": len(owned), "cum": paid,
                      "eta": clock(paid), **kw})

    def buy(iid, **kw):
        it = by_id[iid]
        for c in comps(it):                          # make sure the direct components are held first (stepping stones)
            if c["id"] not in owned:
                buy(c["id"], kind_override="stone", into=kw.get("into") or iid)
        used = [c["id"] for c in comps(it) if c["id"] in owned]
        credit = sum(by_id[c]["cost"] for c in used)
        for c in used:
            owned.remove(c)
        owned.append(iid)
        kind = kw.pop("kind_override", None) or ("upgrade" if used else "buy")
        step(kind, iid, used, credit, **kw)

    def enforce(cap):
        while len(owned) > cap:
            keep = needed(done)
            spare = [o for o in owned if o not in keep and o not in done]
            pool = spare or [o for o in owned if o not in done]
            if not pool:
                return
            victim = min(pool, key=lambda o: (by_id[o]["item_tier"], comp.get(o, 0)))
            owned.remove(victim)
            step("sell", victim, note="slot needed" if spare else "slot needed - rebuy later")

    # lane: first LANE_ITEMS first-tier items, taken from the upgrade chains of the earliest targets
    lane = []
    for t in targets[:EARLY_TARGETS]:
        for c in ([by_id[t]] if by_id[t]["item_tier"] == 1 else sorted(closure(by_id[t]).values(), key=lambda x: x["item_tier"])):
            if c["item_tier"] == 1 and c["id"] not in lane:
                lane.append(c["id"])
    if len(lane) < LANE_ITEMS:                      # no upgrade chain to lean on -> best lane items, sold later
        pool = sorted((i for i in metrics if metrics[i]["item"]["item_tier"] == 1 and metrics[i]["pick"] >= MIN_PICK_PCT
                       and metrics[i]["lift"] > 0 and i not in lane and i not in targets), key=lambda i: -comp.get(i, 0))
        lane += pool[:LANE_ITEMS - len(lane)]
    for lid in lane[:LANE_ITEMS]:
        into = next((t for t in targets if lid in closure(by_id[t])), None)
        if lid in targets:
            done.add(lid)
            buy(lid, target=True, st=meta[lid])
        elif into:
            buy(lid, kind_override="stone", into=into)
        else:
            buy(lid, kind_override="temp")
    for t in targets:
        if t in done:
            continue
        buy(t, target=True, st=meta[t])
        done.add(t)
        keep = needed(done)
        for o in list(owned):                       # stale 800-soul items do nothing late: sell
            if o not in keep and o not in done and by_id[o]["item_tier"] == 1 and paid >= STALE_T1:
                owned.remove(o)
                step("sell", o, note="too weak this late")
        enforce(BASE_SLOTS if len(done) <= BASE_SLOTS else BASE_SLOTS + FLEX_SLOTS)
    # finish every upgrade chain: a final item that can still become something better is a half-built item
    def shop_ok(x):
        return x.get("shopable") and not x.get("disabled") and x.get("item_tier") in (1, 2, 3, 4) and x.get("cost") and x.get("cost") != 9999
    changed = True
    while changed:
        changed = False
        for iid in list(owned):
            if iid not in done:
                continue
            opts = [q for q in by_id.values() if shop_ok(q) and q["id"] not in owned and iid in [c["id"] for c in comps(q)]]
            if not opts:
                continue
            best = max(opts, key=lambda q: (metrics[q["id"]]["lift"] if q["id"] in metrics else -0.5, q["item_tier"]))
            buy(best["id"], target=True, st={"saveup": False, "amp": False}, finish=True)
            done.add(best["id"])
            done.discard(iid)
            changed = True
    for k, st in enumerate(steps):                  # what each sale makes room for
        if st["kind"] == "sell":
            st["replace"] = next((x["id"] for x in steps[k + 1:] if x["kind"] != "sell"), None)
    return steps, owned


def why(m, extra=""):
    t = m["item"]["item_tier"]
    bits = [f"{m['lift']:+.1f}pp vs T{t} peers ({m['wlabel']})", f"{m['pick']:.0f}% pick", f"bought ~{mmss(m['buy_s'])}"]
    cats = sorted(m["prof"]["cats"])
    stats = "; ".join(m["prof"]["stats"][:2])
    return ", ".join(bits) + (f" | {'/'.join(cats)}" if cats else "") + (f" | {stats}" if stats else "") + extra


# ---------------------------------------------------------------- enemy traits

def enemy_traits(enemy_heroes):
    t = {"heal": 0, "spirit": 0, "gun": 0, "cc": 0, "burst": 0, "tank": 0}
    for h in enemy_heroes:
        abilities = get(f"/v1/assets/items/by-hero-id/{h['id']}")
        kit = ability_by_slot(h, abilities)
        blob = " ".join(json.dumps(a["properties"]) + plain(json.dumps(a.get("description") or {})) for a in kit).lower()
        s = w = 0
        for a in kit:
            ss, ww = damage_type(a)
            s += ss
            w += ww
        if re.search(r"heal|lifesteal", blob):
            t["heal"] += 1
        if (s / (s + w) if s + w else 0.5) > 0.6 or h.get("hero_type") == "mystic":
            t["spirit"] += 1
        if h.get("hero_type") == "marksman" or (s + w and w / (s + w) > 0.5):
            t["gun"] += 1
        if re.search(r"stun|silence|slow|freeze|root|sleep|immobil", blob):
            t["cc"] += 1
        if re.search(r"burst|one shot|assassin", " ".join(h.get("tags", [])).lower()) or h.get("hero_type") == "assassin":
            t["burst"] += 1
        if h.get("hero_type") == "brawler" or "tank" in " ".join(h.get("tags", [])).lower():
            t["tank"] += 1
    return t


# ---------------------------------------------------------------- main

def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("facts", "render"):          # skill commands live in deadlock_skill.py
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import deadlock_skill
        return {"facts": deadlock_skill.cmd_facts, "render": deadlock_skill.cmd_render}[sys.argv[1]](sys.argv[2:])
    ap = argparse.ArgumentParser(description="Deadlock item build generator (live data, current patch)")
    ap.add_argument("hero")
    ap.add_argument("--min-badge", type=int, default=70, help="min average match badge (70=Archon, 80=Oracle, 90=Phantom)")
    ap.add_argument("--vs", default="", help='comma list of enemy heroes, e.g. "Haze,Seven"')
    ap.add_argument("--all-time", action="store_true", help="ignore the patch date; use the last %d days" % FALLBACK_DAYS)
    ap.add_argument("--refresh", action="store_true", help="ignore the local cache")
    ap.add_argument("--out", help="also write markdown to this path (and an .html next to it)")
    ap.add_argument("--account-id", help="your Deadlock account id (3rd field of a build you copied in-game); saved for next time")
    ap.add_argument("--html", help="write a styled HTML report to this path")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    R = a.refresh
    global IMPORT_IDS
    acct = resolve_account_id(a.account_id)
    if acct is None:
        print("note: no --account-id given; import strings need one (copy any build in-game, decode it, or pass --account-id)", file=sys.stderr)
    IMPORT_IDS = (1, acct or 0, int(time.time()))

    heroes = get("/v1/assets/heroes", refresh=R)
    hero = find_hero(a.hero, heroes)
    hero = get(f"/v1/assets/heroes/{hero['id']}", refresh=R)
    pc = patch_context(R)
    abilities = get(f"/v1/assets/items/by-hero-id/{hero['id']}", refresh=R)
    kit = ability_by_slot(hero, abilities)
    all_items = get("/v1/assets/items", refresh=R)
    items = {i["id"]: i for i in all_items
             if i.get("type") == "upgrade" and i.get("shopable") and not i.get("disabled")
             and i.get("item_tier") in (1, 2, 3, 4) and i.get("cost") and (not i.get("heroes") or hero["id"] in i["heroes"])}

    # --- stat window: since the latest patch, widened if the sample is too thin
    now = int(time.time())
    flt = {"min_average_badge": a.min_badge}
    since = None if a.all_time else pc["patch_ts"]
    note = f"since patch '{pc['patch_title']}'"
    for attempt in (0, 1):
        if since:
            flt["min_unix_timestamp"] = since
        hs = get("/v1/analytics/hero-stats", flt, R)
        roster = roster_table(hs)
        if roster.get(hero["id"], {}).get("matches", 0) >= MIN_HERO_MATCHES or attempt:
            break
        since = now - FALLBACK_DAYS * 86400
        note = f"last {FALLBACK_DAYS} days (patch sample too thin)"
    if a.all_time:
        note = f"last {FALLBACK_DAYS} days"
        flt["min_unix_timestamp"] = now - FALLBACK_DAYS * 86400
        roster = roster_table(get("/v1/analytics/hero-stats", flt, R))
    tree = build_tree(all_items)
    ICONS.update({i["name"]: i.get("shop_image_webp") or i.get("shop_image") for i in tree["by_id"].values()
                  if i.get("shop_image_webp") or i.get("shop_image")})
    hero_matches = roster.get(hero["id"], {}).get("matches", 0)
    if not hero_matches:
        raise SystemExit("No match data for this hero in the window.")
    hero_wr = roster[hero["id"]]["wr"] * 100

    profile = kit_profile(hero, kit, roster)
    rewards = reward_schedule(hero)

    # --- ability order from real games
    ao = get("/v1/analytics/ability-order-stats", dict(flt, hero_id=hero["id"], min_matches=30), R)
    tot = sum(r["matches"] for r in ao) or 1
    cand = [r for r in ao if r["matches"] >= max(40, 0.01 * tot) and len(r["abilities"]) >= 8]
    shr = lambda r: (r["wins"] + 0.5 * 200) / (r["matches"] + 200)
    best_order = max(cand or ao, key=shr)
    pop_order = max(cand or ao, key=lambda r: r["matches"])
    spikes = build_spikes(hero, kit, best_order, rewards)

    # --- item statistics
    stats_all = fetch_item_stats(hero["id"], flt, R)
    stats_win = {w: fetch_item_stats(hero["id"], flt, R, window=rng if rng[1] else (rng[0], 9999)) for w, rng in WINDOWS.items()}
    metrics = score_items(items, stats_all, stats_win, hero_matches, profile)
    for i, m in metrics.items():
        m["prof"] = item_profile(m["item"])

    rel = sorted(r["avg_buy_time_s"] / (r["avg_buy_time_relative"] / 100) for r in stats_all.values()
                 if r["matches"] >= 300 and r.get("avg_buy_time_relative", 0) > 5)
    duration = rel[len(rel) // 2] if rel else None                   # typical match length (s)
    clock = make_clock(spikes, roster[hero["id"]]["networth"], duration)
    builds = {}
    for arch in ARCH:
        comp = composite(metrics, arch, profile)
        chosen = pick_build(arch, metrics, comp, profile, tree)
        order = sequence(chosen, metrics, comp, spikes, clock)
        steps, final = plan_purchases(order, comp, tree, clock, metrics)
        swaps = late_swaps(final, metrics, comp, tree)
        imb = {}
        for st in steps:
            if st["kind"] == "sell":
                continue
            inherited = next((imb[c] for c in st.get("consumes", []) if c in imb), None)    # upgrades keep the imbue
            tab = imbue_target(tree["by_id"][st["id"]], kit)
            if inherited:
                imb[st["id"]] = inherited
            elif tab:
                imb[st["id"]] = tab["id"]
        builds[arch] = {"imbue": imb, "swaps": swaps, "comp": comp, "order": order, "chosen": chosen, "steps": steps, "final": final, "tree": tree}

    exports = {arch: make_export(hero, arch, builds[arch], metrics, best_order, pc, None) for arch in ARCH}

    # --- render
    L = []
    P = L.append
    P(f"# {hero['name']} - item build  (game build {pc['build']}, {pc['build_date']})")
    P(f"Data: {hero_matches:,} {hero['name']} matches, badge >= {a.min_badge}, {note}. Hero WR {hero_wr:.1f}%.")
    P("")
    P("## 1. Kit read")
    P(f"{hero['name']} - {hero.get('hero_type', '?')} / {hero.get('gun_tag', '?')} / tags: {', '.join(hero.get('tags', []))}")
    P(f"Damage scaling: {profile['spirit_w']*100:.0f}% spirit-based abilities, weapon relevance {profile['weapon_w']*100:.0f}%.")
    P("")
    sc = profile["scores"]
    ranked = sorted(sc.items(), key=lambda kv: -kv[1])
    for k, v in ranked:
        P(f"- **{k}** {v}%")
    margin = ranked[0][1] - ranked[1][1]
    P(f"=> Kit favours **{ranked[0][0]}**" + (f" (close to {ranked[1][0]} - flexible)" if margin < 20 else "") + ".")
    P("")
    P("Abilities (current numbers):")
    for ab in kit:
        cd = num(ab["properties"].get("AbilityCooldown", {}).get("value", 0))
        P(f"- {'[ULT] ' if ab.get('ability_type') == 'ultimate' else ''}**{ab['name']}**"
          + (f" ({cd:g}s cd)" if cd else "") + f": {plain((ab.get('description') or {}).get('desc', ''))[:170]}")
    P("")
    P("## 2. Ability order and power spikes")
    P(f"Best-performing common order in your bracket ({best_order['matches']:,} games, {best_order['wins']/best_order['matches']*100:.1f}% WR"
      + ("" if best_order is not pop_order else "; also the most popular") + "). Souls = net worth when the point unlocks.")
    P("")
    P("| # | Time | Souls (Lv) | Ability | Step | Power | What you get |")
    P("|---|------|-----------|---------|------|-------|--------------|")
    for s in spikes:
        flag = "**SPIKE** " if s["major"] else ""
        P(f"| {s['step']} | {mmss(s['time']) if s['time'] else '-'} | {s['souls'] if s['souls'] is not None else '-'} (L{s['level'] or '-'}) | {s['ability']} | {s['kind']} | {flag}{s['gain']:.2f} | {s['text']} |")
    P("")

    # default build assessment
    P("## 3. Is the default build good value?")
    P("Community picks (hero page) vs. win-rate lift against same-tier items. `lift` = shrunk WR minus tier-peer WR, in points.")
    P("")
    pop = hero.get("popular_items") or {}
    for phase in ("early_game", "mid_game", "late_game"):
        rows = sorted(pop.get(phase, []), key=lambda r: -r["pick_pct"])[:6]
        if not rows:
            continue
        P(f"**{phase.replace('_', ' ').title()}**")
        P("")
        P("| Item | Cost | Pick% | WR% | Lift | Verdict |")
        P("|------|------|-------|-----|------|---------|")
        for r in rows:
            m = metrics.get(r["item_id"])
            it = items.get(r["item_id"])
            if not it:
                continue
            if not m:
                P(f"| {it['name']} | {it['cost']} | {r['pick_pct']:.0f} | {r['winrate_pct']:.1f} | n/a | thin data |")
                continue
            v = "Strong - keep" if m["lift"] >= 0.5 else "Weak for cost - swap" if m["lift"] <= -0.7 else "Fine"
            P(f"| {it['name']} | {it['cost']} | {r['pick_pct']:.0f} | {r['winrate_pct']:.1f} | {m['lift']:+.1f} | {v} |")
        P("")
    traps = sorted([m for m in metrics.values() if m["lift"] < -0.8 and m["pick"] >= 8], key=lambda m: m["lift"])[:5]
    if traps:
        P("Popular but underperforming (trap picks): " + "; ".join(f"{m['item']['name']} ({m['lift']:+.1f}pp, {m['pick']:.0f}% pick)" for m in traps))
        P("")

    # builds
    budget = roster[hero["id"]]["networth"] * 1.1
    P("## 4. Builds")
    P(f"Each build is a **final 12-slot inventory ({BASE_SLOTS} slots + {FLEX_SLOTS} flex, any category)**, bought in the order below. "
      "Upgrades consume their component items (Upgrade rows), so a component is never counted as a separate final item; "
      "the price of consumed components is credited (assumption: upgrades cost the difference). "
      f"Lane-only items (800-soul tier) are sold once they stop mattering or when a slot is needed - see Sell order. **Primary for {hero['name']}: {ranked[0][0]}.**")
    P("`SAVE UP` = expensive item worth skipping cheaper filler for. `Slots` = items held after the purchase (flex slots open late, so 10-12 only in the last buys).")
    for arch, _ in ranked:
        b = builds[arch]
        P("")
        P(f"### {arch}" + (" (recommended)" if arch == ranked[0][0] else ""))
        P(f"Focus: {arch_desc(arch)}" + (f"  *Off-kit for {hero['name']} ({sc[arch]}% fit) - only if the game demands it.*" if sc[arch] < 20 else ""))
        P("")
        P("| # | Action | Item | Pay | Total | ~Time | Lv | Slots | Why |")
        P("|---|--------|------|-----|-------|-------|----|-------|-----|")
        pending = [s for s in spikes if s["major"] and s["souls"] is not None]
        prev, n = 0, 0
        for st in b["steps"]:
            it = tree["by_id"][st["id"]]
            for sp in list(pending):
                if prev < sp["souls"] <= st["cum"]:
                    P(f"| | >> SPIKE ~{sp['souls']} souls | | | | ~{mmss(clock(sp['souls']))} | L{sp['level']} | | {sp['ability']} {sp['kind']}: {sp['text'][:90]} |")
                    pending.remove(sp)
            prev = st["cum"]
            m = metrics.get(st["id"])
            slots = f"{st['slots']}/{BASE_SLOTS + FLEX_SLOTS if st['slots'] > BASE_SLOTS else BASE_SLOTS}"
            if st["kind"] == "sell":
                rep_ = f"; makes room for {tree['by_id'][st['replace']]['name']}" if st.get("replace") else ""
                P(f"| | SELL | {it['name']} | | {st['cum']} | ~{mmss(st['eta'])} | {level_at(hero, st['cum'])} | {slots} | {st['note']}{rep_} |")
                continue
            n += 1
            used = ", ".join(tree["by_id"][c]["name"] for c in st["consumes"])
            action = {"buy": "Buy", "upgrade": "Upgrade", "stone": "Buy (stepping stone)", "temp": "Buy (lane only)"}[st["kind"]]
            extra = ""
            if st["consumes"]:
                extra += f" (consumes {used}, credit {sum(tree['by_id'][c]['cost'] for c in st['consumes'])})"
            if st.get("finish"):
                extra += " (finishes the upgrade chain)"
            if st["id"] in b["imbue"]:
                tgt = next(a["name"] for a in kit if a["id"] == b["imbue"][st["id"]])
                extra += f" **IMBUE on {tgt}**"
            if st["kind"] == "temp":
                extra += " (temporary - sold later)"
            if st["cum"] > budget:
                extra += " (luxury - only if the game goes long)"
            if st["kind"] == "stone":
                extra += f" (upgrades into {tree['by_id'][st['into']]['name']})" if st.get("into") else ""
            if st.get("st"):
                extra += (" **SAVE UP**" if st["st"]["saveup"] else "") + (" (amps next spike)" if st["st"]["amp"] else "")
            if m:
                extra += " (niche - unproven)" if m["pick"] < NICHE_PICK_PCT else ""
                why_txt = why(m, extra)
            else:
                prof = item_profile(it)
                why_txt = ("stepping stone" + (" | " + "; ".join(prof["stats"][:2]) if prof["stats"] else "")) + extra
            P(f"| {n} | {action} | {it['name']} | {st['pay']} | {st['cum']} | ~{mmss(st['eta'])} | {level_at(hero, st['cum'])} | {slots} | {why_txt} |")
        P("")
        P(f"**Final inventory ({len(b['final'])} slots)**")
        P("")
        P("| Slot | Item | Cost |")
        P("|------|------|------|")
        for k, iid in enumerate(b["final"], 1):
            P(f"| {k}{' (flex)' if k > BASE_SLOTS else ''} | {tree['by_id'][iid]['name']} | {tree['by_id'][iid]['cost']} |")
        sells = [st for st in b["steps"] if st["kind"] == "sell"]
        P("")
        P("**Sell order and replacements**")
        P("")
        if sells:
            P("| Sell | After spending | Replace with | Why |")
            P("|------|----------------|--------------|-----|")
            for st in sells:
                r = tree["by_id"][st["replace"]]["name"] if st.get("replace") else "-"
                P(f"| {tree['by_id'][st['id']]['name']} | {st['cum']} (~{mmss(st['eta'])}) | {r} | {st['note']} |")
        else:
            P("Nothing is sold early - every lane item is consumed by an upgrade, so no dead 800-soul items stay in your slots.")
        if b["swaps"]:
            P("")
            P(f"**Late-game swaps** - once you have spent about {int(budget)} souls with all slots full, trade the weakest item for a stronger one:")
            P("")
            P("| Sell | Replace with | Cost | Why |")
            P("|------|--------------|------|-----|")
            for sw in b["swaps"]:
                ms, mb = metrics[sw["sell"]], metrics[sw["buy"]]
                P(f"| {ms['item']['name']} (T{ms['item']['item_tier']}) | {mb['item']['name']} | {mb['item']['cost']} | {mb['lift']:+.1f}pp vs T{mb['item']['item_tier']} peers, {mb['pick']:.0f}% pick; sold item is only {ms['lift']:+.1f}pp |")
    P("")

    # situational
    P("## 5. Situational / counter items")
    enemies = []
    if a.vs.strip():
        names = [x.strip() for x in a.vs.split(",") if x.strip()]
        enemies = [find_hero(n, heroes) for n in names]
    traits = enemy_traits(enemies) if enemies else {}
    all_chosen = {i for b in builds.values() for i in b["chosen"]}
    if enemies:
        P(f"Enemy team: {', '.join(h['name'] for h in enemies)} -> traits {', '.join(f'{k} x{v}' for k, v in traits.items() if v) or 'none detected'}")
        est = fetch_item_stats(hero["id"], flt, R, enemy=[h["id"] for h in enemies])
        rows = []
        for iid, r in est.items():
            m = metrics.get(iid)
            if not m or r["matches"] < 150:
                continue
            base = (m["wr"] / 100)
            vs = (r["wins"] + base * SHRINK_K) / (r["matches"] + SHRINK_K)
            rows.append(((vs - base) * 100, m, r["matches"]))
        rows.sort(key=lambda x: -x[0])
        P("")
        up = [r for r in rows if r[0] >= 1.0][:5]
        down = [r for r in rows[::-1] if r[0] <= -1.0][:3]
        P("Data-backed swings into this lineup (item WR vs its normal WR; |swing| >= 1pp, 150+ games):")
        P("- Better: " + (", ".join(f"{m['item']['name']} ({m['item']['cost']}, {d:+.1f}pp, {n}g)" for d, m, n in up) or "nothing clears the bar"))
        P("- Worse: " + (", ".join(f"{m['item']['name']} ({d:+.1f}pp, {n}g)" for d, m, n in down) or "nothing clears the bar"))
        P("")
    P("| Need | Trigger | Best options (by win-rate lift) |")
    P("|------|---------|---------------------------------|")
    for name, pat, trait, why_ in COUNTERS:
        opts = [m for m in metrics.values() if any(re.match(pat, p) for p in m["prof"]["props"]) and not any(re.search(r"Immunity|LifeThreshold|Revive", p) for p in m["prof"]["props"])]
        opts.sort(key=lambda m: -m["lift"])
        top = ", ".join(f"{m['item']['name']} ({m['item']['cost']}, {m['lift']:+.1f}pp)" for m in opts[:3]) or "-"
        hit = " **<- ACTIVE**" if traits.get(trait, 0) >= 2 else ""
        P(f"| {name} | {why_}{hit} | {top} |")
    P("")
    flex = sorted([m for i, m in metrics.items() if i not in all_chosen and m["lift"] > 0.3 and m["pick"] >= 3], key=lambda m: -m["lift"])[:5]
    if flex:
        P("Strong flex picks not in any build: " + ", ".join(f"{m['item']['name']} ({m['item']['cost']}, {m['lift']:+.1f}pp)" for m in flex))
    text = "\n".join(L)
    print(text)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    imgs = hero.get("images") or {}
    hero_img = fetch_image(imgs.get("icon_hero_card_webp") or imgs.get("icon_hero_card")) if (a.html or a.out) else None
    html_path = a.html or (os.path.splitext(a.out)[0] + ".html" if a.out else None)
    if html_path:
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(to_html(text, f"{hero['name']} build", exports, hero_img))
        base = os.path.splitext(html_path)[0]
        if hero_img:
            with open(f"{base}_hero.{hero_img[1].split('/')[1]}", "wb") as fh:
                fh.write(hero_img[0])
        for arch, e in exports.items():
            with open(f"{base}_{arch.lower()}.json", "w", encoding="utf-8") as fh:
                fh.write(e["json"])
            with open(f"{base}_{arch.lower()}_import.txt", "w", encoding="utf-8") as fh:
                fh.write(e["import"] + "\n")
        print(f"\nHTML written to {html_path}", file=sys.stderr)


# ---------------------------------------------------------------- in-game import string
# The game's build "copy/paste" string is base64( zstd( protobuf CMsgHeroBuild ) ). Field numbers were
# reverse-engineered from a build copied out of the game (see README).

def _vi(n):
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _f_vi(f, v):
    return _vi(f << 3) + _vi(v)


def _f_len(f, b):
    b = b.encode("utf-8") if isinstance(b, str) else b
    return _vi(f << 3 | 2) + _vi(len(b)) + b


def _f_f32(f, v):
    return _vi(f << 3 | 5) + struct.pack("<f", v)


def _mod_msg(item_id, annotation="", sell=0, imbue=None):
    m = _f_vi(1, item_id)
    if annotation:
        m += _f_len(2, annotation)
    if sell:
        m += _f_vi(4, sell)
    if imbue:
        m += _f_vi(5, imbue)
    return m


def _category_msg(name, item_ids, width=600.0, height=160.0, optional=False, extra=None):
    extra = extra or {}
    m = b"".join(_f_len(1, _mod_msg(i, **extra.get(i, {}))) for i in item_ids)
    m += _f_len(2, name) + _f_len(3, "") + _f_f32(4, width) + _f_f32(5, height)
    if optional:
        m += _f_vi(6, 1)
    return m


def _ability_order_msg(changes):
    return b"".join(_f_len(1, _f_vi(1, c["ability_id"]) + _f_vi(2, c["currency_type"]) + _f_vi(3, c["delta"])) for c in changes)


def zstd_raw_frame(data):
    """Valid zstd frame made of raw (uncompressed) blocks - no zstd library needed."""
    n = len(data)
    if n < 256:                                    # frame header: single-segment, content size in 1/2/4 bytes
        hdr = bytes([0x20, n])
    elif n < 65792:
        hdr = bytes([0x60]) + struct.pack("<H", n - 256)
    else:
        hdr = bytes([0xA0]) + struct.pack("<I", n)
    out, pos = bytearray(bytes([0x28, 0xB5, 0x2F, 0xFD]) + hdr), 0
    while True:
        chunk = data[pos:pos + 131071]
        pos += len(chunk)
        last = pos >= n
        out += struct.pack("<I", (1 if last else 0) | (len(chunk) << 3))[:3] + chunk
        if last:
            return bytes(out)


def zstd_frame(data):
    """Compress like the game does (zstd level 19) when the optional `zstandard` package is installed
    (pip install zstandard); otherwise fall back to an uncompressed frame, which the game may reject."""
    try:
        import zstandard
    except ImportError:
        print("note: `pip install zstandard` for game-compatible import strings", file=sys.stderr)
        return zstd_raw_frame(data)
    return zstandard.ZstdCompressor(level=19, write_checksum=False, write_content_size=True).compress(data)


def encode_build(hero_id, name, description, categories, ability_changes=None, ids=None, extra=None):
    """categories: [(name, [item_ids], optional)] -> importable string."""
    details = b"".join(_f_len(1, _category_msg(n, ids, optional=opt, extra=extra)) for n, ids, opt in categories)
    if ability_changes:
        details += _f_len(2, _ability_order_msg(ability_changes))
    # field 1 = build id, field 2 = HERO id (confirmed from builds copied out of the game), 3 = account, 4 = timestamp
    head = (_f_vi(1, ids[0]) if ids else b"") + _f_vi(2, hero_id) + (_f_vi(3, ids[1]) + _f_vi(4, ids[2]) if ids else b"")
    msg = (head + _f_len(5, name) + _f_len(6, description) + _f_vi(7, 0) + _f_vi(8, 0) + _f_vi(9, 0)
           + _f_len(10, details) + _f_vi(11, 0))
    return base64.b64encode(zstd_frame(msg)).decode()


def imbue_target(it, kit):
    """Items with an `imbue` field are applied to ONE ability. Pick the ability that gets the most from this
    item's effect (range/radius, duration, cooldown, or damage) - the choice is made from live numbers."""
    kind = it.get("imbue")
    if not kind:
        return None
    names = set()
    for u in it.get("upgrades") or []:
        for pu in u.get("property_upgrades", []):
            names.add(pu["name"])
    names |= {k for k, v in it["properties"].items() if k.startswith("Imbued") and isinstance(v, dict) and num(v.get("value"))}
    cd_item = any("Cooldown" in n for n in names)
    best = None
    for ab in kit:
        ult = ab.get("ability_type") == "ultimate"
        if ult and kind == "imbue_active_non_ult":
            continue
        pr = ab["properties"]
        val = lambda k: num(pr.get(k, {}).get("value", 0)) if isinstance(pr.get(k), dict) else 0.0
        cd = val("AbilityCooldown")
        dmg = max([num(v.get("value")) for k, v in pr.items() if isinstance(v, dict) and re.search(r"damage|dps|heal", k, re.I) and "Percent" not in k] or [0])
        rad = max([num(v.get("value")) for k, v in pr.items() if isinstance(v, dict) and re.search(r"radius", k, re.I)] or [0])
        dur = val("AbilityDuration") + val("BuffDuration")
        upm = 60 / cd if cd > 0 else 1.0
        if names & {"TechRangeMultiplier", "TechRadiusMultiplier"}:
            sc = val("AbilityCastRange") + 2 * rad
        elif names & {"BuffDuration", "AbilityDuration", "BonusAbilityDurationPercent", "ImbuedBonusDuration"}:
            sc = dur
        elif cd_item:
            sc = cd
        else:
            sc = dmg * upm
        if ult and not cd_item:
            continue                                    # an ultimate is a poor home for range/duration/damage tweaks
        if best is None or sc > best[0]:
            best = (sc, ab)
    return best[1] if best and best[0] > 0 else None


# ---------------------------------------------------------------- build export

AP_COST = {1: 1, 2: 2, 3: 5}          # ability-point cost of upgrade tiers 1..3 (unlock costs 1 unlock)


IMPORT_IDS = None      # (build id, account id, timestamp) - the game refuses import strings without them


def resolve_account_id(arg):
    """--account-id, else $DEADLOCK_ACCOUNT_ID, else the id saved in the cache folder."""
    v = arg or os.environ.get("DEADLOCK_ACCOUNT_ID")
    f = os.path.join(CACHE, "account_id.txt")
    if not v and os.path.exists(f):
        v = open(f).read().strip()
    if v:
        n = int(str(v).strip())
        if n >= 76561197960265728:                  # a SteamID64 (from a profile URL): convert to the 32-bit account id
            n -= 76561197960265728
        os.makedirs(CACHE, exist_ok=True)
        with open(f, "w") as fh:
            fh.write(str(n))
        return n
    return None


def make_export(hero, arch, build, metrics, order_row, pc, pcts):
    """Build in the same JSON shape deadlock-api serves for in-game builds (/v1/builds -> hero_build)."""
    buys = [st for st in build["steps"] if st["kind"] != "sell"]
    ids = [st["id"] for st in buys]
    mod = lambda i: {"ability_id": i, "annotation": None, "required_flex_slots": None, "sell_priority": None, "imbue_target_ability_id": build["imbue"].get(i)}
    uniq = list(dict.fromkeys(ids))                           # each item once, in buy order
    cats = [(n, uniq[k:k + 5], None) for n, k in (("Lane", 0), ("Core", 5), ("Mid game", 10), ("Late game", 15))]
    ids = uniq
    flex = sorted((i for i, m in metrics.items() if i not in ids and m["lift"] > 0.3 and m["pick"] >= MIN_PICK_PCT),
                  key=lambda i: -metrics[i]["lift"])[:5]
    if flex:
        cats.append(("Situational / flex", flex, True))
    seen, changes = {}, []
    for aid in order_row["abilities"]:
        seen[aid] = seen.get(aid, 0) + 1
        n = seen[aid]
        if n == 1:
            changes.append({"ability_id": aid, "currency_type": 2, "delta": -1, "annotation": None})
        elif n - 1 in AP_COST:
            changes.append({"ability_id": aid, "currency_type": 1, "delta": -AP_COST[n - 1], "annotation": None})
    data = {"hero_build": {
        "hero_id": hero["id"], "hero_build_id": 0, "author_account_id": 0, "name": f"{hero['name']} - {arch} (auto, build {pc['build']})",
        "description": f"Generated from live match data. {arch}: " + arch_desc(arch), "language": 0, "version": pc["build"], "origin_build_id": 0,
        "tags": [], "development_build": None,
        "details": {"mod_categories": [{"name": n, "width": 400.0, "height": 160.0, "description": "", "optional": opt,
                                         "mods": [mod(i) for i in ii]} for n, ii, opt in cats if ii],
                    "ability_order": {"currency_changes": changes}}}}
    lines = [f"{hero['name']} - {arch} build"]
    byid = build["tree"]["by_id"]
    for k, st in enumerate(build["steps"], 1):
        it = byid[st["id"]]
        if st["kind"] == "sell":
            lines.append(f"  {k}. SELL {it['name']}" + (f" (makes room for {byid[st['replace']]['name']})" if st.get("replace") else ""))
        else:
            used = ", ".join(byid[c]["name"] for c in st["consumes"])
            lines.append(f"  {k}. {'Upgrade' if st['consumes'] else 'Buy'} {it['name']} ({st['pay']})" + (f" - consumes {used}" if used else ""))
    lines.append("Situational / flex: " + ", ".join(metrics[i]["item"]["name"] for i in flex))
    cat_args = [(n, ii, bool(opt)) for n, ii, opt in cats if ii]
    byid = build["tree"]["by_id"]
    extra, prio = {}, 0
    for iid, abid in build["imbue"].items():
        extra.setdefault(iid, {})["imbue"] = abid
    sells = [(st["id"], st.get("replace")) for st in build["steps"] if st["kind"] == "sell"] + [(sw["sell"], sw["buy"]) for sw in build["swaps"]]
    for sid, rid in sells:                                   # sell order: lower number = sell first
        prio += 1
        e = extra.setdefault(sid, {})
        e["sell"] = prio
        e["annotation"] = f"Sell for {byid[rid]['name']}" if rid else "Sell when slots are needed"
    # every build needs its own id: a reused id collides with a build already in your list and the paste fails
    ts = IMPORT_IDS[2]
    ids = ((ts - 1_700_000_000) * 8 + list(ARCH).index(arch) + 1, IMPORT_IDS[1], ts)
    name, desc = data["hero_build"]["name"], data["hero_build"]["description"]
    return {"json": json.dumps(data, indent=2), "list": "\n".join(lines),
            "import": encode_build(hero["id"], name, desc, cat_args, ids=ids, extra=extra),
            "import_ao": encode_build(hero["id"], name, desc, cat_args, changes, ids=ids, extra=extra)}


# ---------------------------------------------------------------- HTML rendering

HTML_CSS = """
:root{--bg:#f6f4f1;--card:#fff;--ink:#1d1b22;--mute:#6b6676;--line:#e4e0e9;--acc:#7c3aed;--acc2:#be185d;
--t1:#64748b;--t2:#0d9488;--t3:#7c3aed;--t4:#d97706;--spike:#fdf2f8;--good:#15803d;--bad:#b91c1c}
@media(prefers-color-scheme:dark){:root{--bg:#14121a;--card:#1d1a26;--ink:#ece9f3;--mute:#9a94a8;--line:#322d40;
--acc:#a78bfa;--acc2:#f472b6;--spike:#2a1830;--good:#4ade80;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
header{display:flex;align-items:center;gap:18px;padding:28px 0 8px}img.hero{height:150px;width:auto;border-radius:12px;border:1px solid var(--line);box-shadow:0 4px 18px rgba(0,0,0,.25)}header{padding:28px 0 8px}h1{margin:0;font-size:2rem;letter-spacing:-.02em}
h1 small{display:block;font-size:.9rem;font-weight:400;color:var(--mute);margin-top:4px}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin:18px 0}
h2{margin:0 0 10px;font-size:1.25rem;color:var(--acc)}h3{margin:22px 0 6px;font-size:1.1rem}
h3 .rec{font-size:.72rem;background:var(--acc);color:#fff;border-radius:99px;padding:2px 9px;margin-left:8px;vertical-align:middle}
p{margin:8px 0}ul{padding-left:20px;margin:8px 0}li{margin:3px 0}em{color:var(--mute)}
.tablewrap{overflow-x:auto;margin:10px 0}table{border-collapse:collapse;width:100%;font-size:.88rem}
th{text-align:left;color:var(--mute);font-weight:600;border-bottom:2px solid var(--line);padding:6px 8px;white-space:nowrap}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr.spike td{background:var(--spike);font-size:.82rem;color:var(--acc2)}
.pill{display:inline-block;border-radius:99px;padding:1px 9px;font-size:.78rem;font-weight:600;color:#fff}
.t1{background:var(--t1)}.t2{background:var(--t2)}.t3{background:var(--t3)}.t4{background:var(--t4)}
.pos{color:var(--good);font-weight:600}.neg{color:var(--bad);font-weight:600}
.tag{font-size:.72rem;border-radius:4px;padding:1px 6px;margin-left:4px;font-weight:700}
.save{background:var(--acc);color:#fff}.niche{background:var(--line);color:var(--mute)}.item{display:inline-flex;align-items:center;gap:8px;font-weight:600;white-space:nowrap}.item img{border-radius:6px;background:var(--line);object-fit:contain}textarea.manual{width:100%;font:12px/1.4 ui-monospace,Consolas,monospace;margin:6px 0;padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}.actions{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 4px}.actions button{font:inherit;font-size:.82rem;font-weight:600;cursor:pointer;border:1px solid var(--acc);background:var(--acc);color:#fff;border-radius:8px;padding:5px 12px}
.actions button.alt{background:transparent;color:var(--acc)}.actions button.done{background:var(--good);border-color:var(--good);color:#fff}.big{color:var(--acc2);font-weight:700}
.bar{display:flex;height:22px;border-radius:8px;overflow:hidden;margin:10px 0}
.bar div{color:#fff;font-size:.78rem;font-weight:600;padding:2px 8px;white-space:nowrap}
"""


HTML_JS = """
async function copyText(text){
  try{ if(navigator.clipboard&&window.isSecureContext){ await navigator.clipboard.writeText(text); return true; } }catch(e){}
  try{ const t=document.createElement('textarea'); t.value=text; t.style.cssText='position:fixed;top:0;left:0;opacity:0';
       document.body.appendChild(t); t.focus(); t.select(); const ok=document.execCommand('copy'); t.remove(); return ok; }catch(e){ return false; }
}
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{
  const text=EXPORTS[b.dataset.copy][b.dataset.kind], old=b.dataset.label||(b.dataset.label=b.textContent);
  const ok=await copyText(text);
  const box=document.getElementById('manual-'+b.dataset.copy);
  if(ok){ b.textContent='Copied!'; b.classList.add('done'); if(box) box.hidden=true; }
  else{ b.textContent='Copy blocked - select the text below'; if(box){ box.value=text; box.hidden=false; box.focus(); box.select(); } }
  setTimeout(()=>{b.textContent=old;b.classList.remove('done')},2500)}));
document.querySelectorAll('textarea.manual').forEach(t=>t.addEventListener('focus',()=>t.select()));
"""


def _inline(t):
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)\*(?![\w*])", r"<em>\1</em>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    t = t.replace("(niche - unproven)", '<span class="tag niche">niche</span>')
    t = t.replace("<strong>SAVE UP</strong>", '<span class="tag save">SAVE UP</span>')
    t = re.sub(r"([+-]\d+\.\d)pp", lambda m: f'<span class="{"pos" if m.group(1)[0] == "+" else "neg"}">{m.group(1)}pp</span>', t)
    return t


ICONS = {}   # item name -> icon URL, filled in main()


def _cell(c, col):
    if col == "Item" and c.strip() in ICONS:
        return f'<span class="item"><img src="{ICONS[c.strip()]}" alt="" loading="lazy" width="32" height="32">{_inline(c.strip())}</span>'
    
    if col == "Cost" and c.strip().isdigit():
        v = int(c)
        tier = 1 if v <= 800 else 2 if v <= 1600 else 3 if v <= 3200 else 4
        return f'<span class="pill t{tier}">{v}</span>'
    return _inline(c.strip())


def to_html(md, title, exports, hero_img=None):
    out, lines, i = [], md.split("\n"), 0
    sec = False
    bars = ""
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("# "):
            img = ""
            if hero_img:
                img = f'<img class="hero" alt="" src="data:{hero_img[1]};base64,{base64.b64encode(hero_img[0]).decode()}">'
            out.append(f"<header>{img}<div><h1>{_inline(ln[2:])}</h1></div></header>")
        elif ln.startswith("## "):
            if sec:
                out.append("</section>")
            out.append(f"<section><h2>{_inline(ln[3:])}</h2>")
            sec = True
        elif ln.startswith("### "):
            h = ln[4:]
            rec = " (recommended)" in h
            name = h.replace(" (recommended)", "")
            out.append(f"<h3>{_inline(name)}" + ('<span class="rec">recommended</span>' if rec else "") + "</h3>")
            if name in exports:
                out.append(f'<div class="actions"><button data-copy="{name}" data-kind="import" title="Paste into the in-game builds menu">Copy import string</button>'
                           f'<button data-copy="{name}" data-kind="import_ao" class="alt" title="Experimental: also encodes ability order">Import string + ability order (experimental)</button>'
                           f'<button data-copy="{name}" data-kind="list" class="alt">Copy item list</button>'
                           f'<button data-copy="{name}" data-kind="json" class="alt">Copy JSON</button></div>'
                           f'<textarea class="manual" id="manual-{name}" readonly hidden rows="4"></textarea>')
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[2:]]
            t = "<div class='tablewrap'><table><thead><tr>" + "".join(f"<th>{_inline(h.strip())}</th>" for h in head) + "</tr></thead><tbody>"
            for r in body:
                joined = " ".join(r)
                if ">> SPIKE" in joined:
                    t += f"<tr class='spike'><td colspan='{len(head)}'>&#9889; {_inline(' '.join(c.strip() for c in r if c.strip()).replace('>> SPIKE', 'Spike'))}</td></tr>"
                    continue
                t += "<tr>" + "".join(f"<td>{_cell(c, head[k].strip() if k < len(head) else '')}</td>" for k, c in enumerate(r)) + "</tr>"
            t += "</tbody></table></div>"
            out.append(t.replace("**SPIKE**", "").replace("<strong>SPIKE</strong>", '<span class="big">SPIKE</span>'))
            continue
        elif ln.startswith("- **") and re.match(r"- \*\*(Aggressive|Farming|Support)\*\* \d+%", ln):
            grp = []
            while i < len(lines) and re.match(r"- \*\*(Aggressive|Farming|Support)\*\* \d+%", lines[i]):
                m = re.match(r"- \*\*(\w+)\*\* (\d+)%", lines[i])
                grp.append((m.group(1), int(m.group(2))))
                i += 1
            col = {"Aggressive": "#be185d", "Farming": "#0d9488", "Support": "#7c3aed"}
            out.append("<div class='bar'>" + "".join(f"<div style='width:{max(v, 8)}%;background:{col[k]}'>{k} {v}%</div>" for k, v in grp) + "</div>")
            continue
        elif ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(f"<li>{_inline(lines[i][2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        elif ln.strip():
            out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    if sec:
        out.append("</section>")
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{HTML_CSS}</style></head><body><main>{''.join(out)}</main>"
            f"<script>const EXPORTS={json.dumps(exports).replace('</', '<\/')};{HTML_JS}</script></body></html>")


def arch_desc(arch):
    return {"Aggressive": "win fights - damage, ability uptime and burst; spend on fight power first.",
            "Farming": "snowball the economy - sustain and clear speed early, fewer luxury items, fight later.",
            "Support": "buff the team and hit your spikes - utility, cooldowns, survivability, auras."}[arch]


if __name__ == "__main__":
    main()
