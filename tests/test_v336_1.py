"""Tests v3.36.1 — nettoyage Mode Décodeur, rapprochement groupes/alias et protection EPG sur catalogues réels."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import builder
from app.custom import validate_playlist
from app.epg import DEFAULT_EPG_GUIDES, EpgCache, read_schedule, xml_channel_id, xmltv_time

from .fake_xtream import EPG_END, EPG_START

START, STOP = xmltv_time(EPG_START), xmltv_time(EPG_END)


def ch(sid, name, cat="1", epg_id=""):
    return {
        "stream_id": str(sid),
        "name": name,
        "logo": "",
        "epg_id": epg_id,
        "num": "",
        "category_id": str(cat),
        "category_ids": [str(cat)],
        "archive": 0,
    }


class StubApi:
    def __init__(self, items, categories):
        self._items = items
        self._categories = categories

    def catalog(self, refresh=False, kind="live"):
        return list(self._items), dict(self._categories)


class CleaningAndMatchingTests(unittest.TestCase):
    def test_clean_channel_name_handles_real_provider_noise(self):
        cases = [
            ("AR: BEIN SP⚽RTS 1 ᴴᴰ", "BEIN SPORTS 1"),
            ("VIP: EURO SPORT 1 ꜰʜᴅ", "EURO SPORT 1"),
            ("BE-VIP: LA UNE (13)", "LA UNE"),
            ("BE: BE VRT 1 ᴴᴰ", "VRT 1"),
            ("|4K| FR| ARTE UHD", "ARTE"),
            ("|BEIN FR| BEIN SPORTS 1HD", "BEIN SPORTS 1"),
            ("FR: CANAL+ CINEMA(S) FHD/SD", "CANAL+ CINEMA(S)"),
            ("FR: 13.EME RUE (LIVE EVENT ONLY)", "13.EME RUE"),
            ("FR: SCIENCE & VIE TV [MULTI-AUDIO]", "SCIENCE & VIE TV"),
            ("TV: ABC7 NEW YORK ᶜᶦᵗʸ ᴿᴬᵂ", "ABC7 NEW YORK city"),
            ("FR: TF1 ⁸ᴷ ³⁸⁴⁰ᵖ ⁽ᴮᴷ⁾", "TF1"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(builder.clean_channel_name(raw), expected)

    def test_quality_recognizes_smallcaps_and_8k_superscripts(self):
        self.assertEqual(builder.quality("VIP: TF1 ꜰʜᴅ"), 3.0)
        self.assertEqual(builder.quality("FR: ARTE ⁸ᴷ"), 4.0)
        self.assertEqual(builder.quality("FR: CANAL+ ³⁸⁴⁰ᵖ"), 4.0)
        self.assertEqual(builder.quality("FR: M6 ᴴᴰ"), 2.0)

    def test_match_name_canonical_aliases_and_bein_ar_isolation(self):
        self.assertEqual(builder.match_name("VIP: EURO SPORT 1 ꜰʜᴅ"), "eurosport 1")
        self.assertEqual(builder.match_name("|FR| EUROSPORT 1 HD"), "eurosport 1")
        self.assertEqual(builder.match_name("FR: 13.EME RUE (LIVE EVENT ONLY)"), "13eme rue")
        self.assertEqual(builder.match_name("FR: CANAL+ CINEMA(S) FHD/SD"), "canal+ cinema")
        self.assertEqual(builder.match_name("FR: AUTOMOTO LA CHAINE ᴴᴰ"), "automoto")
        self.assertEqual(builder.match_name("FR: EQUIDIA LIVE ᴴᴰ"), "equidia")
        self.assertEqual(builder.match_name("BE-VIP: LA UNE (13)"), "la une")
        self.assertEqual(builder.match_name("|BEIN FR| BEIN SPORTS 1HD"), "bein sports 1")
        # |BEIN AR| ne doit jamais fusionner avec le « bein sports 1 » français hors groupe régional.
        self.assertNotEqual(builder.match_name("|BEIN AR| BEIN SPORTS 1 HD"), "bein sports 1")

    def test_regional_group_key_fixes_uhd_arrabiaa_and_ad_sports_premium(self):
        self.assertEqual(builder.regional_group_key("UHD: BEIN SPORTS 1 ᵁᴴᴰ", "FR| SPORT"), ("fr", "bein sports 1"))
        self.assertEqual(builder.regional_group_key("AR: BEIN SP⚽RTS 01 ᴴᴰ", "AR| BEIN SPORTS"), ("ar", "bein sports 1"))
        self.assertEqual(builder.regional_group_key("|BEIN AR| BEIN SPORTS 1 HD", "BEIN SPORTS | OTT"), ("ar", "bein sports 1"))
        self.assertEqual(builder.regional_group_key("AR: ARRABIAA ᴿᴬᵂ", "AR| MOROCCO"), ("ar", "athaqafia"))
        self.assertEqual(builder.regional_group_key("AR: AD SPORTS 1 PREMIUM ᴴᴰ", "AR| SPORT"), ("ar", "ad sports premium 1"))
        self.assertEqual(builder.regional_group_key("FR: AUTOMOTO LA CHAINE ᴴᴰ", "FR| SPORT"), ("fr", "automoto"))
        self.assertEqual(builder.regional_group_key("FR: EQUIDIA LIVE ᴴᴰ", "FR| SPORT"), ("fr", "equidia"))

    def test_js_python_parity_for_match_and_clean(self):
        if shutil.which("node") is None:
            self.skipTest("node absent")
        node_code = r"""
const fs = require('fs');
const src = fs.readFileSync('app/static/app.js', 'utf8');
const norm = (text) => String(text || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const m1 = src.indexOf('const EMPTY_SET = new Set();');
const m2 = src.indexOf('/** Groupes perso par priorité');
const m3 = src.indexOf('const CLEAN_PREFIX =');
const m4 = src.indexOf('function ruleMatches(');
const block = src.slice(m1, m2) + '\n' + src.slice(m3, m4);
const fn = new Function('norm', 'state', '$', block + '\nreturn { matchName, cleanChannelName };');
const env = fn(norm, { kind: 'live', cat: { live: { categoryNames: new Map() } } }, () => ({ checked: false }));
const samples = JSON.parse(process.argv[1]);
console.log(JSON.stringify(samples.map((s) => [s, env.matchName(s), env.cleanChannelName(s)])));
"""
        samples = [
            "VIP: EURO SPORT 1 ꜰʜᴅ",
            "AR: BEIN SP⚽RTS 1 ᴴᴰ",
            "BE-VIP: LA UNE (13)",
            "BE: BE VRT 1 ᴴᴰ",
            "|4K| FR| ARTE UHD",
            "|BEIN FR| BEIN SPORTS 1HD",
            "|BEIN AR| BEIN SPORTS 1 HD",
            "FR: CANAL+ CINEMA(S) FHD/SD",
            "FR: 13.EME RUE (LIVE EVENT ONLY)",
            "TV: ABC7 NEW YORK ᶜᶦᵗʸ ᴿᴬᵂ",
            "FR: SCIENCE & VIE TV [MULTI-AUDIO]",
            "FR: AUTOMOTO LA CHAINE ᴴᴰ",
        ]
        root = Path(__file__).resolve().parents[1]
        out = subprocess.check_output(["node", "-e", node_code, json.dumps(samples)], cwd=root, text=True)
        for raw, js_match, js_clean in json.loads(out):
            with self.subTest(raw=raw):
                self.assertEqual(js_match, builder.match_name(raw))
                self.assertEqual(js_clean, builder.clean_channel_name(raw))


class EpgSanitizationAndMatchingTests(unittest.TestCase):
    def test_garbage_and_poisoned_epg_ids_sanitized_while_real_channels_kept(self):
        self.assertFalse(builder.is_usable_epg_id(""))
        self.assertFalse(builder.is_usable_epg_id("TS"))
        self.assertFalse(builder.is_usable_epg_id("0"))
        self.assertTrue(builder.is_usable_epg_id("tf1.fr"))

        items = [
            ch(1, "FR: 01TV HD", "1", "01tv.fr"),
            ch(2, "|BEIN FR| BEIN SPORTS 1 HD", "1", "01tv.fr"),
            ch(3, "|BEIN FR| BEIN SPORTS 2 HD", "1", "01tv.fr"),
            ch(4, "|BEIN FR| BEIN SPORTS 3 HD", "1", "01tv.fr"),
            ch(5, "AR: 2M Monde", "1", "TS"),
            ch(6, "NL: 100% NL TV", "1", "100nltv.nl"),
            ch(7, "AR: BEIN SPORTS 4", "1", "100nltv.nl"),
            ch(8, "AR: BEIN SPORTS 5", "1", "100nltv.nl"),
            ch(9, "AR: BEIN SPORTS 6", "1", "100nltv.nl"),
        ]
        saved = validate_playlist({"slug": "p", "name": "P", "category_ids": ["1"]})
        entries, _ = builder.select_items(StubApi(items, {"1": "Mix"}), saved, "live")
        by_id = {item["stream_id"]: item["epg_id"] for item, _ in entries}
        self.assertEqual(by_id["1"], "01tv.fr")      # vraie chaîne 01TV conservée
        self.assertEqual(by_id["2"], "")             # 01tv.fr empoisonné retiré de beIN Sports 1..3
        self.assertEqual(by_id["3"], "")
        self.assertEqual(by_id["4"], "")
        self.assertEqual(by_id["5"], "")             # « TS » bidon retiré
        self.assertEqual(by_id["6"], "100nltv.nl")   # vraie chaîne 100% NL TV conservée
        self.assertEqual(by_id["7"], "")             # 100nltv.nl empoisonné retiré de beIN 4..6

    def test_dedupe_inherits_valid_epg_id_from_merged_duplicate(self):
        items = [
            ch(1, "AR: Al Aoula Inter ᴴᴰ", "1", "TS"),
            ch(2, "AR: Al Aoula Inter SD", "1", "AlAoula.ma"),
            ch(3, "|BEIN FR| BEIN SPORTS 1 FHD", "1", "01tv.fr"),
            ch(4, "|BEIN FR| BEIN SPORTS 1 HD", "1", "beinsports1.fr"),
            ch(5, "|BEIN FR| BEIN SPORTS 2 HD", "1", "01tv.fr"),
            ch(6, "|BEIN FR| BEIN SPORTS 3 HD", "1", "01tv.fr"),
        ]
        saved = validate_playlist({"slug": "p", "name": "P", "category_ids": ["1"], "dedupe": True})
        entries, _ = builder.select_items(StubApi(items, {"1": "Mix"}), saved, "live")
        by_id = {item["stream_id"]: item for item, _ in entries}
        self.assertEqual(by_id["1"]["epg_id"], "AlAoula.ma")
        self.assertEqual(by_id["3"]["epg_id"], "beinsports1.fr")

    def test_external_guide_overrides_mismatched_provider_epg_id_and_matches_trailing_tv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "provider.xml").write_text(
                f'<tv><channel id="01tv.fr"><display-name>01TV</display-name></channel>'
                f'<programme channel="01tv.fr" start="{START}" stop="{STOP}"><title>Tech 01TV</title></programme></tv>',
                encoding="utf-8",
            )
            (root / "extra.xml").write_text(
                f'<tv>'
                f'<channel id="01tv.fr"><display-name>01TV</display-name></channel>'
                f'<channel id="beinsports1.fr"><display-name>beIN Sports 1</display-name></channel>'
                f'<channel id="histoiretv.fr"><display-name>Histoire TV</display-name></channel>'
                f'<programme channel="01tv.fr" start="{START}" stop="{STOP}"><title>Tech 01TV</title></programme>'
                f'<programme channel="beinsports1.fr" start="{START}" stop="{STOP}"><title>Ligue des Champions</title></programme>'
                f'<programme channel="histoiretv.fr" start="{START}" stop="{STOP}"><title>Documentaire Histoire</title></programme>'
                f'</tv>',
                encoding="utf-8",
            )
            epg = EpgCache((root / "provider.xml").as_uri, str(root / "p"))
            epg.mode = "xmltv"
            epg.extras = [EpgCache((root / "extra.xml").as_uri, str(root / "ext"))]

            # Même sur 1 seule chaîne beIN Sports 1 dotée par erreur de « 01tv.fr », le guide externe résout beinsports1.fr.
            channels = [
                {**ch(10, "|BEIN FR| BEIN SPORTS 1 HD", "1", "01tv.fr"), "epg_prefer_external": True},
                {**ch(11, "FR: Histoire FHD", "1", "TS"), "epg_prefer_external": True},
            ]
            path, stats = epg.playlist_guide("test", channels)
            self.assertEqual(stats["sources"]["10"], {"source": "external:1", "id": "beinsports1.fr"})
            self.assertEqual(stats["sources"]["11"], {"source": "external:1", "id": "histoiretv.fr"})
            self.assertEqual(xml_channel_id(channels[1]), "xtream-11")
            schedule = read_schedule(path)
            self.assertEqual(schedule["xtream-11"][0][2], "Documentaire Histoire")

    def test_moroccan_default_guide_covers_2m_national_typo(self):
        arabic = DEFAULT_EPG_GUIDES["https://raw.githubusercontent.com/iimnm/arabic-epg/output/guide.xml.gz"]
        self.assertEqual(arabic["2mnatioanl"], "2mmonde.ma@sd")
        self.assertEqual(arabic["2mnational"], "2mmonde.ma@sd")
        morocco = DEFAULT_EPG_GUIDES["https://www.open-epg.com/files/morocco.xml.gz"]
        self.assertEqual(morocco["alaoulamaroc"], "alaoulaar.ma")


class BelgiquePresetTests(unittest.TestCase):
    """Le modèle « Belgique » couvre la liste belge francophone + Sports + Régionales + Radio Vision + Luxembourg."""

    def _patterns(self) -> list[str]:
        if shutil.which("node") is None:
            self.skipTest("node absent")
        node_code = r"""
const fs = require('fs');
const src = fs.readFileSync('app/static/app.js', 'utf8');
const norm = (text) => String(text || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const m1 = src.indexOf('const EMPTY_SET = new Set();');
const m2 = src.indexOf('/** Groupes perso par priorité');
const fn = new Function('norm', 'state', '$', src.slice(m1, m2) + '\nreturn BELGIQUE;');
console.log(JSON.stringify(fn(norm, { kind: 'live', cat: { live: { categoryNames: new Map() } } }, () => ({ checked: false }))));
"""
        root = Path(__file__).resolve().parents[1]
        return json.loads(subprocess.check_output(["node", "-e", node_code], cwd=root, text=True))

    def test_exact_belgium_and_luxembourg_list_order_and_cross_provider_aliases(self):
        group = builder.ordered_custom_groups(
            {"custom_groups": [{"id": "be", "name": "Belgique", "match": "exact", "patterns": self._patterns()}]}
        )[0]
        provider_list = [
            "|BE| LA UNE FHD", "|BE| TIPIK FHD", "|BE| LA TROIS FHD", "|BE| RTL-TVI FHD",
            "|BE| CLUB-RTL FHD", "|BE| PLUG-RTL FHD", "|BE| RTL DISTRICT FHD", "|BE| RTL PLAY FHD",
            "|BE| RTL PLAY 2 FHD", "|BE| ARTE BELGIQUE FHD", "|BE| LN24 FHD", "|BE| CANAL Z",
            "|BE| AB1 FHD", "|BE| AB3 FHD", "|BE| ABXPLORE FHD", "|BE| DOBBIT TV HD (FR)",
            "|BE| BE 1 FHD", "|BE| BE CINE FHD", "|BE| BE SERIES FHD",
            "|BE| DAZN CHANNEL 1 FHD (FR)", "|BE| DAZN CHANNEL 2 FHD (FR)",
            "|BE| DAZN PRO LEAGUE 1 FHD (FR)", "|BE| DAZN PRO LEAGUE 2 FHD (FR)",
            "|BE| DAZN 1 FHD (FR)", "|BE| DAZN 2 FHD (FR)", "|BE| DAZN 3 FHD (FR)",
            "|BE| DAZN 4 FHD (FR)", "|BE| DAZN 5 FHD (FR)",
            "|BE| PLAY SPORTS 1 FHD (FR)", "|BE| PLAY SPORTS 2 FHD (FR)",
            "|BE| VOO SPORT WORLD 1 FHD", "|BE| VOO SPORT WORLD 2 FHD", "|BE| VOO SPORT WORLD 3 FHD",
            "|BE| DIVERTISSEZ-VOO HD",
            "|BE| ANTENNE CENTRE TV", "|BE| BX1", "|BE| BOUKÉ", "|BE| CANAL ZOOM",
            "|BE| MA TELE", "|BE| NO TELE", "|BE| QU4TRE (RTC LIEGE)", "|BE| TELE SAMBRE",
            "|BE| TELE MB", "|BE| TV COM", "|BE| TV LUX", "|BE| VEDIA",
            "|BE| FUN RADIO VISION HD", "|BE| RADIO CONTACT VISION", "|BE| BEL-RTL VISION",
            "|BE| TIPIK VISION", "|BE| ARABEL VISION", "|BE| NRJ LIVE TV",
            "|LU| RTL LUX", "|LU| RTL 2 LUX", "|LU| ELDO TV", "|LU| CHAMBER TV",
        ]
        ranks = []
        for name in provider_list:
            rank = builder.group_matches(builder.match_name(name), group)
            self.assertIsNotNone(rank, name)
            ranks.append(rank)
        self.assertEqual(ranks, sorted(ranks), "ordre exact Belgique/Luxembourg conservé")

        # Variantes d'autres fournisseurs (P1/P2) reconnues et fusionnées via alias canoniques :
        second_provider = [
            "BE: MATELE ◉", "BE: NOTELE ◉", "BE: TELESAMBRE 4K ◉", "BE: TELEMB 4K",
            "BE: RTC TELE LIEGE 4K ◉", "BE: PLAY SPORTS 1 (WALLONIE)", "BE: PLAY SPORTS 2 (WALLONIE)",
            "BE: DAZN PRO LEAGUE 1 FR", "BE| DAZN PRO LEAGUES 1", "BE| DAZN SPORTS 1 FHD [FR]",
            "BE-VIP: RTL CLUB (13)", "BE-VIP: RTL PLUG (13)", "BE: BOUKE 4K ◉", "BE: BE 1 4K ◉",
        ]
        for name in second_provider:
            with self.subTest(name=name):
                self.assertIsNotNone(builder.group_matches(builder.match_name(name), group), name)

        # Les chaînes flamandes / NL ne polluent pas le groupe Belgique francophone :
        for excluded in ("BE: VTM 2 HD", "BE-NL| EEN HD", "BE-NL| VRT CANVAS HD", "BE-NL| PLAY SPORTS 1 FHD"):
            self.assertIsNone(builder.group_matches(builder.match_name(excluded), group), excluded)

    def test_preset_click_replaces_existing_group_in_place(self):
        if shutil.which("node") is None:
            self.skipTest("node absent")
        node_code = r"""
const fs = require('fs');
const src = fs.readFileSync('app/static/app.js', 'utf8');
const norm = (text) => String(text || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const m1 = src.indexOf('const EMPTY_SET = new Set();');
const m2 = src.indexOf('/** Groupes perso par priorité');
const c1 = src.indexOf('async function createGroup(');
const c2 = src.indexOf('\n  function renderCustomGroups(');
const block = src.slice(m1, m2) + '\n' + src.slice(c1, c2);
const toasts = [];
let refreshed = 0;
const state = {
  kind: 'live',
  customGroups: [
    { id: 'tnt', name: 'TNT', match: 'exact', patterns: ['TF1'] },
    { id: 'ott', name: 'OTT', match: 'exact', patterns: ['TF1', 'Peppa Pig', 'BFM TV'] },
    { id: 'maroc', name: 'Maroc', match: 'exact', region: 'ar', patterns: ['Al Aoula'] },
  ],
  groupOrder: ['g:tnt', 'g:ott', 'g:maroc'],
  sel: { live: { included: new Set(), excluded: new Set() } },
  cat: { live: { categoryNames: new Map(), channels: [] } },
  groupCache: 'dirty',
};
const env = new Function(
  'norm', 'state', '$', 'ask', 'toast', 'refresh', 'plural', 'slugify', 'ensureCatalog', 'includeRegionalChannels', 'orderedCustomGroups',
  block + '\nreturn { PRESETS, createGroup };'
)(
  norm, state, () => ({ checked: false, value: 'backup' }),
  async () => '', (msg) => toasts.push(msg), () => { refreshed += 1; },
  (n, s, p) => `${n} ${n > 1 ? p : s}`, (s) => s.toLowerCase(),
  async () => true, () => {}, () => state.customGroups
);
(async () => {
  const ottPreset = env.PRESETS.find((p) => p.name === 'OTT');
  await env.createGroup(ottPreset);
  console.log(JSON.stringify({
    ids: state.customGroups.map((g) => g.id),
    groupOrder: state.groupOrder,
    ottLen: state.customGroups[1].patterns.length,
    hasPeppa: state.customGroups[1].patterns.includes('Peppa Pig'),
    refreshed,
    toast: toasts[0] || '',
  }));
})();
"""
        root = Path(__file__).resolve().parents[1]
        data = json.loads(subprocess.check_output(["node", "-e", node_code], cwd=root, text=True))
        self.assertEqual(data["ids"], ["tnt", "ott", "maroc"])
        self.assertEqual(data["groupOrder"], ["g:tnt", "g:ott", "g:maroc"])
        self.assertEqual(data["ottLen"], 112)
        self.assertFalse(data["hasPeppa"])
        self.assertEqual(data["refreshed"], 1)
        self.assertIn("remplacé à la même place", data["toast"])


if __name__ == "__main__":
    unittest.main()
