"""Inline-translation layer for log content.

WS.log mixes:
  1. English (mostly UE engine logs).
  2. Chinese characters (e.g. 死亡日志 = "Death log", 濒死 = "Knockdown").
  3. Pinyin transliterations of asset blueprint names
     (e.g. BP_WuQi_DaJian_4 = "Tier 4 Great Sword",
     ZhaoMuChanged = "Recruit changed", RuQin = "Invasion").

This module annotates raw log lines with English translations as
inline HTML <span class="tr">[...]</span> tags, leaving the
original tokens visible alongside. The CSS class .tr renders in
a distinct colour so the operator can tell translations from raw
log content at a glance.

Two design notes:

- Operators whose terminal lacks Chinese fonts see boxes/squares
  in place of the Chinese, but the [English] annotation right
  beside it is still readable. So the page stays useful even
  with broken font support.

- The dictionary is intentionally compact (~150 entries) to start.
  Coverage is the highest-frequency log content per
  docs/LOG_PATTERNS.md and docs/INVASIONS.md. Adding more is just
  a matter of editing the maps below; no code change needed.

Cross-reference: docs/LOG_PATTERNS.md (log content this targets),
                 GameXishu_Translation.md (gameplay-settings
                 vocabulary, separate from log content but shares
                 some pinyin terms).
"""

import html
import re
from typing import Optional


# ── Chinese-character phrase translations ──────────────────────────────────
# Long phrases first so substring matches don't get clobbered by short
# ones. The annotate_line() function sorts by length descending before
# applying.

CHINESE_PHRASE_MAP: dict[str, str] = {
    # Log-prefix phrases (the LogWS Warning header strings)
    "死亡日志": "Death Log",
    "濒死对象": "Dying Subject",
    "来源对象": "Source Object",
    "传送日志": "Teleport Log",
    "掉落日志": "Drop Log",
    "异常的辐射区体积残留中心坐标": "Anomalous Radiation Residue Centre",
    "强制传回出生点瞬移到": "Forced Respawn Teleport To",

    # Invasion phase log lines (see docs/INVASIONS.md)
    "入侵进入准备阶段": "Invasion: Preparation phase begins",
    "入侵进入探查阶段": "Invasion: Scouting phase begins",
    "入侵进入进攻阶段": "Invasion: Attack phase begins",
    "在线玩家至少有": "Online players (at least)",
    "建筑数": "Buildings",
    "生物数": "Creatures",
    "起始点原因": "Source-point reason",
    "起始点": "Source",
    "目标点": "Target",

    # Death / knockdown sub-fields
    "公会": "Guild/Tribe",
    "主人": "Owner",
    "面具": "Mask",
    "濒死": "Knockdown",

    # Common short markers
    "玩家": "Player",
    "敌人": "Enemy",
    "动物": "Animal",
    "物品": "Item",
    "建筑": "Building",
    "工会": "Tribe",      # alt spelling sometimes seen
    "工会名称": "Tribe Name",
}


# ── Pinyin-word translations (standalone tokens in the log) ────────────────
# These appear as bare words in log lines (RuQin, ZhaoMuChanged, etc.).
# Match is case-sensitive and word-boundary anchored to avoid catching
# substrings inside larger names.

PINYIN_TOKEN_MAP: dict[str, str] = {
    # Invasion lifecycle
    "RuQin": "Invasion",
    "JieSuan": "Settlement",
    "JinGong": "Attack",
    "TanCha": "Scouting",
    "ZhunBei": "Preparation",
    "GuaiWu": "Mobs/Monsters",
    "EnterJieSuanStage": "Enter Settlement Stage",

    # Recruit / thrall lifecycle
    "ZhaoMuChanged": "Recruit Changed",
    "ZiDongJinShi": "Auto-eat",
    "OnSiWangChanged": "On Death-state Changed",
    "AHCharacterRen": "Character Class",
    "ChongSheng": "Respawn",
    "FuHuoDian": "Respawn Point",

    # Generic log structure terms
    "RiZhiType": "Log Type",
    "GongHuiName": "Tribe Name",
    "GongHui": "Tribe",
    "PlayerName": "Player",
    "OwnerPlayerName": "Owner",
    "CharacterName": "Character",
    "SourceGuid": "Source GUID",
    "Reason": "Reason",
    "DaoJu": "Item",

    # ZiDongJinShi UseTo categories (the food/drug type the thrall ate)
    "ZhuShi": "Staple Food",
    "ShiWu": "Food",
    "GuoShu": "Fruits & Veg",
    "LingShi": "Snack",
    "Shui": "Water",
    "YaoWu": "Medicine",
    "Du": "Poison",
    "FuShe": "Radiation",
    "ZhenShe": "Stun",
    "ZhenSheZhiDing": "Designated Stun",

    # Settlement outcomes
    "Win Clear GuaiWu": "Won — cleared all enemies",
    "Failed Timeout": "Failed — invaders ran out of time",
}


# ── BP_<category>_<class>_<suffix...> asset-name parser ────────────────────
#
# Patterns of interest:
#   BP_WuQi_DaJian_4               -> "Weapon: Great Sword T4"
#   BP_WuQi_QuanTao_YiJi_2         -> "Weapon: Knuckles Variant 2"
#   BP_DaoJu_JianTou_4             -> "Item: Arrow T4"
#   BP_DaoJu_Key_Sobeck            -> "Item: Sobek Key"
#   BP_An_Camel                    -> "Animal: Camel"
#   BP_EgyptDLC_TribeF_SavageHorn_Elite_C  -> "Egypt-DLC Tribe Savagehorn Elite"
#   DaoJu_Item_IronOre             -> "Item: Iron Ore"

BP_CATEGORY_MAP: dict[str, str] = {
    "WuQi": "Weapon",       # 武器
    "DaoJu": "Item",        # 道具
    "ZB": "Equipment",      # 装备 (ZhuangBei)
    "An": "Animal",         # 动物 (DongWu) -- An is the BP shorthand
    "GZ": "Workstation",    # 工作台 (GongZuoTai)
    "EG": "Egypt-DLC",
    "EgyptDLC": "Egypt-DLC",
    "GongJu": "Tool",       # 工具
    "JianZhu": "Building",  # 建筑
    "Weapon": "Weapon",     # already English in some BPs
    "Robber": "Plunderer",  # invaders (BP_Robber_Intrusion_C)
    "TribeF": "Tribe",      # 部落 (BuLuo) -- F suffix unclear
    "ChiHou": "Scout",      # 斥候 (world-map scout, distinct from invasion spy)
    "PlayerBase": "Player Character",
    "DLC": "DLC",
}

# Weapon classes (BP_WuQi_<class>_<tier>)
WEAPON_FAMILY_MAP: dict[str, str] = {
    "Chui": "Hammer",        # 锤
    "DaJian": "Great Sword", # 大剑
    "Dao": "Saber",          # 刀
    "Dun": "Shield",         # 盾
    "Gong": "Bow",           # 弓
    "Mao": "Spear",          # 矛
    "QuanTao": "Knuckles",   # 拳套
    "ShuangDao": "Dual Blades",  # 双刀
    "Glider": "Glider",
    "WeistLight": "Lantern", # likely typo of WaistLight (腰灯)
    "BiShou": "Dagger",      # 匕首
    "Nu": "Crossbow",        # 弩
    "Fu": "Axe",             # 斧
}

# Animal classes (BP_An_<species>)
ANIMAL_FAMILY_MAP: dict[str, str] = {
    "Ass": "Donkey",         # 驴
    "Camel": "Camel",        # 骆驼
    "DaXiang": "Elephant",   # 大象
    "Rhinoceros": "Rhino",   # 犀牛
    "TuoNiao": "Ostrich",    # 鸵鸟
    "YeZhu": "Boar",         # 野猪
    "Hema": "Hippo",         # 河马 (typo: actual = HeMa, sometimes Hema)
    "HeMa": "Hippo",
    "ChangJiaoNiu": "Longhorn",      # 长角牛
    "Lv": "Donkey",                  # 驴 (alt)
    "Luotuo": "Camel",               # 骆驼 (alt)
    "QiuYuXi": "Armadillo Lizard",   # 球鱼蜥
    "Chicken": "Chicken",
    "ChangJingLu": "Giraffe",        # 长颈鹿
    "Dan": "Egg",                    # 蛋
    "XiNiu": "Rhino",                # 犀牛 (alt)
    "XianGui": "Tortoise",           # 仙龟
}

# Item classes (BP_DaoJu_<class>_<...> AND DaoJu_Item_<class>)
ITEM_FAMILY_MAP: dict[str, str] = {
    "JianTou": "Arrow",          # 箭头
    "DuanGang": "Steel",         # 锻钢 (used in higher-tier arrows)
    "GuZhi": "Bone",             # 骨制
    "HeiTie": "Iron",            # 黑铁
    "QingTong": "Bronze",        # 青铜
    "Key": "Key",
    "Ship": "Ship",
    "Item": "Item",
    "Festival": "Festival",
    "QiMin": "Civilian",         # 启民 / civilian-tier
    "JianZhu": "Building",       # 建筑

    # DaoJu_Item_* -- common resource items
    "Ice": "Ice",
    "IronOre": "Iron Ore",
    "Meteorites": "Meteorite",
    "Nitre": "Nitre/Saltpetre",
    "PhosphateOre": "Phosphate Ore",
    "SaltOre": "Salt Ore",
    "SulfurOre": "Sulfur Ore",
    "TinOre": "Tin Ore",
    "HugeStone": "Huge Stone",
    "XiuMianCang": "Sleep Cabin",  # 休眠仓
    "Broken": "(broken)",
    "Sobeck": "Sobek",             # boss key
    "Scarab": "Scarab",
    "DiXiaCheng": "Dungeon",       # 地下城
    "JK": "JK (unidentified)",     # placeholder
    "SYJ_30SS": "30SS Key",        # placeholder, exact meaning unknown
}

# Suffix interpretations (tier, variant, special)
TIER_SUFFIX_MAP: dict[str, str] = {
    "1": "T1",
    "2": "T2",
    "3": "T3",
    "4": "T4",
    "5": "T5",
    "6": "T6",
    "sp": "Special",
    "YiJi": "Variant",       # 异级 (different tier)
    "GongCheng": "Siege",    # 工程 (engineering / siege variant)
    "Big": "Large",
    "Small": "Small",
    "Broken": "Broken",
    "Rot": "Rotten",
    "Elite": "Elite",
    "C": "",     # the trailing _C is UE class marker; drop it
    "M": "(M)",  # gender / male marker on some character BPs
    "F": "(F)",  # gender / female
    "Nv": "(F)",
    "Boss": "Boss",
    "DesertWolf": "Desert Wolf",
    "SavageHorn": "Savagehorn",
    "Exiles": "Exiles",
}


# Composite parser: handles BP_ / DaoJu_ asset names by walking parts.

def parse_bp_name(name: str) -> Optional[str]:
    """Translate an asset-name token like BP_WuQi_DaJian_4 into a
    readable English string. Returns None if the name doesn't look
    like a parseable asset name."""
    if not name or "_" not in name:
        return None
    parts = name.split("_")

    # Strip the leading 'BP' marker if present (UE blueprint prefix).
    if parts[0] == "BP":
        parts = parts[1:]
    if not parts:
        return None

    # First part is the category. If unknown, bail -- not our format.
    cat = parts[0]
    cat_en = BP_CATEGORY_MAP.get(cat)
    rest = parts[1:]
    if cat_en is None:
        return None

    # DaoJu_Item_<X> -- "Item" appears twice (once as the category,
    # once as the BP family name). Drop the redundant body 'Item' so
    # we get "Item: Iron Ore" not "Item: Item Iron Ore".
    if cat == "DaoJu" and rest and rest[0] == "Item":
        rest = rest[1:]

    pieces: list[str] = [cat_en + ":"]
    for p in rest:
        if not p:
            continue
        # Try class lookup based on category context first, then
        # generic lookups, then fall through to TIER_SUFFIX, then
        # leave the raw token if nothing matches.
        if cat == "WuQi" and p in WEAPON_FAMILY_MAP:
            pieces.append(WEAPON_FAMILY_MAP[p])
        elif cat == "An" and p in ANIMAL_FAMILY_MAP:
            pieces.append(ANIMAL_FAMILY_MAP[p])
        elif cat in ("DaoJu", "Item") and p in ITEM_FAMILY_MAP:
            pieces.append(ITEM_FAMILY_MAP[p])
        elif p in WEAPON_FAMILY_MAP:
            pieces.append(WEAPON_FAMILY_MAP[p])
        elif p in ANIMAL_FAMILY_MAP:
            pieces.append(ANIMAL_FAMILY_MAP[p])
        elif p in ITEM_FAMILY_MAP:
            pieces.append(ITEM_FAMILY_MAP[p])
        elif p in TIER_SUFFIX_MAP:
            v = TIER_SUFFIX_MAP[p]
            if v:
                pieces.append(v)
        else:
            # Unknown token -- leave raw so the operator sees the
            # unprocessed part. Beats hiding it.
            pieces.append(p)

    if len(pieces) <= 1:
        # Just a category and no body -- not useful
        return None
    return " ".join(pieces).strip()


# ── Annotation: turn a raw log line into HTML with translation spans ───────


# Match BP_<category>_<...> AND DaoJu_<...> tokens. Word boundary at
# either end, alphanumerics + underscores in the body. Trailing _C
# (UE class marker) is consumed too.
_BP_NAME_RE = re.compile(
    r"\bBP_[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"
    r"|\bDaoJu_[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+\b"
)


def annotate_line(raw: str) -> str:
    """Return HTML-safe string with translation spans inserted next
    to recognized tokens. Use as innerHTML on the client (server has
    already escaped).

    Order of operations:
      1. HTML-escape the raw line (XSS-safe for innerHTML).
      2. Replace BP_/DaoJu_ asset names with `<token> [English]`.
      3. Replace Chinese phrases (longest first).
      4. Replace pinyin standalone tokens (longest first, word-boundary).
    """
    out = html.escape(raw, quote=False)

    # 1. BP_ / DaoJu_ asset names
    def _bp_repl(m: re.Match) -> str:
        token = m.group(0)
        eng = parse_bp_name(token)
        if eng:
            return f'{token} <span class="tr">[{html.escape(eng)}]</span>'
        return token
    out = _BP_NAME_RE.sub(_bp_repl, out)

    # 2. Chinese phrases. Longest first so 'XYZ123' doesn't get
    #    partial-matched by 'XYZ' before we see the longer phrase.
    for chinese, eng in sorted(CHINESE_PHRASE_MAP.items(),
                                key=lambda x: -len(x[0])):
        if chinese in out:
            esc_eng = html.escape(eng, quote=False)
            out = out.replace(
                chinese,
                f'{chinese} <span class="tr">[{esc_eng}]</span>',
            )

    # 3. Pinyin standalone tokens. Word-boundary; case-sensitive
    #    (preserves CamelCase pattern).
    for pinyin, eng in sorted(PINYIN_TOKEN_MAP.items(),
                               key=lambda x: -len(x[0])):
        # Avoid double-translation: skip a token already inside an
        # annotation span. Cheap heuristic: don't translate inside
        # an existing <span class="tr">.
        # (For simplicity we just don't worry about it -- the spans
        # get re-rendered on each client refresh and we're operating
        # on already-escaped HTML where '<' is '&lt;'.)
        pattern = r"(?<![A-Za-z0-9_])" + re.escape(pinyin) + r"(?![A-Za-z0-9_])"
        repl = f'{pinyin} <span class="tr">[{html.escape(eng, quote=False)}]</span>'
        out = re.sub(pattern, repl, out)

    return out


# ── Single-term lookup (for use outside log rendering) ─────────────────────


def translate_term(term: str) -> Optional[str]:
    """Look up a single term and return its English translation, or
    None if unknown. Useful when rendering structured fields (event
    summaries, settings labels) where you don't want to scan the
    whole string."""
    if term in CHINESE_PHRASE_MAP:
        return CHINESE_PHRASE_MAP[term]
    if term in PINYIN_TOKEN_MAP:
        return PINYIN_TOKEN_MAP[term]
    if term in WEAPON_FAMILY_MAP:
        return WEAPON_FAMILY_MAP[term]
    if term in ANIMAL_FAMILY_MAP:
        return ANIMAL_FAMILY_MAP[term]
    if term in ITEM_FAMILY_MAP:
        return ITEM_FAMILY_MAP[term]
    if term in BP_CATEGORY_MAP:
        return BP_CATEGORY_MAP[term]
    if term.startswith("BP_") or term.startswith("DaoJu_"):
        return parse_bp_name(term)
    return None
