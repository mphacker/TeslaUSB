# TeslaUSB B-1 web app — gunicorn configuration.
#
# Install path (matches Phase 6 setup.sh + the teslausb-web.service unit):
#   /etc/teslausb/gunicorn.conf.py
#
# Usage from systemd:
#   ExecStart=/usr/bin/gunicorn -c /etc/teslausb/gunicorn.conf.py \
#                               teslausb_web.wsgi:app
#
# Charter §"No shortcuts" / Decisions #29: single SYNC worker, Unix
# socket upstream, never the Flask dev server. The Pi Zero 2 W has
# 512 MiB of RAM and 4 cores; a single sync worker keeps Flask's
# memory footprint predictable (~80 MB resident) and avoids racey
# SQLite writers in the mapping DB. Peak request rate during user
# interaction is ~1/min, so concurrency is a non-goal — fault
# isolation, restart determinism, and steady-state memory are.
#
# DO NOT raise ``workers`` above 1 without:
#   1. Switching every SQLite writer to WAL + a writer lock, and
#   2. Re-running Phase 4c cache-invalidation tests against a
#      multi-worker layout.
# See docs/00-PLAN.md "Decisions" row 29 and ADR-0008.

from __future__ import annotations

# --- Bind ---------------------------------------------------------------

# Unix socket. nginx (the only upstream) talks to this; the socket
# lives in a tmpfs-backed dir created by setup.sh with mode 0770 and
# group www-data so the nginx worker can connect without the gunicorn
# worker needing to chown the socket itself.
bind = "unix:/run/teslausb/gunicorn.sock"

# Socket permissions. nginx runs as www-data; gunicorn runs as the
# teslausb user (also a member of www-data). 0660 = owner+group rw.
umask = 0o007


# --- Worker model -------------------------------------------------------

workers = 1
worker_class = "sync"

# Threads stay at 1 too — the Flask app is not thread-safe across all
# blueprints (e.g., the mapping cache invalidator's debouncer state).
threads = 1


# --- Timeouts -----------------------------------------------------------

# A large lightshow ZIP upload can take real wall time on a Pi Zero 2 W
# (USB 2.0 high-speed peaks ~30 MB/s and uploads land on the SD card,
# which is much slower). 120 s covers a 500 MiB upload at 5 MB/s with
# headroom; if the user hits this they have other problems.
timeout = 120
graceful_timeout = 30

# Keep-alive only matters for the nginx -> gunicorn hop. nginx sets
# keepalive 32 upstream-side, so the gunicorn-side timeout just needs
# to be longer than nginx's proxy_read_timeout (60 s).
keepalive = 75


# --- Process hygiene ----------------------------------------------------

# Recycle the worker after this many requests to clamp any slow leak.
# The Flask app caches mapping DB query results in-process; a recycle
# every ~24 h of light usage clears any pathological growth without
# user-visible downtime (gunicorn forks the replacement before killing
# the old worker, and the socket is shared).
max_requests = 5000
max_requests_jitter = 500


# --- Logging ------------------------------------------------------------

# Logs go to stdout/stderr; systemd captures them via journald. Do not
# write a separate log file — that would race with logrotate and clutter
# the SD card. Use ``journalctl -u teslausb-web -f`` to tail.
accesslog = "-"
errorlog = "-"
loglevel = "info"

# Concise access log — drop the verbose User-Agent. The captive-portal
# probes from Tesla's nav unit add no signal.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" rt=%(L)ss'


# --- preload_app: deliberately off --------------------------------------

# Preloading would import teslausb_web.wsgi (and thus create_app) in the
# master process before fork. That is incompatible with the IPC client
# which opens a Unix socket to teslausb-worker at create_app time — the
# socket would be inherited by the worker and any future fork (e.g., a
# max_requests recycle) would end up with two FDs racing. Single sync
# worker + no preload = each new worker opens its own socket cleanly.
preload_app = False


# --- Hooks --------------------------------------------------------------


def on_starting(server):  # noqa: ANN001, D401, ARG001 — gunicorn hook signature
    """Log readable boot banner so journalctl shows the config path."""
    import logging

    logging.getLogger("gunicorn.error").info(
        "teslausb-web starting: socket=%s workers=%d timeout=%ds",
        bind,
        workers,
        timeout,
    )


def worker_exit(server, worker):  # noqa: ANN001, D401, ARG001
    """Best-effort cleanup hook.

    teslausb_web.app registers atexit handlers that close the IPC
    socket and stop the cache-invalidator thread, so we do not need to
    duplicate that work here — this hook exists only so that worker
    recycle events are visible in journalctl.
    """
    import logging

    logging.getLogger("gunicorn.error").info(
        "teslausb-web worker pid=%d exiting",
        worker.pid,
    )
