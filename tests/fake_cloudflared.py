#!/usr/bin/env python3
"""Faux cloudflared pour les tests et la démo : mêmes sorties que le vrai, sans réseau."""
import json
import os
import sys
import time
from pathlib import Path

home = Path(os.environ["HOME"])
args = sys.argv[1:]
with open(home / "calls.log", "a", encoding="utf-8") as log:
    log.write(" ".join(args) + "\n")
cert = Path(os.environ.get("TUNNEL_ORIGIN_CERT") or home / ".cloudflared" / "cert.pem")
TUNNEL_ID = "6ff42ae2-765d-4adf-8112-31c55c1551ef"

if args[:2] == ["tunnel", "login"]:
    if cert.exists():
        print("You have an existing certificate at cert.pem which login would overwrite.", flush=True)
        sys.exit(1)
    print("Please open the following URL and log in with your Cloudflare account:\n", flush=True)
    print("https://dash.cloudflare.com/argotunnel?aud=&callback=https%3A%2F%2Flogin.cloudflareaccess.org%2Ffake\n",
          flush=True)
    print("Leave cloudflared running to download the cert automatically.", flush=True)
    time.sleep(float(os.environ.get("FAKE_CF_LOGIN_DELAY", "0.5")))
    cert.parent.mkdir(parents=True, exist_ok=True)
    cert.write_text("-----BEGIN ARGO TUNNEL TOKEN-----\nfake\n-----END ARGO TUNNEL TOKEN-----\n")
    print(f"You have successfully logged in.\nIf you wish to copy your credentials to a server, they have been saved to:\n{cert}", flush=True)
elif args[:2] == ["tunnel", "create"]:
    creds = Path(args[args.index("--credentials-file") + 1])
    creds.write_text(json.dumps({"AccountTag": "x", "TunnelSecret": "secret", "TunnelID": TUNNEL_ID}))
    print(f"Tunnel credentials written to {creds}.\nCreated tunnel {args[-1]} with id {TUNNEL_ID}", flush=True)
elif args[:3] == ["tunnel", "route", "dns"]:
    host = args[-1]
    if host.endswith(".refuse.test"):
        print(f'2026-11-22T10:00:00Z ERR Failed to add route: code: 1003, reason: zone for "{host}" not found', flush=True)
        sys.exit(1)
    print(f"2026-11-22T10:00:00Z INF Added CNAME {host} which will route to this tunnel tunnelID={TUNNEL_ID}", flush=True)
elif args and args[-1] == "run":
    print("2026-11-22T10:00:00Z INF Starting tunnel tunnelID=" + TUNNEL_ID, flush=True)
    print("2026-11-22T10:00:01Z INF Registered tunnel connection connIndex=0 location=cdg01 protocol=quic", flush=True)
    while True:
        time.sleep(1)
elif args[:2] == ["tunnel", "delete"]:
    print(f"Deleted tunnel {args[-1]}", flush=True)
else:
    print("unknown command", flush=True)
    sys.exit(2)
