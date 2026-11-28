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
    channelsByCat: new Map(), catalogLoaded: false, categoriesError: null, loading: null, raw: null });
  const emptySelection = () => ({ selected: new Set(), included: new Set(), excluded: new Set() });

  // ------------------------------------------------------------------ état
  const state = {
    status: null,
    kind: 'live',            // type affiché : live | vod | series
    cat: { live: emptyCatalog(), vod: emptyCatalog(), series: emptyCatalog() },
    sel: { live: emptySelection(), vod: emptySelection(), series: emptySelection() },
    playlists: [],
    sources: [],             // tous les abonnements configurés [{id, name, mixable, aux}]
    profiles: [],            // espaces membres affichés en haut [{id, name, mixable, aux}]
    openGroups: new Set(),   // blocs « fournisseur » dépliés dans la liste des catégories (playlist mélange)
    openCgDrawers: new Set(),// tiroirs de motifs dépliés dans « Mes groupes »
    fullCardTouched: false,  // l'utilisateur a déplié/replié « Liens bruts complets » à la main
    profile: '1',            // espace membre sélectionné
    accounts: [],            // comptes des abonnements (onglet Système, badge)
    editing: null,           // slug de la playlist ouverte (null = nouvelle)
    groupOrder: [],          // ordre personnalisé des groupes (clés : "12", "vod:3", "series:7")
    groupNames: {},          // noms de groupes personnalisés {clé: nom}
    view: 'playlists',
    sysTab: 'providers',     // sous-onglet Réglages : providers | alerts | network | overview | backup
    slugTouched: false,
    snapshot: '',            // pour détecter les modifications non enregistrées
    catFilter: 'all',
    limit: PAGE,
    tab: 'categories',
    summary: null,           // {live|vod|series: {items, categories, error}} (/api/catalog/summary)
    rules: [],               // règles automatiques [{kind, match, value}]
    favorites: [],           // clés des favoris ("42", "vod:7", "series:9"), dans l'ordre
    itemNames: {},           // noms personnalisés {clé: nom}
    itemNumbers: {},         // numéros des chaînes du direct {id: numéro}
    hiddenCache: null,       // doublons masqués (calculé à la demande)
    customGroups: [],        // groupes perso du direct [{id, name, match: words|exact, patterns}]
    itemGroups: {},          // rangements manuels {id_chaîne: id_groupe | "-" (groupe d'origine)}
    groupCache: null,        // chaînes rangées dans les groupes perso (calculé à la demande)
    itemLogos: {},           // logos de remplacement {clé: URL}
    itemEpg: {},             // guide emprunté {id_chaîne: id_chaîne_source}
    itemXmltv: {},           // guide choisi {id_chaîne: identifiant XMLTV} (suggestions validées)
    itemPinned: {},          // version préférée épinglée {id_canonique: id_version}
    epgSuggestions: null,    // suggestions de guide de la playlist ouverte {id_chaîne: [{id, name, source, …}]}
    section: 'content',      // partie de l'éditeur : content | organize | preview | options | guide
    tvPreview: null,         // dernier aperçu TV calculé par /api/playlists/preview
    tvPreviewKind: 'all',    // filtre de type dans l'aperçu TV : all | live | vod | series
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
    if (key.startsWith(CG)) {
      const group = state.customGroups.find((g) => CG + g.id === key);
      return { kind: 'live', id: key, name: group ? group.name : 'Groupe supprimé', custom: true };
    }
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
  const norm = (text) => String(text || '').normalize('NFKD').replace(/[̀-ͯ]/g, '').toLowerCase();

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

  function applyTheme(mode) {
    const resolved = mode === 'light' || mode === 'dark' ? mode
      : (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    if (mode === 'light' || mode === 'dark') document.documentElement.dataset.theme = mode;
    else delete document.documentElement.dataset.theme;
    const btn = $('theme-toggle');
    if (btn) {
      btn.textContent = resolved === 'light' ? '☾' : '☀';
      btn.title = resolved === 'light' ? 'Passer en thème sombre' : 'Passer en thème clair';
      btn.setAttribute('aria-label', btn.title);
    }
  }
  function initTheme() { applyTheme(storage('xtream-m3u-theme')); }
  function toggleTheme() {
    const cur = document.documentElement.dataset.theme
      || (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    const next = cur === 'light' ? 'dark' : 'light';
    storage('xtream-m3u-theme', next);
    applyTheme(next);
  }

  // --------------------------------------------------- espaces & abonnements
  // Espace membre (profiles) = onglet dédié en haut (Moi, Sœur…).
  // Source complémentaire (aux dans sources) = enrichissement / débordement sans onglet dédié.
  const multiProfile = () => state.profiles.length > 1;
  const hasMultipleSources = () => state.sources.length > 1 || state.profiles.length > 1;
  const sourceName = (id) => (state.sources.find((s) => s.id === String(id)) || state.profiles.find((p) => p.id === String(id)) || {}).name || `Abonnement ${id}`;
  const profileName = (id) => (state.profiles.find((p) => p.id === String(id)) || {}).name || sourceName(id);
  const playlistProfile = (p) => (p && p.provider) || '1';
  const playlistMixed = (p) => Boolean(p && (p.mix || !p.provider));
  /** Autres abonnements mélangeables (espaces membres autorisés + compléments/secours). */
  const mixPartners = () => {
    const pool = state.sources.length ? state.sources : state.profiles;
    return pool.filter((p) => p.mixable && p.id !== state.profile);
  };
  const allowMix = () => mixPartners().length > 0;
  const inProfile = (p) => !hasMultipleSources() || playlistProfile(p) === state.profile;
  const profileQuery = () => (hasMultipleSources() ? `?profile=${encodeURIComponent(state.profile)}` : '');
  /** Abonnements visibles dans l'éditeur (null = tous) : l'espace actif, plus les mélangeables en mode « mélange ». */
  const catalogProviders = () => {
    if (!hasMultipleSources()) return null;
    return new Set([state.profile, ...($('opt-provider').value === 'mix' ? mixPartners().map((p) => p.id) : [])]);
  };

  function renderProfileBar() {
    $('profile-bar').hidden = !multiProfile();
    if (!multiProfile()) return;
    $('profile-buttons').replaceChildren(...state.profiles.map((p) => h('button', {
      type: 'button', class: 'view-btn', 'aria-pressed': String(p.id === state.profile), text: p.name,
      onclick: () => switchProfile(p.id),
    })));
  }

  /** Choix « cet abonnement seul » ou « mélange » ; ``value`` = 'mix' ou l'abonnement de l'espace actif. */
  function renderProviderSelect(value) {
    const select = $('opt-provider');
    $('opt-provider-row').hidden = !hasMultipleSources();
    const own = profileName(state.profile);
    const options = hasMultipleSources()
      ? [[state.profile, `${own} seul`],
        ...(allowMix() ? [['mix', `${own} + ${mixPartners().map((p) => p.name).join(', ')} (mélange & secours)`]] : [])]
      : [[state.profile || '1', 'Abonnement principal']];
    select.replaceChildren(...options.map(([id, name]) => h('option', { value: id, text: name })));
    select.value = options.some(([id]) => id === value) ? value : options[0][0];
  }

  const providerChoice = (p) => (p && playlistMixed(p) && allowMix() ? 'mix' : state.profile);

  /** Catalogues filtrés sur l'abonnement de la playlist ouverte (tous si « mélange »). */
  function reapplyCatalogs() {
    for (const kind of KINDS) if (state.cat[kind].raw) applyCatalog(state.cat[kind].raw);
  }

  async function switchProfile(id) {
    if (id === state.profile) return;
    if (!(await confirmLeave())) return;
    storage('xtream-m3u-profile', id);
    $('dirty').hidden = true;
    location.reload();
  }

  async function request(path, method = 'GET', body) {
    // En-tête de l'interface sur toutes les requêtes (exigé pour les écritures et l'affichage des identifiants).
    const options = { method, headers: { Accept: 'application/json', 'X-Requested-With': 'XtreamM3ULocal' }, credentials: 'same-origin' };
    if (method !== 'GET') {
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

  function ask({ title, text, okLabel = 'Confirmer', danger = false, input = null, input2 = null, select = null }) {
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
    $('dialog-field2').hidden = !input2;
    if (input2) {
      $('dialog-label2').textContent = input2.label;
      $('dialog-input2').value = input2.value || '';
    }
    $('dialog-field3').hidden = !select;
    if (select) {
      $('dialog-label3').textContent = select.label;
      $('dialog-select').replaceChildren(...select.options.map(([value, label]) => h('option', { value, text: label, selected: value === select.value })));
    }
    if (typeof dialog.showModal !== 'function') {
      if (select) return Promise.resolve(null);
      if (input && input2) {
        const value = window.prompt(`${title}\n${input.label}`, input.value || '');
        return Promise.resolve(value === null ? null : { value, value2: window.prompt(input2.label, input2.value || '') || '' });
      }
      if (input) return Promise.resolve(window.prompt(`${title}\n${input.label}`, input.value || ''));
      return Promise.resolve(window.confirm(`${title}\n${text || ''}`) ? true : null);
    }
    return new Promise((resolve) => {
      dialog.returnValue = 'cancel';
      dialog.addEventListener('close', () => {
        if (dialog.returnValue !== 'ok') return resolve(null);
        if (select && input) resolve({ value: $('dialog-input').value, value2: $('dialog-input2').value, choice: $('dialog-select').value });
        else if (select) resolve($('dialog-select').value);
        else if (input2) resolve({ value: $('dialog-input').value, value2: $('dialog-input2').value });
        else resolve(input ? $('dialog-input').value : true);
      }, { once: true });
      dialog.showModal();
      if (input) $('dialog-input').select();
      else if (select) $('dialog-select').focus();
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
    toast('Copié dans le presse-papiers');
  }

  // ------------------------------------------------- règles et doublons
  // Mêmes règles que le serveur (app/builder.py) pour que les compteurs concordent.
  const EMPTY_SET = new Set();
  const SMALL_CAPS = {
    'ᴀ': 'A', 'ʙ': 'B', 'ᴄ': 'C', 'ᴅ': 'D', 'ᴇ': 'E', 'ꜰ': 'F', 'ɢ': 'G', 'ʜ': 'H', 'ɪ': 'I',
    'ᴊ': 'J', 'ᴋ': 'K', 'ʟ': 'L', 'ᴍ': 'M', 'ɴ': 'N', 'ᴏ': 'O', 'ᴘ': 'P', 'ǫ': 'Q', 'ʀ': 'R',
    'ꜱ': 'S', 'ᴛ': 'T', 'ᴜ': 'U', 'ᴠ': 'V', 'ᴡ': 'W', 'ʏ': 'Y', 'ᴢ': 'Z',
    'ᴴ': 'H', 'ᴰ': 'D', 'ᴿ': 'R', 'ᴬ': 'A', 'ᵂ': 'W', 'ᵁ': 'U', 'ᶠ': 'F', 'ˢ': 'S', 'ᵏ': 'K',
    'ᴷ': 'K', 'ᴮ': 'B', 'ᴵ': 'I', 'ᴾ': 'P', 'ᵀ': 'T', 'ᴱ': 'E', 'ᴼ': 'O', 'ᴺ': 'N', 'ᴹ': 'M',
    'ᴳ': 'G', 'ᴸ': 'L', 'ⱽ': 'V', 'ᶜ': 'c', 'ᶦ': 'i', 'ᵗ': 't', 'ʸ': 'y', 'ᵖ': 'p', 'ʳ': 'r',
    '⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
    '⁽': '(', '⁾': ')',
  };
  const SMALL_CAPS_RE = new RegExp(`[${Object.keys(SMALL_CAPS).join('')}]`, 'g');
  const expandSuperscripts = (text) => String(text || '').replace(SMALL_CAPS_RE, (ch) => SMALL_CAPS[ch] || ch);
  const fixSoccerO = (text) => String(text || '').replace(/(sp)\s*⚽\ufe0f?\s*(rts)/gi, (_, a, b) => a + (a === a.toUpperCase() ? 'O' : 'o') + b);
  const QUALITY = [
    [/(?<![a-z0-9])(8k|4k|uhd|3840p?|2160p?)(?![a-z0-9])/, 4],
    [/(?<![a-z0-9])(fhd|full ?hd|1080[pi]?)(?![a-z0-9])/, 3],
    [/(?<![a-z0-9])(hd|720p?)(?![a-z0-9])/, 2],
    [/(?<![a-z0-9])(sd|576[pi]?|480[pi]?)(?![a-z0-9])/, 1],
  ];
  const QUALITY_TAGS = /(?<![a-z0-9])(8k|4k|uhd|3840p?|2160p?|fhd|full ?hd|1080[pi]?|hd|720p?|sd|576[pi]?|480[pi]?|hevc|h\.?26[45]|x26[45]|hdr10?|dolby|atmos|60 ?fps|50 ?fps|30 ?fps|25 ?fps|multi[- ]?audio|multi[- ]?langues?|backup|bkp|bk|raw|multi|vip|local|low|lq|hq)(?![a-z0-9])/g;
  const NOISE_BRACKETS = /(?:\*+\s*(?:local|backup|bkp|bk|multi|raw|hevc|h\.?26[45]|vip|fhd|uhd|hd|sd|4k|8k|low|lq|hq)\s*\*+|[\[(]\s*(?:local|backup|bkp|bk|multi(?:[- ]?(?:audio|langues?))?|raw|hevc|h\.?26[45]|vip|fhd|uhd|hd|sd|4k|8k|3840p?|2160p?|1080p?|720p?|low|lq|hq|hdr10?|50\s*fps|60\s*fps|30\s*fps|25\s*fps|5\.1|aac|ac3|live(?:\s+event(?:\s+only)?)?|event\s+only|stz|stc|13)\s*[\])])/gi;
  const SLASH_QUALITY = /(?:^|(?<=\s))(?:(?:8k|4k|uhd|fhd|hd|sd|raw|hevc|hdr)\s*\/\s*)+(?:8k|4k|uhd|fhd|hd|sd|raw|hevc|hdr)(?=\s|$|\.)/gi;
  const GLUED_DIGIT_QUALITY = /(?<=\d)(?:fhd|uhd|hd|sd)(?=\b|$|\s|\.)/gi;
  const SEP_CHARS = '#=*~_\\-•●◆★☆▬▭━─═';
  const SEPARATOR = new RegExp(`^[${SEP_CHARS}|\\s]*[${SEP_CHARS}]{3,}.*[${SEP_CHARS}]{3,}[${SEP_CHARS}|\\s]*$`, 'u');
  /** Fausse chaîne servant de titre de section (même règle que le serveur). */
  const SYMBOL_EDGES = /^\s*[^\p{L}\p{N}\s]{3,}.*[^\p{L}\p{N}\s]{3,}\s*$/u;
  const PLACEHOLDER = /^[^\p{L}\p{N}]*no\s+(live\s+)?events?\b|\bno\s+event\s+streaming\b/iu;
  const isSeparator = (name) => { const t = String(name || '').trim(); return !t || PLACEHOLDER.test(t) || SEPARATOR.test(t) || SYMBOL_EDGES.test(t) || !/[\p{L}\p{N}]/u.test(t); };
  const squash = (text) => norm(expandSuperscripts(text)).split(/\s+/).filter(Boolean).join(' ');
  const ADULT_RE = /(?<![a-z0-9])(?:xxx|18\s*\+|\+\s*18|for\s+adults?|adults?(?!\s+swim)|adultes?|porno?|eroti(?:c|que)|x-rated|nsfw|dorcel|brazzers|penthouse|hustler|playboy\s*tv)(?![a-z0-9])/i;
  const isAdultCategory = (name) => ADULT_RE.test(squash(name));
  const isAdultItem = (c, kind = state.kind) => {
    if (c.adult || ADULT_RE.test(squash(c.name))) return true;
    const catNames = state.cat[kind].categoryNames;
    return (c.category_ids || []).some((cid) => isAdultCategory(catNames.get(cid) || ''));
  };
  const adultHidden = (c, kind = state.kind) => $('opt-adult').checked && isAdultItem(c, kind);

  // ------------------------------------------- groupes personnalisés (même règle que app/builder.py)
  const CG = 'g:';
  const MATCH_PREFIX = /^(?:\s*[\[\(|•●★☆▶►◉♦♣✦▼\-]+\s*)*(?:(?:[A-Za-z]{2}(?:\s*[-/]\s*(?:VIP|OTT|HD|FHD|4K|UHD|RAW|HEVC))?|VIP|SPO|UHD|FHD|8K|4K|HEVC|F|BEIN\s+FR)\s*[:|\-\]\)●•»◉]+\s*)+/i;
  const CANONICAL_ALIASES = {
    'canal+ box offcie': 'canal+ box office',
    'canal+ cinema s': 'canal+ cinema',
    'canal docs': 'canal+ docs',
    'rmc alerte securs': 'rmc alerte secours',
    'discovery invest': 'discovery investigation',
    'discovery id': 'discovery investigation',
    'planete': 'planete+',
    'planete ae': 'planete+ aventure',
    'planete a&e': 'planete+ aventure',
    'planete ci': 'planete+ crime',
    'polar': 'polar+',
    'piwi': 'piwi+',
    'golf': 'golf+',
    'infosport': 'infosport+',
    'l equipe 21': 'l equipe',
    'la chaine parlementaire': 'lcp',
    'euronews fr': 'euronews',
    'novelas': 'novelas tv',
    'museum': 'museum tv',
    'museum channel': 'museum tv',
    'histoire': 'histoire tv',
    'melody tv': 'melody',
    'lucky jack tv': 'lucky jack',
    'nick junior': 'nickelodeon junior',
    'nick 4 teen': 'nickelodeon teen',
    'equidia live': 'equidia',
    'automoto la chaine': 'automoto',
    'game one1': 'game one',
    'teletoon 1': 'teletoon+1',
    'disney channel 1': 'disney channel+1',
    'nickelodeon 1': 'nickelodeon+1',
    'telemaroc': 'tele maroc',
    'ol tv': 'oltv',
    'natgeo': 'national geographic',
    'nat geo': 'national geographic',
    'national geo': 'national geographic',
    'natgeo wild': 'nat geo wild',
    'national geo wild': 'nat geo wild',
    'tv5monde': 'tv5 monde',
    'tv5monde europe': 'tv5 monde europe',
    'ma tele': 'matele',
    'no tele': 'notele',
    'tele sambre': 'telesambre',
    'tele mb': 'telemb',
    'qu4tre rtc liege': 'rtc tele liege',
    'qu4tre': 'rtc tele liege',
    'antenne centre tv': 'antenne centre',
    'rtl club': 'club rtl',
    'rtl plug': 'plug rtl',
    'play sports 1 wallonie': 'play sports 1 fr',
    'play sports 2 wallonie': 'play sports 2 fr',
    'dazn pro leagues 1': 'dazn pro league 1 fr',
    'dazn pro leagues 2': 'dazn pro league 2 fr',
    'dazn sports 1 fr': 'dazn 1 fr',
  };
  /** Nom comparable : sans pays (« FR: »), accents, ponctuation ni qualité. « FR: TF1 FHD » → « tf1 ». */
  function matchName(name) {
    let text = fixSoccerO(expandSuperscripts(name || '')).replace(MATCH_PREFIX, '');
    text = text.replace(NOISE_BRACKETS, ' ').replace(SLASH_QUALITY, ' ').replace(GLUED_DIGIT_QUALITY, '');
    text = text.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
    text = text.replace(/\b13\s*\.\s*eme\s+rue\b/g, '13eme rue');
    text = text.replace(/[^\p{L}\p{N}+&]+|_/gu, ' ').replace(/\s+\+/g, '+');
    let tokens = text.split(/\s+/).filter(Boolean);
    if (tokens.length > 1 && (tokens[0] === 'fr' || tokens[0] === 'fra')) tokens = tokens.slice(1);
    let core = tokens.join(' ').replace(QUALITY_TAGS, ' ').split(/\s+/).filter(Boolean).join(' ');
    core = core.replace(/\b13\s+eme\s+rue\b/g, '13eme rue')
      .replace(/\beuro\s+sports?\b/g, 'eurosport')
      .replace(/\bligue\s*1\+/g, 'ligue 1+')
      .replace(/\bnat\s*geo\b/g, 'nat geo')
      .replace(/\btoute\s+lhistoire\b/g, 'toute l histoire')
      .replace(/\bchasse\s*&\s*peche\b/g, 'chasse et peche')
      .replace(/\bsciences?\s*(?:&|et)?\s*vie(?:\s+tv)?\b/g, 'science & vie')
      .replace(/\bbe\s*in\s+sports?\b/g, 'bein sports')
      .replace(/\bbein sports 0([1-9])\b/g, 'bein sports $1');
    return CANONICAL_ALIASES[core] || core;
  }
  const NUMBERED = /^\s*(\d{1,5})\s*[:=]\s*(.+)$/;  // « 4: Canal+ » = numéro de chaîne (comme sur le décodeur)
  const LIVES = Array.from({ length: 19 }, (_, i) => `Canal+ Live ${i + 1}`);
  const numbered = (label, first, last) => Array.from({ length: last - first + 1 }, (_, i) => `${label} ${i + first}`);
  const SPORT_FR = [
    ...numbered('beIN Sports', 1, 3), 'Canal+ Foot', 'Ligue 1+ 1', 'Canal+ Sport', 'Canal+ Sport 360',
    'Eurosport 1', 'Eurosport 2', 'RMC Sport 1', 'L’Équipe', 'L’Équipe Live Foot', 'Sport en France',
    'RMC Sport 2', 'RMC Sport Access', 'RMC After Foot TV', 'Infosport+', 'Automoto La Chaîne',
    'Golf+', 'Golf Channel', 'Journal du Golf TV', 'Equidia Live', ...numbered('Equidia Racing', 1, 8),
    ...numbered('beIN Sports Max', 4, 10), ...numbered('Ligue 1+', 2, 10), ...LIVES,
    ...numbered('Eurosport 360', 1, 32), ...numbered('RMC Sport Live', 1, 17), ...numbered('DAZN', 1, 5),
  ];
  const SPORT_AR = [
    ...numbered('beIN Sports', 1, 9), 'Arryadia', 'Arryadia TNT', ...numbered('Alkass', 1, 10),
    ...numbered('Thmanyah', 1, 3), ...numbered('AD Sports Premium', 1, 2), ...numbered('AD Sports', 1, 4),
    ...numbered('AD Sports Asia', 1, 2), ...numbered('StarzPlay Sports', 1, 2),
    ...numbered('Alwan Sport', 1, 5), ...numbered('Diwan Sport', 1, 2),
    'ON Sport', 'ON Sport Plus', 'ON Sport Max', ...numbered('Dubai Sports', 1, 3), 'Yas Sports',
    'Al Ahly TV', 'Zamalek Sports', 'Nile Sport', 'Iraqia Sport', 'Alrabiaa Sport One', 'Alrabiaa Sport Two',
    'Kuwait Sports', 'Kuwait Sports Plus', ...numbered('Bahrain Sport', 1, 2), 'Oman Sports',
    'Jordan Sport', ...numbered('Libya Sport', 1, 2), 'Sharjah Sports', 'Sharjah Sports 2', 'Neelain Sport',
    ...numbered('KSA Sport', 1, 4), ...numbered('Dubai Racing', 1, 3), 'beIN Sports News', 'beIN Sports Global',
    'beIN Sports NBA', ...numbered('beIN Sports Xtra', 1, 5), 'beIN Sports AFC', ...numbered('beIN Sports AFC', 1, 6),
    ...numbered('Shahid Sports Event', 1, 3),
  ];
  const NILESAT = [
    ...numbered('MBC', 1, 5), 'MBC Action', 'MBC Max', 'MBC Bollywood', 'MBC Drama',
    'MBC Masr', 'MBC Masr 2', 'MBC Masr Drama', 'MBC Iraq', 'Wanasah',
    'Rotana Cinema EGY', 'Rotana Cinema KSA', 'Rotana Classic', 'Rotana Drama', 'Rotana Khalijia',
    'Rotana Comedy', 'Rotana Music', 'Rotana Clip', 'OSNtv Yahala', 'OSNtv Yahala Bil Arabi', 'OSNtv Yahala Aflam',
    'DMC', 'DMC Drama', 'ON', 'ON Drama', 'CBC', 'CBC Drama', 'CBC Sofra', 'Al Hayat',
    'Al Hayat Mosalsalat', 'Al Nahar', 'Al Nahar Drama', 'Sada El Balad', 'Nile Drama', 'Al Masriya',
    'Al Aoula', '2M Maroc', '2M Monde', '2M', 'Athaqafia', 'Al Maghribia', 'Assadissa', 'Tamazight',
    'Laayoune TV', 'Medi 1 TV', 'Medi 1 TV Maghreb', 'Medi 1 TV Afrique', 'Chada TV', 'Télé Maroc',
    'Programme National', 'Canal Algérie', 'A3', 'TV4 Tamazight', 'TV5 Coran', 'Echourouk TV', 'Echourouk News',
    'Ennahar TV', 'Samira TV', 'Algérie 6', 'Algérie 7', 'Algérie 8', 'Algérie 9 El Barlamaniya',
    'Wataniya 1', 'Wataniya 2', 'El Hiwar Ettounsi', 'Nessma', 'Hannibal TV',
    'Attessia', 'Libya Al Ahrar', 'Libya Al Wataniya', 'Al Wasat', 'El Mouritania',
    'Saudi TV', 'SBC', 'Abu Dhabi TV', 'Al Emarat', 'Dubai TV', 'Sama Dubai', 'Sharjah TV',
    'Kuwait TV 1', 'Kuwait TV 2', 'Bahrain TV', 'Qatar TV', 'Qatar 2', 'Al Rayyan', 'Oman TV',
    'LBCI', 'LBC Sat', 'LBC International', 'MTV Lebanon', 'Al Jadeed', 'Télé Liban', 'Roya TV', 'Jordan TV',
    'Palestine TV', 'Falastini TV', 'Syria TV', 'Al Iraqiya', 'Al Sharqiya', 'Al Sumaria', 'Yemen TV', 'Aden TV', 'Sudan TV',
    'Al Jazeera', 'Al Jazeera Mubasher', 'Al Jazeera Documentary', 'Al Arabiya', 'Al Hadath',
    'Al Araby TV', 'Al Araby 2', 'Asharq News', 'Sky News Arabia', 'Al Qahera News', 'BBC Arabic',
    'France 24 Arabic', 'TRT Arabi', 'National Geographic Abu Dhabi',
    'Spacetoon', 'Cartoon Network Arabic', 'Majid TV', 'Jeem TV', 'Baraem', 'Toyor Al Janah',
    'Saudi Quran TV', 'Saudi Sunnah TV', 'Iqraa', 'Al Resalah', 'Zad TV', ...SPORT_AR,
  ];
  // Liste « OTT » (112 chaînes françaises : Généralistes, Canal+, Cinéma, Canal Play, Séries, Divertissement, Découverte, Jeunesse).
  const OTT = `TF1, France 2, France 3, France 4, France 5, M6, Arte, RTS Un, RTS Deux,
    Canal+, Canal+ Foot, Canal+ Sport, Canal+ Sport 360, Canal+ Box Office, Canal+ Grand Ecran, Canal+ Cinema(s), Canal+ Docs, Canal+ Kids,
    Cine+ OCS, Cine+ Frisson, Cine+ Emotion, Cine+ Family, Cine+ Festival, Cine+ Classic, Drive In Movie Channel, Action, TCM Cinema,
    Eurochannel, La Chaine de Noel, Studio Movie 1, Studio Movie 2, Studio Movie 3, Disney+ Marvels, Disney+ Star Wars,
    Canal Play 1, Canal Play 2, Canal Play 3, Canal Play 4, Canal Play 5, Canal Play 6, Canal Play 7, Canal Play 8, Canal Play 9, Canal Play 10,
    13eme Rue, Syfy, Warner TV, Polar+, Serie Club, TV Breizh, RTL9, AB1, Novelas TV, TF1 Series Films, France TV Series, Studio Comedy, Studio Crime,
    Comedie+, Paris Premiere, Teva, W9, TMC, TFX, CStar, T18, Novo19, 6ter, RMC Story, RMC Life, AB3, Carac 1, TV5 Monde, TV5 Monde Europe,
    Planete+, Planete+ Crime, Planete+ Aventure, National Geographic, Nat Geo Wild, Discovery Channel, TLC, Discovery ID, RMC Decouverte, Museum,
    Ushuaia TV, Histoire, Toute l'Histoire, Seasons, Chasse et Peche, Science & Vie, Crime District, Animaux, Trek, L'Esprit Sorcier, France TV Docs, Imearth,
    Piwi+, Disney Junior, Nickelodeon Junior, Boomerang, Cartoonito, Tiji, Teletoon+, Disney Channel, Nickelodeon, Nickelodeon +1, Canal J,
    Nickelodeon Teen, Cartoon Network, Gulli, Dreamworks, Pitchoun TV, Pitchoun Kids Music`
    .split(',').map((p) => p.trim()).filter(Boolean);
  const BELGIQUE = [
    'La Une', 'RTBF La Une', 'Tipik', 'RTBF Tipik', 'La Deux', 'La Trois', 'RTBF La Trois',
    'RTL-TVI', 'Club-RTL', 'Plug-RTL', 'RTL District', 'RTL Play', 'RTL Play 2', 'Arte Belgique',
    'LN24', 'Canal Z', 'AB1', 'AB3', 'ABXplore', 'AB Xplore', 'Dobbit TV (FR)', 'Dobbit TV',
    'Be 1', 'Be Ciné', 'Be Séries',
    'DAZN Channel 1 (FR)', 'DAZN Channel 2 (FR)', 'DAZN Pro League 1 (FR)', 'DAZN Pro League 2 (FR)',
    ...numbered('DAZN', 1, 5).map((n) => `${n} (FR)`),
    'Play Sports 1 (FR)', 'Play Sports 2 (FR)',
    ...numbered('VOO Sport World', 1, 3), 'Divertissez-VOO',
    'Antenne Centre TV', 'BX1', 'Bouké', 'Canal Zoom', 'Ma Télé', 'No Télé', 'Qu4tre (RTC Liège)',
    'Télé Sambre', 'Télé MB', 'TV Com', 'TV Lux', 'Vedia',
    'Fun Radio Vision', 'Radio Contact Vision', 'Bel-RTL Vision', 'Tipik Vision', 'Arabel Vision', 'NRJ Live TV',
    'RTL Lux', 'RTL 2 Lux', 'Eldo TV', 'Chamber TV',
  ];
  const PRESETS = [
    { name: 'OTT', match: 'exact', hint: 'Ta liste « OTT » (112 chaînes françaises : Généralistes, Canal+, Cinéma, Canal Play, Séries, Divertissement, Découverte, Jeunesse) dans l\'ordre du fournisseur',
      patterns: OTT },
    { name: 'Bouquet Canal+', match: 'exact', hint: 'Toutes les chaînes du bouquet Canal+, rangées par thème comme sur le décodeur (TNT, Canal+, cinéma, sport, séries, découverte, jeunesse, info, musique)',
      patterns: ['1: TF1', '2: France 2', '3: France 3', '4: Canal+', '5: France 5', '6: M6', '7: Arte',
        'W9', 'TMC', 'TFX', 'NRJ 12', 'NRJ12', 'LCP', 'Public Sénat', 'France 4', 'BFM TV', 'BFMTV', 'CNews', 'CStar', 'C Star', 'Gulli',
        'TF1 Séries Films', 'L\'Équipe', 'La chaîne L\'Équipe', '6ter', 'RMC Story', 'RMC Découverte', 'Chérie 25', 'LCI', 'franceinfo',
        'France Info', 'T18', 'Novo19', 'Novo 19', 'Paris Première', 'Téva', 'RTL9', 'TV5Monde', 'TV5 Monde',
        'Canal+ Cinéma', 'Canal+ Cinémas', 'Canal+ Cinéma(s)', 'Canal+ Sport', 'Canal+ Sport 360', 'Canal+ Foot', 'Canal+ Séries',
        'Canal+ Docs', 'Canal+ Kids', 'Canal+ Box Office', 'Canal+ Grand Écran', 'Canal+ Premier League', 'Canal+ Magic',
        'Ciné+ OCS', 'Ciné+ Premier', 'Ciné+ Frisson', 'Ciné+ Émotion', 'Ciné+ Famiz', 'Ciné+ Classic', 'Ciné+ Festival',
        'OCS', 'OCS Max', 'OCS City', 'OCS Choc', 'OCS Géants', 'TCM Cinéma', 'Paramount Channel', 'Action', 'Polar+', 'Comédie+',
        'beIN Sports 1', 'beIN Sports 2', 'beIN Sports 3', 'beIN Sports Max 4', 'beIN Sports Max 5', 'beIN Sports Max 6',
        'beIN Sports Max 7', 'beIN Sports Max 8', 'beIN Sports Max 9', 'beIN Sports Max 10', 'Eurosport 1', 'Eurosport 2',
        'Infosport+', 'Golf+', 'Automoto', 'Equidia', 'Sport en France', 'MotorVision', 'Trace Sport Stars',
        'Série Club', '13ème Rue', '13e Rue', 'Syfy', 'Warner TV', 'Paramount Network', 'MTV', 'E!', 'AB1', 'Novelas TV', 'J-One',
        'Planète+', 'Planète+ Aventure', 'Planète+ Crime', 'Ushuaïa TV', 'Love Nature', 'Histoire TV', 'Toute l\'Histoire',
        'Discovery Channel', 'Discovery', 'Discovery Science', 'Discovery Family', 'Discovery Investigation', 'National Geographic', 'Nat Geo Wild', 'Dogs & Cats',
        'Science et Vie TV', 'Animaux', 'Seasons', 'Trek', 'Museum TV', 'Crime District', 'Chasse et Pêche',
        'Piwi+', 'TiJi', 'Canal J', 'Disney Channel', 'Disney Junior', 'Nickelodeon', 'Nickelodeon Junior', 'Nick Jr',
        'Nickelodeon Teen', 'Boomerang', 'Cartoon Network', 'Cartoonito', 'Boing', 'Mangas', 'Game One',
        'CNews Prime', 'France 24', 'Euronews', 'BFM Business', 'BFM Paris', 'BFM Lyon', 'BFM Marseille', 'BFM Côte d\'Azur',
        'BFM Alsace', 'BFM Littoral', 'BFM Grand Lille', 'BFM Normandie', 'i24News', 'Europe 1 TV', 'Le Figaro TV', 'TV Monaco',
        'MCM', 'M6 Music', 'MTV Hits', 'NRJ Hits', 'CStar Hits', 'RFM TV', 'Trace Urban', 'Mezzo', 'Mezzo Live', 'Melody',
        'Clubbing TV', ...LIVES] },
    { name: 'TNT', match: 'exact', hint: 'Chaînes gratuites de la TNT, dans l\'ordre de leurs numéros',
      patterns: ['TF1', 'France 2', 'France 3', 'Canal+', 'France 5', 'M6', 'Arte', 'C8', 'W9', 'TMC', 'TFX', 'NRJ 12', 'NRJ12',
        'LCP', 'Public Sénat', 'France 4', 'BFM TV', 'BFMTV', 'CNews', 'CStar', 'C Star', 'Gulli', 'TF1 Séries Films',
        'L\'Équipe', 'La chaîne L\'Équipe', '6ter', 'RMC Story', 'RMC Découverte', 'Chérie 25', 'LCI', 'franceinfo',
        'France Info', 'T18', 'Novo19', 'Novo 19'] },
    { name: 'Maroc', match: 'exact', region: 'ar', hint: 'Chaînes marocaines dans l\'ordre SNRT (Al Aoula, 2M, Arryadia, Athaqafia, Al Maghribia, Assadissa, Aflam 7, Tamazight, Laâyoune, Medi 1 TV, Chada TV, Télé Maroc, M24, MBC 5…)',
      patterns: [
        '1: Al Aoula', 'Al Aoula HD', 'Al Aoula Inter', 'Al Aoula International', 'Al Aoula Maroc', 'TVM',
        '2: 2M', '2M Maroc', '2M Monde', '2M National', '2M Natioanl', '2M TV', '2M HD',
        '3: Arryadia', 'Arryadia HD', 'Arriadia', 'Arryadia Live', 'Arryadia TNT', 'Arryadia TNT Local',
        'Arryadia 1', 'Arryadia 2', 'Arryadia 3', 'Arryadia Olympics', 'Arryadia TV Maroc',
        '4: Athaqafia', 'Assaqafia', 'Attakafia', 'Arrabia', 'Arrabiaa',
        '5: Al Maghribia', 'Al Maghribia HD', 'Maghribia',
        '6: Assadissa', 'Assadissa Maroc', 'Assadissa Moroc', 'Mohammed VI', 'Assadissa Mohammed VI',
        '7: Aflam 7', 'Aflam 7 Local', 'Aflam TV',
        '8: Tamazight', 'Tamazight TV', 'Tamazighte', 'Tamazight Maroc', 'Tamazight Moroc',
        '9: Laayoune', 'Laayoune TV', 'Al Aoula Laayoune', 'Laâyoune TV',
        '10: Medi 1 TV', 'Medi1 TV', 'Medi 1 TV Maghreb', 'Medi1 TV Maghreb', 'Medi 1 Maghreb TV',
        'Medi 1 Arabic TV', 'Medi 1 TV Arabic', 'Medi 1 TV Afrique', 'Medi 1 Afrique TV', 'Medi 1 TV Europe', 'Medi 1 News',
        '11: Chada TV', 'Chada',
        '12: Télé Maroc', 'Tele Maroc', 'Tele Moroc', 'Telemaroc',
        '13: M24', 'M24 Maroc', 'M24 TV',
        'MBC 5', 'MBC5', 'Al Ons TV', 'Al Ons', 'TV5', 'Omar Peche 24/7',
      ] },
    { name: 'Nilesat', match: 'exact', region: 'ar', hint: 'Chaînes du monde arabe disponibles dans le catalogue, avec MBC et Rotana en tête',
      patterns: NILESAT },
    { name: 'Sport FR', match: 'exact', region: 'fr', hint: 'Sport francophone, avec beIN Sports 1, 2 et 3 en tête',
      patterns: SPORT_FR },
    { name: 'Sport AR', match: 'exact', region: 'ar', hint: 'Sport arabophone, avec beIN Sports MENA en tête',
      patterns: SPORT_AR },
    { name: 'Belgique', match: 'exact', hint: 'Chaînes belges francophones (RTBF, RTL, Be TV, DAZN FR, VOO Sport, régionales, Radio Vision) et Luxembourg',
      patterns: BELGIQUE },
    { name: 'Francophonie', match: 'exact', hint: 'Chaînes francophones de Belgique & Luxembourg (en premier) et de Suisse romande, puis TV5Monde',
      patterns: [...BELGIQUE,
        'RTS 1', 'RTS Un', 'RTS 2', 'RTS Deux', 'RTS Info', 'Léman Bleu', 'La Télé', 'Canal 9', 'Canal Alpha', 'Carac 1', 'Carac 2',
        ...numbered('Blue Sport', 1, 10), ...numbered('My Sports', 1, 9),
        'TV5Monde', 'TV5 Monde', 'TV5Monde Europe', 'TV5Monde FBS'] },
    { name: 'Chaînes Canal+', match: 'words', hint: 'Seulement les chaînes dont le nom contient Canal+',
      patterns: ['Canal+', 'Canal plus'] },
    { name: 'Sport', match: 'words', hint: 'beIN, RMC Sport, Eurosport, Canal+ Sport…',
      patterns: ['beIN Sports', 'beIN Sport', 'RMC Sport', 'Eurosport', 'Canal+ Sport', 'Canal+ Foot', 'L\'Équipe', 'Infosport+',
        'Golf+', 'Automoto', 'DAZN', 'Sport en France', 'Equidia'] },
    { name: 'Info', match: 'words', hint: 'Chaînes d\'information',
      patterns: ['BFM TV', 'BFMTV', 'CNews', 'LCI', 'franceinfo', 'France Info', 'France 24', 'Euronews', 'BFM Business', 'i24News',
        'LCP', 'Public Sénat'] },
    { name: 'Cinéma', match: 'words', hint: 'Canal+ Cinéma, Ciné+, OCS, TCM…',
      patterns: ['Canal+ Cinéma', 'Ciné+', 'OCS', 'TCM Cinéma', 'Paramount Channel', 'Polar+'] },
    { name: 'Jeunesse', match: 'words', hint: 'Gulli, Disney, Nickelodeon…',
      patterns: ['Gulli', 'TiJi', 'Canal J', 'Disney Channel', 'Disney Junior', 'Nickelodeon', 'Nick Jr', 'Boomerang',
        'Cartoon Network', 'Cartoonito', 'Piwi+', 'Boing', 'Mangas', 'Game One'] },
    { name: 'Documentaires', match: 'words', hint: 'Planète+, National Geographic, Discovery…',
      patterns: ['Planète+', 'National Geographic', 'Nat Geo', 'Discovery', 'Histoire TV', 'Toute l\'Histoire', 'Ushuaïa TV',
        'Science et Vie TV', 'RMC Découverte', 'Animaux', 'Seasons', 'Trek', 'Museum TV'] },
  ];

  /** Groupes perso par priorité : ordre des groupes de la playlist, puis ordre de création. */
  function orderedCustomGroups() {
    const rank = new Map(state.groupOrder.map((k, i) => [k, i]));
    return state.customGroups.map((g, i) => ({ g, i }))
      .sort((a, b) => (rank.get(CG + a.g.id) ?? 1e9) - (rank.get(CG + b.g.id) ?? 1e9) || a.i - b.i)
      .map(({ g }) => {
        const cores = new Map();
        for (const raw of (g.patterns || [])) {
          const text = String(raw).replace(NUMBERED, (m, n, rest) => rest).trim();
          const core = matchName(text);
          if (core && !cores.has(core)) cores.set(core, text);
        }
        const patterns = [...cores.keys()];
        return { ...g, _patterns: patterns, _labels: cores, _exact: new Map(patterns.map((p, i) => [p, i]).reverse()) };
      });
  }

  function groupMatches(c, group) {
    if (group.region && (c.regional?.[0] !== group.region || isSeparator(c.name))) return false;
    if (c._core === undefined) c._core = matchName(c.name);
    const core = group.region ? c.regional[1] : c._core;
    if (group.match === 'exact') return group._exact.has(core);
    const padded = ` ${core} `;
    return group._patterns.some((p) => padded.includes(` ${p} `));
  }

  /** Chaînes retenues rangées dans un groupe perso : Map id → id du groupe. */
  /** Groupes supplémentaires d'une chaîne (option « une chaîne dans plusieurs groupes ») : id → [groupes]. */
  function groupExtras() {
    groupAssignment();
    return state.groupExtras || new Map();
  }

  function groupAssignment() {
    if (state.groupCache) return state.groupCache;
    const result = new Map();
    const extras = new Map();
    const cleanLabels = new Map();
    const multi = $('opt-multi-groups').checked;
    const groups = orderedCustomGroups();
    if (groups.length) {
      const known = new Set(groups.map((g) => g.id));
      const sel = state.sel.live;
      const separators = $('opt-separators').checked;
      for (const c of state.cat.live.channels) {
        if (sel.excluded.has(c.stream_id) || adultHidden(c, 'live')) continue;
        if (!sel.included.has(c.stream_id) && !c.category_ids.some((id) => catOn('live', id))) continue;
        if (separators && isSeparator(c.name)) continue;
        const manual = state.itemGroups[c.stream_id];
        if (manual === '-') continue;
        if (manual && known.has(manual)) { result.set(c.stream_id, manual); continue; }
        const matching = multi ? groups.filter((g) => g._patterns.length && groupMatches(c, g))
          : [groups.find((g) => g._patterns.length && groupMatches(c, g))].filter(Boolean);
        if (matching.length) {
          const g0 = matching[0];
          result.set(c.stream_id, g0.id);
          if (g0.match === 'exact') {
            const core = g0.region ? c.regional?.[1] : c._core;
            const lbl = g0._labels && g0._labels.get(core);
            if (lbl) cleanLabels.set(c.stream_id, lbl);
          }
        }
        if (matching.length > 1) extras.set(c.stream_id, matching.slice(1).map((g) => g.id));
      }
    }
    state.groupCache = result;
    state.groupExtras = extras;
    state.groupCleanLabels = cleanLabels;
    return result;
  }
  const customName = (id) => (state.customGroups.find((g) => g.id === id) || {}).name || id;
  function qualityOf(name) {
    const n = squash(name);
    for (const [re, score] of QUALITY) if (re.test(n)) return score;
    return 1.5;
  }
  function baseName(name) {
    return squash(name).replace(QUALITY_TAGS, ' ').replace(/[|()[\]{}:._\-+*/\\]+/g, ' ').split(/\s+/).filter(Boolean).join(' ');
  }
  const CLEAN_PREFIX = /^(?:\s*[\[\(|•●★☆▶►◉♦♣✦▼\-]+\s*)*(?:(?:[A-Za-z]{2,3}(?:\s*[-/]\s*(?:VIP|OTT|HD|FHD|4K|UHD|RAW|HEVC))?|VIP|SPO|UHD|FHD|8K|4K|HEVC|F|BEIN\s+(?:FR|AR))\s*[:|\-\]\)●•»◉]+\s*)+/i;
  const CLEAN_SYMBOLS = /[◉●★☆▶►•♦♣✦▼▲⚽✟\ufe0f]+/g;
  const CLEAN_TAGS = /(?:^|(?<=\s))(?:full\s*hd|fhd|uhd|4k|8k|3840p?|2160p?|1080[pi]?|720p?|576[pi]?|480[pi]?|hd|sd|hevc|h\.?26[45]|x26[45]|hdr10?|dolby|atmos|60\s*fps|50\s*fps|30\s*fps|25\s*fps|multi[- ]?audio|multi[- ]?langues?|raw|vip|low|lq|hq|backup|bkp|bk|multi|local|5\.1|aac|ac3)\.?(?=\s|$)/gi;
  function cleanChannelName(name) {
    const raw = String(name || '').trim();
    if (!raw) return raw;
    let text = fixSoccerO(expandSuperscripts(raw)).replace(CLEAN_SYMBOLS, ' ');
    const prefixMatch = text.match(CLEAN_PREFIX);
    if (prefixMatch) {
      const codeMatch = prefixMatch[0].match(/^\W*([A-Za-z]{2})\b/);
      const prefixCode = codeMatch ? codeMatch[1].toUpperCase() : '';
      text = text.slice(prefixMatch[0].length);
      if (prefixCode && text.startsWith(`${prefixCode} `)) text = text.slice(prefixCode.length + 1);
    }
    text = text.replace(NOISE_BRACKETS, ' ').replace(SLASH_QUALITY, ' ').replace(GLUED_DIGIT_QUALITY, '');
    text = text.replace(/\s*[|•]+\s*/g, ' ').replace(CLEAN_PREFIX, '');
    let prev;
    do { prev = text; text = text.replace(CLEAN_TAGS, ' '); } while (text !== prev);
    text = text.replace(/^[\s\-:|*]+|[\s\-:|*]+$/g, '').replace(/\s+/g, ' ').trim();
    return text || raw;
  }
  function ruleMatches(name, rule) {
    const n = squash(name), v = squash(rule.value);
    if (rule.match === 'prefix') return n.startsWith(v);
    if (rule.match === 'exact') return n === v;
    return n.includes(v);
  }
  const ruleCache = new Map();
  function ruleSet(kind, rules = state.rules) {
    const list = rules.filter((r) => r.kind === kind);
    if (!list.length) return EMPTY_SET;
    const key = `${kind}|${JSON.stringify(list)}|${state.cat[kind].categories.length}`;
    if (!ruleCache.has(key)) {
      ruleCache.set(key, new Set(state.cat[kind].categories.filter((cat) => list.some((r) => ruleMatches(cat.name, r))).map((cat) => cat.category_id)));
    }
    return ruleCache.get(key);
  }
  const catOn = (kind, id) => state.sel[kind].selected.has(id) || ruleSet(kind).has(id);
  const keyOf = (c, kind = state.kind) => KIND[kind].prefix + c.stream_id;
  function displayName(c, kind = state.kind) {
    const manual = state.itemNames[keyOf(c, kind)];
    if (manual) return manual;
    if (kind === 'live' && $('opt-clean-names').checked) {
      groupAssignment();
      return (state.groupCleanLabels && state.groupCleanLabels.get(c.stream_id)) || cleanChannelName(c.name);
    }
    return c.name;
  }

  /** Chaînes du direct masquées comme doublons (option « Masquer les doublons »). */
  function hiddenDuplicates() {
    const dedupe = $('opt-dedupe').checked, separators = $('opt-separators').checked;
    if (!dedupe && !separators) return EMPTY_SET;
    if (state.hiddenCache) return state.hiddenCache;
    const sel = state.sel.live;
    const best = new Map();
    const regions = new Map(state.customGroups.filter((g) => g.region).map((g) => [g.id, g.region]));
    const kept = [];
    for (const c of state.cat.live.channels) {
      if (sel.excluded.has(c.stream_id) || adultHidden(c, 'live')) continue;
      const group = c.category_ids.find((id) => catOn('live', id)) || (sel.included.has(c.stream_id) ? c.category_id : null);
      if (group == null) continue;
      if (separators && isSeparator(c.name)) { kept.push(c.stream_id); continue; }
      if (!dedupe) { best.set(`id\u0000${c.stream_id}`, [{ q: 0, id: c.stream_id, i: kept.length }]); kept.push(c.stream_id); continue; }
      const custom = groupAssignment().get(c.stream_id);
      if (c._core === undefined) c._core = matchName(c.name);
      const regional = regions.get(custom);
      const core = regional && regional === c.regional?.[0] ? c.regional[1] : c._core;
      const key = `${custom ? CG + custom : group}\u0000${core || baseName(c.name)}`;
      const raw = qualityOf(c.name);
      const q = raw === 4 && $('opt-uhd').value !== 'normal' ? 0.5 : raw;
      if (!best.has(key)) best.set(key, []);
      best.get(key).push({ q, uhd: raw === 4, id: c.stream_id, i: kept.length });
      kept.push(c.stream_id);
    }
    const keep = new Set();
    state.altMap = new Map();
    state.altOf = new Map();
    for (const versions of best.values()) {
      let sorted = versions.sort((a, b) => b.q - a.q || a.i - b.i);
      if ($('opt-uhd').value === 'never' && sorted.some((v) => !v.uhd)) sorted = sorted.filter((v) => !v.uhd);
      const ranked = sorted.map((v) => v.id);
      keep.add(ranked[0]);
      if (ranked.length > 1) {
        state.altMap.set(ranked[0], ranked.slice(1, 9));
        ranked.slice(1).forEach((id, n) => state.altOf.set(id, { kept: ranked[0], rank: n + 1, total: ranked.length - 1 }));
      }
    }
    state.hiddenCache = new Set(kept.filter((id) => !keep.has(id)));
    return state.hiddenCache;
  }
  /** Versions de secours d'une chaîne gardée (doublons masqués), dans l'ordre d'essai. */
  function backupsOf(id) {
    hiddenDuplicates();
    return (state.altMap && $('opt-dedupe').checked && state.altMap.get(id)) || [];
  }

  // ----------------------------------------------------------- sélection
  const inCategory = (c) => c.category_ids.some((id) => catOn(state.kind, id));
  const isOn = (c) => !adultHidden(c) && !state.excluded.has(c.stream_id) && (inCategory(c) || state.included.has(c.stream_id));

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
      let excluded = [...sel.excluded];
      if (!sorted) excluded = excluded.filter((id) => { const c = byId.get(id); return !c || c.category_ids.some((cid) => catOn(kind, cid)); });
      const list = (set) => (sorted ? [...set].sort() : [...set]);
      data[catField] = list(sel.selected);
      data[incField] = list(sel.included);
      data[excField] = sorted ? excluded.sort() : excluded;
    }
    data.live_format = $('opt-live-format').value;
    data.catchup = $('opt-catchup').checked;
    data.dedupe = $('opt-dedupe').checked;
    data.failover = $('opt-failover').checked;
    data.failover_any_category = $('opt-failover-any').checked;
    data.adaptive_failover = $('opt-adaptive-failover').checked;
    data.uhd = $('opt-uhd').value;
    data.group_epg_external = $('opt-group-epg').checked;
    data.multi_groups = $('opt-multi-groups').checked;
    data.hq_logos = $('opt-hq-logos').checked;
    data.provider = hasMultipleSources() ? state.profile : '';
    data.mix = hasMultipleSources() && $('opt-provider').value === 'mix';
    data.hide_separators = $('opt-separators').checked;
    data.hide_adult = $('opt-adult').checked;
    data.clean_names = $('opt-clean-names').checked;
    data.favorites_name = $('opt-fav-name').value.trim() || '★ Favoris';
    data.rules = state.rules.map((r) => ({ ...r }));
    data.favorites = sorted ? [...state.favorites].sort() : [...state.favorites];
    const sortedObject = (obj) => Object.fromEntries(Object.keys(obj).sort().map((k) => [k, obj[k]]));
    data.item_names = sortedObject(Object.fromEntries(Object.entries(state.itemNames).filter(([, v]) => String(v).trim()).map(([k, v]) => [k, String(v).trim()])));
    data.item_numbers = sortedObject(state.itemNumbers);
    data.group_order = [...state.groupOrder];
    data.group_names = cleanGroupNames();
    data.item_logos = sortedObject(state.itemLogos);
    data.item_epg = sortedObject(state.itemEpg);
    data.item_xmltv = sortedObject(state.itemXmltv);
    data.item_pinned = sortedObject(state.itemPinned);
    data.custom_groups = state.customGroups.map((g) => ({ id: g.id, name: String(g.name || '').trim() || g.id, match: g.match,
      ...(g.region ? { region: g.region } : {}),
      patterns: [...new Set((g.patterns || []).map((p) => String(p).trim()).filter(Boolean))] }));
    const known = new Set(state.customGroups.map((g) => g.id));
    data.item_groups = sortedObject(Object.fromEntries(Object.entries(state.itemGroups).filter(([, v]) => v === '-' || known.has(v))));
    return data;
  }

  function cleanGroupNames() {
    const result = {};
    for (const id of Object.keys(state.groupNames).sort()) {
      if (id.startsWith(CG)) continue;
      const name = String(state.groupNames[id] || '').trim();
      if (name && name !== groupInfo(id).name) result[id] = name.slice(0, 100);
    }
    return result;
  }

  const signature = () => JSON.stringify(payload({ sorted: true }));
  const hasSelection = () => state.rules.length > 0 || KINDS.some((k) => state.sel[k].selected.size || state.sel[k].included.size);
  const isDirty = () => signature() !== state.snapshot;

  function countFor(p, kind = 'live') {
    const [catField, incField, excField] = KIND[kind].fields;
    const cats = new Set([...(p[catField] || []), ...ruleSet(kind, p.rules || [])]);
    const inc = new Set(p[incField] || []), exc = new Set(p[excField] || []);
    let n = 0;
    for (const c of state.cat[kind].channels) {
      if (p.hide_adult && isAdultItem(c, kind)) continue;
      if (!exc.has(c.stream_id) && (c.category_ids.some((id) => cats.has(id)) || inc.has(c.stream_id))) n += 1;
    }
    return n;
  }

  /** Nombre d'éléments retenus pour la sélection en cours, par type. */
  function selectionCount(kind) {
    const sel = state.sel[kind];
    let n = 0;
    for (const c of state.cat[kind].channels) {
      if (adultHidden(c, kind)) continue;
      if (!sel.excluded.has(c.stream_id) && (c.category_ids.some((id) => catOn(kind, id)) || sel.included.has(c.stream_id))) n += 1;
    }
    if (kind !== 'live') return n;
    let extraCopies = 0;
    if ($('opt-multi-groups').checked) {
      const hidden = hiddenDuplicates();
      for (const [sid, more] of groupExtras()) {
        if (!hidden.has(sid)) extraCopies += more.length;
      }
    }
    return n - hiddenDuplicates().size + extraCopies;
  }

  const usesKind = (p, kind) => Boolean((p[KIND[kind].fields[0]] || []).length || (p[KIND[kind].fields[1]] || []).length
    || (p.rules || []).some((r) => r.kind === kind));

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
    const catsOn = (k) => state.cat[k].categories.filter((cat) => catOn(k, cat.category_id)).length || state.sel[k].selected.size;
    $('stat-categories').textContent = KINDS.reduce((n, k) => n + catsOn(k), 0).toLocaleString('fr-FR');
    const n = counts[state.kind] || 0;
    $('tab-cat-count').textContent = state.categories.length ? `(${catsOn(state.kind)}/${state.categories.length})` : '';
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
    const missing = ['vod', 'series'].filter((kind) => available(kind) > 0 && !state.sel[kind].selected.size
      && !state.sel[kind].included.size && !state.rules.some((r) => r.kind === kind));
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
    const visible = epgVisible();
    $('epg-content').hidden = !visible;
    $('epg-unavailable').hidden = visible;
    $('epg-pill').hidden = !visible;
    if (!visible) return;
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

  let lastMissing = [];   // chaînes sans programme de la playlist ouverte (dernier état du guide)

  function renderEpg(data) {
    const pl = data.playlist;
    const src = data.source || {};
    const sourceNameLabel = data.custom_source ? 'la source EPG_URL' : 'xmltv.php';
    $('epg-xmltv-label').textContent = `via ${data.custom_source ? 'EPG_URL' : 'xmltv.php'}`;
    if (!pl) {
      setEpgPill('error', 'Guide indisponible');
      ['epg-covered', 'epg-xmltv', 'epg-external', 'epg-api', 'epg-missing-count'].forEach((id) => { $(id).textContent = '–'; });
      $('epg-source').textContent = data.error || 'Guide indisponible.';
      $('epg-missing-box').hidden = true;
      return;
    }
    const external = pl.external || 0;
    const covered = pl.xmltv + external + pl.api;
    const pct = pl.channels ? Math.round((covered / pl.channels) * 100) : 0;
    $('epg-covered').textContent = `${covered} / ${pl.channels}`;
    $('epg-xmltv').textContent = pl.xmltv.toLocaleString('fr-FR');
    $('epg-external').textContent = external.toLocaleString('fr-FR');
    $('epg-api').textContent = data.mode === 'xmltv' ? 'désactivé' : pl.api.toLocaleString('fr-FR');
    $('epg-missing-count').textContent = pl.missing.length.toLocaleString('fr-FR');
    const mine = pl.mine && pl.mine.channels ? pl.mine : null;
    const minePct = mine ? Math.round((mine.covered / mine.channels) * 100) : null;
    const mineText = mine ? `Mes groupes ${minePct} % · ` : '';
    if (pl.pending) setEpgPill('warn', `${mineText}récupération en cours… ${pct} %`);
    else if (!pl.channels) setEpgPill('', 'Aucune chaîne');
    else if (!covered) setEpgPill('error', 'Aucun programme');
    else if (mine) setEpgPill(minePct >= 80 ? 'ok' : 'warn', `Mes groupes ${minePct} % · toutes ${pct} %`);
    else setEpgPill(pl.missing.length ? 'warn' : 'ok', `${pct} % des chaînes`);
    const parts = [];
    if (mine) parts.push(`Mes groupes et favoris : ${mine.covered} / ${mine.channels} chaînes avec programmes (récupérées en premier).`);
    if (external) parts.push(`${plural(external, 'chaîne couverte', 'chaînes couvertes')} par les guides XMLTV externes.`);
    if (data.mode === 'api') parts.push('Mode EPG_SOURCE=api : programmes récupérés chaîne par chaîne via l\'API Xtream.');
    else if (src.ok) {
      const when = src.updated_at ? new Date(src.updated_at * 1000).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' }) : '';
      parts.push(`Source ${sourceNameLabel} : ${plural(src.channels, 'chaîne', 'chaînes')}, ${plural(src.programmes, 'programme', 'programmes')}${when ? `, mise à jour le ${when}` : ''}.`);
      if (data.mode === 'auto' && pl.api) parts.push(`Les chaînes absentes de ${sourceNameLabel} sont complétées par l'API Xtream.`);
    } else {
      parts.push(`${sourceNameLabel} ne fonctionne pas${src.error ? ` (${src.error})` : ''}${data.mode === 'auto' ? ' : les programmes sont récupérés chaîne par chaîne via l\'API Xtream.' : '.'}`);
    }
    if (pl.pending) parts.push(`${plural(pl.pending, 'chaîne', 'chaînes')} en cours de récupération (actualisation automatique).`);
    $('epg-source').textContent = parts.join(' ');
    lastMissing = pl.missing;
    renderMissing();
    $('epg-missing-summary').textContent = `${plural(pl.missing.length, 'chaîne', 'chaînes')} sans programme (le fournisseur n'en publie pas)`;
    $('epg-missing-box').hidden = !pl.missing.length;
  }

  /** Chaînes sans programme, avec les guides proposés (noms proches) à choisir en un clic. */
  function renderMissing() {
    const list = $('epg-missing');
    list.replaceChildren();
    const suggestions = state.epgSuggestions || {};
    for (const item of lastMissing.slice(0, 300)) {
      const chosen = state.itemXmltv[item.stream_id];
      const options = suggestions[item.stream_id] || [];
      const meta = chosen ? `guide choisi : ${chosen} (à enregistrer)` : item.epg_id ? `tvg-id ${item.epg_id}` : 'sans identifiant EPG';
      const row = h('li', { class: 'epg-row' }, h('span', { class: 'epg-name' }, h('span', { text: item.name }),
        h('span', { class: `num${chosen ? ' ok-text' : ''}`, text: meta })));
      if (options.length && !chosen) {
        row.append(h('span', { class: 'epg-choices' }, ...options.map((o) => h('button', {
          type: 'button', class: 'btn small', title: `${o.source} · ${o.programmes} programmes · ressemblance ${o.score} %`,
          text: `${o.name} · ${o.score} %`,
          onclick: () => { state.itemXmltv[item.stream_id] = o.id; refresh({ channels: false }); renderMissing(); },
        }))));
      } else if (chosen) {
        row.append(h('button', { type: 'button', class: 'btn small ghost', text: 'Annuler',
          onclick: () => { delete state.itemXmltv[item.stream_id]; refresh({ channels: false }); renderMissing(); } }));
      }
      list.append(row);
    }
    if (lastMissing.length > 300) list.append(h('li', { class: 'empty', text: `… et ${lastMissing.length - 300} autres` }));
  }

  /** Guide de chaque chaîne de « Mes groupes » : source actuelle, changement manuel (recherche dans tous les guides). */
  async function loadGroupGuides() {
    if (!state.editing) { toast('Enregistre d\'abord la playlist', 'error'); return; }
    const button = $('epg-groups-load');
    button.disabled = true;
    button.textContent = 'Recherche des guides…';
    try {
      const data = await request(`/api/epg/groups?slug=${encodeURIComponent(state.editing)}`);
      renderGroupGuides(data);
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'Actualiser la liste';
    }
  }

  function renderGroupGuides(data) {
    const list = $('epg-groups');
    if (!data.channels.length) {
      list.replaceChildren(h('li', { class: 'empty', text: 'Aucune chaîne dans « Mes groupes » (Organisation → ajoute OTT, TNT, Maroc…).' }));
      return;
    }
    $('epg-groups-hint').textContent = data.external_guides
      ? `${data.channels.filter((c) => c.source.startsWith('external')).length} / ${data.channels.length} chaînes avec un guide externe. « Changer » pour choisir à la main, puis enregistre la playlist.`
      : 'Aucun guide externe ajouté (Système → Accès & Guides) : les chaînes utilisent le guide du fournisseur.';
    list.replaceChildren(...data.channels.map((c) => {
      const chosen = state.itemXmltv[c.stream_id];
      const where = chosen && !c.manual ? `choisi : ${chosen} (à enregistrer)`
        : `${c.source_label}${c.guide_name ? ` · ${c.guide_name}` : ''}${c.id ? ` (${c.id})` : ''}${c.manual ? ' · choisi à la main' : ''}`;
      const cls = c.source === 'none' ? 'warn-text' : c.source.startsWith('external') ? 'ok-text' : 'muted';
      const picker = h('div', { class: 'epg-pick', hidden: true });
      const row = h('li', { class: 'epg-row' },
        h('span', { class: 'epg-name' }, h('span', { text: `${c.group} · ${c.name}` }), h('span', { class: `num ${cls}`, text: where })),
        h('span', { class: 'epg-choices' },
          h('button', { type: 'button', class: 'btn small', text: 'Changer', onclick: () => openGuidePicker(c, picker) }),
          (c.manual || chosen) ? h('button', { type: 'button', class: 'btn small ghost', text: 'Automatique',
            onclick: () => { delete state.itemXmltv[c.stream_id]; refresh({ channels: false }); toast('Guide automatique : enregistre la playlist'); } }) : null),
        picker);
      return row;
    }));
  }

  function openGuidePicker(c, box) {
    if (!box.hidden) { box.hidden = true; box.replaceChildren(); return; }
    const input = h('input', { type: 'search', placeholder: 'Nom de la chaîne dans le guide (ex. TF1, 2M)', value: String(c.name).replace(/^[A-Z]{2,4}\s*[:|]\s*/i, '') });
    const results = h('ul', { class: 'sum-list epg-results' });
    const search = debounce(async () => {
      const q = input.value.trim();
      if (q.length < 2) { results.replaceChildren(); return; }
      try {
        const data = await request(`/api/epg/search?q=${encodeURIComponent(q)}`);
        results.replaceChildren(...data.results.map((r) => h('li', null,
          h('span', null, h('strong', { text: r.name }), h('span', { class: 'muted small', text: ` · ${r.source_label} · ${r.id} · ${r.programmes} programmes` })),
          h('button', { type: 'button', class: 'btn small', text: 'Choisir', onclick: () => {
            state.itemXmltv[c.stream_id] = r.id;
            refresh({ channels: false });
            box.hidden = true;
            toast(`Guide « ${r.name} » choisi pour ${c.name} : enregistre la playlist`);
          } }))));
        if (!data.results.length) results.append(h('li', { class: 'empty', text: 'Aucune chaîne de ce nom dans les guides.' }));
      } catch (error) { results.replaceChildren(h('li', { class: 'empty', text: error.message })); }
    }, 250);
    input.addEventListener('input', search);
    box.replaceChildren(input, results);
    box.hidden = false;
    input.focus();
    search();
  }

  async function suggestGuides() {
    if (!state.editing) return;
    const button = $('epg-suggest');
    button.disabled = true;
    button.textContent = 'Recherche…';
    try {
      const data = await request(`/api/epg/suggest?slug=${encodeURIComponent(state.editing)}`);
      state.epgSuggestions = Object.fromEntries(data.suggestions.map((s) => [s.stream_id, s.suggestions]));
      renderMissing();
      toast(data.suggestions.length
        ? `${plural(data.suggestions.length, 'chaîne', 'chaînes')} avec un guide proposé : choisis, puis enregistre la playlist`
        : 'Aucun guide au nom proche trouvé');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
      button.textContent = 'Proposer des guides';
    }
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
    if ($('opt-dedupe').checked) parts.push('Sans doublons');
    if ($('opt-separators').checked) parts.push('Sans séparateurs');
    if ($('opt-adult').checked) parts.push('Sans 18+');
    if ($('opt-clean-names').checked) parts.push('Mode Décodeur');
    if ($('opt-hq-logos').checked) parts.push('Logos HQ');
    const failoverActive = $('opt-failover').checked && $('opt-dedupe').checked && $('opt-live-format').value !== 'hls';
    if (failoverActive) parts.push('Bascule auto');
    $('opt-failover-hint').classList.toggle('warn-text', $('opt-failover').checked && (!$('opt-dedupe').checked || $('opt-live-format').value === 'hls'));
    $('opt-failover-any').disabled = !$('opt-failover').checked;
    $('opt-adaptive-failover').disabled = !$('opt-failover').checked;
    $('opt-uhd').disabled = !$('opt-dedupe').checked;
    if (!$('opt-failover').checked) {
      $('opt-failover-any').checked = false;
      $('opt-adaptive-failover').checked = false;
    }
    if (failoverActive && $('opt-failover-any').checked) parts[parts.length - 1] += ' (toutes catégories)';
    if (failoverActive && $('opt-adaptive-failover').checked) parts.push('Adaptatif');
    $('options-pill-text').textContent = parts.join(' · ');
  }

  async function loadSummary(force = false) {
    const params = new URLSearchParams();
    if (force) params.set('refresh', '1');
    if (hasMultipleSources()) params.set('profile', state.profile);
    const qs = params.toString() ? `?${params}` : '';
    try {
      state.summary = await request(`/api/catalog/summary${qs}`);
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
    const sorted = [...state.playlists].filter(inProfile).sort((a, b) => collator.compare(a.name, b.name));
    const badge = $('nav-badge-playlists');
    if (badge) {
      badge.textContent = String(sorted.length);
      badge.hidden = sorted.length === 0;
    }
    $('help-card').hidden = sorted.length > 0;
    if (!state.fullCardTouched) $('full-card').open = sorted.length === 0;
    if (!sorted.length) {
      list.append(h('li', { class: 'empty', text: multiProfile()
        ? `Aucune playlist dans le profil « ${profileName(state.profile)} ». Crée-la avec « + Nouvelle ».`
        : 'Aucune playlist. Crée la première avec « + Nouvelle ».' }));
      return;
    }
    for (const p of sorted) {
      const parts = KINDS.filter((k) => state.cat[k].catalogLoaded && usesKind(p, k)).map((k) => noun(countFor(p, k), k));
      const count = parts.length ? ` · ${parts.join(' · ')}` : '';
      list.append(h('li', null, h('button', {
        type: 'button', class: 'playlist-item', 'aria-current': p.slug === state.editing ? 'true' : 'false',
        onclick: () => openPlaylist(p.slug),
      }, h('span', { class: 'pl-name', text: p.name },
        hasMultipleSources() && playlistMixed(p) && allowMix() ? h('span', { class: 'pl-tag', text: 'mélange' }) : null),
      h('span', { class: 'pl-meta', text: `/p/${p.slug}.m3u${count}` }))));
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

  /** Catégories regroupées par abonnement (celui de l'espace affiché d'abord), seulement s'il y en a plusieurs. */
  function providerGroups(visible) {
    const ids = [...new Set(state.categories.map((c) => c.profile || '1'))];
    if (ids.length < 2) return [];
    ids.sort((a, b) => (a === state.profile ? -1 : b === state.profile ? 1 : Number(a) - Number(b)));
    const hideAdult = $('opt-adult').checked;
    return ids.map((id) => ({
      id, name: sourceName(id),
      all: state.categories.filter((c) => (c.profile || '1') === id && (!hideAdult || !isAdultCategory(c.name))),
      cats: visible.filter((c) => (c.profile || '1') === id),
    })).filter((g) => g.cats.length);
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
    const hideAdult = $('opt-adult').checked;
    const retained = categoryStats();
    const visible = state.categories.filter((cat) => {
      if (hideAdult && isAdultCategory(cat.name)) return false;
      if (query && !cat._search.includes(query)) return false;
      if (state.catFilter === 'on' && !catOn(state.kind, cat.category_id)) return false;
      if (state.catFilter === 'off' && catOn(state.kind, cat.category_id)) return false;
      return true;
    });
    const clusters = new Map();
    for (const cat of state.categories) {
      if (hideAdult && isAdultCategory(cat.name)) continue;
      const base = `${cat.profile || '1'}|${baseName(cat.name) || cat.name}`;
      if (!clusters.has(base)) clusters.set(base, []);
      clusters.get(base).push(cat);
    }
    const fragment = document.createDocumentFragment();
    const items = new Map();
    for (const cat of visible) {
      const ruled = !state.selected.has(cat.category_id) && ruleSet(state.kind).has(cat.category_id);
      const on = catOn(state.kind, cat.category_id);
      const kept = retained.get(cat.category_id) || 0;
      let metaText = noun(cat.count);
      let partial = false;
      const parent = cat.parent_id && state.categoryNames.get(cat.parent_id);
      if (on && kept !== cat.count) { metaText = `${kept} / ${cat.count} ${K().many} ${K().kept}`; partial = true; }
      else if (!on && kept) { metaText = `${noun(cat.count)} · ${kept} ${K().added.toLowerCase()}${kept > 1 ? 's' : ''}`; partial = true; }
      const box = h('input', { type: 'checkbox', checked: on, disabled: ruled, 'aria-label': `Inclure la catégorie ${cat.name}`,
        title: ruled ? 'Ajoutée par une règle automatique' : null });
      box.addEventListener('change', () => {
        if (box.checked) state.selected.add(cat.category_id); else state.selected.delete(cat.category_id);
        refresh({ channels: false });
      });
      if (ruled) { metaText = `${metaText} · ajoutée par une règle`; partial = true; }
      const siblings = (clusters.get(`${cat.profile || '1'}|${baseName(cat.name) || cat.name}`) || []).filter((c) => c.category_id !== cat.category_id);
      const uncheckedSiblings = siblings.filter((c) => !state.selected.has(c.category_id));
      items.set(cat.category_id, h('label', { class: `cat${on ? ' on' : ''}${ruled ? ' ruled' : ''}` },
        box,
        h('span', { class: 'grow' }, h('span', { class: 'name', text: cat.name }),
          parent ? h('span', { class: 'meta', text: `Sous-groupe de ${parent}` }) : null,
          h('span', { class: `meta${partial ? ' partial' : ''}`, text: metaText }),
          uncheckedSiblings.length ? h('button', { type: 'button', class: 'link-btn qlink',
            text: `🔗 Cocher aussi ${plural(uncheckedSiblings.length, 'catégorie liée', 'catégories liées')} (même chaînes, autre qualité)`,
            title: uncheckedSiblings.map((c) => c.name).join(', '),
            onclick: (event) => {
              event.preventDefault();
              for (const c of uncheckedSiblings) state.selected.add(c.category_id);
              state.selected.add(cat.category_id);
              refresh({ channels: false });
            } }) : null),
        cat.count ? h('button', {
          type: 'button', class: 'view', text: 'Voir', title: `Voir les ${K().many} de ${cat.name}`,
          onclick: (event) => { event.preventDefault(); showCategoryChannels(cat.category_id); },
        }) : null,
      ));
    }
    const groups = providerGroups(visible);
    if (groups.length < 2) fragment.append(...items.values());
    else {
      for (const group of groups) {
        const checked = group.all.filter((c) => catOn(state.kind, c.category_id)).length;
        const details = h('details', { class: 'provider-group', open: query || state.openGroups.has(group.id) },
          h('summary', {},
            h('span', { class: 'pg-name', text: group.name }),
            h('span', { class: `pg-count${checked ? ' on' : ''}`,
              text: `${checked} cochée${checked > 1 ? 's' : ''} sur ${plural(group.all.length, 'catégorie', 'catégories')}` })),
          h('div', { class: 'category-grid' }, ...group.cats.map((c) => items.get(c.category_id))));
        details.addEventListener('toggle', () => {
          if (query) return;
          if (details.open) state.openGroups.add(group.id); else state.openGroups.delete(group.id);
          storage('xtream-m3u-open-providers', JSON.stringify([...state.openGroups]));
        });
        fragment.append(details);
      }
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
      if (adultHidden(c)) return false;
      if (category && !c.category_ids.includes(category)) return false;
      if (terms.length) {
        const custom = state.itemNames[keyOf(c)];
        const hay = custom ? `${c._search} ${norm(custom)}` : c._search;
        if (!terms.every((t) => hay.includes(t))) return false;
      }
      switch (mode) {
        case 'on': return isOn(c);
        case 'off': return !isOn(c);
        case 'included': return state.included.has(c.stream_id);
        case 'excluded': return state.excluded.has(c.stream_id);
        case 'favorites': return state.favorites.includes(keyOf(c));
        case 'custom': return Boolean(state.itemNames[keyOf(c)] || state.itemLogos[keyOf(c)]
          || (state.kind === 'live' && (state.itemNumbers[c.stream_id] || state.itemEpg[c.stream_id])));
        case 'grouped': return state.kind === 'live' && groupAssignment().has(c.stream_id);
        default: return true;
      }
    });
  }

  function thumb(c, kind = state.kind) {
    const hq = kind === 'live' && c.logo_hq && $('opt-hq-logos').checked ? c.logo_hq : '';
    const logo = state.itemLogos[keyOf(c, kind)] || hq || c.logo;
    const box = h('span', { class: `thumb${kind === 'live' ? '' : ' poster'}`, 'aria-hidden': 'true', text: String(c.name).replace(/[^\p{L}\p{N}]/gu, '').slice(0, 2).toUpperCase() || 'TV' });
    if (logo) {
      // Image ajoutée tout de suite : une image « lazy » hors de la page ne se charge jamais.
      const img = h('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer', class: 'pending' });
      const initials = box.firstChild;
      img.addEventListener('load', () => { initials.remove(); img.classList.remove('pending'); }, { once: true });
      img.addEventListener('error', () => img.remove(), { once: true });
      img.src = logo;
      box.append(img);
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
          h('span', { class: 'ch-name', text: displayName(c) }),
          state.itemNames[keyOf(c)] ? h('span', { class: 'ch-orig', text: `Nom d'origine : ${c.name}` }) : null,
          h('span', { class: 'ch-meta', text: itemMeta(c) }),
          state.kind === 'live' ? backupList(c) : null,
          state.kind === 'series' ? episodesButton(c) : null),
        h('span', { class: 'badges' }, badgesFor(c)),
        itemActions(c),
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

  /** « 🔁 4 versions de secours » : dépliable, dans l'ordre où la bascule automatique les essaie. */
  function backupList(c) {
    const ids = backupsOf(c.stream_id);
    if (!ids.length) return null;
    const byId = state.cat.live.channelById;
    const failover = $('opt-failover').checked;
    const pinned = state.itemPinned[c.stream_id] || '';
    const allIds = [c.stream_id, ...ids];
    const details = h('details', { class: 'alt-list' },
      h('summary', null,
        h('span', { text: `🔁 ${plural(ids.length, failover ? 'version de secours' : 'autre version masquée', failover ? 'versions de secours' : 'autres versions masquées')}` })),
      h('ol', null, allIds.map((id, idx) => {
        const other = byId.get(id);
        const label = other ? `${other.name} · ${(other.category_ids || []).map((cid) => state.cat.live.categoryNames.get(cid) || cid).join(', ')}` : `Chaîne ${id}`;
        const isPin = pinned ? id === pinned : idx === 0;
        return h('li', null, label, isPin ? h('span', { class: 'badge ok', text: pinned ? '📌 Épinglée n° 1' : 'n° 1' }) : null);
      })),
      h('div', { class: 'btn-row' },
        h('button', { type: 'button', class: 'btn small', text: '🧪 Tester & choisir la n° 1',
          onclick: (event) => { event.preventDefault(); event.stopPropagation(); openVersionProbe(c.stream_id, displayName(c), allIds); } }),
        pinned ? h('button', { type: 'button', class: 'btn small ghost', text: 'Retirer l\'épingle',
          onclick: (event) => { event.preventDefault(); event.stopPropagation(); delete state.itemPinned[c.stream_id]; refresh(); toast('Version par défaut rétablie'); } }) : null),
      failover ? null : h('p', { class: 'hint', text: 'Active « Bascule automatique » (Options) pour que le serveur les utilise si la chaîne coupe.' }));
    // La ligne est un <label> : sans ça, un clic sur le résumé cocherait/décocherait la chaîne.
    details.querySelector('summary').addEventListener('click', (event) => { event.preventDefault(); details.open = !details.open; });
    details.addEventListener('click', (event) => { if (event.target.tagName !== 'SUMMARY' && event.target.tagName !== 'BUTTON') event.preventDefault(); });
    return details;
  }

  let probeContext = null;
  async function openVersionProbe(canonicalId, title, ids) {
    const uniqueIds = [...new Set((ids || []).map(String).filter(Boolean))].slice(0, 6);
    if (!uniqueIds.length) return;
    probeContext = { canonicalId: canonicalId ? String(canonicalId) : '', title: title || 'Chaîne', ids: uniqueIds };
    const dialog = $('probe-dialog');
    $('probe-title').textContent = `Versions de « ${probeContext.title} »`;
    $('probe-status').textContent = `Test séquentiel doux de ${plural(uniqueIds.length, 'version', 'versions')}…`;
    $('probe-list').replaceChildren(h('li', { class: 'empty', text: 'Vérification des flux en cours (jusqu\'à quelques secondes)…' }));
    $('probe-reset-health').hidden = true;
    $('probe-rerun').disabled = true;
    if (!dialog.open && typeof dialog.showModal === 'function') dialog.showModal();
    try {
      const data = await request('/api/channels/probe', 'POST', { stream_ids: uniqueIds });
      if (!probeContext || probeContext.title !== (title || 'Chaîne')) return;
      renderProbeResults(data.results || []);
    } catch (error) {
      $('probe-status').textContent = error.message;
      $('probe-list').replaceChildren(h('li', { class: 'empty', text: 'Impossible de tester les versions.' }));
    } finally {
      $('probe-rerun').disabled = false;
    }
  }

  function renderProbeResults(results) {
    const list = $('probe-list');
    list.replaceChildren();
    if (!results.length) {
      $('probe-status').textContent = 'Aucun résultat.';
      list.append(h('li', { class: 'empty', text: 'Aucune version testée.' }));
      return;
    }
    const okCount = results.filter((r) => r.ok).length;
    const anyQuarantined = results.some((r) => (r.quarantined_for || 0) > 0);
    $('probe-reset-health').hidden = !anyQuarantined;
    $('probe-status').textContent = `${okCount}/${results.length} version${results.length > 1 ? 's' : ''} joignable${okCount > 1 ? 's' : ''}. Épingle ta préférée : elle passera en n° 1 sans casser l'EPG ni tes favoris.`;
    const canon = probeContext && probeContext.canonicalId;
    const currentPin = canon ? (state.itemPinned[canon] || '') : '';
    const activeFirst = currentPin || (canon || results[0].stream_id);
    for (const r of results) {
      const qMin = r.quarantined_for ? Math.max(1, Math.ceil(r.quarantined_for / 60)) : 0;
      const metaBits = [];
      if (r.ok) metaBits.push(`✅ HTTP ${r.status || 200} · ${r.latency_ms} ms`);
      else metaBits.push(`❌ ${r.error || `HTTP ${r.status || '?'}`}${r.latency_ms ? ` · ${r.latency_ms} ms` : ''}`);
      if (hasMultipleSources() && r.provider_name) metaBits.push(`🔑 ${r.provider_name}`);
      const isFirst = r.stream_id === activeFirst;
      const actions = [];
      if (canon) {
        if (isFirst) {
          actions.push(h('span', { class: 'badge ok', text: currentPin ? '📌 Épinglée n° 1' : 'n° 1 actuelle' }));
          if (currentPin) {
            actions.push(h('button', { type: 'button', class: 'btn small ghost', text: 'Par défaut',
              onclick: () => {
                delete state.itemPinned[canon];
                refresh();
                renderProbeResults(results);
                toast('Version par défaut rétablie — pense à enregistrer');
              } }));
          }
        } else {
          actions.push(h('button', { type: 'button', class: 'btn small primary', text: '📌 Épingler n° 1',
            onclick: () => {
              if (r.stream_id === canon) delete state.itemPinned[canon];
              else state.itemPinned[canon] = r.stream_id;
              refresh();
              renderProbeResults(results);
              toast(`« ${r.name} » épinglée en n° 1 — pense à enregistrer la playlist`);
            } }));
        }
      }
      list.append(h('li', { class: `probe-row${r.ok ? ' is-ok' : ' is-err'}` },
        h('span', { class: 'probe-info' },
          h('strong', { text: r.name }),
          h('span', { class: 'muted small', text: metaBits.join(' · ') }),
          qMin ? h('span', { class: 'badge warn', text: `⏳ En quarantaine (${qMin} min restantes)` }) : null),
        h('span', { class: 'btn-row' }, ...actions)));
    }
  }

  function itemMeta(c) {
    const cats = c.category_ids.map((id) => state.categoryNames.get(id) || 'Sans catégorie').join(' · ');
    if (state.kind === 'live') {
      const custom = state.itemNumbers[c.stream_id];
      return (custom ? `N° ${custom} (perso) · ` : c.num ? `N° ${c.num} · ` : '') + cats;
    }
    return [c.year, c.rating ? `★ ${c.rating}` : '', cats, c.ext ? c.ext.toUpperCase() : ''].filter(Boolean).join(' · ');
  }

  function badgesFor(c) {
    const badges = [];
    if (state.included.has(c.stream_id)) badges.push(h('span', { class: 'badge ok', text: K().added }));
    if (state.excluded.has(c.stream_id)) badges.push(h('span', { class: 'badge danger', text: K().excludedLabel }));
    const grouped = state.kind === 'live' ? groupAssignment().get(c.stream_id) : null;
    if (grouped) badges.push(h('span', { class: 'badge grp', text: `📁 ${customName(grouped)}`,
      title: state.itemGroups[c.stream_id] ? 'Rangée à la main dans ce groupe' : 'Rangée automatiquement (nom correspondant)' }));
    if (state.kind === 'live' && state.itemPinned[c.stream_id]) {
      const target = state.cat.live.channelById.get(state.itemPinned[c.stream_id]);
      badges.push(h('span', { class: 'badge ok', text: '📌 Épinglée',
        title: `Version préférée en n° 1 : ${target ? target.name : state.itemPinned[c.stream_id]}` }));
    }
    if (c.epg) badges.push(h('span', { class: 'badge opt', text: 'EPG', title: 'Identifiant EPG disponible' }));
    if (c.archive) badges.push(h('span', { class: 'badge opt', text: `Replay ${c.archive} j`, title: 'Programmes passés disponibles (catch-up)' }));
    if (state.kind === 'live' && hiddenDuplicates().has(c.stream_id)) {
      const sep = $('opt-separators').checked && isSeparator(c.name);
      const alt = !sep && state.altOf && state.altOf.get(c.stream_id);
      const main = alt && state.cat.live.channelById.get(alt.kept);
      badges.push(h('span', { class: 'badge warn', text: sep ? (PLACEHOLDER.test(c.name) ? 'Emplacement vide masqué' : 'Séparateur masqué') : alt && $('opt-failover').checked ? `Secours ${alt.rank}/${alt.total}` : 'Doublon masqué',
        title: sep ? 'Fausse chaîne servant de titre de section chez le fournisseur'
          : main ? `Version masquée de « ${main.name} »${$('opt-failover').checked ? ` : essayée en n° ${alt.rank} si elle coupe` : ''}` : 'Une meilleure qualité de cette chaîne est déjà dans le groupe' }));
    }
    return badges;
  }

  function toggleFavorite(c) {
    const key = keyOf(c);
    state.favorites = state.favorites.includes(key) ? state.favorites.filter((k) => k !== key) : [...state.favorites, key];
  }

  /** Fenêtre « Personnaliser » : nom, numéro, logo et guide emprunté (direct). */
  function editItem(c) {
    const isLive = state.kind === 'live';
    const key = keyOf(c);
    const dialog = $('item-dialog');
    $('item-title').textContent = `Personnaliser « ${c.name} »`;
    $('item-name').value = state.itemNames[key] || '';
    $('item-name').placeholder = c.name;
    $('item-number-field').hidden = !isLive;
    $('item-number').value = isLive ? state.itemNumbers[c.stream_id] || '' : '';
    $('item-logo').value = state.itemLogos[key] || '';
    $('item-logo').placeholder = c.logo || 'https://…/logo.png';
    $('item-epg-field').hidden = !isLive;
    const byLabel = new Map();
    if (isLive) {
      const list = $('item-epg-list');
      list.replaceChildren();
      for (const other of state.cat.live.channels) {
        if (other.stream_id === c.stream_id || isSeparator(other.name)) continue;
        if (!isOn(other) && !(c._core && (other._core ?? (other._core = matchName(other.name))) === c._core)) continue;
        const label = `${other.name} (n° ${other.stream_id})`;
        byLabel.set(label, other.stream_id);
        if (byLabel.size <= 3000) list.append(h('option', { value: label }));
      }
      const source = state.itemEpg[c.stream_id] && state.cat.live.channelById.get(state.itemEpg[c.stream_id]);
      $('item-epg').value = source ? `${source.name} (n° ${source.stream_id})` : (state.itemEpg[c.stream_id] || '');
    }
    if (typeof dialog.showModal !== 'function') return;
    dialog.returnValue = 'cancel';
    dialog.addEventListener('close', () => {
      if (dialog.returnValue !== 'ok') return;
      const name = $('item-name').value.trim();
      if (name && name !== c.name) state.itemNames[key] = name.slice(0, 100); else delete state.itemNames[key];
      const logo = $('item-logo').value.trim();
      if (/^https?:\/\/\S{3,}$/.test(logo)) state.itemLogos[key] = logo; else delete state.itemLogos[key];
      if (logo && !state.itemLogos[key]) toast('Logo ignoré : il faut une adresse http(s)', 'warn');
      if (isLive) {
        const number = parseInt($('item-number').value, 10);
        if (Number.isInteger(number) && number >= 1 && number <= 99999) state.itemNumbers[c.stream_id] = number;
        else delete state.itemNumbers[c.stream_id];
        const text = $('item-epg').value.trim();
        const sourceId = byLabel.get(text) || (/\(n° (\d+)\)$/.exec(text) || [])[1] || (/^\d+$/.test(text) ? text : '');
        if (sourceId && sourceId !== c.stream_id) state.itemEpg[c.stream_id] = sourceId; else delete state.itemEpg[c.stream_id];
        if (text && !sourceId) toast('Chaîne du guide introuvable : choisis-la dans la liste proposée', 'warn');
      }
      refresh();
    }, { once: true });
    dialog.showModal();
    $('item-name').focus();
  }

  function itemActions(c) {
    const fav = state.favorites.includes(keyOf(c));
    const alts = state.kind === 'live' ? backupsOf(c.stream_id) : [];
    return h('span', { class: 'item-actions' },
      alts.length ? h('button', { type: 'button', class: 'item-btn', text: '🧪', title: 'Tester et comparer les versions',
        'aria-label': `Comparer les versions de ${displayName(c)}`,
        onclick: (event) => { event.preventDefault(); event.stopPropagation(); openVersionProbe(c.stream_id, displayName(c), [c.stream_id, ...alts]); } }) : null,
      state.kind === 'live' ? h('button', { type: 'button', class: 'item-btn', text: '📁', title: 'Ranger dans un de mes groupes',
        'aria-label': `Ranger ${displayName(c)} dans un groupe`,
        onclick: (event) => { event.preventDefault(); event.stopPropagation(); moveToGroup(c); } }) : null,
      h('button', { type: 'button', class: `item-btn${fav ? ' fav-on' : ''}`, text: fav ? '★' : '☆',
        title: fav ? 'Retirer des favoris' : 'Ajouter aux favoris', 'aria-label': `${fav ? 'Retirer' : 'Ajouter'} ${displayName(c)} ${fav ? 'des' : 'aux'} favoris`,
        onclick: (event) => { event.preventDefault(); event.stopPropagation(); toggleFavorite(c); refresh(); } }),
      state.kind === 'live' ? h('button', { type: 'button', class: 'item-btn', text: '▶', title: 'Aperçu dans le navigateur',
        'aria-label': `Aperçu de ${displayName(c)}`,
        onclick: (event) => { event.preventDefault(); event.stopPropagation(); preview(c.stream_id, displayName(c)); } }) : null,
      h('button', { type: 'button', class: 'item-btn', text: '✎', title: 'Renommer, numéroter, logo, guide',
        'aria-label': `Personnaliser ${displayName(c)}`,
        onclick: (event) => { event.preventDefault(); event.stopPropagation(); editItem(c); } }));
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

  let dragScope = '';
  let dragIndex = -1;

  function wireDragReorder(row, handle, scope, index, onMove) {
    handle.addEventListener('mousedown', () => { row.draggable = true; });
    handle.addEventListener('mouseup', () => { row.draggable = false; });
    row.addEventListener('dragstart', (event) => {
      dragScope = scope;
      dragIndex = index;
      row.classList.add('dragging');
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', String(index));
      }
    });
    row.addEventListener('dragend', () => {
      row.draggable = false;
      row.classList.remove('dragging');
      dragScope = '';
      dragIndex = -1;
      document.querySelectorAll('.drag-over').forEach((n) => n.classList.remove('drag-over'));
    });
    row.addEventListener('dragover', (event) => {
      if (dragScope !== scope || dragIndex < 0 || dragIndex === index) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
      row.classList.add('drag-over');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', (event) => {
      row.classList.remove('drag-over');
      if (dragScope !== scope || dragIndex < 0 || dragIndex === index) return;
      event.preventDefault();
      const from = dragIndex;
      dragScope = '';
      dragIndex = -1;
      onMove(from, index);
    });
  }

  function groupCounts() {
    const groups = new Map();
    const extras = groupExtras();
    for (const kind of KINDS) {
      const sel = state.sel[kind];
      for (const c of state.cat[kind].channels) {
        if (sel.excluded.has(c.stream_id) || adultHidden(c, kind) || (kind === 'live' && hiddenDuplicates().has(c.stream_id))) continue;
        const inCat = c.category_ids.find((id) => catOn(kind, id));
        if (!inCat && !sel.included.has(c.stream_id)) continue;
        const custom = kind === 'live' ? groupAssignment().get(c.stream_id) : null;
        const key = custom ? CG + custom : KIND[kind].prefix + (inCat || c.category_id);
        groups.set(key, (groups.get(key) || 0) + 1);
        if (kind === 'live' && extras.has(c.stream_id)) {
          for (const extraGid of extras.get(c.stream_id)) {
            const extraKey = CG + extraGid;
            groups.set(extraKey, (groups.get(extraKey) || 0) + 1);
          }
        }
      }
    }
    return groups;
  }

  function orderedGroups(groups) {
    // Ordre par défaut : direct, puis films, puis séries, chacun dans l'ordre du fournisseur.
    const provider = new Map();
    KINDS.forEach((kind, k) => state.cat[kind].categories.forEach((cat, i) => provider.set(KIND[kind].prefix + cat.category_id, k * 1e6 + i)));
    orderedCustomGroups().forEach((g, i) => provider.set(CG + g.id, -1e3 + i));  // groupes perso d'abord
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

  function reorderGroupTo(order, from, to) {
    const next = [...order];
    const [item] = next.splice(from, 1);
    next.splice(to, 0, item);
    state.groupOrder = next;
    renderSummary();
    renderStats();
  }

  function renderSummary() {
    const groups = groupCounts();
    const order = orderedGroups(groups);
    const gl = $('sum-groups');
    gl.replaceChildren();
    order.forEach((id, index) => {
      const info = groupInfo(id);
      const original = info.name;
      const group = info.custom ? state.customGroups.find((g) => CG + g.id === id) : null;
      const input = group
        ? h('input', { type: 'text', maxlength: '100', value: group.name, placeholder: group.name, class: 'renamed',
          'aria-label': `Nom de mon groupe ${original}` })
        : h('input', { type: 'text', maxlength: '100', value: state.groupNames[id] || '', placeholder: original,
          'aria-label': `Nom du groupe ${original} dans cette playlist`, class: state.groupNames[id] ? 'renamed' : null });
      input.addEventListener('input', () => {
        if (group) { if (input.value.trim()) group.name = input.value.trim(); renderStats(); return; }
        state.groupNames[id] = input.value;
        input.classList.toggle('renamed', Boolean(input.value.trim()));
        renderStats();
      });
      if (group) input.addEventListener('change', () => { input.value = group.name; renderCustomGroups(); });
      const handle = h('span', { class: 'drag-handle', title: 'Glisser pour réordonner', 'aria-hidden': 'true', text: '⠿' });
      const row = h('li', { class: 'group-row' },
        handle,
        h('span', { class: 'pos', text: String(index + 1) }),
        group ? h('span', { class: 'kind-tag mine', text: '📁 Perso', title: 'Mon groupe (rempli par les noms de chaînes et les rangements 📁)' })
          : h('span', { class: `kind-tag ${info.kind}`, text: KIND[info.kind].label, title: `Groupe de ${KIND[info.kind].many}` }),
        input,
        h('span', { class: 'num', text: groups.get(id).toLocaleString('fr-FR'), title: KIND[info.kind].title }),
        h('button', { type: 'button', class: 'icon-btn', text: '↑', title: 'Monter', 'aria-label': `Monter ${original}`, disabled: index === 0, onclick: () => moveGroup(order, index, -1) }),
        h('button', { type: 'button', class: 'icon-btn', text: '↓', title: 'Descendre', 'aria-label': `Descendre ${original}`, disabled: index === order.length - 1, onclick: () => moveGroup(order, index, 1) }),
        index >= 2 ? h('button', { type: 'button', class: 'icon-btn', text: '⤒', title: 'Placer tout en haut', 'aria-label': `Placer ${original} en tête`, onclick: () => reorderGroupTo(order, index, 0) }) : null,
      );
      wireDragReorder(row, handle, 'sum-groups', index, (from, to) => reorderGroupTo(order, from, to));
      gl.append(row);
    });
    if (!order.length) gl.append(h('li', { class: 'empty', text: 'La playlist est vide pour le moment.' }));
    $('reset-groups').disabled = !state.groupOrder.length && !Object.keys(cleanGroupNames()).length;

    let missingCount = 0;
    for (const kind of KINDS) {
      if (!state.cat[kind].catalogLoaded) continue;
      for (const id of state.sel[kind].included) {
        if (!state.cat[kind].channelById.has(id)) missingCount += 1;
      }
    }
    $('missing-included-bar').hidden = !missingCount;
    if (missingCount) {
      $('missing-included-text').textContent = `⚠️ ${plural(missingCount, 'ajout manuel ne figure plus', 'ajouts manuels ne figurent plus')} dans le catalogue actuel du fournisseur.`;
    }

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
    renderCustomGroups();
    fill($('sum-included'), 'included', 'Aucun ajout hors catégories.');
    fill($('sum-excluded'), 'excluded', 'Aucune exclusion.');
    renderCustomSummary();
  }

  // ------------------------------------------------------- mes groupes
  const splitPatterns = (text) => [...new Set(String(text).split(/[,;\n]+/).map((p) => p.trim()).filter(Boolean))].slice(0, 300);

  /** Place les groupes perso (dans cet ordre) en tête de l'ordre des groupes de la playlist. */
  function setCustomOrder(ids) {
    state.groupOrder = [...ids.map((id) => CG + id), ...state.groupOrder.filter((k) => !k.startsWith(CG))];
  }

  /** Ajoute une version par chaîne du modèle, avec priorité aux chaînes déjà retenues. */
  function includeRegionalChannels(group) {
    const sel = state.sel.live;
    const uhd = $('opt-uhd').value;
    const candidates = new Map();
    for (const c of state.cat.live.channels) {
      if (sel.excluded.has(c.stream_id) || adultHidden(c, 'live') || !groupMatches(c, group)) continue;
      const selected = sel.included.has(c.stream_id) || c.category_ids.some((cid) => catOn('live', cid));
      const quality = qualityOf(c.name);
      const score = quality === 4 && uhd !== 'normal' ? 0.5 : quality;
      const key = c.regional[1];
      const current = candidates.get(key);
      if (!current || (selected && !current.selected) || (selected === current.selected && score > current.score)) {
        candidates.set(key, { id: c.stream_id, selected, score });
      }
    }
    for (const c of candidates.values()) if (!c.selected) sel.included.add(c.id);
  }

  async function createGroup(preset = null) {
    let name = preset ? preset.name : await ask({ title: 'Nouveau groupe', text: 'Il sera placé en premier dans la playlist. Tu pourras le remplir avec des noms de chaînes ou en rangeant des chaînes avec 📁.',
      okLabel: 'Créer', input: { label: 'Nom du groupe (ex. TNT, Mes chaînes, Canal+)', value: '' } });
    name = String(name || '').trim().slice(0, 100);
    if (!name) return null;
    const draft = state.customGroups;
    if (preset?.region && (!(await ensureCatalog('live')) || draft !== state.customGroups)) return null;
    const existing = state.customGroups.find((g) => norm(g.name) === norm(name));
    if (existing) {
      if (!preset) { toast(`Le groupe « ${existing.name} » existe déjà.`, 'warn'); return existing; }
      const stripNum = (p) => matchName(String(p).replace(NUMBERED, (m, n, rest) => rest));
      const known = new Set((existing.patterns || []).map(stripNum).filter(Boolean));
      const added = (preset.patterns || []).filter((p) => {
        const core = stripNum(p);
        if (!core || known.has(core)) return false;
        known.add(core);
        return true;
      });
      const regionAdded = Boolean(preset.region && existing.region !== preset.region);
      if (regionAdded) existing.region = preset.region;
      if (added.length) existing.patterns = [...(existing.patterns || []), ...added].slice(0, 300);
      const beforeInc = state.sel.live.included.size;
      if (existing.region) includeRegionalChannels(orderedCustomGroups().find((g) => g.id === existing.id));
      const addedChannels = state.sel.live.included.size - beforeInc;
      if (added.length || regionAdded || addedChannels > 0) {
        refresh();
        const details = [
          added.length ? `+${plural(added.length, 'nom', 'noms')}` : '',
          addedChannels > 0 ? `+${plural(addedChannels, 'chaîne ajoutée', 'chaînes ajoutées')}` : '',
        ].filter(Boolean).join(', ');
        toast(`Groupe « ${existing.name} » mis à jour${details ? ` (${details})` : ''} — pense à enregistrer.`);
      } else {
        toast(`Le groupe « ${existing.name} » est déjà à jour.`);
      }
      return existing;
    }
    if (state.customGroups.length >= 50) { toast('50 groupes maximum.', 'warn'); return null; }
    const base = slugify(name).replace(/[^a-z0-9_-]/g, '').slice(0, 36) || 'groupe';
    let id = base;
    for (let n = 2; state.customGroups.some((g) => g.id === id); n += 1) id = `${base}-${n}`;
    const group = { id, name, match: preset ? preset.match : 'words', patterns: preset ? [...preset.patterns] : [] };
    if (preset?.region) group.region = preset.region;
    if (!preset) state.openCgDrawers.add(id);
    state.customGroups.push(group);
    setCustomOrder([id, ...orderedCustomGroups().map((g) => g.id).filter((g) => g !== id)]);
    if (group.region) includeRegionalChannels(orderedCustomGroups().find((g) => g.id === id));
    refresh();
    toast(`Groupe « ${name} » ajouté en tête de la playlist.`);
    return group;
  }

  async function moveToGroup(c) {
    const current = state.itemGroups[c.stream_id] || '';
    const options = [['', 'Automatique (selon les noms de mes groupes)'], ['-', 'Son groupe d\'origine (catégorie du fournisseur)'],
      ...orderedCustomGroups().map((g) => [g.id, `📁 ${g.name}`]), ['__new', '+ Nouveau groupe en premier…']];
    const value = await ask({ title: `Ranger « ${displayName(c)} »`, text: 'Groupe de cette chaîne dans la playlist (le catalogue du fournisseur ne change pas).',
      okLabel: 'Ranger', select: { label: 'Groupe', options, value: current } });
    if (value === null) return;
    let target = value;
    if (value === '__new') {
      const group = await createGroup();
      if (!group) return;
      target = group.id;
    }
    if (target) state.itemGroups[c.stream_id] = target; else delete state.itemGroups[c.stream_id];
    refresh();
  }

  function groupMembers() {
    const members = new Map();
    const byId = state.cat.live.channelById;
    const groups = new Map(orderedCustomGroups().filter((g) => g.region).map((g) => [g.id, g]));
    const pairs = [...groupAssignment()];
    for (const [sid, more] of groupExtras()) for (const gid of more) pairs.push([sid, gid]);
    for (const [sid, gid] of pairs) {
      if (!members.has(gid)) members.set(gid, []);
      const c = byId.get(sid);
      if (c && !hiddenDuplicates().has(sid) && !adultHidden(c, 'live')) {
        const n = backupsOf(sid).length;
        const group = groups.get(gid);
        const rank = group && c.regional?.[0] === group.region ? group._exact.get(c.regional[1]) : null;
        members.get(gid).push({ rank: rank ?? Number.MAX_SAFE_INTEGER, name: displayName(c, 'live') + (n ? ` (+${n})` : '') });
      }
    }
    return new Map([...members].map(([id, entries]) => [id, entries.sort((a, b) => a.rank - b.rank).map((c) => c.name)]));
  }

  /** Secours disponibles pour les chaînes d'un groupe perso (parmi les catégories cochées). */
  function groupBackupStats(gid) {
    let total = 0, withBackup = 0;
    const byId = state.cat.live.channelById;
    for (const [sid, g] of groupAssignment()) {
      const c = byId.get(sid);
      if (g !== gid || hiddenDuplicates().has(sid) || (c && adultHidden(c, 'live'))) continue;
      total += 1;
      if (backupsOf(sid).length) withBackup += 1;
    }
    return { total, withBackup };
  }

  function previewText(names) {
    if (!state.cat.live.catalogLoaded) return 'Chargement du catalogue…';
    if (!names || !names.length) return 'Aucune chaîne retenue. Sélectionne une catégorie ou range une chaîne avec 📁.';
    return `${plural(names.length, 'chaîne', 'chaînes')} : ${names.slice(0, 8).join(', ')}${names.length > 8 ? '…' : ''}`;
  }

  /** Avertissement « X/Y chaînes sans secours » (bascule automatique activée mais peu de doublons disponibles). */
  function failoverWarnText(gid) {
    if (!$('opt-failover').checked || $('opt-failover-any').checked) return '';
    const { total, withBackup } = groupBackupStats(gid);
    if (!total || withBackup >= total) return '';
    return `⚠️ ${total - withBackup}/${total} chaîne${total - withBackup > 1 ? 's' : ''} sans version de secours. `
      + 'Coche d\'autres catégories de la même chaîne (ex. une autre qualité) dans l\'onglet 1, ou active '
      + '« Secours étendu » dans Options.';
  }

  function renderCustomGroups() {
    const list = $('cg-list');
    list.replaceChildren();
    const ordered = orderedCustomGroups();
    const members = groupMembers();
    const reorderCg = (from, to) => {
      const ids = ordered.map((g) => g.id);
      const [moved] = ids.splice(from, 1);
      ids.splice(to, 0, moved);
      setCustomOrder(ids);
      refresh();
    };
    ordered.forEach((info, index) => {
      const group = state.customGroups.find((g) => g.id === info.id);
      const count = h('span', { class: 'num', text: String((members.get(group.id) || []).length) });
      const preview = h('p', { class: 'hint cg-preview', text: previewText(members.get(group.id)) });
      const warnText = failoverWarnText(group.id);
      const warn = h('p', { class: 'hint warn-text', text: warnText, hidden: !warnText });
      const drawerSummaryText = () => {
        const len = (group.patterns || []).length;
        if (!len) return 'Motifs automatiques (vide = rangement manuel 📁)';
        return `Motifs automatiques (${len} ${group.match === 'exact' ? (len > 1 ? 'noms exacts' : 'nom exact') : (len > 1 ? 'mots-clés' : 'mot-clé')})`;
      };
      const drawerSummary = h('summary', { text: drawerSummaryText() });
      const update = () => {
        state.groupCache = null; state.hiddenCache = null;
        const names = groupMembers().get(group.id);
        preview.textContent = previewText(names);
        count.textContent = String((names || []).length);
        drawerSummary.textContent = drawerSummaryText();
        const w = failoverWarnText(group.id);
        warn.textContent = w;
        warn.hidden = !w;
        renderStats();
      };
      const name = h('input', { type: 'text', maxlength: '100', value: group.name, 'aria-label': 'Nom du groupe' });
      name.addEventListener('input', () => { if (name.value.trim()) group.name = name.value.trim(); renderStats(); });
      name.addEventListener('change', () => { name.value = group.name; refresh(); });
      const match = h('select', { 'aria-label': `Correspondance du groupe ${group.name}` },
        h('option', { value: 'words', text: 'Le nom de la chaîne contient l\'un de ces noms', selected: group.match !== 'exact' }),
        h('option', { value: 'exact', text: 'Le nom de la chaîne est exactement l\'un de ces noms', selected: group.match === 'exact' }));
      match.addEventListener('change', () => { group.match = match.value; update(); });
      const patterns = h('textarea', { rows: '2', 'aria-label': `Noms de chaînes du groupe ${group.name}`,
        placeholder: 'ex. TF1, France 2, M6 — séparés par des virgules ; « 4: Canal+ » donne aussi le numéro 4 (vide = rangement à la main avec 📁)' });
      patterns.value = (group.patterns || []).join(', ');
      patterns.addEventListener('input', () => { group.patterns = splitPatterns(patterns.value); update(); });
      patterns.addEventListener('change', () => refresh());
      const move = (delta) => {
        const target = index + delta;
        if (target < 0 || target >= ordered.length) return;
        reorderCg(index, target);
      };
      const isOpen = state.openCgDrawers.has(group.id) || (group.patterns || []).length <= 10;
      const drawer = h('details', { class: 'cg-drawer', open: isOpen },
        drawerSummary,
        h('div', { class: 'cg-drawer-body' }, match, patterns));
      drawer.addEventListener('toggle', () => {
        if (drawer.open) state.openCgDrawers.add(group.id);
        else state.openCgDrawers.delete(group.id);
      });
      const manual = Object.values(state.itemGroups).filter((v) => v === group.id).length;
      const handle = h('span', { class: 'drag-handle', title: 'Glisser pour réordonner', 'aria-hidden': 'true', text: '⠿' });
      const card = h('div', { class: 'cg-card' },
        h('div', { class: 'cg-head' },
          handle,
          h('span', { class: 'pos', text: String(index + 1) }),
          name, count,
          h('button', { type: 'button', class: 'icon-btn', text: '↑', title: 'Monter', 'aria-label': `Monter ${group.name}`, disabled: index === 0, onclick: () => move(-1) }),
          h('button', { type: 'button', class: 'icon-btn', text: '↓', title: 'Descendre', 'aria-label': `Descendre ${group.name}`, disabled: index === ordered.length - 1, onclick: () => move(1) }),
          index >= 2 ? h('button', { type: 'button', class: 'icon-btn', text: '⤒', title: 'Placer tout en haut', 'aria-label': `Placer ${group.name} en premier`, onclick: () => reorderCg(index, 0) }) : null,
          h('button', { type: 'button', class: 'icon-btn danger', text: '×', title: 'Supprimer ce groupe', 'aria-label': `Supprimer ${group.name}`,
            onclick: async () => {
              if (!await ask({ title: `Supprimer le groupe « ${group.name} » ?`, text: 'Ses chaînes retournent dans leur groupe d\'origine (rien n\'est retiré de la playlist).', okLabel: 'Supprimer', danger: true })) return;
              state.customGroups = state.customGroups.filter((g) => g !== group);
              state.groupOrder = state.groupOrder.filter((k) => k !== CG + group.id);
              state.openCgDrawers.delete(group.id);
              for (const [sid, gid] of Object.entries(state.itemGroups)) if (gid === group.id) delete state.itemGroups[sid];
              refresh();
            } })),
        h('div', { class: 'cg-body' }, preview, warn,
          manual ? h('p', { class: 'hint', text: `${plural(manual, 'chaîne rangée', 'chaînes rangées')} à la main avec 📁.` }) : null,
          drawer));
      wireDragReorder(card, handle, 'cg-list', index, reorderCg);
      list.append(card);
    });
    if (!ordered.length) list.append(h('p', { class: 'empty', text: 'Aucun groupe pour le moment : choisis un modèle (TNT, Canal+…) ou crée un groupe vide puis range tes chaînes avec 📁.' }));
  }

  function itemByKey(key) {
    for (const kind of ['vod', 'series']) {
      if (key.startsWith(KIND[kind].prefix)) return { kind, item: state.cat[kind].channelById.get(key.slice(KIND[kind].prefix.length)) };
    }
    return { kind: 'live', item: state.cat.live.channelById.get(key) };
  }

  function renderCustomSummary() {
    const favList = $('sum-favorites');
    favList.replaceChildren();
    state.favorites.forEach((key, index) => {
      const { kind, item } = itemByKey(key);
      const label = item ? displayName(item, kind) : `${KIND[kind].one} ${key}`;
      favList.append(h('li', null,
        h('span', null, h('span', { class: `kind-tag ${kind}`, text: KIND[kind].label }), ' ', label),
        h('span', { class: 'btn-row' },
          h('button', { type: 'button', class: 'x', text: '↑', title: 'Monter', disabled: index === 0, onclick: () => {
            const list = [...state.favorites]; [list[index - 1], list[index]] = [list[index], list[index - 1]]; state.favorites = list; refresh();
          } }),
          h('button', { type: 'button', class: 'x', text: '×', title: 'Retirer', onclick: () => { state.favorites = state.favorites.filter((k) => k !== key); refresh(); } }))));
    });
    if (!state.favorites.length) favList.append(h('li', { class: 'empty', text: 'Aucun favori.' }));
    const custom = $('sum-custom');
    custom.replaceChildren();
    const keys = new Set([...Object.keys(state.itemNames), ...Object.keys(state.itemNumbers),
      ...Object.keys(state.itemLogos), ...Object.keys(state.itemEpg), ...Object.keys(state.itemXmltv),
      ...Object.keys(state.itemPinned)]);
    for (const key of keys) {
      const { kind, item } = itemByKey(key);
      const parts = [];
      if (state.itemNames[key]) parts.push(`« ${state.itemNames[key]} »`);
      if (kind === 'live' && state.itemNumbers[key]) parts.push(`n° ${state.itemNumbers[key]}`);
      if (state.itemLogos[key]) parts.push('logo');
      if (kind === 'live' && state.itemEpg[key]) {
        const source = state.cat.live.channelById.get(state.itemEpg[key]);
        parts.push(`guide de « ${source ? source.name : state.itemEpg[key]} »`);
      }
      if (kind === 'live' && state.itemXmltv[key]) parts.push(`guide ${state.itemXmltv[key]}`);
      if (kind === 'live' && state.itemPinned[key]) {
        const target = state.cat.live.channelById.get(state.itemPinned[key]);
        parts.push(`📌 ${target ? target.name : state.itemPinned[key]}`);
      }
      custom.append(h('li', null,
        h('span', null, h('span', { class: `kind-tag ${kind}`, text: KIND[kind].label }), ' ', `${item ? item.name : key} → ${parts.join(' · ')}`),
        h('button', { type: 'button', class: 'x', text: '×', title: 'Annuler la personnalisation', onclick: () => {
          delete state.itemNames[key]; delete state.itemNumbers[key]; delete state.itemLogos[key]; delete state.itemEpg[key];
          delete state.itemXmltv[key]; delete state.itemPinned[key]; refresh();
        } })));
    }
    if (!keys.size) custom.append(h('li', { class: 'empty', text: 'Aucune personnalisation (nom, numéro, logo, guide).' }));
  }

  function renderRules() {
    const list = $('rules-list');
    list.replaceChildren();
    const mine = state.rules.filter((r) => r.kind === state.kind);
    const labels = { prefix: 'commence par', contains: 'contient', exact: 'est exactement' };
    for (const rule of mine) {
      const n = ruleSet(state.kind, [rule]).size;
      list.append(h('li', null,
        h('span', { text: `Nom ${labels[rule.match]} « ${rule.value} » → ${plural(n, 'catégorie', 'catégories')}` }),
        h('button', { type: 'button', class: 'x', text: '×', title: 'Supprimer la règle', onclick: () => {
          state.rules = state.rules.filter((r) => r !== rule); refresh();
        } })));
    }
    if (!mine.length) list.append(h('li', { class: 'empty', text: `Aucune règle pour ${K().label.toLowerCase()}.` }));
    $('rules-count').textContent = state.rules.length ? `(${state.rules.length})` : '';
    previewRule();
  }

  function previewRule() {
    const value = $('rule-value').value.trim();
    if (!value) { $('rule-preview').textContent = ''; return; }
    const rule = { kind: state.kind, match: $('rule-match').value, value };
    const matched = state.categories.filter((cat) => ruleMatches(cat.name, rule) && (!$('opt-adult').checked || !isAdultCategory(cat.name)));
    $('rule-preview').textContent = matched.length
      ? `Aperçu : ${plural(matched.length, 'catégorie', 'catégories')} — ${matched.slice(0, 6).map((c) => c.name).join(', ')}${matched.length > 6 ? '…' : ''}`
      : 'Aperçu : aucune catégorie actuelle ne correspond (la règle s\'appliquera aux futures catégories).';
  }

  function renderCategorySelect() {
    const select = $('ch-category');
    const current = select.value;
    select.replaceChildren(h('option', { value: '', text: 'Toutes les catégories' }));
    for (const cat of state.categories) {
      if ($('opt-adult').checked && isAdultCategory(cat.name)) continue;
      select.append(h('option', { value: cat.category_id, text: `${cat.name} (${cat.count})` }));
    }
    select.value = state.categoryNames.has(current) ? current : '';
  }

  let tvPreviewTicket = 0;
  const debouncedTvPreview = debounce(() => {
    if (state.section === 'preview') loadTvPreview();
  }, 220);

  async function loadTvPreview() {
    const ticket = ++tvPreviewTicket;
    const box = $('tv-preview-groups');
    $('tv-preview-meta').textContent = 'Calcul du rendu TV final…';
    if (!state.tvPreview) box.replaceChildren(h('p', { class: 'empty', text: 'Génération de l\'aperçu en cours…' }));
    try {
      const data = await request('/api/playlists/preview', 'POST', payload());
      if (ticket !== tvPreviewTicket) return;
      state.tvPreview = data;
      renderTvPreview();
    } catch (error) {
      if (ticket !== tvPreviewTicket) return;
      $('tv-preview-meta').textContent = error.message;
      box.replaceChildren(h('p', { class: 'empty', text: 'Aperçu indisponible.' }));
    }
  }

  function renderTvPreview() {
    const box = $('tv-preview-groups');
    box.replaceChildren();
    const data = state.tvPreview;
    if (!data) {
      $('tv-preview-meta').textContent = '';
      return;
    }
    const c = data.counts || {};
    const summaryParts = [];
    if (c.groups) summaryParts.push(plural(c.groups, 'groupe', 'groupes'));
    if (c.live) summaryParts.push(`${plural(c.live, 'chaîne', 'chaînes')}${c.backups ? ` (${c.backups} avec secours 🔁)` : ''}`);
    if (c.vod) summaryParts.push(plural(c.vod, 'film', 'films'));
    if (c.series) summaryParts.push(plural(c.series, 'série', 'séries'));
    $('tv-preview-meta').textContent = summaryParts.length
      ? `Résultat final : ${summaryParts.join(' · ')}`
      : 'La playlist ne contient aucun élément pour le moment (coche des catégories dans Contenu).';
    if (!data.groups || !data.groups.length) {
      box.append(h('p', { class: 'empty', text: 'Aucun groupe à afficher dans l\'aperçu TV.' }));
      return;
    }
    const q = norm($('tv-preview-search').value.trim());
    const kindFilter = state.tvPreviewKind || 'all';
    let shownGroups = 0;
    data.groups.forEach((g) => {
      if (kindFilter !== 'all' && g.kind !== kindFilter && !(g.kind === 'favorites' && g.items.some((it) => it.kind === kindFilter))) return;
      const groupMatch = q && norm(g.name).includes(q);
      const items = g.items.filter((it) => {
        if (kindFilter !== 'all' && it.kind !== kindFilter) return false;
        if (q && !groupMatch && !norm(`${it.name} ${it.num || ''}`).includes(q)) return false;
        return true;
      });
      if (!items.length && (q || kindFilter !== 'all')) return;
      const tagClass = g.kind === 'favorites' ? 'mine' : (g.custom ? 'mine' : g.kind);
      const tagText = g.kind === 'favorites' ? '★ Favoris' : (g.custom ? '📁 Perso' : (KIND[g.kind]?.label || g.kind));
      const rows = items.map((it) => {
        const sub = [];
        if (it.backups) sub.push(`🔁 ${it.backups} secours`);
        if (it.epg) sub.push('EPG');
        if (it.archive) sub.push(`Replay ${it.archive} j`);
        if (it.year) sub.push(it.year);
        return h('li', { class: 'tv-row' },
          it.num ? h('span', { class: 'tv-chno', text: String(it.num) }) : null,
          thumb({ stream_id: it.id, name: it.name, logo: it.logo }, it.kind === 'live' ? 'live' : it.kind),
          h('span', { class: 'tv-info' },
            h('strong', { text: it.name }),
            sub.length ? h('span', { text: sub.join(' · ') }) : null),
          it.kind === 'live' ? h('button', {
            type: 'button', class: 'item-btn', text: '▶', title: 'Aperçu dans le navigateur',
            'aria-label': `Aperçu de ${it.name}`, onclick: () => preview(it.id, it.name),
          }) : null);
      });
      if (g.total > g.items.length && !q && kindFilter === 'all') {
        rows.push(h('li', { class: 'empty', text: `… et ${g.total - g.items.length} autres dans ce groupe` }));
      }
      const open = Boolean(q) || shownGroups < 4;
      shownGroups += 1;
      box.append(h('details', { class: 'tv-group', open },
        h('summary', null,
          h('span', { class: `kind-tag ${tagClass}`, text: tagText }),
          h('span', { class: 'tv-g-name', text: g.name }),
          h('span', { class: 'badge', text: (q || kindFilter !== 'all' ? items.length : g.total).toLocaleString('fr-FR') })),
        h('ul', { class: 'tv-items' }, ...rows)));
    });
    if (!shownGroups) box.append(h('p', { class: 'empty', text: 'Aucun élément ne correspond à ta recherche dans l\'aperçu.' }));
  }

  function refresh(options = {}) {
    const { channels = true, row = null, channel = null } = options;
    state.hiddenCache = null;
    state.groupCache = null;
    state.tvPreview = null;
    renderStats();
    if (state.tab === 'categories' || channels === true) { renderCategories(); renderRules(); }
    if (channels === true) renderChannels();
    else if (channels === 'row') updateRow(row, channel);
    if (state.tab === 'summary' || state.section === 'organize') renderSummary();
    if (state.section === 'preview') debouncedTvPreview();
    renderUrl();
    renderPlaylists();
  }

  // ------------------------------------------------- parties de l'éditeur
  const SECTIONS = ['content', 'organize', 'preview', 'options', 'guide'];
  function selectSection(name) {
    state.section = name;
    for (const sec of SECTIONS) {
      const active = sec === name;
      $(`sec-btn-${sec}`).setAttribute('aria-selected', String(active));
      $(`sec-btn-${sec}`).tabIndex = active ? 0 : -1;
      $(`sec-${sec}`).hidden = !active;
    }
    if (name === 'organize') renderSummary();
    if (name === 'preview') loadTvPreview();
    if (name === 'guide') loadEpg();
  }

  // ------------------------------------------------------------- Guide TV
  let guideWhen = 'now';
  let guideRequest = 0;
  const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  const dayLabel = (ts) => new Date(ts * 1000).toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'short' });

  function guideAt() {
    const d = new Date();
    if (guideWhen === 'now') return Math.floor(Date.now() / 1000);
    if (guideWhen === 'tomorrow') d.setDate(d.getDate() + 1);
    d.setHours(21, 0, 0, 0);
    return Math.floor(d.getTime() / 1000);
  }

  function renderGuidePlaylists() {
    const select = $('guide-playlist');
    const current = select.value || state.editing || storage('xtream-m3u-guide') || '';
    select.replaceChildren(...state.playlists.filter((p) => usesKind(p, 'live') && inProfile(p))
      .map((p) => h('option', { value: p.slug, text: p.name, selected: p.slug === current })));
    if (!select.value && select.options.length) select.value = select.options[0].value;
  }

  async function loadGuide() {
    renderGuidePlaylists();
    const slug = $('guide-playlist').value;
    const list = $('guide-list');
    if (!slug) {
      list.replaceChildren(h('li', { class: 'empty', text: 'Crée d\'abord une playlist avec des chaînes en direct.' }));
      $('guide-status').textContent = '';
      return;
    }
    try { storage('xtream-m3u-guide', slug); } catch (_) { /* facultatif */ }
    const q = $('guide-search').value.trim();
    const params = new URLSearchParams({ slug, at: String(q ? Math.floor(Date.now() / 1000) : guideAt()) });
    if (q) params.set('q', q);
    const ticket = ++guideRequest;
    $('guide-status').textContent = 'Chargement du guide…';
    let data;
    try { data = await request(`/api/now?${params}`); } catch (error) {
      if (ticket === guideRequest) { $('guide-status').textContent = error.message; list.replaceChildren(); }
      return;
    }
    if (ticket !== guideRequest) return;
    const mine = $('guide-mine').checked;
    list.replaceChildren();
    const now = Math.floor(Date.now() / 1000);
    if (q) {
      const rows = data.results.filter((r) => !mine || r.mine);
      for (const r of rows) {
        const live = r.start <= now && now < r.stop;
        list.append(guideRow(r, h('span', { class: 'g-prog' },
          h('span', { class: 'g-time', text: `${live ? 'En ce moment' : dayLabel(r.start)} · ${hhmm(r.start)} – ${hhmm(r.stop)}` }),
          h('strong', { text: r.title }), r.desc ? h('span', { class: 'g-desc', text: r.desc }) : null)));
      }
      $('guide-status').textContent = rows.length ? `${plural(rows.length, 'programme trouvé', 'programmes trouvés')} (7 prochains jours)` : 'Aucun programme trouvé dans le guide.';
      return;
    }
    const at = data.at;
    const rows = data.channels.filter((c) => !mine || c.mine);
    let withProgramme = 0;
    for (const c of rows) {
      const cur = c.now;
      if (cur) withProgramme += 1;
      const pct = cur ? Math.max(0, Math.min(100, ((at - cur.start) / Math.max(1, cur.stop - cur.start)) * 100)) : 0;
      list.append(guideRow(c, h('span', { class: 'g-prog' },
        cur ? h('span', { class: 'g-time', text: `${hhmm(cur.start)} – ${hhmm(cur.stop)}` }) : null,
        h('strong', { text: cur ? cur.title : 'Pas de programme dans le guide' }),
        cur ? h('progress', { class: 'g-bar', max: '100', value: String(Math.round(pct)), 'aria-hidden': 'true' }) : null,
        c.next ? h('span', { class: 'g-next', text: `Ensuite ${hhmm(c.next.start)} : ${c.next.title}` }) : null)));
    }
    const when = guideWhen === 'now' ? 'en ce moment' : `${dayLabel(at)} à ${hhmm(at)}`;
    $('guide-status').textContent = rows.length
      ? `${plural(rows.length, 'chaîne', 'chaînes')} ${when}${data.total > data.channels.length ? ` (les ${data.channels.length} premières)` : ''} · ${withProgramme} avec programme`
      : 'Aucune chaîne à afficher.';
  }

  function guideRow(c, body) {
    return h('li', { class: `guide-row${c.mine ? ' mine' : ''}` },
      thumb({ ...c, stream_id: c.id, logo: c.logo }, 'live'),
      h('span', { class: 'g-chan' }, h('strong', { text: c.name }),
        h('span', { class: 'muted small', text: [c.num ? `n° ${c.num}` : '', c.group, c.backups && c.backups.length ? `🔁 ${c.backups.length} secours` : ''].filter(Boolean).join(' · '),
          title: c.backups && c.backups.length ? `Versions de secours : ${c.backups.join(' → ')}` : null })),
      body,
      h('button', { type: 'button', class: 'item-btn', text: '▶', title: 'Aperçu dans le navigateur', 'aria-label': `Aperçu de ${c.name}`,
        onclick: () => preview(c.id, c.name) }));
  }

  // ------------------------------------------------- aperçu (navigateur)
  let previewPlayer = null;
  let hlsLoading = null;
  function loadHlsLibrary() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsLoading) {
      hlsLoading = new Promise((resolve, reject) => {
        const script = document.createElement('script');
        script.src = '/static/vendor/hls.light.min.js';
        script.onload = () => resolve(window.Hls);
        script.onerror = () => { hlsLoading = null; reject(new Error('Lecteur HLS introuvable')); };
        document.head.append(script);
      });
    }
    return hlsLoading;
  }

  let previewId = null;
  function stopPreview() {
    if (previewPlayer) { try { previewPlayer.destroy(); } catch (_) { /* déjà détruit */ } previewPlayer = null; }
    if (previewId) {
      // Libère tout de suite la connexion au fournisseur (sinon gardée 30 s) : la TV peut reprendre la main.
      const id = previewId;
      previewId = null;
      request(`/api/streams${profileQuery()}`).then((data) => Promise.all(data.streams.filter((s) => s.kind === 'hls' && s.item_id === id)
        .map((s) => request(`/api/streams/${s.id}/stop?cooldown=0`, 'POST')))).then(() => loadStreams()).catch(() => {});
    }
    const video = $('preview-video');
    video.pause();
    video.removeAttribute('src');
    video.load();
  }

  async function preview(id, name) {
    const dialog = $('preview');
    const video = $('preview-video');
    $('preview-title').textContent = `Aperçu : ${name}`;
    $('preview-status').textContent = 'Connexion au flux… Utilise la connexion de ton abonnement : une lecture en cours sur une TV peut être coupée (selon les priorités des appareils).';
    stopPreview();
    if (typeof dialog.showModal === 'function') dialog.showModal();
    const url = withToken(`${baseUrl()}/hls/${encodeURIComponent(id)}.m3u8`);
    previewId = String(id);
    const failed = (message) => { $('preview-status').textContent = message; };
    video.onerror = () => failed('Lecture impossible dans le navigateur (format vidéo non pris en charge, souvent le HEVC/4K) : la chaîne peut quand même marcher sur la TV.');
    video.onplaying = () => { $('preview-status').textContent = 'Lecture en cours (son coupé : clique sur le haut-parleur).'; };
    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url;
      video.play().catch(() => {});
      return;
    }
    try {
      const Hls = await loadHlsLibrary();
      if (!Hls || !Hls.isSupported()) { failed('Ce navigateur ne sait pas lire le HLS.'); return; }
      previewPlayer = new Hls({ enableWorker: false, lowLatencyMode: false });
      previewPlayer.on(Hls.Events.ERROR, (_, info) => {
        if (info && info.fatal) failed(info.response && info.response.code
          ? `Le serveur a répondu ${info.response.code} : ${info.response.code === 503 ? 'connexion occupée par une autre lecture prioritaire.' : 'flux indisponible (le fournisseur propose-t-il le HLS ?).'}`
          : 'Lecture impossible (flux indisponible ou format non pris en charge par le navigateur).');
      });
      previewPlayer.loadSource(url);
      previewPlayer.attachMedia(video);
      video.play().catch(() => {});
    } catch (error) { failed(error.message); }
  }

  // ------------------------------------------------------------ historique
  const historyFilter = { provider: 'all', device: '', sort: 'seconds' };
  let historyMeta = { providers: [], devices: [] };

  function historyQueryParams() {
    const params = new URLSearchParams();
    params.set('provider', historyFilter.provider || 'all');
    if (historyFilter.device) params.set('device', historyFilter.device);
    if (historyFilter.sort && historyFilter.sort !== 'seconds') params.set('sort', historyFilter.sort);
    return params.toString();
  }

  async function loadHistory() {
    let data;
    try { data = await request(`/api/history?${historyQueryParams()}`); } catch (_) { return; }
    const provs = data.by_provider || [];
    const devs = data.by_device || [];
    historyMeta = { providers: provs, devices: devs };

    // Mettre à jour les listes déroulantes sans perdre la sélection active
    const provSel = $('history-provider');
    const provOpts = [h('option', { value: 'all', text: 'Tous les abonnements' }),
      ...provs.map((p) => h('option', {
        value: String(p.id),
        text: `${p.id}. ${p.name} (${formatDuration(p.seconds)} · ${plural(p.sessions, 'lecture', 'lectures')})`,
      }))];
    provSel.replaceChildren(...provOpts);
    provSel.value = provs.some((p) => String(p.id) === historyFilter.provider) ? historyFilter.provider : 'all';
    if (provSel.value !== historyFilter.provider) historyFilter.provider = provSel.value;

    const devSel = $('history-device');
    const devOpts = [h('option', { value: '', text: 'Tout le monde' }),
      ...devs.map((d) => h('option', {
        value: d.id,
        text: `${d.label} (${formatDuration(d.seconds)} · ${plural(d.sessions, 'lecture', 'lectures')})`,
      }))];
    devSel.replaceChildren(...devOpts);
    devSel.value = devs.some((d) => d.id === historyFilter.device) ? historyFilter.device : '';
    if (devSel.value !== historyFilter.device) historyFilter.device = devSel.value;

    $('history-sort').value = historyFilter.sort;
    const hasFilter = historyFilter.provider !== 'all' || Boolean(historyFilter.device);
    $('history-reset').hidden = !hasFilter;

    // Sous-titre résumant le périmètre affiché
    const scopeParts = [];
    if (historyFilter.provider !== 'all') {
      const p = provs.find((x) => String(x.id) === historyFilter.provider);
      scopeParts.push(`🔑 ${p ? p.name : `Abonnement ${historyFilter.provider}`}`);
    }
    if (historyFilter.device) {
      const d = devs.find((x) => x.id === historyFilter.device);
      scopeParts.push(`👤 ${d ? d.label : historyFilter.device}`);
    }
    $('history-subtitle').textContent = scopeParts.length
      ? `Filtre actif : ${scopeParts.join(' · ')} — ${plural(data.total || 0, 'lecture', 'lectures')} sur 30 jours.`
      : `Chaînes les plus suivies et bascules automatiques (${plural(data.total || 0, 'lecture', 'lectures')} sur 30 jours) — clique sur un abonnement ou une personne pour filtrer.`;

    // Cartes cliquables « Par abonnement » et « Par personne / appareil »
    const showBreakdown = provs.length > 0 || devs.length > 0;
    $('history-breakdown').hidden = !showBreakdown;
    $('history-prov-box').hidden = provs.length === 0;
    $('history-dev-box').hidden = devs.length === 0;

    $('history-by-provider').replaceChildren(...provs.map((p) => {
      const active = historyFilter.provider === String(p.id);
      return h('button', {
        type: 'button', class: 'history-chip', 'aria-pressed': String(active),
        onclick: () => {
          historyFilter.provider = active ? 'all' : String(p.id);
          loadHistory();
        },
      },
      h('span', { class: 'history-chip-main' },
        h('span', { class: 'history-chip-title', text: `🔑 ${p.id}. ${p.name}` }),
        h('span', { class: 'history-chip-sub', text: p.top_label ? `Top : ${p.top_label}` : 'Zapping (< 20 s)' })),
      h('span', { class: 'history-chip-stats' },
        h('strong', { text: formatDuration(p.seconds) }),
        h('span', { text: `${plural(p.sessions, 'lecture', 'lectures')}${p.switches ? ` · ${plural(p.switches, 'bascule', 'bascules')}` : ''}` })));
    }));

    $('history-by-device').replaceChildren(...devs.map((d) => {
      const active = historyFilter.device === d.id;
      const provList = (d.providers || []).map((x) => x.name).join(', ');
      const subBits = [];
      if (d.top_label) subBits.push(`Top : ${d.top_label}`);
      if (provList && provs.length > 1) subBits.push(provList);
      return h('button', {
        type: 'button', class: 'history-chip', 'aria-pressed': String(active),
        onclick: () => {
          historyFilter.device = active ? '' : d.id;
          loadHistory();
        },
      },
      h('span', { class: 'history-chip-main' },
        h('span', { class: 'history-chip-title', text: `👤 ${d.label}` }),
        h('span', { class: 'history-chip-sub', text: subBits.join(' · ') || 'Zapping (< 20 s)' })),
      h('span', { class: 'history-chip-stats' },
        h('strong', { text: formatDuration(d.seconds) }),
        h('span', { text: `${plural(d.sessions, 'lecture', 'lectures')}${d.switches ? ` · ${plural(d.switches, 'bascule', 'bascules')}` : ''}` })));
    }));

    // Étiquettes discrètes (qui / quel abonnement) sur les lignes de classement
    const showProvTag = historyFilter.provider === 'all' && provs.length > 1;
    const showDevTag = !historyFilter.device && devs.length > 1;
    const rowTags = (r) => {
      const tags = [];
      if (showDevTag && r.devices && r.devices.length) {
        const shown = r.devices.slice(0, 3).join(', ') + (r.devices.length > 3 ? ` +${r.devices.length - 3}` : '');
        tags.push(h('span', { class: 'history-tag', text: `👤 ${shown}` }));
      }
      if (showProvTag && r.providers && r.providers.length) {
        const shown = r.providers.map((p) => p.name).join(', ');
        tags.push(h('span', { class: 'history-tag', text: `🔑 ${shown}` }));
      }
      return tags.length ? h('span', { class: 'history-row-tags' }, ...tags) : null;
    };

    const findLiveForProbe = (label) => {
      if (!state.cat.live.catalogLoaded) return null;
      const target = norm(label);
      for (const c of state.cat.live.channels) {
        if (norm(c.name) === target || norm(displayName(c, 'live')) === target) {
          const alts = backupsOf(c.stream_id);
          if (alts.length) return { c, ids: [c.stream_id, ...alts] };
        }
      }
      return null;
    };

    const fillRanked = (id, rows, text, empty, withProbe = false) => {
      const node = $(id);
      node.replaceChildren(...rows.map((r) => {
        const match = withProbe ? findLiveForProbe(r.label) : null;
        return h('li', null,
          h('span', { class: 'history-row-main' }, h('span', { text: r.label }), rowTags(r)),
          h('span', { class: 'btn-row' },
            match ? h('button', { type: 'button', class: 'btn small ghost', text: '🧪', title: 'Tester et comparer les versions',
              onclick: () => openVersionProbe(match.c.stream_id, r.label, match.ids) }) : null,
            h('span', { class: 'num', text: text(r) })));
      }));
      if (!rows.length) node.append(h('li', { class: 'empty', text: empty }));
    };

    fillRanked('history-top', data.top, (r) => `${formatDuration(r.seconds)} · ${plural(r.count, 'fois', 'fois')}`, 'Aucune lecture enregistrée pour ce filtre.');
    fillRanked('history-unstable', data.unstable, (r) => `${plural(r.switches, 'bascule', 'bascules')}`, 'Aucune bascule : les chaînes tiennent bien.', true);

    const listNode = $('history-list');
    listNode.replaceChildren(...(data.entries || []).slice(0, 50).map((r) => {
      const tags = [];
      if (r.device_label) tags.push(h('span', { class: 'history-tag', text: `👤 ${r.device_label}` }));
      if (r.provider_name && (provs.length > 1 || r.backup)) tags.push(h('span', { class: 'history-tag', text: `🔑 ${r.provider_name}` }));
      if (r.backup) tags.push(h('span', { class: 'history-tag warn', text: '🛡️ Secours' }));
      const when = new Date(r.started_at * 1000).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' });
      const meta = `${when} · ${formatDuration(r.seconds)}${r.switches ? ` · ${plural(r.switches, 'bascule', 'bascules')}` : ''}`;
      return h('li', null,
        h('span', { class: 'history-row-main' }, h('span', { text: r.label }), tags.length ? h('span', { class: 'history-row-tags' }, ...tags) : null),
        h('span', { class: 'num', text: meta }));
    }));
    if (!(data.entries || []).length) listNode.append(h('li', { class: 'empty', text: 'Aucune lecture.' }));
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
    $('opt-dedupe').checked = Boolean(p && p.dedupe);
    $('opt-failover').checked = Boolean(p && p.failover);
    $('opt-failover-any').checked = Boolean(p && p.failover_any_category);
    $('opt-adaptive-failover').checked = Boolean(p && p.adaptive_failover);
    $('opt-uhd').value = (p && p.uhd) || 'backup';
    $('opt-group-epg').checked = !p || p.group_epg_external !== false;
    $('opt-multi-groups').checked = Boolean(p && p.multi_groups);
    $('epg-groups').replaceChildren();
    $('opt-hq-logos').checked = !p || p.hq_logos !== false;
    $('opt-separators').checked = Boolean(p && p.hide_separators);
    $('opt-adult').checked = Boolean(p && p.hide_adult);
    $('opt-clean-names').checked = Boolean(p && p.clean_names);
    $('opt-fav-name').value = (p && p.favorites_name) || '★ Favoris';
    renderProviderSelect(providerChoice(p));
    reapplyCatalogs();
    state.rules = p && p.rules ? p.rules.map((r) => ({ ...r })) : [];
    state.favorites = p && p.favorites ? [...p.favorites] : [];
    state.itemNames = p && p.item_names ? { ...p.item_names } : {};
    state.itemNumbers = p && p.item_numbers ? { ...p.item_numbers } : {};
    state.itemLogos = p && p.item_logos ? { ...p.item_logos } : {};
    state.itemEpg = p && p.item_epg ? { ...p.item_epg } : {};
    state.itemXmltv = p && p.item_xmltv ? { ...p.item_xmltv } : {};
    state.itemPinned = p && p.item_pinned ? { ...p.item_pinned } : {};
    state.epgSuggestions = null;
    state.hiddenCache = null;
    state.tvPreview = null;
    renderOptionsPill();
    state.groupOrder = p && p.group_order ? [...p.group_order] : [];
    state.groupNames = p && p.group_names ? { ...p.group_names } : {};
    state.customGroups = p && p.custom_groups ? p.custom_groups.map((g) => ({ ...g, patterns: [...(g.patterns || [])] })) : [];
    state.itemGroups = p && p.item_groups ? { ...p.item_groups } : {};
    state.groupCache = null;
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
    if (state.section === 'preview') loadTvPreview();
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
  const VIEWS = { dashboard: '#tableau-de-bord', playlists: '#', guide: '#guide', catalog: '#catalogue', devices: '#appareils', system: '#systeme' };

  function loadDashboard() {
    loadStreams();
    loadAccount();
    loadHistory();
    loadDevices({ quiet: true });
    loadProviders({ quiet: true });
    loadAlerts({ silent: true });
  }

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
    if (view === 'dashboard') loadDashboard();
    if (view === 'catalog') renderProvider();
    if (view === 'system') loadSystem();
    if (view === 'devices') loadDevices();
    if (view === 'guide') loadGuide();
    try { history.replaceState(null, '', VIEWS[view]); } catch (_) { /* ignore */ }
  }

  // ------------------------------------------------------------ appareils
  let devicesData = null;
  let deviceDrawerTouched = false;
  let qrState = { device: null, mode: 'm3u' };

  async function openDeviceQr(d, mode = 'm3u') {
    qrState = { device: d, mode };
    const dialog = $('qr-dialog');
    $('qr-title').textContent = `QR code — ${d.name}`;
    $('qr-mode-m3u').setAttribute('aria-pressed', String(mode === 'm3u'));
    $('qr-mode-xtream').setAttribute('aria-pressed', String(mode === 'xtream'));
    $('qr-url').textContent = 'Génération du QR code…';
    $('qr-frame').replaceChildren();
    if (!dialog.open && typeof dialog.showModal === 'function') dialog.showModal();
    try {
      const data = await request(`/api/devices/${encodeURIComponent(d.id)}/qr?mode=${encodeURIComponent(mode)}`);
      if (!qrState.device || qrState.device.id !== d.id || qrState.mode !== mode) return;
      renderQrSvg($('qr-frame'), data.qr_svg);
      $('qr-url').textContent = data.url || '—';
    } catch (error) {
      $('qr-url').textContent = error.message;
    }
  }

  function credRow(label, value, { secret = false } = {}) {
    const code = h('code', { class: secret ? 'secret' : null, text: value });
    return h('div', { class: 'cred' }, h('span', { class: 'k', text: label }), code,
      h('button', { type: 'button', class: 'btn small', text: 'Copier', onclick: () => copyText(value) }));
  }

  function renderDevicePlaylistSelect(select, value) {
    select.replaceChildren();
    if (!multiProfile()) select.append(h('option', { value: '', text: 'Tout le catalogue (sans séries en M3U)' }));
    else if (!value && select.id !== 'device-playlist') select.append(h('option', { value: '', text: 'Tout le catalogue de cet espace' }));
    for (const p of [...state.playlists].filter(inProfile).sort((a, b) => collator.compare(a.name, b.name))) {
      select.append(h('option', { value: p.slug, text: `${p.name} (/p/${p.slug}.m3u)` }));
    }
    if (value && !state.playlists.some((p) => p.slug === value)) {
      select.append(h('option', { value, text: `${value} (playlist supprimée !)` }));
    }
    select.value = value || '';
    if (!select.value && select.options.length) select.value = select.options[0].value;
  }

  /** Adresse du port des lecteurs vue depuis la maison (nom ou IP de cette page). */
  function lanBase() {
    const s = state.status;
    const port = (s && s.playlist_port) || 7777;
    const host = location.hostname.includes(':') && !location.hostname.startsWith('[') ? `[${location.hostname}]` : location.hostname;
    return `http://${host}:${port}`;
  }

  /** Adresses publiques : tunnel Cloudflare, reverse proxy déclarés, sinon PUBLIC_BASE_URL. */
  const publicAddresses = () => ((state.status && state.status.public_addresses) || []).map((a) => a.url.replace(/\/$/, ''));
  const externalBase = () => publicAddresses()[0] || '';
  /** Adresse affichée pour un appareil « externe » : l'adresse choisie si elle existe encore, sinon la première. */
  const deviceExternal = (d) => (d.links && d.links.startsWith('https://') && publicAddresses().includes(d.links) ? d.links : externalBase());

  function deviceCard(d) {
    const external = d.links === 'external' || String(d.links || '').startsWith('https://');
    const server = external ? (deviceExternal(d) || lanBase()) : lanBase();
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
    const prioLabel = d.priority === 'high' ? 'priorité haute' : d.priority === 'low' ? 'priorité basse' : 'priorité normale';
    const advSummary = `⚙️ Réglages (${prioLabel} · ${external ? 'externe' : 'réseau local'}${d.ip ? ` · IP ${d.ip}` : ''})`;
    const paused = Boolean(d.paused);
    return h('section', { class: `card device-card${paused ? ' is-paused' : ''}` },
      h('div', { class: 'card-head' },
        h('div', null,
          h('h3', { class: 'dev-title' }, h('span', { text: d.name }),
            paused ? h('span', { class: 'badge warn', text: '⏸ En pause' }) : null,
            h('span', { class: 'pill ok dev-live', id: `dev-live-${d.id}`, hidden: true },
              h('span', { class: 'dot', 'aria-hidden': 'true' }), h('span', { class: 'dev-live-text' }))),
          h('p', { class: 'muted small', text: `Identifiant ${d.username} · ${seen}` })),
        h('div', { class: 'btn-row' },
          h('button', { type: 'button', class: `btn small${paused ? ' primary' : ''}`, text: paused ? '▶ Reprendre' : '⏸️ Pause',
            title: paused ? 'Rétablir immédiatement l\'accès de cet appareil' : 'Suspendre temporairement cet appareil et couper sa lecture en cours',
            onclick: async () => {
              try {
                await request(`/api/devices/${d.id}`, 'PUT', { paused: !paused });
                toast(paused ? `« ${d.name} » réactivé` : `« ${d.name} » mis en pause`);
                loadDevices();
                loadStreams();
              } catch (error) { toast(error.message, 'error'); }
            } }),
          h('button', { type: 'button', class: 'btn small', text: '📱 QR', title: 'Afficher le QR code de configuration',
            onclick: () => openDeviceQr(d, 'm3u') }),
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
      h('div', { class: 'dev-top-row' },
        h('div', { class: 'field' }, h('label', { text: 'Playlist associée' }), select),
        h('details', { class: 'dev-adv' },
          h('summary', { text: advSummary }),
          h('div', { class: 'device-settings' },
            h('div', { class: 'field' }, h('label', { text: 'Priorité (limite de connexions)' }), prioritySelect(d)),
            h('div', { class: 'field' }, h('label', { text: 'Adresse IP de la TV (facultatif)' }), ipInput(d)),
            h('div', { class: 'field' }, h('label', { text: 'Réseau / liens' }), linksSelect(d))))),
      external && !externalBase() ? h('p', { class: 'hint warn-text', text: 'Aucune adresse publique : active le tunnel Cloudflare ou ajoute ton adresse de reverse proxy (Réglages → Accès & Guides). Liens du réseau local affichés en attendant.' }) : null,
      h('div', { class: 'device-grid' },
        h('div', { class: 'device-mode' },
          h('h4', { text: '📺 Mode « Playlist M3U » (VLC, TiviMate, Android TV…)' }),
          credRow('Playlist', m3u), credRow('Guide (EPG)', epg)),
        h('div', { class: 'device-mode' },
          h('h4', { text: '🔐 Mode « Xtream Codes » (TiviMate, IPTV Smarters…)' }),
          credRow('Serveur', server), credRow('Identifiant', d.username), credRow('Mot de passe', d.password, { secret: true }),
          h('p', { class: 'hint', text: 'Rubriques Direct / Films / Séries natives du lecteur, guide et replay inclus.' }))),
      h('details', { class: 'hdhr-details' },
        h('summary', { text: '📡 Tuner HDHomeRun avancé (Plex, Jellyfin, Emby)' }),
        h('div', { class: 'device-mode' },
          credRow('Tuner', `${server}/hdhr/${d.username}/${d.password}`, { secret: true }),
          credRow('Guide (XMLTV)', `${server}/hdhr/${d.username}/${d.password}/guide.xml`, { secret: true }),
          h('p', { class: 'hint', text: `Jellyfin / Emby : ajoute un tuner HDHomeRun avec cette adresse. Plex : si seule « IP:port » est acceptée, mets ${server.replace(/^https?:\/\//, '')} et indique ci-dessus l'adresse IP du serveur Plex.` }))));
  }

  function linksSelect(d) {
    const current = d.links === 'external' ? externalBase() : d.links;
    const addresses = publicAddresses();
    const select = h('select', { 'aria-label': `Liens de ${d.name}` },
      h('option', { value: 'lan', text: 'Réseau local : à la maison', selected: !current || current === 'lan' }),
      ...addresses.map((url) => h('option', { value: url, text: `Externe : ${url.replace('https://', '')}`, selected: url === current })),
      addresses.length ? null : h('option', { value: 'external', text: 'Externe : hors de la maison', selected: d.links === 'external' }));
    select.addEventListener('change', async () => {
      try { await request(`/api/devices/${d.id}`, 'PUT', { links: select.value }); toast('Liens de l\'appareil modifiés'); loadDevices(); }
      catch (error) { toast(error.message, 'error'); loadDevices(); }
    });
    return select;
  }

  function prioritySelect(d) {
    const select = h('select', { 'aria-label': `Priorité de ${d.name}` },
      h('option', { value: 'high', text: 'Haute : jamais coupée par une autre TV', selected: d.priority === 'high' }),
      h('option', { value: 'normal', text: 'Normale', selected: !d.priority || d.priority === 'normal' }),
      h('option', { value: 'low', text: 'Basse : cède toujours sa place', selected: d.priority === 'low' }));
    select.addEventListener('change', async () => {
      try { await request(`/api/devices/${d.id}`, 'PUT', { priority: select.value }); toast('Priorité modifiée'); loadDevices(); }
      catch (error) { toast(error.message, 'error'); loadDevices(); }
    });
    return select;
  }

  function ipInput(d) {
    const input = h('input', { type: 'text', value: d.ip || '', placeholder: d.last_ip ? `vue : ${d.last_ip}` : 'ex. 192.168.0.42', inputmode: 'decimal',
      'aria-label': `Adresse IP de ${d.name}`, title: 'Sert à reconnaître la TV même si sa playlist n\'utilise pas son code' });
    input.addEventListener('change', async () => {
      try { await request(`/api/devices/${d.id}`, 'PUT', { ip: input.value.trim() }); toast('Adresse enregistrée'); loadDevices(); }
      catch (error) { toast(error.message, 'error'); input.value = d.ip || ''; }
    });
    return input;
  }

  async function loadDevices({ quiet = false } = {}) {
    try { devicesData = await request('/api/devices'); } catch (error) { if (!quiet) showError(error.message); return; }
    renderDevicePlaylistSelect($('device-playlist'), $('device-playlist').value);
    const links = $('device-links');
    const previous = links.value;
    links.replaceChildren(h('option', { value: 'lan', text: 'Maison (réseau local)' }),
      ...(publicAddresses().length ? publicAddresses().map((url) => h('option', { value: url, text: `Externe : ${url.replace('https://', '')}` }))
        : [h('option', { value: 'external', text: 'Externe (hors domicile)' })]));
    if ([...links.options].some((o) => o.value === previous)) links.value = previous;
    $('require-token').checked = devicesData.require_token;
    $('require-token-hint').textContent = devicesData.access_token_env
      ? 'ACCESS_TOKEN est défini dans la configuration : un code est toujours exigé (ACCESS_TOKEN ou code d\'appareil).'
      : devicesData.require_token
        ? 'Les URL sans code (ou avec un code supprimé) sont refusées. Les liens générés reprennent le code de l\'appareil.'
        : 'Option désactivée : les playlists restent accessibles sans code sur ton réseau local. Les identifiants Xtream restent toujours nécessaires.';
    const list = $('devices-list');
    const visible = devicesData.devices.filter((d) => {
      if (!multiProfile()) return true;
      const p = state.playlists.find((x) => x.slug === d.playlist);
      if (p) return inProfile(p);
      if (d.profile) return String(d.profile) === state.profile;
      return state.profile === '1';
    });
    const navBadge = $('nav-badge-devices');
    if (navBadge) {
      navBadge.textContent = String(visible.length);
      navBadge.hidden = visible.length === 0;
    }
    const pausedCount = visible.filter((d) => d.paused).length;
    if ($('kpi-devices')) $('kpi-devices').textContent = String(visible.length);
    if ($('kpi-devices-sub')) {
      $('kpi-devices-sub').textContent = !visible.length
        ? 'Aucun configuré'
        : (pausedCount ? `${pausedCount} en pause · gérer` : 'Tous actifs');
    }
    if (!deviceDrawerTouched && $('device-add-drawer')) {
      $('device-add-drawer').open = visible.length === 0;
    }
    list.replaceChildren(...visible.map(deviceCard));
    refreshDeviceLive();
    if (!visible.length) list.append(h('p', { class: 'empty', text: multiProfile()
      ? `Aucun appareil dans l'espace « ${profileName(state.profile)} ». Ajoute sa TV ci-dessus, liée à une de ses playlists.`
      : 'Aucun appareil pour le moment. Ajoute ta première TV ci-dessus.' }));
  }

  /** « En lecture » dans la fiche de chaque appareil (mis à jour toutes les 8 s tant que la vue est ouverte). */
  let deviceLiveTimer = null;
  async function refreshDeviceLive() {
    clearTimeout(deviceLiveTimer);
    if (state.view !== 'devices' || !devicesData) return;
    let data = null;
    try { data = await request(`/api/streams${profileQuery()}`); } catch (_) { /* affichage seulement */ }
    for (const d of devicesData.devices) {
      const pill = $(`dev-live-${d.id}`);
      if (!pill) continue;
      const mine = ((data && data.streams) || []).filter((s) => {
        if (s.kind === 'hls' && s.idle > 20) return false;
        if (Array.isArray(s.device_ids) && s.device_ids.length) return s.device_ids.includes(d.id);
        if (s.device_id) return s.device_id === d.id;
        if (Array.isArray(s.devices) && s.devices.length) return s.devices.includes(d.name);
        return s.device === d.name;
      });
      pill.hidden = !mine.length;
      if (!mine.length) continue;
      const s = mine[mine.length - 1];
      const extra = mine.length > 1 ? ` (+${mine.length - 1})` : '';
      const shared = s.viewers > 1 ? ' · 🔗 partagé' : '';
      pill.querySelector('.dev-live-text').textContent =
        `En lecture : ${s.channel || s.label} · ${KIND_LABEL[s.kind] || s.kind} · depuis ${formatDuration(s.seconds)}${shared}${extra}`;
      pill.title = `${s.label}${s.switches ? ` · ${plural(s.switches, 'bascule', 'bascules')}` : ''} · ${formatBytes(s.bytes)}`;
    }
    deviceLiveTimer = setTimeout(refreshDeviceLive, 8000);
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
    try { data = await request(`/api/streams${profileQuery()}`); } catch (_) { data = null; }
    if (data) renderStreams(data);
    const delay = state.view === 'system' || state.view === 'dashboard' ? 4000 : 15000;
    if (!document.hidden) streamsTimer = setTimeout(loadStreams, delay);
  }

  function renderStreams(data) {
    const n = data.streams.length;
    state.localStreams = n;
    renderAccountPill();
    $('streams-pill').hidden = !n;
    const totalViewers = data.streams.reduce((acc, s) => acc + Math.max(1, Number(s.viewers) || 1), 0);
    $('streams-pill-text').textContent = totalViewers > n
      ? `${n} flux (${totalViewers} écrans)`
      : `${n} lecture${n > 1 ? 's' : ''}`;
    if ($('kpi-streams')) $('kpi-streams').textContent = String(n);
    if ($('kpi-streams-sub')) {
      $('kpi-streams-sub').textContent = !n ? 'Aucune lecture'
        : (totalViewers > n ? `${totalViewers} écrans · partagé` : `En direct${data.limit ? ` (${n}/${data.limit})` : ''}`);
    }
    const quarantined = Object.entries(data.health_quarantined || {});
    if ($('health-bar')) {
      $('health-bar').hidden = quarantined.length === 0;
      if (quarantined.length) {
        const maxMin = Math.max(1, Math.ceil(Math.max(...quarantined.map(([, sec]) => Number(sec) || 0)) / 60));
        $('health-status').textContent = `⏳ ${plural(quarantined.length, 'source temporairement mise de côté', 'sources temporairement mises de côté')} (retour auto d'ici ${maxMin} min).`;
      }
    }
    const resInfo = data.reservation && data.limit > 1 && data.reservation.count > 0
      ? (() => {
          const rc = Math.min(Number(data.reservation.count) || 0, data.limit);
          const bk = Math.max(0, data.limit - rc);
          return ` · 🔒 ${plural(rc, 'connexion garantie', 'connexions garanties')} pour « ${sourceName(data.reservation.profile)} »${bk ? ` · 🛡️ ${bk} secours` : ''}`;
        })()
      : '';
    const limitText = data.limit
      ? `Limite : ${plural(data.limit, 'connexion simultanée', 'connexions simultanées')}${data.auto ? ' (valeur de l\'abonnement)' : ' (MAX_CONNECTIONS)'}${data.takeover ? '' : ' · reprise automatique désactivée'}${resInfo}.`
      : 'Aucune limite de connexions appliquée (MAX_CONNECTIONS=0 ou valeur de l\'abonnement inconnue).';
    $('streams-limit').textContent = limitText;
    const list = $('streams-list');
    list.replaceChildren();
    for (const s of data.streams) {
      const devicesLabel = Array.isArray(s.devices) && s.devices.length > 1
        ? s.devices.join(', ')
        : (s.device ? `${s.device}${s.priority === 'high' ? ' (prioritaire)' : s.priority === 'low' ? ' (priorité basse)' : ''}` : `appareil ${s.client}`);
      const isBackup = s.consumer_profile && String(s.consumer_profile) !== String(s.provider);
      list.append(h('li', { class: 'stream-item' },
        h('span', { class: 'dot live-dot' }),
        h('span', { class: 'grow' },
          h('span', { class: 's-title' },
            h('span', { text: s.label || s.item_id }),
            s.viewers > 1 ? h('span', { class: 'badge ok', text: `🔗 Partagé (${s.viewers} écrans · 1 connexion)` }) : null,
            isBackup ? h('span', { class: 'badge', text: `🛡️ Secours (${sourceName(s.consumer_profile)})` }) : null),
          h('span', { class: 's-meta', text: `${KIND_LABEL[s.kind] || s.kind} · ${devicesLabel} · depuis ${formatDuration(s.seconds)} · ${formatBytes(s.bytes)}${s.switches ? ` · ${plural(s.switches, 'bascule', 'bascules')}` : ''}${hasMultipleSources() ? ` · ${sourceName(s.provider)}` : ''}` })),
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
    loadAccount();
    loadHistory();
    loadProviders();
    loadSettings();
    loadTunnel();
    loadAlerts();
  }

  let settingsData = null;

  function renderHosts() {
    const hosts = (settingsData && settingsData.public_hosts) || [];
    const list = $('hosts-list');
    list.replaceChildren(...hosts.map((host) => h('li', null, h('span', null, h('code', { text: `https://${host}` })),
      h('button', { type: 'button', class: 'btn small', text: 'Retirer', onclick: () => saveHosts(hosts.filter((x) => x !== host)) }))));
    if (!hosts.length) list.append(h('li', { class: 'empty', text: 'Aucune adresse de reverse proxy.' }));
    $('hosts-form').hidden = !(settingsData && settingsData.editable);
  }

  function renderGuides() {
    const fixed = (settingsData && settingsData.epg_env_sources) || [];
    const mine = (settingsData && settingsData.epg_sources) || [];
    const list = $('guides-list');
    list.replaceChildren(
      ...fixed.map((url) => h('li', null, h('span', null, h('code', { text: url }), h('span', { class: 'muted small', text: ' · configuration du conteneur (EPG_EXTRA_URLS)' })))),
      ...mine.map((url) => h('li', null, h('span', null, h('code', { text: url })),
        h('button', { type: 'button', class: 'btn small', text: 'Retirer', onclick: () => saveGuides(mine.filter((x) => x !== url)) }))));
    if (!fixed.length && !mine.length) list.append(h('li', { class: 'empty', text: 'Aucun guide externe : seul le guide du fournisseur est utilisé.' }));
    $('guides-form').hidden = !(settingsData && settingsData.editable);
  }

  async function saveGuides(urls) {
    try {
      settingsData = { ...settingsData, ...(await request('/api/settings', 'PUT', { epg_sources: urls })) };
      renderGuides();
      toast('Guides externes enregistrés : ils seront téléchargés à la prochaine mise à jour du guide');
    } catch (error) { toast(error.message, 'error'); }
  }

  async function saveHosts(hosts) {
    try {
      settingsData = { ...settingsData, ...(await request('/api/settings', 'PUT', { public_hosts: hosts })) };
      renderHosts();
      state.status = await request('/api/status');   // adresses proposées aux appareils
      toast('Adresses publiques enregistrées');
    } catch (error) { toast(error.message, 'error'); }
  }

  async function loadSettings() {
    let data;
    try { data = await request('/api/settings'); } catch (_) { return; }
    settingsData = data;
    renderHosts();
    renderGuides();
    $('tailscale-local').checked = data.tailscale_local;
    $('tailscale-local').disabled = !data.editable;
    $('remote-hint').textContent = `Cette page est ouverte depuis ${data.client_ip}${data.client_tailscale ? ' (Tailscale)' : ''}.`
      + (data.remote_access ? ' Accès distant avec code d\'appareil : activé (REMOTE_ACCESS).' : '');
  }

  // ---------------------------------------------------- tunnel Cloudflare
  let tunnelTimer = null;

  function renderTunnel(t) {
    $('tunnel-box').hidden = !t.managed;
    if (!t.managed) return;
    const ready = Boolean(t.tunnel_id && t.hostname);
    const pill = $('tunnel-pill');
    const [cls, text] = !t.available ? ['error', 'Indisponible']
      : t.connected ? ['ok', 'Connecté'] : t.running ? ['warn', 'Connexion…']
        : t.login_pending ? ['warn', 'En attente de Cloudflare'] : ready && !t.enabled ? ['', 'Arrêté']
          : ready ? ['error', 'Déconnecté'] : ['', 'Non configuré'];
    pill.className = `pill ${cls}`;
    $('tunnel-pill-text').textContent = text;
    $('tunnel-form').hidden = ready || !t.available;
    $('tunnel-login').hidden = !(t.login_pending && t.login_url);
    if (t.login_url) $('tunnel-login-link').href = t.login_url;
    $('tunnel-ready').hidden = !ready;
    if (ready) {
      const url = `https://${t.hostname}`;
      $('tunnel-link').href = url;
      $('tunnel-link').textContent = url;
      $('tunnel-toggle').textContent = t.enabled ? 'Arrêter' : 'Démarrer';
      $('tunnel-detail').textContent = t.connected
        ? `Dans TiviMate hors de chez toi : serveur ${url}, avec l'identifiant et le mot de passe de l'appareil.`
        : (t.error ? `Dernière erreur : ${t.error}` : '');
    }
    $('tunnel-hint').textContent = !t.available ? 'cloudflared est absent de cette installation : mets à jour l\'image Docker.'
      : (t.login_error && !t.login_pending && !t.logged_in ? t.login_error : '');
  }

  async function loadTunnel({ poll = false } = {}) {
    clearTimeout(tunnelTimer);
    let t;
    try { t = await request('/api/tunnel'); } catch (_) { return; }
    renderTunnel(t);
    const pending = $('tunnel-host').dataset.pending;
    if (t.logged_in && pending && !t.tunnel_id) {
      // Autorisation donnée dans Cloudflare : création du tunnel et de l'adresse, sans autre clic.
      delete $('tunnel-host').dataset.pending;
      await setupTunnel(pending);
      return;
    }
    if (poll || t.login_pending || (t.enabled && !t.connected)) tunnelTimer = setTimeout(() => loadTunnel(), 3000);
  }

  async function setupTunnel(hostname) {
    $('tunnel-hint').textContent = `Création du tunnel et de l'adresse ${hostname}…`;
    try {
      renderTunnel(await request('/api/tunnel/setup', 'POST', { hostname }));
      toast(`Tunnel créé : https://${hostname}`);
    } catch (error) {
      $('tunnel-hint').textContent = error.message;
      toast(error.message, 'error');
    }
    loadTunnel({ poll: true });
  }

  async function connectTunnel(event) {
    event.preventDefault();
    const hostname = $('tunnel-host').value.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '');
    if (!/^([a-z0-9-]+\.)+[a-z]{2,}$/.test(hostname)) { toast('Adresse attendue : ex. tv.mondomaine.fr', 'error'); return; }
    const button = $('tunnel-connect');
    button.disabled = true;
    try {
      const t = await request('/api/tunnel/login', 'POST');
      if (t.logged_in && !t.login_url) { await setupTunnel(hostname); return; }   // compte déjà connecté
      $('tunnel-host').dataset.pending = hostname;
      renderTunnel(t);
      window.open(t.login_url, '_blank', 'noopener');
      loadTunnel({ poll: true });
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  // ------------------------------------------------ alertes Telegram + QR
  let alertsData = null;
  let alertsTimer = null;
  let lastQrLink = '';

  function renderQrSvg(container, svgText) {
    if (!svgText) { container.replaceChildren(); return; }
    const doc = new DOMParser().parseFromString(svgText, 'image/svg+xml');
    const svg = doc.documentElement;
    if (svg && svg.nodeName.toLowerCase() === 'svg') {
      container.replaceChildren(document.importNode(svg, true));
    }
  }

  function renderAlerts(data) {
    clearTimeout(alertsTimer);
    alertsTimer = null;
    const wasWaiting = Boolean(alertsData && alertsData.pairing && alertsData.pairing.status === 'waiting');
    alertsData = data;
    if (!data) return;

    const badge = $('tg-status-badge');
    const pair = data.pairing;
    if (data.connected && data.enabled) {
      badge.className = 'badge ok';
      badge.textContent = 'Connecté · Actif';
    } else if (data.connected) {
      badge.className = 'badge warn';
      badge.textContent = 'Connecté · En pause';
    } else if (pair && pair.status === 'waiting') {
      badge.className = 'badge warn';
      badge.textContent = 'En attente du scan…';
    } else {
      badge.className = 'badge';
      badge.textContent = 'Non connecté';
    }

    if ($('kpi-alerts')) {
      $('kpi-alerts').textContent = data.connected ? (data.enabled ? 'Actif' : 'Pause') : 'Off';
    }
    if ($('kpi-alerts-sub')) {
      $('kpi-alerts-sub').textContent = data.connected
        ? (data.bot_username ? `@${data.bot_username}` : 'Telegram connecté')
        : 'Configurer Telegram';
    }

    $('tg-setup-box').hidden = Boolean(data.connected);
    $('tg-connected-box').hidden = !data.connected;

    if (data.connected) {
      lastQrLink = '';
      $('tg-qr-box').hidden = true;
      $('tg-conn-title').textContent = `Connecté à ${data.chat_label || 'Telegram'}${data.bot_username ? ` via @${data.bot_username}` : ''}`;
      const subParts = [];
      if (data.bot_token_masked) subParts.push(`Token ${data.bot_token_masked}`);
      if (data.last_sent_at) subParts.push(`Dernière alerte : ${new Date(data.last_sent_at * 1000).toLocaleString('fr-FR')}`);
      $('tg-conn-sub').textContent = subParts.join(' · ') || 'Prêt à envoyer des notifications.';
      $('tg-enabled').checked = Boolean(data.enabled);
      const ev = data.events || {};
      $('tg-ev-expiry').checked = ev.expiry !== false;
      $('tg-ev-provider').checked = ev.provider !== false;
      $('tg-ev-unstable').checked = ev.unstable !== false;
      $('tg-ev-weekly').checked = Boolean(ev.weekly);
      $('tg-ev-tunnel').checked = ev.tunnel !== false;
      $('tg-ev-security').checked = ev.security !== false;
      $('tg-hint').textContent = data.last_error
        ? `⚠️ Dernière erreur d'envoi : ${data.last_error}`
        : 'Les vérifications d\'expiration (J-7, J-2, expiré), chaînes instables, récap hebdo et tunnel tournent automatiquement en arrière-plan.';
      if (wasWaiting) toast('Telegram connecté !');
      return;
    }

    if (pair && pair.status === 'waiting') {
      $('tg-qr-box').hidden = false;
      if (pair.deep_link !== lastQrLink) {
        renderQrSvg($('tg-qr-svg'), pair.qr_svg);
        lastQrLink = pair.deep_link || '';
      }
      $('tg-qr-title').textContent = `Scanne ce QR code (@${pair.bot_username})`;
      const mins = Math.max(1, Math.ceil((pair.expires_in || 0) / 60));
      $('tg-qr-status').textContent = `Puis appuie sur « Démarrer » dans Telegram… (${mins} min restantes)`;
      $('tg-qr-link').href = pair.deep_link || '#';
      $('tg-hint').textContent = 'Connexion automatique en cours d\'écoute : aucune manipulation technique à faire.';
      alertsTimer = setTimeout(() => loadAlerts({ silent: true }), 2500);
    } else {
      lastQrLink = '';
      $('tg-qr-box').hidden = true;
      if (pair && pair.status === 'expired') {
        $('tg-hint').textContent = 'Le QR code a expiré : clique de nouveau sur « Générer le QR code ».';
      } else if (pair && pair.status === 'error') {
        $('tg-hint').textContent = `⚠️ ${pair.error || 'Erreur Telegram'}`;
      } else {
        $('tg-hint').textContent = 'Ton token est enregistré uniquement dans /data/alerts.json (permissions 0600) et n\'est jamais inclus dans les sauvegardes.';
      }
    }
  }

  async function loadAlerts({ silent = false } = {}) {
    try {
      renderAlerts(await request('/api/alerts'));
    } catch (error) {
      if (!silent) $('tg-hint').textContent = error.message;
    }
  }

  async function saveAlertOptions() {
    try {
      const data = await request('/api/alerts', 'PUT', {
        enabled: $('tg-enabled').checked,
        events: {
          expiry: $('tg-ev-expiry').checked,
          provider: $('tg-ev-provider').checked,
          unstable: $('tg-ev-unstable').checked,
          weekly: $('tg-ev-weekly').checked,
          tunnel: $('tg-ev-tunnel').checked,
          security: $('tg-ev-security').checked,
        },
      });
      renderAlerts(data);
      toast('Préférences d\'alertes enregistrées');
    } catch (error) {
      toast(error.message, 'error');
      loadAlerts({ silent: true });
    }
  }

  // ---------------------------------------------------------- abonnements
  const SYS_TABS = ['providers', 'alerts', 'network', 'overview', 'backup'];
  function selectSysTab(name) {
    if (!SYS_TABS.includes(name)) name = 'providers';
    state.sysTab = name;
    for (const tab of SYS_TABS) {
      const active = tab === name;
      $(`sys-btn-${tab}`).setAttribute('aria-selected', String(active));
      $(`sys-btn-${tab}`).tabIndex = active ? 0 : -1;
      $(`sys-${tab}`).hidden = !active;
    }
  }

  let providersData = null;
  let providerProbe = { key: '', max: 0 };

  function providerSpaceName(id) {
    const sid = String(id || '');
    const fromData = providersData && providersData.providers.find((x) => String(x.index) === sid);
    if (fromData) return fromData.name;
    return sourceName(sid);
  }

  function reservationSummaryText(maxCons, count, profileLabel) {
    if (!count || count <= 0) {
      return `🔓 Les ${maxCons} connexions sont partagées librement sans réservation particulière.`;
    }
    const backup = Math.max(0, maxCons - count);
    const resLabel = plural(count, 'connexion garantie', 'connexions garanties');
    if (backup === 0) {
      return `🔒 Toutes les ${maxCons} connexions sont réservées à « ${profileLabel} » (0 secours pour les autres espaces).`;
    }
    return `🔒 ${resLabel} pour « ${profileLabel} » · 🛡️ ${plural(backup, 'connexion de secours / mélange', 'connexions de secours / mélange')} pour les autres espaces.`;
  }

  function buildSpaceChoices({ isAux, editingIndex, currentName }) {
    const items = [];
    const selfId = editingIndex ? String(editingIndex) : 'self';
    const selfLabel = `${currentName || 'Nouvel abonnement'} (cet espace)`;
    if (!isAux && !editingIndex) items.push({ value: 'self', label: selfLabel });
    const rows = (providersData && providersData.providers) || [];
    const seen = new Set();
    for (const p of rows) {
      if (p.error) continue;
      const pid = String(p.index);
      if (pid === selfId) {
        if (!isAux) { items.push({ value: pid, label: `${p.index}. ${selfLabel}` }); seen.add(pid); }
        continue;
      }
      if (p.index === 1 || !p.aux) {
        items.push({ value: pid, label: `${p.index}. ${p.name}` });
        seen.add(pid);
      }
    }
    for (const p of state.profiles || []) {
      const pid = String(p.id);
      if (seen.has(pid) || (isAux && pid === selfId)) continue;
      items.push({ value: pid, label: `${pid}. ${p.name}` });
      seen.add(pid);
    }
    if (!items.length) items.push({ value: '1', label: `1. ${providerSpaceName('1')}` });
    return items;
  }

  function fillSplitPanel(box, badgeEl, profSel, countSel, summaryEl, maxCons, opts) {
    const { isAux, editingIndex, currentName, initialRes, defaultCount = 0 } = opts;
    if (!maxCons || maxCons <= 1) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    badgeEl.textContent = `${maxCons} connexions détectées`;
    const prevProf = initialRes ? String(initialRes.profile || '') : profSel.value;
    const prevCount = initialRes ? Number(initialRes.count || 0)
      : (countSel.options.length ? Number(countSel.value || 0) : defaultCount);
    const choices = buildSpaceChoices({ isAux, editingIndex, currentName });
    profSel.replaceChildren(...choices.map((c) => h('option', { value: c.value, text: c.label })));
    profSel.value = choices.some((c) => c.value === prevProf) ? prevProf : choices[0].value;
    const counts = [];
    for (let k = 0; k <= maxCons; k++) {
      const bk = maxCons - k;
      const text = k === 0
        ? `0 — aucune réservation (${maxCons} partagées)`
        : (bk === 0 ? `${k} réservée${k > 1 ? 's' : ''} (0 secours)` : `${k} réservée${k > 1 ? 's' : ''} · ${bk} secours / mélange`);
      counts.push(h('option', { value: String(k), text }));
    }
    countSel.replaceChildren(...counts);
    const clamped = Math.max(0, Math.min(maxCons, Number.isFinite(prevCount) ? prevCount : defaultCount));
    countSel.value = String(clamped);
    const selectedLabel = (choices.find((c) => c.value === profSel.value) || choices[0]).label.replace(/^\d+\.\s*/, '').replace(/\s*\(cet espace\)$/, '');
    summaryEl.textContent = reservationSummaryText(maxCons, clamped, selectedLabel);
    profSel.disabled = clamped === 0;
  }

  function providerProbeKey() {
    const form = $('provider-form');
    return [
      $('provider-url').value.trim(), $('provider-user').value.trim(),
      $('provider-pass').value, $('provider-ua').value.trim(), form.dataset.edit || '',
    ].join('|');
  }

  function refreshProviderSplit(initialRes = null, defaultCount = 0) {
    const form = $('provider-form');
    const editing = form.dataset.edit ? Number(form.dataset.edit) : null;
    const isAux = $('provider-role').value === 'aux';
    const maxCons = providerProbe.max || 0;
    fillSplitPanel(
      $('provider-split-box'), $('provider-split-badge'), $('provider-res-profile'),
      $('provider-res-count'), $('provider-split-summary'), maxCons,
      { isAux, editingIndex: editing, currentName: $('provider-name').value.trim(), initialRes, defaultCount },
    );
    const count = Number($('provider-res-count').value || 0);
    const forceMix = isAux || (maxCons > 1 && count > 0 && count < maxCons);
    if (forceMix) $('provider-mix').checked = true;
    $('provider-mix').disabled = forceMix;
  }

  function readProviderReservation() {
    if ($('provider-split-box').hidden || providerProbe.max <= 1) return null;
    const count = Number($('provider-res-count').value || 0);
    if (!count || count <= 0) return null;
    return { profile: $('provider-res-profile').value || 'self', count };
  }

  async function probeProviderForm({ quiet = false } = {}) {
    const form = $('provider-form');
    const editing = form.dataset.edit ? Number(form.dataset.edit) : null;
    const payload = {
      url: $('provider-url').value.trim(),
      username: $('provider-user').value.trim(),
      password: $('provider-pass').value,
      user_agent: $('provider-ua').value.trim(),
      ...(editing ? { index: editing } : {}),
    };
    const res = await request('/api/providers/probe', 'POST', payload);
    const maxCons = Number(res.max_connections || 0);
    providerProbe = { key: providerProbeKey(), max: maxCons };
    if (maxCons > 1) {
      const existing = editing && providersData
        ? (providersData.providers.find((x) => x.index === editing) || {}).reservation || null
        : null;
      refreshProviderSplit(existing, existing ? Number(existing.count || 0) : 1);
      if (!quiet) toast(`Connexion OK — ${maxCons} connexions simultanées détectées`);
    } else {
      $('provider-split-box').hidden = true;
      if (!quiet) toast('Connexion OK — 1 connexion simultanée');
    }
    return maxCons;
  }

  async function loadProviders({ quiet = false } = {}) {
    try { providersData = await request('/api/providers'); } catch (error) { if (!quiet) $('providers-hint').textContent = error.message; return; }
    const list = $('providers-list');
    const provList = providersData.providers || [];
    if ($('kpi-providers')) $('kpi-providers').textContent = String(provList.length || 1);
    if ($('kpi-providers-sub')) {
      const errs = provList.filter((p) => p.error).length;
      $('kpi-providers-sub').textContent = errs
        ? `⚠️ ${plural(errs, 'erreur', 'erreurs')}`
        : (provList.length > 1 ? 'Multi-abonnements actifs' : 'Principal actif');
    }
    list.replaceChildren(...provList.map((p) => {
      const meta = p.source === 'configuration'
        ? `${p.host} · configuration du conteneur`
        : `${p.host} · ${p.username} · ajouté dans l'interface`;
      const role = p.index === 1 ? ' · 👤 espace principal' : (p.aux ? ' · 🛡️ complément / secours' : ' · 👤 espace membre');
      let conns = '';
      if (!p.error && p.max_connections > 1) {
        if (p.reservation && p.reservation.count > 0) {
          const rc = Math.min(Number(p.reservation.count), p.max_connections);
          const bk = Math.max(0, p.max_connections - rc);
          const who = providerSpaceName(p.reservation.profile);
          conns = ` · ${p.max_connections} connexions (🔒 ${rc} réservée${rc > 1 ? 's' : ''} « ${who} »${bk ? ` · 🛡️ ${bk} secours` : ''})`;
        } else {
          conns = ` · ${p.max_connections} connexions (partagées)`;
        }
      }
      const agent = p.user_agent ? ` · user agent : ${p.user_agent}` : '';
      const mix = p.mixable ? ' · mélangeable' : ' · isolé';
      const creds = h('span', { class: 'prov-creds', hidden: true });
      const show = h('button', { type: 'button', class: 'btn small', text: 'Afficher', title: 'Afficher l\'utilisateur et le mot de passe',
        onclick: () => toggleCredentials(p, creds, show) });
      const actions = p.source === 'configuration'
        ? h('span', { class: 'row-actions' }, show,
          providersData.mix_editable ? h('button', { type: 'button', class: 'btn small', text: 'Modifier', onclick: () => editMix(p) }) : null)
        : h('span', { class: 'row-actions' }, show,
        h('button', { type: 'button', class: 'btn small', text: 'Modifier', onclick: () => editProvider(p) }),
        h('button', { type: 'button', class: 'btn small danger', text: 'Supprimer', onclick: () => deleteProvider(p) }));
      return h('li', null,
        h('span', null, h('strong', { text: `${p.index}. ${p.name}` }), h('br'),
          h('span', { class: p.error ? 'warn-text' : 'muted small', text: p.error || meta + role + conns + mix + agent }), creds),
        actions);
    }));
    $('provider-add-drawer').hidden = !providersData.editable;
    $('provider-form').hidden = !providersData.editable;
    $('providers-hint').textContent = providersData.editable ? ''
      : 'Ajout désactivé dans ce mode de démarrage : utilise les variables XTREAM_URL_2… puis redémarre le conteneur.';
  }

  /** Après un changement d'abonnement : profils, barre « Espace membre », catalogues et badge à jour sans recharger la page. */
  async function afterProvidersChange() {
    try {
      const status = await request('/api/status');
      state.status = status;
      state.sources = status.sources || status.profiles || [];
      state.profiles = status.profiles || [];
      if (!state.profiles.some((p) => p.id === state.profile)) state.profile = '1';
      document.body.dataset.space = multiProfile() ? state.profile : '1';
      renderProfileBar();
    } catch (_) { /* affichage seulement */ }
    for (const kind of KINDS) state.cat[kind].raw = null;
    renderProviderSelect($('opt-provider').value || state.profile);
    await loadCatalog(false, 'live');
    refresh();
    renderPlaylists();
    loadSummary();
    loadAccount();
    loadProviders();
  }

  /** Abonnement de la configuration du conteneur : rôle, mélange et répartition des connexions se modifient ici. */
  async function editMix(p) {
    const dialog = $('provider-mix-dialog');
    $('pmix-title').textContent = `Modifier « ${p.name} »`;
    const modeSel = $('pmix-mode');
    const options = [
      ['0', 'Non : reste isolé dans son espace membre'],
      ['1', 'Oui : espace membre + mélangeable avec les autres profils'],
      ...(p.index > 1 ? [['2', 'Complément / secours : sans onglet en haut, mélange & débordement auto']] : []),
    ];
    modeSel.replaceChildren(...options.map(([v, label]) => h('option', { value: v, text: label })));
    modeSel.value = p.aux ? '2' : (p.mixable ? '1' : '0');
    const maxCons = Number(p.max_connections || 0);
    const updateModalSplit = (initialRes = null) => {
      fillSplitPanel(
        $('pmix-split-box'), $('pmix-split-badge'), $('pmix-res-profile'),
        $('pmix-res-count'), $('pmix-split-summary'), maxCons,
        { isAux: modeSel.value === '2', editingIndex: p.index, currentName: p.name, initialRes, defaultCount: 0 },
      );
      const count = Number($('pmix-res-count').value || 0);
      if (maxCons > 1 && count > 0 && count < maxCons && modeSel.value === '0') modeSel.value = '1';
    };
    updateModalSplit(p.reservation || null);
    modeSel.onchange = () => updateModalSplit(null);
    $('pmix-res-profile').onchange = () => updateModalSplit(null);
    $('pmix-res-count').onchange = () => updateModalSplit(null);
    const confirmed = await new Promise((resolve) => {
      dialog.returnValue = 'cancel';
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
      dialog.showModal();
    });
    if (!confirmed) return;
    const isAux = modeSel.value === '2';
    const resCount = maxCons > 1 ? Number($('pmix-res-count').value || 0) : 0;
    const reservation = resCount > 0 ? { profile: $('pmix-res-profile').value || String(p.index), count: resCount } : null;
    const mixable = isAux || modeSel.value === '1' || (maxCons > 1 && resCount > 0 && resCount < maxCons);
    try {
      await request(`/api/providers/${p.index}/mix`, 'PUT', { aux: isAux, mixable, reservation });
      toast('Abonnement enregistré');
      afterProvidersChange();
    } catch (error) { toast(error.message, 'error'); }
  }

  /** Identifiants d'un abonnement, demandés au serveur seulement quand on clique (réseau local seulement). */
  const fetchCredentials = (p) => request(`/api/providers/${p.index}/credentials`);

  async function toggleCredentials(p, box, button) {
    if (!box.hidden) {
      box.hidden = true;
      box.replaceChildren();
      button.textContent = 'Afficher';
      return;
    }
    try {
      const c = await fetchCredentials(p);
      box.replaceChildren(h('br'), h('span', { class: 'k', text: 'Utilisateur : ' }), h('code', { text: c.username }),
        h('span', { class: 'k', text: ' · Mot de passe : ' }), h('code', { text: c.password }));
      box.hidden = false;
      button.textContent = 'Masquer';
    } catch (error) { toast(error.message, 'error'); }
  }

  /** Modifier un abonnement ajouté dans l'interface : le formulaire passe en mode édition (tous les champs). */
  async function editProvider(p) {
    const drawer = $('provider-add-drawer');
    if (drawer) drawer.open = true;
    const form = $('provider-form');
    form.dataset.edit = String(p.index);
    $('provider-name').value = p.name;
    $('provider-url').value = p.url || '';
    $('provider-user').value = '';
    $('provider-user').placeholder = `inchangé (${p.username})`;
    $('provider-pass').value = '';
    $('provider-pass').placeholder = 'inchangé';
    try {
      const c = await fetchCredentials(p);
      $('provider-user').value = c.username;
      $('provider-pass').value = c.password;
    } catch (_) { /* vide = inchangé */ }
    $('provider-user').required = false;
    $('provider-pass').required = false;
    $('provider-role').value = p.aux ? 'aux' : 'space';
    $('provider-ua').value = p.user_agent || '';
    $('provider-mix').checked = Boolean(p.mixable || p.aux);
    $('provider-mix').disabled = Boolean(p.aux);
    providerProbe = { key: providerProbeKey(), max: Number(p.max_connections || 0) };
    refreshProviderSplit(p.reservation || null, 0);
    $('provider-add').textContent = `Enregistrer « ${p.name} »`;
    $('provider-cancel').hidden = false;
    $('provider-edit-hint').hidden = false;
    $('provider-edit-hint').textContent = `Modification de « ${p.name} » : corrige ce qui a changé (👁 pour voir le mot de passe). Une nouvelle adresse ou de nouveaux identifiants sont testés auprès du fournisseur avant l'enregistrement.`;
    form.scrollIntoView({ behavior: 'smooth', block: 'center' });
    $('provider-name').focus();
  }

  function resetProviderForm() {
    const form = $('provider-form');
    delete form.dataset.edit;
    providerProbe = { key: '', max: 0 };
    for (const id of ['provider-name', 'provider-url', 'provider-user', 'provider-pass', 'provider-ua']) $(id).value = '';
    $('provider-user').placeholder = '';
    $('provider-pass').placeholder = '';
    $('provider-pass').type = 'password';
    $('provider-user').required = true;
    $('provider-pass').required = true;
    $('provider-role').value = 'space';
    $('provider-mix').checked = false;
    $('provider-mix').disabled = false;
    $('provider-split-box').hidden = true;
    $('provider-res-profile').replaceChildren();
    $('provider-res-count').replaceChildren();
    $('provider-split-summary').textContent = '';
    $('provider-add').textContent = '+ Ajouter l\'abonnement';
    $('provider-cancel').hidden = true;
    $('provider-edit-hint').hidden = true;
    if ($('provider-add-drawer')) $('provider-add-drawer').open = false;
  }

  async function deleteProvider(p) {
    const used = state.playlists.filter((x) => x.provider === String(p.index)).map((x) => x.name);
    const ok = await ask({ title: `Supprimer « ${p.name} » ?`, okLabel: 'Supprimer', danger: true,
      text: used.length ? `Ses playlists (${used.join(', ')}) et leurs appareils restent enregistrés mais deviennent vides.`
        : 'Ses identifiants sont effacés du serveur.' });
    if (!ok) return;
    try {
      await request(`/api/providers/${p.index}`, 'DELETE');
      toast('Abonnement supprimé');
      afterProvidersChange();
    } catch (error) { toast(error.message, 'error'); }
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
      cat.provider ? h('span', { class: 'badge', text: cat.provider }) : null,
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
    $('export').href = `/api/catalog/export?kind=${state.kind}${hasMultipleSources() ? `&profile=${encodeURIComponent(state.profile)}` : ''}`;
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
    ruleCache.clear();
    target.raw = catalog;
    const allowed = catalogProviders();
    const keep = (x) => !allowed || allowed.has(x.profile || '1');
    target.categories = catalog.categories.filter(keep).map((cat) => ({ ...cat, _search: `${norm(cat.name)} #${cat.category_id}` }));
    target.categoryNames = new Map(target.categories.map((cat) => [cat.category_id, cat.name]));
    target.channels = catalog.channels.filter(keep).map((c) => ({ ...c, _search: norm(`${c.name} ${c.year || ''}`) }));
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

  function renderExpiry(a) {
    const banner = $('expiry-banner');
    banner.hidden = true;
    if (!a) return;
    const now = Date.now() / 1000;
    if (a.exp_date && a.exp_date < now) {
      banner.className = 'banner error';
      banner.textContent = `⚠️ Ton abonnement Xtream a expiré le ${new Date(a.exp_date * 1000).toLocaleDateString('fr-FR')} : les chaînes ne se lanceront plus.`;
      banner.hidden = false;
    } else if (a.exp_date && a.exp_date - now < 14 * 86400) {
      const days = Math.max(0, Math.ceil((a.exp_date - now) / 86400));
      banner.className = 'banner warn';
      banner.textContent = `⏳ Ton abonnement Xtream expire dans ${plural(days, 'jour', 'jours')} (le ${new Date(a.exp_date * 1000).toLocaleDateString('fr-FR')}). Pense à le renouveler.`;
      banner.hidden = false;
    } else if (a.status && !/^active$/i.test(a.status)) {
      banner.className = 'banner warn';
      banner.textContent = `Statut de l'abonnement renvoyé par le fournisseur : ${a.status}.`;
      banner.hidden = false;
    }
  }

  function renderAccountFacts(status) {
    const own = hasMultipleSources() && status && (status.providers || []).find((p) => String(p.index) === state.profile);
    const a = own ? own.account : status && status.account;
    const facts = $('account-facts');
    const rows = [];
    if (!a) rows.push(['État', 'Informations indisponibles (le fournisseur ne répond pas)']);
    else {
      const now = Date.now() / 1000;
      rows.push(['Statut', a.status || '?']);
      if (a.exp_date) {
        const days = Math.ceil((a.exp_date - now) / 86400);
        rows.push(['Expiration', `${new Date(a.exp_date * 1000).toLocaleDateString('fr-FR', { dateStyle: 'long' })} (${days >= 0 ? `dans ${plural(days, 'jour', 'jours')}` : 'expiré'})`]);
      } else rows.push(['Expiration', 'non communiquée']);
      rows.push(['Connexions', `${a.active_connections || 0} active(s) sur ${a.max_connections || '?'} autorisée(s)`]);
      rows.push(['Fuseau du fournisseur', a.timezone || 'non communiqué (UTC)']);
      if (a.is_trial) rows.push(['Type', 'Essai']);
    }
    if (own) rows.unshift(['Espace', own.name]);
    facts.replaceChildren(...rows.flatMap(([k, v]) => [h('dt', { text: k }), h('dd', { text: v })]));
    if (status) $('sys-version').textContent = `Xtream M3U Local ${status.version}`;
  }

  const DIAG_ICON = { ok: '✅', warn: '⚠️', error: '❌', skipped: '⏭️' };
  let lastDiagnostic = null;

  async function runDiagnostic() {
    const btn = $('diag-run');
    btn.disabled = true;
    btn.textContent = 'Diagnostic en cours…';
    $('diag-results').replaceChildren(h('li', { class: 'empty', text: 'Tests en cours (jusqu\'à une minute selon le fournisseur)…' }));
    try {
      lastDiagnostic = await request(`/api/diagnostic${profileQuery()}`, 'POST');
      const list = $('diag-results');
      list.replaceChildren(...lastDiagnostic.results.map((r) => h('li', { class: `diag-item ${r.status}` },
        h('span', { class: 'icon', text: DIAG_ICON[r.status] || '•' }),
        h('span', { class: 'grow' }, h('span', { class: 'd-label', text: r.label }), h('span', { class: 'd-detail', text: r.detail })),
        r.ms != null ? h('span', { class: 'd-ms', text: `${r.ms.toLocaleString('fr-FR')} ms` }) : null)));
      $('diag-copy').hidden = false;
      const errors = lastDiagnostic.results.filter((r) => r.status === 'error').length;
      const warnings = lastDiagnostic.results.filter((r) => r.status === 'warn').length;
      toast(errors ? `Diagnostic terminé : ${plural(errors, 'erreur', 'erreurs')}`
        : `Diagnostic terminé : aucune erreur${warnings ? ` (${plural(warnings, 'avertissement', 'avertissements')})` : ''}`, errors ? 'error' : 'ok');
    } catch (error) {
      $('diag-results').replaceChildren(h('li', { class: 'empty', text: error.message }));
    } finally {
      btn.disabled = false;
      btn.textContent = 'Relancer le diagnostic';
    }
  }

  async function restoreBackup() {
    const file = $('restore-file').files[0];
    if (!file) { toast('Choisis d\'abord un fichier de sauvegarde', 'error'); return; }
    let backup;
    try { backup = JSON.parse(await file.text()); } catch (_) { toast('Fichier JSON illisible', 'error'); return; }
    const mode = $('restore-mode').value;
    const withDevices = $('restore-devices').checked;
    const count = Array.isArray(backup.playlists) ? backup.playlists.length : 0;
    if (!(await ask({ title: 'Restaurer la sauvegarde ?', text: `${plural(count, 'playlist', 'playlists')} à ${mode === 'replace' ? 'restaurer en remplaçant toutes les playlists actuelles' : 'fusionner avec les playlists actuelles'}${withDevices ? ', appareils compris' : ''}.`, okLabel: 'Restaurer', danger: mode === 'replace' || withDevices }))) return;
    try {
      const result = await request('/api/restore', 'POST', { backup, mode, include_devices: withDevices });
      toast(`Restauration terminée : ${result.added} ajoutée(s), ${result.replaced} remplacée(s)`);
      state.playlists = await request('/api/playlists');
      load(state.playlists.length ? [...state.playlists].filter(inProfile).sort((a, b) => collator.compare(a.name, b.name))[0] || null : null);
    } catch (error) { toast(error.message, 'error'); }
  }

  async function loadAccount() {
    try {
      const status = await request('/api/status?account=1');
      renderAccountFacts(status);
      state.account = status.account;
      state.accounts = status.providers || [];
      const activeAccount = hasMultipleSources()
        ? (((state.accounts.find((x) => String(x.index) === state.profile) || {}).account) ?? state.account)
        : state.account;
      renderExpiry(activeAccount);
      renderAccountPill();
    } catch (_) { /* facultatif */ }
  }

  /** Badge de l'abonnement. Connexions : le compteur du fournisseur n'est lu qu'à l'ouverture de la page
   *  (et il est souvent en retard) ; on prend le plus grand avec les lectures relayées par ce serveur. */
  function renderAccountPill() {
    const a = hasMultipleSources()
      ? (((state.accounts.find((x) => String(x.index) === state.profile) || {}).account) ?? state.account)
      : state.account;
    if (!a) return;
    const parts = [];
    if (a.status) parts.push(a.status === 'Active' ? 'Abonnement actif' : `Abonnement : ${a.status}`);
    if (a.exp_date) parts.push(`expire le ${new Date(a.exp_date * 1000).toLocaleDateString('fr-FR')}`);
    if (a.max_connections) {
      const active = Math.max(Number(a.active_connections) || 0, state.localStreams || 0);
      parts.push(`${active}/${a.max_connections} connexion${a.max_connections === '1' ? '' : 's'}`);
    }
    if (parts.length) {
      $('account').textContent = parts.join(' · ');
      $('account').title = 'Abonnement (player_api.php) ; connexions = lectures en cours sur ce serveur ou compteur du fournisseur';
      $('account').hidden = false;
    }
  }

  async function init() {
    initTheme();
    const [status, playlists] = await Promise.allSettled([request('/api/status'), request('/api/playlists')]);
    if (status.status === 'fulfilled') {
      state.status = status.value;
      $('version').textContent = `version ${state.status.version}`;
      if ($('rail-version')) $('rail-version').textContent = `v${state.status.version}`;
      state.sources = state.status.sources || state.status.profiles || [];
      state.profiles = state.status.profiles || [];
      const remembered = storage('xtream-m3u-profile');
      state.profile = state.profiles.some((p) => p.id === remembered) ? remembered : '1';
      document.body.dataset.space = multiProfile() ? state.profile : '1';
      let open = null;
      try { open = JSON.parse(storage('xtream-m3u-open-providers') || 'null'); } catch (_) { open = null; }
      state.openGroups = new Set(Array.isArray(open) ? open.map(String) : [state.profile]);
      renderProfileBar();
      if (state.status.token_required && !state.status.access_token) {
        $('token-card').hidden = false;
        $('token').value = storage('xtream-m3u-token');
      }
    } else {
      showError(status.reason.message);
    }
    if (playlists.status === 'fulfilled') state.playlists = playlists.value;
    else showError(playlists.reason.message);
    const firstInProfile = [...state.playlists].sort((a, b) => collator.compare(a.name, b.name));
    load(firstInProfile.find((p) => hasMultipleSources() && playlistProfile(p) === state.profile) || firstInProfile.find(inProfile) || null);
    const fromHash = Object.keys(VIEWS).find((v) => VIEWS[v] === location.hash);
    if (fromHash) {
      if (fromHash !== 'playlists') switchView(fromHash);
    } else if (state.playlists.length > 0) {
      switchView('dashboard');
    }
    loadStreams();
    if (await loadCatalog(false, 'live')) refresh();
    loadSummary();
    loadAccount();
  }

  // ---------------------------------------------------------- événements
  $('theme-toggle').addEventListener('click', toggleTheme);
  $('dash-refresh').addEventListener('click', loadDashboard);
  $('dash-new-playlist').addEventListener('click', async () => {
    switchView('playlists');
    if (!(await confirmLeave())) return;
    load(null);
    $('name').focus();
  });
  $('kpi-providers-btn').addEventListener('click', () => { switchView('system'); selectSysTab('providers'); });
  $('kpi-devices-btn').addEventListener('click', () => switchView('devices'));
  $('kpi-alerts-btn').addEventListener('click', () => { switchView('system'); selectSysTab('alerts'); });
  $('device-add-drawer').addEventListener('toggle', (event) => {
    if (event.isTrusted) deviceDrawerTouched = true;
  });
  $('probe-close').addEventListener('click', () => $('probe-dialog').close());
  $('probe-rerun').addEventListener('click', () => {
    if (probeContext) openVersionProbe(probeContext.canonicalId, probeContext.title, probeContext.ids);
  });
  $('probe-reset-health').addEventListener('click', async () => {
    try {
      await request('/api/streams/health/reset', 'POST', {});
      toast('Quarantaines réinitialisées');
      loadStreams();
      if (probeContext) openVersionProbe(probeContext.canonicalId, probeContext.title, probeContext.ids);
    } catch (error) { toast(error.message, 'error'); }
  });
  $('health-reset-btn').addEventListener('click', async () => {
    try {
      await request('/api/streams/health/reset', 'POST', {});
      toast('Stabilité des versions réinitialisée (retour sur HD)');
      loadStreams();
    } catch (error) { toast(error.message, 'error'); }
  });
  $('qr-close').addEventListener('click', () => $('qr-dialog').close());
  $('qr-mode-m3u').addEventListener('click', () => { if (qrState.device) openDeviceQr(qrState.device, 'm3u'); });
  $('qr-mode-xtream').addEventListener('click', () => { if (qrState.device) openDeviceQr(qrState.device, 'xtream'); });
  $('new').addEventListener('click', async () => {
    if (!(await confirmLeave())) return;
    load(null);
    $('name').focus();
  });
  $('save').addEventListener('click', save);
  $('profile-manage').addEventListener('click', () => { switchView('system'); selectSysTab('providers'); });
  $('full-card').addEventListener('toggle', (event) => {
    if (event.isTrusted) state.fullCardTouched = true;
  });
  $('opt-live-format').addEventListener('change', () => { renderOptionsPill(); renderStats(); });
  $('opt-catchup').addEventListener('change', () => { renderOptionsPill(); renderStats(); });
  $('opt-dedupe').addEventListener('change', () => { renderOptionsPill(); refresh(); });
  $('opt-failover').addEventListener('change', () => { renderOptionsPill(); refresh({ channels: false }); });
  $('opt-adaptive-failover').addEventListener('change', () => { renderOptionsPill(); renderStats(); });
  $('opt-hq-logos').addEventListener('change', () => { renderOptionsPill(); refresh(); });
  $('opt-clean-names').addEventListener('change', () => { renderOptionsPill(); refresh(); });
  $('opt-adult').addEventListener('change', () => { renderOptionsPill(); refresh(); });
  $('opt-provider').addEventListener('change', () => { reapplyCatalogs(); refresh(); });
  $('tg-token-eye').addEventListener('click', () => {
    const field = $('tg-token');
    field.type = field.type === 'password' ? 'text' : 'password';
  });
  $('tg-pair-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const token = $('tg-token').value.trim();
    if (!token) { $('tg-token').focus(); return; }
    const btn = $('tg-pair-btn');
    btn.disabled = true;
    btn.textContent = 'Génération du QR code…';
    try {
      const data = await request('/api/alerts/pair', 'POST', { bot_token: token });
      renderAlerts(data);
      toast('QR code prêt : scanne-le puis clique sur Démarrer dans Telegram');
    } catch (error) {
      toast(error.message, 'error');
      $('tg-hint').textContent = `⚠️ ${error.message}`;
    } finally {
      btn.disabled = false;
      btn.textContent = 'Générer le QR code';
    }
  });
  $('tg-pair-cancel').addEventListener('click', async () => {
    try {
      renderAlerts(await request('/api/alerts/cancel', 'POST'));
    } catch (error) { toast(error.message, 'error'); }
  });
  $('tg-manual-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const token = $('tg-token').value.trim();
    const chatId = $('tg-manual-chat').value.trim();
    if (!token) { toast('Colle d\'abord le token du bot ci-dessus', 'error'); $('tg-token').focus(); return; }
    if (!chatId) { $('tg-manual-chat').focus(); return; }
    const btn = $('tg-manual-btn');
    btn.disabled = true;
    try {
      const data = await request('/api/alerts/manual', 'POST', { bot_token: token, chat_id: chatId });
      $('tg-token').value = '';
      $('tg-manual-chat').value = '';
      renderAlerts(data);
      toast('Telegram connecté !');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      btn.disabled = false;
    }
  });
  $('tg-test-btn').addEventListener('click', async () => {
    const btn = $('tg-test-btn');
    btn.disabled = true;
    try {
      renderAlerts(await request('/api/alerts/test', 'POST'));
      toast('Message de test envoyé sur Telegram');
    } catch (error) {
      toast(error.message, 'error');
      loadAlerts({ silent: true });
    } finally {
      btn.disabled = false;
    }
  });
  $('tg-weekly-btn').addEventListener('click', async () => {
    const btn = $('tg-weekly-btn');
    btn.disabled = true;
    try {
      renderAlerts(await request('/api/alerts/weekly', 'POST'));
      toast('Récapitulatif 7 jours envoyé sur Telegram');
    } catch (error) {
      toast(error.message, 'error');
      loadAlerts({ silent: true });
    } finally {
      btn.disabled = false;
    }
  });
  $('tg-disconnect-btn').addEventListener('click', async () => {
    if (!(await ask({
      title: 'Déconnecter Telegram ?', okLabel: 'Déconnecter', danger: true,
      text: 'Le token et l\'identifiant de discussion seront supprimés de ce serveur et tu ne recevras plus d\'alertes.',
    }))) return;
    try {
      renderAlerts(await request('/api/alerts/disconnect', 'POST'));
      $('tg-token').value = '';
      toast('Telegram déconnecté');
    } catch (error) { toast(error.message, 'error'); }
  });
  for (const id of ['tg-enabled', 'tg-ev-expiry', 'tg-ev-provider', 'tg-ev-unstable', 'tg-ev-weekly', 'tg-ev-tunnel', 'tg-ev-security']) {
    $(id).addEventListener('change', saveAlertOptions);
  }
  $('tailscale-local').addEventListener('change', async (event) => {
    const on = event.target.checked;
    if (on && !(await ask({ title: 'Tailscale = mon réseau local ?', okLabel: 'Activer',
      text: 'Tous les appareils de ton réseau Tailscale auront le même accès que chez toi (interface Web et playlists, sans code). À éviter si tu as partagé ton serveur Tailscale avec d\'autres personnes : pour elles, utilise plutôt un code d\'appareil.' }))) {
      event.target.checked = false;
      return;
    }
    try {
      await request('/api/settings', 'PUT', { tailscale_local: on });
      toast(on ? 'Tailscale traité comme ton réseau local' : 'Tailscale traité comme un accès distant');
    } catch (error) {
      event.target.checked = !on;
      toast(error.message, 'error');
    }
    loadSettings();
  });
  $('epg-suggest').addEventListener('click', suggestGuides);
  $('tunnel-form').addEventListener('submit', connectTunnel);
  $('guides-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const url = $('guides-input').value.trim();
    if (!/^https?:\/\/\S{4,}$/.test(url)) { toast('Adresse attendue : https://…', 'error'); return; }
    $('guides-input').value = '';
    saveGuides([...new Set([...((settingsData && settingsData.epg_sources) || []), url])]);
  });
  $('epg-groups-load').addEventListener('click', loadGroupGuides);
  $('opt-group-epg').addEventListener('change', () => refresh({ channels: false }));
  $('opt-multi-groups').addEventListener('change', () => { state.groupCache = null; state.hiddenCache = null; refresh(); });
  $('hosts-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const host = $('hosts-input').value.trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '');
    if (!/^([a-z0-9-]+\.)+[a-z]{2,}$/.test(host)) { toast('Adresse attendue : ex. tv2.mondomaine.fr', 'error'); return; }
    $('hosts-input').value = '';
    saveHosts([...new Set([...((settingsData && settingsData.public_hosts) || []), host])]);
  });
  $('tunnel-toggle').addEventListener('click', async () => {
    const stopping = $('tunnel-toggle').textContent === 'Arrêter';
    try { renderTunnel(await request(`/api/tunnel/${stopping ? 'stop' : 'start'}`, 'POST')); loadTunnel({ poll: !stopping }); }
    catch (error) { toast(error.message, 'error'); }
  });
  $('tunnel-remove').addEventListener('click', async () => {
    if (!(await ask({ title: 'Supprimer le tunnel ?', okLabel: 'Supprimer', danger: true,
      text: 'Les TV hors de chez toi n\'auront plus accès. L\'adresse reste dans les DNS de ton domaine : tu peux l\'effacer dans Cloudflare.' }))) return;
    try { renderTunnel(await request('/api/tunnel/remove', 'POST')); toast('Tunnel supprimé'); } catch (error) { toast(error.message, 'error'); }
  });
  $('provider-role').addEventListener('change', () => {
    const isAux = $('provider-role').value === 'aux';
    if (isAux) $('provider-mix').checked = true;
    $('provider-mix').disabled = isAux;
    if (providerProbe.max > 1) refreshProviderSplit(null, 1);
  });
  $('provider-name').addEventListener('input', () => {
    if (providerProbe.max > 1) refreshProviderSplit(null, 1);
  });
  $('provider-res-profile').addEventListener('change', () => refreshProviderSplit(null, 1));
  $('provider-res-count').addEventListener('change', () => refreshProviderSplit(null, 1));
  $('provider-probe').addEventListener('click', async () => {
    const btn = $('provider-probe');
    const prev = btn.textContent;
    btn.disabled = true;
    btn.textContent = 'Vérification…';
    try {
      const maxCons = await probeProviderForm({ quiet: false });
      if (maxCons > 1 && !$('provider-form').dataset.edit) {
        $('provider-add').textContent = 'Confirmer et ajouter';
        $('provider-split-box').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = prev;
    }
  });
  $('provider-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const form = $('provider-form');
    const editing = form.dataset.edit;
    const button = $('provider-add');
    const label = button.textContent;
    button.disabled = true;
    button.textContent = 'Test de la connexion…';
    try {
      // À l'ajout : on détecte d'abord le nombre de connexions. Si > 1, on propose la répartition avant d'enregistrer.
      if (!editing && providerProbe.key !== providerProbeKey()) {
        const maxCons = await probeProviderForm({ quiet: true });
        if (maxCons > 1) {
          button.textContent = 'Confirmer et ajouter';
          $('provider-split-box').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
          toast(`${maxCons} connexions détectées : choisis la répartition ci-dessous puis confirme`);
          return;
        }
      }
      const isAux = $('provider-role').value === 'aux';
      const reservation = readProviderReservation();
      const mixable = isAux || $('provider-mix').checked
        || Boolean(reservation && providerProbe.max > 1 && reservation.count < providerProbe.max);
      const body = {
        name: $('provider-name').value.trim(), url: $('provider-url').value.trim(),
        username: $('provider-user').value.trim(), password: $('provider-pass').value,
        user_agent: $('provider-ua').value.trim(), aux: isAux, mixable, reservation };
      if (editing) {
        const saved = await request(`/api/providers/${editing}`, 'PUT', body);
        toast(`Abonnement « ${saved.name} » enregistré`);
      } else {
        const added = await request('/api/providers', 'POST', body);
        toast(isAux
          ? `Abonnement « ${added.name} » ajouté en complément / secours`
          : `Abonnement « ${added.name} » ajouté : choisis son espace en haut`);
      }
      resetProviderForm();
      await afterProvidersChange();
    } catch (error) {
      toast(error.message, 'error');
      button.textContent = label;
    } finally {
      button.disabled = false;
      if (button.textContent === 'Test de la connexion…') button.textContent = label;
    }
  });
  $('provider-cancel').addEventListener('click', resetProviderForm);
  $('provider-pass-eye').addEventListener('click', () => {
    const field = $('provider-pass');
    field.type = field.type === 'password' ? 'text' : 'password';
  });
  $('opt-failover-any').addEventListener('change', () => { renderOptionsPill(); refresh({ channels: false }); });
  $('opt-uhd').addEventListener('change', () => { state.hiddenCache = null; renderOptionsPill(); refresh(); });
  $('opt-separators').addEventListener('change', () => { renderOptionsPill(); refresh(); });
  $('opt-fav-name').addEventListener('input', () => renderStats());
  $('rule-value').addEventListener('input', debounce(previewRule, 150));
  $('rule-match').addEventListener('change', previewRule);
  const addRule = () => {
    const value = $('rule-value').value.trim();
    if (!value) { $('rule-value').focus(); return; }
    const rule = { kind: state.kind, match: $('rule-match').value, value: value.slice(0, 100) };
    if (state.rules.some((r) => r.kind === rule.kind && r.match === rule.match && squash(r.value) === squash(rule.value))) { toast('Cette règle existe déjà', 'error'); return; }
    state.rules = [...state.rules, rule];
    $('rule-value').value = '';
    refresh();
    toast(`Règle ajoutée : ${plural(ruleSet(rule.kind, [rule]).size, 'catégorie', 'catégories')}`);
  };
  $('rule-add').addEventListener('click', addRule);
  $('rule-value').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); addRule(); } });
  $('clear-favorites').addEventListener('click', () => { state.favorites = []; refresh(); });
  $('clear-custom').addEventListener('click', async () => {
    if (!(await ask({ title: 'Effacer les personnalisations ?', text: 'Tous les noms, numéros, logos, guides et versions épinglées de cette playlist seront supprimés.', okLabel: 'Effacer', danger: true }))) return;
    state.itemNames = {}; state.itemNumbers = {}; state.itemLogos = {}; state.itemEpg = {}; state.itemXmltv = {}; state.itemPinned = {};
    refresh();
  });
  $('auto-number').addEventListener('click', async () => {
    const groups = groupCounts();
    const order = orderedGroups(groups).filter((key) => groupInfo(key).kind === 'live');
    const sel = state.sel.live;
    const hidden = hiddenDuplicates();
    const assign = groupAssignment();
    const cgById = new Map(orderedCustomGroups().map((g) => [g.id, g]));
    const byGroup = new Map(order.map((g) => [g, []]));
    for (const c of state.cat.live.channels) {
      if (sel.excluded.has(c.stream_id) || hidden.has(c.stream_id) || adultHidden(c, 'live')) continue;
      const inCat = c.category_ids.find((id) => catOn('live', id));
      if (!inCat && !sel.included.has(c.stream_id)) continue;
      const custom = assign.get(c.stream_id);
      const g = custom ? CG + custom : (inCat || c.category_id);
      if (g != null && byGroup.has(g)) byGroup.get(g).push(c);
    }
    const patternRank = (c, gKey) => {
      if (!gKey.startsWith(CG)) return 1e9;
      const cg = cgById.get(gKey.slice(CG.length));
      if (!cg || !cg._patterns.length || state.itemGroups[c.stream_id] === cg.id) return 1e8;
      if (c._core === undefined) c._core = matchName(c.name);
      const core = cg.region ? (c.regional?.[1] || '') : c._core;
      if (cg.match === 'exact') return cg._exact.get(core) ?? 1e8;
      const padded = ` ${core} `;
      const idx = cg._patterns.findIndex((p) => padded.includes(` ${p} `));
      return idx >= 0 ? idx : 1e8;
    };
    const ordered = [];
    for (const g of order) {
      const list = byGroup.get(g);
      list.sort((a, b) => (state.itemNumbers[a.stream_id] || 1e9) - (state.itemNumbers[b.stream_id] || 1e9)
        || patternRank(a, g) - patternRank(b, g));
      ordered.push(...list);
    }
    if (!ordered.length) { toast('Aucune chaîne à numéroter', 'error'); return; }
    if (Object.keys(state.itemNumbers).length && !(await ask({ title: 'Renuméroter ?', text: `Les ${ordered.length} chaînes seront numérotées de 1 à ${ordered.length} dans l'ordre actuel ; les numéros existants seront remplacés.`, okLabel: 'Numéroter' }))) return;
    state.itemNumbers = {};
    ordered.forEach((c, i) => { state.itemNumbers[c.stream_id] = i + 1; });
    refresh();
    toast(`${ordered.length} chaînes numérotées`);
  });
  $('epg-refresh').addEventListener('click', refreshEpg);
  document.querySelectorAll('[data-kind]').forEach((btn) => btn.addEventListener('click', () => switchKind(btn.dataset.kind)));
  $('view-btn-dashboard').addEventListener('click', () => switchView('dashboard'));
  $('view-btn-playlists').addEventListener('click', () => switchView('playlists'));
  $('view-btn-guide').addEventListener('click', () => switchView('guide'));
  $('view-btn-catalog').addEventListener('click', () => switchView('catalog'));
  $('view-btn-system').addEventListener('click', () => switchView('system'));
  $('view-btn-devices').addEventListener('click', () => switchView('devices'));
  for (const tab of SYS_TABS) $(`sys-btn-${tab}`).addEventListener('click', () => selectSysTab(tab));
  document.querySelector('.sys-nav').addEventListener('keydown', (event) => {
    const index = SYS_TABS.indexOf(state.sysTab);
    const next = { ArrowRight: SYS_TABS[(index + 1) % SYS_TABS.length], ArrowLeft: SYS_TABS[(index + SYS_TABS.length - 1) % SYS_TABS.length] }[event.key];
    if (next) { event.preventDefault(); selectSysTab(next); $(`sys-btn-${next}`).focus(); }
  });
  $('device-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = $('device-name').value.trim();
    if (!name) { $('device-name').focus(); return; }
    try {
      const device = await request('/api/devices', 'POST', { name, playlist: $('device-playlist').value, priority: $('device-priority').value,
        links: $('device-links').value, profile: hasMultipleSources() ? state.profile : '' });
      $('device-name').value = '';
      deviceDrawerTouched = false;
      if ($('device-add-drawer')) $('device-add-drawer').open = false;
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
  $('streams-pill').addEventListener('click', () => switchView('dashboard'));
  $('streams-refresh').addEventListener('click', loadStreams);
  $('diag-run').addEventListener('click', runDiagnostic);
  $('diag-copy').addEventListener('click', () => {
    if (!lastDiagnostic) return;
    const lines = [`Diagnostic Xtream M3U Local ${lastDiagnostic.version} — ${new Date(lastDiagnostic.started_at * 1000).toLocaleString('fr-FR')} (${lastDiagnostic.seconds} s)`];
    for (const r of lastDiagnostic.results) lines.push(`${DIAG_ICON[r.status] || '-'} ${r.label} : ${r.detail}${r.ms != null ? ` (${r.ms} ms)` : ''}`);
    copyText(lines.join('\n'));
  });
  $('restore-run').addEventListener('click', restoreBackup);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadStreams(); });
  $('prov-search').addEventListener('input', debounce(renderProvider, 200));
  $('prov-expand').addEventListener('click', () => {
    let passes = 0;
    const pass = () => {
      const closed = [...document.querySelectorAll('#view-catalog details.tree-node:not([open])')];
      closed.forEach((d) => { d.open = true; });
      if (closed.length && (passes += 1) < 8) setTimeout(pass, 60);
    };
    pass();
  });
  $('prov-collapse').addEventListener('click', () => document.querySelectorAll('#view-catalog details.tree-node').forEach((d) => { d.open = false; }));
  $('cg-add').addEventListener('click', () => createGroup());
  const PRESET_FAMILIES = [
    ['France', ['OTT', 'Bouquet Canal+', 'TNT', 'Chaînes Canal+']],
    ['Maghreb & Arabe', ['Maroc', 'Nilesat', 'Sport AR']],
    ['Belgique & Suisse', ['Belgique', 'Francophonie']],
    ['Thématiques', ['Sport FR', 'Sport', 'Info', 'Cinéma', 'Jeunesse', 'Documentaires']],
  ];
  for (const [family, names] of PRESET_FAMILIES) {
    const line = h('div', { class: 'preset-line' }, h('span', { class: 'preset-cat', text: family }));
    for (const name of names) {
      const preset = PRESETS.find((p) => p.name === name);
      if (preset) line.append(h('button', { type: 'button', class: 'btn small', text: `+ ${preset.name}`, title: preset.hint, onclick: () => createGroup(preset) }));
    }
    $('cg-presets').append(line);
  }
  $('reset-groups').addEventListener('click', () => { state.groupOrder = []; state.groupNames = {}; renderSummary(); renderStats(); });
  $('delete').addEventListener('click', remove);
  $('duplicate').addEventListener('click', duplicate);
  $('revert').addEventListener('click', () => load(state.playlists.find((p) => p.slug === state.editing) || null));
  $('refresh').addEventListener('click', async () => {
    $('refresh').disabled = true;
    clearError();
    setConnection('', 'Actualisation…');
    if (await loadCatalog(true, state.kind)) { refresh(); if (state.view === 'catalog') renderProvider(); }
    loadSummary(true);
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
  for (const sec of SECTIONS) $(`sec-btn-${sec}`).addEventListener('click', () => selectSection(sec));
  document.querySelector('#editor .section-nav').addEventListener('keydown', (event) => {
    const index = SECTIONS.indexOf(state.section);
    const next = { ArrowRight: SECTIONS[(index + 1) % SECTIONS.length], ArrowLeft: SECTIONS[(index + SECTIONS.length - 1) % SECTIONS.length] }[event.key];
    if (next) { event.preventDefault(); selectSection(next); $(`sec-btn-${next}`).focus(); }
  });
  $('tv-preview-refresh').addEventListener('click', () => loadTvPreview());
  $('tv-preview-search').addEventListener('input', debounce(() => renderTvPreview(), 120));
  document.querySelectorAll('[data-pvkind]').forEach((btn) => btn.addEventListener('click', () => {
    state.tvPreviewKind = btn.dataset.pvkind;
    document.querySelectorAll('[data-pvkind]').forEach((b) => b.setAttribute('aria-pressed', String(b === btn)));
    renderTvPreview();
  }));
  $('clean-missing-included').addEventListener('click', async () => {
    await Promise.all(KINDS.filter((k) => state.sel[k].included.size).map(ensureCatalog));
    let removed = 0;
    for (const kind of KINDS) {
      if (!state.cat[kind].catalogLoaded) continue;
      for (const id of [...state.sel[kind].included]) {
        if (!state.cat[kind].channelById.has(id)) {
          state.sel[kind].included.delete(id);
          removed += 1;
        }
      }
    }
    if (removed) {
      refresh();
      toast(`${plural(removed, 'élément introuvable retiré', 'éléments introuvables retirés')} : pense à enregistrer`);
    }
  });
  $('guide-playlist').addEventListener('change', loadGuide);
  $('guide-mine').addEventListener('change', loadGuide);
  $('guide-search').addEventListener('input', debounce(loadGuide, 400));
  for (const btn of document.querySelectorAll('[data-when]')) {
    btn.addEventListener('click', () => {
      guideWhen = btn.dataset.when;
      for (const other of document.querySelectorAll('[data-when]')) other.setAttribute('aria-pressed', String(other === btn));
      $('guide-search').value = '';
      loadGuide();
    });
  }
  $('preview-close').addEventListener('click', () => $('preview').close());
  $('preview').addEventListener('close', stopPreview);
  $('history-provider').addEventListener('change', (event) => {
    historyFilter.provider = event.target.value || 'all';
    loadHistory();
  });
  $('history-device').addEventListener('change', (event) => {
    historyFilter.device = event.target.value || '';
    loadHistory();
  });
  $('history-sort').addEventListener('change', (event) => {
    historyFilter.sort = event.target.value || 'seconds';
    loadHistory();
  });
  $('history-reset').addEventListener('click', () => {
    historyFilter.provider = 'all';
    historyFilter.device = '';
    loadHistory();
  });
  $('history-clear').addEventListener('click', async () => {
    const scope = [];
    if (historyFilter.provider !== 'all') {
      const p = historyMeta.providers.find((x) => String(x.id) === historyFilter.provider);
      scope.push(`l'abonnement « ${p ? p.name : historyFilter.provider} »`);
    }
    if (historyFilter.device) {
      const d = historyMeta.devices.find((x) => x.id === historyFilter.device);
      scope.push(`« ${d ? d.label : historyFilter.device} »`);
    }
    const text = scope.length
      ? `Effacer uniquement l'historique correspondant à ${scope.join(' et ')} ?`
      : 'Effacer tout l\'historique des lectures (tous les abonnements et appareils) ?';
    if (!(await ask({ title: 'Effacer l\'historique des lectures ?', text, okLabel: 'Effacer', danger: true }))) return;
    try { await request(`/api/history/clear?${historyQueryParams()}`, 'POST'); loadHistory(); } catch (error) { toast(error.message, 'error'); }
  });
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
