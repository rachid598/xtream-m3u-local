// Test navigateur des profils (un abonnement par membre de la famille).
//
// Usage :
//   python -m tools.demo --profiles --data /tmp/demo-profiles --port 8787 --web-port 8788 &
//   node tests/e2e_profiles.mjs http://127.0.0.1:8788 [dossier_captures]
//
// Scénario : barre « Profil » (Moi / Sœur), playlist créée dans « Sœur » avec son seul abonnement,
// invisible dans « Moi », appareils filtrés par profil, M3U sans aucune chaîne de l'autre abonnement.
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const require = createRequire(import.meta.url);
const { chromium } = (() => {
  try { return require('@playwright/test'); } catch (_) { return require('playwright'); }
})();

const base = (process.argv[2] || 'http://127.0.0.1:8788').replace(/\/$/, '');
const shots = process.argv[3] || null;
if (shots) mkdirSync(shots, { recursive: true });
const OFFSET = 10 ** 11;

function assert(cond, msg) { if (!cond) throw new Error(`Échec : ${msg}`); }
async function waitFor(fn, msg, timeout = 10000) {
  const end = Date.now() + timeout;
  for (;;) {
    try { if (await fn()) return; } catch (_) { /* retry */ }
    if (Date.now() > end) throw new Error(`Délai dépassé : ${msg}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}
const api = async (path) => (await fetch(`${base}${path}`, { headers: { 'X-Requested-With': 'XtreamM3ULocal' } })).json();

const errors = [];
const browser = await chromium.launch();
try {
  const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(`${base}/`);
  await waitFor(async () => (await page.getAttribute('#connection', 'class')) === 'pill ok', 'connexion catalogue');
  const showSection = async (name) => { await page.click(`#sec-btn-${name}`); await waitFor(() => page.isVisible(`#sec-${name}`), `partie ${name}`); };

  // Barre de profils
  await waitFor(() => page.isVisible('#profile-bar'), 'barre des profils');
  const buttons = await page.locator('#profile-buttons button').allInnerTexts();
  assert(JSON.stringify(buttons) === JSON.stringify(['Moi', 'Sœur']), `profils : ${buttons}`);
  const catalog = await api('/api/catalog?kind=live');
  const total = catalog.categories.length;
  const countText = async () => page.innerText('#tab-cat-count');
  await waitFor(async () => /\(0\/\d+\)/.test(await countText()), 'compteur de catégories');
  const mine = Number((await countText()).match(/\/(\d+)/)[1]);
  assert(mine > 0 && mine < total, `catégories du profil « Moi » : ${mine} sur ${total}`);

  // Profil « Sœur » : aucune playlist, son abonnement seulement
  // Profil = serveur à part : la page est rechargée sur ce profil seul.
  await Promise.all([page.waitForEvent('load'), page.locator('#profile-buttons button', { hasText: 'Sœur' }).click()]);
  await waitFor(async () => (await page.getAttribute('#connection', 'class')) === 'pill ok', 'connexion catalogue (Sœur)');
  await waitFor(async () => /Aucune playlist dans le profil « Sœur »/.test(await page.innerText('#saved')), 'profil Sœur vide');
  assert(await page.inputValue('#opt-provider') === '2', 'nouvelle playlist sur l\'abonnement de Sœur');
  const choices = await page.locator('#opt-provider option').allInnerTexts();
  assert(choices.length === 1, `mélange désactivé par défaut : ${choices}`);
  const hers = Number((await countText()).match(/\/(\d+)/)[1]);
  assert(hers === mine, `même nombre de catégories (même faux fournisseur) : ${hers} / ${mine}`);
  await page.fill('#name', 'Playlist sœur');
  const box = page.getByRole('checkbox', { name: /^Inclure la catégorie FR \| Généralistes$/ });
  await box.check();
  await page.click('#save');
  await waitFor(async () => !(await page.isVisible('#dirty')), 'enregistrement');
  const saved = (await api('/api/playlists')).find((p) => p.name === 'Playlist sœur');
  assert(saved && saved.provider === '2', `playlist enregistrée dans le profil 2 : ${saved && saved.provider}`);
  const m3u = await (await fetch(`${base.replace(/:\d+$/, ':8787')}/p/${saved.slug}.m3u`)).text();
  const ids = [...m3u.matchAll(/\/stream\/(\d+)\.ts/g)].map((m) => Number(m[1]));
  assert(ids.length && ids.every((id) => id >= 2 * OFFSET), `M3U de Sœur : uniquement son abonnement (${ids.slice(0, 3)})`);
  if (shots) await page.screenshot({ path: join(shots, 'desktop-profiles.png') });

  // Appareils : la liste ne propose que les playlists du profil
  await page.click('#view-btn-devices');
  await waitFor(async () => (await page.locator('#device-playlist option').count()) > 0, 'vue appareils');
  const options = await page.locator('#device-playlist option').allInnerTexts();
  assert(options.length === 1 && /Playlist sœur/.test(options[0]), `playlists proposées : ${options}`);
  await page.click('#view-btn-playlists');

  // Retour à « Moi » : la playlist de Sœur n'apparaît pas
  await Promise.all([page.waitForEvent('load'), page.locator('#profile-buttons button', { hasText: 'Moi' }).click()]);
  await waitFor(async () => !/Playlist sœur/.test(await page.innerText('#saved')), 'playlist de Sœur cachée dans Moi');
  assert(await page.inputValue('#opt-provider') === '1', 'nouvelle playlist sur l\'abonnement de Moi');

  // Abonnement ajouté depuis l'interface (Système → Abonnements) : nouveau profil sans redémarrer
  await page.click('#view-btn-system');
  await waitFor(async () => (await page.locator('#providers-list li').count()) >= 2, 'liste des abonnements');
  const host = (await api('/api/providers')).providers[0].host;   // faux fournisseur de la démo
  await page.fill('#provider-name', 'Maman');
  await page.fill('#provider-url', `http://localhost:${host.split(':')[1]}`);
  await page.fill('#provider-user', 'user');
  await page.fill('#provider-pass', 'pw');
  await page.click('#provider-add');
  await waitFor(async () => (await page.locator('#profile-buttons button').count()) === 3, 'profil « Maman » ajouté');
  await waitFor(async () => /Maman/.test(await page.innerText('#providers-list')), 'abonnement listé');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-providers.png'), fullPage: false });
  // Mélange : réglage de chaque abonnement. « Maman » (ajouté sans cocher) et les autres restent isolés.
  const providers = async () => (await api('/api/providers')).providers;
  assert((await providers()).every((p) => !p.mixable), 'abonnements isolés par défaut');
  await page.click('#view-btn-playlists');
  await waitFor(async () => (await page.locator('#opt-provider option').count()) === 1, 'aucun mélange proposé');
  // Abonnement de « Sœur » rendu mélangeable avec le bouton « Modifier ».
  await page.click('#view-btn-system');
  const row = page.locator('#providers-list li', { hasText: 'Sœur' });
  await row.getByRole('button', { name: 'Modifier' }).click();
  await page.selectOption('#dialog-select', '1');
  await page.click('#dialog-ok');
  await waitFor(async () => (await providers()).find((p) => p.index === 2).mixable === true, 'Sœur mélangeable');
  await waitFor(async () => /mélangeable avec les autres/.test(await row.innerText()), 'état affiché');
  await page.click('#view-btn-playlists');
  await waitFor(async () => (await page.locator('#opt-provider option').count()) === 2, 'choix « mélange » proposé');
  assert(/Moi \+ Sœur/.test((await page.locator('#opt-provider option').allInnerTexts())[1]), 'mélange = Moi + Sœur seulement');
  // Playlist « mélange » : un bloc repliable par fournisseur, celui du profil déplié, les autres repliés.
  await showSection('options');
  await page.selectOption('#opt-provider', 'mix');
  await showSection('content');
  await waitFor(async () => (await page.locator('#categories details.provider-group').count()) === 2, 'blocs Moi et Sœur (pas Maman)');
  const groups = page.locator('#categories details.provider-group');
  assert(/Moi/.test(await groups.nth(0).locator('summary').innerText()), 'premier bloc : abonnement du profil');
  assert(await groups.nth(0).evaluate((d) => d.open) && !(await groups.nth(1).evaluate((d) => d.open)), 'Moi déplié, autres repliés');
  await groups.nth(0).locator('summary').click();
  await groups.nth(1).locator('summary').click();
  await waitFor(async () => !(await groups.nth(0).evaluate((d) => d.open)) && await groups.nth(1).evaluate((d) => d.open), 'replier / déplier');
  if (shots) await page.screenshot({ path: join(shots, 'desktop-provider-groups.png') });
  await showSection('options');
  await page.selectOption('#opt-provider', '1');
  await waitFor(async () => (await page.locator('#categories details.provider-group').count()) === 0, 'un seul abonnement : pas de blocs');
  await showSection('content');
  if (shots) {
    await page.click('#view-btn-system');
    await page.locator('#providers-card').screenshot({ path: join(shots, 'desktop-providers-mix.png') });
    await page.click('#view-btn-playlists');
  }

  // Téléphone : pas de défilement horizontal avec la barre de profils
  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  await phone.goto(`${base}/`);
  await waitFor(() => phone.isVisible('#profile-bar'), 'barre des profils (mobile)');
  const overflow = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  assert(overflow <= 1, `défilement horizontal sur mobile : ${overflow}px`);
  if (shots) await phone.screenshot({ path: join(shots, 'mobile-profiles.png') });

  assert(!errors.length, `erreurs JavaScript : ${errors.join(' | ')}`);
  console.log('OK : profils séparés (rechargement, playlists, appareils, M3U, mélange optionnel, mobile)');
} finally {
  await browser.close();
}
