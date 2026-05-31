#!/usr/bin/env python3
"""weyland live setup dashboard — forge-fire wizard (v3 design, state-driven).

Usage: dashboard.py <state_dir> <nonce>

Serves a ThreadingHTTPServer on 0.0.0.0:8080:
  GET  /            -> the wizard HTML (polls /state every 1.5s)
  GET  /state?k=N   -> state.json verbatim (nonce-gated; carries the bearer)
  POST /save-pat?k=N (pat=...) -> writes WEYLAND_PAT to /etc/weyland/weyland.env
  POST /done?k=N    -> 200 then shuts the server down

Reads state.json (written atomically by install.sh) on every /state. The bash
side is the single writer; this server is read-only except /save-pat. Shuts
itself down after 15 minutes idle. Never logs the PAT or bearer.

ENV_FILE and PORT are env-overridable (WEYLAND_ENV_FILE / WEYLAND_DASH_PORT)
for testing; production uses the defaults.
"""
from __future__ import annotations

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
ENV_FILE = os.environ.get("WEYLAND_ENV_FILE", "/etc/weyland/weyland.env")
PORT = int(os.environ.get("WEYLAND_DASH_PORT", "8080"))
IDLE_TIMEOUT = 900  # 15 minutes

PAT_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]+|ghp_[A-Za-z0-9]+)$")

_last = [time.time()]
_srv = [None]


def read_state() -> bytes:
    try:
        with open(STATE_FILE, "rb") as f:
            return f.read()
    except OSError:
        return b"{}"


def write_pat(pat: str):
    """Replace the WEYLAND_PAT= line in ENV_FILE, preserving other keys.
    Tries a direct write first, falls back to passwordless sudo tee. Never
    logs or echoes the token."""
    try:
        with open(ENV_FILE, "r") as f:
            lines = f.read().splitlines()
    except OSError:
        cur = subprocess.run(["sudo", "-n", "cat", ENV_FILE],
                             capture_output=True, text=True)
        lines = cur.stdout.splitlines() if cur.returncode == 0 else []
    kept = [ln for ln in lines if not ln.startswith("WEYLAND_PAT=")]
    kept.append("WEYLAND_PAT=" + pat)
    content = "\n".join(kept) + "\n"
    try:
        with open(ENV_FILE, "w") as f:
            f.write(content)
    except OSError:
        p = subprocess.run(["sudo", "-n", "tee", ENV_FILE],
                           input=content, text=True, capture_output=True)
        if p.returncode != 0:
            return False
    subprocess.run(["sudo", "-n", "chown", "root:admin", ENV_FILE], capture_output=True)
    subprocess.run(["sudo", "-n", "chmod", "0640", ENV_FILE], capture_output=True)
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence access logs (never risk logging secrets)
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

    def do_GET(self):
        self._touch()
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/state":
            if not self._nonce_ok():
                self._send(403, b'{"error":"nonce"}', "application/json")
            else:
                self._send(200, read_state(), "application/json")
        else:
            self._send(404, b"not found")

    def do_POST(self):
        self._touch()
        path = urlparse(self.path).path
        if not self._nonce_ok():
            self._send(403, b"nonce")
            return
        if path == "/save-pat":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            pat = parse_qs(raw).get("pat", [""])[0].strip()
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
# The wizard HTML (v3 forge-fire design, state-driven). Self-contained.
# ──────────────────────────────────────────────────────────────────────────
HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>weyland · the binding</title>
<style>
  :root{
    --bg:#0c0a08; --iron:#1c1510; --iron2:#160f09; --line:#2d1f10; --line2:#3a2510;
    --ink:#ead9bc; --muted:#8a7a65; --ash:#6f614e;
    --ember:#e8520a; --ember-bright:#ff6a1f; --ember-deep:#a8370a;
    --gold:#d4a017; --gold-soft:#c9a227;
    --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,"Times New Roman",serif;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --grain:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.55'/%3E%3C/svg%3E");
  }
  *{box-sizing:border-box}
  html,body{margin:0; height:100%}
  body{
    background:var(--bg); color:var(--ink); font-family:var(--sans); -webkit-font-smoothing:antialiased;
    overflow:hidden;
    background-image:
      radial-gradient(130% 78% at 50% 122%, #e8520a26, transparent 58%),
      radial-gradient(90% 52% at 50% 134%, #ff6a1f1c, transparent 52%),
      radial-gradient(120% 70% at 50% -20%, #1a0f0699, transparent 60%);
    background-attachment:fixed;
  }
  body::before{content:""; position:fixed; inset:0; z-index:60; pointer-events:none;
    background-image:var(--grain); background-size:140px 140px; opacity:.05; mix-blend-mode:overlay}
  body::after{content:""; position:fixed; left:0; right:0; bottom:0; height:34vh; z-index:1; pointer-events:none;
    background:linear-gradient(to top,#e8520a14,transparent); filter:blur(2px)}

  .app{position:relative; z-index:2; height:100dvh; display:flex; flex-direction:column; max-width:720px; margin:0 auto; padding:10px 16px 16px}
  body.details-open{overflow:auto}
  body.details-open .app{height:auto; min-height:100dvh}

  .topbar{display:flex; align-items:center; justify-content:space-between; gap:10px; flex:0 0 auto}
  .sigil{font-family:var(--mono); font-size:10.5px; letter-spacing:.26em; color:var(--ember); opacity:.85; white-space:nowrap}
  .sigil b{color:var(--ink); opacity:.6}

  .hero{flex:0 0 auto; text-align:center; padding:16px 0 10px}
  .eyebrow{margin:0 0 12px; font-family:var(--mono); font-size:11px; letter-spacing:.46em; text-transform:uppercase; color:var(--ember)}
  body[data-state="complete"] .eyebrow{color:var(--gold)}
  .plate{display:inline-block; max-width:100%; padding:14px 26px; border-radius:8px;
    border:1px solid var(--line2); background:linear-gradient(180deg,#1e150bcc,#0f0a06cc);
    box-shadow:0 0 0 1px #000 inset, 0 0 46px #e8520a1f, 0 14px 40px #000a, 0 1px 0 #5c3a14 inset}
  .name{margin:0; font-family:var(--mono); font-weight:700; letter-spacing:.16em; line-height:1.04;
    font-size:clamp(28px,8vw,54px); word-break:break-word;
    color:#ff8a3d; text-shadow:0 0 26px #e8520a55, 0 2px 1px #2a1606, 0 0 5px #ff6a1f33}
  @supports ((-webkit-background-clip:text) or (background-clip:text)){
    .name{background:linear-gradient(180deg,#ffe0b0 2%,#ff8a32 42%,#d2440a 86%,#9a3408);
      -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent; color:transparent}
    body[data-state="complete"] .name{background:linear-gradient(180deg,#f4e3b0,#d4a017 55%,#9c7411); -webkit-background-clip:text; background-clip:text}
  }
  .subtitle{margin:14px 0 0; font-family:var(--serif); font-style:italic; font-size:15px; letter-spacing:.01em; color:var(--ink)}
  .glyph{font-style:normal; display:inline-block; margin-right:9px; color:var(--ember); text-shadow:0 0 12px var(--ember); animation:emberpulse 2.1s ease-in-out infinite}
  body[data-state="complete"] .glyph{color:var(--gold); text-shadow:0 0 12px var(--gold)}
  @keyframes emberpulse{0%,100%{opacity:1; text-shadow:0 0 14px var(--ember)}50%{opacity:.45; text-shadow:0 0 5px var(--ember)}}

  .rosterwrap{flex:1 1 0; min-height:0; overflow:auto; margin-top:8px}
  .roster{list-style:none; margin:0; padding:0}
  .roster li{display:flex; align-items:center; gap:13px; padding:7px 6px; border-bottom:1px solid #20160c}
  .roster li:last-child{border-bottom:0}
  .badge{flex:0 0 auto; width:22px; height:22px; border-radius:5px; transform:rotate(45deg);
    display:grid; place-items:center; border:1px solid}
  .badge span{transform:rotate(-45deg); font-size:11px; font-weight:700; line-height:1}
  .badge.done{background:#241803; color:var(--gold); border-color:#6e5212; box-shadow:0 0 12px #d4a01726}
  .badge.run{background:#2a1203; color:var(--ember-bright); border-color:var(--ember); box-shadow:0 0 16px #e8520a55; animation:anvil 1.5s ease-in-out infinite}
  .badge.pend{background:#140d07; color:var(--ash); border-color:#2a1d10}
  .badge.error{background:#2a0a08; color:#ff6f61; border-color:#7a261c}
  @keyframes anvil{0%,100%{box-shadow:0 0 8px #e8520a44}50%{box-shadow:0 0 22px #e8520a99}}
  .roster .label{flex:1; font-family:var(--serif); font-size:16px; letter-spacing:.01em}
  li.is-pend .label{color:var(--muted)} li.is-run .label{color:var(--ink)} li.is-done .label{color:var(--ink)} li.is-error .label{color:#ffb3a8}
  .stamp{font-family:var(--mono); font-size:9.5px; letter-spacing:.16em; text-transform:uppercase; text-align:right}
  li.is-done .stamp{color:var(--gold)} li.is-run .stamp{color:var(--ember-bright)} li.is-pend .stamp{color:var(--ash)} li.is-error .stamp{color:#ff6f61}

  .authcard{flex:0 0 auto; margin-top:12px; border:1px solid var(--ember); border-radius:12px;
    background:linear-gradient(180deg,#2a1305,#140a04); padding:16px 16px 15px; position:relative; overflow:hidden;
    box-shadow:0 0 0 1px #000 inset, 0 0 30px #e8520a44; animation:forgeglow 2.6s ease-in-out infinite}
  .authcard::after{content:""; position:absolute; inset:0; background-image:var(--grain); background-size:140px 140px; opacity:.06; mix-blend-mode:overlay; pointer-events:none}
  @keyframes forgeglow{0%,100%{box-shadow:0 0 0 1px #000 inset,0 0 20px #e8520a33}50%{box-shadow:0 0 0 1px #000 inset,0 0 40px #ff6a1f66}}
  .authcard h3{margin:0; font-family:var(--serif); font-weight:700; font-size:18px; letter-spacing:.02em; color:var(--ember-bright); text-shadow:0 0 18px #e8520a55}
  .authcard .sub{margin:6px 0 15px; font-family:var(--serif); font-style:italic; font-size:13.5px; color:#f0c79a}
  .authrow{display:flex; gap:11px; align-items:stretch; flex-wrap:wrap}
  .rune{flex:0 0 auto; font-family:var(--mono); font-size:23px; font-weight:700; letter-spacing:.22em; color:var(--ink);
    background:#0a0604; border:1px solid var(--ember); border-radius:8px; padding:8px 16px; text-shadow:0 0 14px #e8520a55;
    box-shadow:0 0 0 1px #000 inset, 0 0 18px #e8520a33}
  .instr{margin:13px 0 0; font-family:var(--mono); font-size:10.5px; letter-spacing:.12em; color:var(--muted); text-transform:uppercase}

  .btn{-webkit-appearance:none; appearance:none; cursor:pointer; border:0; font-family:var(--mono);
    letter-spacing:.14em; text-transform:uppercase; border-radius:9px; font-size:13px; padding:14px 18px; text-align:center; text-decoration:none; display:inline-block}
  .btn-fire{background:linear-gradient(180deg,#ff7a2a,#c4400a); color:#180a02; font-weight:700;
    box-shadow:0 0 0 1px #7a3208,0 0 24px #e8520a44, 0 2px 0 #ffb37a55 inset; flex:1 1 auto; min-width:220px}
  .btn-fire:hover{filter:brightness(1.07)} .btn-fire:active{transform:translateY(1px)}
  .copy{font-family:var(--mono); font-size:10.5px; letter-spacing:.1em; text-transform:uppercase; color:var(--ink);
    background:#241a0f; border:1px solid var(--line2); border-radius:8px; padding:0 14px; cursor:pointer}
  .copy:hover{border-color:var(--ember); color:var(--ember-bright)} .copy:active{transform:translateY(1px)}
  .copy.ok{color:var(--gold); border-color:#6e5212}

  .details{flex:0 0 auto; margin-top:12px}
  .disclose{width:100%; text-align:center; background:#160f08; color:var(--muted); border:1px solid var(--line2);
    border-radius:9px; padding:12px; font-family:var(--mono); font-size:11.5px; letter-spacing:.2em; text-transform:uppercase; cursor:pointer}
  .disclose:hover{color:var(--ember-bright); border-color:var(--ember)}
  .panel{display:none; border:1px solid var(--line2); border-top:0; border-radius:0 0 12px 12px;
    background:linear-gradient(180deg,var(--iron),var(--iron2)); padding:16px; margin-top:-6px; position:relative}
  .panel::after{content:""; position:absolute; inset:0; background-image:var(--grain); background-size:140px 140px; opacity:.04; mix-blend-mode:overlay; pointer-events:none; border-radius:0 0 12px 12px}
  body.details-open .panel{display:block} body.details-open .disclose{border-radius:12px 12px 0 0; color:var(--ember-bright); border-color:var(--ember)}

  .field{margin:12px 0; position:relative; z-index:1} .field:first-child{margin-top:0}
  .field label{display:block; font-family:var(--mono); font-size:10.5px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); margin:0 0 6px}
  .copybox{display:flex; gap:8px; align-items:stretch}
  .val{flex:1; min-width:0; font-family:var(--mono); font-size:13px; color:var(--ink); background:#0a0604;
    border:1px solid var(--line2); border-radius:8px; padding:10px 12px; overflow-x:auto; white-space:nowrap}
  a.val{display:block; text-decoration:none; color:var(--ember-bright)} a.val:hover{text-decoration:underline}
  textarea.val{white-space:pre-wrap; word-break:break-word; height:108px; resize:vertical; line-height:1.45; width:100%}
  input.val{width:100%}
  .pair{display:grid; grid-template-columns:1fr 1fr; gap:12px} @media (max-width:560px){.pair{grid-template-columns:1fr}}
  .talisman{border:1px solid var(--line2); background:#1a0f0633; border-radius:10px; padding:14px; margin-top:4px}
  .warn{font-size:12.5px; color:#f0c79a; background:#2a1602; border:1px solid var(--line2); border-radius:8px; padding:9px 11px; margin:0 0 12px}
  .warn b{color:var(--ember-bright)}
  .seal{margin-top:14px; width:100%}
  .patmsg{font-family:var(--mono); font-size:11px; letter-spacing:.1em; margin:8px 0 0; min-height:14px}
  .patmsg.ok{color:var(--gold)} .patmsg.err{color:#ff6f61}

  .gate{flex:1 1 auto; display:grid; place-items:center; text-align:center; font-family:var(--serif); font-style:italic;
    color:var(--muted); font-size:15px; padding:20px}

  :focus-visible{outline:2px solid var(--ember); outline-offset:2px; border-radius:6px}
  @media (prefers-reduced-motion:reduce){*{animation:none !important; transition:none !important}}
</style>
</head>
<body data-state="binding">
  <div class="app">
    <div class="topbar">
      <span class="sigil">&#9874; <b>WEYLAND</b> &middot; DIVINE SMITH &middot; FLEET BOUND</span>
      <span class="sigil" id="live" style="opacity:.5">&#9672; live</span>
    </div>

    <header class="hero">
      <p class="eyebrow" id="eyebrow">A binding is upon us</p>
      <div class="plate"><p class="name" id="piname">&hellip;</p></div>
      <p class="subtitle"><span class="glyph">&#9672;</span><span id="subtext">the forge is lit</span></p>
    </header>

    <div class="rosterwrap"><ul class="roster" id="roster"></ul></div>

    <section class="authcard" id="authcard" style="display:none" aria-label="The forge demands tribute">
      <h3 id="auth-title"></h3>
      <p class="sub" id="auth-sub"></p>
      <div class="authrow">
        <a class="btn btn-fire" id="auth-btn" href="#" target="_blank" rel="noopener">Swear the oath &rarr;</a>
        <code class="rune" id="auth-code" style="display:none"></code>
        <button class="copy" id="auth-copy" data-copy="auth-code" type="button" style="display:none">Copy</button>
      </div>
      <p class="instr" id="auth-instr"></p>
    </section>

    <div class="details" id="details" style="display:none">
      <button class="disclose" id="disclose" type="button" aria-expanded="false" aria-controls="panel">Consult the talisman &#9662;</button>
      <section class="panel" id="panel" aria-label="Connector details">
        <div class="field"><label for="f-url">MCP URL</label>
          <div class="copybox"><code class="val" id="f-url"></code><button class="copy" data-copy="f-url" type="button">Copy</button></div></div>
        <div class="pair">
          <div class="field"><label for="f-cid">OAuth Client ID</label>
            <div class="copybox"><code class="val" id="f-cid"></code><button class="copy" data-copy="f-cid" type="button">Copy</button></div></div>
          <div class="field"><label>OAuth Client Secret</label><code class="val" style="color:var(--muted)">&mdash; leave blank (PKCE) &mdash;</code></div>
        </div>
        <div class="field"><label for="f-bearer">Bearer token &mdash; speak it once into the consent page (your only copy)</label>
          <div class="copybox"><code class="val" id="f-bearer"></code><button class="copy" data-copy="f-bearer" type="button">Copy</button></div></div>
        <div class="pair">
          <div class="field"><label for="f-ct">Consent &mdash; tunnel</label>
            <div class="copybox"><a class="val" id="f-ct" href="#" target="_blank" rel="noopener"></a><button class="copy" data-copy="f-ct" type="button">Copy</button></div></div>
          <div class="field"><label for="f-cl">Consent &mdash; local fallback</label>
            <div class="copybox"><a class="val" id="f-cl" href="#" target="_blank" rel="noopener"></a><button class="copy" data-copy="f-cl" type="button">Copy</button></div></div>
        </div>
        <div class="field"><label for="f-repo">Per-Pi repo (the chronicles)</label>
          <div class="copybox"><a class="val" id="f-repo" href="#" target="_blank" rel="noopener"></a><button class="copy" data-copy="f-repo" type="button">Copy</button></div></div>
        <div class="field"><label for="f-proj">Project instructions</label>
          <div class="copybox"><textarea class="val" id="f-proj" readonly></textarea><button class="copy" data-copy="f-proj" type="button">Copy</button></div></div>
        <div class="talisman">
          <p class="warn"><b>WEYLAND_PAT &mdash; permanent &amp; shared across all minions.</b> Do not revoke unless rotating every minion. Normally drawn from your private <code>weyland-pat</code> gist; offer it here only to override.</p>
          <div class="field" style="margin:0"><label for="f-pat">Fine-grained PAT (weyland + weyland-secrets, Contents R/W)</label>
            <input class="val" id="f-pat" type="password" autocomplete="off" spellcheck="false" placeholder="github_pat_&hellip; (blank = draw from the vault gist)"></div>
          <button class="copy" id="patsave" type="button" style="margin-top:10px; padding:9px 14px">Offer the talisman</button>
          <p class="patmsg" id="patmsg"></p>
        </div>
        <button class="btn btn-fire seal" type="button" id="seal">&#9874; The rite is complete &mdash; bind the minion</button>
      </section>
    </div>

    <div class="gate" id="gate" style="display:none">open the link printed in the terminal &mdash; it carries the key that opens this rite</div>
  </div>

<script>
  var K = new URLSearchParams(location.search).get("k") || "";
  // internal phase name -> [ritual label, completion word]
  var PH = {
    preflight:["The forge is inspected","the forge is ready"],
    identity:["The minion receives its name","name bound in iron"],
    packages:["Tools of war are gathered","arsenal consecrated"],
    tailscale:["The minion enters the realm","fealty sworn"],
    github_auth:["GitHub demands tribute","tribute paid in blood"],
    per_pi_repo:["The chronicles are opened","chronicles sealed"],
    tunnel:["The passage through the void is opened","the void crossed"],
    claude_code:["The intelligence is summoned","awakened and bound"],
    connector:["The connector is forged","the sigil set"],
    vault:["The ancient secrets are retrieved","secrets bestowed"],
    selfdoc:["The minion speaks its name","the name spoken"],
    summary:["The induction is sealed","the rite is complete"]
  };
  var AUTH = {
    github:{t:"THE FORGE DEMANDS A BLOOD OATH", s:"GitHub guards the ancient gate — present yourself or the fire dies", b:"Swear fealty to GitHub →", i:"speak the rune · swear the oath · the gate shall open"},
    tailscale:{t:"THE TAILNET DEMANDS FEALTY", s:"swear allegiance to the realm or be cast out", b:"Pledge yourself to Tailscale →", i:"open the gate · swear the oath · enter the realm"},
    cloudflare:{t:"THE GATEKEEPER STIRS", s:"Cloudflare guards the passage — offer your credentials to cross the void", b:"Present yourself to Cloudflare →", i:"the void cannot be crossed without tribute"},
    anthropic:{t:"THE INTELLIGENCE AWAITS AWAKENING", s:"the ancient mind will not stir without your blessing", b:"Summon Claude into service →", i:"grant leave · speak the words · the intelligence awakens"}
  };
  var STAMP_RUN = "the hammer strikes", STAMP_PEND = "awaits the rite", STAMP_ERR = "the strike falters";
  var ORDER = ["preflight","identity","packages","tailscale","github_auth","per_pi_repo","tunnel","claude_code","connector","vault","selfdoc","summary"];

  var roster = document.getElementById("roster");
  function txt(id,v){ var e=document.getElementById(id); if(e) e.textContent = v==null?"":v; }
  function val(id,v){ var e=document.getElementById(id); if(e) e.value = v==null?"":v; }
  function href(id,v){ var e=document.getElementById(id); if(e){ e.textContent=v==null?"":v; e.setAttribute("href", v||"#"); } }

  function renderRoster(phases){
    // map by name; fall back to ORDER if state has none yet
    var byName = {}; (phases||[]).forEach(function(p){ byName[p.name]=p.status; });
    roster.innerHTML = "";
    ORDER.forEach(function(name){
      var meta = PH[name] || [name,"done"]; var st = byName[name] || "pending";
      var cls = st==="done"?"done":st==="running"?"run":st==="error"?"error":"pend";
      var stamp = st==="done"?meta[1]:st==="running"?STAMP_RUN:st==="error"?STAMP_ERR:STAMP_PEND;
      var li=document.createElement("li"); li.className="is-"+cls;
      li.innerHTML='<span class="badge '+cls+'"><span>'+(st==="done"?"✦":st==="error"?"!":"")+'</span></span><span class="label"></span><span class="stamp"></span>';
      li.querySelector(".label").textContent=meta[0];
      li.querySelector(".stamp").textContent=stamp;
      roster.appendChild(li);
    });
  }

  function applyState(s){
    document.getElementById("gate").style.display="none";
    document.querySelector(".rosterwrap").style.display="";
    var ready = !!(s.result && s.result.ready);
    document.body.setAttribute("data-state", ready?"complete":"binding");
    txt("piname", (s.pi_name && s.pi_name.length) ? s.pi_name : "…");
    if(ready){
      txt("eyebrow","The rite is complete");
      txt("subtext","forged in iron · sworn by ancient oath · "+(s.pi_name||"the minion")+" serves");
    } else {
      txt("eyebrow","A binding is upon us");
      var pn=(s.pi_name && s.pi_name.length)?s.pi_name:"the minion";
      txt("subtext","Weyland's hammer falls — "+pn+" shall be bound");
    }
    renderRoster(s.phases);

    // auth card
    var a=s.action, ac=document.getElementById("authcard");
    if(a && a.active){
      var c=AUTH[a.provider]||{t:"THE FORGE DEMANDS TRIBUTE",s:"present yourself",b:"Proceed →",i:""};
      txt("auth-title",c.t); txt("auth-sub",c.s); txt("auth-instr",c.i);
      var btn=document.getElementById("auth-btn"); btn.textContent=c.b; btn.setAttribute("href", a.url||"#");
      var code=document.getElementById("auth-code"), cp=document.getElementById("auth-copy");
      if(a.code){ code.textContent=a.code; code.style.display=""; cp.style.display=""; }
      else { code.style.display="none"; cp.style.display="none"; }
      ac.style.display="";
    } else ac.style.display="none";

    // result panel
    var d=document.getElementById("details");
    if(ready){
      d.style.display="";
      var r=s.result;
      txt("f-url",r.mcp_url); txt("f-cid",r.client_id||"weyland-mcp-claude-ai"); txt("f-bearer",r.bearer);
      href("f-ct",r.consent_tunnel); href("f-cl",r.consent_local); href("f-repo",r.repo);
      val("f-proj",r.project_instructions);
    } else d.style.display="none";
  }

  function gate(msg){
    document.querySelector(".rosterwrap").style.display="none";
    document.getElementById("authcard").style.display="none";
    document.getElementById("details").style.display="none";
    var g=document.getElementById("gate"); g.style.display=""; if(msg) g.textContent=msg;
  }

  function tick(){
    fetch("/state?k="+encodeURIComponent(K), {cache:"no-store"})
      .then(function(res){ if(res.status===403){ gate("open the link printed in the terminal — it carries the key that opens this rite"); return null;} return res.json(); })
      .then(function(j){ if(j) applyState(j); })
      .catch(function(){ /* transient; keep last view */ });
  }

  // disclosure
  document.getElementById("disclose").addEventListener("click", function(){
    var open=!document.body.classList.contains("details-open");
    document.body.classList.toggle("details-open", open);
    this.setAttribute("aria-expanded", String(open));
    this.innerHTML = open ? "Seal the talisman &#9652;" : "Consult the talisman &#9662;";
    if(open) document.getElementById("panel").scrollIntoView({behavior:"smooth",block:"start"});
  });
  // copy buttons
  function flash(b){var t=b.textContent;b.textContent="Copied";b.classList.add("ok");setTimeout(function(){b.textContent=t;b.classList.remove("ok");},1400);}
  function cp(text,b){ if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text).then(function(){flash(b);},function(){fb(text,b);});} else fb(text,b); }
  function fb(text,b){var ta=document.createElement("textarea");ta.value=text;ta.style.position="fixed";ta.style.opacity="0";document.body.appendChild(ta);ta.select();try{document.execCommand("copy");flash(b);}catch(e){}document.body.removeChild(ta);}
  document.addEventListener("click", function(e){
    var b=e.target.closest(".copy"); if(!b||!b.getAttribute("data-copy")) return;
    var el=document.getElementById(b.getAttribute("data-copy")); if(!el) return;
    var text=(el.tagName==="TEXTAREA"||el.tagName==="INPUT")?el.value:el.textContent;
    cp((text||"").trim(), b);
  });
  // save PAT
  document.getElementById("patsave").addEventListener("click", function(){
    var pat=document.getElementById("f-pat").value.trim(), m=document.getElementById("patmsg");
    if(!pat){ m.className="patmsg err"; m.textContent="no talisman offered"; return; }
    m.className="patmsg"; m.textContent="offering…";
    fetch("/save-pat?k="+encodeURIComponent(K), {method:"POST", headers:{"Content-Type":"application/x-www-form-urlencoded"}, body:"pat="+encodeURIComponent(pat)})
      .then(function(r){ if(r.ok){ m.className="patmsg ok"; m.textContent="the talisman is bound to the forge"; document.getElementById("f-pat").value=""; }
        else if(r.status===400){ m.className="patmsg err"; m.textContent="this is no true talisman (github_pat_ / ghp_)"; }
        else { m.className="patmsg err"; m.textContent="the forge rejected it"; } })
      .catch(function(){ m.className="patmsg err"; m.textContent="the forge could not be reached"; });
  });
  // seal -> /done
  document.getElementById("seal").addEventListener("click", function(){
    var b=this; b.textContent="⚒ the minion is bound"; b.disabled=true;
    fetch("/done?k="+encodeURIComponent(K), {method:"POST"}).catch(function(){});
  });

  tick(); setInterval(tick, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
