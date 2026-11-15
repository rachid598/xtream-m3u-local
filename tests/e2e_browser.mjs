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
