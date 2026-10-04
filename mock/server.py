"""Start the mock portal (port 8701) and the mock CEM (port 8702).

    python -m mock.server

Both share one in-memory state.  Open http://127.0.0.1:8701 and
http://127.0.0.1:8702 in a browser to look around.  The current state
(tickets, CEM users) is at /__state, POST /__reset restores the seed data.
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
from pathlib import Path

from werkzeug.serving import make_server

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from .cem_app import app as cem_app
from .portal_app import app as portal_app


class MockServers:
    def __init__(self, host="127.0.0.1", portal_port=8701, cem_port=8702):
        logging.getLogger("werkzeug").setLevel(logging.WARNING)   # no per-request lines
        self.servers = [make_server(host, portal_port, portal_app, threaded=True),
                        make_server(host, cem_port, cem_app, threaded=True)]
        self.threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in self.servers]

    def start(self):
        for t in self.threads:
            t.start()
        return self

    def stop(self):
        for s in self.servers:
            s.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Run the mock portal + mock CEM")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--portal-port", type=int, default=8701)
    ap.add_argument("--cem-port", type=int, default=8702)
    args = ap.parse_args()
    env = Path(__file__).resolve().parent.parent / ".env"
    if load_dotenv and env.exists():
        load_dotenv(env)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    m = MockServers(args.host, args.portal_port, args.cem_port).start()
    print(f"Mock portal : http://{args.host}:{args.portal_port}   (user {os.environ.get('PORTAL_USERNAME', '233786')})")
    print(f"Mock CEM    : http://{args.host}:{args.cem_port}")
    print("State       : /__state on either server.  Ctrl+C to stop.")
    try:
        for t in m.threads:
            t.join()
    except KeyboardInterrupt:
        m.stop()


if __name__ == "__main__":
    main()
