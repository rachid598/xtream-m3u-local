"""Outils communs aux tests : environnement complet (faux fournisseur + ports 7777/7778)."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import replace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app.custom import PlaylistStore
from app.server import Config, XtreamAPI, make_epg, make_series, make_server, make_streams
from app.web import make_web_server

from .fake_xtream import PASSWORD, USERNAME, reset_fake, start_fake

HEADERS = {"X-Requested-With": "XtreamM3ULocal", "Content-Type": "application/json"}


class Stack:
    """Instance complète de l'application sur des ports éphémères."""

    def __init__(self, provider_url: str, data_dir: str, **overrides):
        self.config = replace(Config(provider_url, USERNAME, PASSWORD, data_dir=data_dir), **overrides)
        self.api = XtreamAPI(self.config)
        self.store = PlaylistStore(data_dir)
        self.epg = make_epg(self.config, self.api)
        self.series = make_series(self.config, self.api)
        self.streams = make_streams(self.config, self.api)
        self.proxy = make_server(self.config, host="127.0.0.1", port=0, api=self.api, store=self.store,
                                 epg=self.epg, series=self.series, streams=self.streams)
        self.web = make_web_server(self.config, self.api, self.store, host="127.0.0.1", port=0,
                                   epg=self.epg, series=self.series, streams=self.streams)
        for srv in (self.proxy, self.web):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.proxy_base = f"http://127.0.0.1:{self.proxy.server_address[1]}"
        self.web_base = f"http://127.0.0.1:{self.web.server_address[1]}"

    def close(self):
        for srv in (self.web, self.proxy):
            srv.shutdown()
            srv.server_close()


class StackTestCase(unittest.TestCase):
    overrides: dict = {}

    @classmethod
    def setUpClass(cls):
        cls.provider, cls.provider_url = start_fake()

    @classmethod
    def tearDownClass(cls):
        cls.provider.shutdown()
        cls.provider.server_close()

    def setUp(self):
        reset_fake()
        self.tmp = tempfile.TemporaryDirectory()
        self.stack = self.make_stack(**self.overrides)

    def tearDown(self):
        for stack in getattr(self, "_extra", []):
            stack.close()
        self.stack.close()
        self.tmp.cleanup()

    def make_stack(self, data_dir: str | None = None, **overrides) -> Stack:
        stack = Stack(self.provider_url, data_dir or self.tmp.name, **overrides)
        if hasattr(self, "stack"):
            self._extra = getattr(self, "_extra", []) + [stack]
        return stack

    # ------------------------------------------------------------- requêtes
    @staticmethod
    def get(url: str, headers: dict | None = None) -> tuple[int, bytes, dict]:
        try:
            with urlopen(Request(url, headers=headers or {}), timeout=10) as r:
                return r.status, r.read(), dict(r.headers)
        except HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    @staticmethod
    def send(url: str, method: str, data: object = None, headers: dict | None = None) -> tuple[int, object]:
        body = None if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())
        req = Request(url, data=body, method=method, headers=HEADERS if headers is None else headers)
        try:
            with urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"null")
        except HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw

    def create(self, payload: dict, stack: Stack | None = None) -> tuple[int, object]:
        return self.send((stack or self.stack).web_base + "/api/playlists", "POST", payload)
