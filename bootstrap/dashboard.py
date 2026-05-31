#!/usr/bin/env python3
"""weyland live setup dashboard — forge-steel wizard (browser-first, v2).

Usage: dashboard.py <state_dir> <nonce>

ThreadingHTTPServer on 0.0.0.0:8080:
  GET  /              -> the wizard HTML (polls /state every 1.5s)
  GET  /state?k=N     -> state.json + {"identity_submitted": bool} (nonce-gated)
  POST /identity?k=N  -> validate + write identity.json (the browser identity form)
  POST /save-pat?k=N  -> validate github_pat_/ghp_, write /etc/weyland/weyland.env
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
import sys
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


def state_with_flags() -> bytes:
    """state.json verbatim, plus identity_submitted (without writing state.json)."""
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s["identity_submitted"] = os.path.exists(IDENTITY_FILE)
    return json.dumps(s).encode("utf-8")


def write_identity(form) -> bool:
    name = (form.get("pi_name", [""])[0] or "").strip().lower()
    if not NAME_RE.match(name):
        return False
    data = {
        "pi_name": name,
        "domain": (form.get("domain", [""])[0] or "").strip(),
        "pc_wake": (form.get("pc_wake", [""])[0] or "").strip(),
        "wake_token": (form.get("wake_token", [""])[0] or "").strip(),
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
<style>
  :root{
    --bg:#1a1e24; --steel:#222831; --steel2:#252d38;
    --flame:#e8750a; --flame-bright:#ff8c1a; --flame-deep:#b8560a;
    --blood:#c0392b; --blood-border:#9b2020; --blood-bg:#2a0a0a; --blood-bg2:#1f0808;
    --steelblue:#4a7fa5; --ink:#f0e6cc; --muted:#9a8a6a; --leather:#8a7a5a;
    --line:#3a424f; --line2:#404858; --gold:#d4a017; --gold-deep:#c9980f;
    --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --grain:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.5'/%3E%3C/svg%3E");
  }
  *{box-sizing:border-box} html,body{margin:0; height:100%}
  body{background:var(--bg); color:var(--ink); font-family:var(--sans); -webkit-font-smoothing:antialiased; overflow:hidden;
    background-image:radial-gradient(120% 80% at 50% -12%, #2b3542, transparent 60%),radial-gradient(100% 60% at 50% 118%, #1f2731, transparent 62%),linear-gradient(180deg,#1c2128,#15181d); background-attachment:fixed;}
  body::before{content:""; position:fixed; inset:0; z-index:60; pointer-events:none; background-image:var(--grain); background-size:150px 150px; opacity:.045; mix-blend-mode:overlay}
  body::after{content:""; position:fixed; left:0; right:0; bottom:0; height:30vh; z-index:1; pointer-events:none; background:linear-gradient(to top,#e8750a12,transparent); filter:blur(3px)}
  .app{position:relative; z-index:2; height:100dvh; display:flex; flex-direction:column; max-width:720px; margin:0 auto; padding:10px 16px 16px}
  body.details-open{overflow:auto} body.details-open .app{height:auto; min-height:100dvh}
  .topbar{display:flex; align-items:center; justify-content:flex-start; gap:10px; flex:0 0 auto}
  .sigil{font-family:var(--mono); font-size:10.5px; letter-spacing:.24em; color:var(--flame); opacity:.9}
  .sigil b{color:var(--ink); opacity:.65}
  .hero{flex:0 0 auto; text-align:center; padding:14px 0 8px}
  .eyebrow{margin:0 0 12px; font-family:var(--mono); font-size:11px; letter-spacing:.44em; text-transform:uppercase; color:var(--flame)}
  body[data-state="complete"] .eyebrow{color:var(--gold)}
  .plate{display:inline-block; max-width:100%; padding:13px 26px; border-radius:7px; border:1px solid var(--line2); background:linear-gradient(180deg,#2a323d,#1c222b); box-shadow:0 0 0 1px #0b0e12 inset,0 0 44px #e8750a22,0 14px 38px #0009,0 1px 0 #56627240 inset}
  .name{margin:0; font-family:var(--mono); font-weight:700; letter-spacing:.17em; line-height:1.04; font-size:clamp(26px,7.5vw,50px); word-break:break-word; color:#f3b06a; text-shadow:0 0 26px #e8750a55,0 2px 1px #14181d,0 0 6px #ff8c1a33}
  @supports ((-webkit-background-clip:text) or (background-clip:text)){
    .name{background:linear-gradient(180deg,#f4ead0 2%,#ff8c1a 48%,#c45a08 92%); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; color:transparent}
    body[data-state="complete"] .name{background:linear-gradient(180deg,#f4eccf,#d4a017 55%,#a07a10); -webkit-background-clip:text; background-clip:text}
  }
  .name.unnamed{opacity:.5; letter-spacing:.3em}
  .subtitle{margin:12px 0 0; font-family:var(--serif); font-style:italic; font-size:15px; color:var(--ink)}
  .glyph{font-style:normal; display:inline-block; margin-right:9px; color:var(--flame); text-shadow:0 0 12px var(--flame); animation:emberpulse 2.1s ease-in-out infinite}
  body[data-state="complete"] .glyph{color:var(--gold); text-shadow:0 0 12px var(--gold)}
  @keyframes emberpulse{0%,100%{opacity:1; text-shadow:0 0 14px var(--flame)}50%{opacity:.45; text-shadow:0 0 5px var(--flame)}}
  .stage{flex:1 1 0; min-height:0; overflow:auto; margin-top:8px}
  .forge-form{border:1px solid var(--line2); border-radius:12px; background:linear-gradient(180deg,var(--steel2),var(--steel)); padding:18px; position:relative; overflow:hidden}
  .forge-form::after{content:""; position:absolute; inset:0; background-image:var(--grain); background-size:150px 150px; opacity:.05; mix-blend-mode:overlay; pointer-events:none}
  .forge-form h3{margin:0 0 4px; font-family:var(--serif); font-weight:700; font-size:19px; letter-spacing:.04em; color:var(--flame-bright); text-shadow:0 0 16px #e8750a44}
  .forge-form .lead{margin:0 0 16px; font-family:var(--serif); font-style:italic; font-size:13.5px; color:var(--leather)}
  .frow{margin:12px 0; position:relative; z-index:1}
  .frow label{display:block; font-family:var(--mono); font-size:10.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); margin:0 0 6px}
  .frow label .opt{color:var(--steelblue); letter-spacing:.1em}
  .frow input{width:100%; font-family:var(--mono); font-size:14px; color:var(--ink); background:#12161c; border:1px solid var(--line2); border-radius:8px; padding:11px 12px}
  .frow input::placeholder{color:#5f5848}
  .frow input:focus{border-color:var(--flame); box-shadow:0 0 0 1px var(--flame),0 0 16px #e8750a33; outline:none}
  .pair{display:grid; grid-template-columns:1fr 1fr; gap:12px} @media (max-width:560px){.pair{grid-template-columns:1fr}}
  .fmsg{font-family:var(--mono); font-size:11px; letter-spacing:.08em; margin:10px 0 0; min-height:14px; color:#e88}
  .roster{list-style:none; margin:0; padding:0}
  .roster li{display:flex; align-items:center; gap:13px; padding:7px 6px; border-bottom:1px solid #232a33}
  .roster li:last-child{border-bottom:0}
  .badge{flex:0 0 auto; width:22px; height:22px; border-radius:5px; transform:rotate(45deg); display:grid; place-items:center; border:1px solid}
  .badge span{transform:rotate(-45deg); font-size:11px; font-weight:700; line-height:1}
  .badge.done{background:#2b2410; color:var(--gold); border-color:#6e5212; box-shadow:0 0 12px #d4a01726}
  .badge.run{background:#2e1a06; color:var(--flame-bright); border-color:var(--flame); box-shadow:0 0 16px #e8750a66; animation:anvil 1.4s ease-in-out infinite}
  .badge.pend{background:#1b212a; color:var(--leather); border-color:#333b46}
  .badge.error{background:#2a0a0a; color:#ff6f61; border-color:#7a261c}
  @keyframes anvil{0%,100%{box-shadow:0 0 7px #e8750a44}50%{box-shadow:0 0 22px #e8750aaa}}
  .roster .label{flex:1; font-family:var(--serif); font-size:16px}
  li.is-pend .label{color:var(--muted)} li.is-run .label,li.is-done .label{color:var(--ink)} li.is-error .label{color:#ffb3a8}
  .stamp{font-family:var(--mono); font-size:9.5px; letter-spacing:.16em; text-transform:uppercase; text-align:right}
  li.is-done .stamp{color:var(--gold)} li.is-run .stamp{color:var(--flame-bright)} li.is-pend .stamp{color:var(--leather)} li.is-error .stamp{color:#ff6f61}
  .authcard{flex:0 0 auto; margin-top:12px; border:1px solid var(--blood-border); border-radius:12px; background:linear-gradient(180deg,var(--blood-bg),var(--blood-bg2)); padding:16px 16px 15px; position:relative; overflow:hidden; box-shadow:0 0 0 1px #0b0405 inset,0 0 30px #8b1a1a55; animation:bloodglow 2.4s ease-in-out infinite}
  .authcard::after{content:""; position:absolute; inset:0; background-image:var(--grain); background-size:150px 150px; opacity:.06; mix-blend-mode:overlay; pointer-events:none}
  @keyframes bloodglow{0%,100%{box-shadow:0 0 0 1px #0b0405 inset,0 0 18px #8b1a1a44}50%{box-shadow:0 0 0 1px #0b0405 inset,0 0 40px #c0392b88}}
  .authcard h3{margin:0; font-family:var(--serif); font-weight:700; font-size:18px; letter-spacing:.02em; color:#ff5a4a; text-shadow:0 0 18px #c0392b66}
  .authcard .sub{margin:6px 0 15px; font-family:var(--serif); font-style:italic; font-size:13.5px; color:#e8b0a4}
  .authrow{display:flex; gap:11px; align-items:stretch; flex-wrap:wrap}
  .rune{flex:0 0 auto; font-family:var(--mono); font-size:23px; font-weight:700; letter-spacing:.22em; color:var(--ink); background:#160404; border:1px solid var(--blood); border-radius:8px; padding:8px 16px; text-shadow:0 0 14px #c0392b66; box-shadow:0 0 0 1px #000 inset,0 0 18px #8b1a1a44}
  .instr{margin:13px 0 0; font-family:var(--mono); font-size:10.5px; letter-spacing:.12em; color:#c79c92; text-transform:uppercase}
  .btn{-webkit-appearance:none; appearance:none; cursor:pointer; border:0; font-family:var(--mono); letter-spacing:.14em; text-transform:uppercase; border-radius:9px; font-size:13px; padding:14px 18px; text-align:center; text-decoration:none; display:inline-block}
  .btn-fire{background:linear-gradient(180deg,#ff9024,#c45a08); color:#180c02; font-weight:700; box-shadow:0 0 0 1px #7a3c08,0 0 24px #e8750a44,0 2px 0 #ffc78a55 inset}
  .btn-fire:hover{filter:brightness(1.07)} .btn-fire:active{transform:translateY(1px)}
  .btn-blood{background:linear-gradient(180deg,#d0432f,#8b1a1a); color:#fbe3de; font-weight:700; box-shadow:0 0 0 1px #5a1212,0 0 22px #8b1a1a55; flex:1 1 auto; min-width:200px}
  .btn-block{width:100%; margin-top:16px}
  .copy{font-family:var(--mono); font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--ink); background:#2a323d; border:1px solid var(--line2); border-radius:8px; padding:0 14px; cursor:pointer}
  .copy:hover{border-color:var(--flame); color:var(--flame-bright)} .copy:active{transform:translateY(1px)} .copy.ok{color:var(--gold); border-color:#6e5212}
  .details{flex:0 0 auto; margin-top:12px}
  .disclose{width:100%; text-align:center; background:#1b212a; color:var(--muted); border:1px solid var(--line2); border-radius:9px; padding:12px; font-family:var(--mono); font-size:11.5px; letter-spacing:.2em; text-transform:uppercase; cursor:pointer}
  .disclose:hover{color:var(--flame-bright); border-color:var(--flame)}
  .panel{display:none; border:1px solid var(--line2); border-top:0; border-radius:0 0 12px 12px; background:linear-gradient(180deg,var(--steel2),var(--steel)); padding:16px; margin-top:-6px}
  body.details-open .panel{display:block} body.details-open .disclose{border-radius:12px 12px 0 0; color:var(--flame-bright); border-color:var(--flame)}
  .field{margin:12px 0} .field:first-child{margin-top:0}
  .field label{display:block; font-family:var(--mono); font-size:10.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); margin:0 0 6px}
  .copybox{display:flex; gap:8px; align-items:stretch}
  .val{flex:1; min-width:0; font-family:var(--mono); font-size:13px; color:var(--ink); background:#12161c; border:1px solid var(--line2); border-radius:8px; padding:10px 12px; overflow-x:auto; white-space:nowrap}
  a.val{display:block; text-decoration:none; color:var(--flame-bright)} a.val:hover{text-decoration:underline}
  textarea.val{white-space:pre-wrap; word-break:break-word; height:104px; resize:vertical; line-height:1.45; width:100%}
  input.val{width:100%}
  .talisman{border:1px solid var(--line2); background:#1f1810; border-radius:10px; padding:14px; margin-top:4px}
  .warn{font-size:12.5px; color:#e8c79a; background:#241a08; border:1px solid #5a4a20; border-radius:8px; padding:9px 11px; margin:0 0 12px}
  .warn b{color:var(--flame-bright)}
  .patmsg{font-family:var(--mono); font-size:11px; margin:8px 0 0; min-height:14px} .patmsg.ok{color:var(--gold)} .patmsg.err{color:#ff6f61}
  .seal{margin-top:14px; width:100%}
  .gate{flex:1 1 auto; display:grid; place-items:center; text-align:center; font-family:var(--serif); font-style:italic; color:var(--muted); font-size:15px; padding:20px}
  :focus-visible{outline:2px solid var(--flame); outline-offset:2px; border-radius:6px}
  @media (prefers-reduced-motion:reduce){*{animation:none !important; transition:none !important}}
</style>
</head>
<body data-state="identity">
  <div class="app">
    <div class="topbar"><span class="sigil">&#9874; <b>WEYLAND</b> &middot; DIVINE SMITH &middot; FLEET BOUND</span></div>
    <header class="hero">
      <p class="eyebrow" id="eyebrow">The rite of binding awaits</p>
      <div class="plate"><p class="name unnamed" id="piname">UNNAMED</p></div>
      <p class="subtitle"><span class="glyph">&#9672;</span><span id="subtext"></span></p>
    </header>

    <div class="stage" id="stage">
      <section class="forge-form" id="form" aria-label="Name the minion">
        <h3>NAME THE MINION</h3>
        <p class="lead">speak the minion's name and domain, my Lord — the rite cannot begin without it</p>
        <div class="frow"><label for="i-name">Minion name</label>
          <input id="i-name" type="text" autocomplete="off" spellcheck="false" placeholder="e.g. inkypi"></div>
        <div class="frow"><label for="i-domain">Domain</label>
          <input id="i-domain" type="text" autocomplete="off" spellcheck="false" placeholder="julianburton.com"></div>
        <div class="pair">
          <div class="frow"><label for="i-pc">PC wake hostname <span class="opt">&middot; optional</span></label>
            <input id="i-pc" type="text" autocomplete="off" spellcheck="false" placeholder="ju-laptop.tail875649.ts.net"></div>
          <div class="frow"><label for="i-tok">Wake token <span class="opt">&middot; optional</span></label>
            <input id="i-tok" type="text" autocomplete="off" spellcheck="false" placeholder="X-Wake-Token"></div>
        </div>
        <button class="btn btn-fire btn-block" type="button" id="begin">Begin the rite &rarr;</button>
        <p class="fmsg" id="fmsg"></p>
      </section>
      <ul class="roster" id="roster" style="display:none"></ul>
    </div>

    <section class="authcard" id="authcard" style="display:none" aria-label="The forge demands a blood oath">
      <h3 id="auth-title"></h3>
      <p class="sub" id="auth-sub"></p>
      <div class="authrow">
        <a class="btn btn-blood" id="auth-btn" href="#" target="_blank" rel="noopener">Swear the oath &rarr;</a>
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
  </div>

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
  function $(id){return document.getElementById(id);}
  function txt(id,v){var e=$(id); if(e) e.textContent=v==null?"":v;}
  function setval(id,v){var e=$(id); if(e) e.value=v==null?"":v;}
  function href(id,v){var e=$(id); if(e){e.textContent=v==null?"":v; e.setAttribute("href",v||"#");}}
  function liveName(){var v=$("i-name").value.trim().toLowerCase(); return v?v.toUpperCase():"";}
  function setName(nm){var e=$("piname"); if(nm){e.textContent=nm; e.classList.remove("unnamed");} else {e.textContent="UNNAMED"; e.classList.add("unnamed");}}

  function renderRoster(phases){
    var by={}; (phases||[]).forEach(function(p){by[p.name]=p.status;});
    var r=$("roster"); r.innerHTML="";
    ORDER.forEach(function(n){
      var m=PH[n]||[n,"done"], st=by[n]||"pending";
      var cls=st==="done"?"done":st==="running"?"run":st==="error"?"error":"pend";
      var stamp=st==="done"?m[1]:st==="running"?"the hammer strikes":st==="error"?"the strike falters":"awaits the rite";
      var li=document.createElement("li"); li.className="is-"+cls;
      li.innerHTML='<span class="badge '+cls+'"><span>'+(st==="done"?"✦":st==="error"?"!":"")+'</span></span><span class="label"></span><span class="stamp">'+stamp+'</span>';
      li.querySelector(".label").textContent=m[0]; r.appendChild(li);
    });
  }
  function applyState(s){
    $("gate").style.display="none";
    var ready=!!(s.result&&s.result.ready);
    var inBind = submitted || s.identity_submitted || ready || (s.pi_name&&s.pi_name.length>0);
    var stage = ready?"complete":(inBind?"binding":"identity");
    document.body.setAttribute("data-state", stage);
    var pn=(s.pi_name&&s.pi_name.length)?s.pi_name:(liveName()||"the minion");
    if(stage==="identity"){ setName(liveName()); txt("eyebrow","The rite of binding awaits"); txt("subtext","answer the call, my Lord — name the minion"); }
    else if(stage==="complete"){ setName(s.pi_name||pn); txt("eyebrow","The rite is complete"); txt("subtext","forged in steel · sworn by ancient oath · the Old One's will is done"); }
    else { setName(s.pi_name||pn); txt("eyebrow","A binding is upon us"); txt("subtext","Weyland's hammer falls — "+(s.pi_name||pn)+" shall be bound, Master"); }

    $("form").style.display = stage==="identity"?"":"none";
    $("roster").style.display = stage==="identity"?"none":"";
    if(stage!=="identity") renderRoster(s.phases);

    var a=s.action, ac=$("authcard");
    if(stage==="binding" && a && a.active){
      var c=AUTH[a.provider]||{t:"THE FORGE DEMANDS TRIBUTE",s:"present yourself",b:"Proceed →",i:""};
      txt("auth-title",c.t); txt("auth-sub",c.s); txt("auth-instr",c.i);
      var btn=$("auth-btn"); btn.textContent=c.b; btn.setAttribute("href",a.url||"#");
      var code=$("auth-code"), cp=$("auth-copy");
      if(a.code){code.textContent=a.code; code.style.display=""; cp.style.display="";} else {code.style.display="none"; cp.style.display="none";}
      ac.style.display="";
    } else ac.style.display="none";

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
  $("i-name").addEventListener("input", function(){ if(document.body.getAttribute("data-state")==="identity") setName(liveName()); });
  $("begin").addEventListener("click", function(){
    var nm=$("i-name").value.trim().toLowerCase(), m=$("fmsg");
    if(!/^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/.test(nm)){ m.style.color="#e88"; m.textContent="a true name, my Lord: lowercase letters, digits, hyphens (2–32)"; return; }
    m.style.color="var(--muted)"; m.textContent="presenting the minion to the forge…";
    var body="pi_name="+encodeURIComponent(nm)+"&domain="+encodeURIComponent($("i-domain").value.trim())+"&pc_wake="+encodeURIComponent($("i-pc").value.trim())+"&wake_token="+encodeURIComponent($("i-tok").value.trim());
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
  document.addEventListener("click", function(e){
    var b=e.target.closest(".copy"); if(!b||!b.getAttribute("data-copy")) return;
    var el=$(b.getAttribute("data-copy")); if(!el) return;
    var text=(el.tagName==="TEXTAREA"||el.tagName==="INPUT")?el.value:el.textContent;
    if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText((text||"").trim()).then(function(){flash(b);},function(){}); else flash(b);
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
  tick(); setInterval(tick, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
