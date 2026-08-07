"""envload.py — tiny .env loader (no dependency).

Import this FIRST in any entrypoint (serve.py, worker.py, apply.py, pipeline.py)
so keys placed in a local `.env` file get picked up automatically:

    import envload   # noqa: F401  (loads .env into os.environ)

Existing environment variables always win over .env (we use setdefault),
so you can still override per-invocation on the command line.
"""
import os


def load(path=".env"):
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    os.environ.setdefault(key, val)
    except FileNotFoundError:
        pass


load()
