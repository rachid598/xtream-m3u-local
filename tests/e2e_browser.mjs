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

  await page.click('#tab-summary');
  assert(await page.locator('#sum-excluded li').count() === 1, '1 exclusion');
  assert(await page.locator('#sum-included li').count() === 1, '1 ajout');
  // Ordre et noms de groupes propres à la playlist
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
  assert(await page.isVisible('#epg-box'), 'bloc EPG visible');
  await page.click('#epg-box > summary');
  await waitFor(async () => /% des chaînes/.test(await page.innerText('#epg-pill-text')), 'couverture EPG', 20000);
  assert((await page.innerText('#epg-url')).endsWith('/p/salon.xml'), 'URL EPG');
  assert(/^\d+ \/ \d+$/.test(await page.innerText('#epg-covered')), 'compteur EPG');
  assert(num(await page.innerText('#epg-api')) > 0, 'chaînes complétées par l\'API');
  assert(/xmltv\.php/.test(await page.innerText('#epg-source')), 'source EPG décrite');
  const { text: guide } = await fetchText((await page.innerText('#epg-url')));
  assert(guide.includes('<programme') && guide.trim().endsWith('</tv>'), 'XMLTV de la playlist');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-epg.png') });
  await page.click('#epg-refresh');
  await waitFor(async () => (await page.locator('.toast', { hasText: 'Actualisation du guide' }).count()) > 0, 'actualisation EPG');
  await page.click('#epg-box > summary');
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
    await page.click('#tab-categories');
    await page.fill('#cat-search', '');
    await page.waitForTimeout(300);
    await page.screenshot({ path: join(shots, 'desktop-saved.png') });
  }

  // Modification : même URL, contenu différent
  await page.click('#tab-categories');
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
  await page.click('#tab-categories');
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
  await page.click('#tab-channels');
  await page.fill('#search', '');
  await page.waitForTimeout(250);
  await page.selectOption('#ch-category', { label: (await page.locator('#ch-category option', { hasText: 'FR | Information' }).innerText()) });
  await page.selectOption('#ch-state', 'on');
  const firstRow = page.locator('#channels .channel').first();
  const firstName = await firstRow.locator('.ch-name').innerText();
  await firstRow.locator('.item-btn', { hasText: '☆' }).click();
  await waitFor(async () => (await page.locator('#channels .channel').first().locator('.fav-on').count()) === 1, 'favori');
  await page.locator('#channels .channel').first().locator('.item-btn', { hasText: '✎' }).click();
  await page.fill('#dialog-input', 'Ma chaîne info');
  await page.fill('#dialog-input2', '7');
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.locator('#channels .channel').first().locator('.ch-name').innerText()) === 'Ma chaîne info', 'renommage');
  await page.selectOption('#ch-state', 'all');
  await page.selectOption('#ch-category', '');
  await page.click('#options-box > summary');
  await page.check('#opt-dedupe');
  await page.click('#options-box > summary');
  await page.click('#tab-summary');
  assert(await page.locator('#sum-favorites li', { hasText: 'Ma chaîne info' }).count() === 1, 'favori dans le résumé');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement des personnalisations');
  const { text: m3uCustom } = await fetchText(url);
  const lines = m3uCustom.split('\n');
  assert(/group-title="★ Favoris",Ma chaîne info$/.test(lines[1]), `favori en tête : ${lines[1]}`);
  assert(m3uCustom.includes('tvg-chno="7"') && m3uCustom.includes('group-title="BE | Belgique"'), 'numéro et règle dans le M3U');
  assert(!m3uCustom.includes(`,${firstName}\n`) || firstName === 'Ma chaîne info', 'nom d\'origine remplacé');
  await page.click('#auto-number');
  await page.click('#dialog-ok');
  await waitFor(async () => (await page.locator('#sum-custom li').count()) > 5, 'numérotation automatique');
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement de la numérotation');
  const { text: numbered } = await fetchText(url);
  assert(numbered.includes('tvg-chno="1"') && numbered.includes('tvg-chno="2"'), 'numéros 1, 2… dans le M3U');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-summary.png') });
  await page.click('#tab-categories');

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
  await page.click('#tab-channels');
  assert((await page.innerText('#tab-items-label')) === 'Séries', 'onglet Séries');
  await page.locator('#channels .link-btn').first().click();
  await waitFor(async () => /épisode/.test(await page.locator('#channels .ok-text').first().innerText()), 'épisodes affichés');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-series.png') });
  await page.click('#tab-summary');
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
  await page.click('#tab-categories');
  await kindBtn('live').click();

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
    await p2.click('#tab-categories');
    if (shots) await p2.screenshot({ path: join(shots, `${name}.png`) });
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
