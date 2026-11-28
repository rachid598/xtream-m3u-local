"""Construction d'une playlist personnalisée : sélection, règles, doublons, noms, numéros, favoris.

Partagé par les playlists M3U (``/p/<slug>.m3u``) et le serveur Xtream local (``player_api.php``)
pour que les deux présentent exactement la même sélection.
"""
from __future__ import annotations

import re
import threading
import unicodedata

from .custom import SELECTION_FIELDS, filter_kind

GROUP_PREFIX = {"live": "", "vod": "vod:", "series": "series:"}
FAVORITES_GROUP = "★"
CUSTOM_PREFIX = "g:"       # clé d'un groupe personnalisé du direct : « g:tnt »
KEEP_ORIGINAL = "-"        # item_groups[id] = "-" : la chaîne reste dans son groupe d'origine

# Qualité déduite du nom de la chaîne (pour ne garder que la meilleure version d'un doublon).
_QUALITY = [
    (re.compile(r"(?<![a-z0-9])(8k|4k|uhd|3840p?|2160p?)(?![a-z0-9])"), 4.0),
    (re.compile(r"(?<![a-z0-9])(fhd|full ?hd|1080[pi]?)(?![a-z0-9])"), 3.0),
    (re.compile(r"(?<![a-z0-9])(hd|720p?)(?![a-z0-9])"), 2.0),
    (re.compile(r"(?<![a-z0-9])(sd|576[pi]?|480[pi]?)(?![a-z0-9])"), 1.0),
]
_TAGS = re.compile(
    r"(?<![a-z0-9])(8k|4k|uhd|3840p?|2160p?|fhd|full ?hd|1080[pi]?|hd|720p?|sd|576[pi]?|480[pi]?|hevc|"
    r"h\.?26[45]|x26[45]|hdr10?|dolby|atmos|60 ?fps|50 ?fps|30 ?fps|25 ?fps|"
    r"multi[- ]?audio|multi[- ]?langues?|backup|bkp|bk|raw|multi|vip|local|low|lq|hq)(?![a-z0-9])"
)


_SEPARATOR_EDGE = r"[#=*~_\-|•●◆★☆▬▭━─═\s]"
_SEPARATOR = re.compile(rf"^{_SEPARATOR_EDGE}*[#=*~_\-•●◆★☆▬▭━─═]{{3,}}.*[#=*~_\-•●◆★☆▬▭━─═]{{3,}}{_SEPARATOR_EDGE}*$")


# Générique : au moins 3 symboles (ni lettre, ni chiffre, ni espace) au début ET à la fin du nom :
# « ♣♦♣----|FR| FRANCE HD |FR|----♣♦♣ », « •●★---|FR| CINEMA HD |FR|---★●• »…
_SYMBOL_EDGES = re.compile(r"^\s*(?:[^\w\s]|_){3,}.*(?:[^\w\s]|_){3,}\s*$")


# Emplacement d'événement vide (chaînes PPV) : « - NO EVENT STREAMING - | 4K EXCLUSIVE | … ».
_PLACEHOLDER = re.compile(r"^[\W_]*no\s+(live\s+)?events?\b|\bno\s+event\s+streaming\b", re.IGNORECASE)


def is_separator(name: str) -> bool:
    """Fausse chaîne : titre de section du fournisseur (« #### PRIME #### », « ===== FR ===== »…)
    ou emplacement d'événement vide (« NO EVENT STREAMING ») — elle reprend un vrai nom quand un événement démarre."""
    text = (name or "").strip()
    if not text:
        return True
    if _PLACEHOLDER.search(text):
        return True
    if _SEPARATOR.match(text) or _SYMBOL_EDGES.match(text):
        return True
    return not any(ch.isalnum() for ch in text)


_ADULT_RE = re.compile(
    r"(?<![a-z0-9])(?:xxx|18\s*\+|\+\s*18|for\s+adults?|adults?(?!\s+swim)|adultes?|porno?|eroti(?:c|que)|"
    r"x-rated|nsfw|dorcel|brazzers|penthouse|hustler|playboy\s*tv)(?![a-z0-9])",
    re.IGNORECASE,
)


def is_adult_category(name: str) -> bool:
    """Catégorie réservée aux adultes (18+, XXX, Adult, Dorcel…)."""
    return bool(_ADULT_RE.search(normalize(name)))


def is_adult_item(item: dict, categories: dict[str, str] | None = None) -> bool:
    """Élément réservé aux adultes (drapeau ``is_adult`` du fournisseur, nom ou catégorie)."""
    if item.get("adult"):
        return True
    if _ADULT_RE.search(normalize(item.get("name") or "")):
        return True
    if categories:
        for cid in item.get("category_ids") or ():
            if is_adult_category(categories.get(cid, "")):
                return True
    return False


_SMALL_CAPS = str.maketrans({
    "ᴀ": "A", "ʙ": "B", "ᴄ": "C", "ᴅ": "D", "ᴇ": "E", "ꜰ": "F", "ɢ": "G", "ʜ": "H", "ɪ": "I",
    "ᴊ": "J", "ᴋ": "K", "ʟ": "L", "ᴍ": "M", "ɴ": "N", "ᴏ": "O", "ᴘ": "P", "ǫ": "Q", "ʀ": "R",
    "ꜱ": "S", "ᴛ": "T", "ᴜ": "U", "ᴠ": "V", "ᴡ": "W", "ʏ": "Y", "ᴢ": "Z",
})
_SOCCER_O = re.compile(r"(sp)\s*⚽\ufe0f?\s*(rts)", re.IGNORECASE)
_NOISE_BRACKETS = re.compile(
    r"(?:\*+\s*(?:local|backup|bkp|bk|multi|raw|hevc|h\.?26[45]|vip|fhd|uhd|hd|sd|4k|8k|low|lq|hq)\s*\*+"
    r"|[\[(]\s*(?:local|backup|bkp|bk|multi(?:[- ]?(?:audio|langues?))?|raw|hevc|h\.?26[45]|vip|fhd|uhd|hd|sd|4k|8k|"
    r"3840p?|2160p?|1080p?|720p?|low|lq|hq|hdr10?|50\s*fps|60\s*fps|30\s*fps|25\s*fps|5\.1|aac|ac3|"
    r"live(?:\s+event(?:\s+only)?)?|event\s+only|stz|stc|13)\s*[\])])",
    re.IGNORECASE,
)
_SLASH_QUALITY = re.compile(
    r"(?:^|(?<=\s))(?:(?:8k|4k|uhd|fhd|hd|sd|raw|hevc|hdr)\s*/\s*)+(?:8k|4k|uhd|fhd|hd|sd|raw|hevc|hdr)(?=\s|$|\.)",
    re.IGNORECASE,
)
_GLUED_DIGIT_QUALITY = re.compile(r"(?<=\d)(?:fhd|uhd|hd|sd)(?=\b|$|\s|\.)", re.IGNORECASE)


def _expand_superscripts(text: str) -> str:
    """Convertit exposants et petites capitales Unicode (« ᴿᴬᵂ », « ᴴᴰ », « ꜰʜᴅ », « ⁴ᴷ », « ᶜᶦᵗʸ ») sans perdre les accents."""
    out: list[str] = []
    for ch in text or "":
        if ch == "ᶦ":
            out.append("i")
            continue
        decomp = unicodedata.decomposition(ch)
        if decomp and (decomp.startswith("<super>") or decomp.startswith("<compat>") or decomp.startswith("<small>")):
            out.append(unicodedata.normalize("NFKD", ch))
        else:
            out.append(ch)
    return "".join(out).translate(_SMALL_CAPS)


def _fix_soccer_o(text: str) -> str:
    """Remplace « SP⚽RTS » / « Sp⚽rts » (très courant chez les fournisseurs 8K) par SPORTS / Sports."""
    return _SOCCER_O.sub(lambda m: m.group(1) + ("O" if m.group(1).isupper() else "o") + m.group(2), text)


# Préfixe de pays/fournisseur retiré par match_name : « FR: », « |FR| », « [FR] », « VIP: », « SPO: », « 8K: »,
# « BE-VIP: », « |4K| FR| », « |BEIN FR| », « F: ». Note : « |BEIN AR| » est volontairement laissé ici pour éviter
# de fusionner beIN AR dans les groupes FR non régionaux ; regional_group_key() le classe en région « ar ».
_MATCH_PREFIX = re.compile(
    r"^(?:\s*[\[\(|•●★☆▶►◉♦♣✦▼\-]+\s*)*"
    r"(?:(?:[A-Za-z]{2}(?:\s*[-/]\s*(?:VIP|OTT|HD|FHD|4K|UHD|RAW|HEVC))?"
    r"|VIP|SPO|UHD|FHD|8K|4K|HEVC|F|BEIN\s+FR)\s*[:|\-\]\)●•»◉]+\s*)+",
    re.IGNORECASE,
)

# Synonymes de chaînes réelles (normalisés) pour aligner plusieurs fournisseurs et modèles de groupes.
_CANONICAL_ALIASES = {
    "canal+ box offcie": "canal+ box office",
    "canal+ cinema s": "canal+ cinema",
    "canal docs": "canal+ docs",
    "rmc alerte securs": "rmc alerte secours",
    "discovery invest": "discovery investigation",
    "discovery id": "discovery investigation",
    "planete": "planete+",
    "planete ae": "planete+ aventure",
    "planete a&e": "planete+ aventure",
    "planete ci": "planete+ crime",
    "polar": "polar+",
    "piwi": "piwi+",
    "golf": "golf+",
    "infosport": "infosport+",
    "l equipe 21": "l equipe",
    "la chaine parlementaire": "lcp",
    "euronews fr": "euronews",
    "novelas": "novelas tv",
    "museum": "museum tv",
    "museum channel": "museum tv",
    "histoire": "histoire tv",
    "melody tv": "melody",
    "lucky jack tv": "lucky jack",
    "nick junior": "nickelodeon junior",
    "nick 4 teen": "nickelodeon teen",
    "equidia live": "equidia",
    "automoto la chaine": "automoto",
    "game one1": "game one",
    "teletoon 1": "teletoon+1",
    "disney channel 1": "disney channel+1",
    "nickelodeon 1": "nickelodeon+1",
    "telemaroc": "tele maroc",
    "ol tv": "oltv",
    "natgeo": "national geographic",
    "nat geo": "national geographic",
    "national geo": "national geographic",
    "natgeo wild": "nat geo wild",
    "national geo wild": "nat geo wild",
    "tv5monde": "tv5 monde",
    "tv5monde europe": "tv5 monde europe",
    "ma tele": "matele",
    "no tele": "notele",
    "tele sambre": "telesambre",
    "tele mb": "telemb",
    "qu4tre rtc liege": "rtc tele liege",
    "qu4tre": "rtc tele liege",
    "antenne centre tv": "antenne centre",
    "rtl club": "club rtl",
    "rtl plug": "plug rtl",
    "play sports 1 wallonie": "play sports 1 fr",
    "play sports 2 wallonie": "play sports 2 fr",
    "dazn pro leagues 1": "dazn pro league 1 fr",
    "dazn pro leagues 2": "dazn pro league 2 fr",
    "dazn sports 1 fr": "dazn 1 fr",
}


def match_name(name: str) -> str:
    """Nom comparable pour les groupes personnalisés : sans pays, accents, ponctuation ni qualité.

    « FR: TF1 FHD » → « tf1 » ; « |FR| CANAL + 4K » → « canal+ » ; « L'Équipe HD » → « l equipe ».
    """
    text = _fix_soccer_o(_expand_superscripts(name or ""))
    text = _MATCH_PREFIX.sub("", text)
    text = _NOISE_BRACKETS.sub(" ", text)
    text = _SLASH_QUALITY.sub(" ", text)
    text = _GLUED_DIGIT_QUALITY.sub("", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"\b13\s*\.\s*eme\s+rue\b", "13eme rue", text)
    text = re.sub(r"[^\w+&]+|_", " ", text)
    text = re.sub(r"\s+\+", "+", text)
    tokens = text.split()
    if len(tokens) > 1 and tokens[0] in ("fr", "fra"):
        tokens = tokens[1:]
    core = " ".join(_TAGS.sub(" ", " ".join(tokens)).split())
    core = re.sub(r"\b13\s+eme\s+rue\b", "13eme rue", core)
    core = re.sub(r"\beuro\s+sports?\b", "eurosport", core)
    core = re.sub(r"\bligue\s*1\+", "ligue 1+", core)
    core = re.sub(r"\bnat\s*geo\b", "nat geo", core)
    core = re.sub(r"\btoute\s+lhistoire\b", "toute l histoire", core)
    core = re.sub(r"\bchasse\s*&\s*peche\b", "chasse et peche", core)
    core = re.sub(r"\bsciences?\s*(?:&|et)?\s*vie(?:\s+tv)?\b", "science & vie", core)
    core = re.sub(r"\bbe\s*in\s+sports?\b", "bein sports", core)
    core = re.sub(r"\bbein sports 0([1-9])\b", r"bein sports \1", core)
    return _CANONICAL_ALIASES.get(core, core)


_REGION_PREFIX = re.compile(r"^\W*(?:(bein)\s+)?([a-z0-9]{1,4})(?:\s*-\s*(?:vip|ott|hd|fhd|raw))?\s*[:|\-\]●•»◉]+\s*")
_ARAB_COUNTRIES = frozenset("ar ma dz tn ly mr eg sa ksa ae uae qa kw om bh iq jo lb sy ps sd ye".split())
_OTHER_COUNTRIES = frozenset("af at au br ca ch cz de dk es fi gb gr hr hu in it jp kr nl no nz pl pt ro ru se sk tr uk us".split())
_ARAB_CATEGORY = re.compile(
    r"^\W*(?:ar[| :\-]|arabic\b|maghreb\b|maroc\b|morocco\b|algerie\b|tunisie\b|egypt\b|mbc groupe\b|thmanyah\b|starzplay ad sport\b|"
    r"alwan sport\b|bein sports (?:fhd|h265)\b|(?:ma|dz|tn|eg|iq|qa|ksa|uae)\s*[|:]|fr\s*[|:]\s*(?:maroc|morocco|maghreb)\b)"
)
_FR_CATEGORY = re.compile(r"^\W*(?:fr\s*[|:]|france\b)")
_REGIONAL_ALIASES = {
    "l equipe 21": "l equipe", "la chaine l equipe": "l equipe",
    "arriadia": "arryadia", "arriyadia": "arryadia", "arryadia live": "arryadia",
    "arryadia tv maroc": "arryadia", "arryadia tnt local": "arryadia tnt",
    "al aula": "al aoula", "al aoula inter": "al aoula", "al aoula international": "al aoula",
    "al aoula maroc": "al aoula", "tvm": "al aoula",
    "2m natioanl": "2m", "2m national": "2m",
    "al laayoune": "laayoune tv", "laayoune": "laayoune tv", "tamazight tv": "tamazight",
    "tamazight moroc": "tamazight", "tamazight maroc": "tamazight", "tamazighte": "tamazight",
    "assadissa moroc": "assadissa", "assadissa maroc": "assadissa",
    "arrabia": "athaqafia", "arrabiaa": "athaqafia", "assaqafia": "athaqafia", "attakafia": "athaqafia",
    "tele moroc": "tele maroc", "telemaroc": "tele maroc", "aflam 7 local": "aflam 7", "m24 maroc": "m24", "m24 tv": "m24",
    "al ons": "al ons tv", "mbc5": "mbc 5",
    "medi1tv": "medi 1 tv", "mbc egypt": "mbc masr",
    "mbc egypt 2": "mbc masr 2", "مصر mbc": "mbc masr", "mbc مصر": "mbc masr",
    "mbc مصر 2": "mbc masr 2", "القناة الأولى": "al aoula", "الرياضية": "arryadia",
    "alqass": "alkass", "sportenfrance": "sport en france", "automoto tv": "automoto",
    "automoto la chaine": "automoto",
    "golf plus": "golf+", "equidia live": "equidia", "infosport": "infosport+",
    "on time sports": "on sport", "on time sports 1": "on sport", "on sports": "on sport",
    "on sports plus": "on sport plus", "zamalek sport": "zamalek sports",
    "al ahly": "al ahly tv", "oman tv sport": "oman sports", "oman sport tv": "oman sports",
    "kuwait sport": "kuwait sports", "kuwait sport 1": "kuwait sports", "kuwait sports 2": "kuwait sports plus",
    "sharjah sport": "sharjah sports", "sharjah sport 2": "sharjah sports 2",
    "dubai sport 1": "dubai sports 1", "dubai sport 2": "dubai sports 2", "dubai sport 3": "dubai sports 3",
    "dubai racing 1 tv": "dubai racing 1", "yas sport": "yas sports",
    "saudia sport 1": "ksa sport 1", "saudia sport 2": "ksa sport 2", "saudia sport 3": "ksa sport 3", "saudia sport 4": "ksa sport 4",
    "al rabiaa sport": "alrabiaa sport one", "ad sports tv": "ad sports 1",
    "mbc masr 1": "mbc masr", "mbc drama masr": "mbc masr drama", "rotana khalijiah": "rotana khalijia",
    "yah ala": "osntv yahala", "yahala": "osntv yahala", "osn yahala": "osntv yahala",
    "yahala bil arabi": "osntv yahala bil arabi", "osn yahala bil arabi": "osntv yahala bil arabi",
    "yahala aflam": "osntv yahala aflam", "osn yahala aflam": "osntv yahala aflam",
    "on e": "on", "on e tv": "on", "cbc tv": "cbc", "cbc drama tv": "cbc drama", "cbc sofra tv": "cbc sofra",
    "al masryia": "al masriya", "al aoula laayoune": "laayoune tv", "al aoula layoune": "laayoune tv",
    "medi 1 maghreb tv": "medi 1 tv maghreb", "medi 1 arabic tv": "medi 1 tv",
    "a3 algerie": "a3", "algerie 4 tamazight": "tv4 tamazight", "algerie 5 quran": "tv5 coran",
    "tunisia nat 1": "wataniya 1", "tunisia nat 2": "wataniya 2",
    "tunisia 1": "wataniya 1", "tunisia 2": "wataniya 2",
    "tunisia 1 tnt": "wataniya 1", "tunisia 2 tnt": "wataniya 2",
    "nessma tv": "nessma", "attessia tv": "attessia",
    "abu dhabi": "abu dhabi tv", "abu dhabi al emarat": "al emarat",
    "qatar": "qatar tv", "al rayyan tv": "al rayyan", "oman tv general": "oman tv",
    "jordan": "jordan tv", "jordan satellite channel": "jordan tv", "sharjah sports 2 e a u": "sharjah sports 2",
    "bbc world news arabic": "bbc arabic", "trt news arabic": "trt arabi",
    "abu dhabi natioanl geo": "national geographic abu dhabi", "bein nat geo": "national geographic abu dhabi",
    "bein nat geographic": "national geographic abu dhabi", "majid": "majid tv", "majid kids tv": "majid tv",
    "bein jeem": "jeem tv", "bein baraem": "baraem", "saudi quran": "saudi quran tv",
    "saudi sunnah": "saudi sunnah tv",
    "mbc wanasah": "wanasah", "al ahrar": "libya al ahrar", "libya al ahrar tv": "libya al ahrar",
    "al wasat tv wtv": "al wasat", "elmouritania": "el mouritania", "sbc saudia": "sbc",
    "kuwait 1": "kuwait tv 1", "kuwait 2": "kuwait tv 2", "sunnah tv": "saudi sunnah tv",
    "saudia sunnah": "saudi sunnah tv",
}


def regional_group_key(name: str, category: str) -> tuple[str, str]:
    """Région et nom comparables pour Sport FR, Sport AR et Nilesat.

    Une catégorie du fournisseur peut mélanger plusieurs pays. Les marqueurs de la chaîne
    (p. ex. « |BEIN FR| » et « |BEIN AR| ») et la langue explicite de beIN passent avant elle.
    """
    text = normalize(_fix_soccer_o(_expand_superscripts(name)))
    label = normalize(_expand_superscripts(category))
    prefix = _REGION_PREFIX.match(text)
    code = prefix[2] if prefix else ""
    if prefix:
        text = text[prefix.end():]
    text = re.sub(r"\bh[ .]?26[45]\b|\b8k\b", " ", text)
    text = _GLUED_DIGIT_QUALITY.sub("", text)
    core = match_name(text)
    if re.search(r"^be\s*in\s*(?:sports?|max)\b", core):
        if re.search(r"\b(?:english|en)\b", core):
            return "", ""
        if re.search(r"\b(?:fr|france|french)\b", core):
            if code != "fr" and _ARAB_CATEGORY.match(label):
                return "", ""
            code = "fr"
            core = re.sub(r"\b(?:fr|france|french)\b", "", core).strip()
        core = re.sub(r"^be\s*in\s*(?:sports?\s+)?", "bein sports ", core)
        core = re.sub(r"^(bein sports) 0([1-9])\b", r"\1 \2", core)
        core = re.sub(r"^(bein sports) (\d+) (?:extra|xtra)$", r"\1 xtra \2", core).strip()
        core = core.replace(" sports extra ", " sports xtra ")
    if code in _OTHER_COUNTRIES or re.search(r"^(?:ma\| malta|bh\| bosnia|tn\| taiwan)", label):
        return "", ""
    if (prefix and prefix[1] and code == "fr") or (code == "fr" and "oqee arabic" not in label):
        region = "fr"
    elif code in _ARAB_COUNTRIES or _ARAB_CATEGORY.match(label) or "oqee arabic" in label:
        region = "ar"
    else:
        region = "fr" if _FR_CATEGORY.match(label) and "africa" not in label else ""
    if not region:
        return "", ""

    core = re.sub(r"\bcanal\+\s*(?=foot|sport|live)", "canal+ ", core)
    core = re.sub(r"\bligue\s*1\s*\+\s*", "ligue 1+ ", core).strip()
    core = re.sub(r"^al\s*kass\s*", "alkass ", core).strip()
    core = re.sub(r"^(?:thmanayh|thmanyah|thamania)\s*(?:sports?\s*)?", "thmanyah ", core).strip()
    core = re.sub(r"^(?:ad|abu dhabi) sports?\s+", "ad sports ", core)
    core = re.sub(r"^(?:adsp|ad sports) (\d+) premium$", r"ad sports premium \1", core)
    core = re.sub(r"^gobx\s+(?=mbc\b)", "", core)
    core = re.sub(r"^mbc\s*(?=\d)", "mbc ", core)
    if label.startswith("ar| osn"):
        core = re.sub(r" f$", "", core)
    return region, _REGIONAL_ALIASES.get(core, core)


_NUMBERED = re.compile(r"^\s*(\d{1,5})\s*[:=]\s*(.+)$")


def split_pattern(text: str) -> tuple[int | None, str]:
    """« 4: Canal+ » → (4, « Canal+ ») : numéro de chaîne optionnel (comme sur un décodeur)."""
    found = _NUMBERED.match(text or "")
    if found and 1 <= int(found.group(1)) <= 99_999:
        return int(found.group(1)), found.group(2)
    return None, text or ""


def group_matches(core: str, group: dict) -> int | None:
    """Rang du premier motif du groupe qui correspond au nom (déjà passé par :func:`match_name`)."""
    padded = f" {core} "
    for rank, pattern in enumerate(group.get("_patterns") or ()):
        if (core == pattern) if group.get("match") == "exact" else (f" {pattern} " in padded):
            return rank
    return None


_CLEAN_PREFIX = re.compile(
    r"^(?:\s*[\[\(|•●★☆▶►◉♦♣✦▼\-]+\s*)*"
    r"(?:(?:[A-Za-z]{2,3}(?:\s*[-/]\s*(?:VIP|OTT|HD|FHD|4K|UHD|RAW|HEVC))?"
    r"|VIP|SPO|UHD|FHD|8K|4K|HEVC|F|BEIN\s+(?:FR|AR))\s*[:|\-\]\)●•»◉]+\s*)+",
    re.IGNORECASE,
)
_CLEAN_SYMBOLS = re.compile(r"[◉●★☆▶►•♦♣✦▼▲⚽✟\ufe0f]+")
_CLEAN_TAGS = re.compile(
    r"(?:^|(?<=\s))(?:full\s*hd|fhd|uhd|4k|8k|3840p?|2160p?|1080[pi]?|720p?|576[pi]?|480[pi]?|"
    r"hd|sd|hevc|h\.?26[45]|x26[45]|hdr10?|dolby|atmos|60\s*fps|50\s*fps|30\s*fps|25\s*fps|"
    r"multi[- ]?audio|multi[- ]?langues?|raw|vip|low|lq|hq|backup|bkp|bk|multi|local|5\.1|aac|ac3)\.?(?=\s|$)",
    re.IGNORECASE,
)


def clean_channel_name(name: str) -> str:
    """Mode Décodeur : retire préfixes pays/fournisseur, symboles et mentions techniques en gardant les accents."""
    raw = str(name or "").strip()
    if not raw:
        return raw
    text = _fix_soccer_o(_expand_superscripts(raw))
    text = _CLEAN_SYMBOLS.sub(" ", text)
    prefix_match = _CLEAN_PREFIX.match(text)
    prefix_code = ""
    if prefix_match:
        m = re.match(r"^\W*([A-Za-z]{2})\b", prefix_match.group(0))
        prefix_code = m.group(1).upper() if m else ""
        text = text[prefix_match.end():]
        # Ex. « BE: BE VRT 1 » → « VRT 1 »
        if prefix_code and re.match(rf"^{prefix_code}\s+", text):
            text = text[len(prefix_code) + 1:]
    text = _NOISE_BRACKETS.sub(" ", text)
    text = _SLASH_QUALITY.sub(" ", text)
    text = _GLUED_DIGIT_QUALITY.sub("", text)
    text = re.sub(r"\s*[|•]+\s*", " ", text)
    # Deuxième passe si « |4K| FR| ARTE UHD » laissait un code pays après le « | »
    text = _CLEAN_PREFIX.sub("", text)
    text = _CLEAN_TAGS.sub(" ", text)
    text = re.sub(r"^[\s\-:|*]+|[\s\-:|*]+$", "", text)
    text = " ".join(text.split())
    return text or raw


def ordered_custom_groups(saved: dict) -> list[dict]:
    """Groupes personnalisés par priorité : ordre des groupes de la playlist, puis ordre de création."""
    groups = saved.get("custom_groups") or []
    rank = {key: i for i, key in enumerate(saved.get("group_order") or [])}
    ordered = sorted(enumerate(groups), key=lambda e: (rank.get(CUSTOM_PREFIX + e[1]["id"], len(rank)), e[0]))
    result = []
    for _, group in ordered:
        cores: dict[str, tuple[int | None, str]] = {}
        for value in group.get("patterns") or []:
            number, text = split_pattern(value)
            core = match_name(text)
            if core and core not in cores:
                cores[core] = (number, text.strip())
        result.append({
            **group,
            "_patterns": list(cores),
            "_numbers": [v[0] for v in cores.values()],
            "_labels": [v[1] for v in cores.values()],
        })
    return result


def apply_custom_groups(entries: list[tuple[dict, str]], saved: dict,
                        categories: dict[str, str] | None = None) -> list[tuple[dict, str]]:
    """Range les chaînes retenues dans les groupes personnalisés (« TNT », « Canal+ »…).

    Une chaîne rangée à la main (``item_groups``) va dans ce groupe ; sinon dans le premier groupe
    (le plus haut) dont un motif correspond à son nom. Dans un groupe : ordre des motifs, puis
    rangements manuels, puis ordre du fournisseur. Les chaînes déplacées quittent leur groupe d'origine.
    Un nom numéroté (« 4: Canal+ ») donne ce numéro à la chaîne, sauf numéro personnalisé (✎).
    """
    groups = ordered_custom_groups(saved)
    if not groups:
        return entries
    known = {g["id"] for g in groups}
    has_regional = any(g.get("region") for g in groups)
    categories = categories or {}
    manual = saved.get("item_groups") or {}
    # Guide : les chaînes de « Mes groupes » prennent d'abord les guides externes fiables (option
    # ``group_epg_external``, activée par défaut), rapprochées par le nom propre du groupe (« TF1 »).
    external_first = saved.get("group_epg_external", True)
    multi = bool(saved.get("multi_groups"))
    placed: list[tuple[int, int, int, tuple[dict, str]]] = []
    rest: list[tuple[dict, str]] = []
    priority = {g["id"]: i for i, g in enumerate(groups)}
    for index, (item, group) in enumerate(entries):
        target = manual.get(item["stream_id"])
        if target == KEEP_ORIGINAL:
            rest.append((item, group))
            continue
        if target in known:
            if external_first:
                item = {**item, "epg_prefer_external": True}
            placed.append((priority[target], 10_000, index, (item, CUSTOM_PREFIX + target)))
            continue
        core = match_name(item["name"])
        region, regional_core = regional_group_key(item["name"], categories.get(item["category_id"], "")) if has_regional else ("", "")
        original = item
        found = False
        for g in groups:
            scope = g.get("region")
            if scope and (scope != region or is_separator(original["name"])):
                continue
            rank = group_matches(regional_core if scope else core, g) if g["_patterns"] else None
            if rank is not None:
                item = original
                if scope:
                    item = {**item, "_regional_group": (region, regional_core)}
                number = g["_numbers"][rank]
                if number is not None:
                    item = {**item, "num": str(number), "custom_num": True}
                if g.get("match") == "exact" and g["_labels"][rank]:
                    item = {**item, "clean_name": g["_labels"][rank]}
                if region == "ar" and regional_core.startswith("bein sports ") and scope:
                    # Le guide beIN France peut porter le même nom que la chaîne MENA.
                    item = {**item, "epg_match_name": False}
                elif external_first:
                    # Nom propre du groupe (« TF1 ») seulement pour les listes exactes : un motif « beIN
                    # Sports » d'un groupe par mots ne désigne pas une chaîne précise.
                    item = {**item, "epg_prefer_external": True,
                            **({"epg_name": g["_patterns"][rank]} if g.get("match") == "exact" else {})}
                placed.append((priority[g["id"]], rank, index, (item, CUSTOM_PREFIX + g["id"])))
                found = True
                if not multi:
                    break
                # Option « une chaîne dans plusieurs groupes » : elle est aussi rangée dans les groupes
                # suivants qui la contiennent (même flux, même guide, aucune connexion de plus).
        if not found:
            rest.append((original, group))
    placed.sort(key=lambda e: e[:3])
    return [entry for *_, entry in placed] + rest


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", _expand_superscripts(text or ""))  # « ᴴᴰ » → « hd », « ꜰʜᴅ » → « fhd », « ⁸ᴷ » → « 8k »
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.lower().split())


def quality(name: str) -> float:
    lowered = normalize(name)
    for pattern, score in _QUALITY:
        if pattern.search(lowered):
            return score
    return 1.5  # sans mention : entre SD et HD


def base_name(name: str) -> str:
    """Nom sans mention de qualité ni ponctuation : « TF1 HD », « TF1 (FHD) » → « tf1 »."""
    text = _TAGS.sub(" ", normalize(name))
    text = re.sub(r"[|()\[\]{}:._\-+*/\\]+", " ", text)
    return " ".join(text.split())


def rule_matches(category_name: str, rule: dict) -> bool:
    name, value = normalize(category_name), normalize(rule["value"])
    if rule["match"] == "prefix":
        return name.startswith(value)
    if rule["match"] == "exact":
        return name == value
    return value in name


_GARBAGE_EPG_IDS = frozenset({"ts", "none", "null", "n/a", "na", "0", "-", "--", "unknown", "dummy", "test"})
_EPG_ID_SUFFIX = re.compile(
    r"(?:@[a-z0-9]+|\.[a-z]{2,4}(?:\.[a-z]{2})?|\.(?:provider|ext|local|tv|hd|sd|fhd|uhd|4k))$",
    re.IGNORECASE,
)
_EPG_STOPWORDS = frozenset({
    "tv", "hd", "sd", "fhd", "uhd", "4k", "8k", "ch", "channel", "chaine", "live", "plus", "news", "the",
    "sport", "sports", "cinema", "cinemas", "movie", "movies", "film", "films", "music", "kids", "info",
    "radio", "france", "french", "arab", "arabic", "maroc", "morocco", "world", "international", "inter",
    "national", "network", "extra", "max", "prime", "gold", "one", "two", "three",
})
_EPG_FAMILY_NOISE = re.compile(
    r"\b(?:ultra|sat|mobil|eon|hdr|solo|eventos|sub|dvb|overlay|hot|yes|no|ca|en|fr|enf|kids|live|plus)\b"
)


def is_usable_epg_id(eid: str | None) -> bool:
    """Vrai si l'identifiant XMLTV semble réel (écarte les valeurs vides ou génériques comme « TS » ou « 0 »)."""
    text = (eid or "").strip().lower()
    if len(text) < 3 or text in _GARBAGE_EPG_IDS:
        return False
    return any(ch.isalnum() for ch in text)


def _epg_id_stem(eid: str) -> str:
    text = (eid or "").strip().lower()
    for _ in range(3):
        stripped = _EPG_ID_SUFFIX.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9+]+", "", text).replace("+", "plus")


def epg_id_matches_name(eid: str, *names: str) -> bool:
    """Vrai si l'identifiant XMLTV (ex. ``01tv.fr``) est cohérent avec l'un des noms donnés."""
    stem = _epg_id_stem(eid)
    if not stem:
        return False
    for name in names:
        core = match_name(name or "")
        if not core:
            continue
        nsquash = core.replace("+", "plus").replace(" ", "")
        tokens = [t.replace("+", "plus") for t in core.split()]
        if stem == nsquash or stem in tokens:
            return True
        if len(stem) >= 3 and len(nsquash) >= 3 and (stem in nsquash or nsquash in stem):
            return True
        if len(nsquash) >= 2 and stem.startswith(nsquash) and not nsquash.isdigit():
            return True
        for tok in tokens:
            if len(tok) >= 4 and not tok.isdigit() and tok not in _EPG_STOPWORDS and tok in stem:
                return True
        brand = next((t for t in tokens if len(t) >= 3 and not t.isdigit() and t not in _EPG_STOPWORDS), None)
        if brand and brand in stem:
            return True
        if "national" in tokens and "geographic" in tokens and "natgeo" in stem:
            return True
    return False


def _epg_core_family(core: str) -> str:
    cleaned = _EPG_FAMILY_NOISE.sub(" ", core)
    parts = cleaned.split()
    return " ".join(parts[:3]) if parts else core


def sanitize_epg_ids(entries: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """Nettoie les identifiants EPG bidons (« TS ») ou empoisonnés par le fournisseur (ex. ``01tv.fr`` sur 50 chaînes).

    Un identifiant attribué par le fournisseur à au moins 3 chaînes distinctes dont le nom n'a aucun rapport
    avec lui est effacé de ces chaînes (la vraie chaîne correspondante, ex. ``01TV``, garde le sien).
    Ainsi M3U, Xtream et XMLTV attribuent à chacune son propre identifiant ``xtream-<id>`` et son vrai guide.
    """
    bad_cores: dict[str, set[str]] = {}
    bad_families: dict[str, set[str]] = {}
    for item, _ in entries:
        if item.get("epg_manual"):
            continue
        eid = (item.get("epg_id") or "").strip().lower()
        if not is_usable_epg_id(eid):
            continue
        core = match_name(item.get("name") or "")
        if core and not epg_id_matches_name(eid, item.get("name") or "", item.get("epg_name") or ""):
            bad_cores.setdefault(eid, set()).add(core)
            bad_families.setdefault(eid, set()).add(_epg_core_family(core))
    poisoned = {eid for eid, cores in bad_cores.items() if len(cores) >= 3 and len(bad_families.get(eid, ())) >= 3}
    result: list[tuple[dict, str]] = []
    for item, group in entries:
        if item.get("epg_manual"):
            result.append((item, group))
            continue
        eid = (item.get("epg_id") or "").strip()
        low = eid.lower()
        if eid and (not is_usable_epg_id(eid) or (
            low in poisoned and not epg_id_matches_name(low, item.get("name") or "", item.get("epg_name") or "")
        )):
            item = {**item, "epg_id": ""}
        result.append((item, group))
    return result


MAX_ALTERNATES = 8
UHD_MODES = ("backup", "normal", "never")
UHD_QUALITY = 4.0


def version_rank(name: str, uhd: str = "backup") -> float:
    """Rang d'une version pour choisir la chaîne gardée et l'ordre des secours.

    ``uhd`` (option de playlist « Versions 4K / UHD ») : ``backup`` (défaut) = une version 4K passe
    après toutes les autres (beaucoup de « 4K » de fournisseurs sont des chaînes d'événement ou une
    vidéo de logo en boucle) ; ``normal`` = meilleure qualité d'abord ; ``never`` = 4K jamais utilisée
    comme version d'une autre chaîne.
    """
    q = quality(name)
    return 0.5 if q == UHD_QUALITY and uhd != "normal" else q


def dedupe(entries: list[tuple[dict, str]], uhd: str = "backup") -> list[tuple[dict, str]]:
    """Dans chaque groupe, garde la meilleure qualité d'une même chaîne (SD/HD/FHD/4K…).

    La version gardée reçoit ``alternates`` : les autres versions, de la meilleure à la moins bonne,
    utilisées par la bascule automatique (option ``failover``). Si la version gardée n'a pas d'identifiant
    EPG utilisable, elle hérite du premier identifiant EPG valide trouvé parmi ses doublons fusionnés.
    """
    versions: dict[tuple[str, str | tuple[str, str]], list[tuple[float, int]]] = {}
    for index, (item, group) in enumerate(entries):
        # Même nom comparable que les groupes perso : « VIP: TF1 ᴴᴰ », « FR: TF1 », « AF: TF1 » → « tf1 ».
        key = (group, item.get("_regional_group") or match_name(item["name"]) or base_name(item["name"]))
        versions.setdefault(key, []).append((version_rank(item["name"], uhd), index))
    keep: dict[int, list[int]] = {}
    for found in versions.values():
        ranked = [index for _, index in sorted(found, key=lambda v: (-v[0], v[1]))]
        if uhd == "never":
            # 4K écartée dès qu'une autre version existe (une chaîne qui n'existe qu'en 4K reste).
            others = [i for i in ranked if quality(entries[i][0]["name"]) != UHD_QUALITY]
            ranked = others or ranked
        keep[ranked[0]] = ranked[1:]
    result = []
    for index, (item, group) in enumerate(entries):
        if index not in keep:
            continue
        merged_indices = keep[index]
        others = [entries[i][0]["stream_id"] for i in merged_indices][:MAX_ALTERNATES]
        updates: dict = {}
        if others:
            updates["alternates"] = others
        if not is_usable_epg_id(item.get("epg_id")):
            inherited = next(
                (entries[i][0]["epg_id"] for i in merged_indices
                 if is_usable_epg_id(entries[i][0].get("epg_id"))
                 and epg_id_matches_name(entries[i][0]["epg_id"], item.get("name") or "")),
                None,
            ) or next(
                (entries[i][0]["epg_id"] for i in merged_indices if is_usable_epg_id(entries[i][0].get("epg_id"))),
                None,
            )
            if inherited:
                updates["epg_id"] = inherited
        result.append(({**item, **updates} if updates else item, group))
    return result


_CORE_INDEX_LOCK = threading.Lock()
_CORE_INDEX_CACHE: dict[int, tuple[list[dict], dict[str, list[tuple[float, str]]]]] = {}
_CORE_INDEX_ORDER: list[int] = []
_CORE_INDEX_MAX = 8  # catalogues distincts gardés en mémoire (multi-fournisseurs, tests…)


def _core_index(items: list[dict]) -> dict[str, list[tuple[float, str]]]:
    """Index ``{nom comparable: [(qualité, stream_id)…]}`` (meilleure qualité d'abord) de tout le catalogue.

    Caché par identité de la liste ``items`` (la référence est conservée tant que l'entrée reste en cache
    pour éviter toute collision d'adresse mémoire après libération d'une liste filtrée par profil).
    """
    key = id(items)
    with _CORE_INDEX_LOCK:
        cached = _CORE_INDEX_CACHE.get(key)
        if cached is not None and cached[0] is items:
            return cached[1]
    index: dict[str, list[tuple[float, str]]] = {}
    for item in items:
        name = item.get("name") or ""
        if is_separator(name):
            continue
        core = match_name(name) or base_name(name)
        index.setdefault(core, []).append((quality(name), item["stream_id"]))
    for versions in index.values():
        versions.sort(key=lambda v: -v[0])
    with _CORE_INDEX_LOCK:
        _CORE_INDEX_CACHE[key] = (items, index)
        if key in _CORE_INDEX_ORDER:
            _CORE_INDEX_ORDER.remove(key)
        _CORE_INDEX_ORDER.append(key)
        while len(_CORE_INDEX_ORDER) > _CORE_INDEX_MAX:
            _CORE_INDEX_CACHE.pop(_CORE_INDEX_ORDER.pop(0), None)
    return index


def extend_alternates_from_catalog(entries: list[tuple[dict, str]], items: list[dict], uhd: str = "backup",
                                   excluded: set | frozenset = frozenset(),
                                   categories: dict[str, str] | None = None) -> list[tuple[dict, str]]:
    """Complète les versions de secours avec tout le catalogue, même les catégories non cochées.

    Option de playlist ``failover_any_category`` : les chaînes trouvées restent invisibles (aucune
    catégorie, aucune entrée dans la playlist, le guide ni l'export) — elles ne servent que de secours
    si toutes les versions sélectionnées de la chaîne sont indisponibles. Nécessite ``dedupe`` et ``failover``.
    """
    index = _core_index(items)
    keys = {item["_regional_group"] for item, _ in entries if "_regional_group" in item}
    regional_index: dict[tuple[str, str], list[tuple[float, str]]] = {}
    if keys:
        categories = categories or {}
        for item in items:
            key = regional_group_key(item["name"], categories.get(item["category_id"], ""))
            if key in keys and not is_separator(item["name"]):
                regional_index.setdefault(key, []).append((quality(item["name"]), item["stream_id"]))
        for versions in regional_index.values():
            versions.sort(key=lambda v: -v[0])
    result = []
    for item, group in entries:
        existing = list(item.get("alternates") or [])
        core = match_name(item["name"]) or base_name(item["name"])
        seen = {item["stream_id"], *existing}
        # Chaînes exclues de la playlist : jamais reprises comme secours. Versions 4K : selon ``uhd``.
        key = item.get("_regional_group")
        source = regional_index.get(key, []) if key else index.get(core, [])
        candidates = [(q, sid) for q, sid in source if sid not in seen and sid not in excluded
                      and not (uhd == "never" and q == UHD_QUALITY)]
        if uhd == "backup":
            candidates.sort(key=lambda v: -(0.5 if v[0] == UHD_QUALITY else v[0]))
        extra = [sid for _, sid in candidates]
        if extra:
            item = {**item, "alternates": (existing + extra)[:MAX_ALTERNATES]}
        result.append((item, group))
    return result


def apply_hq_logos(entries: list[tuple[dict, str]], categories: dict[str, str]) -> list[tuple[dict, str]]:
    """Logos haute qualité (option ``hq_logos``) : ``logo_hq`` = chemin local ``/logo/<clé>.png``.

    Le logo du fournisseur reste dans ``logo`` (guide XMLTV, repli) ; :func:`display_logo` choisit.
    Les images manquantes sont téléchargées en arrière-plan.
    """
    from . import logos as logo_lib
    library = logo_lib.current()
    if library is None:
        return entries
    library.maybe_refresh()
    result, keys = [], []
    for item, group in entries:
        key = library.match(item["name"], categories.get(item.get("category_id", ""), ""))
        if key:
            item = {**item, "logo_hq": f"/logo/{key}.png"}
            keys.append(key)
        result.append((item, group))
    if keys:
        library.prefetch(keys)
    return result


def display_logo(item: dict, base: str = "") -> str:
    """Logo à montrer : logo haute qualité local (adresse absolue avec ``base``) sinon celui du fournisseur."""
    return base + item["logo_hq"] if item.get("logo_hq") else item.get("logo", "")


def item_key(item: dict) -> str:
    kind = item.get("kind", "live")
    if kind == "movie":
        return "vod:" + item["stream_id"]
    if kind == "series":
        return "series:" + item["stream_id"]
    if kind == "episode":
        return "series:" + item.get("series_id", "")
    return item["stream_id"]


def playlist_providers(saved: dict) -> set[str] | None:
    """Abonnements utilisés par une playlist (``None`` = tous).

    ``provider`` = abonnement du profil (vide = 1, playlists d'avant les profils : en mélange) ;
    ``mix`` = playlist « mélange » : son abonnement + les abonnements « mélangeables ».
    """
    from . import settings
    provider = str(saved.get("provider") or "1")
    if saved.get("mix") or not saved.get("provider"):
        mixable = settings.mixable()
        return None if mixable is None else {provider} | mixable
    return {provider}


def select_items(api, saved: dict, kind: str, *, refresh: bool = False) -> tuple[list[tuple[dict, str]], dict[str, str]]:
    """Éléments d'un type (direct, films ou séries) retenus par la playlist, personnalisations comprises.

    Retourne ``(entrées [(élément, clé_de_groupe)], {clé_de_groupe: nom d'origine})``.
    """
    items, categories = api.catalog(refresh=refresh, kind=kind)
    allowed = playlist_providers(saved)
    if allowed is not None and len(getattr(api, "apis", ())) > 1:
        # Profils : une playlist n'utilise que l'abonnement de son profil (jamais celui d'un autre membre),
        # plus — playlist « mélange » — les abonnements cochés « peut être mélangé aux autres ».
        items = [item for item in items if api.provider_of(item["stream_id"]) in allowed]
        categories = {cid: name for cid, name in categories.items()
                      if cid == "none" or api.provider_of(cid) in allowed}
    if saved.get("hide_adult"):
        items = [item for item in items if not is_adult_item(item, categories)]
        categories = {cid: name for cid, name in categories.items() if not is_adult_category(name)}
    prefix = GROUP_PREFIX[kind]
    cats_field = SELECTION_FIELDS[kind][0]
    effective = saved
    rules = [r for r in saved.get("rules", []) if r["kind"] == kind]
    if rules:
        extra = [cid for cid, name in categories.items() if any(rule_matches(name, r) for r in rules)]
        effective = {**saved, cats_field: list(dict.fromkeys(list(saved.get(cats_field, [])) + extra))}
    entries = filter_kind(items, effective, kind, prefix)
    if kind == "live" and saved.get("hide_separators"):
        entries = [entry for entry in entries if not is_separator(entry[0]["name"])]
    if kind == "live":
        entries = apply_custom_groups(entries, saved, categories)
        entries = sanitize_epg_ids(entries)
    if kind == "live" and saved.get("dedupe"):
        uhd = saved.get("uhd") if saved.get("uhd") in UHD_MODES else "backup"
        entries = dedupe(entries, uhd)
        if saved.get("failover") and saved.get("failover_any_category"):
            entries = extend_alternates_from_catalog(entries, items, uhd,
                                                     set(saved.get(SELECTION_FIELDS["live"][2]) or ()), categories)
    if kind == "live" and saved.get("item_pinned"):
        pinned = saved["item_pinned"]
        updated = []
        for item, group in entries:
            target = pinned.get(item["stream_id"])
            alts = list(item.get("alternates") or [])
            if target and target in alts:
                # Garde stream_id canonique intact (EPG, favoris, numéros, TiviMate) mais passe la version épinglée en n° 1.
                item = {**item, "pinned_stream": target, "alternates": [item["stream_id"], *(a for a in alts if a != target)]}
            updated.append((item, group))
        entries = updated
    if kind == "live" and saved.get("hq_logos", True):
        entries = apply_hq_logos(entries, categories)
    if kind == "live" and saved.get("clean_names"):
        entries = [({**item, "name": item.get("clean_name") or clean_channel_name(item["name"])}, group)
                   for item, group in entries]
    names = saved.get("item_names") or {}
    numbers = saved.get("item_numbers") or {} if kind == "live" else {}
    logos = saved.get("item_logos") or {}
    epg_from = saved.get("item_epg") or {} if kind == "live" else {}
    xmltv_ids = saved.get("item_xmltv") or {} if kind == "live" else {}
    if names or numbers or logos or epg_from or xmltv_ids:
        lookup = getattr(api, "lookup", None)
        customized = []
        for item, group in entries:
            key = prefix + item["stream_id"]
            sid = item["stream_id"]
            if key in names or sid in numbers or key in logos or sid in epg_from or sid in xmltv_ids:
                item = dict(item)
                if key in names:
                    item["name"] = names[key]
                if sid in numbers:
                    item["num"] = str(numbers[sid])
                    item["custom_num"] = True
                if key in logos:
                    item["logo"] = logos[key]
                    item.pop("logo_hq", None)  # logo choisi à la main : prioritaire
                if sid in epg_from:
                    # Guide emprunté à une autre chaîne : son identifiant EPG (xmltv) et sa source API.
                    item["epg_from"] = epg_from[sid]
                    source = lookup("live", epg_from[sid]) if lookup else None
                    if source and source.get("epg_id"):
                        item["epg_id"] = source["epg_id"]
                if sid in xmltv_ids:
                    # Guide choisi à la main (suggestion validée) : identifiant XMLTV prioritaire, quelle
                    # que soit sa source (guide du fournisseur ou guide externe).
                    item["epg_id"] = xmltv_ids[sid]
                    item["epg_manual"] = True
                    item.pop("epg_prefer_external", None)
            customized.append((item, group))
        entries = customized
    groups = {prefix + cid: name for cid, name in categories.items()}
    if kind == "live" and saved.get("custom_groups"):
        # Groupes personnalisés en premier (ordre par défaut, avant ceux du fournisseur).
        groups = {**{CUSTOM_PREFIX + g["id"]: g["name"] for g in ordered_custom_groups(saved)}, **groups}
    return entries, groups


def add_favorites(entries: list[tuple[dict, str]], groups: dict[str, str], saved: dict) -> list[tuple[dict, str]]:
    """Ajoute en tête un groupe « ★ Favoris » (les favoris restent aussi dans leur groupe)."""
    favorites = saved.get("favorites") or []
    if not favorites:
        return entries
    rank = {key: i for i, key in enumerate(favorites)}
    fav = [(item, FAVORITES_GROUP) for item, _ in entries if item_key(item) in rank]
    fav.sort(key=lambda entry: rank[item_key(entry[0])])  # tri stable : épisodes dans l'ordre
    if fav:
        groups[FAVORITES_GROUP] = saved.get("favorites_name") or "★ Favoris"
    return fav + entries
