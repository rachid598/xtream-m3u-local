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
    section: 'content',      // partie de l'éditeur : content | organize | options | guide
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
      // Navigateur très ancien : repli sur les boîtes natives.
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
        if (select) resolve($('dialog-select').value);
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
  const QUALITY = [
    [/(?<![a-z0-9])(4k|uhd|2160p?)(?![a-z0-9])/, 4],
    [/(?<![a-z0-9])(fhd|full ?hd|1080[pi]?)(?![a-z0-9])/, 3],
    [/(?<![a-z0-9])(hd|720p?)(?![a-z0-9])/, 2],
    [/(?<![a-z0-9])(sd|576[pi]?|480[pi]?)(?![a-z0-9])/, 1],
  ];
  const QUALITY_TAGS = /(?<![a-z0-9])(4k|uhd|2160p?|fhd|full ?hd|1080[pi]?|hd|720p?|sd|576[pi]?|480[pi]?|hevc|h\.?26[45]|x26[45]|hdr|50 ?fps|60 ?fps|25 ?fps|backup|raw|multi|vip)(?![a-z0-9])/g;
  const SEP_CHARS = '#=*~_\\-•●◆★☆▬▭━─═';
  const SEPARATOR = new RegExp(`^[${SEP_CHARS}|\\s]*[${SEP_CHARS}]{3,}.*[${SEP_CHARS}]{3,}[${SEP_CHARS}|\\s]*$`, 'u');
  /** Fausse chaîne servant de titre de section (même règle que le serveur). */
  const SYMBOL_EDGES = /^\s*[^\p{L}\p{N}\s]{3,}.*[^\p{L}\p{N}\s]{3,}\s*$/u;  // ♣♦♣--- … ---♣♦♣
  const isSeparator = (name) => { const t = String(name || '').trim(); return !t || SEPARATOR.test(t) || SYMBOL_EDGES.test(t) || !/[\p{L}\p{N}]/u.test(t); };
  const squash = (text) => norm(text).split(/\s+/).filter(Boolean).join(' ');

  // ------------------------------------------- groupes personnalisés (même règle que app/builder.py)
  const CG = 'g:';
  const COUNTRY = /^[^\p{L}\p{N}_]*[A-Za-z]{2}\s*[:|\-\]●•»◉]+\s*/u;
  /** Nom comparable : sans pays (« FR: »), accents, ponctuation ni qualité. « FR: TF1 FHD » → « tf1 ». */
  function matchName(name) {
    let text = String(name || '').replace(COUNTRY, '').normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase();
    text = text.replace(/[^\p{L}\p{N}+&]+/gu, ' ').replace(/\s+\+/g, '+');
    let tokens = text.split(/\s+/).filter(Boolean);
    if (tokens.length > 1 && (tokens[0] === 'fr' || tokens[0] === 'fra')) tokens = tokens.slice(1);
    return tokens.join(' ').replace(QUALITY_TAGS, ' ').split(/\s+/).filter(Boolean).join(' ');
  }
  const NUMBERED = /^\s*(\d{1,5})\s*[:=]\s*(.+)$/;  // « 4: Canal+ » = numéro de chaîne (comme sur le décodeur)
  const LIVES = Array.from({ length: 19 }, (_, i) => `Canal+ Live ${i + 1}`);
  // Liste « OTT » d'un fournisseur (catégorie « FRANCE HD | OTT »), dans son ordre, sans les séparateurs.
  const OTT = `TF1, France 2, France 3, France 4, France 5, M6, Arte, RTS Un, RTS Deux,
    Canal+, Canal+ Foot, Canal+ Sport, Canal+ Sport 360, Canal+ Box Office, Canal+ Grand Ecran, Canal+ Cinema(s), Canal+ Docs, Canal+ Kids,
    Cine+ OCS, Cine+ Frisson, Cine+ Emotion, Cine+ Family, Cine+ Festival, Cine+ Classic, Drive In Movie Channel, Action, TCM Cinema,
    Eurochannel, La Chaine de Noel, Studio Movie 1, Studio Movie 2, Studio Movie 3, Disney+ Marvels, Disney+ Star Wars,
    Canal Play 1, Canal Play 2, Canal Play 3, Canal Play 4, Canal Play 5, Canal Play 6, Canal Play 7, Canal Play 8, Canal Play 9, Canal Play 10,
    13eme Rue, Syfy, Warner TV, Polar+, Serie Club, TV Breizh, RTL9, AB1, Novelas TV, TF1 Series Films, France TV Series, Studio Comedy, Studio Crime,
    Comedie+, Paris Premiere, Teva, W9, TMC, TFX, CStar, T18, Novo19, 6ter, RMC Story, RMC Life, AB3, Carac 1, TV5 Monde, TV5 Monde Europe,
    Planete+, Planete+ Crime, Planete+ Aventure, National Geo, National Geo Wild, Discovery Channel, TLC, Discovery ID, RMC Decouverte, Museum,
    Ushuaia TV, Histoire, Toute l'Histoire, Seasons, Chasse et Peche, Science & Vie, Crime District, Animaux, Trek, L'Esprit Sorcier, France TV Docs, Imearth,
    Piwi+, Disney Junior, Nickelodeon Junior, Boomerang, Cartoonito, Tiji, Teletoon+, Disney Channel, Nickelodeon, Nickelodeon +1, Canal J,
    Nickelodeon Teen, Cartoon Network, Gulli, Dreamworks, Pitchoun TV, Pitchoun Kids Music, Baby TV, Duck TV,
    Disney+ Pixar [VOD], Les Simpson [VOD], Bob l'Eponge [VOD], Spirit : au galop en toute liberte [VOD], Miraculous [VOD], Masha et Michka [VOD],
    Peppa Pig, Lilo & Stitch [VOD], La Maison de Mickey [VOD], La Garde du Roi Lion [VOD], Elena d'Avalor [VOD], Dora l'exploratrice [VOD],
    Docteur la Peluche [VOD], South Park [VOD], La Pat Patrouille [VOD], A table T'choupi [VOD], T'choupi et Doudou [VOD], T'choupi et ses amis [VOD],
    MTV, MCM, Bravo, Comedy Central, Warner TV Next, Mangas, ADN, MGG TV, Ginx Esport, Game Toon, Lucky Jack, Gong, Gong Max, Sqool TV,
    La Chaine Meteo, CNews, CNews Prime, BFM, BFM 2, LCI, France Info, LCP-AN 24/24, Public Sénat, France 24, Euronews, BFM Business, Tech&Co,
    B-Smart, Le Media, I24 News, RT France, Bloomberg Europe, CNBC Europe, BBC News, CNN International,
    beIN Sports 1, beIN Sports 2, beIN Sports 3, RMC Sport 1, Infosport+, L'Equipe, Eurosport 1, Eurosport 2, Journal du Golf, Golf+, Automoto,
    Autoplus, Equidia, Cheval TV, OLTV, After Foot RMC, SportenFrance, Nautical Channel, Motorvision, Motorvision+, Padel Time TV`
    .split(',').map((p) => p.trim()).filter(Boolean);
  const PRESETS = [
    { name: 'OTT', match: 'exact', hint: 'Ta liste « OTT » (188 chaînes françaises) dans l\'ordre du fournisseur ; les autres versions de chaque chaîne (VIP, 4K, FHD…) deviennent ses secours',
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
        'Discovery Channel', 'Discovery', 'Discovery Investigation', 'National Geographic', 'Nat Geo Wild', 'Dogs & Cats',
        'Science et Vie TV', 'Animaux', 'Seasons', 'Trek', 'Museum TV', 'Crime District', 'Chasse et Pêche',
        'Piwi+', 'TiJi', 'Canal J', 'Disney Channel', 'Disney Junior', 'Nickelodeon', 'Nickelodeon Junior', 'Nick Jr',
        'Nickelodeon Teen', 'Boomerang', 'Cartoon Network', 'Cartoonito', 'Boing', 'Mangas', 'Game One',
        'CNews Prime', 'France 24', 'Euronews', 'BFM Business', 'BFM Paris', 'i24News', 'Europe 1 TV', 'Le Figaro TV', 'TV Monaco',
        'MCM', 'M6 Music', 'MTV Hits', 'NRJ Hits', 'CStar Hits', 'RFM TV', 'Trace Urban', 'Mezzo', 'Mezzo Live', 'Melody',
        'Clubbing TV', ...LIVES] },
    { name: 'TNT', match: 'exact', hint: 'Chaînes gratuites de la TNT, dans l\'ordre de leurs numéros',
      patterns: ['TF1', 'France 2', 'France 3', 'Canal+', 'France 5', 'M6', 'Arte', 'C8', 'W9', 'TMC', 'TFX', 'NRJ 12', 'NRJ12',
        'LCP', 'Public Sénat', 'France 4', 'BFM TV', 'BFMTV', 'CNews', 'CStar', 'C Star', 'Gulli', 'TF1 Séries Films',
        'L\'Équipe', 'La chaîne L\'Équipe', '6ter', 'RMC Story', 'RMC Découverte', 'Chérie 25', 'LCI', 'franceinfo',
        'France Info', 'T18', 'Novo19', 'Novo 19'] },
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
        const patterns = [...new Set((g.patterns || []).map((p) => matchName(String(p).replace(NUMBERED, (m, n, rest) => rest))).filter(Boolean))];
        return { ...g, _patterns: patterns, _exact: new Map(patterns.map((p, i) => [p, i]).reverse()) };
      });
  }

  function groupMatches(core, group) {
    if (group.match === 'exact') return group._exact.has(core);
    const padded = ` ${core} `;
    return group._patterns.some((p) => padded.includes(` ${p} `));
  }

  /** Chaînes retenues rangées dans un groupe perso : Map id → id du groupe. */
  function groupAssignment() {
    if (state.groupCache) return state.groupCache;
    const result = new Map();
    const groups = orderedCustomGroups();
    if (groups.length) {
      const known = new Set(groups.map((g) => g.id));
      const sel = state.sel.live;
      const separators = $('opt-separators').checked;
      for (const c of state.cat.live.channels) {
        if (sel.excluded.has(c.stream_id)) continue;
        if (!sel.included.has(c.stream_id) && !c.category_ids.some((id) => catOn('live', id))) continue;
        if (separators && isSeparator(c.name)) continue;
        const manual = state.itemGroups[c.stream_id];
        if (manual === '-') continue;
        if (manual && known.has(manual)) { result.set(c.stream_id, manual); continue; }
        if (c._core === undefined) c._core = matchName(c.name);
        const group = groups.find((g) => g._patterns.length && groupMatches(c._core, g));
        if (group) result.set(c.stream_id, group.id);
      }
    }
    state.groupCache = result;
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
  const displayName = (c, kind = state.kind) => state.itemNames[keyOf(c, kind)] || c.name;

  /** Chaînes du direct masquées comme doublons (option « Masquer les doublons »). */
  function hiddenDuplicates() {
    const dedupe = $('opt-dedupe').checked, separators = $('opt-separators').checked;
    if (!dedupe && !separators) return EMPTY_SET;
    if (state.hiddenCache) return state.hiddenCache;
    const sel = state.sel.live;
    const best = new Map();
    const kept = [];
    for (const c of state.cat.live.channels) {
      if (sel.excluded.has(c.stream_id)) continue;
      const group = c.category_ids.find((id) => catOn('live', id)) || (sel.included.has(c.stream_id) ? c.category_id : null);
      if (group == null) continue;
      if (separators && isSeparator(c.name)) { kept.push(c.stream_id); continue; }
      if (!dedupe) { best.set(`id\u0000${c.stream_id}`, [{ q: 0, id: c.stream_id, i: kept.length }]); kept.push(c.stream_id); continue; }
      const custom = groupAssignment().get(c.stream_id);
      if (c._core === undefined) c._core = matchName(c.name);
      const key = `${custom ? CG + custom : group}\u0000${c._core || baseName(c.name)}`;
      const q = qualityOf(c.name);
      if (!best.has(key)) best.set(key, []);
      best.get(key).push({ q, id: c.stream_id, i: kept.length });
      kept.push(c.stream_id);
    }
    // Même règle que le serveur (builder.dedupe) : meilleure qualité gardée, les autres = versions de secours.
    const keep = new Set();
    state.altMap = new Map();   // id gardé → [ids des versions de secours, meilleure d'abord]
    state.altOf = new Map();    // id masqué → { kept, rank }
    for (const versions of best.values()) {
      const ranked = versions.sort((a, b) => b.q - a.q || a.i - b.i).map((v) => v.id);
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
    data.hide_separators = $('opt-separators').checked;
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
    data.custom_groups = state.customGroups.map((g) => ({ id: g.id, name: String(g.name || '').trim() || g.id, match: g.match,
      patterns: [...new Set((g.patterns || []).map((p) => String(p).trim()).filter(Boolean))] }));
    const known = new Set(state.customGroups.map((g) => g.id));
    data.item_groups = sortedObject(Object.fromEntries(Object.entries(state.itemGroups).filter(([, v]) => v === '-' || known.has(v))));
    return data;
  }

  function cleanGroupNames() {
    const result = {};
    for (const id of Object.keys(state.groupNames).sort()) {
      if (id.startsWith(CG)) continue;  // les groupes perso portent leur propre nom
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
      if (!exc.has(c.stream_id) && (c.category_ids.some((id) => cats.has(id)) || inc.has(c.stream_id))) n += 1;
    }
    return n;
  }

  /** Nombre d'éléments retenus pour la sélection en cours, par type. */
  function selectionCount(kind) {
    const sel = state.sel[kind];
    let n = 0;
    for (const c of state.cat[kind].channels) {
      if (!sel.excluded.has(c.stream_id) && (c.category_ids.some((id) => catOn(kind, id)) || sel.included.has(c.stream_id))) n += 1;
    }
    return kind === 'live' ? n - hiddenDuplicates().size : n;
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
    if ($('opt-dedupe').checked) parts.push('Sans doublons');
    if ($('opt-separators').checked) parts.push('Sans séparateurs');
    if ($('opt-failover').checked && $('opt-dedupe').checked && $('opt-live-format').value !== 'hls') parts.push('Bascule auto');
    $('opt-failover-hint').classList.toggle('warn-text', $('opt-failover').checked && (!$('opt-dedupe').checked || $('opt-live-format').value === 'hls'));
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
      if (state.catFilter === 'on' && !catOn(state.kind, cat.category_id)) return false;
      if (state.catFilter === 'off' && catOn(state.kind, cat.category_id)) return false;
      return true;
    });
    const fragment = document.createDocumentFragment();
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
      fragment.append(h('label', { class: `cat${on ? ' on' : ''}${ruled ? ' ruled' : ''}` },
        box,
        h('span', { class: 'grow' }, h('span', { class: 'name', text: cat.name }),
          cat.provider ? h('span', { class: 'meta', text: `Fournisseur : ${cat.provider}` }) : null,
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
    const logo = state.itemLogos[keyOf(c, kind)] || c.logo;
    const box = h('span', { class: `thumb${kind === 'live' ? '' : ' poster'}`, 'aria-hidden': 'true', text: String(c.name).replace(/[^\p{L}\p{N}]/gu, '').slice(0, 2).toUpperCase() || 'TV' });
    if (logo) {
      const img = h('img', { alt: '', loading: 'lazy', decoding: 'async', referrerpolicy: 'no-referrer' });
      img.addEventListener('load', () => { box.textContent = ''; box.append(img); }, { once: true });
      img.addEventListener('error', () => img.remove(), { once: true });
      img.src = logo;
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
    const details = h('details', { class: 'alt-list' },
      h('summary', { text: `🔁 ${plural(ids.length, failover ? 'version de secours' : 'autre version masquée', failover ? 'versions de secours' : 'autres versions masquées')}` }),
      h('ol', null, ids.map((id) => {
        const other = byId.get(id);
        return h('li', { text: other ? `${other.name} · ${(other.category_ids || []).map((cid) => state.cat.live.categoryNames.get(cid) || cid).join(', ')}` : `Chaîne ${id}` });
      })),
      failover ? null : h('p', { class: 'hint', text: 'Active « Bascule automatique » (Options) pour que le serveur les utilise si la chaîne coupe.' }));
    // La ligne est un <label> : sans ça, un clic sur le résumé cocherait/décocherait la chaîne.
    details.querySelector('summary').addEventListener('click', (event) => { event.preventDefault(); details.open = !details.open; });
    details.addEventListener('click', (event) => { if (event.target.tagName !== 'SUMMARY') event.preventDefault(); });
    return details;
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
    if (c.epg) badges.push(h('span', { class: 'badge opt', text: 'EPG', title: 'Identifiant EPG disponible' }));
    if (c.archive) badges.push(h('span', { class: 'badge opt', text: `Replay ${c.archive} j`, title: 'Programmes passés disponibles (catch-up)' }));
    if (state.kind === 'live' && hiddenDuplicates().has(c.stream_id)) {
      const sep = $('opt-separators').checked && isSeparator(c.name);
      const alt = !sep && state.altOf && state.altOf.get(c.stream_id);
      const main = alt && state.cat.live.channelById.get(alt.kept);
      badges.push(h('span', { class: 'badge warn', text: sep ? 'Séparateur masqué' : alt && $('opt-failover').checked ? `Secours ${alt.rank}/${alt.total}` : 'Doublon masqué',
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
    return h('span', { class: 'item-actions' },
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

  function groupCounts() {
    const groups = new Map();
    for (const kind of KINDS) {
      const sel = state.sel[kind];
      for (const c of state.cat[kind].channels) {
        if (sel.excluded.has(c.stream_id) || (kind === 'live' && hiddenDuplicates().has(c.stream_id))) continue;
        const inCat = c.category_ids.find((id) => catOn(kind, id));
        if (!inCat && !sel.included.has(c.stream_id)) continue;
        const custom = kind === 'live' ? groupAssignment().get(c.stream_id) : null;
        const key = custom ? CG + custom : KIND[kind].prefix + (inCat || c.category_id);
        groups.set(key, (groups.get(key) || 0) + 1);
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
      gl.append(h('li', { class: 'group-row' },
        h('span', { class: 'pos', text: String(index + 1) }),
        group ? h('span', { class: 'kind-tag mine', text: '📁 Perso', title: 'Mon groupe (rempli par les noms de chaînes et les rangements 📁)' })
          : h('span', { class: `kind-tag ${info.kind}`, text: KIND[info.kind].label, title: `Groupe de ${KIND[info.kind].many}` }),
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

  async function createGroup(preset = null) {
    let name = preset ? preset.name : await ask({ title: 'Nouveau groupe', text: 'Il sera placé en premier dans la playlist. Tu pourras le remplir avec des noms de chaînes ou en rangeant des chaînes avec 📁.',
      okLabel: 'Créer', input: { label: 'Nom du groupe (ex. TNT, Mes chaînes, Canal+)', value: '' } });
    name = String(name || '').trim().slice(0, 100);
    if (!name) return null;
    const existing = state.customGroups.find((g) => norm(g.name) === norm(name));
    if (existing) { toast(`Le groupe « ${existing.name} » existe déjà.`, 'warn'); return existing; }
    if (state.customGroups.length >= 50) { toast('50 groupes maximum.', 'warn'); return null; }
    const base = slugify(name).replace(/[^a-z0-9_-]/g, '').slice(0, 36) || 'groupe';
    let id = base;
    for (let n = 2; state.customGroups.some((g) => g.id === id); n += 1) id = `${base}-${n}`;
    const group = { id, name, match: preset ? preset.match : 'words', patterns: preset ? [...preset.patterns] : [] };
    state.customGroups.push(group);
    setCustomOrder([id, ...orderedCustomGroups().map((g) => g.id).filter((g) => g !== id)]);
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
    for (const [sid, gid] of groupAssignment()) {
      if (!members.has(gid)) members.set(gid, []);
      const c = byId.get(sid);
      if (c && !hiddenDuplicates().has(sid)) {
        const n = backupsOf(sid).length;
        members.get(gid).push(displayName(c, 'live') + (n ? ` (+${n})` : ''));
      }
    }
    return members;
  }

  function previewText(names) {
    if (!state.cat.live.catalogLoaded) return 'Chargement du catalogue…';
    if (!names || !names.length) return 'Aucune chaîne retenue pour l\'instant (coche d\'abord les catégories FR, puis ajoute des noms ou range des chaînes avec 📁).';
    return `${plural(names.length, 'chaîne', 'chaînes')} : ${names.slice(0, 8).join(', ')}${names.length > 8 ? '…' : ''}`;
  }

  function renderCustomGroups() {
    const list = $('cg-list');
    list.replaceChildren();
    const ordered = orderedCustomGroups();
    const members = groupMembers();
    ordered.forEach((info, index) => {
      const group = state.customGroups.find((g) => g.id === info.id);
      const count = h('span', { class: 'num', text: String((members.get(group.id) || []).length) });
      const preview = h('p', { class: 'hint cg-preview', text: previewText(members.get(group.id)) });
      const update = () => {
        state.groupCache = null; state.hiddenCache = null;
        const names = groupMembers().get(group.id);
        preview.textContent = previewText(names);
        count.textContent = String((names || []).length);
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
        const ids = ordered.map((g) => g.id);
        const target = index + delta;
        if (target < 0 || target >= ids.length) return;
        [ids[index], ids[target]] = [ids[target], ids[index]];
        setCustomOrder(ids);
        refresh();
      };
      const manual = Object.values(state.itemGroups).filter((v) => v === group.id).length;
      list.append(h('div', { class: 'cg-card' },
        h('div', { class: 'cg-head' },
          h('span', { class: 'pos', text: String(index + 1) }),
          name, count,
          h('button', { type: 'button', class: 'icon-btn', text: '↑', title: 'Monter', 'aria-label': `Monter ${group.name}`, disabled: index === 0, onclick: () => move(-1) }),
          h('button', { type: 'button', class: 'icon-btn', text: '↓', title: 'Descendre', 'aria-label': `Descendre ${group.name}`, disabled: index === ordered.length - 1, onclick: () => move(1) }),
          h('button', { type: 'button', class: 'icon-btn danger', text: '×', title: 'Supprimer ce groupe', 'aria-label': `Supprimer ${group.name}`,
            onclick: async () => {
              if (!await ask({ title: `Supprimer le groupe « ${group.name} » ?`, text: 'Ses chaînes retournent dans leur groupe d\'origine (rien n\'est retiré de la playlist).', okLabel: 'Supprimer', danger: true })) return;
              state.customGroups = state.customGroups.filter((g) => g !== group);
              state.groupOrder = state.groupOrder.filter((k) => k !== CG + group.id);
              for (const [sid, gid] of Object.entries(state.itemGroups)) if (gid === group.id) delete state.itemGroups[sid];
              refresh();
            } })),
        h('div', { class: 'cg-body' }, match, patterns, preview,
          manual ? h('p', { class: 'hint', text: `${plural(manual, 'chaîne rangée', 'chaînes rangées')} à la main avec 📁.` }) : null)));
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
      ...Object.keys(state.itemLogos), ...Object.keys(state.itemEpg)]);
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
      custom.append(h('li', null,
        h('span', null, h('span', { class: `kind-tag ${kind}`, text: KIND[kind].label }), ' ', `${item ? item.name : key} → ${parts.join(' · ')}`),
        h('button', { type: 'button', class: 'x', text: '×', title: 'Annuler la personnalisation', onclick: () => {
          delete state.itemNames[key]; delete state.itemNumbers[key]; delete state.itemLogos[key]; delete state.itemEpg[key]; refresh();
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
    const matched = state.categories.filter((cat) => ruleMatches(cat.name, rule));
    $('rule-preview').textContent = matched.length
      ? `Aperçu : ${plural(matched.length, 'catégorie', 'catégories')} — ${matched.slice(0, 6).map((c) => c.name).join(', ')}${matched.length > 6 ? '…' : ''}`
      : 'Aperçu : aucune catégorie actuelle ne correspond (la règle s\'appliquera aux futures catégories).';
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
    state.hiddenCache = null;
    state.groupCache = null;
    renderStats();
    if (state.tab === 'categories' || channels === true) { renderCategories(); renderRules(); }
    if (channels === true) renderChannels();
    else if (channels === 'row') updateRow(row, channel);
    if (state.tab === 'summary' || state.section === 'organize') renderSummary();
    renderUrl();
    renderPlaylists();
  }

  // ------------------------------------------------- parties de l'éditeur
  const SECTIONS = ['content', 'organize', 'options', 'guide'];
  function selectSection(name) {
    state.section = name;
    for (const sec of SECTIONS) {
      const active = sec === name;
      $(`sec-btn-${sec}`).setAttribute('aria-selected', String(active));
      $(`sec-btn-${sec}`).tabIndex = active ? 0 : -1;
      $(`sec-${sec}`).hidden = !active;
    }
    if (name === 'organize') renderSummary();
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
    select.replaceChildren(...state.playlists.filter((p) => usesKind(p, 'live'))
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
        cur ? h('span', { class: 'g-bar', 'aria-hidden': 'true' }, h('span', { style: null, class: 'g-fill', 'data-pct': String(Math.round(pct)) })) : null,
        c.next ? h('span', { class: 'g-next', text: `Ensuite ${hhmm(c.next.start)} : ${c.next.title}` }) : null)));
    }
    for (const fill of list.querySelectorAll('.g-fill')) fill.style.width = `${fill.dataset.pct}%`;
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
      request('/api/streams').then((data) => Promise.all(data.streams.filter((s) => s.kind === 'hls' && s.item_id === id)
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
  async function loadHistory() {
    let data;
    try { data = await request('/api/history'); } catch (_) { return; }
    const fill = (id, rows, text, empty) => {
      const node = $(id);
      node.replaceChildren(...rows.map((r) => h('li', null, h('span', { text: r.label }), h('span', { class: 'num', text: text(r) }))));
      if (!rows.length) node.append(h('li', { class: 'empty', text: empty }));
    };
    fill('history-top', data.top, (r) => `${formatDuration(r.seconds)} · ${plural(r.count, 'fois', 'fois')}`, 'Aucune lecture enregistrée.');
    fill('history-unstable', data.unstable, (r) => `${plural(r.switches, 'bascule', 'bascules')}`, 'Aucune bascule : les chaînes tiennent bien.');
    fill('history-list', data.entries.slice(0, 50), (r) => `${new Date(r.started_at * 1000).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' })} · ${formatDuration(r.seconds)}${r.device ? ` · ${r.device}` : ''}${r.switches ? ` · ${plural(r.switches, 'bascule', 'bascules')}` : ''}`, 'Aucune lecture.');
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
    $('opt-separators').checked = Boolean(p && p.hide_separators);
    $('opt-fav-name').value = (p && p.favorites_name) || '★ Favoris';
    state.rules = p && p.rules ? p.rules.map((r) => ({ ...r })) : [];
    state.favorites = p && p.favorites ? [...p.favorites] : [];
    state.itemNames = p && p.item_names ? { ...p.item_names } : {};
    state.itemNumbers = p && p.item_numbers ? { ...p.item_numbers } : {};
    state.itemLogos = p && p.item_logos ? { ...p.item_logos } : {};
    state.itemEpg = p && p.item_epg ? { ...p.item_epg } : {};
    state.hiddenCache = null;
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
  const VIEWS = { playlists: '#', guide: '#guide', catalog: '#catalogue', devices: '#appareils', system: '#systeme' };

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
    if (view === 'guide') loadGuide();
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
      h('div', { class: 'device-settings' },
        h('div', { class: 'field' }, h('label', { text: 'Playlist de cet appareil' }), select),
        h('div', { class: 'field' }, h('label', { text: 'Priorité (limite de connexions)' }), prioritySelect(d)),
        h('div', { class: 'field' }, h('label', { text: 'Adresse IP de la TV (facultatif)' }), ipInput(d))),
      h('div', { class: 'device-grid' },
        h('div', { class: 'device-mode' },
          h('h4', { text: '📺 Mode « Playlist M3U » (VLC, TiviMate, Android TV…)' }),
          credRow('Playlist', m3u), credRow('Guide (EPG)', epg)),
        h('div', { class: 'device-mode' },
          h('h4', { text: '🔐 Mode « Xtream Codes » (TiviMate, IPTV Smarters…)' }),
          credRow('Serveur', server), credRow('Identifiant', d.username), credRow('Mot de passe', d.password, { secret: true }),
          h('p', { class: 'hint', text: 'Rubriques Direct / Films / Séries natives du lecteur, guide et replay inclus.' }))));
  }

  function prioritySelect(d) {
    const select = h('select', { 'aria-label': `Priorité de ${d.name}` },
      h('option', { value: 'high', text: 'Haute : jamais coupée par une autre TV', selected: d.priority === 'high' }),
      h('option', { value: 'normal', text: 'Normale', selected: !d.priority || d.priority === 'normal' }),
      h('option', { value: 'low', text: 'Basse : cède toujours sa place', selected: d.priority === 'low' }));
    select.addEventListener('change', async () => {
      try { await request(`/api/devices/${d.id}`, 'PUT', { priority: select.value }); toast('Priorité modifiée'); }
      catch (error) { toast(error.message, 'error'); loadDevices(); }
    });
    return select;
  }

  function ipInput(d) {
    const input = h('input', { type: 'text', value: d.ip || '', placeholder: d.last_ip ? `vue : ${d.last_ip}` : 'ex. 192.168.0.42', inputmode: 'decimal',
      'aria-label': `Adresse IP de ${d.name}`, title: 'Sert à reconnaître la TV même si sa playlist n\'utilise pas son code' });
    input.addEventListener('change', async () => {
      try { await request(`/api/devices/${d.id}`, 'PUT', { ip: input.value.trim() }); toast('Adresse enregistrée'); }
      catch (error) { toast(error.message, 'error'); input.value = d.ip || ''; }
    });
    return input;
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
    state.localStreams = n;
    renderAccountPill();
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
          h('span', { class: 's-meta', text: `${KIND_LABEL[s.kind] || s.kind} · ${s.device ? `${s.device}${s.priority === 'high' ? ' (prioritaire)' : s.priority === 'low' ? ' (priorité basse)' : ''}` : `appareil ${s.client}`} · depuis ${formatDuration(s.seconds)} · ${formatBytes(s.bytes)}${s.switches ? ` · ${plural(s.switches, 'bascule', 'bascules')}` : ''}` })),
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
    ruleCache.clear();
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
    const a = status && status.account;
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
    for (const p of (status && status.providers) || []) {
      const acc = p.account;
      if (!acc) { rows.push([p.name, 'injoignable']); continue; }
      const exp = acc.exp_date ? new Date(acc.exp_date * 1000).toLocaleDateString('fr-FR') : 'expiration inconnue';
      rows.push([p.name, `${acc.status || '?'} · expire le ${exp} · ${acc.active_connections || 0}/${acc.max_connections || '?'} connexion(s)`]);
    }
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
      lastDiagnostic = await request('/api/diagnostic', 'POST');
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
      load(state.playlists.length ? [...state.playlists].sort((a, b) => collator.compare(a.name, b.name))[0] : null);
    } catch (error) { toast(error.message, 'error'); }
  }

  async function loadAccount() {
    try {
      const status = await request('/api/status?account=1');
      renderAccountFacts(status);
      state.account = status.account;
      renderExpiry(state.account);
      renderAccountPill();
    } catch (_) { /* facultatif */ }
  }

  /** Badge de l'abonnement. Connexions : le compteur du fournisseur n'est lu qu'à l'ouverture de la page
   *  (et il est souvent en retard) ; on prend le plus grand avec les lectures relayées par ce serveur. */
  function renderAccountPill() {
    const a = state.account;
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
  $('opt-dedupe').addEventListener('change', () => { renderOptionsPill(); refresh(); });
  $('opt-failover').addEventListener('change', () => { renderOptionsPill(); refresh({ channels: false }); });
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
    if (!(await ask({ title: 'Effacer les personnalisations ?', text: 'Tous les noms et numéros personnalisés de cette playlist seront supprimés.', okLabel: 'Effacer', danger: true }))) return;
    state.itemNames = {}; state.itemNumbers = {}; refresh();
  });
  $('auto-number').addEventListener('click', async () => {
    const groups = groupCounts();
    const order = orderedGroups(groups).filter((key) => groupInfo(key).kind === 'live');
    const sel = state.sel.live;
    const hidden = hiddenDuplicates();
    const byGroup = new Map(order.map((g) => [g, []]));
    for (const c of state.cat.live.channels) {
      if (sel.excluded.has(c.stream_id) || hidden.has(c.stream_id)) continue;
      const g = c.category_ids.find((id) => catOn('live', id)) || (sel.included.has(c.stream_id) ? c.category_id : null);
      if (g != null && byGroup.has(g)) byGroup.get(g).push(c);
    }
    const ordered = [];
    for (const g of order) {
      const list = byGroup.get(g);
      list.sort((a, b) => (state.itemNumbers[a.stream_id] || 1e9) - (state.itemNumbers[b.stream_id] || 1e9));
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
  $('view-btn-playlists').addEventListener('click', () => switchView('playlists'));
  $('view-btn-guide').addEventListener('click', () => switchView('guide'));
  $('view-btn-catalog').addEventListener('click', () => switchView('catalog'));
  $('view-btn-system').addEventListener('click', () => switchView('system'));
  $('view-btn-devices').addEventListener('click', () => switchView('devices'));
  $('device-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = $('device-name').value.trim();
    if (!name) { $('device-name').focus(); return; }
    try {
      const device = await request('/api/devices', 'POST', { name, playlist: $('device-playlist').value, priority: $('device-priority').value });
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
  $('cg-add').addEventListener('click', () => createGroup());
  for (const preset of PRESETS) {
    $('cg-presets').append(h('button', { type: 'button', class: 'btn small', text: `+ ${preset.name}`, title: preset.hint,
      onclick: () => createGroup(preset) }));
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
  for (const sec of SECTIONS) $(`sec-btn-${sec}`).addEventListener('click', () => selectSection(sec));
  document.querySelector('.section-nav').addEventListener('keydown', (event) => {
    const index = SECTIONS.indexOf(state.section);
    const next = { ArrowRight: SECTIONS[(index + 1) % SECTIONS.length], ArrowLeft: SECTIONS[(index + SECTIONS.length - 1) % SECTIONS.length] }[event.key];
    if (next) { event.preventDefault(); selectSection(next); $(`sec-btn-${next}`).focus(); }
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
  $('history-clear').addEventListener('click', async () => {
    if (!(await ask({ title: 'Effacer l\'historique des lectures ?', okLabel: 'Effacer', danger: true }))) return;
    try { await request('/api/history/clear', 'POST'); loadHistory(); } catch (error) { toast(error.message, 'error'); }
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
