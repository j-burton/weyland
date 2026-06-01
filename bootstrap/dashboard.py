#!/usr/bin/env python3
"""weyland live setup dashboard — forge-steel wizard (browser-first, v2).

Usage: dashboard.py <state_dir> <nonce>

ThreadingHTTPServer on 0.0.0.0:8080:
  GET  /              -> the wizard HTML (polls /state every 1.5s)
  GET  /state?k=N     -> state.json + {"identity_submitted": bool} (nonce-gated)
  POST /identity?k=N  -> validate + write identity.json (the browser identity form)
  POST /save-pat?k=N  -> validate github_pat_/ghp_, write /etc/weyland/weyland.env
  POST /restart?k=N   -> stop the bootstrap, reset state, re-exec it (Start over)
  POST /done?k=N      -> 200 then shut down

The bash side is the single writer of state.json; this server writes only
identity.json (POST /identity) and weyland.env (POST /save-pat). Shuts down
after 15 min idle. Never logs the PAT or bearer.

ENV_FILE / PORT are env-overridable (WEYLAND_ENV_FILE / WEYLAND_DASH_PORT) for
testing; production uses the defaults.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import sys
import tempfile
import time
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

STATE_DIR = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/weyland"
NONCE = sys.argv[2] if len(sys.argv) > 2 else ""
STATE_FILE = os.path.join(STATE_DIR, "state.json")
IDENTITY_FILE = os.path.join(STATE_DIR, "identity.json")
ENV_FILE = os.environ.get("WEYLAND_ENV_FILE", "/etc/weyland/weyland.env")
PORT = int(os.environ.get("WEYLAND_DASH_PORT", "8080"))
IDLE_TIMEOUT = 900  # 15 minutes

PAT_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+)$")
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

_last = [time.time()]
_srv = [None]


# Phase (name, label) list — mirrors install.sh's _state_op PHASES exactly, so a
# /restart can reset state.json to the identity stage without the bash side.
PHASES = [
    ("preflight", "The forge is inspected"),
    ("identity", "The minion receives its name"),
    ("packages", "Tools of war are gathered"),
    ("tailscale", "The minion enters the realm"),
    ("github_auth", "GitHub demands tribute"),
    ("per_pi_repo", "The chronicles are opened"),
    ("tunnel", "The passage through the void is opened"),
    ("claude_code", "The intelligence is summoned"),
    ("connector", "The connector is forged"),
    ("vault", "The ancient secrets are retrieved"),
    ("selfdoc", "The minion speaks its name"),
    ("summary", "The induction is sealed"),
]


def _valid_name(n: str) -> bool:
    return bool(NAME_RE.match(n)) and n != "raspberrypi"


# --- Live phase progress (a plain-English DEBUG line for the running phase) ---
# Computed PER /state REQUEST from the active phase's log tail — never written
# into state.json, so the bash side stays its single writer (no race). The text
# is deliberately literal (real tool names / actions), unlike the mythical phase
# labels. pct is filled only when the tool actually emits one (apt/git progress).
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9AB]|\x1b[=>]")
_PCT_RES = [
    re.compile(r"Progress:\s*\[\s*(\d{1,3})%\]"),                       # apt fancy progress
    re.compile(r"(?:Receiving|Resolving)\s+(?:objects|deltas):\s+(\d{1,3})%"),  # git
]


def _strip_control(text: str) -> str:
    return _ANSI_RE.sub("", text).replace("\r", "\n")


def _read_log_tail(path: str, nbytes: int = 8192) -> str:
    try:
        with open(path, "rb") as f:
            try:
                f.seek(-nbytes, os.SEEK_END)
            except OSError:
                f.seek(0)
            data = f.read()
    except OSError:
        return ""
    return _strip_control(data.decode("utf-8", "replace"))


def _progress_pct(lines):
    for ln in reversed(lines):
        for rx in _PCT_RES:
            m = rx.search(ln)
            if m:
                v = int(m.group(1))
                if 0 <= v <= 100:
                    return v
    return None


def _progress_text(lines):
    """Most recent log line matching a known tool signal, as plain English.
    Rules tried per line; the most recent matching line wins (live updates)."""
    for ln in reversed(lines):
        s = ln.strip()
        if not s:
            continue
        low = s.lower()
        m = re.match(r"Setting up (\S+)", s)
        if m:
            return "apt: installing " + m.group(1) + "..."
        m = re.match(r"Unpacking (\S+)", s)
        if m:
            return "apt: unpacking " + m.group(1) + "..."
        # apt Get line: "Get:N <url> <suite>/<comp> <arch> <pkg> ..." — match the
        # FIRST arch token (non-greedy) and take the package right after it.
        m = re.match(r"Get:\d+\s.*?\b(?:all|arm64|armhf|amd64|i386)\s+(\S+)", s)
        if m and m.group(1) not in (
                "Packages", "Sources", "InRelease", "Release", "Translation-en"):
            return "apt: downloading " + m.group(1) + "..."
        m = re.match(r"Preparing to unpack \S*?/?([A-Za-z0-9][A-Za-z0-9.+-]*?)[_ ]", s)
        if m:
            return "apt: installing " + m.group(1) + "..."
        if low.startswith("cloning into"):
            return "git: cloning repository..."
        if "receiving objects" in low:
            return "git: receiving objects..."
        if "resolving deltas" in low:
            return "git: resolving deltas..."
        if "one-time code" in low:
            return "gh: authenticating with GitHub..."
        if "to authenticate, visit" in low or "login.tailscale.com/a/" in low:
            return "tailscale: waiting for you to authenticate..."
        if "installing tailscale" in low:
            return "tailscale: installing..."
        if "installing cloudflared" in low:
            return "cloudflared: installing..."
        if "installing claude code" in low or low.startswith("installing claude"):
            return "claude: installing..."
        if low.startswith("% total") or ("curl" in low and "%" in s):
            return "downloading..."
    return None


def _active_progress(s):
    """{'text', 'pct'} for the running phase, or None. Reads the per-phase log
    if present (run_dance writes <phase>.log), else the global bootstrap.log."""
    running = None
    for p in (s.get("phases") or []):
        if p.get("status") == "running":
            running = p.get("name")
            break
    if not running:
        return None
    logf = os.path.join(STATE_DIR, running + ".log")
    if not (os.path.exists(logf) and os.path.getsize(logf) > 0):
        logf = os.path.join(STATE_DIR, "bootstrap.log")
    tail = _read_log_tail(logf)
    if not tail:
        return None
    lines = tail.splitlines()
    text = _progress_text(lines)
    if text is None:
        last = ""
        for ln in reversed(lines):
            if ln.strip():
                last = ln.strip()
                break
        if not last:
            return None
        text = running + ": " + last[:60]
    return {"text": text, "pct": _progress_pct(lines)}


def state_with_flags() -> bytes:
    """state.json verbatim, plus identity_submitted, this Pi's hostname, and a
    live `progress` line for the running phase.

    hostname lets the browser pre-fill the name field on a fresh/started-over Pi.
    progress is a per-request debug line (never written back to state.json, so
    the bash side stays the single writer). None when no phase is running.
    """
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s["identity_submitted"] = os.path.exists(IDENTITY_FILE)
    host = (socket.gethostname() or "").strip().lower()
    s["hostname"] = host if _valid_name(host) else ""
    try:
        s["progress"] = _active_progress(s)
    except Exception:
        s["progress"] = None
    return json.dumps(s).encode("utf-8")


def write_identity(form) -> bool:
    name = (form.get("pi_name", [""])[0] or "").strip().lower()
    if not NAME_RE.match(name):
        return False
    # SSH public keys are a single line; take the first non-empty line so a
    # paste/upload with a trailing newline stays one line (the bash side reads
    # this positionally). The password is taken verbatim (spaces may matter).
    mode = (form.get("ssh_mode", ["none"])[0] or "none").strip()
    if mode not in ("none", "existing", "generate"):
        mode = "none"
    pub_raw = form.get("ssh_pub_key", [""])[0] or ""
    ssh_pub_key = next((ln.strip() for ln in pub_raw.splitlines() if ln.strip()), "")
    data = {
        "pi_name": name,
        "domain": (form.get("domain", [""])[0] or "").strip(),
        "pc_wake": (form.get("pc_wake", [""])[0] or "").strip(),
        "wake_token": (form.get("wake_token", [""])[0] or "").strip(),
        "new_password": form.get("new_password", [""])[0] or "",
        "ssh_mode": mode,
        "ssh_pub_key": ssh_pub_key,
    }
    d = os.path.dirname(IDENTITY_FILE) or "."
    tmp = os.path.join(d, ".identity.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, IDENTITY_FILE)
        return True
    except OSError:
        return False


def write_pat(pat: str) -> bool:
    """Replace WEYLAND_PAT= in ENV_FILE, preserving other keys. Never logged."""
    try:
        with open(ENV_FILE) as f:
            lines = f.read().splitlines()
    except OSError:
        cur = subprocess.run(["sudo", "-n", "cat", ENV_FILE], capture_output=True, text=True)
        lines = cur.stdout.splitlines() if cur.returncode == 0 else []
    kept = [ln for ln in lines if not ln.startswith("WEYLAND_PAT=")]
    kept.append("WEYLAND_PAT=" + pat)
    content = "\n".join(kept) + "\n"
    try:
        with open(ENV_FILE, "w") as f:
            f.write(content)
    except OSError:
        p = subprocess.run(["sudo", "-n", "tee", ENV_FILE], input=content, text=True, capture_output=True)
        if p.returncode != 0:
            return False
    subprocess.run(["sudo", "-n", "chown", "root:admin", ENV_FILE], capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "0640", ENV_FILE], capture_output=True)
    return True


# ---------------------------------------------------------------------------
# /restart — "Start over": stop the running bootstrap, wipe identity, reset
# state.json to the identity stage, then re-exec the bootstrap so it ADOPTS this
# same dashboard (same nonce/port → the open browser tab keeps working) and
# waits for the operator to name the minion again. The bash side is the normal
# sole writer of state.json; we only touch it here while tearing the bootstrap
# down and relaunching it.
# ---------------------------------------------------------------------------
DASH_PID_FILE = os.path.join(STATE_DIR, "dashboard.pid")
BOOTSTRAP_PID_FILE = os.path.join(STATE_DIR, "bootstrap.pid")
STATE_ENV_FILE = os.path.join(STATE_DIR, "env")


def _kill_bootstrap() -> None:
    """SIGTERM the running bootstrap, if any. Guarded against PID reuse: only
    kills a process whose cmdline actually mentions install.sh."""
    try:
        pid = int(open(BOOTSTRAP_PID_FILE).read().strip())
    except Exception:
        return
    if pid <= 1 or pid == os.getpid():
        return
    try:
        cmdline = open("/proc/%d/cmdline" % pid, "rb").read().decode("utf-8", "replace")
    except OSError:
        return  # already gone (or unreadable) — nothing to kill
    if "install.sh" not in cmdline:
        return  # PID was reused by something else — leave it alone
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _clear_identity_env() -> None:
    """Drop PI_NAME=/DOMAIN= from $STATE_DIR/env so the relaunched bootstrap
    asks for identity again instead of treating it as a re-run."""
    try:
        with open(STATE_ENV_FILE) as f:
            lines = f.read().splitlines()
    except OSError:
        r = subprocess.run(["sudo", "-n", "cat", STATE_ENV_FILE], capture_output=True, text=True)
        if r.returncode != 0:
            return
        lines = r.stdout.splitlines()
    kept = [ln for ln in lines if not (ln.startswith("PI_NAME=") or ln.startswith("DOMAIN="))]
    content = ("\n".join(kept) + "\n") if kept else ""
    try:
        with open(STATE_ENV_FILE, "w") as f:
            f.write(content)
    except OSError:
        subprocess.run(["sudo", "-n", "tee", STATE_ENV_FILE], input=content,
                       text=True, capture_output=True)


def _reset_state_identity() -> None:
    """Rewrite state.json back to the fresh identity stage (mirrors install.sh
    state_init), preserving local_ip."""
    try:
        with open(STATE_FILE) as f:
            old = json.load(f)
    except Exception:
        old = {}
    ip = old.get("local_ip", "") if isinstance(old, dict) else ""
    s = {
        "pi_name": "", "domain": "", "local_ip": ip,
        "phases": [{"name": n, "label": l, "status": "pending"} for n, l in PHASES],
        "action": {"active": False},
        "result": {"ready": False, "bearer": "", "mcp_url": "", "consent_tunnel": "",
                   "consent_local": "", "client_id": "weyland-mcp-claude-ai",
                   "repo": "", "project_instructions": ""},
    }
    d = os.path.dirname(STATE_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=d)
    with os.fdopen(fd, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE_FILE)


def _relaunch_bootstrap() -> bool:
    """Re-exec install.sh detached, told to adopt this dashboard (same nonce)."""
    here = os.path.dirname(os.path.abspath(__file__))
    install_sh = os.environ.get("WEYLAND_INSTALL_SH") or os.path.join(here, "install.sh")
    if not os.path.isfile(install_sh):
        return False
    env = dict(os.environ)
    env["WEYLAND_REUSE_DASHBOARD"] = "1"
    env["WEYLAND_NONCE"] = NONCE
    env["WEYLAND_STATE_DIR"] = STATE_DIR
    env["WEYLAND_DASH_PORT"] = str(PORT)
    try:
        subprocess.Popen(["bash", install_sh], env=env, cwd=here,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def do_restart() -> None:
    # The dying bootstrap's EXIT trap runs phase_dashboard_stop, which reads
    # dashboard.pid and SIGTERMs it — i.e. it would kill US. Hide the pid file
    # while we tear the bootstrap down, then restore it (pointing at this very
    # process) so the relaunched bootstrap sees the dashboard as live.
    try:
        os.remove(DASH_PID_FILE)
    except OSError:
        pass
    _kill_bootstrap()
    try:
        os.remove(IDENTITY_FILE)
    except OSError:
        pass
    _clear_identity_env()
    _reset_state_identity()
    try:
        with open(DASH_PID_FILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    _relaunch_bootstrap()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def _touch(self):
        _last[0] = time.time()

    def _nonce_ok(self) -> bool:
        q = parse_qs(urlparse(self.path).query)
        return bool(NONCE) and q.get("k", [""])[0] == NONCE

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body_form(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        return parse_qs(raw)

    def do_GET(self):
        self._touch()
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/state":
            if not self._nonce_ok():
                self._send(403, b'{"error":"nonce"}', "application/json")
            else:
                self._send(200, state_with_flags(), "application/json")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        self._touch()
        path = urlparse(self.path).path
        if not self._nonce_ok():
            self._send(403, b"nonce")
            return
        if path == "/identity":
            ok = write_identity(self._body_form())
            self._send(200 if ok else 400, b"ok" if ok else b"invalid name")
        elif path == "/save-pat":
            pat = (self._body_form().get("pat", [""])[0] or "").strip()
            if not PAT_RE.match(pat):
                self._send(400, b"invalid PAT prefix")
                return
            ok = write_pat(pat)
            self._send(200 if ok else 500, b"ok" if ok else b"write failed")
        elif path == "/restart":
            do_restart()
            self._send(200, b"restarting")
        elif path == "/done":
            self._send(200, b"sealed")
            threading.Thread(target=self._shutdown, daemon=True).start()
        else:
            self._send(404, b"not found")

    def _shutdown(self):
        time.sleep(0.3)
        if _srv[0]:
            _srv[0].shutdown()


def idle_watch():
    while True:
        time.sleep(15)
        if time.time() - _last[0] > IDLE_TIMEOUT and _srv[0]:
            _srv[0].shutdown()
            return


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    _srv[0] = srv
    threading.Thread(target=idle_watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ──────────────────────────────────────────────────────────────────────────
# The wizard HTML — forge-steel (Thor/Conan) palette, browser-first identity.
# ──────────────────────────────────────────────────────────────────────────
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>weyland · the rite of binding</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700&family=Cinzel+Decorative:wght@700&display=swap" rel="stylesheet">
<style>
  :root{
    --bg:#252c36; --steel:#2d3540; --steel2:#323d4a;
    --flame:#e8750a; --flame-bright:#ff8c1a; --flame-deep:#b8560a;
    --blood:#c0392b; --blood-border:#9b2020; --blood-bg:#2a0a0a; --blood-bg2:#1f0808;
    --steelblue:#5a93bd; --ink:#f0e6cc; --muted:#b0a080; --leather:#9a8a6a;
    --line:#4a5568; --line2:#4f5b6e; --gold:#d4a017; --gold-deep:#c9980f;
    --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
    --cinzel:"Cinzel","Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --grain:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.5'/%3E%3C/svg%3E");
  }
  *{box-sizing:border-box} html,body{margin:0; height:100%}
  body{background:var(--bg); color:var(--ink); font-family:var(--sans); -webkit-font-smoothing:antialiased; overflow:hidden;
    background-image:radial-gradient(120% 80% at 50% -12%, #3a4656, transparent 60%),radial-gradient(100% 60% at 50% 118%, #2a333f, transparent 62%),linear-gradient(180deg,#2a323d,#1e242c); background-attachment:fixed;}
  body::before{content:""; position:fixed; inset:0; z-index:60; pointer-events:none; background-image:var(--grain); background-size:150px 150px; opacity:.05; mix-blend-mode:overlay}
  body::after{content:""; position:fixed; left:0; right:0; bottom:0; height:30vh; z-index:1; pointer-events:none; background:linear-gradient(to top,#e8750a16,transparent); filter:blur(3px)}
  .app{position:relative; z-index:2; height:100dvh; display:flex; flex-direction:column; max-width:720px; margin:0 auto; padding:10px 16px 16px}
  body.details-open{overflow:auto} body.details-open .app{height:auto; min-height:100dvh}
  .topbar{display:flex; align-items:center; justify-content:flex-start; gap:10px; flex:0 0 auto}
  .sigil{font-family:var(--mono); font-size:10.5px; letter-spacing:.24em; color:var(--flame); opacity:.9}
  .sigil b{color:var(--ink); opacity:.65}
  .hero{flex:0 0 auto; text-align:center; padding:8px 0 4px}
  /* eyebrow — Cinzel 400, widely spaced, with a subtle 1-2px raise */
  .eyebrow{margin:0 0 7px; font-family:var(--cinzel); font-weight:400; font-size:11px; letter-spacing:.34em; text-transform:uppercase; color:var(--flame); text-shadow:0 1px 0 #7a3c08,0 2px 2px rgba(0,0,0,.45)}
  body[data-state="complete"] .eyebrow{color:var(--gold); text-shadow:0 1px 0 #6e5212,0 2px 2px rgba(0,0,0,.45)}
  .plate{display:inline-block; max-width:100%; padding:10px 24px; border-radius:8px; border:1px solid var(--line2); background:linear-gradient(180deg,#36414f,#262d38); box-shadow:0 0 0 1px #11161c inset,0 0 48px #e8750a22,0 14px 38px #0008,0 1px 0 #6a7686aa inset}
  /* hero Pi name — hot iron letters STAMPED into cold steel: Cinzel 700, a
     parchment->ember gradient fill, and a layered text-shadow that raises the
     glyphs 3-4px off the plate. */
  .name{margin:0; font-family:var(--cinzel); font-weight:700; letter-spacing:.1em; line-height:1.05; font-size:clamp(22px,6.2vw,40px); word-break:break-word; color:#f3b06a; text-shadow:0 1px 0 #ff9040,0 2px 0 #c45a08,0 3px 0 #8a3a05,0 4px 0 #5a2503,0 5px 8px rgba(0,0,0,.6),0 0 26px #e8750a44}
  @supports ((-webkit-background-clip:text) or (background-clip:text)){
    .name{background:linear-gradient(180deg,#fff3e0 2%,#ff8c1a 50%,#c45a08 94%); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; color:transparent}
    body[data-state="complete"] .name{background:linear-gradient(180deg,#fff6dd 2%,#d4a017 55%,#a07a10 96%); -webkit-background-clip:text; background-clip:text}
  }
  .name.unnamed{opacity:.55; letter-spacing:.24em}
  .subtitle{margin:7px 0 0; font-family:var(--serif); font-style:italic; font-size:13px; color:var(--ink)}
  .glyph{font-style:normal; display:inline-block; margin-right:9px; color:var(--flame); text-shadow:0 0 12px var(--flame); animation:emberpulse 2.1s ease-in-out infinite}
  body[data-state="complete"] .glyph{color:var(--gold); text-shadow:0 0 12px var(--gold)}
  @keyframes emberpulse{0%,100%{opacity:1; text-shadow:0 0 14px var(--flame)}50%{opacity:.45; text-shadow:0 0 5px var(--flame)}}
  .stage{flex:1 1 0; min-height:0; overflow:auto; margin-top:8px}
  .forge-form{border:1px solid var(--line2); border-radius:12px; background:linear-gradient(180deg,var(--steel2),var(--steel)); padding:14px; position:relative; overflow:hidden}
  .forge-form::after{content:""; position:absolute; inset:0; background-image:var(--grain); background-size:150px 150px; opacity:.05; mix-blend-mode:overlay; pointer-events:none}
  .forge-form h3{margin:0 0 3px; font-family:var(--cinzel); font-weight:700; font-size:16px; letter-spacing:.05em; text-transform:uppercase; color:var(--flame-bright); text-shadow:0 1px 0 #7a3c08,0 0 16px #e8750a44}
  .forge-form .lead{margin:0 0 9px; font-family:var(--serif); font-style:italic; font-size:12.5px; color:var(--leather)}
  .frow{margin:7px 0; position:relative; z-index:1}
  .frow label{display:block; font-family:var(--mono); font-size:9.5px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); margin:0 0 4px}
  .frow label .opt{color:var(--steelblue); letter-spacing:.1em}
  .frow input{width:100%; font-family:var(--mono); font-size:13.5px; color:var(--ink); background:#1a212a; border:1px solid var(--line2); border-radius:8px; padding:8px 11px}
  .frow input::placeholder{color:#6f6753}
  .frow input:focus{border-color:var(--flame); box-shadow:0 0 0 1px var(--flame),0 0 16px #e8750a33; outline:none}
  .frow textarea{width:100%; font-family:var(--mono); font-size:12.5px; color:var(--ink); background:#1a212a; border:1px solid var(--line2); border-radius:8px; padding:10px 12px; resize:vertical; line-height:1.4; white-space:pre; overflow-x:auto}
  .frow textarea::placeholder{color:#6f6753}
  .frow textarea:focus{border-color:var(--flame); box-shadow:0 0 0 1px var(--flame),0 0 16px #e8750a33; outline:none}
  .pair{display:grid; grid-template-columns:1fr 1fr; gap:9px} @media (max-width:340px){.pair{grid-template-columns:1fr}}
  .fmsg{font-family:var(--mono); font-size:11px; letter-spacing:.08em; margin:10px 0 0; min-height:14px; color:#e88}
  /* SSH access section (skip / use existing / generate) */
  .ssh-sec>label{display:block; font-family:var(--mono); font-size:9.5px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); margin:0 0 6px}
  .ssh-opts{display:flex; flex-direction:column; gap:5px}
  .ssh-opt{display:flex; align-items:baseline; gap:9px; font-family:var(--serif); font-size:13.5px; color:var(--ink); cursor:pointer; padding:5px 11px; border:1px solid var(--line2); border-radius:9px; background:#1a212a}
  .ssh-opt:hover{border-color:var(--flame)}
  .ssh-opt input{accent-color:var(--flame); margin:0}
  .ssh-opt b{font-weight:700}
  .ssh-od{font-family:var(--mono); font-size:10px; letter-spacing:.06em; text-transform:uppercase; color:var(--leather); margin-left:auto}
  .ssh-panel{margin-top:11px; padding:13px; border:1px solid var(--line2); border-radius:10px; background:linear-gradient(180deg,var(--steel2),var(--steel))}
  .btn-ghost{-webkit-appearance:none; appearance:none; cursor:pointer; font-family:var(--mono); font-size:12px; letter-spacing:.06em; color:var(--flame-bright); background:#11161c; border:1px solid var(--flame); border-radius:9px; padding:11px 15px; text-align:center}
  .btn-ghost:hover{background:#1c1206} .btn-ghost:active{transform:translateY(1px)} .btn-ghost[disabled]{opacity:.5; cursor:default}
  .btn-ghost.dl{display:inline-flex; flex-direction:column; gap:2px; min-width:170px}
  .dlsub{font-size:9.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--leather)}
  .ssh-hint{font-family:var(--serif); font-style:italic; font-size:12.5px; color:var(--leather); margin:11px 0 7px}
  .chips{display:flex; flex-wrap:wrap; gap:7px}
  .chip{font-family:var(--mono); font-size:10.5px; color:var(--ink); background:#11161c; border:1px dashed var(--line); border-radius:7px; padding:6px 9px; cursor:pointer; max-width:100%; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .chip:hover{border-color:var(--flame); color:var(--flame-bright)} .chip.ok{border-style:solid; border-color:#6e5212; color:var(--gold)}
  .ssh-status{font-family:var(--mono); font-size:11px; letter-spacing:.04em; margin:9px 0 0; min-height:13px; color:var(--muted)}
  .ssh-status.ok{color:var(--gold)} .ssh-status.err{color:#e88}
  .ssh-warn{font-family:var(--mono); font-size:11px; letter-spacing:.04em; color:#ffcf8a; background:#241a08; border:1px solid #5a4a20; border-radius:8px; padding:8px 10px; margin:0 0 11px}
  .dlrow{display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin:0 0 6px}
  /* post-download "now move it here" instruction, revealed once a download fires */
  .dl-after{margin:0 0 12px; padding-left:2px}
  .dl-after .ssh-hint{margin:0 0 6px}
  .howto{margin-top:6px; border-top:1px solid var(--line2); padding-top:11px}
  .tabs{display:flex; gap:6px; margin-bottom:9px}
  .tab{-webkit-appearance:none; appearance:none; cursor:pointer; font-family:var(--mono); font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--muted); background:#11161c; border:1px solid var(--line2); border-radius:7px; padding:6px 11px}
  .tab[aria-pressed="true"]{color:#180c02; background:linear-gradient(180deg,var(--flame-bright),var(--flame-deep)); border-color:var(--flame); font-weight:700}
  .tabpane{font-family:var(--serif); font-size:13px; color:var(--ink)}
  .tabpane code{font-family:var(--mono); font-size:12px; color:var(--flame-bright); background:#11161c; border:1px solid var(--line2); border-radius:6px; padding:2px 7px; display:inline-block; margin-bottom:5px}
  .tabpane p{margin:5px 0 0; color:var(--leather); font-size:12.5px}
  .roster{list-style:none; margin:0; padding:0}
  .roster li{display:flex; flex-wrap:wrap; align-items:center; gap:11px; padding:4px 6px; border-bottom:1px solid #2e3744}
  .roster li:last-child{border-bottom:0}
  .badge{flex:0 0 auto; width:18px; height:18px; border-radius:5px; transform:rotate(45deg); display:grid; place-items:center; border:1px solid}
  .badge span{transform:rotate(-45deg); font-size:9.5px; font-weight:700; line-height:1}
  .badge.done{background:#2b2410; color:var(--gold); border-color:#6e5212; box-shadow:0 0 12px #d4a01726}
  .badge.run{background:#2e1a06; color:var(--flame-bright); border-color:var(--flame); box-shadow:0 0 16px #e8750a66; animation:anvil 1.4s ease-in-out infinite}
  .badge.pend{background:#252d38; color:var(--leather); border-color:#414c5c}
  .badge.error{background:#2a0a0a; color:#ff6f61; border-color:#7a261c}
  @keyframes anvil{0%,100%{box-shadow:0 0 7px #e8750a44}50%{box-shadow:0 0 22px #e8750aaa}}
  .roster .label{flex:1; font-family:var(--cinzel); font-weight:400; font-size:14px; letter-spacing:.01em}
  li.is-pend .label{color:var(--muted)} li.is-done .label{color:var(--ink)} li.is-error .label{color:#ffb3a8}
  li.is-run .label{color:#ffd9a0; text-shadow:0 0 10px #e8750a44}
  .stamp{font-family:var(--mono); font-size:9.5px; letter-spacing:.16em; text-transform:uppercase; text-align:right}
  li.is-done .stamp{color:var(--gold)} li.is-run .stamp{color:var(--flame-bright)} li.is-pend .stamp{color:var(--leather)} li.is-error .stamp{color:#ff6f61}
  /* active phase row: a forge-glow sweep so progress is obvious even with no auth card */
  .roster li.is-run{background:linear-gradient(90deg,#e8750a05,#e8750a22,#e8750a05); background-size:220% 100%; animation:forgesweep 2.6s linear infinite; border-radius:6px}
  @keyframes forgesweep{0%{background-position:120% 0}100%{background-position:-120% 0}}
  /* live progress sub-line on the RUNNING row — a plain-English DEBUG window
     (real tool names, not mythology); removed the instant the phase completes */
  .subline{flex:0 0 100%; margin:1px 0 2px 29px; display:flex; align-items:center; gap:9px;
    font-family:var(--mono); font-size:11px; letter-spacing:.02em; color:var(--muted); min-height:14px}
  .sub-text{white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; flex:0 1 auto}
  .sub-bar{flex:0 0 88px; height:5px; background:#1a212a; border:1px solid var(--line); border-radius:3px; overflow:hidden}
  .sub-bar i{display:block; height:100%; width:0; background:linear-gradient(90deg,var(--flame),var(--flame-bright)); transition:width .35s ease}
  .sub-pct{flex:0 0 auto; color:var(--flame-bright)}
  .sub-dots::after{content:""; animation:subdots 1.3s steps(1,end) infinite}
  @keyframes subdots{0%{content:""}25%{content:"."}50%{content:".."}75%{content:"..."}100%{content:""}}
  /* continue/restart choice screen — shown FIRST when a partial run is found */
  .choice{display:none; flex-direction:column; gap:14px; margin-top:6px}
  .choice .clead{font-family:var(--serif); font-style:italic; font-size:14.5px; color:var(--leather); margin:0 0 2px}
  .choice .cbtns{display:flex; gap:13px; flex-wrap:wrap}
  .cbtn{flex:1 1 220px; text-align:center; cursor:pointer; border-radius:12px; padding:18px 16px;
    font-family:var(--cinzel); font-weight:700; font-size:16px; letter-spacing:.04em; text-transform:uppercase; border:1px solid}
  .cbtn .csub{display:block; margin-top:5px; font-family:var(--mono); font-weight:400; font-size:10.5px;
    letter-spacing:.12em; text-transform:uppercase; opacity:.8}
  .cbtn-go{color:#ffd9a0; border-color:var(--flame); background:linear-gradient(180deg,#2e1a06,#1c1408);
    box-shadow:0 0 0 1px #0b0405 inset,0 0 22px #e8750a33}
  .cbtn-go:hover{box-shadow:0 0 0 1px #0b0405 inset,0 0 32px #e8750a66}
  .cbtn-again{color:#e8b0a4; border-color:var(--blood-border); background:linear-gradient(180deg,#1a0c0c,#140a0a)}
  .cbtn-again:hover{color:#ff8c7a; border-color:#a3392c}
  /* top-of-page activity bar — visible whenever a phase runs and no auth card is up */
  #forgebar{position:fixed; top:0; left:0; right:0; height:3px; z-index:70; display:none;
    background:linear-gradient(90deg,transparent,#e8750a,#ff8c1a,#e8750a,transparent); background-size:45% 100%;
    animation:barslide 1.5s linear infinite; box-shadow:0 0 10px #e8750a66}
  @keyframes barslide{0%{background-position:-45% 0}100%{background-position:145% 0}}
  body.forge-active #forgebar{display:block}
  /* auth card 'awaiting the gate' state — URL not captured yet */
  .awaiting{flex:1 1 auto; min-width:200px; display:flex; align-items:center; justify-content:center; gap:10px;
    font-family:var(--mono); font-size:12px; letter-spacing:.14em; text-transform:uppercase; color:#e8b0a4;
    border:1px dashed var(--blood-border); border-radius:9px; padding:13px 18px; animation:awaitpulse 1.6s ease-in-out infinite}
  @keyframes awaitpulse{0%,100%{opacity:1}50%{opacity:.5}}
  .authcard{flex:0 0 auto; margin-top:12px; border:1px solid var(--blood-border); border-radius:12px; background:linear-gradient(180deg,var(--blood-bg),var(--blood-bg2)); padding:16px 16px 15px; position:relative; overflow:hidden; box-shadow:0 0 0 1px #0b0405 inset,0 0 30px #8b1a1a55; animation:bloodglow 2.4s ease-in-out infinite}
  .authcard::after{content:""; position:absolute; inset:0; background-image:var(--grain); background-size:150px 150px; opacity:.06; mix-blend-mode:overlay; pointer-events:none}
  @keyframes bloodglow{0%,100%{box-shadow:0 0 0 1px #0b0405 inset,0 0 18px #8b1a1a44}50%{box-shadow:0 0 0 1px #0b0405 inset,0 0 40px #c0392b88}}
  .authcard h3{margin:0; font-family:var(--cinzel); font-weight:700; font-size:18px; letter-spacing:.04em; text-transform:uppercase; color:#ff5a4a; text-shadow:0 1px 0 #7a1410,0 2px 3px rgba(0,0,0,.5),0 0 18px #c0392b66}
  .authcard .sub{margin:6px 0 15px; font-family:var(--serif); font-style:italic; font-size:13.5px; color:#e8b0a4}
  .authrow{display:flex; gap:11px; align-items:stretch; flex-wrap:wrap}
  .rune{flex:0 0 auto; font-family:var(--mono); font-size:23px; font-weight:700; letter-spacing:.22em; color:var(--ink); background:#160404; border:1px solid var(--blood); border-radius:8px; padding:8px 16px; text-shadow:0 0 14px #c0392b66; box-shadow:0 0 0 1px #000 inset,0 0 18px #8b1a1a44}
  .instr{margin:13px 0 0; font-family:var(--mono); font-size:10.5px; letter-spacing:.12em; color:#c79c92; text-transform:uppercase}
  .btn{-webkit-appearance:none; appearance:none; cursor:pointer; border:0; font-family:var(--mono); letter-spacing:.14em; text-transform:uppercase; border-radius:9px; font-size:13px; padding:14px 18px; text-align:center; text-decoration:none; display:inline-block}
  .btn-fire{background:linear-gradient(180deg,#ff9024,#c45a08); color:#180c02; font-weight:700; box-shadow:0 0 0 1px #7a3c08,0 0 24px #e8750a44,0 2px 0 #ffc78a55 inset}
  .btn-fire:hover{filter:brightness(1.07)} .btn-fire:active{transform:translateY(1px)}
  .btn-blood{background:linear-gradient(180deg,#d0432f,#8b1a1a); color:#fbe3de; font-weight:700; box-shadow:0 0 0 1px #5a1212,0 0 22px #8b1a1a55; flex:1 1 auto; min-width:200px}
  .btn-block{width:100%; margin-top:11px; padding-top:11px; padding-bottom:11px}
  /* Page 1 "Next" is a subordinate STEP — muted steel outline, not the flame
     btn-fire. SUBJUGATE on page 2 keeps btn-fire and reads as the commitment. */
  .btn-outline{background:#1a212a; color:var(--flame-bright); border:1px solid var(--flame); box-shadow:none; font-weight:400}
  .btn-outline:hover{background:#1c1206; color:#ffb45a} .btn-outline:active{transform:translateY(1px)}
  /* page-1/2 split: small unobtrusive Back link (matches the 'optional' labels) */
  .backlink{display:inline-block; margin-top:8px; font-family:var(--mono); font-size:10.5px; letter-spacing:.14em; text-transform:uppercase; color:var(--muted); text-decoration:none; cursor:pointer}
  .backlink:hover{color:var(--flame-bright)}
  .copy{font-family:var(--mono); font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--ink); background:#323d4a; border:1px solid var(--line2); border-radius:8px; padding:0 14px; cursor:pointer}
  .copy:hover{border-color:var(--flame); color:var(--flame-bright)} .copy:active{transform:translateY(1px)} .copy.ok{color:var(--gold); border-color:#6e5212}
  .details{flex:0 0 auto; margin-top:12px}
  .disclose{width:100%; text-align:center; background:#283039; color:var(--muted); border:1px solid var(--line2); border-radius:9px; padding:12px; font-family:var(--mono); font-size:11.5px; letter-spacing:.2em; text-transform:uppercase; cursor:pointer}
  .disclose:hover{color:var(--flame-bright); border-color:var(--flame)}
  /* "Start over" — small, unobtrusive; a quiet escape hatch, not a CTA */
  .restartbar{display:none; text-align:center; margin:16px 0 2px}
  .startover{background:none; border:0; color:var(--muted); font-family:var(--mono); font-size:10.5px; letter-spacing:.18em; text-transform:uppercase; cursor:pointer; opacity:.55; padding:6px 10px; transition:opacity .15s,color .15s}
  .startover:hover{opacity:1; color:#e08a7a}
  .startover[disabled]{cursor:default; opacity:.4}
  .panel{display:none; border:1px solid var(--line2); border-top:0; border-radius:0 0 12px 12px; background:linear-gradient(180deg,var(--steel2),var(--steel)); padding:16px; margin-top:-6px}
  body.details-open .panel{display:block} body.details-open .disclose{border-radius:12px 12px 0 0; color:var(--flame-bright); border-color:var(--flame)}
  .field{margin:12px 0} .field:first-child{margin-top:0}
  .field label{display:block; font-family:var(--mono); font-size:10.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); margin:0 0 6px}
  .copybox{display:flex; gap:8px; align-items:stretch}
  .val{flex:1; min-width:0; font-family:var(--mono); font-size:13px; color:var(--ink); background:#1a212a; border:1px solid var(--line2); border-radius:8px; padding:10px 12px; overflow-x:auto; white-space:nowrap}
  a.val{display:block; text-decoration:none; color:var(--flame-bright)} a.val:hover{text-decoration:underline}
  textarea.val{white-space:pre-wrap; word-break:break-word; height:104px; resize:vertical; line-height:1.45; width:100%}
  input.val{width:100%}
  .talisman{border:1px solid var(--line2); background:#1f1810; border-radius:10px; padding:14px; margin-top:4px}
  .warn{font-size:12.5px; color:#e8c79a; background:#241a08; border:1px solid #5a4a20; border-radius:8px; padding:9px 11px; margin:0 0 12px}
  .warn b{color:var(--flame-bright)}
  .patmsg{font-family:var(--mono); font-size:11px; margin:8px 0 0; min-height:14px} .patmsg.ok{color:var(--gold)} .patmsg.err{color:#ff6f61}
  .seal{margin-top:14px; width:100%; font-family:var(--cinzel); font-weight:700; letter-spacing:.08em}
  .gate{flex:1 1 auto; display:grid; place-items:center; text-align:center; font-family:var(--serif); font-style:italic; color:var(--muted); font-size:15px; padding:20px}
  :focus-visible{outline:2px solid var(--flame); outline-offset:2px; border-radius:6px}
  @media (prefers-reduced-motion:reduce){*{animation:none !important; transition:none !important}}
</style>
</head>
<body data-state="identity">
  <div id="forgebar"></div>
  <div class="app">
    <div class="topbar"><span class="sigil">&#9874; <b>WEYLAND</b> &middot; DIVINE SMITH &middot; FLEET BOUND</span></div>
    <header class="hero">
      <p class="eyebrow" id="eyebrow">The rite of binding awaits</p>
      <div class="plate"><p class="name unnamed" id="piname">UNNAMED</p></div>
      <p class="subtitle"><span class="glyph">&#9672;</span><span id="subtext"></span></p>
    </header>

    <div class="stage" id="stage">
      <section class="forge-form" id="form" aria-label="Name the minion">
        <!-- Page 1 — essentials. Next validates the name, then reveals page 2. -->
        <div class="form-page" id="form-p1">
          <h3>NAME THE MINION</h3>
          <p class="lead">speak the minion's name and domain, my Lord — the rite cannot begin without it</p>
          <div class="frow"><label for="i-name">Minion name</label>
            <input id="i-name" type="text" autocomplete="off" spellcheck="false" placeholder="e.g. inkypi"></div>
          <div class="frow"><label for="i-domain">Domain</label>
            <input id="i-domain" type="text" autocomplete="off" spellcheck="false" placeholder="julianburton.com"></div>
          <button class="btn btn-outline btn-block" type="button" id="form-next">Next &rarr;</button>
          <p class="fmsg" id="fmsg1"></p>
        </div>
        <!-- Page 2 — optional extras. Values persist across Back (hidden, not reset). -->
        <div class="form-page" id="form-p2" style="display:none">
        <h3>ARM THE MINION</h3>
        <p class="lead">optional rites, my Lord — leave any blank to skip it</p>
        <div class="pair">
          <div class="frow"><label for="i-pw">Change Pi password <span class="opt">&middot; optional</span></label>
            <input id="i-pw" type="password" autocomplete="new-password" placeholder="blank = keep current"></div>
          <div class="frow"><label for="i-pw2">Confirm password</label>
            <input id="i-pw2" type="password" autocomplete="new-password" placeholder="re-enter to confirm"></div>
        </div>
        <div class="pair">
          <div class="frow"><label for="i-pc">PC wake hostname <span class="opt">&middot; optional</span></label>
            <input id="i-pc" type="text" autocomplete="off" spellcheck="false" placeholder="ju-laptop.tail875649.ts.net"></div>
          <div class="frow"><label for="i-tok">Wake token <span class="opt">&middot; optional</span></label>
            <input id="i-tok" type="text" autocomplete="off" spellcheck="false" placeholder="X-Wake-Token"></div>
        </div>
        <button class="btn btn-outline btn-block" type="button" id="form-next2">Next &rarr;</button>
        <p class="fmsg" id="fmsg2"></p>
        <a class="backlink" id="form-back" href="#" role="button">&larr; back to the minion's name</a>
        </div>
        <!-- Page 3 — SSH access + the final commit. -->
        <div class="form-page" id="form-p3" style="display:none">
        <h3>OPEN THE GATE</h3>
        <p class="lead">optional — set up SSH, then seal the binding</p>
        <div class="frow ssh-sec">
          <label>SSH access <span class="opt">&middot; optional &middot; password login stays enabled either way</span></label>
          <div class="ssh-opts">
            <label class="ssh-opt"><input type="radio" name="sshmode" value="none" checked> <b>Skip</b> <span class="ssh-od">no SSH changes</span></label>
            <label class="ssh-opt"><input type="radio" name="sshmode" value="existing"> <b>Use existing key</b> <span class="ssh-od">pick your .pub file</span></label>
            <label class="ssh-opt"><input type="radio" name="sshmode" value="generate"> <b>Generate new key</b> <span class="ssh-od">made in your browser</span></label>
          </div>

          <div class="ssh-panel" id="ssh-existing" style="display:none">
            <button class="btn-ghost" type="button" id="ssh-pick">Choose public-key file&hellip;</button>
            <input type="file" id="ssh-file" accept=".pub,text/plain" style="display:none">
            <p class="ssh-hint">your key usually lives here &mdash; click to copy the path, then paste it into the file picker:</p>
            <div class="chips">
              <button class="chip" type="button" data-copy-text="C:\Users\&lt;name&gt;\.ssh\id_rsa.pub">Windows: C:\Users\&lt;name&gt;\.ssh\id_rsa.pub</button>
              <button class="chip" type="button" data-copy-text="~/.ssh/id_rsa.pub">Mac: ~/.ssh/id_rsa.pub</button>
            </div>
            <p class="ssh-status" id="ssh-existing-status"></p>
          </div>

          <div class="ssh-panel" id="ssh-generate" style="display:none">
            <button class="btn-ghost" type="button" id="ssh-gen">&#9881; Generate key</button>
            <div id="ssh-gen-result" style="display:none">
              <p class="ssh-warn">&#9888; Save these keys now &mdash; you will not see them again.</p>
              <div class="dlrow">
                <button class="btn-ghost dl" type="button" id="dl-openssh">&#8595; id_ed25519 <span class="dlsub">Mac / PowerShell</span></button>
              </div>
              <div class="dl-after" id="after-openssh" style="display:none">
                <p class="ssh-hint">saved to your Downloads &mdash; now move it to your SSH folder (click a path to copy):</p>
                <div class="chips">
                  <button class="chip" type="button" data-copy-text="~/.ssh/id_ed25519">Mac: ~/.ssh/id_ed25519</button>
                  <button class="chip" type="button" data-copy-text="C:\Users\&lt;name&gt;\.ssh\id_ed25519">Windows: C:\Users\&lt;name&gt;\.ssh\id_ed25519</button>
                </div>
              </div>
              <div class="dlrow">
                <button class="btn-ghost dl" type="button" id="dl-ppk">&#8595; id_ed25519.ppk <span class="dlsub">PuTTY (no PuTTYgen)</span></button>
              </div>
              <div class="dl-after" id="after-ppk" style="display:none">
                <p class="ssh-hint">saved to your Downloads &mdash; keep it anywhere, just remember where (PuTTY browses to it when you connect).</p>
              </div>
              <div class="howto">
                <div class="tabs">
                  <button class="tab" type="button" data-tab="mac" aria-pressed="true">Mac</button>
                  <button class="tab" type="button" data-tab="win" aria-pressed="false">Windows</button>
                  <button class="tab" type="button" data-tab="putty" aria-pressed="false">PuTTY</button>
                </div>
                <div class="tabpane" data-pane="mac"><code>ssh admin@<span class="howto-name">minion</span>.local</code><p>save <b>id_ed25519</b> to <code>~/.ssh/</code> &mdash; it's picked up automatically.</p></div>
                <div class="tabpane" data-pane="win" style="display:none"><code>ssh admin@<span class="howto-name">minion</span>.local</code><p>save <b>id_ed25519</b> to <code>%USERPROFILE%\.ssh\</code> &mdash; PowerShell uses it automatically.</p></div>
                <div class="tabpane" data-pane="putty" style="display:none"><p>PuTTY &rarr; Connection &rarr; SSH &rarr; Auth &rarr; <i>browse to the .ppk file</i>. Host: <code>admin@<span class="howto-name">minion</span>.local</code></p></div>
              </div>
            </div>
            <p class="ssh-status" id="ssh-gen-status"></p>
          </div>
        </div>
        <button class="btn btn-fire btn-block" type="button" id="begin">SUBJUGATE &rarr;</button>
        <p class="fmsg" id="fmsg3"></p>
        <a class="backlink" id="form-back2" href="#" role="button">&larr; back</a>
        </div>
      </section>
      <ul class="roster" id="roster" style="display:none"></ul>
    </div>

    <section class="choice" id="choice" aria-label="Continue or begin again">
      <p class="clead">a previous binding was begun &mdash; choose your path, my Lord</p>
      <div class="cbtns">
        <div class="cbtn cbtn-go" id="choice-continue" role="button" tabindex="0">CONTINUE THE RITE &rarr;<span class="csub">resume where it left off</span></div>
        <div class="cbtn cbtn-again" id="choice-again" role="button" tabindex="0">BEGIN AGAIN &#8635;<span class="csub">reset &amp; name the minion anew</span></div>
      </div>
    </section>

    <section class="authcard" id="authcard" style="display:none" aria-label="The forge demands a blood oath">
      <h3 id="auth-title"></h3>
      <p class="sub" id="auth-sub"></p>
      <div class="authrow">
        <a class="btn btn-blood" id="auth-btn" href="#" target="_blank" rel="noopener">Swear the oath &rarr;</a>
        <span class="awaiting" id="auth-wait" style="display:none">&#9672; awaiting the gate&hellip;</span>
        <code class="rune" id="auth-code" style="display:none"></code>
        <button class="copy" id="auth-copy" data-copy="auth-code" type="button" style="display:none">Copy</button>
      </div>
      <p class="instr" id="auth-instr"></p>
    </section>

    <div class="details" id="details" style="display:none">
      <button class="disclose" id="disclose" type="button" aria-expanded="false" aria-controls="panel">Consult the talisman &#9662;</button>
      <section class="panel" id="panel" aria-label="Connector details">
        <div class="field"><label for="f-url">MCP URL</label><div class="copybox"><code class="val" id="f-url"></code><button class="copy" data-copy="f-url" type="button">Copy</button></div></div>
        <div class="pair">
          <div class="field"><label for="f-cid">OAuth Client ID</label><div class="copybox"><code class="val" id="f-cid"></code><button class="copy" data-copy="f-cid" type="button">Copy</button></div></div>
          <div class="field"><label>OAuth Client Secret</label><code class="val" style="color:var(--muted)">&mdash; leave blank (PKCE) &mdash;</code></div>
        </div>
        <div class="field"><label for="f-bearer">Bearer token &mdash; speak it once into the consent page</label><div class="copybox"><code class="val" id="f-bearer"></code><button class="copy" data-copy="f-bearer" type="button">Copy</button></div></div>
        <div class="pair">
          <div class="field"><label for="f-ct">Consent &mdash; tunnel</label><div class="copybox"><a class="val" id="f-ct" href="#" target="_blank" rel="noopener"></a><button class="copy" data-copy="f-ct" type="button">Copy</button></div></div>
          <div class="field"><label for="f-cl">Consent &mdash; local</label><div class="copybox"><a class="val" id="f-cl" href="#" target="_blank" rel="noopener"></a><button class="copy" data-copy="f-cl" type="button">Copy</button></div></div>
        </div>
        <div class="field"><label for="f-repo">Per-Pi repo (the chronicles)</label><div class="copybox"><a class="val" id="f-repo" href="#" target="_blank" rel="noopener"></a><button class="copy" data-copy="f-repo" type="button">Copy</button></div></div>
        <div class="field"><label for="f-proj">Project instructions</label><div class="copybox"><textarea class="val" id="f-proj" readonly></textarea><button class="copy" data-copy="f-proj" type="button">Copy</button></div></div>
        <div class="talisman">
          <p class="warn"><b>WEYLAND_PAT &mdash; permanent &amp; shared across all minions.</b> Normally drawn from the private <code>weyland-pat</code> gist; offer it here only to override.</p>
          <div class="field" style="margin:0"><label for="f-pat">Fine-grained PAT</label><input class="val" id="f-pat" type="password" autocomplete="off" placeholder="github_pat_&hellip; (blank = vault gist)"></div>
          <button class="copy" id="patsave" type="button" style="margin-top:10px; padding:9px 14px">Offer the talisman</button>
          <p class="patmsg" id="patmsg"></p>
        </div>
        <button class="btn btn-fire seal" type="button" id="seal">&#9874; The rite is complete &mdash; bind the minion</button>
      </section>
    </div>

    <div class="gate" id="gate" style="display:none">open the link the forge revealed to you, my Lord &mdash; it bears the key to this rite</div>

    <div class="restartbar" id="restartbar">
      <button class="startover" id="startover" type="button">&#8635; Start over &mdash; unname the minion</button>
    </div>
  </div>

<script>!function(i){"use strict";var m=function(r,n){this.hi=0|r,this.lo=0|n},v=function(r){var n,e=new Float64Array(16);if(r)for(n=0;n<r.length;n++)e[n]=r[n];return e},a=function(){throw new Error("no PRNG")},o=new Uint8Array(16),e=new Uint8Array(32);e[0]=9;var c=v(),w=v([1]),g=v([56129,1]),y=v([30883,4953,19914,30187,55467,16705,2637,112,59544,30585,16505,36039,65139,11119,27886,20995]),l=v([61785,9906,39828,60374,45398,33411,5274,224,53552,61171,33010,6542,64743,22239,55772,9222]),t=v([54554,36645,11616,51542,42930,38181,51040,26924,56412,64982,57905,49316,21502,52590,14035,8553]),f=v([26200,26214,26214,26214,26214,26214,26214,26214,26214,26214,26214,26214,26214,26214,26214,26214]),s=v([41136,18958,6951,50414,58488,44335,6150,12099,55207,15867,153,11085,57099,20417,9344,11139]);function h(r,n){return r<<n|r>>>32-n}function b(r,n){var e=255&r[n+3];return(e=(e=e<<8|255&r[n+2])<<8|255&r[n+1])<<8|255&r[n+0]}function B(r,n){var e=r[n]<<24|r[n+1]<<16|r[n+2]<<8|r[n+3],t=r[n+4]<<24|r[n+5]<<16|r[n+6]<<8|r[n+7];return new m(e,t)}function p(r,n,e){var t;for(t=0;t<4;t++)r[n+t]=255&e,e>>>=8}function S(r,n,e){r[n]=e.hi>>24&255,r[n+1]=e.hi>>16&255,r[n+2]=e.hi>>8&255,r[n+3]=255&e.hi,r[n+4]=e.lo>>24&255,r[n+5]=e.lo>>16&255,r[n+6]=e.lo>>8&255,r[n+7]=255&e.lo}function u(r,n,e,t,o){var i,a=0;for(i=0;i<o;i++)a|=r[n+i]^e[t+i];return(1&a-1>>>8)-1}function A(r,n,e,t){return u(r,n,e,t,16)}function _(r,n,e,t){return u(r,n,e,t,32)}function U(r,n,e,t,o){var i,a,f,u=new Uint32Array(16),c=new Uint32Array(16),w=new Uint32Array(16),y=new Uint32Array(4);for(i=0;i<4;i++)c[5*i]=b(t,4*i),c[1+i]=b(e,4*i),c[6+i]=b(n,4*i),c[11+i]=b(e,16+4*i);for(i=0;i<16;i++)w[i]=c[i];for(i=0;i<20;i++){for(a=0;a<4;a++){for(f=0;f<4;f++)y[f]=c[(5*a+4*f)%16];for(y[1]^=h(y[0]+y[3]|0,7),y[2]^=h(y[1]+y[0]|0,9),y[3]^=h(y[2]+y[1]|0,13),y[0]^=h(y[3]+y[2]|0,18),f=0;f<4;f++)u[4*a+(a+f)%4]=y[f]}for(f=0;f<16;f++)c[f]=u[f]}if(o){for(i=0;i<16;i++)c[i]=c[i]+w[i]|0;for(i=0;i<4;i++)c[5*i]=c[5*i]-b(t,4*i)|0,c[6+i]=c[6+i]-b(n,4*i)|0;for(i=0;i<4;i++)p(r,4*i,c[5*i]),p(r,16+4*i,c[6+i])}else for(i=0;i<16;i++)p(r,4*i,c[i]+w[i]|0)}function E(r,n,e,t){U(r,n,e,t,!1)}function x(r,n,e,t){return U(r,n,e,t,!0),0}var d=new Uint8Array([101,120,112,97,110,100,32,51,50,45,98,121,116,101,32,107]);function K(r,n,e,t,o,i,a){var f,u,c=new Uint8Array(16),w=new Uint8Array(64);if(!o)return 0;for(u=0;u<16;u++)c[u]=0;for(u=0;u<8;u++)c[u]=i[u];for(;64<=o;){for(E(w,c,a,d),u=0;u<64;u++)r[n+u]=(e?e[t+u]:0)^w[u];for(f=1,u=8;u<16;u++)f=f+(255&c[u])|0,c[u]=255&f,f>>>=8;o-=64,n+=64,e&&(t+=64)}if(0<o)for(E(w,c,a,d),u=0;u<o;u++)r[n+u]=(e?e[t+u]:0)^w[u];return 0}function Y(r,n,e,t,o){return K(r,n,null,0,e,t,o)}function L(r,n,e,t,o){var i=new Uint8Array(32);return x(i,t,o,d),Y(r,n,e,t.subarray(16),i)}function T(r,n,e,t,o,i,a){var f=new Uint8Array(32);return x(f,i,a,d),K(r,n,e,t,o,i.subarray(16),f)}function k(r,n){var e,t=0;for(e=0;e<17;e++)t=t+(r[e]+n[e]|0)|0,r[e]=255&t,t>>>=8}var z=new Uint32Array([5,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,252]);function R(r,n,e,t,o,i){var a,f,u,c,w=new Uint32Array(17),y=new Uint32Array(17),l=new Uint32Array(17),s=new Uint32Array(17),h=new Uint32Array(17);for(u=0;u<17;u++)y[u]=l[u]=0;for(u=0;u<16;u++)y[u]=i[u];for(y[3]&=15,y[4]&=252,y[7]&=15,y[8]&=252,y[11]&=15,y[12]&=252,y[15]&=15;0<o;){for(u=0;u<17;u++)s[u]=0;for(u=0;u<16&&u<o;++u)s[u]=e[t+u];for(s[u]=1,t+=u,o-=u,k(l,s),f=0;f<17;f++)for(u=w[f]=0;u<17;u++)w[f]=w[f]+l[u]*(u<=f?y[f-u]:320*y[f+17-u]|0)|0;for(f=0;f<17;f++)l[f]=w[f];for(u=c=0;u<16;u++)c=c+l[u]|0,l[u]=255&c,c>>>=8;for(c=c+l[16]|0,l[16]=3&c,c=5*(c>>>2)|0,u=0;u<16;u++)c=c+l[u]|0,l[u]=255&c,c>>>=8;c=c+l[16]|0,l[16]=c}for(u=0;u<17;u++)h[u]=l[u];for(k(l,z),a=0|-(l[16]>>>7),u=0;u<17;u++)l[u]^=a&(h[u]^l[u]);for(u=0;u<16;u++)s[u]=i[u+16];for(s[16]=0,k(l,s),u=0;u<16;u++)r[n+u]=l[u];return 0}function P(r,n,e,t,o,i){var a=new Uint8Array(16);return R(a,0,e,t,o,i),A(r,n,a,0)}function M(r,n,e,t,o){var i;if(e<32)return-1;for(T(r,0,n,0,e,t,o),R(r,16,r,32,e-32,r),i=0;i<16;i++)r[i]=0;return 0}function N(r,n,e,t,o){var i,a=new Uint8Array(32);if(e<32)return-1;if(L(a,0,32,t,o),0!==P(n,16,n,32,e-32,a))return-1;for(T(r,0,n,0,e,t,o),i=0;i<32;i++)r[i]=0;return 0}function O(r,n){var e;for(e=0;e<16;e++)r[e]=0|n[e]}function C(r){var n,e;for(e=0;e<16;e++)r[e]+=65536,n=Math.floor(r[e]/65536),r[(e+1)*(e<15?1:0)]+=n-1+37*(n-1)*(15===e?1:0),r[e]-=65536*n}function F(r,n,e){for(var t,o=~(e-1),i=0;i<16;i++)t=o&(r[i]^n[i]),r[i]^=t,n[i]^=t}function Z(r,n){var e,t,o,i=v(),a=v();for(e=0;e<16;e++)a[e]=n[e];for(C(a),C(a),C(a),t=0;t<2;t++){for(i[0]=a[0]-65517,e=1;e<15;e++)i[e]=a[e]-65535-(i[e-1]>>16&1),i[e-1]&=65535;i[15]=a[15]-32767-(i[14]>>16&1),o=i[15]>>16&1,i[14]&=65535,F(a,i,1-o)}for(e=0;e<16;e++)r[2*e]=255&a[e],r[2*e+1]=a[e]>>8}function G(r,n){var e=new Uint8Array(32),t=new Uint8Array(32);return Z(e,r),Z(t,n),_(e,0,t,0)}function q(r){var n=new Uint8Array(32);return Z(n,r),1&n[0]}function D(r,n){var e;for(e=0;e<16;e++)r[e]=n[2*e]+(n[2*e+1]<<8);r[15]&=32767}function I(r,n,e){var t;for(t=0;t<16;t++)r[t]=n[t]+e[t]|0}function V(r,n,e){var t;for(t=0;t<16;t++)r[t]=n[t]-e[t]|0}function X(r,n,e){var t,o,i=new Float64Array(31);for(t=0;t<31;t++)i[t]=0;for(t=0;t<16;t++)for(o=0;o<16;o++)i[t+o]+=n[t]*e[o];for(t=0;t<15;t++)i[t]+=38*i[t+16];for(t=0;t<16;t++)r[t]=i[t];C(r),C(r)}function j(r,n){X(r,n,n)}function H(r,n){var e,t=v();for(e=0;e<16;e++)t[e]=n[e];for(e=253;0<=e;e--)j(t,t),2!==e&&4!==e&&X(t,t,n);for(e=0;e<16;e++)r[e]=t[e]}function J(r,n){var e,t=v();for(e=0;e<16;e++)t[e]=n[e];for(e=250;0<=e;e--)j(t,t),1!==e&&X(t,t,n);for(e=0;e<16;e++)r[e]=t[e]}function Q(r,n,e){var t,o,i=new Uint8Array(32),a=new Float64Array(80),f=v(),u=v(),c=v(),w=v(),y=v(),l=v();for(o=0;o<31;o++)i[o]=n[o];for(i[31]=127&n[31]|64,i[0]&=248,D(a,e),o=0;o<16;o++)u[o]=a[o],w[o]=f[o]=c[o]=0;for(f[0]=w[0]=1,o=254;0<=o;--o)F(f,u,t=i[o>>>3]>>>(7&o)&1),F(c,w,t),I(y,f,c),V(f,f,c),I(c,u,w),V(u,u,w),j(w,y),j(l,f),X(f,c,f),X(c,u,y),I(y,f,c),V(f,f,c),j(u,f),V(c,w,l),X(f,c,g),I(f,f,w),X(c,c,f),X(f,w,l),X(w,u,a),j(u,y),F(f,u,t),F(c,w,t);for(o=0;o<16;o++)a[o+16]=f[o],a[o+32]=c[o],a[o+48]=u[o],a[o+64]=w[o];var s=a.subarray(32),h=a.subarray(16);return H(s,s),X(h,h,s),Z(r,h),0}function W(r,n){return Q(r,n,e)}function $(r,n){return a(n,32),W(r,n)}function rr(r,n,e){var t=new Uint8Array(32);return Q(t,e,n),x(r,o,t,d)}var nr=M,er=N;function tr(){var r,n,e,t=0,o=0,i=0,a=0,f=65535;for(e=0;e<arguments.length;e++)t+=(r=arguments[e].lo)&f,o+=r>>>16,i+=(n=arguments[e].hi)&f,a+=n>>>16;return new m((i+=(o+=t>>>16)>>>16)&f|(a+=i>>>16)<<16,t&f|o<<16)}function or(r,n){return new m(r.hi>>>n,r.lo>>>n|r.hi<<32-n)}function ir(){var r,n=0,e=0;for(r=0;r<arguments.length;r++)n^=arguments[r].lo,e^=arguments[r].hi;return new m(e,n)}function ar(r,n){var e,t,o=32-n;return n<32?(e=r.hi>>>n|r.lo<<o,t=r.lo>>>n|r.hi<<o):n<64&&(e=r.lo>>>n|r.hi<<o,t=r.hi>>>n|r.lo<<o),new m(e,t)}var fr=[new m(1116352408,3609767458),new m(1899447441,602891725),new m(3049323471,3964484399),new m(3921009573,2173295548),new m(961987163,4081628472),new m(1508970993,3053834265),new m(2453635748,2937671579),new m(2870763221,3664609560),new m(3624381080,2734883394),new m(310598401,1164996542),new m(607225278,1323610764),new m(1426881987,3590304994),new m(1925078388,4068182383),new m(2162078206,991336113),new m(2614888103,633803317),new m(3248222580,3479774868),new m(3835390401,2666613458),new m(4022224774,944711139),new m(264347078,2341262773),new m(604807628,2007800933),new m(770255983,1495990901),new m(1249150122,1856431235),new m(1555081692,3175218132),new m(1996064986,2198950837),new m(2554220882,3999719339),new m(2821834349,766784016),new m(2952996808,2566594879),new m(3210313671,3203337956),new m(3336571891,1034457026),new m(3584528711,2466948901),new m(113926993,3758326383),new m(338241895,168717936),new m(666307205,1188179964),new m(773529912,1546045734),new m(1294757372,1522805485),new m(1396182291,2643833823),new m(1695183700,2343527390),new m(1986661051,1014477480),new m(2177026350,1206759142),new m(2456956037,344077627),new m(2730485921,1290863460),new m(2820302411,3158454273),new m(3259730800,3505952657),new m(3345764771,106217008),new m(3516065817,3606008344),new m(3600352804,1432725776),new m(4094571909,1467031594),new m(275423344,851169720),new m(430227734,3100823752),new m(506948616,1363258195),new m(659060556,3750685593),new m(883997877,3785050280),new m(958139571,3318307427),new m(1322822218,3812723403),new m(1537002063,2003034995),new m(1747873779,3602036899),new m(1955562222,1575990012),new m(2024104815,1125592928),new m(2227730452,2716904306),new m(2361852424,442776044),new m(2428436474,593698344),new m(2756734187,3733110249),new m(3204031479,2999351573),new m(3329325298,3815920427),new m(3391569614,3928383900),new m(3515267271,566280711),new m(3940187606,3454069534),new m(4118630271,4000239992),new m(116418474,1914138554),new m(174292421,2731055270),new m(289380356,3203993006),new m(460393269,320620315),new m(685471733,587496836),new m(852142971,1086792851),new m(1017036298,365543100),new m(1126000580,2618297676),new m(1288033470,3409855158),new m(1501505948,4234509866),new m(1607167915,987167468),new m(1816402316,1246189591)];function ur(r,n,e){var t,o,i,a=[],f=[],u=[],c=[];for(o=0;o<8;o++)a[o]=u[o]=B(r,8*o);for(var w,y,l,s,h,v,g,b,p,A,_,U,E,x,d=0;128<=e;){for(o=0;o<16;o++)c[o]=B(n,8*o+d);for(o=0;o<80;o++){for(i=0;i<8;i++)f[i]=u[i];for(t=tr(u[7],ir(ar(x=u[4],14),ar(x,18),ar(x,41)),(p=u[4],A=u[5],_=u[6],0,U=p.hi&A.hi^~p.hi&_.hi,E=p.lo&A.lo^~p.lo&_.lo,new m(U,E)),fr[o],c[o%16]),f[7]=tr(t,ir(ar(b=u[0],28),ar(b,34),ar(b,39)),(l=u[0],s=u[1],h=u[2],0,v=l.hi&s.hi^l.hi&h.hi^s.hi&h.hi,g=l.lo&s.lo^l.lo&h.lo^s.lo&h.lo,new m(v,g))),f[3]=tr(f[3],t),i=0;i<8;i++)u[(i+1)%8]=f[i];if(o%16==15)for(i=0;i<16;i++)c[i]=tr(c[i],c[(i+9)%16],ir(ar(y=c[(i+1)%16],1),ar(y,8),or(y,7)),ir(ar(w=c[(i+14)%16],19),ar(w,61),or(w,6)))}for(o=0;o<8;o++)u[o]=tr(u[o],a[o]),a[o]=u[o];d+=128,e-=128}for(o=0;o<8;o++)S(r,8*o,a[o]);return e}var cr=new Uint8Array([106,9,230,103,243,188,201,8,187,103,174,133,132,202,167,59,60,110,243,114,254,148,248,43,165,79,245,58,95,29,54,241,81,14,82,127,173,230,130,209,155,5,104,140,43,62,108,31,31,131,217,171,251,65,189,107,91,224,205,25,19,126,33,121]);function wr(r,n,e){var t,o=new Uint8Array(64),i=new Uint8Array(256),a=e;for(t=0;t<64;t++)o[t]=cr[t];for(ur(o,n,e),e%=128,t=0;t<256;t++)i[t]=0;for(t=0;t<e;t++)i[t]=n[a-e+t];for(i[e]=128,i[(e=256-128*(e<112?1:0))-9]=0,S(i,e-8,new m(a/536870912|0,a<<3)),ur(o,i,e),t=0;t<64;t++)r[t]=o[t];return 0}function yr(r,n){var e=v(),t=v(),o=v(),i=v(),a=v(),f=v(),u=v(),c=v(),w=v();V(e,r[1],r[0]),V(w,n[1],n[0]),X(e,e,w),I(t,r[0],r[1]),I(w,n[0],n[1]),X(t,t,w),X(o,r[3],n[3]),X(o,o,l),X(i,r[2],n[2]),I(i,i,i),V(a,t,e),V(f,i,o),I(u,i,o),I(c,t,e),X(r[0],a,f),X(r[1],c,u),X(r[2],u,f),X(r[3],a,c)}function lr(r,n,e){var t;for(t=0;t<4;t++)F(r[t],n[t],e)}function sr(r,n){var e=v(),t=v(),o=v();H(o,n[2]),X(e,n[0],o),X(t,n[1],o),Z(r,t),r[31]^=q(e)<<7}function hr(r,n,e){var t,o;for(O(r[0],c),O(r[1],w),O(r[2],w),O(r[3],c),o=255;0<=o;--o)lr(r,n,t=e[o/8|0]>>(7&o)&1),yr(n,r),yr(r,r),lr(r,n,t)}function vr(r,n){var e=[v(),v(),v(),v()];O(e[0],t),O(e[1],f),O(e[2],w),X(e[3],t,f),hr(r,e,n)}function gr(r,n,e){var t,o=new Uint8Array(64),i=[v(),v(),v(),v()];for(e||a(n,32),wr(o,n,32),o[0]&=248,o[31]&=127,o[31]|=64,vr(i,o),sr(r,i),t=0;t<32;t++)n[t+32]=r[t];return 0}var br=new Float64Array([237,211,245,92,26,99,18,88,214,156,247,162,222,249,222,20,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,16]);function pr(r,n){var e,t,o,i;for(t=63;32<=t;--t){for(e=0,o=t-32,i=t-12;o<i;++o)n[o]+=e-16*n[t]*br[o-(t-32)],e=Math.floor((n[o]+128)/256),n[o]-=256*e;n[o]+=e,n[t]=0}for(o=e=0;o<32;o++)n[o]+=e-(n[31]>>4)*br[o],e=n[o]>>8,n[o]&=255;for(o=0;o<32;o++)n[o]-=e*br[o];for(t=0;t<32;t++)n[t+1]+=n[t]>>8,r[t]=255&n[t]}function Ar(r){var n,e=new Float64Array(64);for(n=0;n<64;n++)e[n]=r[n];for(n=0;n<64;n++)r[n]=0;pr(r,e)}function _r(r,n,e,t){var o,i,a=new Uint8Array(64),f=new Uint8Array(64),u=new Uint8Array(64),c=new Float64Array(64),w=[v(),v(),v(),v()];wr(a,t,32),a[0]&=248,a[31]&=127,a[31]|=64;var y=e+64;for(o=0;o<e;o++)r[64+o]=n[o];for(o=0;o<32;o++)r[32+o]=a[32+o];for(wr(u,r.subarray(32),e+32),Ar(u),vr(w,u),sr(r,w),o=32;o<64;o++)r[o]=t[o];for(wr(f,r,e+64),Ar(f),o=0;o<64;o++)c[o]=0;for(o=0;o<32;o++)c[o]=u[o];for(o=0;o<32;o++)for(i=0;i<32;i++)c[o+i]+=f[o]*a[i];return pr(r.subarray(32),c),y}function Ur(r,n,e,t){var o,i=new Uint8Array(32),a=new Uint8Array(64),f=[v(),v(),v(),v()],u=[v(),v(),v(),v()];if(e<64)return-1;if(function(r,n){var e=v(),t=v(),o=v(),i=v(),a=v(),f=v(),u=v();if(O(r[2],w),D(r[1],n),j(o,r[1]),X(i,o,y),V(o,o,r[2]),I(i,r[2],i),j(a,i),j(f,a),X(u,f,a),X(e,u,o),X(e,e,i),J(e,e),X(e,e,o),X(e,e,i),X(e,e,i),X(r[0],e,i),j(t,r[0]),X(t,t,i),G(t,o)&&X(r[0],r[0],s),j(t,r[0]),X(t,t,i),G(t,o))return 1;q(r[0])===n[31]>>7&&V(r[0],c,r[0]),X(r[3],r[0],r[1])}(u,t))return-1;for(o=0;o<e;o++)r[o]=n[o];for(o=0;o<32;o++)r[o+32]=t[o];if(wr(a,r,e),Ar(a),hr(f,u,a),vr(u,n.subarray(32)),yr(f,u),sr(i,f),e-=64,_(n,0,i,0)){for(o=0;o<e;o++)r[o]=0;return-1}for(o=0;o<e;o++)r[o]=n[o+64];return e}function Er(r,n){if(32!==r.length)throw new Error("bad key size");if(24!==n.length)throw new Error("bad nonce size")}function xr(){for(var r=0;r<arguments.length;r++)if(!(arguments[r]instanceof Uint8Array))throw new TypeError("unexpected type, use Uint8Array")}function dr(r){for(var n=0;n<r.length;n++)r[n]=0}i.lowlevel={crypto_core_hsalsa20:x,crypto_stream_xor:T,crypto_stream:L,crypto_stream_salsa20_xor:K,crypto_stream_salsa20:Y,crypto_onetimeauth:R,crypto_onetimeauth_verify:P,crypto_verify_16:A,crypto_verify_32:_,crypto_secretbox:M,crypto_secretbox_open:N,crypto_scalarmult:Q,crypto_scalarmult_base:W,crypto_box_beforenm:rr,crypto_box_afternm:nr,crypto_box:function(r,n,e,t,o,i){var a=new Uint8Array(32);return rr(a,o,i),nr(r,n,e,t,a)},crypto_box_open:function(r,n,e,t,o,i){var a=new Uint8Array(32);return rr(a,o,i),er(r,n,e,t,a)},crypto_box_keypair:$,crypto_hash:wr,crypto_sign:_r,crypto_sign_keypair:gr,crypto_sign_open:Ur,crypto_secretbox_KEYBYTES:32,crypto_secretbox_NONCEBYTES:24,crypto_secretbox_ZEROBYTES:32,crypto_secretbox_BOXZEROBYTES:16,crypto_scalarmult_BYTES:32,crypto_scalarmult_SCALARBYTES:32,crypto_box_PUBLICKEYBYTES:32,crypto_box_SECRETKEYBYTES:32,crypto_box_BEFORENMBYTES:32,crypto_box_NONCEBYTES:24,crypto_box_ZEROBYTES:32,crypto_box_BOXZEROBYTES:16,crypto_sign_BYTES:64,crypto_sign_PUBLICKEYBYTES:32,crypto_sign_SECRETKEYBYTES:64,crypto_sign_SEEDBYTES:32,crypto_hash_BYTES:64,gf:v,D:y,L:br,pack25519:Z,unpack25519:D,M:X,A:I,S:j,Z:V,pow2523:J,add:yr,set25519:O,modL:pr,scalarmult:hr,scalarbase:vr},i.randomBytes=function(r){var n=new Uint8Array(r);return a(n,r),n},i.secretbox=function(r,n,e){xr(r,n,e),Er(e,n);for(var t=new Uint8Array(32+r.length),o=new Uint8Array(t.length),i=0;i<r.length;i++)t[i+32]=r[i];return M(o,t,t.length,n,e),o.subarray(16)},i.secretbox.open=function(r,n,e){xr(r,n,e),Er(e,n);for(var t=new Uint8Array(16+r.length),o=new Uint8Array(t.length),i=0;i<r.length;i++)t[i+16]=r[i];return t.length<32||0!==N(o,t,t.length,n,e)?null:o.subarray(32)},i.secretbox.keyLength=32,i.secretbox.nonceLength=24,i.secretbox.overheadLength=16,i.scalarMult=function(r,n){if(xr(r,n),32!==r.length)throw new Error("bad n size");if(32!==n.length)throw new Error("bad p size");var e=new Uint8Array(32);return Q(e,r,n),e},i.scalarMult.base=function(r){if(xr(r),32!==r.length)throw new Error("bad n size");var n=new Uint8Array(32);return W(n,r),n},i.scalarMult.scalarLength=32,i.scalarMult.groupElementLength=32,i.box=function(r,n,e,t){var o=i.box.before(e,t);return i.secretbox(r,n,o)},i.box.before=function(r,n){xr(r,n),function(r,n){if(32!==r.length)throw new Error("bad public key size");if(32!==n.length)throw new Error("bad secret key size")}(r,n);var e=new Uint8Array(32);return rr(e,r,n),e},i.box.after=i.secretbox,i.box.open=function(r,n,e,t){var o=i.box.before(e,t);return i.secretbox.open(r,n,o)},i.box.open.after=i.secretbox.open,i.box.keyPair=function(){var r=new Uint8Array(32),n=new Uint8Array(32);return $(r,n),{publicKey:r,secretKey:n}},i.box.keyPair.fromSecretKey=function(r){if(xr(r),32!==r.length)throw new Error("bad secret key size");var n=new Uint8Array(32);return W(n,r),{publicKey:n,secretKey:new Uint8Array(r)}},i.box.publicKeyLength=32,i.box.secretKeyLength=32,i.box.sharedKeyLength=32,i.box.nonceLength=24,i.box.overheadLength=i.secretbox.overheadLength,i.sign=function(r,n){if(xr(r,n),64!==n.length)throw new Error("bad secret key size");var e=new Uint8Array(64+r.length);return _r(e,r,r.length,n),e},i.sign.open=function(r,n){if(xr(r,n),32!==n.length)throw new Error("bad public key size");var e=new Uint8Array(r.length),t=Ur(e,r,r.length,n);if(t<0)return null;for(var o=new Uint8Array(t),i=0;i<o.length;i++)o[i]=e[i];return o},i.sign.detached=function(r,n){for(var e=i.sign(r,n),t=new Uint8Array(64),o=0;o<t.length;o++)t[o]=e[o];return t},i.sign.detached.verify=function(r,n,e){if(xr(r,n,e),64!==n.length)throw new Error("bad signature size");if(32!==e.length)throw new Error("bad public key size");var t,o=new Uint8Array(64+r.length),i=new Uint8Array(64+r.length);for(t=0;t<64;t++)o[t]=n[t];for(t=0;t<r.length;t++)o[t+64]=r[t];return 0<=Ur(i,o,o.length,e)},i.sign.keyPair=function(){var r=new Uint8Array(32),n=new Uint8Array(64);return gr(r,n),{publicKey:r,secretKey:n}},i.sign.keyPair.fromSecretKey=function(r){if(xr(r),64!==r.length)throw new Error("bad secret key size");for(var n=new Uint8Array(32),e=0;e<n.length;e++)n[e]=r[32+e];return{publicKey:n,secretKey:new Uint8Array(r)}},i.sign.keyPair.fromSeed=function(r){if(xr(r),32!==r.length)throw new Error("bad seed size");for(var n=new Uint8Array(32),e=new Uint8Array(64),t=0;t<32;t++)e[t]=r[t];return gr(n,e,!0),{publicKey:n,secretKey:e}},i.sign.publicKeyLength=32,i.sign.secretKeyLength=64,i.sign.seedLength=32,i.sign.signatureLength=64,i.hash=function(r){xr(r);var n=new Uint8Array(64);return wr(n,r,r.length),n},i.hash.hashLength=64,i.verify=function(r,n){return xr(r,n),0!==r.length&&0!==n.length&&(r.length===n.length&&0===u(r,0,n,0,r.length))},i.setPRNG=function(r){a=r},function(){var o="undefined"!=typeof self?self.crypto||self.msCrypto:null;if(o&&o.getRandomValues){i.setPRNG(function(r,n){var e,t=new Uint8Array(n);for(e=0;e<n;e+=65536)o.getRandomValues(t.subarray(e,e+Math.min(n-e,65536)));for(e=0;e<n;e++)r[e]=t[e];dr(t)})}else"undefined"!=typeof require&&(o=require("crypto"))&&o.randomBytes&&i.setPRNG(function(r,n){var e,t=o.randomBytes(n);for(e=0;e<n;e++)r[e]=t[e];dr(t)})}()}("undefined"!=typeof module&&module.exports?module.exports:self.nacl=self.nacl||{});</script>
<script>
  var K = new URLSearchParams(location.search).get("k") || "";
  var PH = {
    preflight:["The forge is inspected","the forge is ready"], identity:["The minion receives its name","name bound in iron"],
    packages:["Tools of war are gathered","arsenal consecrated"], tailscale:["The minion enters the realm","fealty sworn"],
    github_auth:["GitHub demands tribute","tribute paid in blood"], per_pi_repo:["The chronicles are opened","chronicles sealed"],
    tunnel:["The passage through the void is opened","the void crossed"], claude_code:["The intelligence is summoned","awakened and bound"],
    connector:["The connector is forged","the sigil set"], vault:["The ancient secrets are retrieved","secrets bestowed"],
    selfdoc:["The minion speaks its name","the name spoken"], summary:["The induction is sealed","the rite is complete"]
  };
  var ORDER=["preflight","identity","packages","tailscale","github_auth","per_pi_repo","tunnel","claude_code","connector","vault","selfdoc","summary"];
  var AUTH={
    github:{t:"THE FORGE DEMANDS A BLOOD OATH", s:"GitHub guards the ancient gate — present yourself or the fire dies", b:"Swear fealty to GitHub →", i:"speak the rune · swear the oath · the gate shall open"},
    tailscale:{t:"THE TAILNET DEMANDS FEALTY", s:"swear allegiance to the realm or be cast out", b:"Pledge yourself to Tailscale →", i:"open the gate · swear the oath · enter the realm"},
    cloudflare:{t:"THE GATEKEEPER STIRS", s:"Cloudflare guards the passage — offer your credentials to cross the void", b:"Present yourself to Cloudflare →", i:"the void cannot be crossed without tribute"},
    anthropic:{t:"THE INTELLIGENCE AWAITS AWAKENING", s:"the ancient mind will not stir without your blessing", b:"Summon Claude into service →", i:"grant leave · speak the words · the intelligence awakens"}
  };
  var submitted=false;
  var nameTouched=false;   // true once the operator types in the name field
  // Continue/restart choice screen. choiceArmed is decided ONCE from the FIRST
  // /state (so it only fires for a run THIS page session didn't start — never
  // popping up on the operator who just submitted identity). choiceDismissed
  // flips true once they pick CONTINUE / BEGIN AGAIN.
  var choiceArmed=null;
  var choiceDismissed=false;
  function $(id){return document.getElementById(id);}
  function txt(id,v){var e=$(id); if(e) e.textContent=v==null?"":v;}
  function setval(id,v){var e=$(id); if(e) e.value=v==null?"":v;}
  function href(id,v){var e=$(id); if(e){e.textContent=v==null?"":v; e.setAttribute("href",v||"#");}}
  function liveName(){var v=$("i-name").value.trim().toLowerCase(); return v?v.toUpperCase():"";}
  function setName(nm){var e=$("piname"); if(nm){e.textContent=nm; e.classList.remove("unnamed");} else {e.textContent="UNNAMED"; e.classList.add("unnamed");}}

  function renderRoster(phases, progress){
    var by={}; (phases||[]).forEach(function(p){by[p.name]=p.status;});
    var r=$("roster"); r.innerHTML="";
    ORDER.forEach(function(n){
      var m=PH[n]||[n,"done"], st=by[n]||"pending";
      var cls=st==="done"?"done":st==="running"?"run":st==="error"?"error":"pend";
      var stamp=st==="done"?m[1]:st==="running"?"the hammer strikes":st==="error"?"the strike falters":"awaits the rite";
      var li=document.createElement("li"); li.className="is-"+cls;
      li.innerHTML='<span class="badge '+cls+'"><span>'+(st==="done"?"✦":st==="error"?"!":"")+'</span></span><span class="label"></span><span class="stamp">'+stamp+'</span>';
      li.querySelector(".label").textContent=m[0];
      // Live DEBUG sub-line: only on the RUNNING row, only when we have progress
      // text. It vanishes on completion because the row is rebuilt as "done".
      if(st==="running" && progress && progress.text){
        var sub=document.createElement("div"); sub.className="subline";
        var hasPct=(progress.pct!=null && progress.pct>=0);
        sub.innerHTML = hasPct
          ? '<span class="sub-text"></span><span class="sub-bar"><i></i></span><span class="sub-pct"></span>'
          : '<span class="sub-text"></span><span class="sub-dots"></span>';
        sub.querySelector(".sub-text").textContent=progress.text;   // textContent: log is untrusted
        if(hasPct){
          var p=Math.max(0,Math.min(100,progress.pct));
          sub.querySelector(".sub-bar i").style.width=p+"%";
          sub.querySelector(".sub-pct").textContent=p+"%";
        }
        li.appendChild(sub);
      }
      r.appendChild(li);
    });
  }
  function renderChoice(s){
    document.body.setAttribute("data-state","binding");
    setName(s.pi_name||"the minion");
    txt("eyebrow","THE RITE WAS INTERRUPTED");
    txt("subtext","a previous binding was begun — choose your path");
    ["form","roster","authcard","details","restartbar","gate"].forEach(function(id){ $(id).style.display="none"; });
    document.body.classList.remove("forge-active");
    $("choice").style.display="flex";
  }
  function applyState(s){
    $("gate").style.display="none";
    var ready=!!(s.result&&s.result.ready);
    var inBind = submitted || s.identity_submitted || ready || (s.pi_name&&s.pi_name.length>0);
    var stage = ready?"complete":(inBind?"binding":"identity");

    // First-load detection of an interrupted run: a binding under way, not yet
    // complete, with at least one phase already done. Decide armed-ness ONCE.
    var somePhaseDone = (s.phases||[]).some(function(p){ return p.status==="done"; });
    var partial = inBind && !ready && somePhaseDone;
    if(choiceArmed===null){ choiceArmed = partial; }
    if(choiceArmed && partial && !choiceDismissed){ renderChoice(s); return; }
    $("choice").style.display="none";

    document.body.setAttribute("data-state", stage);
    var pn=(s.pi_name&&s.pi_name.length)?s.pi_name:(liveName()||"the minion");
    if(stage==="identity"){
      // Pre-fill the name with this Pi's current hostname (or a prior name) so
      // the operator can keep or edit it — only while the field is untouched and
      // empty, so we never clobber what they're typing.
      var inp=$("i-name");
      if(inp && !nameTouched && !inp.value){
        var pre=((s.pi_name||s.hostname||"")+"").trim().toLowerCase();
        if(/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(pre) && pre!=="raspberrypi"){ inp.value=pre; }
      }
      setName(liveName()); txt("eyebrow","The rite of binding awaits"); txt("subtext","answer the call, my Lord — name the minion");
    }
    else if(stage==="complete"){ setName(s.pi_name||pn); txt("eyebrow","The rite is complete"); txt("subtext","forged in steel · sworn by ancient oath · the Old One's will is done"); }
    else { setName(s.pi_name||pn); txt("eyebrow","A binding is upon us"); txt("subtext","Weyland's hammer falls — "+(s.pi_name||pn)+" shall be bound, Master"); }

    $("form").style.display = stage==="identity"?"":"none";
    $("roster").style.display = stage==="identity"?"none":"";
    if(stage!=="identity") renderRoster(s.phases, s.progress);

    var a=s.action, ac=$("authcard");
    var authShown = (stage==="binding" && a && a.active);
    if(authShown){
      var c=AUTH[a.provider]||{t:"THE FORGE DEMANDS TRIBUTE",s:"present yourself",b:"Proceed →",i:""};
      txt("auth-title",c.t); txt("auth-sub",c.s); txt("auth-instr",c.i);
      var btn=$("auth-btn"), wait=$("auth-wait");
      // Button only once the URL is captured; until then a pulsing "awaiting".
      if(a.url){ btn.textContent=c.b; btn.setAttribute("href",a.url); btn.style.display=""; wait.style.display="none"; }
      else { btn.style.display="none"; wait.style.display=""; }
      var code=$("auth-code"), cp=$("auth-copy");
      if(a.code){code.textContent=a.code; code.style.display=""; cp.style.display="";} else {code.style.display="none"; cp.style.display="none";}
      ac.style.display="";
    } else ac.style.display="none";

    // Top activity bar: a phase is forging and no auth card is up — proof the
    // forge is alive even when the page would otherwise look idle.
    var running = (s.phases||[]).some(function(p){ return p.status==="running"; });
    document.body.classList.toggle("forge-active", stage==="binding" && running && !authShown);

    // Start over: only meaningful once a binding is under way or complete (the
    // identity stage has nothing to reset). Hidden otherwise.
    $("restartbar").style.display = (stage==="binding"||stage==="complete") ? "" : "none";

    var d=$("details");
    if(ready){ d.style.display=""; var r=s.result;
      txt("f-url",r.mcp_url); txt("f-cid",r.client_id||"weyland-mcp-claude-ai"); txt("f-bearer",r.bearer);
      href("f-ct",r.consent_tunnel); href("f-cl",r.consent_local); href("f-repo",r.repo); setval("f-proj",r.project_instructions);
    } else d.style.display="none";
  }
  function gate(){ $("form").style.display="none"; $("roster").style.display="none"; $("authcard").style.display="none"; $("details").style.display="none"; $("gate").style.display=""; }
  function tick(){
    fetch("/state?k="+encodeURIComponent(K),{cache:"no-store"})
      .then(function(res){ if(res.status===403){gate(); return null;} return res.json(); })
      .then(function(j){ if(j) applyState(j); }).catch(function(){});
  }
  $("i-name").addEventListener("input", function(){ nameTouched=true; if(document.body.getAttribute("data-state")==="identity") setName(liveName()); });
  // Three-page identity form so each screen fits without scrolling:
  //   p1 name+domain  ->  p2 password+wake  ->  p3 SSH + SUBJUGATE (final commit).
  // Values persist across Back/Next since the inputs are only hidden, never reset.
  var NAME_OK=/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/;
  function showFormPage(n){
    $("form-p1").style.display = n===1?"":"none";
    $("form-p2").style.display = n===2?"":"none";
    $("form-p3").style.display = n===3?"":"none";
  }
  $("form-next").addEventListener("click", function(){
    var nm=$("i-name").value.trim().toLowerCase(), m=$("fmsg1");
    if(!NAME_OK.test(nm)){ m.style.color="#e88"; m.textContent="a true name, my Lord: lowercase letters, digits, hyphens (2–32)"; return; }
    m.textContent=""; showFormPage(2);
  });
  $("form-next2").addEventListener("click", function(){
    var pw=$("i-pw").value, pw2=$("i-pw2").value, m=$("fmsg2");
    if(pw && pw!==pw2){ m.style.color="#e88"; m.textContent="the passwords do not match, my Lord"; return; }
    m.textContent=""; showFormPage(3);
  });
  $("form-back").addEventListener("click", function(e){ e.preventDefault(); showFormPage(1); });
  $("form-back2").addEventListener("click", function(e){ e.preventDefault(); showFormPage(2); });
  $("begin").addEventListener("click", function(){
    var nm=$("i-name").value.trim().toLowerCase(), m=$("fmsg3");
    if(!NAME_OK.test(nm)){ showFormPage(1); var m1=$("fmsg1"); m1.style.color="#e88"; m1.textContent="a true name, my Lord: lowercase letters, digits, hyphens (2–32)"; return; }
    var pw=$("i-pw").value, pw2=$("i-pw2").value;
    if(pw && pw!==pw2){ showFormPage(2); var m2=$("fmsg2"); m2.style.color="#e88"; m2.textContent="the passwords do not match, my Lord"; return; }
    if(sshMode==="existing" && !sshPubKey){ m.style.color="#e88"; m.textContent="choose your public-key (.pub) file first, my Lord"; return; }
    if(sshMode==="generate" && !sshPubKey){ m.style.color="#e88"; m.textContent="generate a key first — and save both downloads"; return; }
    m.style.color="var(--muted)"; m.textContent="presenting the minion to the forge…";
    var body="pi_name="+encodeURIComponent(nm)+"&domain="+encodeURIComponent($("i-domain").value.trim())+"&pc_wake="+encodeURIComponent($("i-pc").value.trim())+"&wake_token="+encodeURIComponent($("i-tok").value.trim())+"&new_password="+encodeURIComponent(pw)+"&ssh_mode="+encodeURIComponent(sshMode)+"&ssh_pub_key="+encodeURIComponent(sshPubKey);
    fetch("/identity?k="+encodeURIComponent(K),{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:body})
      .then(function(r){ if(r.ok){ submitted=true; m.textContent=""; tick(); } else { m.style.color="#e88"; m.textContent="the forge refused that name"; } })
      .catch(function(){ m.style.color="#e88"; m.textContent="the forge could not be reached"; });
  });
  $("disclose").addEventListener("click", function(){
    var open=!document.body.classList.contains("details-open");
    document.body.classList.toggle("details-open", open);
    this.setAttribute("aria-expanded", String(open)); this.innerHTML = open ? "Seal the talisman &#9652;" : "Consult the talisman &#9662;";
    if(open) $("panel").scrollIntoView({behavior:"smooth",block:"start"});
  });
  function flash(b){var t=b.textContent;b.textContent="Copied";b.classList.add("ok");setTimeout(function(){b.textContent=t;b.classList.remove("ok");},1400);}
  // execCommand('copy') via a temp textarea — works over plain HTTP, where the
  // async Clipboard API is blocked (non-secure context).
  function execCopy(s){
    try{
      var ta=document.createElement("textarea");
      ta.value=s; ta.setAttribute("readonly","");
      ta.style.position="fixed"; ta.style.top="-1000px"; ta.style.left="-1000px"; ta.style.opacity="0";
      document.body.appendChild(ta);
      ta.focus(); ta.select(); try{ ta.setSelectionRange(0, s.length); }catch(_){}
      var ok=document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    }catch(e){ return false; }
  }
  // Copy text via the Clipboard API when allowed, else fall back to execCopy.
  // Calls onOk() only when the copy actually succeeded.
  function copyText(s, onOk){
    s=(""+s);
    function fb(){ if(execCopy(s) && onOk) onOk(); }
    if(navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(s).then(function(){ if(onOk) onOk(); }, fb);
    } else { fb(); }
  }
  document.addEventListener("click", function(e){
    // One copy handler for both: data-copy (look up an element by id and copy its
    // value/text) and data-copy-text (copy the literal string, e.g. the .chip
    // path hints). Same "Copied" flash for both.
    var b=e.target.closest("[data-copy-text],.copy[data-copy]"); if(!b) return;
    var text;
    if(b.hasAttribute("data-copy-text")){
      text=b.getAttribute("data-copy-text");
    } else {
      var el=$(b.getAttribute("data-copy")); if(!el) return;
      text=(el.tagName==="TEXTAREA"||el.tagName==="INPUT")?el.value:el.textContent;
    }
    if(text==null) return;
    copyText((""+text).trim(), function(){ flash(b); });
  });
  $("patsave").addEventListener("click", function(){
    var pat=$("f-pat").value.trim(), m=$("patmsg");
    if(!pat){ m.className="patmsg err"; m.textContent="no talisman offered"; return; }
    m.className="patmsg"; m.textContent="offering…";
    fetch("/save-pat?k="+encodeURIComponent(K),{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:"pat="+encodeURIComponent(pat)})
      .then(function(r){ if(r.ok){m.className="patmsg ok"; m.textContent="the talisman is bound to the forge"; $("f-pat").value="";}
        else if(r.status===400){m.className="patmsg err"; m.textContent="no true talisman (github_pat_ / ghp_)";}
        else {m.className="patmsg err"; m.textContent="the forge rejected it";} })
      .catch(function(){m.className="patmsg err"; m.textContent="the forge could not be reached";});
  });
  $("seal").addEventListener("click", function(){ this.textContent="⚒ the minion is bound"; this.disabled=true; fetch("/done?k="+encodeURIComponent(K),{method:"POST"}).catch(function(){}); });
  $("startover").addEventListener("click", function(){
    if(!confirm("Start over? This stops the current rite and returns to naming the minion. Nothing already installed is undone.")) return;
    var b=this; b.disabled=true; b.innerHTML="&#8635; starting over…";
    fetch("/restart?k="+encodeURIComponent(K),{method:"POST"})
      .then(function(){
        // The bootstrap reset state + relaunched; drop our local stage memory so
        // applyState recomputes from the fresh (identity) state, and clear the
        // name field so the prefill can repopulate it.
        submitted=false; nameTouched=false; var inp=$("i-name"); if(inp) inp.value=""; showFormPage(1);
        setTimeout(function(){ b.disabled=false; b.innerHTML="&#8635; Start over &mdash; unname the minion"; tick(); }, 700);
      })
      .catch(function(){ b.disabled=false; b.innerHTML="&#8635; Start over &mdash; unname the minion"; });
  });
  // Choice screen: CONTINUE just dismisses (shows the live checklist); BEGIN
  // AGAIN resets everything via /restart and returns to the identity form.
  function bindClick(id, fn){ var e=$(id); e.addEventListener("click", fn);
    e.addEventListener("keydown", function(ev){ if(ev.key==="Enter"||ev.key===" "){ ev.preventDefault(); fn(); } }); }
  bindClick("choice-continue", function(){ choiceDismissed=true; $("choice").style.display="none"; tick(); });
  bindClick("choice-again", function(){
    if(!confirm("Begin again? This stops the interrupted rite and returns to naming the minion. Nothing already installed is undone.")) return;
    choiceDismissed=true; $("choice").style.display="none";
    fetch("/restart?k="+encodeURIComponent(K),{method:"POST"})
      .then(function(){ submitted=false; nameTouched=false; choiceArmed=false; var inp=$("i-name"); if(inp) inp.value=""; showFormPage(1); setTimeout(tick,700); })
      .catch(function(){ tick(); });
  });
  // ===== SSH access: existing-key picker + browser-side ed25519 generation =====
  // Pure JS so it works over plain HTTP (crypto.subtle needs a secure context;
  // getRandomValues + TweetNaCl do not). Private key NEVER leaves the browser —
  // only the public key is POSTed. Formats validated against ssh-keygen+puttygen.
  var sshMode="none", sshPubKey="", genPriv="", genPpk="";
  var SSHKEY_RE=/^(ssh-ed25519|ssh-rsa|ssh-dss|ecdsa-sha2-\S+|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-\S+)\s+\S+/;
  function _u32(n){return new Uint8Array([(n>>>24)&255,(n>>>16)&255,(n>>>8)&255,n&255]);}
  function _cat(a){var L=0,i;for(i=0;i<a.length;i++)L+=a[i].length;var o=new Uint8Array(L),p=0;for(i=0;i<a.length;i++){o.set(a[i],p);p+=a[i].length;}return o;}
  function _sb(s){return new TextEncoder().encode(s);}
  function _sshStr(b){if(typeof b==="string")b=_sb(b);return _cat([_u32(b.length),b]);}
  function _b64(b){var s="";for(var i=0;i<b.length;i++)s+=String.fromCharCode(b[i]);return btoa(s);}
  function _b64w(b,w){var s=_b64(b),o=[];for(var i=0;i<s.length;i+=w)o.push(s.slice(i,i+w));return o.join("\n");}
  function _hex(b){return Array.prototype.map.call(b,function(x){return x.toString(16).padStart(2,"0");}).join("");}
  function _rotl(n,s){return (n<<s)|(n>>>(32-s));}
  function _sha1(bytes){
    var ml=bytes.length*8, total=(((bytes.length+1)+8+63)>>6)<<6;
    var msg=new Uint8Array(total); msg.set(bytes); msg[bytes.length]=0x80;
    var dv=new DataView(msg.buffer);
    dv.setUint32(total-4, ml>>>0, false); dv.setUint32(total-8, Math.floor(ml/4294967296)>>>0, false);
    var h0=0x67452301,h1=0xEFCDAB89,h2=0x98BADCFE,h3=0x10325476,h4=0xC3D2E1F0,w=new Int32Array(80),i,t;
    for(i=0;i<total;i+=64){
      for(t=0;t<16;t++)w[t]=dv.getInt32(i+t*4,false);
      for(t=16;t<80;t++)w[t]=_rotl(w[t-3]^w[t-8]^w[t-14]^w[t-16],1);
      var a=h0,b=h1,c=h2,d=h3,e=h4,f,k,tmp;
      for(t=0;t<80;t++){
        if(t<20){f=(b&c)|((~b)&d);k=0x5A827999;}else if(t<40){f=b^c^d;k=0x6ED9EBA1;}
        else if(t<60){f=(b&c)|(b&d)|(c&d);k=0x8F1BBCDC;}else{f=b^c^d;k=0xCA62C1D6;}
        tmp=(_rotl(a,5)+f+e+k+w[t])|0; e=d;d=c;c=_rotl(b,30);b=a;a=tmp;
      }
      h0=(h0+a)|0;h1=(h1+b)|0;h2=(h2+c)|0;h3=(h3+d)|0;h4=(h4+e)|0;
    }
    var out=new Uint8Array(20),ov=new DataView(out.buffer);
    ov.setInt32(0,h0,false);ov.setInt32(4,h1,false);ov.setInt32(8,h2,false);ov.setInt32(12,h3,false);ov.setInt32(16,h4,false);
    return out;
  }
  function _hmac1(key,msg){var B=64;if(key.length>B)key=_sha1(key);var k=new Uint8Array(B);k.set(key);var ip=new Uint8Array(B),op=new Uint8Array(B);for(var i=0;i<B;i++){ip[i]=k[i]^0x36;op[i]=k[i]^0x5c;}return _sha1(_cat([op,_sha1(_cat([ip,msg]))]));}
  function _sshPubLine(pub,c){return "ssh-ed25519 "+_b64(_cat([_sshStr("ssh-ed25519"),_sshStr(pub)]))+" "+c;}
  function _opensshPriv(seed,pub,c){
    var pb=_cat([_sshStr("ssh-ed25519"),_sshStr(pub)]);
    var ci=crypto.getRandomValues(new Uint8Array(4));
    var pv=_cat([ci,ci,_sshStr("ssh-ed25519"),_sshStr(pub),_sshStr(_cat([seed,pub])),_sshStr(c)]);
    var ex=[],pad=1; while((pv.length+ex.length)%8!==0)ex.push(pad++); pv=_cat([pv,new Uint8Array(ex)]);
    var body=_cat([_cat([_sb("openssh-key-v1"),new Uint8Array([0])]),_sshStr("none"),_sshStr("none"),_sshStr(""),_u32(1),_sshStr(pb),_sshStr(pv)]);
    return "-----BEGIN OPENSSH PRIVATE KEY-----\n"+_b64w(body,70)+"\n-----END OPENSSH PRIVATE KEY-----\n";
  }
  function _ppkV2(seed,pub,c){
    var pb=_cat([_sshStr("ssh-ed25519"),_sshStr(pub)]);
    var be=new Uint8Array(seed).reverse(),i=0; while(i<be.length-1&&be[i]===0)i++; var mag=be.slice(i);
    if(mag[0]&0x80)mag=_cat([new Uint8Array([0]),mag]);
    var vb=_sshStr(mag);
    var md=_cat([_sshStr("ssh-ed25519"),_sshStr("none"),_sshStr(c),_sshStr(pb),_sshStr(vb)]);
    var mac=_hex(_hmac1(_sha1(_sb("putty-private-key-file-mac-key")),md));
    var pl=_b64w(pb,64),vl=_b64w(vb,64);
    return "PuTTY-User-Key-File-2: ssh-ed25519\nEncryption: none\nComment: "+c+"\nPublic-Lines: "+pl.split("\n").length+"\n"+pl+"\nPrivate-Lines: "+vl.split("\n").length+"\n"+vl+"\nPrivate-MAC: "+mac+"\n";
  }
  function generateKeypair(comment){
    var seed=crypto.getRandomValues(new Uint8Array(32));
    var kp=nacl.sign.keyPair.fromSeed(seed), pub=new Uint8Array(kp.publicKey);
    return {pubLine:_sshPubLine(pub,comment), privOpenssh:_opensshPriv(seed,pub,comment), ppk:_ppkV2(seed,pub,comment)};
  }
  function _dlFallback(name,text){var b=new Blob([text],{type:"application/octet-stream"});var a=document.createElement("a");a.href=URL.createObjectURL(b);a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(function(){URL.revokeObjectURL(a.href);},1500);}
  function saveFile(name,text){
    if(window.showSaveFilePicker){
      window.showSaveFilePicker({suggestedName:name}).then(function(h){return h.createWritable();})
        .then(function(w){return w.write(text).then(function(){return w.close();});})
        .catch(function(e){ if(!(e&&e.name==="AbortError")) _dlFallback(name,text); });
      return;
    }
    _dlFallback(name,text);
  }
  function setHowtoName(nm){ document.querySelectorAll(".howto-name").forEach(function(e){e.textContent=nm;}); }
  function setSshMode(mode){
    sshMode=mode;
    $("ssh-existing").style.display = mode==="existing"?"":"none";
    $("ssh-generate").style.display = mode==="generate"?"":"none";
    if(mode==="none") sshPubKey="";   // existing/generate keep whatever was loaded
  }
  document.querySelectorAll('input[name="sshmode"]').forEach(function(r){ r.addEventListener("change", function(){ if(this.checked) setSshMode(this.value); }); });
  $("ssh-pick").addEventListener("click", function(){ $("ssh-file").click(); });
  $("ssh-file").addEventListener("change", function(){
    var f=this.files&&this.files[0], st=$("ssh-existing-status"); if(!f) return;
    var rd=new FileReader();
    rd.onload=function(){
      var line=((rd.result||"")+"").split(/\r?\n/).map(function(s){return s.trim();}).filter(Boolean)[0]||"";
      if(SSHKEY_RE.test(line)){ sshPubKey=line; st.className="ssh-status ok"; st.textContent="✓ public key loaded from "+f.name; }
      else { sshPubKey=""; st.className="ssh-status err"; st.textContent="that file isn't an SSH public key (look for one ending .pub)"; }
    };
    rd.readAsText(f);
  });
  $("ssh-gen").addEventListener("click", function(){
    var st=$("ssh-gen-status"), btn=this;
    if(typeof nacl==="undefined"||!nacl.sign){ st.className="ssh-status err"; st.textContent="the key generator didn't load — use 'Use existing key' instead"; return; }
    btn.disabled=true; st.className="ssh-status"; st.textContent="forging a key in your browser…";
    try{
      var nm=$("i-name").value.trim().toLowerCase()||"minion";
      var kp=generateKeypair("weyland@"+nm);
      sshPubKey=kp.pubLine; genPriv=kp.privOpenssh; genPpk=kp.ppk;
      setHowtoName(nm); $("ssh-gen-result").style.display="";
      $("after-openssh").style.display="none"; $("after-ppk").style.display="none";   // fresh keys: re-download
      st.className="ssh-status ok"; st.textContent="key forged — save BOTH files below, then begin the rite";
      btn.textContent="↻ Regenerate";
    }catch(e){ st.className="ssh-status err"; st.textContent="couldn't generate a key in this browser — use 'Use existing key'"; }
    btn.disabled=false;
  });
  // showSaveFilePicker isn't available over plain HTTP (non-secure context), so
  // the download lands in Downloads. Reveal a "now move it here" instruction with
  // the destination path as copy chips the moment the download fires.
  $("dl-openssh").addEventListener("click", function(){ if(genPriv){ saveFile("id_ed25519", genPriv); $("after-openssh").style.display=""; } });
  $("dl-ppk").addEventListener("click", function(){ if(genPpk){ saveFile("id_ed25519.ppk", genPpk); $("after-ppk").style.display=""; } });
  document.querySelectorAll(".howto .tab").forEach(function(t){ t.addEventListener("click", function(){
    var tab=this.getAttribute("data-tab");
    document.querySelectorAll(".howto .tab").forEach(function(x){ x.setAttribute("aria-pressed", String(x===t)); });
    document.querySelectorAll(".howto .tabpane").forEach(function(p){ p.style.display = p.getAttribute("data-pane")===tab?"":"none"; });
  }); });
  tick(); setInterval(tick, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
