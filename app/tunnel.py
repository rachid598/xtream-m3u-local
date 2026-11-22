"""Tunnel Cloudflare piloté depuis l'interface : « je clique, je me connecte ».

1. ``login()`` lance ``cloudflared tunnel login`` : Cloudflare donne une adresse ; tu t'y connectes, tu
   choisis ton domaine et tu autorises. ``cloudflared`` télécharge alors tout seul le certificat du compte
   (``cert.pem``).
2. ``setup(adresse)`` crée le tunnel (une fois), ajoute l'adresse (sous-domaine ou domaine) dans les DNS
   Cloudflare et démarre le tunnel vers le port des lecteurs.
3. Le tunnel redémarre tout seul (coupure, redémarrage du conteneur) tant qu'il est activé.

Tout est rangé dans ``DATA_DIR/cloudflared`` (droits 0700) ; le certificat et les clés du tunnel ne sont
jamais renvoyés à l'interface ni écrits dans les journaux. Seul le port des lecteurs est publié : chaque
lecteur s'y présente avec son code d'appareil (accès distant), l'interface de gestion reste à la maison.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

LOG = logging.getLogger("xtream-m3u-local.tunnel")
HOSTNAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
LOGIN_URL_RE = re.compile(r"https://dash\.cloudflare\.com/argotunnel\?\S+")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LOGIN_TIMEOUT = 15 * 60       # l'autorisation dans le navigateur doit être faite dans ce délai
RESTART_DELAYS = (5, 15, 30, 60, 120)


class TunnelError(Exception):
    """Erreur affichable telle quelle dans l'interface."""


def clean_hostname(value: object) -> str:
    host = str(value or "").strip().lower().rstrip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0]
    if not HOSTNAME_RE.fullmatch(host):
        raise TunnelError("Adresse attendue : un domaine ou un sous-domaine, ex. tv.mondomaine.fr")
    return host


class Tunnel:
    def __init__(self, data_dir: str, port: int, *, binary: str | None = None) -> None:
        self.dir = Path(data_dir) / "cloudflared"
        self.port = port
        self.binary = binary or os.environ.get("CLOUDFLARED_BIN") or shutil.which("cloudflared") or ""
        self._lock = threading.RLock()
        self._login: subprocess.Popen | None = None
        self._login_url = ""
        self._login_error = ""
        self._run: subprocess.Popen | None = None
        self._connected = False
        self._last_error = ""
        self._stopping = False
        self._failures = 0

    # ---------------------------------------------------------------- fichiers
    @property
    def cert(self) -> Path:
        return self.dir / ".cloudflared" / "cert.pem"

    @property
    def state_file(self) -> Path:
        return self.dir / "tunnel.json"

    def _state(self) -> dict:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, data: dict) -> None:
        self._prepare_dir()
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_file)

    def _prepare_dir(self) -> None:
        (self.dir / ".cloudflared").mkdir(parents=True, exist_ok=True)
        for path in (self.dir, self.dir / ".cloudflared"):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass

    def _env(self) -> dict:
        # cloudflared range le certificat dans $HOME/.cloudflared : on le garde dans /data.
        env = {k: v for k, v in os.environ.items() if not k.startswith("TUNNEL_")}
        env.update({"HOME": str(self.dir), "TUNNEL_ORIGIN_CERT": str(self.cert), "NO_AUTOUPDATE": "true"})
        return env

    def _cmd(self, *args: str, timeout: float = 60) -> str:
        if not self.binary:
            raise TunnelError("cloudflared absent de cette installation (utilise l'image Docker à jour)")
        try:
            done = subprocess.run([self.binary, *args], env=self._env(), capture_output=True, text=True,
                                  timeout=timeout, cwd=str(self.dir))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TunnelError(f"cloudflared ne répond pas ({type(exc).__name__})") from None
        output = (done.stdout or "") + (done.stderr or "")
        if done.returncode != 0:
            raise TunnelError(self._explain(output) or f"cloudflared a échoué (code {done.returncode})")
        return output

    @staticmethod
    def _explain(output: str) -> str:
        """Dernière ligne d'erreur utile de cloudflared (sans chemin ni secret)."""
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        for line in reversed(lines):
            low = line.lower()
            if "err" in low or "fail" in low or "error" in low:
                line = re.sub(r"/\S+", "…", line)
                return line[-240:]
        return ""

    # ------------------------------------------------------------------ état
    def status(self) -> dict:
        with self._lock:
            state = self._state()
            login_running = self._login is not None and self._login.poll() is None
            running = self._run is not None and self._run.poll() is None
            return {
                "available": bool(self.binary),
                "logged_in": self.cert.exists(),
                "login_pending": login_running,
                "login_url": self._login_url if login_running else "",
                "login_error": self._login_error,
                "hostname": state.get("hostname", ""),
                "tunnel_id": state.get("tunnel_id", ""),
                "enabled": bool(state.get("enabled")),
                "running": running,
                "connected": running and self._connected,
                "error": self._last_error,
            }

    def active(self) -> bool:
        """Tunnel activé : les requêtes arrivent par cloudflared (adresse 127.0.0.1)."""
        return bool(self._state().get("enabled")) and bool(self._state().get("tunnel_id"))

    def hostname(self) -> str:
        return self._state().get("hostname", "") if self.active() else ""

    # ------------------------------------------------------------- connexion
    def login(self) -> str:
        """Démarre la connexion au compte Cloudflare et retourne l'adresse à ouvrir dans le navigateur."""
        with self._lock:
            if self.cert.exists():
                return ""
            if self._login is not None and self._login.poll() is None and self._login_url:
                return self._login_url
            if not self.binary:
                raise TunnelError("cloudflared absent de cette installation (utilise l'image Docker à jour)")
            self._prepare_dir()
            self._login_url, self._login_error = "", ""
            try:
                proc = subprocess.Popen([self.binary, "tunnel", "login"], env=self._env(), cwd=str(self.dir),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            except OSError as exc:
                raise TunnelError(f"cloudflared ne démarre pas ({type(exc).__name__})") from None
            self._login = proc
        found = threading.Event()
        threading.Thread(target=self._watch_login, args=(proc, found), daemon=True, name="cf-login").start()
        found.wait(20)
        with self._lock:
            if not self._login_url:
                raise TunnelError(self._login_error or "Cloudflare n'a pas donné d'adresse de connexion : réessaie")
            return self._login_url

    def _watch_login(self, proc: subprocess.Popen, found: threading.Event) -> None:
        started = time.monotonic()
        output = []
        try:
            for line in proc.stdout:
                output.append(line)
                match = LOGIN_URL_RE.search(line)
                if match and not self._login_url:
                    with self._lock:
                        self._login_url = match.group(0)
                    found.set()
                if time.monotonic() - started > LOGIN_TIMEOUT:
                    proc.kill()
                    break
            proc.wait(timeout=5)
        except Exception:  # surveillance seulement
            proc.kill()
        finally:
            proc.stdout.close()
        with self._lock:
            if not self.cert.exists():
                self._login_error = self._explain("".join(output)) or "Connexion à Cloudflare non terminée"
            else:
                self._login_error = ""
                try:
                    os.chmod(self.cert, 0o600)
                except OSError:
                    pass
                LOG.info("Tunnel Cloudflare : compte connecté")
        found.set()

    def logout(self) -> None:
        """Oublie le compte Cloudflare (certificat), sans toucher au tunnel déjà créé."""
        with self._lock:
            if self._login is not None and self._login.poll() is None:
                self._login.kill()
            self._login_url = ""
            try:
                self.cert.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------- tunnel
    def setup(self, hostname: object) -> dict:
        """Crée le tunnel si besoin, publie l'adresse dans les DNS Cloudflare et démarre le tunnel."""
        host = clean_hostname(hostname)
        with self._lock:
            if not self.cert.exists():
                raise TunnelError("Connecte d'abord ton compte Cloudflare")
            state = self._state()
            tunnel_id = state.get("tunnel_id", "")
            creds = self.dir / f"{tunnel_id}.json" if tunnel_id else None
            if not tunnel_id or not creds.exists():
                name = state.get("name") or f"xtream-m3u-local-{os.urandom(3).hex()}"
                tmp_creds = self.dir / "credentials.json"
                output = self._cmd("tunnel", "create", "--credentials-file", str(tmp_creds), name)
                found = UUID_RE.search(output)
                if not found or not tmp_creds.exists():
                    raise TunnelError("Tunnel non créé : réponse inattendue de Cloudflare")
                tunnel_id = found.group(0)
                creds = self.dir / f"{tunnel_id}.json"
                os.replace(tmp_creds, creds)
                os.chmod(creds, 0o600)
                state = {"name": name, "tunnel_id": tunnel_id}
                LOG.info("Tunnel Cloudflare « %s » créé", name)
            try:
                self._cmd("tunnel", "route", "dns", "--overwrite-dns", tunnel_id, host)
            except TunnelError as exc:
                raise TunnelError(f"Adresse {host} non publiée : {exc} (le domaine doit être celui choisi chez "
                                  "Cloudflare à la connexion)") from None
            state.update({"hostname": host, "enabled": True})
            self._save_state(state)
            self._write_config(state, creds)
        self.start()
        return self.status()

    def _write_config(self, state: dict, creds: Path) -> None:
        config = (
            f"tunnel: {state['tunnel_id']}\n"
            f"credentials-file: {creds}\n"
            "ingress:\n"
            f"  - hostname: {state['hostname']}\n"
            f"    service: http://127.0.0.1:{self.port}\n"
            "  - service: http_status:404\n"
        )
        path = self.dir / "config.yml"
        path.write_text(config, encoding="utf-8")
        os.chmod(path, 0o600)

    def start(self) -> None:
        """Démarre (ou garde démarré) le tunnel s'il est configuré et activé."""
        with self._lock:
            state = self._state()
            if not state.get("tunnel_id") or not self.binary:
                return
            if not state.get("enabled"):
                state["enabled"] = True
                self._save_state(state)
            if self._run is not None and self._run.poll() is None:
                return
            self._stopping = False
            self._spawn()

    def _spawn(self) -> None:
        self._connected = False
        try:
            self._run = subprocess.Popen(
                [self.binary, "tunnel", "--no-autoupdate", "--config", str(self.dir / "config.yml"), "run"],
                env=self._env(), cwd=str(self.dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        except OSError as exc:
            self._last_error = f"cloudflared ne démarre pas ({type(exc).__name__})"
            return
        threading.Thread(target=self._watch_run, args=(self._run,), daemon=True, name="cf-run").start()

    def _watch_run(self, proc: subprocess.Popen) -> None:
        tail: list[str] = []
        for line in proc.stdout:
            tail = (tail + [line])[-30:]
            low = line.lower()
            if "registered tunnel connection" in low:
                with self._lock:
                    if not self._connected:
                        LOG.info("Tunnel Cloudflare connecté : https://%s", self._state().get("hostname", ""))
                    self._connected, self._last_error, self._failures = True, "", 0
        code = proc.wait()
        proc.stdout.close()
        with self._lock:
            if self._run is not proc:
                return
            self._connected = False
            if self._stopping:
                return
            self._last_error = self._explain("".join(tail)) or f"cloudflared arrêté (code {code})"
            delay = RESTART_DELAYS[min(self._failures, len(RESTART_DELAYS) - 1)]
            self._failures += 1
        LOG.warning("Tunnel Cloudflare coupé (%s) : nouvel essai dans %d s", self._last_error, delay)
        time.sleep(delay)
        with self._lock:
            if not self._stopping and self._run is proc and self._state().get("enabled"):
                self._spawn()

    def stop(self, *, disable: bool = True) -> None:
        with self._lock:
            self._stopping = True
            if disable:
                state = self._state()
                if state:
                    state["enabled"] = False
                    self._save_state(state)
            proc, self._run = self._run, None
            self._connected = False
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def remove(self) -> None:
        """Arrête et supprime le tunnel chez Cloudflare. L'enregistrement DNS reste (à effacer chez Cloudflare)."""
        self.stop()
        with self._lock:
            state = self._state()
            tunnel_id = state.get("tunnel_id")
            if tunnel_id and self.cert.exists():
                try:
                    self._cmd("tunnel", "delete", "-f", tunnel_id)
                except TunnelError as exc:
                    LOG.warning("Tunnel Cloudflare non supprimé chez Cloudflare : %s", exc)
            for path in (self.dir / f"{tunnel_id}.json" if tunnel_id else None, self.dir / "config.yml",
                         self.state_file):
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            self._last_error = ""


_TUNNEL: Tunnel | None = None


def install(tunnel: Tunnel | None) -> None:
    global _TUNNEL
    _TUNNEL = tunnel


def current() -> Tunnel | None:
    return _TUNNEL


def active() -> bool:
    return _TUNNEL is not None and _TUNNEL.active()


def hostname() -> str:
    return _TUNNEL.hostname() if _TUNNEL is not None else ""
