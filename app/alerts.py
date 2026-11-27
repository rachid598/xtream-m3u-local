"""Alertes automatiques Telegram avec couplage par QR code local (100 % bibliothèque standard).

Identifiants stockés dans ``DATA_DIR/alerts.json`` (droits 0600), jamais exposés en clair ni exportés
dans les sauvegardes. Le couplage utilise un lien officiel ``https://t.me/<bot>?start=<code>`` encodé
en QR code SVG local et récupère automatiquement le ``chat_id`` via ``getUpdates`` (aucun port à ouvrir).
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import qrcode_svg

LOG = logging.getLogger("xtream-m3u-local.alerts")
DEFAULT_API_BASE = "https://api.telegram.org"
TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{20,80}$")
CHAT_ID_RE = re.compile(r"^-?\d{4,20}$")
PAIRING_TTL = 300          # 5 min pour scanner le QR code et appuyer sur « Démarrer »
WATCHDOG_INTERVAL = 300    # vérifications légères (tunnel) toutes les 5 min ; expiration toutes les 6 h
EXPIRY_CHECK_EVERY = 6 * 3600
DEFAULT_EVENTS = {
    "expiry": True, "provider": True, "tunnel": True, "security": True,
    "unstable": True, "weekly": False,
}


class AlertError(Exception):
    """Erreur de configuration ou d'envoi Telegram (message lisible par l'utilisateur)."""


def mask_token(token: str) -> str:
    if not token:
        return ""
    if ":" in token:
        head, tail = token.split(":", 1)
        return f"{head}:•••{tail[-4:]}" if len(tail) > 4 else f"{head}:•••"
    return f"•••{token[-4:]}" if len(token) > 4 else "•••"


class AlertManager:
    def __init__(self, data_dir: str, *, api_base: str = DEFAULT_API_BASE,
                 poll_interval: float = 2.5, pairing_ttl: float = PAIRING_TTL) -> None:
        self.path = Path(data_dir) / "alerts.json"
        self.api_base = api_base.rstrip("/")
        self._poll_interval = poll_interval
        self._pairing_ttl = pairing_ttl
        self._lock = threading.Lock()
        self._state = self._load()
        self._pairing: dict | None = None
        self._stop = threading.Event()
        self._cooldowns: dict[str, float] = {}
        self._last_expiry_check = 0.0
        self._last_tunnel_state = ""

    # ---------------------------------------------------------- persistance
    def _empty(self) -> dict:
        return {
            "enabled": False, "bot_token": "", "bot_username": "",
            "chat_id": "", "chat_label": "",
            "events": dict(DEFAULT_EVENTS), "sent": {},
            "last_sent_at": 0, "last_error": "",
        }

    def _load(self) -> dict:
        base = self._empty()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return base
        if not isinstance(raw, dict):
            return base
        base["enabled"] = bool(raw.get("enabled"))
        base["bot_token"] = str(raw.get("bot_token") or "").strip()
        base["bot_username"] = str(raw.get("bot_username") or "").strip()
        base["chat_id"] = str(raw.get("chat_id") or "").strip()
        base["chat_label"] = str(raw.get("chat_label") or "").strip()
        ev = raw.get("events") if isinstance(raw.get("events"), dict) else {}
        base["events"] = {k: bool(ev.get(k, default)) for k, default in DEFAULT_EVENTS.items()}
        sent = raw.get("sent") if isinstance(raw.get("sent"), dict) else {}
        base["sent"] = {str(k): float(v) for k, v in list(sent.items())[-200:] if isinstance(v, (int, float))}
        base["last_sent_at"] = int(raw.get("last_sent_at") or 0)
        base["last_error"] = str(raw.get("last_error") or "")
        return base

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".alerts-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def secrets(self) -> list[str]:
        """Jeton Telegram à masquer dans les journaux et exports."""
        with self._lock:
            tok = self._state.get("bot_token") or ""
            pair_tok = (self._pairing or {}).get("bot_token") or ""
        return [s for s in dict.fromkeys((tok, pair_tok)) if s]

    # ---------------------------------------------------------- API Telegram
    def _tg_call(self, token: str, method: str, payload: dict | None = None, *, timeout: float = 12) -> dict:
        url = f"{self.api_base}/bot{token}/{method}"
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = ""
            try:
                err_json = json.loads(exc.read().decode("utf-8"))
                detail = str(err_json.get("description") or "")
            except Exception:
                pass
            if exc.code in (401, 404):
                raise AlertError("Token Telegram refusé : vérifie le token copié depuis @BotFather") from exc
            raise AlertError(f"Telegram a répondu HTTP {exc.code}{f' ({detail})' if detail else ''}") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise AlertError("Impossible de joindre api.telegram.org (accès Internet du conteneur ?)") from exc
        if not isinstance(body, dict) or not body.get("ok"):
            raise AlertError(str((body or {}).get("description") or "Réponse Telegram invalide"))
        return body

    def _validate_token(self, token: str) -> str:
        tok = str(token or "").strip()
        if not TOKEN_RE.fullmatch(tok):
            raise AlertError("Format de token Telegram invalide (exemple : 123456789:AAH…)")
        return tok

    # ------------------------------------------------- couplage par QR code
    def start_pairing(self, bot_token: str) -> dict:
        """Vérifie le bot Telegram, génère un QR code SVG local et attend le clic « Démarrer »."""
        tok = self._validate_token(bot_token)
        me = self._tg_call(tok, "getMe").get("result") or {}
        username = str(me.get("username") or "").strip()
        if not username:
            raise AlertError("Nom d'utilisateur du bot introuvable chez Telegram")
        # Sauter les anciens messages pour ne capter que le nouveau /start.
        offset = 0
        try:
            prev = self._tg_call(tok, "getUpdates?offset=-1&limit=1", timeout=8).get("result") or []
            if prev:
                offset = int(prev[-1]["update_id"]) + 1
        except (AlertError, KeyError, ValueError, TypeError):
            offset = 0
        code = "xtm_" + secrets.token_hex(6)
        deep_link = f"https://t.me/{username}?start={code}"
        qr = qrcode_svg.qr_svg(deep_link)
        expires_at = time.time() + self._pairing_ttl
        pair_id = secrets.token_hex(4)
        with self._lock:
            self._pairing = {
                "id": pair_id, "bot_token": tok, "bot_username": username,
                "code": code, "deep_link": deep_link, "qr_svg": qr,
                "offset": offset, "expires_at": expires_at, "status": "waiting", "error": "",
            }
        threading.Thread(target=self._poll_pairing, args=(pair_id,), name="telegram-pair", daemon=True).start()
        return self.status()

    def _poll_pairing(self, pair_id: str) -> None:
        while not self._stop.is_set():
            with self._lock:
                pair = self._pairing
                if pair is None or pair["id"] != pair_id or pair["status"] != "waiting":
                    return
                if time.time() >= pair["expires_at"]:
                    pair["status"] = "expired"
                    return
                tok, code, offset = pair["bot_token"], pair["code"], pair["offset"]
                username = pair["bot_username"]
            try:
                query = urlencode({"offset": offset, "limit": 20, "timeout": 0})
                updates = self._tg_call(tok, f"getUpdates?{query}", timeout=10).get("result") or []
            except AlertError as exc:
                with self._lock:
                    if self._pairing and self._pairing["id"] == pair_id:
                        self._pairing["error"] = str(exc)
                self._stop.wait(self._poll_interval)
                continue
            matched = None
            new_offset = offset
            for upd in updates:
                uid = int(upd.get("update_id") or 0)
                if uid >= new_offset:
                    new_offset = uid + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                text = str(msg.get("text") or "").strip()
                chat = msg.get("chat") or {}
                cid = str(chat.get("id") or "")
                if not cid:
                    continue
                if text == f"/start {code}" or text.startswith(f"/start {code} ") or text == "/start":
                    label = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p).strip()
                    if not label:
                        label = f"@{chat['username']}" if chat.get("username") else f"Chat {cid}"
                    matched = (cid, label)
                    break
            with self._lock:
                pair = self._pairing
                if pair is None or pair["id"] != pair_id:
                    return
                pair["offset"] = new_offset
                if matched:
                    cid, label = matched
                    self._state.update({
                        "enabled": True, "bot_token": tok, "bot_username": username,
                        "chat_id": cid, "chat_label": label, "last_error": "",
                    })
                    self._save_locked()
                    self._pairing = None
            if matched:
                LOG.info("Alertes Telegram connectées (%s, @%s)", matched[1], username)
                try:
                    self.send_message(
                        "✅ Xtream M3U Local connecté !\n"
                        "Tu recevras ici les alertes automatiques (expiration d'abonnement, blocage fournisseur, tunnel, sécurité)."
                    )
                except AlertError:
                    pass
                return
            self._stop.wait(self._poll_interval)

    def cancel_pairing(self) -> dict:
        with self._lock:
            self._pairing = None
        return self.status()

    def connect_manual(self, bot_token: str, chat_id: str) -> dict:
        """Connexion directe (repli avancé sans QR code)."""
        tok = self._validate_token(bot_token)
        cid = str(chat_id or "").strip()
        if not CHAT_ID_RE.fullmatch(cid):
            raise AlertError("Identifiant de discussion (chat_id) invalide (nombre attendu)")
        me = self._tg_call(tok, "getMe").get("result") or {}
        username = str(me.get("username") or "").strip()
        with self._lock:
            self._pairing = None
            self._state.update({
                "enabled": True, "bot_token": tok, "bot_username": username,
                "chat_id": cid, "chat_label": f"Chat {cid}", "last_error": "",
            })
            self._save_locked()
        self.send_message("✅ Xtream M3U Local connecté manuellement !")
        return self.status()

    def update_options(self, *, enabled: bool | None = None, events: dict | None = None) -> dict:
        with self._lock:
            if enabled is not None:
                self._state["enabled"] = bool(enabled)
            if isinstance(events, dict):
                self._state["events"] = {
                    k: bool(events.get(k, self._state["events"].get(k, default)))
                    for k, default in DEFAULT_EVENTS.items()
                }
            self._save_locked()
        return self.status()

    def disconnect(self) -> dict:
        with self._lock:
            self._pairing = None
            self._state = self._empty()
            self._save_locked()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            st = self._state
            connected = bool(st["bot_token"] and st["chat_id"])
            pair = None
            if self._pairing is not None:
                remaining = max(0, int(self._pairing["expires_at"] - time.time()))
                if remaining == 0 and self._pairing["status"] == "waiting":
                    self._pairing["status"] = "expired"
                pair = {
                    "status": self._pairing["status"],
                    "bot_username": self._pairing["bot_username"],
                    "deep_link": self._pairing["deep_link"],
                    "qr_svg": self._pairing["qr_svg"],
                    "expires_in": remaining,
                    "error": self._pairing.get("error", ""),
                }
            return {
                "connected": connected,
                "enabled": bool(st["enabled"]) and connected,
                "bot_username": st["bot_username"],
                "bot_token_masked": mask_token(st["bot_token"]),
                "chat_label": st["chat_label"],
                "events": dict(st["events"]),
                "last_sent_at": st["last_sent_at"],
                "last_error": st["last_error"],
                "pairing": pair,
            }

    # --------------------------------------------------------- envoi alertes
    def send_message(self, text: str) -> None:
        with self._lock:
            tok, cid = self._state.get("bot_token") or "", self._state.get("chat_id") or ""
        if not tok or not cid:
            raise AlertError("Telegram n'est pas encore connecté")
        try:
            self._tg_call(tok, "sendMessage", {
                "chat_id": cid, "text": text[:3800], "disable_web_page_preview": True,
            })
        except AlertError as exc:
            with self._lock:
                self._state["last_error"] = str(exc)
                self._save_locked()
            raise
        with self._lock:
            self._state["last_sent_at"] = int(time.time())
            self._state["last_error"] = ""
            self._save_locked()

    def send_test(self) -> dict:
        self.send_message("🔔 Test Xtream M3U Local : les alertes Telegram fonctionnent correctement !")
        return self.status()

    def notify(self, category: str, dedupe_key: str, text: str, *, cooldown: float = 3600,
               persist: bool = False) -> bool:
        """Envoie une alerte en arrière-plan si Telegram est actif, l'événement coché et hors délai anti-spam."""
        with self._lock:
            if not self._state["enabled"] or not self._state["bot_token"] or not self._state["chat_id"]:
                return False
            if category in DEFAULT_EVENTS and not self._state["events"].get(category, True):
                return False
            now_mono = time.monotonic()
            now_wall = time.time()
            if persist and dedupe_key in self._state["sent"]:
                if cooldown <= 0 or (now_wall - self._state["sent"][dedupe_key]) < cooldown:
                    return False
            if not persist and self._cooldowns.get(dedupe_key, 0.0) > now_mono:
                return False
            if persist:
                self._state["sent"][dedupe_key] = now_wall
                self._save_locked()
            else:
                self._cooldowns[dedupe_key] = now_mono + cooldown

        def _send() -> None:
            try:
                self.send_message(text)
            except AlertError as exc:
                LOG.warning("Alerte Telegram non envoyée (%s) : %s", category, exc)

        threading.Thread(target=_send, name=f"tg-alert-{category}", daemon=True).start()
        return True

    # ----------------------------------------------------------- surveillance
    def check_expiries(self, api) -> int:
        """Vérifie les dates d'expiration de tous les abonnements (paliers J-7, J-2, expiré)."""
        if api is None:
            return 0
        accounts_fn = getattr(api, "accounts", None)
        entries = accounts_fn() if accounts_fn is not None else [{"index": 1, "name": "Mon abonnement", "account": api.account()}]
        now = time.time()
        sent = 0
        for row in entries:
            acct = row.get("account") or {}
            raw_exp = acct.get("exp_date")
            try:
                exp = int(str(raw_exp))
            except (TypeError, ValueError):
                continue
            if exp <= 0:
                continue
            days = (exp - now) / 86400
            name = row.get("name") or f"Abonnement {row.get('index', 1)}"
            idx = row.get("index", 1)
            date_str = time.strftime("%d/%m/%Y", time.localtime(exp))
            if days <= 0:
                tier, msg = "expired", f"🚨 Abonnement « {name} » expiré (échéance : {date_str})."
            elif days <= 2:
                tier, msg = "2d", f"⚠️ Abonnement « {name} » expire dans moins de 48 h (le {date_str})."
            elif days <= 7:
                tier, msg = "7d", f"📅 Abonnement « {name} » expire dans {max(1, int(days))} jour(s) (le {date_str})."
            else:
                continue
            key = f"expiry:{idx}:{exp}:{tier}"
            if self.notify("expiry", key, msg, cooldown=0, persist=True):
                sent += 1
        return sent

    def check_tunnel(self, tunnel_mgr) -> bool:
        if tunnel_mgr is None:
            return False
        st = tunnel_mgr.status()
        state = str(st.get("state") or "")
        host = st.get("hostname") or ""
        prev = self._last_tunnel_state
        self._last_tunnel_state = state
        if prev == "connected" and state in ("error", "stopped") and host:
            return self.notify(
                "tunnel", f"tunnel:{host}",
                f"🌐 Tunnel Cloudflare ({host}) déconnecté (état : {state}).",
                cooldown=3600,
            )
        return False

    def build_weekly_summary(self, api, streams=None) -> str:
        """Construit un récapitulatif lisible des 7 derniers jours (temps, top chaînes, bascules, expirations)."""
        now = time.time()
        entries = streams.history() if streams is not None else []
        recent = [h for h in entries if now - h.get("started_at", 0) <= 7 * 86400]
        total_sec = sum(int(h.get("seconds", 0) or 0) for h in recent)
        total_switches = sum(int(h.get("switches", 0) or 0) for h in recent)

        def fmt_dur(sec: int) -> str:
            h, rem = divmod(max(0, sec), 3600)
            m = rem // 60
            return f"{h} h {m:02d}" if h else f"{m} min"

        lines = [f"📊 Récapitulatif Xtream M3U Local (7 derniers jours)"]
        lines.append(f"• {len(recent)} lecture(s) · {fmt_dur(total_sec)} cumulées · {total_switches} bascule(s)")

        by_dev: dict[str, int] = {}
        by_ch: dict[str, int] = {}
        unstable: dict[str, int] = {}
        for h in recent:
            dev = str(h.get("device") or "").strip() or (f"IP {h.get('client')}" if h.get("client") else "Sans code")
            secs = int(h.get("seconds", 0) or 0)
            by_dev[dev] = by_dev.get(dev, 0) + secs
            lbl = h.get("label") or h.get("item_id") or "?"
            if secs >= 20:
                by_ch[lbl] = by_ch.get(lbl, 0) + secs
            if h.get("switches"):
                unstable[lbl] = unstable.get(lbl, 0) + int(h["switches"])

        if by_dev:
            top_devs = sorted(by_dev.items(), key=lambda kv: -kv[1])[:4]
            lines.append("👤 Par personne : " + " · ".join(f"{k} ({fmt_dur(v)})" for k, v in top_devs))
        if by_ch:
            top_chs = sorted(by_ch.items(), key=lambda kv: -kv[1])[:3]
            lines.append("📺 Top chaînes : " + " · ".join(f"{k} ({fmt_dur(v)})" for k, v in top_chs))
        if unstable:
            top_un = sorted(unstable.items(), key=lambda kv: -kv[1])[:3]
            lines.append("🔁 Plus de bascules : " + " · ".join(f"{k} ({v})" for k, v in top_un))

        if api is not None:
            try:
                accounts_fn = getattr(api, "accounts", None)
                rows = accounts_fn() if accounts_fn is not None else [{"index": 1, "name": "Abonnement 1", "account": api.account()}]
                exp_bits = []
                for r in rows:
                    raw_exp = (r.get("account") or {}).get("exp_date")
                    try:
                        exp = int(str(raw_exp))
                    except (TypeError, ValueError):
                        continue
                    if exp > 0:
                        days_left = int((exp - now) / 86400)
                        name = r.get("name") or f"Abo {r.get('index', 1)}"
                        exp_bits.append(f"{name} ({'expiré' if days_left <= 0 else f'J-{days_left}'})")
                if exp_bits:
                    lines.append("🔑 Abonnements : " + " · ".join(exp_bits))
            except Exception:
                pass
        return "\n".join(lines)

    def send_weekly_summary(self, api, streams=None) -> dict:
        self.send_message(self.build_weekly_summary(api, streams))
        return self.status()

    def check_weekly(self, api, streams=None) -> bool:
        """Envoie le récapitulatif hebdomadaire le dimanche à partir de 20 h (une seule fois par semaine)."""
        lt = time.localtime()
        if lt.tm_wday != 6 or lt.tm_hour < 20:
            return False
        week_key = f"weekly:{time.strftime('%G-W%V', lt)}"
        return self.notify("weekly", week_key, self.build_weekly_summary(api, streams), cooldown=0, persist=True)

    def start_watchdog(self, api, tunnel_mgr=None, streams=None, *, initial_delay: float = 120) -> None:
        def _loop() -> None:
            if self._stop.wait(initial_delay):
                return
            while not self._stop.is_set():
                try:
                    if time.monotonic() - self._last_expiry_check >= EXPIRY_CHECK_EVERY:
                        self._last_expiry_check = time.monotonic()
                        self.check_expiries(api)
                    self.check_tunnel(tunnel_mgr)
                    self.check_weekly(api, streams)
                except Exception as exc:  # ne doit jamais interrompre le serveur
                    LOG.debug("Surveillance alertes ignorée : %s", exc)
                self._stop.wait(WATCHDOG_INTERVAL)

        threading.Thread(target=_loop, name="alerts-watchdog", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


_ALERTS: AlertManager | None = None


def install(manager: AlertManager | None) -> None:
    global _ALERTS
    _ALERTS = manager


def current() -> AlertManager | None:
    return _ALERTS


def notify(category: str, dedupe_key: str, text: str, *, cooldown: float = 3600, persist: bool = False) -> bool:
    mgr = _ALERTS
    if mgr is None:
        return False
    return mgr.notify(category, dedupe_key, text, cooldown=cooldown, persist=persist)
