import io, os, re, time, secrets, threading
from flask import Flask, Request, request, jsonify, send_file, render_template_string
from werkzeug.formparser import FormDataParser
from werkzeug.utils import secure_filename

TTL = int(os.environ.get("SHARE_TTL", 3600))
MAX_FILE = int(os.environ.get("SHARE_MAX_FILE", 25 * 1024 * 1024))
MAX_TOTAL = int(os.environ.get("SHARE_MAX_TOTAL", 512 * 1024 * 1024))
MAX_ROOMS = int(os.environ.get("SHARE_MAX_ROOMS", 500))
MAX_TEXT = int(os.environ.get("SHARE_MAX_TEXT", 100_000))
MAX_ITEMS = int(os.environ.get("SHARE_MAX_ITEMS", 30))                 # entrées gardées par room
MAX_ITEM_CHARS = int(os.environ.get("SHARE_MAX_ITEM_CHARS", 200_000))  # poids du texte, par room
MAX_PENDING = int(os.environ.get("SHARE_MAX_PENDING", 5))              # demandes en attente
PENDING_TTL = int(os.environ.get("SHARE_PENDING_TTL", 180))
MAX_MEMBERS = int(os.environ.get("SHARE_MAX_MEMBERS", 10))
MASTER_GONE = int(os.environ.get("SHARE_MASTER_GONE", 60))   # silence au-delà duquel on le déclare parti

class MemoryParser(FormDataParser):
    """Par défaut werkzeug écrit sur disque au-delà de 500 Ko. Ici tout reste en RAM,
    borné par MAX_CONTENT_LENGTH : rien de ce qui transite ne touche un disque."""
    def __init__(self, *a, **kw):
        kw["stream_factory"] = lambda *args, **kwargs: io.BytesIO()
        super().__init__(*a, **kw)


class MemoryRequest(Request):
    form_data_parser_class = MemoryParser


app = Flask(__name__)
app.request_class = MemoryRequest
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE + 4096  # werkzeug rejette au-delà -> 413

if os.environ.get("SHARE_TRUST_PROXY"):
    # hops = nombre de proxys de confiance devant l'app. 1 derrière Caddy/nginx,
    # 2 sur Cloud Run (X-Forwarded-For: <client>,<load-balancer>). À vérifier sur
    # l'IP affichée dans la liste des postes connectés.
    from werkzeug.middleware.proxy_fix import ProxyFix
    hops = int(os.environ.get("SHARE_PROXY_HOPS", 1))
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops)

CODE_RE = re.compile(r"^[0-9A-F]{6}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
def cookie_name(code):
    return "share_" + code          # un cookie par room : plusieurs onglets, plusieurs rooms


lock = threading.Lock()
# code -> {items:[{id,kind:"text"|"file",ts, text|name+data}], v, ts,
#          tokens:{tok:{ip,ua,master}}, pending:{id:{ip,ua,ts,state,tok}}}
rooms = {}
fails = {}   # ip -> [timestamps des joins ratés]


def who():
    return (request.remote_addr or "?", request.headers.get("User-Agent", "")[:200])


def cleanup(now):
    for k in [k for k, v in rooms.items() if now - v["ts"] > TTL]:
        rooms.pop(k, None)
    for r in rooms.values():
        for i in [i for i, p in r["pending"].items()
                  if now - p["ts"] > PENDING_TTL and p["state"] == "pending"]:
            r["pending"][i]["state"] = "no"
    for ip in [ip for ip, t in fails.items() if not t or now - t[-1] > 60]:
        fails.pop(ip, None)


def total_bytes():
    return sum(len(i["data"]) for v in rooms.values()
               for i in v["items"] if i["kind"] == "file")


def add(r, item):
    """Empile une entrée et rogne les plus anciennes jusqu'à respecter les plafonds."""
    r["v"] += 1
    item["id"] = secrets.token_urlsafe(8)
    item["ts"] = time.time()
    r["items"].append(item)
    while len(r["items"]) > MAX_ITEMS or \
            sum(len(i["text"]) for i in r["items"] if i["kind"] == "text") > MAX_ITEM_CHARS:
        r["items"].pop(0)
    r["ts"] = item["ts"]
    return item["id"]


def listing(r):
    """Métadonnées + texte, sans les octets des fichiers. Plus récent en tête."""
    return [{"id": i["id"], "kind": i["kind"], "ts": i["ts"],
             **({"text": i["text"]} if i["kind"] == "text"
                else {"name": i["name"], "size": len(i["data"])})}
            for i in reversed(r["items"])]


def throttled(ip, now):
    # ponytail: compteur en mémoire, par process. Un vrai rate-limit partagé
    # (redis) seulement si on passe à plusieurs instances.
    t = [x for x in fails.get(ip, []) if now - x < 60]
    fails[ip] = t
    return len(t) >= 10


def norm(code):
    """"a3f 9c2" -> "A3F9C2" : les espaces de lisibilité ne doivent pas gêner."""
    return re.sub(r"\s+", "", code or "").upper()


def room(code):
    """Room valide et vivante, ou None. Appeler sous `lock`."""
    code = norm(code)
    if not CODE_RE.match(code):
        return None
    r = rooms.get(code)
    if r and time.time() - r["ts"] > TTL:
        rooms.pop(code, None)
        return None
    return r


def member(r, master_only=False):
    """Le porteur du cookie, si son IP ET son navigateur correspondent. Sinon None."""
    m = r["tokens"].get(request.cookies.get(cookie_name(r["code"]), ""))
    if not m or (m["ip"], m["ua"]) != who():
        return None
    if master_only and not m["master"]:
        return None
    m["seen"] = time.time()
    return m


def master_alive(r, now=None):
    now = now or time.time()
    return any(x["master"] and now - x["seen"] < MASTER_GONE for x in r["tokens"].values())


def succeed(r):
    """Master silencieux : le plus ancien membre encore actif reprend la main.
    Sans ça, plus personne ne peut accepter d'arrivant jusqu'à l'expiration de la room."""
    now = time.time()
    if master_alive(r, now):
        return
    live = [x for x in r["tokens"].values() if now - x["seen"] < MASTER_GONE]
    if not live:
        return
    for x in r["tokens"].values():
        x["master"] = False
    min(live, key=lambda x: x["since"])["master"] = True


def new_member(ip, ua, master=False):
    now = time.time()
    return {"id": secrets.token_urlsafe(8), "ip": ip, "ua": ua,
            "master": master, "since": now, "seen": now}


def with_cookie(resp, code, tok):
    resp.set_cookie(cookie_name(code), tok, max_age=TTL, httponly=True,
                    samesite="Strict", secure=request.is_secure)
    return resp


HTML = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Partage rapide</title>
<style>
  *{box-sizing:border-box;font-family:system-ui,sans-serif}
  body{max-width:560px;margin:10vh auto;padding:0 16px;color:#222}
  h1{font-size:1.3rem;font-weight:600}
  input,textarea,button{width:100%;padding:10px;border:1px solid #ccc;
    border-radius:8px;font-size:1rem;margin:6px 0}
  button{background:#111;color:#fff;border:none;cursor:pointer}
  button:hover{background:#333}
  .row{display:flex;gap:8px}.row input{flex:1}
  #zone,#wait{display:none}
  .hidden{display:none}
  small{color:#888} #msg{color:#b00}
  #hist{margin:4px 0}
  .item{display:flex;align-items:center;gap:8px;padding:8px 10px;margin:4px 0;
    border:1px solid #ddd;border-radius:8px;cursor:pointer;background:#fafafa}
  .item:hover{background:#eee}
  .item span{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    font-family:ui-monospace,monospace;font-size:.9rem}
  .item b{color:#888;font-weight:400;font-size:.8rem}
  .item b.x{color:#b00;font-size:1rem;padding:0 4px}
  .item b.x:hover{background:#fdd;border-radius:4px}
  .head{display:flex;justify-content:space-between;align-items:baseline;margin-top:12px}
  .head button{width:auto;background:none;color:#888;border:none;font-size:.85rem;padding:0}
  .head button:hover{color:#b00;background:none}
  .ask{border:2px solid #e8a33d;background:#fff8ec;border-radius:8px;padding:10px;margin:8px 0}
  .ask code{font-size:.85rem;color:#555;word-break:break-all}
  .ask .row button{width:auto;flex:1;margin-top:8px}
  .ask .no{background:#fff;color:#b00;border:1px solid #b00}
  .badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:.75rem;
    vertical-align:middle;background:#eee;color:#555}
  .badge.m{background:#1b5e20;color:#fff}
  .peer{display:flex;align-items:center;gap:8px;padding:6px 10px;margin:4px 0;
    border:1px solid #eee;border-radius:8px;font-size:.85rem}
  .peer div{flex:1;min-width:0}
  .peer code{color:#888;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .peer button{width:auto;margin:0;padding:4px 8px;font-size:.8rem;background:#fff;
    color:#b00;border:1px solid #ccc}
</style></head><body>
<h1>📋 Partage rapide</h1>

<div id="join">
  <div class="row">
    <input id="code" placeholder="Code (laisser vide pour créer)" maxlength="7">
    <button style="width:auto" onclick="enter()">Go</button>
  </div>
  <small>Partagez le code avec l'autre poste. Expire après 1h.</small>
</div>

<div id="wait">
  <p>⏳ Demande envoyée. Le créateur de la session doit vous accepter…</p>
  <small id="waitinfo"></small>
</div>

<div id="zone">
  <p>Code : <b id="roomcode"></b> <span id="role" class="badge"></span></p>

  <div id="asks"></div>

  <div class="head hidden" id="peershead"><small>Postes connectés</small></div>
  <div id="peers"></div>

  <textarea id="text" rows="6" placeholder="Collez votre texte ici…"></textarea>
  <button onclick="sendText()">Partager</button>
  <small>Entrée pour partager, Maj+Entrée pour aller à la ligne.</small>
  <hr>
  <input type="file" id="file">
  <button onclick="sendFile()">Envoyer le fichier</button>
  <div class="head hidden" id="histhead">
    <small>Historique — cliquez pour copier ou télécharger</small>
    <button onclick="clearItems()">tout effacer</button>
  </div>
  <div id="hist"></div>
</div>
<p id="msg"></p>

<script>
const MAX_FILE = __MAX_FILE__;
let code = null, v = -1, master = false;
const $ = id => document.getElementById(id);
const say = m => { $('msg').textContent = m || ''; };

async function enter(){
  const c = $('code').value.replace(/\s+/g, '').toUpperCase();
  say('');
  let r;
  try {
    r = await fetch('/api/join', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({code: c || null})});
  } catch(e) { return say("Serveur injoignable."); }
  if(!r.ok){
    const e = await r.json().catch(() => ({}));
    return say(r.status === 429 ? "Trop de tentatives, patientez une minute."
              : r.status === 503 ? "Service saturé, réessayez plus tard."
              : (e.error || "Code introuvable."));
  }
  const d = await r.json();
  code = d.code;
  if(r.status === 202){                       // en attente d'approbation
    $('join').classList.add('hidden');
    $('wait').style.display = 'block';
    $('waitinfo').textContent = "Il verra votre IP (" + d.ip + ") et votre navigateur.";
    return waitApproval(d.req);
  }
  opened(d.role);
}

function setRole(role){
  master = (role === 'master');
  $('role').textContent = master ? '👑 Master' : 'invité';
  $('role').className = 'badge' + (master ? ' m' : '');
  if(!master){ $('asks').textContent = ''; $('peers').textContent = ''; $('peershead').classList.add('hidden'); }
}

function opened(role){
  $('join').classList.add('hidden');
  $('wait').style.display = 'none';
  $('zone').style.display = 'block';
  $('roomcode').textContent = code.slice(0, 3) + ' ' + code.slice(3);   // lisibilité à la dictée
  setRole(role);
  $('text').addEventListener('keydown', e => {          // Entrée = partager, Maj+Entrée = saut de ligne
    if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); sendText(); }
  });
  poll();
}

async function waitApproval(req){
  let r;
  try { r = await fetch('/api/join/' + code + '/' + req); }
  catch(e) { return setTimeout(() => waitApproval(req), 2000); }
  if(r.status === 403 || r.status === 404){
    $('wait').style.display = 'none';
    $('join').classList.remove('hidden');
    return say("Demande refusée ou expirée.");
  }
  const d = await r.json();
  if(d.state === 'ok') return opened('member');
  $('waitinfo').textContent = d.state === 'master_gone'
    ? "⚠ Le créateur de la session ne répond plus. Prévenez-le, ou créez une nouvelle session."
    : "Il verra votre IP et votre navigateur.";
  setTimeout(() => waitApproval(req), 2000);
}

async function poll(){
  try {
    const r = await fetch('/api/room/' + code);
    if(r.status === 404) return say("Session expirée. Rechargez la page.");
    if(r.status === 403) return location.reload();     // déconnecté par le master
    if(r.ok){
      const d = await r.json();
      if((d.role === 'master') !== master){        // promu à la mort du master
        setRole(d.role);
        if(master) say("Le créateur ne répondait plus : vous validez désormais les arrivants.");
      }
      if(master){ renderAsks(d.pending || []); renderPeers(d.members || []); }
      if(d.v !== v){ v = d.v; await loadItems(); }
      say('');
    }
  } catch(e) { say("Connexion perdue, nouvelle tentative…"); }
  setTimeout(poll, 2000);
}

function renderAsks(pending){
  const box = $('asks');
  const key = 'k' + pending.map(p => p.id).join('|');   // préfixe : '' vide != état "à re-rendre"
  if(box.dataset.k === key) return;
  box.dataset.k = key;
  box.textContent = '';
  for(const p of pending){
    const d = document.createElement('div');
    d.className = 'ask';
    const t = document.createElement('p');
    t.textContent = "Un poste demande à rejoindre — IP " + p.ip;   // textContent : pas d'injection
    const ua = document.createElement('code');
    ua.textContent = p.ua || "navigateur inconnu";
    const row = document.createElement('div');
    row.className = 'row';
    const ok = document.createElement('button');
    ok.textContent = "Accepter";
    ok.onclick = () => decide(p.id, 'ok');
    const no = document.createElement('button');
    no.className = 'no'; no.textContent = "Refuser";
    no.onclick = () => decide(p.id, 'no');
    row.append(ok, no);
    d.append(t, ua, row);
    box.append(d);
  }
}

async function decide(id, state){
  await fetch('/api/room/' + code + '/pending/' + id, {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({state})});
  $('asks').dataset.k = '';
}

function renderPeers(members){
  const box = $('peers');
  const key = 'k' + members.map(m => m.id).join('|');
  $('peershead').classList.toggle('hidden', !members.length);
  if(box.dataset.k === key) return;
  box.dataset.k = key;
  box.textContent = '';
  for(const m of members){
    const el = document.createElement('div');
    el.className = 'peer';
    const info = document.createElement('div');
    const t = document.createElement('b');
    t.textContent = m.ip + (m.master ? ' 👑' : '') + (m.me ? ' (vous)' : '');
    const ua = document.createElement('code');
    ua.textContent = (m.ua || 'navigateur inconnu') + ' — depuis ' +
      new Date(m.since * 1000).toLocaleTimeString('fr-FR');
    info.append(t, ua);
    el.append(info);
    if(!m.me){
      const b = document.createElement('button');
      b.textContent = 'déconnecter';
      b.onclick = async () => {
        await fetch('/api/room/' + code + '/members/' + m.id, {method:'DELETE'});
        box.dataset.k = '';
      };
      el.append(b);
    }
    box.append(el);
  }
}

async function loadItems(){
  const r = await fetch('/api/room/' + code + '/items');
  if(!r.ok) return;
  const items = (await r.json()).items;
  const box = $('hist');
  box.textContent = '';
  $('histhead').classList.toggle('hidden', !items.length);
  for(const it of items){
    const el = document.createElement('div');
    el.className = 'item';
    const heure = new Date(it.ts * 1000).toLocaleTimeString('fr-FR');
    const s = document.createElement('span');
    const b = document.createElement('b');
    if(it.kind === 'file'){
      s.textContent = '⬇ ' + it.name;                            // textContent = pas d'injection HTML
      b.textContent = size(it.size) + ' — ' + heure;
      el.onclick = () => { location.href = '/api/room/' + code + '/file/' + it.id; };
    } else {
      s.textContent = it.text.replace(/\s+/g, ' ').slice(0, 200);
      b.textContent = '📋 ' + heure;
      el.onclick = () => copy(it.text, b, heure);
    }
    const x = document.createElement('b');
    x.className = 'x'; x.textContent = '✕'; x.title = 'supprimer';
    x.onclick = async e => {
      e.stopPropagation();                   // sinon le clic déclenche aussi l'action de la ligne
      await fetch('/api/room/' + code + '/items/' + it.id, {method:'DELETE'});
      v = -1;
    };
    el.append(s, b, x);
    box.append(el);
  }
}

function copy(text, badge, heure){
  navigator.clipboard.writeText(text).then(() => {
    badge.textContent = '✓ copié';
    setTimeout(() => { badge.textContent = '📋 ' + heure; }, 1200);
  }).catch(() => say("Copie refusée par le navigateur."));
}

async function clearItems(){
  if(!confirm("Effacer tout l'historique, fichiers compris ?")) return;
  await fetch('/api/room/' + code + '/items', {method:'DELETE'});
  v = -1;
}

async function sendText(){
  const text = $('text').value;
  if(!text.trim()) return;
  const r = await fetch('/api/room/' + code + '/text', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify({text})});
  if(!r.ok) return say("Envoi refusé (texte trop long ou session expirée).");
  $('text').value = '';            // la zone est un champ de saisie : le partagé vit dans l'historique
  say('');
  v = -1;                          // recharge l'historique au prochain sondage
}

async function sendFile(){
  const f = $('file').files[0];
  if(!f) return;
  if(f.size > MAX_FILE) return say("Fichier trop volumineux (max " + (MAX_FILE >> 20) + " Mo).");
  const fd = new FormData(); fd.append('file', f);
  say("Envoi…");
  const r = await fetch('/api/room/' + code + '/file', {method:'POST', body: fd});
  if(r.ok){ $('file').value = ''; v = -1; say("Fichier envoyé."); }
  else say(r.status === 413 ? "Fichier trop volumineux."
         : r.status === 507 ? "Espace serveur saturé." : "Échec de l'envoi.");
}

function size(n){
  return n < 1024 ? n + " o" : n < 1048576 ? (n / 1024).toFixed(0) + " Ko"
                                           : (n / 1048576).toFixed(1) + " Mo";
}

</script></body></html>"""


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    # rien ici n'est cachable : la page embarque le JS, le reste est de l'état volatil
    resp.headers.setdefault("Cache-Control", "no-store")
    resp.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
    return resp


@app.get("/")
def index():
    return render_template_string(HTML.replace("__MAX_FILE__", str(MAX_FILE)))


@app.get("/api/whoami")
def whoami():
    """Diagnostic proxy : dit quelle IP l'app retient pour VOUS, et pourquoi.
    Ne révèle que les données de votre propre requête."""
    ip, ua = who()
    xff = request.headers.get("X-Forwarded-For", "")
    return jsonify(ip=ip, x_forwarded_for=xff,
                   entrees=[x.strip() for x in xff.split(",")] if xff else [],
                   hops=int(os.environ.get("SHARE_PROXY_HOPS", 1)),
                   trust_proxy=bool(os.environ.get("SHARE_TRUST_PROXY")),
                   ua=ua)


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, rooms=len(rooms))


@app.post("/api/join")
def join():
    now = time.time()
    ip, ua = who()
    with lock:
        cleanup(now)
        c = norm((request.get_json(silent=True) or {}).get("code"))

        if not c:                                   # création : le créateur est master
            if len(rooms) >= MAX_ROOMS:
                return jsonify(error="busy"), 503
            while True:
                c = secrets.token_hex(3).upper()
                if c not in rooms:
                    break
            rooms[c] = {"code": c, "items": [], "v": 0,
                        "ts": now, "tokens": {}, "pending": {}}
            tok = secrets.token_urlsafe(32)
            rooms[c]["tokens"][tok] = new_member(ip, ua, master=True)
            return with_cookie(jsonify(code=c, role="master", ttl=TTL, max_file=MAX_FILE), c, tok)

        if throttled(ip, now):
            return jsonify(error="too many attempts"), 429
        r = room(c)
        if not r:
            fails.setdefault(ip, []).append(now)
            return jsonify(error="not found"), 404

        m = member(r)
        if m:                                       # déjà accepté (rechargement de page)
            r["ts"] = now
            return jsonify(code=c, role="master" if m["master"] else "member",
                           ttl=TTL, max_file=MAX_FILE)

        if len(r["tokens"]) >= MAX_MEMBERS:
            return jsonify(error="room full"), 503
        live = [p for p in r["pending"].values() if p["state"] == "pending"]
        if len(live) >= MAX_PENDING:
            return jsonify(error="too many pending"), 429
        if any((p["ip"], p["ua"]) == (ip, ua) for p in live):
            return jsonify(error="already pending"), 429

        req = secrets.token_urlsafe(16)
        r["pending"][req] = {"ip": ip, "ua": ua, "ts": now, "state": "pending", "tok": None}
        r["ts"] = now
    return jsonify(code=c, req=req, ip=ip), 202


@app.get("/api/join/<code>/<req>")
def join_status(code, req):
    """Sondé par le demandeur. Pas de cookie : le req id (128 bits) fait foi."""
    with lock:
        r = room(code)
        if not r or not ID_RE.match(req):
            return jsonify(error="not found"), 404
        p = r["pending"].get(req)
        if not p or (p["ip"], p["ua"]) != who():    # doit revenir du même poste
            return jsonify(error="not found"), 404
        if p["state"] == "no" or (p["state"] == "pending"
                                  and time.time() - p["ts"] > PENDING_TTL):
            r["pending"].pop(req, None)             # l'attente expire ici aussi, pas
            return jsonify(error="denied"), 403     # seulement dans cleanup()
        if p["state"] != "ok":
            # personne ne peut plus accepter : le dire tout de suite
            return jsonify(state="pending" if master_alive(r) else "master_gone")
        tok = p["tok"]
        r["pending"].pop(req, None)                 # jeton retiré une seule fois
        r["ts"] = time.time()
    return with_cookie(jsonify(state="ok", code=code, ttl=TTL, max_file=MAX_FILE), r["code"], tok)


@app.post("/api/room/<code>/pending/<req>")
def decide(code, req):
    state = (request.get_json(silent=True) or {}).get("state")
    if state not in ("ok", "no"):
        return jsonify(error="bad state"), 400
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r, master_only=True):
            return jsonify(error="forbidden"), 403
        p = r["pending"].get(req if ID_RE.match(req) else "")
        if not p or p["state"] != "pending":
            return jsonify(error="not found"), 404
        if state == "ok":
            if len(r["tokens"]) >= MAX_MEMBERS:
                return jsonify(error="room full"), 503
            tok = secrets.token_urlsafe(32)
            r["tokens"][tok] = new_member(p["ip"], p["ua"])
            p["tok"] = tok
        p["state"] = state
        r["ts"] = time.time()
    return jsonify(ok=True)


@app.delete("/api/room/<code>/members/<mid>")
def kick(code, mid):
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        m = member(r, master_only=True)
        if not m:
            return jsonify(error="forbidden"), 403
        tok = next((t for t, x in r["tokens"].items()
                    if x["id"] == mid and x is not m), None)
        if not tok:
            return jsonify(error="not found"), 404   # le master ne se déconnecte pas lui-même
        r["tokens"].pop(tok)
        r["ts"] = time.time()
    return jsonify(ok=True)


@app.get("/api/room/<code>")
def get_room(code):
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        m = member(r)
        if not m:
            return jsonify(error="forbidden"), 403
        succeed(r)                       # m["seen"] vient d'être mis à jour par member()
        r["ts"] = time.time()
        out = dict(v=r["v"], role="master" if m["master"] else "member")
        if m["master"]:
            out["pending"] = [{"id": i, "ip": p["ip"], "ua": p["ua"]}
                              for i, p in r["pending"].items() if p["state"] == "pending"]
            out["members"] = [{"id": x["id"], "ip": x["ip"], "ua": x["ua"],
                               "since": x["since"], "seen": x["seen"],
                               "master": x["master"], "me": x is m}
                              for x in r["tokens"].values()]
        return jsonify(out)


@app.get("/api/room/<code>/items")
def get_items(code):
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r):
            return jsonify(error="forbidden"), 403
        r["ts"] = time.time()
        return jsonify(items=listing(r), v=r["v"])


@app.delete("/api/room/<code>/items/<iid>")
def del_item(code, iid):
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r):
            return jsonify(error="forbidden"), 403
        before = len(r["items"])
        r["items"] = [i for i in r["items"] if i["id"] != iid]
        if len(r["items"]) == before:
            return jsonify(error="not found"), 404
        r["v"] += 1
        r["ts"] = time.time()
    return jsonify(ok=True)


@app.delete("/api/room/<code>/items")
def clear_items(code):
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r):
            return jsonify(error="forbidden"), 403
        r["items"], r["ts"] = [], time.time()
        r["v"] += 1
    return jsonify(ok=True)


@app.post("/api/room/<code>/text")
def set_text(code):
    text = (request.get_json(silent=True) or {}).get("text", "")
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        return jsonify(error="bad text"), 400
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r):
            return jsonify(error="forbidden"), 403
        last = next((i for i in reversed(r["items"]) if i["kind"] == "text"), None)
        if not text or (last and last["text"] == text):
            return jsonify(ok=True, skipped=True)     # vide ou doublon consécutif
        iid = add(r, {"kind": "text", "text": text})
    return jsonify(ok=True, id=iid)


@app.post("/api/room/<code>/file")
def set_file(code):
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r):
            return jsonify(error="forbidden"), 403
    f = request.files.get("file")
    if not f:
        return jsonify(error="no file"), 400
    data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        return jsonify(error="too large"), 413
    name = secure_filename(f.filename or "") or "fichier"
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r):
            return jsonify(error="forbidden"), 403
        if total_bytes() + len(data) > MAX_TOTAL:
            return jsonify(error="storage full"), 507
        fid = add(r, {"kind": "file", "name": name, "data": data})
    return jsonify(ok=True, id=fid, name=name)


@app.get("/api/room/<code>/file/<fid>")
def get_file(code, fid):
    with lock:
        r = room(code)
        if not r:
            return jsonify(error="not found"), 404
        if not member(r):
            return jsonify(error="forbidden"), 403
        f = next((i for i in r["items"]
                  if i["id"] == fid and i["kind"] == "file"), None)
        if not f:
            return jsonify(error="no file"), 404
        name, data = f["name"], f["data"]
        r["ts"] = time.time()
    return send_file(io.BytesIO(data), as_attachment=True, download_name=name,
                     mimetype="application/octet-stream")


if __name__ == "__main__":
    from waitress import serve
    host = os.environ.get("SHARE_HOST", "0.0.0.0")
    # PORT est imposé par Cloud Run / Heroku / Scaleway et doit primer.
    port = int(os.environ.get("PORT") or os.environ.get("SHARE_PORT", 5000))
    print(f"share: écoute sur {host}:{port}", flush=True)
    serve(app, host=host, port=port, threads=8)
