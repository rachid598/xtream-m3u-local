// Test de bout en bout de l'interface Web dans Chromium (Playwright pour Node, facultatif).
//
// Non lancé par `python -m unittest` (nécessite Node + Playwright + Chromium). Usage :
//   python -m tools.demo --data /tmp/demo --port 8777 --web-port 8778 &
//   node tests/e2e_browser.mjs http://127.0.0.1:8778 [dossier_captures]
//
// Scénario : création « Salon » (2 catégories), exclusion d'une chaîne, ajout manuel,
// enregistrement, vérification du M3U via l'URL affichée, modification (même URL),
// duplication, suppression ; puis mise en page mobile/tablette (aucun défilement horizontal).
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium, expect } = (() => {
  try { return require('@playwright/test'); } catch (_) { return { ...require('playwright'), expect: null }; }
})();

const base = (process.argv[2] || 'http://127.0.0.1:8778').replace(/\/$/, '');
const shots = process.argv[3] || null;
if (shots) mkdirSync(shots, { recursive: true });

function assert(cond, msg) { if (!cond) throw new Error(`Échec : ${msg}`); }
const num = (t) => Number(String(t).replace(/[\s  ]/g, ''));
async function waitFor(fn, msg, timeout = 10000) {
  const end = Date.now() + timeout;
  for (;;) {
    try { if (await fn()) return; } catch (_) { /* retry */ }
    if (Date.now() > end) throw new Error(`Délai dépassé : ${msg}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}
const fetchText = async (url) => { const r = await fetch(url); return { status: r.status, text: await r.text() }; };

const errors = [];
const catBox = (page, name) => page.getByRole('checkbox', { name: `Inclure la catégorie ${name}`, exact: true });
const catCard = (page, name) => page.locator('#categories .cat').filter({ has: catBox(page, name) });
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
  page.on('console', (m) => { if (m.type() === 'error' && !/Failed to load resource|ERR_NAME_NOT_RESOLVED/.test(m.text())) errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(`${base}/`);
  await waitFor(async () => (await page.getAttribute('#connection', 'class')) === 'pill ok', 'connexion catalogue');
  // Éditeur en 4 parties : Contenu (onglets 1-2-3), Organisation, Options, Guide.
  const showSection = async (name) => { await page.click(`#sec-btn-${name}`); await waitFor(() => page.isVisible(`#sec-${name}`), `partie ${name}`); };
  const showTab = async (tab) => { await showSection('content'); await page.click(`#tab-${tab}`); };

  // Adresse EPG « à la Xtream » de la playlist complète
  const fullEpg = await page.innerText('#full-epg-url');
  assert(fullEpg.endsWith('/xmltv.php'), `URL EPG complète ${fullEpg}`);
  const xmltv = await fetchText(fullEpg);
  assert(xmltv.status === 200 && xmltv.text.includes('<programme'), '/xmltv.php répond avec des programmes');

  // Création
  await page.fill('#name', 'Salon');
  assert(await page.inputValue('#slug') === 'salon', 'slug automatique');
  await page.fill('#cat-search', 'fr |');
  await catBox(page, 'FR | Information').check();
  await catBox(page, 'FR | Sport').check();
  assert(await page.isVisible('#dirty'), 'badge modifications non enregistrées');
  assert(num(await page.innerText('#stat-channels')) > 0, 'compteur de chaînes');

  // Exclusion d'une chaîne de « FR | Sport » + ajout manuel hors catégories
  await catCard(page, 'FR | Sport').locator('.view').click();
  assert(await page.isVisible('#panel-channels'), 'onglet chaînes');
  const first = page.locator('#channels .channel').first();
  const excludedName = await first.locator('.ch-name').innerText();
  await first.locator('input').uncheck();
  await waitFor(async () => (await first.locator('.badge.danger').innerText()) === 'Exclue', 'badge Exclue');
  await page.selectOption('#ch-category', '');
  await page.selectOption('#ch-state', 'off');
  const added = page.locator('#channels .channel').first();
  const addedName = await added.locator('.ch-name').innerText();
  await added.locator('input').click();
  await page.selectOption('#ch-state', 'included');
  await waitFor(async () => (await page.locator('#channels .channel').count()) === 1, 'filtre ajoutées');
  await page.fill('#search', addedName.split(' ').slice(0, 2).join(' '));
  await page.waitForTimeout(300);
  await page.selectOption('#ch-state', 'all');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-channels.png') });

  await showTab('summary');
  assert(await page.locator('#sum-excluded li').count() === 1, '1 exclusion');
  assert(await page.locator('#sum-included li').count() === 1, '1 ajout');
  // Ordre et noms de groupes propres à la playlist (partie Organisation)
  await showSection('organize');
  const rows = page.locator('#sum-groups .group-row');
  assert(await rows.count() >= 3, 'au moins 3 groupes');
  const secondPlaceholder = await rows.nth(1).locator('input').getAttribute('placeholder');
  await rows.nth(1).locator('.icon-btn').first().click(); // monter le 2e groupe
  assert(await rows.nth(0).locator('input').getAttribute('placeholder') === secondPlaceholder, 'groupe remonté');
  await rows.nth(0).locator('input').fill('Mon groupe favori');

  await page.click('#save');
  await waitFor(async () => (await page.getAttribute('#url-box', 'class')).includes('ready'), 'URL prête');
  const url = await page.innerText('#url');
  assert(url.endsWith('/p/salon.m3u'), `URL ${url}`);
  assert(await page.locator('#saved .playlist-item').count() === 1, 'liste des playlists');
  assert(await page.isDisabled('#slug'), 'slug figé');
  // Guide des programmes (EPG) de la playlist
  await showSection('guide');
  assert(await page.isVisible('#epg-content'), 'bloc EPG visible');
  await waitFor(async () => /%/.test(await page.innerText('#epg-pill-text')) && !/en cours/.test(await page.innerText('#epg-pill-text')), 'couverture EPG', 20000);
  assert((await page.innerText('#epg-url')).endsWith('/p/salon.xml'), 'URL EPG');
  assert(/^\d+ \/ \d+$/.test(await page.innerText('#epg-covered')), 'compteur EPG');
  assert(num(await page.innerText('#epg-api')) > 0, 'chaînes complétées par l\'API');
  assert(/xmltv\.php/.test(await page.innerText('#epg-source')), 'source EPG décrite');
  const { text: guide } = await fetchText((await page.innerText('#epg-url')));
  assert(guide.includes('<programme') && guide.trim().endsWith('</tv>'), 'XMLTV de la playlist');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-epg.png') });
  await page.click('#epg-refresh');
  await waitFor(async () => (await page.locator('.toast', { hasText: 'Actualisation du guide' }).count()) > 0, 'actualisation EPG');
  await showSection('content');
  const { text: m3u } = await fetchText(url);
  assert(m3u.startsWith('#EXTM3U'), 'en-tête M3U');
  assert(!m3u.includes(`,${excludedName}\n`), 'chaîne exclue présente');
  assert(m3u.includes(`,${addedName}\n`), 'chaîne ajoutée absente');
  assert(m3u.split('\n')[1].includes('group-title="Mon groupe favori"'), 'groupe renommé en tête');
  assert(m3u.includes('group-title="FR | Sport"') || secondPlaceholder === 'FR | Sport', 'groupe Sport');
  assert(!m3u.includes(`group-title="${secondPlaceholder}"`), 'nom d\'origine remplacé');
  const entries = (m3u.match(/#EXTINF/g) || []).length;
  assert(entries === num(await page.innerText('#stat-channels')), `nombre d'entrées ${entries}`);
  if (shots) {
    await showTab('categories');
    await page.fill('#cat-search', '');
    await page.waitForTimeout(300);
    await page.screenshot({ path: join(shots, 'desktop-saved.png') });
  }

  // Modification : même URL, contenu différent
  await showTab('categories');
  await page.fill('#cat-search', 'FR | Sport');
  await page.waitForTimeout(300);
  await catBox(page, 'FR | Sport').uncheck();
  await page.fill('#name', 'Salon TV');
  await page.click('#save');
  await waitFor(async () => (await page.innerText('#editor-title')) === 'Modifier « Salon TV »', 'titre modifié');
  assert(await page.innerText('#url') === url, 'URL inchangée');
  const { text: m3u2 } = await fetchText(url);
  assert(!m3u2.includes('group-title="FR | Sport"') && (m3u2.match(/#EXTINF/g) || []).length < entries, 'contenu mis à jour');

  // Personnalisation : règle automatique, favori, renommage + numéro, numérotation, doublons
  await showTab('categories');
  await page.fill('#cat-search', '');
  await page.waitForTimeout(250);
  await page.click('#rules-box > summary');
  await page.selectOption('#rule-match', 'prefix');
  await page.fill('#rule-value', 'be |');
  await waitFor(async () => /BE \| Belgique/.test(await page.innerText('#rule-preview')), 'aperçu de la règle');
  await page.click('#rule-add');
  await waitFor(async () => (await page.locator('#rules-list li', { hasText: 'commence par' }).count()) === 1, 'règle ajoutée');
  assert(await catBox(page, 'BE | Belgique').isChecked() && await catBox(page, 'BE | Belgique').isDisabled(), 'catégorie cochée par la règle');
  await page.fill('#cat-search', '');
  await showTab('channels');
  await page.fill('#search', '');
  await page.waitForTimeout(250);
  await page.selectOption('#ch-category', { label: (await page.locator('#ch-category option', { hasText: 'FR | Information' }).innerText()) });
  await page.selectOption('#ch-state', 'on');
  const realRows = () => page.locator('#channels .channel').filter({ hasNotText: '#####' });
  const firstRow = realRows().first();
  const firstName = await firstRow.locator('.ch-name').innerText();
  await firstRow.locator('.item-btn', { hasText: '☆' }).click();
  await waitFor(async () => (await realRows().first().locator('.fav-on').count()) === 1, 'favori');
  await realRows().first().locator('.item-btn', { hasText: '✎' }).click();
  await page.fill('#item-name', 'Ma chaîne info');
  await page.fill('#item-number', '7');
  await page.click('#item-ok');
  await waitFor(async () => (await realRows().first().locator('.ch-name').innerText()) === 'Ma chaîne info', 'renommage');
  await page.selectOption('#ch-state', 'all');
  await page.selectOption('#ch-category', '');
  await showSection('options');
  const beforeSep = num(await page.innerText('#stat-channels'));
  await page.check('#opt-separators');
  await waitFor(async () => num(await page.innerText('#stat-channels')) < beforeSep, 'séparateurs masqués');
  await page.check('#opt-dedupe');
  await showSection('organize');
  assert(await page.locator('#sum-favorites li', { hasText: 'Ma chaîne info' }).count() === 1, 'favori dans le résumé');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement des personnalisations');
  const { text: m3uCustom } = await fetchText(url);
  const lines = m3uCustom.split('\n');
  assert(/group-title="★ Favoris",Ma chaîne info$/.test(lines[1]), `favori en tête : ${lines[1]}`);
  assert(m3uCustom.includes('tvg-chno="7"') && m3uCustom.includes('group-title="BE | Belgique"'), 'numéro et règle dans le M3U');
  assert(!m3uCustom.includes('#####'), 'séparateurs absents du M3U');
  assert(!m3uCustom.includes(`,${firstName}\n`) || firstName === 'Ma chaîne info', 'nom d\'origine remplacé');
  await page.click('#auto-number');
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.locator('#sum-custom li').count()) > 5, 'numérotation automatique');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement de la numérotation');
  const { text: numbered } = await fetchText(url);
  assert(numbered.includes('tvg-chno="1"') && numbered.includes('tvg-chno="2"'), 'numéros 1, 2… dans le M3U');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-summary.png') });

  // Mes groupes : modèle TNT, nouveau groupe en premier, rangement manuel 📁
  await showTab('categories');
  await catBox(page, 'FR | Généralistes').check();
  await showSection('organize');
  await page.locator('#cg-presets button', { hasText: '+ TNT' }).click();
  await waitFor(async () => num(await page.locator('#cg-list .cg-card').first().locator('.num').innerText()) >= 6, 'modèle TNT rempli');
  await page.click('#cg-add');
  await page.fill('#dialog-input', 'Mes infos');
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.locator('#cg-list .cg-card').first().locator('input').inputValue()) === 'Mes infos', 'nouveau groupe en premier');
  assert((await page.locator('#sum-groups .group-row').first().locator('input').inputValue()) === 'TNT', 'TNT en tête des groupes');
  await showTab('channels');
  await page.fill('#search', 'franceinfo');
  const fi = page.locator('#channels .channel').filter({ hasText: 'FR: franceinfo' }).first();
  await waitFor(async () => (await fi.count()) === 1 && (await fi.locator('.badge.grp').innerText()) === '📁 TNT', 'franceinfo rangée dans TNT');
  await fi.locator('.item-btn', { hasText: '📁' }).click();
  await page.selectOption('#dialog-select', { label: '📁 Mes infos' });
  await page.click('#dialog-ok');
  await waitFor(async () => (await fi.locator('.badge.grp').innerText()) === '📁 Mes infos', 'rangement manuel');
  await page.fill('#search', '');
  await page.waitForTimeout(300);
  await showSection('organize');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-groups.png') });
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement des groupes');
  const infos = (await fetchText(url)).text.split('\n').filter((l) => l.startsWith('#EXTINF'));
  const firstOther = infos.find((l) => !l.includes('★ Favoris'));
  assert(/group-title="Mes infos",FR: franceinfo$/.test(firstOther), `groupe perso en premier : ${firstOther}`);
  const tnt = infos.filter((l) => l.includes('group-title="TNT"')).map((l) => l.split(',').pop());
  assert(['FR: TF1 HD', 'FR: France 2 HD', 'FR: M6 HD', 'FR: BFM TV'].every((n) => tnt.includes(n))
    && tnt.indexOf('FR: TF1 HD') < tnt.indexOf('FR: France 2 HD'), `groupe TNT : ${tnt.join(' | ')}`);
  assert(infos.filter((l) => l.endsWith(',FR: franceinfo')).length === 1, 'chaîne rangée une seule fois');
  // Bascule automatique : TF1 4K (gardée) + TF1 HD (secours)
  await showTab('categories');
  await catBox(page, '4K | UHD').check();
  await showSection('options');
  await page.check('#opt-failover');
  await waitFor(async () => /Bascule auto/.test(await page.innerText('#options-pill-text')), 'option bascule automatique');
  await showSection('organize');
  await page.locator('#cg-presets button', { hasText: '+ Bouquet Canal+' }).click();
  await waitFor(async () => (await page.locator('#cg-list .cg-card').first().locator('input').inputValue()) === 'Bouquet Canal+', 'bouquet Canal+ en premier');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement du bouquet');
  const bouquet = (await fetchText(url)).text.split('\n').filter((l) => l.includes('group-title="Bouquet Canal+"'));
  assert(bouquet.some((l) => /tvg-chno="4" group-title="Bouquet Canal\+",FR: CANAL\+ HD$/.test(l))
    && /,FR: TF1 4K$/.test(bouquet[0]), `bouquet Canal+ numéroté comme le décodeur : ${bouquet.slice(0, 3).join(' | ')}`);
  const m3uLines = (await fetchText(url)).text.split('\n');
  const tf1Url = m3uLines[m3uLines.findIndex((l) => l.endsWith(',FR: TF1 4K')) + 1];
  assert(/\/stream\/800012\.ts\?alt=800001(&|$)/.test(tf1Url), `URL avec version de secours : ${tf1Url}`);
  // Chaîne virtuelle : ses versions de secours sont visibles dans la liste
  await showTab('channels');
  await page.fill('#search', 'FR: TF1');
  const tf1Row = page.locator('#channels .channel').filter({ hasText: 'FR: TF1 4K' }).first();
  await waitFor(async () => (await tf1Row.locator('.alt-list summary').count()) === 1, 'liste des secours');
  assert(/1 version de secours/.test(await tf1Row.locator('.alt-list summary').innerText()), 'nombre de secours');
  const wasOn = await tf1Row.locator('input').isChecked();
  await tf1Row.locator('.alt-list summary').click();
  assert(/FR: TF1 HD/.test(await tf1Row.locator('.alt-list ol').innerText()), 'secours listé');
  assert((await tf1Row.locator('input').isChecked()) === wasOn, 'déplier ne coche/décoche pas la chaîne');
  const hdRow = page.locator('#channels .channel').filter({ hasText: 'FR: TF1 HD' }).first();
  assert((await hdRow.locator('.badge.warn').innerText()) === 'Secours 1/1', 'badge Secours 1/1');
  await page.fill('#search', '');
  await page.waitForTimeout(300);
  await showTab('categories');

  // Catégories qui ne diffèrent que par la qualité : « Cocher aussi les catégories liées »
  await page.fill('#cat-search', 'OTT');
  await page.waitForTimeout(300);
  await catBox(page, 'FRANCE FHD | OTT').waitFor({ timeout: 10000 });
  await catBox(page, 'FRANCE FHD | OTT').check();
  const fhdCard = catCard(page, 'FRANCE FHD | OTT');
  const qlink = fhdCard.locator('.qlink');
  await waitFor(async () => (await qlink.count()) === 1, 'lien catégories liées visible');
  assert(/3 catégories liées/.test(await qlink.innerText()), `texte du lien : ${await qlink.innerText()}`);
  if (shots) await page.screenshot({ path: join(shots, 'desktop-quality-link.png') });

  // Avertissement « sans version de secours » dans Mes groupes tant que les qualités liées ne sont pas cochées
  await showSection('organize');
  await page.click('#cg-add');
  await page.fill('#dialog-input', 'Zenith');
  await page.click('#dialog-ok');
  const zenithCard = page.locator('#cg-list .cg-card').first();
  await zenithCard.locator('textarea').fill('Zenith');
  await waitFor(async () => num(await zenithCard.locator('.num').innerText()) === 1, 'groupe Zenith rempli (dédoublonné)');
  await waitFor(async () => zenithCard.locator('.warn-text').isVisible(), 'avertissement : aucun secours pour Zenith');
  assert(/sans version de secours/.test(await zenithCard.locator('.warn-text').innerText()), 'texte de l\'avertissement');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-failover-warning.png') });

  // Le lien « Cocher aussi les catégories liées » fait disparaître l'avertissement
  await showTab('categories');
  await qlink.click();
  await waitFor(async () => catBox(page, 'FRANCE HEVC | OTT').isChecked(), 'catégories liées cochées');
  assert(await catBox(page, 'FRANCE HD | OTT').isChecked() && await catBox(page, 'FRANCE SD | OTT').isChecked(), 'les 4 qualités cochées');
  assert((await fhdCard.locator('.qlink').count()) === 0, 'lien disparu une fois tout coché');
  await showSection('organize');
  await waitFor(async () => !(await zenithCard.locator('.warn-text').isVisible()), 'avertissement disparu : secours disponibles');
  assert(num(await zenithCard.locator('.num').innerText()) === 1, 'toujours 1 chaîne (dédoublonnée)');

  // Secours hors catégories cochées : décoche les qualités liées, active l'option plutôt que de les recocher
  await showTab('categories');
  await catBox(page, 'FRANCE HD | OTT').uncheck();
  await catBox(page, 'FRANCE SD | OTT').uncheck();
  await catBox(page, 'FRANCE HEVC | OTT').uncheck();
  await showSection('organize');
  await waitFor(async () => zenithCard.locator('.warn-text').isVisible(), 'avertissement de retour : qualités liées décochées');
  await showSection('options');
  assert(!(await page.isDisabled('#opt-failover-any')), 'option activable (bascule automatique déjà cochée)');
  await page.check('#opt-failover-any');
  await showSection('organize');
  await waitFor(async () => !(await zenithCard.locator('.warn-text').isVisible()), 'secours hors catégories cochées : avertissement disparu');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement du secours hors catégories cochées');
  const zenithLines = (await fetchText(url)).text.split('\n');
  const zenithUrl = zenithLines[zenithLines.findIndex((l) => l.endsWith(',Zenith FHD')) + 1];
  assert(/\/stream\/800101\.ts\?alt=800102,800104,800103(&|$)/.test(zenithUrl), `secours hors catégories cochées dans l'URL : ${zenithUrl}`);
  assert(!zenithLines.some((l) => /,Zenith (HD|SD|HEVC)$/.test(l)), 'les secours restent invisibles dans la playlist');
  await showTab('categories');
  await page.fill('#cat-search', '');
  await page.waitForTimeout(300);

  // Films et séries dans la même playlist (même URL)
  const kindBtn = (kind) => page.locator(`#view-playlists [data-kind="${kind}"]`);
  await waitFor(async () => /films disponibles/.test(await page.innerText('[data-kind-meta="vod"]')), 'tuile Films : films disponibles');
  assert(/séries disponibles/.test(await page.innerText('[data-kind-meta="series"]')), 'tuile Séries');
  assert(await page.isVisible('#vod-hint'), 'message : films et séries pas encore ajoutés');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-tiles.png') });
  await page.locator('#vod-hint button', { hasText: 'Ajouter des films' }).click();
  assert((await page.innerText('#kind-title')).startsWith('🎬 Films'), 'titre de l\'éditeur Films');
  await catBox(page, 'VOD | Action').waitFor({ timeout: 10000 });
  await catBox(page, 'VOD | Action').check();
  assert(num(await page.innerText('#view-playlists [data-kind-count="vod"]')) > 0, 'tuile Films : sélection');
  await kindBtn('series').click();
  await catBox(page, 'Séries | Drame').waitFor({ timeout: 10000 });
  await catBox(page, 'Séries | Drame').check();
  assert(num(await page.innerText('#stat-vod')) > 0 && num(await page.innerText('#stat-series')) > 0, 'compteurs films/séries');
  await showTab('channels');
  assert((await page.innerText('#tab-items-label')) === 'Séries', 'onglet Séries');
  await page.locator('#channels .link-btn').first().click();
  await waitFor(async () => /épisode/.test(await page.locator('#channels .ok-text').first().innerText()), 'épisodes affichés');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-series.png') });
  await showSection('organize');
  assert(await page.locator('#sum-groups .kind-tag.vod').count() >= 1, 'groupe films dans le résumé');
  assert(await page.locator('#sum-groups .kind-tag.series').count() >= 1, 'groupe séries dans le résumé');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement films/séries');
  const { text: m3u3 } = await fetchText(url);
  assert(/\/movie\/\d+\.(mkv|mp4)\n/.test(m3u3), 'liens films dans le M3U');
  assert(/\/series\/\d+\.mkv\n/.test(m3u3), 'liens épisodes dans le M3U');
  assert(m3u3.includes('group-title="VOD | Action"') && m3u3.includes('group-title="Séries | Drame"'), 'groupes films/séries');
  assert(/ S01 E01 - Épisode 1\n/.test(m3u3), 'nommage des épisodes');
  const { text: onlyMovies } = await fetchText(`${url}?content=vod`);
  assert(!onlyMovies.includes('/stream/') && onlyMovies.includes('/movie/'), 'filtre content=vod');
  await showTab('categories');
  await kindBtn('live').click();

  // Personnalisation avancée (✎) : logo de remplacement et guide emprunté
  await showTab('channels');
  await page.fill('#search', 'FR: France 2');
  const f2 = page.locator('#channels .channel').filter({ hasText: 'FR: France 2 HD' }).first();
  await waitFor(async () => (await f2.count()) === 1, 'France 2 trouvée');
  await f2.locator('.item-btn', { hasText: '✎' }).click();
  await page.fill('#item-logo', 'https://logos.invalid/france2.png');
  await page.fill('#item-epg', 'FR: TF1 HD (n° 800001)');
  await page.click('#item-ok');
  await page.fill('#search', '');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement logo + guide');
  const f2Line = (await fetchText(url)).text.split('\n').find((l) => l.endsWith(',FR: France 2 HD'));
  assert(f2Line && f2Line.includes('tvg-logo="https://logos.invalid/france2.png"'), `logo remplacé : ${f2Line}`);
  await showTab('categories');

  // Vue « Guide TV » : en ce moment, ce soir, recherche, aperçu
  await page.click('#view-btn-guide');
  await waitFor(async () => (await page.locator('#guide-list .guide-row').count()) > 3, 'guide : chaînes affichées', 20000);
  assert((await page.inputValue('#guide-playlist')) === 'salon', 'playlist du guide');
  const firstGuide = page.locator('#guide-list .guide-row').first();
  assert(await firstGuide.evaluate((n) => n.classList.contains('mine')), 'favoris / groupes en tête du guide');
  const withProg = page.locator('#guide-list .guide-row').filter({ has: page.locator('.g-time') }).first();
  await waitFor(async () => (await withProg.count()) === 1, 'un programme en cours', 20000);
  const progTitle = await withProg.locator('.g-prog strong').innerText();
  if (shots) await page.screenshot({ path: join(shots, 'desktop-guide.png') });
  await page.locator('[data-when="tonight"]').click();
  await waitFor(async () => /à 21:00/.test(await page.innerText('#guide-status')), 'guide de ce soir');
  await page.fill('#guide-search', progTitle.split(' ')[0]);
  await waitFor(async () => /trouvé/.test(await page.innerText('#guide-status')), 'recherche dans le guide');
  await page.check('#guide-mine');
  await page.fill('#guide-search', '');
  await page.locator('[data-when="now"]').click();
  await waitFor(async () => (await page.locator('#guide-list .guide-row').count()) >= 1, 'filtre mes chaînes');
  assert(await page.locator('#guide-list .guide-row:not(.mine)').count() === 0, 'seulement mes chaînes');
  await page.locator('#guide-list .guide-row .item-btn').first().click();
  await waitFor(() => page.isVisible('#preview'), 'aperçu ouvert');
  await waitFor(async () => (await page.innerText('#preview-status')).length > 10, 'état de l\'aperçu');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-preview.png') });
  await page.click('#preview-close');
  await waitFor(async () => !(await page.isVisible('#preview')), 'aperçu fermé');
  await page.click('#view-btn-playlists');

  // Vue « Catalogue fournisseur » (lecture seule) + export JSON
  await page.click('#view-btn-catalog');
  assert(await page.isHidden('#view-playlists') && await page.isVisible('#view-catalog'), 'bascule de vue');
  await page.waitForSelector('#provider-tree details.tree-node');
  const firstNode = page.locator('#provider-tree > details.tree-node').first();
  assert((await firstNode.locator('.t-pos').innerText()) === '#1', 'ordre fournisseur');
  const sportNode = page.locator('#provider-tree > details.tree-node').filter({ has: page.locator(':scope > summary .t-name', { hasText: /^FR \| Sport$/ }) });
  await sportNode.locator('summary').first().click();
  await waitFor(async () => (await sportNode.locator('details.tree-node').count()) >= 1, 'sous-groupes visibles');
  assert(await sportNode.locator('.prov-channels li').count() > 0, 'chaînes de la catégorie');
  await page.click('#prov-expand');
  await page.fill('#prov-search', 'football');
  await waitFor(async () => (await page.locator('#prov-results h3').count()) === 2, 'résultats de recherche');
  await page.locator('#view-catalog [data-kind="vod"]').click();
  await waitFor(async () => (await page.innerText('#prov-actions')).includes('get_vod_streams'), 'catalogue films');
  assert((await page.getAttribute('#export', 'href')).includes('kind=vod'), 'export films');
  await page.locator('#view-catalog [data-kind="live"]').click();
  await page.fill('#prov-search', 'football');
  await waitFor(async () => (await page.locator('#prov-results h3').count()) === 2, 'résultats de recherche (direct)');
  const exportHref = await page.getAttribute('#export', 'href');
  const exported = JSON.parse((await fetchText(`${base}${exportHref}`)).text);
  assert(Array.isArray(exported.get_live_categories) && exported.get_live_categories.some((c) => String(c.parent_id) === '3'), 'export JSON');
  if (shots) {
    await page.fill('#prov-search', '');
    await page.waitForTimeout(300);
    await page.locator('#provider-tree > details.tree-node').filter({ has: page.locator(':scope > summary .t-name', { hasText: /^FR \| Sport$/ }) }).locator('summary').first().click();
    await page.screenshot({ path: join(shots, 'desktop-catalog.png') });
  }
  await page.click('#view-btn-playlists');

  // Duplication puis suppression de la copie
  await page.click('#duplicate');
  await page.fill('#dialog-input', 'Chambre');
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.innerText('#editor-title')) === 'Modifier « Chambre »', 'copie ouverte');
  assert((await page.innerText('#url')).endsWith('/p/chambre.m3u'), 'URL de la copie');
  assert(await page.locator('#saved .playlist-item').count() === 2, '2 playlists');
  await page.click('#delete');
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.locator('#saved .playlist-item').count()) === 1, 'suppression');
  assert((await fetchText(url)).status === 200, 'salon toujours servie');
  assert((await fetchText(url.replace('salon', 'chambre'))).status === 404, 'chambre supprimée');

  // Appareils : création, identifiants Xtream fonctionnels, suppression
  await page.click('#view-btn-devices');
  await page.fill('#device-name', 'TV du salon');
  await page.selectOption('#device-playlist', 'salon');
  await page.click('#device-form button[type=submit]');
  await page.waitForSelector('.device-card');
  const creds = await page.locator('.device-card .device-mode').nth(1).locator('code').allInnerTexts();
  const [server, user, pass] = creds;
  assert(user === 'tv-du-salon' && /^[a-z2-9]{10}$/.test(pass), `identifiants ${creds}`);
  const account = JSON.parse((await fetchText(`${server}/player_api.php?username=${user}&password=${pass}`)).text);
  assert(account.user_info.auth === 1, 'connexion Xtream locale');
  const liveStreams = JSON.parse((await fetchText(`${server}/player_api.php?username=${user}&password=${pass}&action=get_live_streams`)).text);
  assert(liveStreams.length > 0 && liveStreams.every((c) => typeof c.stream_id === 'number'), 'chaînes en mode Xtream');
  const deviceM3u = await page.locator('.device-card .device-mode').first().locator('code').first().innerText();
  assert(deviceM3u.endsWith(`/p/salon.m3u?token=${pass}`), `URL M3U appareil ${deviceM3u}`);
  if (shots) await page.screenshot({ path: join(shots, 'desktop-devices.png') });
  await page.locator('.device-card button', { hasText: 'Supprimer' }).click();
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.locator('.device-card').count()) === 0, 'appareil supprimé');
  const revoked = JSON.parse((await fetchText(`${server}/player_api.php?username=${user}&password=${pass}`)).text);
  assert(revoked.user_info.auth === 0, 'accès révoqué');
  await page.click('#view-btn-playlists');

  // Système : abonnement, diagnostic, sauvegarde / restauration
  await page.click('#view-btn-system');
  await waitFor(async () => /Active/.test(await page.innerText('#account-facts')), 'carte abonnement');
  assert(await page.isHidden('#expiry-banner'), 'pas d\'alerte d\'expiration (2030)');
  await page.click('#diag-run');
  await waitFor(async () => (await page.locator('#diag-results .diag-item').count()) >= 8, 'résultats du diagnostic', 60000);
  const okCount = await page.locator('#diag-results .diag-item.ok').count();
  assert(okCount >= 9, `diagnostic : ${okCount} tests OK`);
  const failoverDiag = page.locator('#diag-results .diag-item').filter({ hasText: 'Bascule automatique : « Salon TV »' });
  await waitFor(async () => (await failoverDiag.count()) === 1, 'diagnostic : résultat de la bascule automatique pour Salon TV');
  assert(!(await failoverDiag.getAttribute('class')).includes('error'),
    `diagnostic bascule automatique en erreur : ${await failoverDiag.locator('.d-detail').innerText()}`);
  assert(/secours/.test(await failoverDiag.locator('.d-detail').innerText()), 'détail du diagnostic mentionne les secours');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-system.png'), fullPage: true });
  const backupHref = await page.getAttribute('#backup-download', 'href');
  const backupText = (await fetchText(`${base}${backupHref}`)).text;
  const backup = JSON.parse(backupText);
  assert(backup.playlists.some((p) => p.slug === 'salon'), 'sauvegarde');
  await page.setInputFiles('#restore-file', { name: 'sauvegarde.json', mimeType: 'application/json', buffer: Buffer.from(backupText) });
  await page.click('#restore-run');
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.locator('.toast', { hasText: 'Restauration terminée' }).count()) > 0, 'restauration');
  await page.click('#view-btn-playlists');

  // Mise en page responsive
  for (const [width, height, name] of [[390, 844, 'mobile'], [768, 1024, 'tablette'], [1920, 1080, 'large']]) {
    const p2 = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
    await p2.goto(`${base}/`);
    await p2.waitForSelector('#categories .cat');
    for (const tab of ['categories', 'channels', 'summary']) {
      await p2.click(`#tab-${tab}`);
      const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 0, `${name}/${tab} : débordement horizontal de ${overflow}px`);
    }
    for (const sec of ['organize', 'options', 'guide']) {
      await p2.click(`#sec-btn-${sec}`);
      const overflow = await p2.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert(overflow <= 0, `${name}/${sec} : débordement horizontal de ${overflow}px`);
    }
    await p2.click('#sec-btn-content');
    await p2.click('#tab-categories');
    if (shots) await p2.screenshot({ path: join(shots, `${name}.png`) });
    await p2.click('#view-btn-guide');
    await p2.waitForSelector('#guide-list .guide-row', { timeout: 20000 });
    const overflowGuide = await p2.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflowGuide <= 0, `${name}/guide : débordement horizontal de ${overflowGuide}px`);
    if (shots) await p2.screenshot({ path: join(shots, `${name}-guide.png`) });
    await p2.click('#view-btn-playlists');
    await p2.click('#view-btn-catalog');
    await p2.waitForSelector('#provider-tree details.tree-node');
    await p2.locator('#provider-tree > details.tree-node summary').first().click();
    const overflowCat = await p2.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    assert(overflowCat <= 0, `${name}/catalogue : débordement horizontal de ${overflowCat}px`);
    if (shots) await p2.screenshot({ path: join(shots, `${name}-catalog.png`) });
    await p2.close();
  }
  assert(!errors.length, `erreurs console : ${errors.join(' | ')}`);
  console.log('OK : scénario navigateur complet (bureau, mobile, tablette, grand écran)');
} finally {
  await browser.close();
}
