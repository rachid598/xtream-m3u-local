/* Xtream M3U Local — interface de gestion (aucune dépendance externe). */
'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const PAGE = 200;
  const collator = new Intl.Collator('fr', { sensitivity: 'base', numeric: true });

  // ------------------------------------------------------- types de contenu
  const KINDS = ['live', 'vod', 'series'];
  const KIND = {
    live: { icon: '📺', selectedUnit: 'sélectionnées', label: 'TV en direct', one: 'chaîne', many: 'chaînes', title: 'Chaînes', kept: 'retenues', added: 'Ajoutée', excludedLabel: 'Exclue',
      prefix: '', fields: ['category_ids', 'included_stream_ids', 'excluded_stream_ids'], actions: 'get_live_categories et get_live_streams', categoriesAction: 'get_live_categories' },
    vod: { icon: '🎬', selectedUnit: 'sélectionnés', label: 'Films', one: 'film', many: 'films', title: 'Films', kept: 'retenus', added: 'Ajouté', excludedLabel: 'Exclu',
      prefix: 'vod:', fields: ['vod_category_ids', 'included_vod_ids', 'excluded_vod_ids'], actions: 'get_vod_categories et get_vod_streams', categoriesAction: 'get_vod_categories' },
    series: { icon: '🎞️', selectedUnit: 'sélectionnées', label: 'Séries', one: 'série', many: 'séries', title: 'Séries', kept: 'retenues', added: 'Ajoutée', excludedLabel: 'Exclue',
      prefix: 'series:', fields: ['series_category_ids', 'included_series_ids', 'excluded_series_ids'], actions: 'get_series_categories et get_series', categoriesAction: 'get_series_categories' },
  };
  const emptyCatalog = () => ({ categories: [], categoryNames: new Map(), channels: [], channelById: new Map(),
    channelsByCat: new Map(), catalogLoaded: false, categoriesError: null, loading: null });
  const emptySelection = () => ({ selected: new Set(), included: new Set(), excluded: new Set() });

  // ------------------------------------------------------------------ état
  const state = {
    status: null,
    kind: 'live',            // type affiché : live | vod | series
    cat: { live: emptyCatalog(), vod: emptyCatalog(), series: emptyCatalog() },
    sel: { live: emptySelection(), vod: emptySelection(), series: emptySelection() },
    playlists: [],
    editing: null,           // slug de la playlist ouverte (null = nouvelle)
    groupOrder: [],          // ordre personnalisé des groupes (clés : "12", "vod:3", "series:7")
    groupNames: {},          // noms de groupes personnalisés {clé: nom}
    view: 'playlists',
    slugTouched: false,
    snapshot: '',            // pour détecter les modifications non enregistrées
    catFilter: 'all',
    limit: PAGE,
    tab: 'categories',
    summary: null,           // {live|vod|series: {items, categories, error}} (/api/catalog/summary)
  };
  // Raccourcis vers le catalogue et la sélection du type affiché (state.categories, state.selected…).
  for (const key of ['categories', 'categoryNames', 'channels', 'channelById', 'channelsByCat', 'catalogLoaded', 'categoriesError']) {
    Object.defineProperty(state, key, { get() { return this.cat[this.kind][key]; }, set(v) { this.cat[this.kind][key] = v; } });
  }
  for (const key of ['selected', 'included', 'excluded']) {
    Object.defineProperty(state, key, { get() { return this.sel[this.kind][key]; }, set(v) { this.sel[this.kind][key] = v; } });
  }
  const K = () => KIND[state.kind];
  const noun = (n, kind = state.kind) => plural(n, KIND[kind].one, KIND[kind].many);

  /** Libellé d'origine d'un groupe de playlist ("12", "vod:3", "series:7"). */
  function groupInfo(key) {
    for (const kind of ['vod', 'series']) {
      if (key.startsWith(KIND[kind].prefix)) {
        const id = key.slice(KIND[kind].prefix.length);
        return { kind, id, name: state.cat[kind].categoryNames.get(id) || (id === 'none' ? 'Sans catégorie' : `Catégorie ${id}`) };
      }
    }
    return { kind: 'live', id: key, name: state.cat.live.categoryNames.get(key) || (key === 'none' ? 'Sans catégorie' : `Catégorie ${key}`) };
  }

  // --------------------------------------------------------------- outils
  function h(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === undefined || value === null || value === false) continue;
        if (key === 'class') node.className = value;
        else if (key === 'text') node.textContent = value;
        else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
        else if (key in node && typeof value !== 'string') node[key] = value;
        else node.setAttribute(key, value === true ? '' : value);
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const plural = (n, one, many) => `${n.toLocaleString('fr-FR')} ${n > 1 ? many : one}`;
  const norm = (text) => String(text || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();

  function slugify(name) {
    return norm(name).replace(/[^a-z0-9_-]+/g, '-').replace(/^[-_]+|-+$/g, '').slice(0, 60);
  }

  function debounce(fn, wait) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), wait); };
  }

  function storage(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(key) || '';
      if (value) localStorage.setItem(key, value); else localStorage.removeItem(key);
    } catch (_) { /* navigation privée */ }
    return '';
  }

  async function request(path, method = 'GET', body) {
    const options = { method, headers: { Accept: 'application/json' }, credentials: 'same-origin' };
    if (method !== 'GET') {
      options.headers['X-Requested-With'] = 'XtreamM3ULocal';
      if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
    }
    let response;
    try {
      response = await fetch(path, options);
    } catch (_) {
      throw new Error('Serveur injoignable : vérifie que le conteneur est démarré.');
    }
    let data = null;
    try { data = await response.json(); } catch (_) { /* réponse non JSON */ }
    if (!response.ok) throw new Error((data && data.error) || `Erreur HTTP ${response.status}`);
    return data;
  }

  function toast(message, type = 'ok') {
    const node = h('div', { class: `toast ${type === 'error' ? 'error' : ''}`, role: 'status', text: message });
    $('toasts').append(node);
    setTimeout(() => node.remove(), type === 'error' ? 6000 : 3000);
  }

  function showError(message) {
    $('alert').textContent = message;
    $('alert').hidden = false;
  }
  function clearError() { $('alert').hidden = true; }

  function ask({ title, text, okLabel = 'Confirmer', danger = false, input = null }) {
    const dialog = $('dialog');
    $('dialog-title').textContent = title;
    $('dialog-text').textContent = text || '';
    $('dialog-ok').textContent = okLabel;
    $('dialog-ok').className = `btn ${danger ? 'danger' : 'primary'}`;
    $('dialog-field').hidden = !input;
    if (input) {
      $('dialog-label').textContent = input.label;
      $('dialog-input').value = input.value || '';
    }
    if (typeof dialog.showModal !== 'function') {
      // Navigateur très ancien : repli sur les boîtes natives.
      if (input) return Promise.resolve(window.prompt(`${title}\n${input.label}`, input.value || ''));
      return Promise.resolve(window.confirm(`${title}\n${text || ''}`) ? true : null);
    }
    return new Promise((resolve) => {
      dialog.returnValue = 'cancel';
      dialog.addEventListener('close', () => {
        if (dialog.returnValue !== 'ok') return resolve(null);
        resolve(input ? $('dialog-input').value : true);
      }, { once: true });
      dialog.showModal();
      if (input) $('dialog-input').select();
    });
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (_) {
      const area = h('textarea', { class: 'sr-only', 'aria-hidden': 'true' });
      area.value = text;
      document.body.append(area);
      area.select();
      try { document.execCommand('copy'); } catch (__) { /* ignore */ }
      area.remove();
    }
    toast('URL copiée dans le presse-papiers');
  }

  // ----------------------------------------------------------- sélection
  const inCategory = (c) => c.category_ids.some((id) => state.selected.has(id));
  const isOn = (c) => !state.excluded.has(c.stream_id) && (inCategory(c) || state.included.has(c.stream_id));

  function setChannel(c, on) {
    const id = c.stream_id;
    if (on) {
      state.excluded.delete(id);
      if (!inCategory(c)) state.included.add(id); else state.included.delete(id);
    } else {
      state.included.delete(id);
      if (inCategory(c)) state.excluded.add(id); else state.excluded.delete(id);
    }
  }

  function payload({ sorted = false } = {}) {
    const data = { name: $('name').value.trim(), slug: $('slug').value.trim() };
    for (const kind of KINDS) {
      const sel = state.sel[kind];
      const byId = state.cat[kind].channelById;
      const [catField, incField, excField] = KIND[kind].fields;
      // Nettoyage : une exclusion n'a de sens que pour un élément d'une catégorie cochée.
      let excluded = [...sel.excluded];
      if (!sorted) excluded = excluded.filter((id) => { const c = byId.get(id); return !c || c.category_ids.some((cid) => sel.selected.has(cid)); });
      const list = (set) => (sorted ? [...set].sort() : [...set]);
      data[catField] = list(sel.selected);
      data[incField] = list(sel.included);
      data[excField] = sorted ? excluded.sort() : excluded;
    }
    data.live_format = $('opt-live-format').value;
    data.catchup = $('opt-catchup').checked;
    data.group_order = [...state.groupOrder];
    data.group_names = cleanGroupNames();
    return data;
  }

  function cleanGroupNames() {
    const result = {};
    for (const id of Object.keys(state.groupNames).sort()) {
      const name = String(state.groupNames[id] || '').trim();
      if (name && name !== groupInfo(id).name) result[id] = name.slice(0, 100);
    }
    return result;
  }

  const signature = () => JSON.stringify(payload({ sorted: true }));
  const hasSelection = () => KINDS.some((k) => state.sel[k].selected.size || state.sel[k].included.size);
  const isDirty = () => signature() !== state.snapshot;

  function countFor(p, kind = 'live') {
    const [catField, incField, excField] = KIND[kind].fields;
    const cats = new Set(p[catField] || []), inc = new Set(p[incField] || []), exc = new Set(p[excField] || []);
    let n = 0;
    for (const c of state.cat[kind].channels) {
      if (!exc.has(c.stream_id) && (c.category_ids.some((id) => cats.has(id)) || inc.has(c.stream_id))) n += 1;
    }
    return n;
  }

  /** Nombre d'éléments retenus pour la sélection en cours, par type. */
  function selectionCount(kind) {
    const sel = state.sel[kind];
    let n = 0;
    for (const c of state.cat[kind].channels) {
      if (!sel.excluded.has(c.stream_id) && (c.category_ids.some((id) => sel.selected.has(id)) || sel.included.has(c.stream_id))) n += 1;
    }
    return n;
  }

  const usesKind = (p, kind) => Boolean((p[KIND[kind].fields[0]] || []).length || (p[KIND[kind].fields[1]] || []).length);

  // -------------------------------------------------------------- URLs
  function baseUrl() {
    const s = state.status;
    if (s && s.public_base_url) return s.public_base_url.replace(/\/$/, '');
    const port = (s && s.playlist_port) || 7777;
    return `${location.protocol === 'https:' ? 'https:' : 'http:'}//${location.hostname.includes(':') && !location.hostname.startsWith('[') ? `[${location.hostname}]` : location.hostname}:${port}`;
  }
  function token() {
    const s = state.status;
    if (s && s.access_token) return s.access_token;
    return s && s.token_required ? $('token').value.trim() : '';
  }
  function withToken(url) {
    const t = token();
    return t ? `${url}?token=${encodeURIComponent(t)}` : url;
  }
  const playlistUrl = (slug) => withToken(`${baseUrl()}/p/${slug}.m3u`);

  // ---------------------------------------------------------- rendu
  function renderStats() {
    const counts = {};
    for (const kind of KINDS) {
      const sel = state.sel[kind];
      const loaded = state.cat[kind].catalogLoaded;
      // Catalogue pas encore chargé : on affiche le nombre de sélections plutôt que 0.
      const pending = !loaded && (sel.selected.size || sel.included.size);
      counts[kind] = loaded ? selectionCount(kind) : (pending ? null : 0);
      document.querySelectorAll(`[data-kind-count="${kind}"]`).forEach((node) => {
        node.textContent = counts[kind] ? counts[kind].toLocaleString('fr-FR') : (pending ? '…' : '');
      });
    }
    const show = (id, kind) => { $(id).textContent = counts[kind] === null ? '…' : counts[kind].toLocaleString('fr-FR'); };
    show('stat-channels', 'live');
    show('stat-vod', 'vod');
    show('stat-series', 'series');
    $('stat-categories').textContent = KINDS.reduce((n, k) => n + state.sel[k].selected.size, 0).toLocaleString('fr-FR');
    const n = counts[state.kind] || 0;
    $('tab-cat-count').textContent = state.categories.length ? `(${state.selected.size}/${state.categories.length})` : '';
    $('tab-ch-count').textContent = state.channels.length ? `(${n.toLocaleString('fr-FR')})` : '';
    const parts = KINDS.filter((k) => counts[k]).map((k) => noun(counts[k], k));
    $('bar-count').textContent = parts.length ? parts.join(' · ') : 'Playlist vide';
    const name = $('name').value.trim();
    $('bar-name').textContent = name ? `· ${name}` : '';
    const dirty = isDirty();
    $('dirty').hidden = !dirty || (!state.editing && !name && !hasSelection());
    $('revert').hidden = !(dirty && state.editing);
    renderTiles();
  }

  /** Tuiles « Contenu de la playlist » : disponible chez le fournisseur / sélectionné dans la playlist. */
  function renderTiles() {
    for (const kind of KINDS) {
      const meta = document.querySelector(`[data-kind-meta="${kind}"]`);
      if (!meta) continue;
      const cat = state.cat[kind];
      const info = state.summary && state.summary[kind];
      const items = cat.catalogLoaded ? cat.channels.length : info ? info.items : null;
      const categories = cat.catalogLoaded ? cat.categories.length : info ? info.categories : null;
      meta.classList.toggle('error', Boolean(info && info.error && !cat.catalogLoaded));
      if (info && info.error && !cat.catalogLoaded) {
        meta.textContent = 'Indisponible chez le fournisseur';
        meta.title = info.error;
      } else if (items === null || items === undefined) {
        meta.textContent = 'Chargement…';
      } else if (!items) {
        meta.textContent = `Aucun${KIND[kind].one === 'film' ? '' : 'e'} ${KIND[kind].one} dans ton abonnement`;
      } else {
        meta.textContent = `${noun(items, kind)} disponibles · ${plural(categories, 'catégorie', 'catégories')}`;
        meta.removeAttribute('title');
      }
      const unit = meta.closest('.kind-tile')?.querySelector('.kt-unit');
      if (unit) unit.textContent = KIND[kind].selectedUnit;
    }
    // Aide : l'abonnement contient des films / séries qui ne sont pas encore dans la playlist.
    const hint = $('vod-hint');
    const available = (kind) => {
      const cat = state.cat[kind];
      return cat.catalogLoaded ? cat.channels.length : (state.summary && state.summary[kind] && state.summary[kind].items) || 0;
    };
    const missing = ['vod', 'series'].filter((kind) => available(kind) > 0 && !state.sel[kind].selected.size && !state.sel[kind].included.size);
    if (!missing.length || state.kind !== 'live') {
      hint.hidden = true;
      return;
    }
    const text = missing.map((kind) => noun(available(kind), kind)).join(' et ');
    hint.replaceChildren(
      h('span', null, `Ton abonnement contient ${text}, pas encore ajouté${missing.length > 1 ? 's' : ''} à cette playlist.`),
      h('span', { class: 'btn-row' }, missing.map((kind) => h('button', {
        type: 'button', class: 'btn small primary', text: `Ajouter des ${KIND[kind].many}`,
        onclick: () => { switchKind(kind); $('kind-title').scrollIntoView({ behavior: 'smooth', block: 'start' }); },
      }))));
    hint.hidden = false;
  }

  // ------------------------------------------------------------------ EPG
  let epgTimer = null;
  let epgPolls = 0;

  function epgVisible() {
    const p = state.editing && state.playlists.find((x) => x.slug === state.editing);
    return Boolean(p && state.status && state.status.epg_enabled && usesKind(p, 'live'));
  }

  async function loadEpg({ poll = false } = {}) {
    clearTimeout(epgTimer);
    if (!poll) epgPolls = 0;
    const box = $('epg-box');
    if (!epgVisible()) { box.hidden = true; return; }
    box.hidden = false;
    const slug = state.editing;
    const url = withToken(`${baseUrl()}/p/${slug}.xml`);
    $('epg-url').textContent = url;
    $('epg-open').href = url;
    let data;
    try {
      data = await request(`/api/epg?slug=${encodeURIComponent(slug)}`);
    } catch (error) {
      setEpgPill('error', 'État indisponible');
      $('epg-source').textContent = error.message;
      return;
    }
    if (slug !== state.editing) return;
    renderEpg(data);
    const pl = data.playlist;
    if (pl && pl.pending > 0 && epgPolls < 20) {
      epgPolls += 1;
      epgTimer = setTimeout(() => loadEpg({ poll: true }), 4000);
    }
  }

  function setEpgPill(kind, text) {
    $('epg-pill').className = `pill ${kind}`;
    $('epg-pill-text').textContent = text;
  }

  function renderEpg(data) {
    const pl = data.playlist;
    const src = data.source || {};
    const sourceName = data.custom_source ? 'la source EPG_URL' : 'xmltv.php';
    $('epg-xmltv-label').textContent = `via ${data.custom_source ? 'EPG_URL' : 'xmltv.php'}`;
    if (!pl) {
      setEpgPill('error', 'Guide indisponible');
      ['epg-covered', 'epg-xmltv', 'epg-api', 'epg-missing-count'].forEach((id) => { $(id).textContent = '–'; });
      $('epg-source').textContent = data.error || 'Guide indisponible.';
      $('epg-missing-box').hidden = true;
      return;
    }
    const covered = pl.xmltv + pl.api;
    const pct = pl.channels ? Math.round((covered / pl.channels) * 100) : 0;
    $('epg-covered').textContent = `${covered} / ${pl.channels}`;
    $('epg-xmltv').textContent = pl.xmltv.toLocaleString('fr-FR');
    $('epg-api').textContent = data.mode === 'xmltv' ? 'désactivé' : pl.api.toLocaleString('fr-FR');
    $('epg-missing-count').textContent = pl.missing.length.toLocaleString('fr-FR');
    if (pl.pending) setEpgPill('warn', `Récupération en cours… ${pct} %`);
    else if (!pl.channels) setEpgPill('', 'Aucune chaîne');
    else if (!covered) setEpgPill('error', 'Aucun programme');
    else setEpgPill(pl.missing.length ? 'warn' : 'ok', `${pct} % des chaînes`);
    const parts = [];
    if (data.mode === 'api') parts.push('Mode EPG_SOURCE=api : programmes récupérés chaîne par chaîne via l\'API Xtream.');
    else if (src.ok) {
      const when = src.updated_at ? new Date(src.updated_at * 1000).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' }) : '';
      parts.push(`Source ${sourceName} : ${plural(src.channels, 'chaîne', 'chaînes')}, ${plural(src.programmes, 'programme', 'programmes')}${when ? `, mise à jour le ${when}` : ''}.`);
      if (data.mode === 'auto' && pl.api) parts.push(`Les chaînes absentes de ${sourceName} sont complétées par l'API Xtream.`);
    } else {
      parts.push(`${sourceName} ne fonctionne pas${src.error ? ` (${src.error})` : ''}${data.mode === 'auto' ? ' : les programmes sont récupérés chaîne par chaîne via l\'API Xtream.' : '.'}`);
    }
    if (pl.pending) parts.push(`${plural(pl.pending, 'chaîne', 'chaînes')} en cours de récupération (actualisation automatique).`);
    $('epg-source').textContent = parts.join(' ');
    const list = $('epg-missing');
    list.replaceChildren();
    for (const item of pl.missing.slice(0, 300)) {
      list.append(h('li', null, h('span', { text: item.name }), h('span', { class: 'num', text: item.epg_id ? `tvg-id ${item.epg_id}` : 'sans identifiant EPG' })));
    }
    if (pl.missing.length > 300) list.append(h('li', { class: 'empty', text: `… et ${pl.missing.length - 300} autres` }));
    $('epg-missing-summary').textContent = `${plural(pl.missing.length, 'chaîne', 'chaînes')} sans programme (le fournisseur n'en publie pas)`;
    $('epg-missing-box').hidden = !pl.missing.length;
  }

  async function refreshEpg() {
    if (!state.editing) return;
    $('epg-refresh').disabled = true;
    try {
      await request('/api/epg/refresh', 'POST', { slug: state.editing });
      toast('Actualisation du guide lancée');
      setEpgPill('warn', 'Actualisation…');
      epgPolls = 0;
      clearTimeout(epgTimer);
      epgTimer = setTimeout(() => loadEpg({ poll: true }), 3000);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      setTimeout(() => { $('epg-refresh').disabled = false; }, 3000);
    }
  }

  function renderOptionsPill() {
    const parts = [$('opt-live-format').value === 'hls' ? 'HLS' : 'MPEG-TS'];
    if ($('opt-catchup').checked) parts.push('Replay');
    $('options-pill-text').textContent = parts.join(' · ');
  }

  async function loadSummary(force = false) {
    try {
      state.summary = await request(`/api/catalog/summary${force ? '?refresh=1' : ''}`);
    } catch (_) { /* facultatif : les tuiles restent sur « Chargement… » */ }
    renderTiles();
  }

  function renderUrl() {
    const box = $('url-box');
    if (state.editing) {
      const url = playlistUrl(state.editing);
      $('url').textContent = url;
      $('open').href = url;
      $('open').setAttribute('aria-disabled', 'false');
      $('copy').disabled = false;
      box.classList.add('ready');
    } else {
      const slug = $('slug').value.trim();
      $('url').textContent = slug ? `${baseUrl()}/p/${slug}.m3u  (après enregistrement)` : 'Enregistre la playlist pour obtenir son URL';
      $('open').removeAttribute('href');
      $('open').setAttribute('aria-disabled', 'true');
      $('copy').disabled = true;
      box.classList.remove('ready');
    }
    $('full-url').textContent = withToken(`${baseUrl()}/playlist.m3u`);
    $('full-epg-url').textContent = state.status && state.status.epg_enabled ? withToken(`${baseUrl()}/xmltv.php`) : 'EPG désactivé (ENABLE_EPG=false)';
  }

  function renderPlaylists() {
    const list = $('saved');
    list.replaceChildren();
    if (!state.playlists.length) {
      list.append(h('li', { class: 'empty', text: 'Aucune playlist. Crée la première avec « + Nouvelle ».' }));
      return;
    }
    const sorted = [...state.playlists].sort((a, b) => collator.compare(a.name, b.name));
    for (const p of sorted) {
      const parts = KINDS.filter((k) => state.cat[k].catalogLoaded && usesKind(p, k)).map((k) => noun(countFor(p, k), k));
      const count = parts.length ? ` · ${parts.join(' · ')}` : '';
      list.append(h('li', null, h('button', {
        type: 'button', class: 'playlist-item', 'aria-current': p.slug === state.editing ? 'true' : 'false',
        onclick: () => openPlaylist(p.slug),
      }, h('span', { class: 'pl-name', text: p.name }), h('span', { class: 'pl-meta', text: `/p/${p.slug}.m3u${count}` }))));
    }
  }

  function categoryStats() {
    const on = new Map();
    for (const c of state.channels) {
      if (!isOn(c)) continue;
      for (const id of c.category_ids) on.set(id, (on.get(id) || 0) + 1);
    }
    return on;
  }

  function renderCategories() {
    const grid = $('categories');
    grid.replaceChildren();
    if (!state.catalogLoaded) {
      grid.append(h('p', { class: 'empty', text: `Chargement du catalogue ${K().label.toLowerCase()}…` }));
      return;
    }
    if (!state.categories.length) {
      grid.append(h('p', { class: 'empty', text: state.kind === 'live'
        ? 'Aucune catégorie disponible. Vérifie la connexion Xtream puis « Actualiser le catalogue ».'
        : `Aucune catégorie de ${K().many} pour ton compte (${K().categoriesAction} vide).` }));
      $('cat-info').textContent = '';
      return;
    }
    const query = norm($('cat-search').value.trim());
    const retained = categoryStats();
    const visible = state.categories.filter((cat) => {
      if (query && !cat._search.includes(query)) return false;
      if (state.catFilter === 'on' && !state.selected.has(cat.category_id)) return false;
      if (state.catFilter === 'off' && state.selected.has(cat.category_id)) return false;
      return true;
    });
    const fragment = document.createDocumentFragment();
    for (const cat of visible) {
      const on = state.selected.has(cat.category_id);
      const kept = retained.get(cat.category_id) || 0;
      let metaText = noun(cat.count);
      let partial = false;
      const parent = cat.parent_id && state.categoryNames.get(cat.parent_id);
      if (on && kept !== cat.count) { metaText = `${kept} / ${cat.count} ${K().many} ${K().kept}`; partial = true; }
      else if (!on && kept) { metaText = `${noun(cat.count)} · ${kept} ${K().added.toLowerCase()}${kept > 1 ? 's' : ''}`; partial = true; }
      const box = h('input', { type: 'checkbox', checked: on, 'aria-label': `Inclure la catégorie ${cat.name}` });
      box.addEventListener('change', () => {
        if (box.checked) state.selected.add(cat.category_id); else state.selected.delete(cat.category_id);
        refresh({ channels: false });
      });
      fragment.append(h('label', { class: `cat${on ? ' on' : ''}` },
        box,
        h('span', { class: 'grow' }, h('span', { class: 'name', text: cat.name }),
          parent ? h('span', { class: 'meta', text: `Sous-groupe de ${parent}` }) : null,
          h('span', { class: `meta${partial ? ' partial' : ''}`, text: metaText })),
        cat.count ? h('button', {
          type: 'button', class: 'view', text: 'Voir', title: `Voir les ${K().many} de ${cat.name}`,
          onclick: (event) => { event.preventDefault(); showCategoryChannels(cat.category_id); },
        }) : null,
      ));
    }
    grid.append(fragment);
    if (!visible.length) grid.append(h('p', { class: 'empty', text: 'Aucune catégorie ne correspond au filtre.' }));
    $('cat-info').textContent = `${plural(state.categories.length, 'catégorie', 'catégories')} · ${noun(state.channels.length)} au total · ${visible.length} affichée${visible.length > 1 ? 's' : ''}`;
  }

  function filteredChannels() {
    const query = norm($('search').value.trim());
    const terms = query.split(/\s+/).filter(Boolean);
    const category = $('ch-category').value;
    const mode = $('ch-state').value;
    return state.channels.filter((c) => {
      if (category && !c.category_ids.includes(category)) return false;
      if (terms.length && !terms.every((t) => c._search.includes(t))) return false;
      switch (mode) {
        case 'on': return isOn(c);
        case 'off': return !isOn(c);
        case 'included': return state.included.has(c.stream_id);
        case 'excluded': return state.excluded.has(c.stream_id);
        default: return true;
      }
    });
  }

  function thumb(c) {
    const box = h('span', { class: `thumb${state.kind === 'live' ? '' : ' poster'}`, 'aria-hidden': 'true', text: c.name.replace(/[^\p{L}\p{N}]/gu, '').slice(0, 2).toUpperCase() || 'TV' });
    if (c.logo) {
      const img = h('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' });
      img.addEventListener('load', () => { box.textContent = ''; box.append(img); }, { once: true });
      img.addEventListener('error', () => img.remove(), { once: true });
      img.src = c.logo;
    }
    return box;
  }

  function renderChannels() {
    const list = $('channels');
    list.replaceChildren();
    if (!state.catalogLoaded) {
      list.append(h('li', { class: 'empty', text: `Chargement du catalogue ${K().label.toLowerCase()}…` }));
      return;
    }
    const found = filteredChannels();
    const visible = found.slice(0, state.limit);
    const fragment = document.createDocumentFragment();
    for (const c of visible) {
      const on = isOn(c);
      const box = h('input', { type: 'checkbox', checked: on, 'aria-label': `Retenir ${c.name}` });
      box.addEventListener('change', () => {
        setChannel(c, box.checked);
        refresh({ channels: $('ch-state').value !== 'all' ? true : 'row', row: box.closest('label'), channel: c });
      });
      fragment.append(h('li', null, h('label', { class: `channel${on ? ' on' : ''}` },
        box,
        thumb(c),
        h('span', { class: 'grow' },
          h('span', { class: 'ch-name', text: c.name }),
          h('span', { class: 'ch-meta', text: itemMeta(c) }),
          state.kind === 'series' ? episodesButton(c) : null),
        h('span', { class: 'badges' }, badgesFor(c)),
      )));
    }
    list.append(fragment);
    if (!visible.length) list.append(h('li', { class: 'empty', text: `Aucun résultat parmi les ${K().many} avec ces critères.` }));
    $('shown').textContent = found.length > visible.length
      ? `${visible.length.toLocaleString('fr-FR')} ${K().many} affichés sur ${found.length.toLocaleString('fr-FR')} résultats`
      : plural(found.length, 'résultat', 'résultats');
    $('more').hidden = found.length <= visible.length;
    $('more').textContent = `Afficher ${Math.min(PAGE, found.length - visible.length)} ${K().many} de plus`;
    $('ch-check').disabled = $('ch-uncheck').disabled = !found.length;
  }

  function itemMeta(c) {
    const cats = c.category_ids.map((id) => state.categoryNames.get(id) || 'Sans catégorie').join(' · ');
    if (state.kind === 'live') return (c.num ? `N° ${c.num} · ` : '') + cats;
    return [c.year, c.rating ? `★ ${c.rating}` : '', cats, c.ext ? c.ext.toUpperCase() : ''].filter(Boolean).join(' · ');
  }

  function badgesFor(c) {
    const badges = [];
    if (state.included.has(c.stream_id)) badges.push(h('span', { class: 'badge ok', text: K().added }));
    if (state.excluded.has(c.stream_id)) badges.push(h('span', { class: 'badge danger', text: K().excludedLabel }));
    if (c.epg) badges.push(h('span', { class: 'badge opt', text: 'EPG', title: 'Identifiant EPG disponible' }));
    if (c.archive) badges.push(h('span', { class: 'badge opt', text: `Replay ${c.archive} j`, title: 'Programmes passés disponibles (catch-up)' }));
    return badges;
  }

  /** Bouton « Épisodes » : appelle get_series_info (via /api/series/<id>) à la demande. */
  function episodesButton(c) {
    const btn = h('button', { type: 'button', class: 'link-btn', text: 'Voir les épisodes' });
    btn.addEventListener('click', async (event) => {
      event.preventDefault();
      event.stopPropagation();
      btn.disabled = true;
      btn.textContent = 'Chargement des épisodes…';
      try {
        const data = await request(`/api/series/${encodeURIComponent(c.stream_id)}`);
        const seasons = new Set(data.episodes.map((e) => e.season));
        btn.replaceWith(h('span', { class: 'ch-meta ok-text', text: data.episodes.length
          ? `${plural(seasons.size, 'saison', 'saisons')} · ${plural(data.episodes.length, 'épisode', 'épisodes')}`
          : 'Aucun épisode renvoyé par le fournisseur' }));
      } catch (error) {
        btn.disabled = false;
        btn.textContent = `Épisodes indisponibles (${error.message}) – réessayer`;
      }
    });
    return btn;
  }

  function updateRow(label, c) {
    if (!label) return;
    label.classList.toggle('on', isOn(c));
    label.querySelector('.badges').replaceChildren(...badgesFor(c));
  }

  function groupCounts() {
    const groups = new Map();
    for (const kind of KINDS) {
      const sel = state.sel[kind];
      for (const c of state.cat[kind].channels) {
        if (sel.excluded.has(c.stream_id)) continue;
        const inCat = c.category_ids.find((id) => sel.selected.has(id));
        if (!inCat && !sel.included.has(c.stream_id)) continue;
        const key = KIND[kind].prefix + (inCat || c.category_id);
        groups.set(key, (groups.get(key) || 0) + 1);
      }
    }
    return groups;
  }

  function orderedGroups(groups) {
    // Ordre par défaut : direct, puis films, puis séries, chacun dans l'ordre du fournisseur.
    const provider = new Map();
    KINDS.forEach((kind, k) => state.cat[kind].categories.forEach((cat, i) => provider.set(KIND[kind].prefix + cat.category_id, k * 1e6 + i)));
    const custom = new Map(state.groupOrder.map((id, i) => [id, i]));
    const rank = (id) => (custom.has(id) ? [0, custom.get(id)] : [custom.size ? 1 : 0, provider.get(id) ?? 1e9]);
    return [...groups.keys()].sort((a, b) => {
      const [ra, rb] = [rank(a), rank(b)];
      return ra[0] - rb[0] || ra[1] - rb[1];
    });
  }

  function moveGroup(order, index, delta) {
    const target = index + delta;
    if (target < 0 || target >= order.length) return;
    [order[index], order[target]] = [order[target], order[index]];
    state.groupOrder = order;
    renderSummary();
    renderStats();
    const buttons = $('sum-groups').children[target]?.querySelectorAll('.icon-btn');
    if (buttons) (delta < 0 ? buttons[0] : buttons[1]).focus();
  }

  function renderSummary() {
    const groups = groupCounts();
    const order = orderedGroups(groups);
    const gl = $('sum-groups');
    gl.replaceChildren();
    order.forEach((id, index) => {
      const info = groupInfo(id);
      const original = info.name;
      const input = h('input', { type: 'text', maxlength: '100', value: state.groupNames[id] || '', placeholder: original,
        'aria-label': `Nom du groupe ${original} dans cette playlist`, class: state.groupNames[id] ? 'renamed' : null });
      input.addEventListener('input', () => {
        state.groupNames[id] = input.value;
        input.classList.toggle('renamed', Boolean(input.value.trim()));
        renderStats();
      });
      gl.append(h('li', { class: 'group-row' },
        h('span', { class: 'pos', text: String(index + 1) }),
        h('span', { class: `kind-tag ${info.kind}`, text: KIND[info.kind].label, title: `Groupe de ${KIND[info.kind].many}` }),
        input,
        h('span', { class: 'num', text: groups.get(id).toLocaleString('fr-FR'), title: KIND[info.kind].title }),
        h('button', { type: 'button', class: 'icon-btn', text: '↑', title: 'Monter', 'aria-label': `Monter ${original}`, disabled: index === 0, onclick: () => moveGroup(order, index, -1) }),
        h('button', { type: 'button', class: 'icon-btn', text: '↓', title: 'Descendre', 'aria-label': `Descendre ${original}`, disabled: index === order.length - 1, onclick: () => moveGroup(order, index, 1) }),
      ));
    });
    if (!order.length) gl.append(h('li', { class: 'empty', text: 'La playlist est vide pour le moment.' }));
    $('reset-groups').disabled = !state.groupOrder.length && !Object.keys(cleanGroupNames()).length;

    const fill = (node, field, emptyText) => {
      node.replaceChildren();
      let total = 0;
      for (const kind of KINDS) {
        const set = state.sel[kind][field];
        for (const id of set) {
          total += 1;
          const c = state.cat[kind].channelById.get(id);
          const label = c ? c.name : (state.cat[kind].catalogLoaded ? `${KIND[kind].one} ${id} (absent du catalogue actuel)` : `${KIND[kind].one} ${id}`);
          node.append(h('li', null,
            h('span', null, h('span', { class: `kind-tag ${kind}`, text: KIND[kind].label }), ' ', label),
            h('button', { type: 'button', class: 'x', title: 'Retirer', 'aria-label': `Retirer ${label}`, text: '×', onclick: () => { set.delete(id); refresh(); } })));
        }
      }
      if (!total) node.append(h('li', { class: 'empty', text: emptyText }));
    };
    fill($('sum-included'), 'included', 'Aucun ajout hors catégories.');
    fill($('sum-excluded'), 'excluded', 'Aucune exclusion.');
  }

  function renderCategorySelect() {
    const select = $('ch-category');
    const current = select.value;
    select.replaceChildren(h('option', { value: '', text: 'Toutes les catégories' }));
    for (const cat of state.categories) {
      select.append(h('option', { value: cat.category_id, text: `${cat.name} (${cat.count})` }));
    }
    select.value = state.categoryNames.has(current) ? current : '';
  }

  function refresh(options = {}) {
    const { channels = true, row = null, channel = null } = options;
    renderStats();
    if (state.tab === 'categories' || channels === true) renderCategories();
    if (channels === true) renderChannels();
    else if (channels === 'row') updateRow(row, channel);
    if (state.tab === 'summary') renderSummary();
    renderUrl();
    renderPlaylists();
  }

  // ------------------------------------------------------------ onglets
  function selectTab(name) {
    state.tab = name;
    for (const tab of ['categories', 'channels', 'summary']) {
      const active = tab === name;
      $(`tab-${tab}`).setAttribute('aria-selected', String(active));
      $(`tab-${tab}`).tabIndex = active ? 0 : -1;
      $(`panel-${tab}`).hidden = !active;
    }
    if (name === 'categories') renderCategories();
    if (name === 'channels') renderChannels();
    if (name === 'summary') renderSummary();
  }

  function showCategoryChannels(id) {
    $('ch-category').value = id;
    $('ch-state').value = 'all';
    $('search').value = '';
    state.limit = PAGE;
    selectTab('channels');
    $('tab-channels').focus();
  }

  // ------------------------------------------------------- playlists
  function load(p) {
    state.editing = p ? p.slug : null;
    for (const kind of KINDS) {
      const [catField, incField, excField] = KIND[kind].fields;
      state.sel[kind] = {
        selected: new Set(p ? p[catField] || [] : []),
        included: new Set(p ? p[incField] || [] : []),
        excluded: new Set(p ? p[excField] || [] : []),
      };
    }
    $('opt-live-format').value = (p && p.live_format) || 'ts';
    $('opt-catchup').checked = !p || p.catchup !== false;
    renderOptionsPill();
    state.groupOrder = p && p.group_order ? [...p.group_order] : [];
    state.groupNames = p && p.group_names ? { ...p.group_names } : {};
    state.slugTouched = Boolean(p);
    $('name').value = p ? p.name : '';
    $('slug').value = p ? p.slug : '';
    $('slug').disabled = Boolean(p);
    $('editor-title').textContent = p ? `Modifier « ${p.name} »` : 'Nouvelle playlist';
    $('delete').hidden = $('duplicate').hidden = !p;
    $('save').replaceChildren(p ? 'Enregistrer' : 'Créer', h('span', { class: 'hide-sm', text: p ? 'les modifications' : 'la playlist' }));
    state.snapshot = signature();
    state.limit = PAGE;
    clearError();
    refresh();
    loadEpg();
    // Charger en arrière-plan les catalogues films / séries utilisés par cette playlist (compteurs, résumé).
    if (p) for (const kind of ['vod', 'series']) if (usesKind(p, kind)) ensureCatalog(kind);
  }

  async function confirmLeave() {
    if (!isDirty() || $('dirty').hidden) return true;
    return Boolean(await ask({
      title: 'Modifications non enregistrées',
      text: 'Les changements de la playlist en cours seront perdus.',
      okLabel: 'Abandonner les changements', danger: true,
    }));
  }

  async function openPlaylist(slug) {
    if (slug === state.editing) return;
    if (!(await confirmLeave())) return;
    load(state.playlists.find((p) => p.slug === slug) || null);
    $('editor').focus({ preventScroll: true });
    if (window.matchMedia('(max-width: 980px)').matches) $('editor').scrollIntoView({ behavior: 'smooth' });
  }

  async function save() {
    clearError();
    const data = payload();
    if (!data.name) { showError('Donne un nom à la playlist.'); $('name').focus(); return; }
    if (!state.editing && !/^[a-z0-9][a-z0-9_-]{0,59}$/.test(data.slug)) {
      showError("L'adresse doit commencer par une lettre ou un chiffre et ne contenir que a-z, 0-9, « - » ou « _ ».");
      $('slug').focus();
      return;
    }
    if (!hasSelection() && !(await ask({ title: 'Playlist vide', text: 'Aucune catégorie, chaîne, film ou série n\'est sélectionné. Enregistrer quand même ?', okLabel: 'Enregistrer' }))) return;
    $('save').disabled = true;
    try {
      const saved = state.editing
        ? await request(`/api/playlists/${encodeURIComponent(state.editing)}`, 'PUT', data)
        : await request('/api/playlists', 'POST', data);
      state.playlists = await request('/api/playlists');
      load(state.playlists.find((p) => p.slug === saved.slug) || saved);
      toast(`Playlist « ${saved.name} » enregistrée`);
    } catch (error) {
      showError(error.message);
      toast(error.message, 'error');
    } finally {
      $('save').disabled = false;
    }
  }

  async function remove() {
    if (!state.editing) return;
    const p = state.playlists.find((x) => x.slug === state.editing);
    const ok = await ask({
      title: 'Supprimer la playlist ?',
      text: `« ${p ? p.name : state.editing} » sera supprimée et son URL ne fonctionnera plus dans les lecteurs.`,
      okLabel: 'Supprimer', danger: true,
    });
    if (!ok) return;
    try {
      await request(`/api/playlists/${encodeURIComponent(state.editing)}`, 'DELETE');
      state.playlists = await request('/api/playlists');
      toast('Playlist supprimée');
      load(null);
    } catch (error) { showError(error.message); }
  }

  async function duplicate() {
    if (!state.editing) return;
    const base = $('name').value.trim() || state.editing;
    const name = await ask({ title: 'Dupliquer la playlist', text: 'La copie aura sa propre URL.', okLabel: 'Dupliquer', input: { label: 'Nom de la copie', value: `${base} (copie)` } });
    if (!name || !name.trim()) return;
    let slug = slugify(name) || 'playlist';
    const taken = new Set(state.playlists.map((p) => p.slug));
    for (let i = 2; taken.has(slug); i += 1) slug = `${slugify(name).slice(0, 55) || 'playlist'}-${i}`;
    try {
      const saved = await request('/api/playlists', 'POST', { ...payload(), name: name.trim(), slug });
      state.playlists = await request('/api/playlists');
      load(state.playlists.find((p) => p.slug === saved.slug) || saved);
      toast(`Copie « ${saved.name} » créée`);
    } catch (error) { showError(error.message); }
  }

  // ------------------------------------------------ catalogue fournisseur
  const VIEWS = { playlists: '#', catalog: '#catalogue', devices: '#appareils', system: '#systeme' };

  function switchView(view) {
    if (!VIEWS[view]) view = 'playlists';
    state.view = view;
    for (const name of Object.keys(VIEWS)) {
      const panel = $(`view-${name}`);
      if (panel) panel.hidden = name !== view;
      const btn = $(`view-btn-${name}`);
      if (btn) btn.setAttribute('aria-pressed', String(name === view));
    }
    document.body.classList.toggle('no-actionbar', view !== 'playlists');
    if (view === 'catalog') renderProvider();
    if (view === 'system') loadSystem();
    if (view === 'devices') loadDevices();
    try { history.replaceState(null, '', VIEWS[view]); } catch (_) { /* ignore */ }
  }

  // ------------------------------------------------------------ appareils
  let devicesData = null;

  function credRow(label, value, { secret = false } = {}) {
    const code = h('code', { class: secret ? 'secret' : null, text: value });
    return h('div', { class: 'cred' }, h('span', { class: 'k', text: label }), code,
      h('button', { type: 'button', class: 'btn small', text: 'Copier', onclick: () => copyText(value) }));
  }

  function renderDevicePlaylistSelect(select, value) {
    select.replaceChildren(h('option', { value: '', text: 'Tout le catalogue (sans séries en M3U)' }));
    for (const p of [...state.playlists].sort((a, b) => collator.compare(a.name, b.name))) {
      select.append(h('option', { value: p.slug, text: `${p.name} (/p/${p.slug}.m3u)` }));
    }
    if (value && !state.playlists.some((p) => p.slug === value)) {
      select.append(h('option', { value, text: `${value} (playlist supprimée !)` }));
    }
    select.value = value || '';
  }

  function deviceCard(d) {
    const base = baseUrl();
    const server = base.replace(/\/$/, '');
    const qs = new URLSearchParams({ username: d.username, password: d.password });
    const m3u = d.playlist ? `${server}/p/${d.playlist}.m3u?token=${encodeURIComponent(d.password)}`
      : `${server}/get.php?${qs}&type=m3u_plus&output=ts`;
    const epg = d.playlist ? `${server}/p/${d.playlist}.xml?token=${encodeURIComponent(d.password)}` : `${server}/xmltv.php?${qs}`;
    const select = h('select', { 'aria-label': `Playlist de ${d.name}` });
    renderDevicePlaylistSelect(select, d.playlist);
    select.addEventListener('change', async () => {
      try { await request(`/api/devices/${d.id}`, 'PUT', { playlist: select.value }); toast('Playlist de l\'appareil modifiée'); loadDevices(); }
      catch (error) { toast(error.message, 'error'); }
    });
    const seen = d.last_seen ? `vu le ${new Date(d.last_seen * 1000).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' })}` : 'jamais utilisé depuis le démarrage';
    return h('section', { class: 'card device-card' },
      h('div', { class: 'card-head' },
        h('div', null, h('h3', { text: d.name }), h('p', { class: 'muted small', text: `Identifiant ${d.username} · ${seen}` })),
        h('div', { class: 'btn-row' },
          h('button', { type: 'button', class: 'btn small', text: 'Renommer', onclick: async () => {
            const name = await ask({ title: 'Renommer l\'appareil', okLabel: 'Renommer', input: { label: 'Nom', value: d.name } });
            if (!name) return;
            try { await request(`/api/devices/${d.id}`, 'PUT', { name }); loadDevices(); } catch (error) { toast(error.message, 'error'); }
          } }),
          h('button', { type: 'button', class: 'btn small', text: 'Nouveau code', onclick: async () => {
            if (!(await ask({ title: 'Générer un nouveau code ?', text: `L'ancien code de « ${d.name} » ne fonctionnera plus : il faudra mettre à jour l'appareil.`, okLabel: 'Générer', danger: true }))) return;
            try { await request(`/api/devices/${d.id}`, 'PUT', { regenerate: true }); toast('Nouveau code généré'); loadDevices(); } catch (error) { toast(error.message, 'error'); }
          } }),
          h('button', { type: 'button', class: 'btn small danger', text: 'Supprimer', onclick: async () => {
            if (!(await ask({ title: 'Supprimer l\'appareil ?', text: `« ${d.name} » perdra immédiatement l'accès.`, okLabel: 'Supprimer', danger: true }))) return;
            try { await request(`/api/devices/${d.id}`, 'DELETE'); toast('Appareil supprimé'); loadDevices(); } catch (error) { toast(error.message, 'error'); }
          } }))),
      h('div', { class: 'field' }, h('label', { text: 'Playlist de cet appareil' }), select),
      h('div', { class: 'device-grid' },
        h('div', { class: 'device-mode' },
          h('h4', { text: '📺 Mode « Playlist M3U » (VLC, TiviMate, Android TV…)' }),
          credRow('Playlist', m3u), credRow('Guide (EPG)', epg)),
        h('div', { class: 'device-mode' },
          h('h4', { text: '🔐 Mode « Xtream Codes » (TiviMate, IPTV Smarters…)' }),
          credRow('Serveur', server), credRow('Identifiant', d.username), credRow('Mot de passe', d.password, { secret: true }),
          h('p', { class: 'hint', text: 'Rubriques Direct / Films / Séries natives du lecteur, guide et replay inclus.' }))));
  }

  async function loadDevices() {
    try { devicesData = await request('/api/devices'); } catch (error) { showError(error.message); return; }
    renderDevicePlaylistSelect($('device-playlist'), $('device-playlist').value);
    $('require-token').checked = devicesData.require_token;
    $('require-token-hint').textContent = devicesData.access_token_env
      ? 'ACCESS_TOKEN est défini dans la configuration : un code est toujours exigé (ACCESS_TOKEN ou code d\'appareil).'
      : devicesData.require_token
        ? 'Les URL sans code (ou avec un code supprimé) sont refusées. Les liens générés reprennent le code de l\'appareil.'
        : 'Option désactivée : les playlists restent accessibles sans code sur ton réseau local. Les identifiants Xtream restent toujours nécessaires.';
    const list = $('devices-list');
    list.replaceChildren(...devicesData.devices.map(deviceCard));
    if (!devicesData.devices.length) list.append(h('p', { class: 'empty', text: 'Aucun appareil pour le moment. Ajoute ta première TV ci-dessus.' }));
  }

  // ------------------------------------------------------------- système
  const KIND_LABEL = { live: 'Direct', movie: 'Film', series: 'Épisode', hls: 'Direct (HLS)', timeshift: 'Replay' };
  let streamsTimer = null;

  function formatBytes(n) {
    if (n >= 1e9) return `${(n / 1e9).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} Go`;
    if (n >= 1e6) return `${(n / 1e6).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} Mo`;
    return `${Math.round(n / 1e3).toLocaleString('fr-FR')} Ko`;
  }
  function formatDuration(sec) {
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h ? `${h} h ${String(m).padStart(2, '0')}` : m ? `${m} min ${String(s).padStart(2, '0')}` : `${s} s`;
  }

  async function loadStreams() {
    clearTimeout(streamsTimer);
    let data;
    try { data = await request('/api/streams'); } catch (_) { data = null; }
    if (data) renderStreams(data);
    const delay = state.view === 'system' ? 3000 : 15000;
    if (!document.hidden) streamsTimer = setTimeout(loadStreams, delay);
  }

  function renderStreams(data) {
    const n = data.streams.length;
    $('streams-pill').hidden = !n;
    $('streams-pill-text').textContent = `${n} lecture${n > 1 ? 's' : ''}`;
    const limitText = data.limit
      ? `Limite : ${plural(data.limit, 'connexion simultanée', 'connexions simultanées')}${data.auto ? ' (valeur de l\'abonnement)' : ' (MAX_CONNECTIONS)'}${data.takeover ? '' : ' · reprise automatique désactivée'}.`
      : 'Aucune limite de connexions appliquée (MAX_CONNECTIONS=0 ou valeur de l\'abonnement inconnue).';
    $('streams-limit').textContent = limitText;
    const list = $('streams-list');
    list.replaceChildren();
    for (const s of data.streams) {
      list.append(h('li', { class: 'stream-item' },
        h('span', { class: 'dot live-dot' }),
        h('span', { class: 'grow' },
          h('span', { class: 's-title', text: s.label || s.item_id }),
          h('span', { class: 's-meta', text: `${KIND_LABEL[s.kind] || s.kind} · appareil ${s.client} · depuis ${formatDuration(s.seconds)} · ${formatBytes(s.bytes)}` })),
        h('button', { type: 'button', class: 'btn small danger', text: 'Arrêter', onclick: async (event) => {
          event.target.disabled = true;
          try { await request(`/api/streams/${s.id}/stop`, 'POST'); toast('Lecture arrêtée'); } catch (error) { toast(error.message, 'error'); }
          loadStreams();
        } })));
    }
    if (!n) list.append(h('li', { class: 'empty', text: 'Aucune lecture en cours.' }));
  }

  function loadSystem() {
    loadStreams();
  }

  function providerChannelList(list, limit = 300, withCategories = false) {
    const ul = h('ul', { class: 'prov-channels' });
    const render = (max) => {
      ul.replaceChildren();
      for (const c of list.slice(0, max)) {
        const extra = state.kind === 'live' ? null : [c.year, c.rating ? `★ ${c.rating}` : ''].filter(Boolean).join(' · ');
        ul.append(h('li', null,
          h('span', { class: 'c-num', text: c.num ? `N° ${c.num}` : '—' }),
          h('span', { class: 'c-name' }, c.name, extra ? h('span', { class: 'ch-meta', text: ` (${extra})` }) : null, withCategories
            ? h('span', { class: 'ch-meta', text: ` — ${c.category_ids.map((id) => state.categoryNames.get(id) || id).join(' · ')}` })
            : null, state.kind === 'series' ? h('span', null, ' ', episodesButton(c)) : null),
          c.epg ? h('span', { class: 'badge', text: 'EPG' }) : null,
          c.ext ? h('span', { class: 'badge', text: c.ext.toUpperCase() }) : null,
          h('span', { class: 'c-id', text: `id ${c.stream_id}` })));
      }
      if (list.length > max) {
        ul.append(h('li', null, h('button', { type: 'button', class: 'btn small ghost', text: `Afficher les ${list.length - max} autres ${K().many}`, onclick: () => render(list.length) })));
      }
    };
    render(limit);
    return ul;
  }

  function providerNode(cat, children, depth) {
    const count = cat.count;
    const meta = h('span', { class: 't-meta' },
      h('span', { class: 'badge', text: `ID ${cat.category_id}` }),
      cat.parent_id ? h('span', { class: 'badge', text: `parent ${cat.parent_id}`, title: `parent_id = ${cat.parent_id}` }) : null,
      h('span', { class: `badge${count ? '' : ' warn'}`, text: noun(count) }),
      cat.source === 'derived' ? h('span', { class: 'badge danger', text: `absente de ${K().categoriesAction}`, title: `Groupe déduit des contenus : la catégorie n'est pas renvoyée par ${K().categoriesAction}` }) : null);
    const body = h('div', { class: 'tree-body' });
    const node = h('details', { class: 'tree-node', 'data-depth': String(depth) },
      h('summary', null, h('span', { class: 't-pos', text: `#${cat.position + 1}` }), h('span', { class: 't-name', text: cat.name }), meta),
      body);
    let rendered = false;
    node.addEventListener('toggle', () => {
      if (!node.open || rendered) return;
      rendered = true;
      const list = state.channelsByCat.get(cat.category_id) || [];
      if (list.length) body.append(providerChannelList(list));
      else if (!(children.get(cat.category_id) || []).length) body.append(h('p', { class: 'empty', text: `Aucun contenu dans cette catégorie pour ton compte.` }));
      for (const child of children.get(cat.category_id) || []) body.append(providerNode(child, children, depth + 1));
    });
    return node;
  }

  function renderProvider() {
    const tree = $('provider-tree');
    const results = $('prov-results');
    tree.replaceChildren();
    results.replaceChildren();
    $('prov-actions').textContent = K().actions;
    $('export').href = `/api/catalog/export?kind=${state.kind}`;
    $('export-raw').href = `/api/catalog?kind=${state.kind}&refresh=1`;
    if (!state.catalogLoaded) {
      $('catalog-stats').replaceChildren();
      tree.append(h('p', { class: 'empty', text: `Chargement du catalogue ${K().label.toLowerCase()}…` }));
      return;
    }
    const warning = $('catalog-warning');
    warning.hidden = !state.categoriesError;
    warning.textContent = state.categoriesError
      ? `${state.categoriesError}. Les groupes affichés sont déduits des contenus : ils peuvent différer de ceux de ton application TV. Réessaie avec « Actualiser le catalogue ».`
      : '';
    const known = new Set(state.categories.map((c) => c.category_id));
    const children = new Map();
    const roots = [];
    for (const cat of state.categories) {
      if (cat.parent_id && known.has(cat.parent_id) && cat.parent_id !== cat.category_id) {
        if (!children.has(cat.parent_id)) children.set(cat.parent_id, []);
        children.get(cat.parent_id).push(cat);
      } else roots.push(cat);
    }
    const subgroups = state.categories.length - roots.length;
    const empty = state.categories.filter((c) => !c.count).length;
    const derived = state.categories.filter((c) => c.source === 'derived').length;
    const stat = (value, label) => h('div', { class: 'stat' }, h('strong', { text: value.toLocaleString('fr-FR') }), h('span', { text: label }));
    $('catalog-stats').replaceChildren(
      stat(state.categories.length - derived, 'catégories fournisseur'),
      stat(subgroups, 'sous-groupes (parent_id)'),
      stat(empty, 'catégories vides'),
      stat(state.channels.length, `${K().many} valides`));

    const query = norm($('prov-search').value.trim());
    if (query) {
      const cats = state.categories.filter((c) => c._search.includes(query) || c.category_id === query);
      const chans = state.channels.filter((c) => c._search.includes(query) || c.stream_id === query || (c.num && c.num === query));
      results.className = 'prov-results';
      results.append(h('h3', { text: `${plural(cats.length, 'catégorie trouvée', 'catégories trouvées')}` }));
      const box = h('div', { class: 'tree' });
      for (const cat of cats.slice(0, 100)) box.append(providerNode(cat, children, 0));
      results.append(box);
      results.append(h('h3', { text: `${noun(chans.length)} trouvé${state.kind === 'vod' ? '' : 'e'}${chans.length > 1 ? 's' : ''}` }));
      if (chans.length) {
        results.append(providerChannelList(chans, 200, true));
      }
      $('prov-info').textContent = 'Résultats de recherche. Efface la recherche pour revoir l\'arborescence complète.';
      return;
    }
    $('prov-info').textContent = `${K().label} · ordre exact du fournisseur · ${plural(roots.length, 'groupe racine', 'groupes racines')}. Clique sur une catégorie pour voir son contenu et ses sous-groupes.`;
    const fragment = document.createDocumentFragment();
    for (const cat of roots) fragment.append(providerNode(cat, children, 0));
    tree.append(fragment);
  }

  // --------------------------------------------------------- chargement
  function setConnection(kind, text) {
    $('connection').className = `pill ${kind}`;
    $('connection-text').textContent = text;
  }

  function applyCatalog(catalog) {
    const kind = catalog.kind || 'live';
    const target = state.cat[kind];
    target.categories = catalog.categories.map((cat) => ({ ...cat, _search: `${norm(cat.name)} #${cat.category_id}` }));
    target.categoryNames = new Map(target.categories.map((cat) => [cat.category_id, cat.name]));
    target.channels = catalog.channels.map((c) => ({ ...c, _search: norm(`${c.name} ${c.year || ''}`) }));
    target.channelById = new Map(target.channels.map((c) => [c.stream_id, c]));
    target.channelsByCat = new Map();
    for (const c of target.channels) {
      for (const id of c.category_ids) {
        if (!target.channelsByCat.has(id)) target.channelsByCat.set(id, []);
        target.channelsByCat.get(id).push(c);
      }
    }
    target.catalogLoaded = true;
    target.categoriesError = catalog.categories_error || null;
    if (kind === state.kind) {
      if (state.view === 'catalog') renderProvider();
      renderCategorySelect();
    }
    if (kind === 'live') {
      const when = catalog.updated_at ? new Date(catalog.updated_at * 1000).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' }) : '';
      setConnection('ok', `Connecté · ${noun(target.channels.length, 'live')}${when ? ` · ${when}` : ''}`);
    }
  }

  async function loadCatalog(force = false, kind = state.kind) {
    try {
      applyCatalog(await request(`/api/catalog?kind=${kind}${force ? '&refresh=1' : ''}`));
      if (force) toast(`Catalogue ${KIND[kind].label.toLowerCase()} actualisé`);
      return true;
    } catch (error) {
      if (kind === 'live') setConnection('error', 'Fournisseur injoignable');
      showError(`Catalogue ${KIND[kind].label.toLowerCase()} indisponible : ${error.message}`);
      return false;
    }
  }

  /** Charge un catalogue une seule fois (requêtes simultanées partagées), puis rafraîchit l'affichage. */
  function ensureCatalog(kind) {
    const target = state.cat[kind];
    if (target.catalogLoaded) return Promise.resolve(true);
    if (!target.loading) {
      target.loading = loadCatalog(false, kind).then((ok) => { target.loading = null; refresh(); return ok; });
    }
    return target.loading;
  }

  async function switchKind(kind) {
    if (!KIND[kind] || kind === state.kind) return;
    state.kind = kind;
    state.limit = PAGE;
    $('search').value = '';
    $('cat-search').value = '';
    $('prov-search').value = '';
    $('ch-state').value = 'all';
    document.querySelectorAll('[data-kind]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.kind === kind)));
    $('tab-items-label').textContent = K().title;
    $('kind-title').textContent = `${K().icon} ${K().label} : choisis les catégories et les ${K().many}`;
    $('search').placeholder = `Rechercher ${state.kind === 'live' ? 'une chaîne' : state.kind === 'vod' ? 'un film' : 'une série'}…`;
    renderCategorySelect();
    refresh();
    if (state.view === 'catalog') renderProvider();
    await ensureCatalog(kind);
    if (state.view === 'catalog') renderProvider();
  }

  async function loadAccount() {
    try {
      const status = await request('/api/status?account=1');
      const a = status.account;
      if (!a) return;
      const parts = [];
      if (a.status) parts.push(a.status === 'Active' ? 'Abonnement actif' : `Abonnement : ${a.status}`);
      if (a.exp_date) parts.push(`expire le ${new Date(a.exp_date * 1000).toLocaleDateString('fr-FR')}`);
      if (a.max_connections) parts.push(`${a.active_connections || 0}/${a.max_connections} connexion${a.max_connections === '1' ? '' : 's'}`);
      if (parts.length) {
        $('account').textContent = parts.join(' · ');
        $('account').title = 'Informations renvoyées par le fournisseur (player_api.php)';
        $('account').hidden = false;
      }
    } catch (_) { /* facultatif */ }
  }

  async function init() {
    const [status, playlists] = await Promise.allSettled([request('/api/status'), request('/api/playlists')]);
    if (status.status === 'fulfilled') {
      state.status = status.value;
      $('version').textContent = `version ${state.status.version}`;
      if (state.status.token_required && !state.status.access_token) {
        $('token-card').hidden = false;
        $('token').value = storage('xtream-m3u-token');
      }
    } else {
      showError(status.reason.message);
    }
    if (playlists.status === 'fulfilled') state.playlists = playlists.value;
    else showError(playlists.reason.message);
    load(state.playlists.length ? [...state.playlists].sort((a, b) => collator.compare(a.name, b.name))[0] : null);
    const fromHash = Object.keys(VIEWS).find((v) => VIEWS[v] === location.hash);
    if (fromHash && fromHash !== 'playlists') switchView(fromHash);
    loadStreams();
    if (await loadCatalog(false, 'live')) refresh();
    loadSummary();
    loadAccount();
  }

  // ---------------------------------------------------------- événements
  $('new').addEventListener('click', async () => {
    if (!(await confirmLeave())) return;
    load(null);
    $('name').focus();
  });
  $('save').addEventListener('click', save);
  $('opt-live-format').addEventListener('change', () => { renderOptionsPill(); renderStats(); });
  $('opt-catchup').addEventListener('change', () => { renderOptionsPill(); renderStats(); });
  $('epg-refresh').addEventListener('click', refreshEpg);
  document.querySelectorAll('[data-kind]').forEach((btn) => btn.addEventListener('click', () => switchKind(btn.dataset.kind)));
  $('view-btn-playlists').addEventListener('click', () => switchView('playlists'));
  $('view-btn-catalog').addEventListener('click', () => switchView('catalog'));
  $('view-btn-system').addEventListener('click', () => switchView('system'));
  $('view-btn-devices').addEventListener('click', () => switchView('devices'));
  $('device-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = $('device-name').value.trim();
    if (!name) { $('device-name').focus(); return; }
    try {
      const device = await request('/api/devices', 'POST', { name, playlist: $('device-playlist').value });
      $('device-name').value = '';
      toast(`Appareil « ${device.name} » ajouté`);
      loadDevices();
    } catch (error) { toast(error.message, 'error'); }
  });
  $('require-token').addEventListener('change', async () => {
    const value = $('require-token').checked;
    if (value && devicesData && !devicesData.devices.length && !devicesData.access_token_env
        && !(await ask({ title: 'Aucun appareil', text: 'Sans appareil (ni ACCESS_TOKEN), plus aucun lecteur ne pourra lire tes playlists. Continuer ?', okLabel: 'Activer', danger: true }))) {
      $('require-token').checked = false;
      return;
    }
    try {
      await request('/api/devices/settings', 'PUT', { require_token: value });
      const status = await request('/api/status');
      state.status = status;
      $('token-card').hidden = !(status.token_required && !status.access_token);
      toast(value ? 'Code d\'appareil exigé' : 'Code d\'appareil facultatif');
      loadDevices();
    } catch (error) { toast(error.message, 'error'); $('require-token').checked = !value; }
  });
  $('streams-pill').addEventListener('click', () => switchView('system'));
  $('streams-refresh').addEventListener('click', loadStreams);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadStreams(); });
  $('prov-search').addEventListener('input', debounce(renderProvider, 200));
  $('prov-expand').addEventListener('click', () => {
    // Les sous-groupes sont créés à l'ouverture du parent : plusieurs passes successives.
    let passes = 0;
    const pass = () => {
      const closed = [...document.querySelectorAll('#view-catalog details.tree-node:not([open])')];
      closed.forEach((d) => { d.open = true; });
      if (closed.length && (passes += 1) < 8) setTimeout(pass, 60);
    };
    pass();
  });
  $('prov-collapse').addEventListener('click', () => document.querySelectorAll('#view-catalog details.tree-node').forEach((d) => { d.open = false; }));
  $('reset-groups').addEventListener('click', () => { state.groupOrder = []; state.groupNames = {}; renderSummary(); renderStats(); });
  $('delete').addEventListener('click', remove);
  $('duplicate').addEventListener('click', duplicate);
  $('revert').addEventListener('click', () => load(state.playlists.find((p) => p.slug === state.editing) || null));
  $('refresh').addEventListener('click', async () => {
    $('refresh').disabled = true;
    clearError();
    setConnection('', 'Actualisation…');
    if (await loadCatalog(true, state.kind)) { refresh(); if (state.view === 'catalog') renderProvider(); }
    loadSummary();
    $('refresh').disabled = false;
  });
  $('name').addEventListener('input', () => {
    if (!state.editing && !state.slugTouched) $('slug').value = slugify($('name').value);
    renderStats();
    renderUrl();
  });
  $('slug').addEventListener('input', () => {
    state.slugTouched = true;
    const cleaned = $('slug').value.toLowerCase().replace(/[^a-z0-9_-]/g, '');
    if (cleaned !== $('slug').value) $('slug').value = cleaned;
    renderStats();
    renderUrl();
  });
  $('token').addEventListener('input', () => {
    storage('xtream-m3u-token', $('token').value.trim());
    renderUrl();
    if (state.editing) $('epg-url').textContent = withToken(`${baseUrl()}/p/${state.editing}.xml`);
  });
  $('copy').addEventListener('click', () => { if (state.editing) copyText(playlistUrl(state.editing)); });
  document.querySelectorAll('[data-copy]').forEach((btn) => btn.addEventListener('click', () => copyText($(btn.dataset.copy).textContent)));

  for (const tab of ['categories', 'channels', 'summary']) $(`tab-${tab}`).addEventListener('click', () => selectTab(tab));
  document.querySelector('.tabs').addEventListener('keydown', (event) => {
    const tabs = ['categories', 'channels', 'summary'];
    const index = tabs.indexOf(state.tab);
    let next = null;
    if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length];
    if (event.key === 'ArrowLeft') next = tabs[(index + tabs.length - 1) % tabs.length];
    if (next) { event.preventDefault(); selectTab(next); $(`tab-${next}`).focus(); }
  });

  $('cat-search').addEventListener('input', debounce(renderCategories, 120));
  document.querySelectorAll('[data-catfilter]').forEach((btn) => btn.addEventListener('click', () => {
    state.catFilter = btn.dataset.catfilter;
    document.querySelectorAll('[data-catfilter]').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    renderCategories();
  }));
  const visibleCategoryIds = () => {
    const query = norm($('cat-search').value.trim());
    return state.categories.filter((cat) => !query || cat._search.includes(query)).map((cat) => cat.category_id);
  };
  $('cat-all').addEventListener('click', () => { visibleCategoryIds().forEach((id) => state.selected.add(id)); refresh(); });
  $('cat-none').addEventListener('click', () => { visibleCategoryIds().forEach((id) => state.selected.delete(id)); refresh(); });

  const rerenderChannels = () => { state.limit = PAGE; renderChannels(); };
  $('search').addEventListener('input', debounce(rerenderChannels, 150));
  $('ch-category').addEventListener('change', rerenderChannels);
  $('ch-state').addEventListener('change', rerenderChannels);
  $('more').addEventListener('click', () => { state.limit += PAGE; renderChannels(); });
  const bulk = async (on) => {
    const found = filteredChannels();
    if (found.length > 50 && !(await ask({
      title: on ? 'Retenir les résultats' : 'Écarter les résultats',
      text: `${on ? 'Retenir' : 'Écarter'} ${noun(found.length)} correspondant aux filtres actuels ?`,
      okLabel: on ? 'Retenir' : 'Écarter',
    }))) return;
    found.forEach((c) => setChannel(c, on));
    refresh();
  };
  $('ch-check').addEventListener('click', () => bulk(true));
  $('ch-uncheck').addEventListener('click', () => bulk(false));
  $('clear-included').addEventListener('click', () => { KINDS.forEach((k) => state.sel[k].included.clear()); refresh(); });
  $('clear-excluded').addEventListener('click', () => { KINDS.forEach((k) => state.sel[k].excluded.clear()); refresh(); });

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); }
  });
  window.addEventListener('beforeunload', (event) => {
    if (isDirty() && !$('dirty').hidden) { event.preventDefault(); event.returnValue = ''; }
  });

  init();
})();
