"""Plusieurs abonnements Xtream fusionnés en un seul catalogue.

Configuration : le fournisseur principal (``XTREAM_URL``…) plus jusqu'à 8 autres
(``XTREAM_URL_2``, ``XTREAM_USERNAME_2``, ``XTREAM_PASSWORD_2``, ``XTREAM_NAME_2`` … ``_9``).

Identifiants : ceux du fournisseur 1 sont inchangés (compatibilité). Ceux du fournisseur *k* ≥ 2
sont décalés : ``k × 10¹¹ + id`` (chaînes, films, séries, épisodes, catégories numériques). Ils restent
numériques (exigé par certains lecteurs Xtream) et chaque flux est relayé vers le bon fournisseur.
Les catégories de même nom se retrouvent dans le même groupe dans les lecteurs.
"""
from __future__ import annotations

import copy
import logging
import threading
from types import SimpleNamespace

LOG = logging.getLogger("xtream-m3u-local.providers")
OFFSET = 10 ** 11


class UnknownProvider(ValueError):
    """Identifiant qui désigne un fournisseur non configuré."""


def encode(index: int, raw: object) -> str:
    raw = str(raw).strip()
    if index <= 1:
        return raw
    if raw.isdigit() and int(raw) < OFFSET:
        return str(index * OFFSET + int(raw))
    return f"p{index}.{raw}"


# Identifiants « courts » pour les lecteurs Xtream (TiviMate, applis Android) : beaucoup lisent les
# identifiants comme des entiers 32 bits (max 2 147 483 647) ; ``k × 10¹¹ + id`` déborde et la liste est
# refusée (« erreur lors du traitement de la liste de lecture »). Abonnement k (2 à 9) :
# 1 000 000 000 + (k − 2) × 100 000 000 + id, pour id < 100 000 000 (sinon identifiant interne inchangé).
PUBLIC_BASE = 10 ** 9
PUBLIC_STEP = 10 ** 8


def to_public(value: object) -> str:
    """Identifiant interne → identifiant court donné aux lecteurs Xtream."""
    text = str(value)
    index, raw = decode(text)
    if index >= 2 and raw.isdigit() and int(raw) < PUBLIC_STEP:
        return str(PUBLIC_BASE + (index - 2) * PUBLIC_STEP + int(raw))
    return text


def from_public(value: object) -> str:
    """Identifiant court reçu d'un lecteur Xtream → identifiant interne (les identifiants internes passent tels quels)."""
    text = str(value)
    if text.isdigit():
        number = int(text)
        if PUBLIC_BASE <= number < PUBLIC_BASE + 8 * PUBLIC_STEP:
            offset = number - PUBLIC_BASE
            return encode(2 + offset // PUBLIC_STEP, offset % PUBLIC_STEP)
    return text


def decode(value: object) -> tuple[int, str]:
    text = str(value).strip()
    if text.isdigit():
        number = int(text)
        index = number // OFFSET
        if 2 <= index <= 9:
            return index, str(number % OFFSET)
        return 1, text
    if text.startswith("p") and "." in text[:4]:
        head, _, raw = text.partition(".")
        if head[1:].isdigit():
            return int(head[1:]), raw
    return 1, text


def _encode_item(item: dict, index: int) -> dict:
    copy_ = dict(item)
    copy_["stream_id"] = encode(index, item["stream_id"])
    copy_["category_ids"] = [c if c == "none" else encode(index, c) for c in item["category_ids"]]
    copy_["category_id"] = copy_["category_ids"][0]
    copy_["provider"] = index
    return copy_


class ProviderSet:
    """Même interface que :class:`app.server.XtreamAPI`, sur plusieurs fournisseurs."""

    def __init__(self, apis: list, names: list[str], indices: list[int] | None = None):
        self.apis = apis
        self.names = names
        # Numéro de chaque abonnement (fixe : il est inscrit dans les identifiants des chaînes et les playlists).
        self.indices = list(indices or range(1, len(apis) + 1))
        self._by_index = dict(zip(self.indices, apis))
        self.config = apis[0].config
        self._lock = threading.Lock()
        self._merged: dict[str, tuple[tuple, SimpleNamespace]] = {}

    # ------------------------------------------------------------- routage
    def _route(self, item_id: object):
        index, raw = decode(item_id)
        if index not in self._by_index:
            raise UnknownProvider("Identifiant de fournisseur inconnu")
        return self._by_index[index], raw, index

    def provider_of(self, item_id: object) -> str:
        return str(decode(item_id)[0])

    def direct_url(self, kind: str, item_id: str, extension: str) -> str:
        api, raw, _ = self._route(item_id)
        return api.direct_url(kind, raw, extension)

    def user_agent_for_url(self, url: str) -> str:
        """User agent de l'abonnement dont l'adresse sert ``url`` (sinon celui du principal)."""
        for api in self.apis:
            base = api.config.xtream_url.rstrip("/")
            if url == base or url.startswith(base + "/"):
                return api.config.user_agent
        return self.apis[0].config.user_agent

    def direct_stream(self, stream_id: str, extension: str = "ts") -> str:
        return self.direct_url("live", stream_id, extension)

    def timeshift_url(self, stream_id: str, start_epoch: int, minutes: int) -> str:
        api, raw, _ = self._route(stream_id)
        return api.timeshift_url(raw, start_epoch, minutes)

    def timeshift_raw_url(self, stream_id: str, minutes: int, start: str) -> str:
        api, raw, _ = self._route(stream_id)
        return api.timeshift_raw_url(raw, minutes, start)

    def simple_epg(self, stream_id: str) -> object:
        api, raw, _ = self._route(stream_id)
        return api.simple_epg(raw)

    def series_info(self, series_id: str) -> object:
        api, raw, index = self._route(series_id)
        return self._encode_series_info(api.series_info(raw), index)

    @staticmethod
    def _encode_series_info(data: object, index: int) -> object:
        if index <= 1 or not isinstance(data, dict):
            return data
        data = copy.deepcopy(data)
        episodes = data.get("episodes")
        groups = episodes.values() if isinstance(episodes, dict) else episodes if isinstance(episodes, list) else []
        for group in groups:
            for ep in group if isinstance(group, list) else [group]:
                if isinstance(ep, dict) and ep.get("id") is not None:
                    ep["id"] = encode(index, ep["id"])
        return data

    def raw_action(self, action: str, **params: str) -> object:
        target = next((params[k] for k in ("series_id", "vod_id", "stream_id") if k in params), None)
        api, index = self.apis[0], 1
        if target is not None:
            api, raw, index = self._route(target)
            params = {k: (raw if k in ("series_id", "vod_id", "stream_id") else v) for k, v in params.items()}
        data = api.raw_action(action, **params)
        if action == "get_series_info":
            return self._encode_series_info(data, index)
        if action == "get_vod_info" and index > 1 and isinstance(data, dict):
            data = copy.deepcopy(data)
            movie = data.get("movie_data")
            if isinstance(movie, dict) and movie.get("stream_id") is not None:
                movie["stream_id"] = int(encode(index, movie["stream_id"])) if str(movie["stream_id"]).isdigit() else movie["stream_id"]
        return data

    # ---------------------------------------------- comptes et paramètres
    def epg_url(self) -> str:
        return self.apis[0].epg_url()

    def epg_urls(self) -> dict[str, tuple[str, str, str]]:
        """``{index: (url_xmltv, nom, user_agent)}`` pour chaque abonnement configuré."""
        return {str(index): (api.epg_url(), name, api.config.user_agent)
                for index, name, api in zip(self.indices, self.names, self.apis)}

    def api_url(self, action: str | None = None, **extra: str) -> str:
        return self.apis[0].api_url(action, **extra)

    def _fetch(self, url: str, label: str):
        return self.apis[0]._fetch(url, label)

    def account(self) -> dict | None:
        return self.apis[0].account()

    def accounts(self) -> list[dict]:
        return [{"index": index, "name": name, "account": api.account()}
                for index, name, api in zip(self.indices, self.names, self.apis)]

    def max_connections_hint(self, provider: str = "1") -> int:
        index = int(provider) if str(provider).isdigit() else 1
        api = self._by_index.get(index, self.apis[0])
        return api.max_connections_hint()

    def provider_timezone(self):
        return self.apis[0].provider_timezone()

    # ----------------------------------------------------------- catalogue
    def catalog(self, *, refresh: bool = False, kind: str = "live") -> tuple[list[dict], dict[str, str]]:
        errors = []
        for api in self.apis:
            try:
                api.catalog(refresh=refresh, kind=kind)
            except Exception as exc:  # un fournisseur en panne ne bloque pas les autres
                errors.append(exc)
        if len(errors) == len(self.apis):
            raise errors[-1]
        merged = self.kind_state(kind)
        return merged.items, merged.categories

    def kind_state(self, kind: str = "live") -> SimpleNamespace:
        states = [api.kind_state(kind) for api in self.apis]
        key = tuple(id(st.items) for st in states)
        with self._lock:
            cached = self._merged.get(kind)
            if cached and cached[0] == key:
                return cached[1]
            items: list[dict] = []
            categories: dict[str, str] = {}
            meta: dict[str, dict] = {}
            raw_by_id: dict[str, dict] = {}
            raw_categories: list = []
            raw_streams: list = []
            errors = []
            for index, st, name in zip(self.indices, states, self.names):
                if st.categories_error:
                    errors.append(f"{name} : {st.categories_error}")
                if st.items is None:
                    if st.last_error:
                        errors.append(f"{name} : {st.last_error}")
                    continue
                for cid, cname in st.categories.items():
                    key_cid = cid if cid == "none" else encode(index, cid)
                    if key_cid not in categories:
                        categories[key_cid] = cname
                        info = dict(st.category_meta.get(cid, {}))
                        if info.get("parent_id"):
                            info["parent_id"] = encode(index, info["parent_id"])
                        info["provider"] = name
                        meta[key_cid] = info
                for item in st.items:
                    items.append(item if index == 1 else _encode_item(item, index))
                for raw_id, raw in st.raw_by_id.items():
                    raw_by_id[encode(index, raw_id)] = raw
                tag = (lambda e, n=name: {**e, "_provider": n} if isinstance(e, dict) else e) if index > 1 else (lambda e: e)
                raw_categories.extend(tag(e) for e in st.raw_categories)
                raw_streams.extend(tag(e) for e in st.raw_streams)
            for position, cid in enumerate(meta):
                meta[cid]["position"] = position
            merged = SimpleNamespace(
                items=items if any(st.items is not None for st in states) else None,
                by_id={item["stream_id"]: item for item in items}, raw_by_id=raw_by_id,
                categories=categories, category_meta=meta, raw_categories=raw_categories, raw_streams=raw_streams,
                categories_error=" ; ".join(errors) or None,
                updated_at=max((st.updated_at or 0 for st in states), default=0) or None,
                last_error=states[0].last_error)
            self._merged[kind] = (key, merged)
            return merged

    def lookup(self, kind: str, item_id: str) -> dict | None:
        return self.kind_state(kind).by_id.get(item_id)

    # Attributs historiques (catalogue du direct).
    category_meta = property(lambda self: self.kind_state("live").category_meta)
    raw_categories = property(lambda self: self.kind_state("live").raw_categories)
    raw_streams = property(lambda self: self.kind_state("live").raw_streams)
    categories_error = property(lambda self: self.kind_state("live").categories_error)
    updated_at = property(lambda self: self.kind_state("live").updated_at)
    last_error = property(lambda self: self.apis[0].last_error)


class ApiHolder:
    """Client Xtream remplaçable à chaud (abonnements ajoutés ou modifiés depuis l'interface).

    Tous les composants gardent cette même référence ; seul le client réel (un abonnement ou un
    :class:`ProviderSet`) change. Les attributs sont lus sur le client courant à chaque accès.
    """

    def __init__(self, api) -> None:
        object.__setattr__(self, "_current", api)

    @property
    def current(self):
        return self._current

    def swap(self, api) -> None:
        object.__setattr__(self, "_current", api)

    def __getattr__(self, name: str):
        return getattr(self._current, name)
