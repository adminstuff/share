"""Auto-check: python test_share.py"""
import io, time
import share as S

S.TTL = 1_000_000
MASTER = {"REMOTE_ADDR": "10.0.0.1", "HTTP_USER_AGENT": "Firefox/master"}
GUEST = {"REMOTE_ADDR": "10.0.0.2", "HTTP_USER_AGENT": "Chrome/guest"}

c = S.app.test_client()          # le master, sur toute la durée du test
c.environ_base.update(MASTER)


def guest(**over):
    g = S.app.test_client()
    g.environ_base.update({**GUEST, **over})
    return g


def new():
    r = c.post("/api/join", json={})
    assert r.status_code == 200 and r.get_json()["role"] == "master"
    return r.get_json()["code"]


# --- création, casse, rechargement de page ---------------------------------
code = new()
assert len(code) == 6
assert c.post("/api/join", json={"code": code.lower()}).get_json()["role"] == "master"
assert c.get(f"/api/room/{code.lower()}").status_code == 200

# code saisi avec des espaces (tel qu'affiché : "A3F 9C2")
espace = code[:3] + " " + code[3:]
assert c.post("/api/join", json={"code": espace}).get_json()["code"] == code
assert c.post("/api/join", json={"code": f"  {espace.lower()} "}).get_json()["code"] == code
assert c.get(f"/api/room/{espace}").status_code == 200

# codes invalides et inconnus -> 404, jamais 500
for bad in ["../etc", "ZZZZZZ", "", "A" * 200]:
    assert c.get(f"/api/room/{bad}").status_code == 404, bad

# --- un inconnu doit être approuvé par le master --------------------------
g = guest()
r = g.post("/api/join", json={"code": code})
assert r.status_code == 202, r.status_code
req = r.get_json()["req"]
assert g.get(f"/api/join/{code}/{req}").get_json()["state"] == "pending"

# tant qu'il n'est pas accepté, aucun accès
for path, meth in [(f"/api/room/{code}", "get"), (f"/api/room/{code}/items", "get"),
                   (f"/api/room/{code}/file/x", "get")]:
    assert getattr(g, meth)(path).status_code == 403, path
assert g.post(f"/api/room/{code}/text", json={"text": "intrus"}).status_code == 403

# le master voit la demande, avec IP et navigateur
p = c.get(f"/api/room/{code}").get_json()["pending"]
assert len(p) == 1 and p[0]["ip"] == "10.0.0.2" and p[0]["ua"] == "Chrome/guest", p

# un invité ne peut pas s'auto-accepter, ni un tiers décider
assert g.post(f"/api/room/{code}/pending/{req}", json={"state": "ok"}).status_code == 403
assert guest().post(f"/api/room/{code}/pending/{req}", json={"state": "ok"}).status_code == 403
assert c.post(f"/api/room/{code}/pending/{req}", json={"state": "peut-etre"}).status_code == 400

# le master accepte -> l'invité récupère son jeton, une seule fois
assert c.post(f"/api/room/{code}/pending/{req}", json={"state": "ok"}).status_code == 200
assert g.get(f"/api/join/{code}/{req}").get_json()["state"] == "ok"
assert g.get(f"/api/room/{code}").get_json()["role"] == "member"
assert g.get(f"/api/join/{code}/{req}").status_code == 404      # req consommé
assert "pending" not in g.get(f"/api/room/{code}").get_json()   # invité ne voit pas les demandes

# deux rooms en parallèle dans le même navigateur : les cookies ne s'écrasent pas
other = new()
assert c.get(f"/api/room/{other}").status_code == 200
assert c.get(f"/api/room/{code}").status_code == 200

# le jeton est lié à l'IP et au navigateur du demandeur
stolen = g.get_cookie(S.cookie_name(code)).value
for env in ({"REMOTE_ADDR": "10.0.0.9"}, {"HTTP_USER_AGENT": "curl/8"}):
    thief = guest(**env)
    thief.set_cookie(S.cookie_name(code), stolen)
    assert thief.get(f"/api/room/{code}").status_code == 403, env

# refus explicite
g2 = guest(REMOTE_ADDR="10.0.0.3")
req2 = g2.post("/api/join", json={"code": code}).get_json()["req"]
assert c.post(f"/api/room/{code}/pending/{req2}", json={"state": "no"}).status_code == 200
assert g2.get(f"/api/join/{code}/{req2}").status_code == 403
assert g2.get(f"/api/room/{code}").status_code == 403

# demande expirée -> refusée
g3 = guest(REMOTE_ADDR="10.0.0.4")
req3 = g3.post("/api/join", json={"code": code}).get_json()["req"]
S.rooms[code]["pending"][req3]["ts"] -= S.PENDING_TTL + 1
c.post("/api/join", json={})                       # déclenche cleanup()
assert g3.get(f"/api/join/{code}/{req3}").status_code == 403

# plafond des demandes en attente
for i in range(S.MAX_PENDING + 2):
    guest(REMOTE_ADDR=f"10.9.0.{i}").post("/api/join", json={"code": code})
live = [p for p in S.rooms[code]["pending"].values() if p["state"] == "pending"]
assert len(live) == S.MAX_PENDING, len(live)

# --- liste des postes connectés / déconnexion ----------------------------
d = c.get(f"/api/room/{code}").get_json()
mem = {m["ip"]: m for m in d["members"]}
assert mem["10.0.0.1"]["master"] and mem["10.0.0.1"]["me"], d["members"]
assert mem["10.0.0.2"]["ua"] == "Chrome/guest" and not mem["10.0.0.2"]["master"]
assert mem["10.0.0.2"]["since"] <= mem["10.0.0.2"]["seen"]
assert "members" not in g.get(f"/api/room/{code}").get_json()   # invisible pour l'invité

gid, mid = mem["10.0.0.2"]["id"], mem["10.0.0.1"]["id"]
assert g.delete(f"/api/room/{code}/members/{gid}").status_code == 403   # invité ne kicke pas
assert c.delete(f"/api/room/{code}/members/{mid}").status_code == 404   # ni le master lui-même
assert c.delete(f"/api/room/{code}/members/inconnu").status_code == 404
assert c.delete(f"/api/room/{code}/members/{gid}").status_code == 200
assert g.get(f"/api/room/{code}").status_code == 403                    # invité éjecté
assert c.get(f"/api/room/{code}").status_code == 200

# l'éjecté doit repasser par une demande
S.rooms[code]["pending"].clear()      # le test du plafond ci-dessus en a laissé
r = g.post("/api/join", json={"code": code})
assert r.status_code == 202
req = r.get_json()["req"]
c.post(f"/api/room/{code}/pending/{req}", json={"state": "ok"})
assert g.get(f"/api/join/{code}/{req}").get_json()["state"] == "ok"

# --- master mort : attente bornée et succession ---------------------------
mc = S.app.test_client(); mc.environ_base.update({"REMOTE_ADDR": "10.1.0.1", "HTTP_USER_AGENT": "M"})
room2 = mc.post("/api/join", json={}).get_json()["code"]

def joins(cl):
    q = cl.post("/api/join", json={"code": room2}).get_json()["req"]
    mc.post(f"/api/room/{room2}/pending/{q}", json={"state": "ok"})
    assert cl.get(f"/api/join/{room2}/{q}").get_json()["state"] == "ok"

a1 = S.app.test_client(); a1.environ_base.update({"REMOTE_ADDR": "10.1.0.2", "HTTP_USER_AGENT": "A"})
a2 = S.app.test_client(); a2.environ_base.update({"REMOTE_ADDR": "10.1.0.3", "HTTP_USER_AGENT": "B"})
joins(a1); joins(a2)

# le master se tait
for t in S.rooms[room2]["tokens"].values():
    if t["master"]:
        t["seen"] -= S.MASTER_GONE + 1

# un invité en attente est prévenu au lieu d'attendre dans le vide
w = S.app.test_client(); w.environ_base.update({"REMOTE_ADDR": "10.1.0.9", "HTTP_USER_AGENT": "W"})
wreq = w.post("/api/join", json={"code": room2}).get_json()["req"]
assert w.get(f"/api/join/{room2}/{wreq}").get_json()["state"] == "master_gone"

# le plus ancien membre actif reprend la main à son sondage
assert a1.get(f"/api/room/{room2}").get_json()["role"] == "master"
assert a2.get(f"/api/room/{room2}").get_json()["role"] == "member"
assert mc.get(f"/api/room/{room2}").get_json()["role"] == "member"   # l'ancien est rétrogradé

# et le nouveau master peut accepter
assert a1.post(f"/api/room/{room2}/pending/{wreq}", json={"state": "ok"}).status_code == 200
assert w.get(f"/api/join/{room2}/{wreq}").get_json()["state"] == "ok"

# l'attente expire d'elle-même, sans qu'aucun POST /api/join ne la déclenche
w2 = S.app.test_client(); w2.environ_base.update({"REMOTE_ADDR": "10.1.0.10", "HTTP_USER_AGENT": "W2"})
w2req = w2.post("/api/join", json={"code": room2}).get_json()["req"]
S.rooms[room2]["pending"][w2req]["ts"] -= S.PENDING_TTL + 1
assert w2.get(f"/api/join/{room2}/{w2req}").status_code == 403

# aucun membre actif : pas de succession arbitraire
solo = S.app.test_client(); solo.environ_base.update({"REMOTE_ADDR": "10.2.0.1", "HTTP_USER_AGENT": "S"})
room3 = solo.post("/api/join", json={}).get_json()["code"]
for t in S.rooms[room3]["tokens"].values():
    t["seen"] -= S.MASTER_GONE + 1
S.succeed(S.rooms[room3])
assert sum(t["master"] for t in S.rooms[room3]["tokens"].values()) == 1   # le master reste titulaire

# --- corps non-JSON accepté sans crash ------------------------------------
assert c.post(f"/api/room/{code}/text", data="pas du json",
              content_type="application/json").status_code in (200, 400)

# --- texte ----------------------------------------------------------------
assert c.post(f"/api/room/{code}/text", json={"text": "hello"}).status_code == 200
assert g.get(f"/api/room/{code}/items").get_json()["items"][0]["text"] == "hello"  # l'invité lit
assert c.post(f"/api/room/{code}/text", json={"text": "x" * (S.MAX_TEXT + 1)}).status_code == 400
assert c.post(f"/api/room/{code}/text", json={"text": 42}).status_code == 400

# --- historique unifié ----------------------------------------------------
def items(cl=c):
    return cl.get(f"/api/room/{code}/items").get_json()["items"]

assert [x["text"] for x in items()] == ["hello"]
assert items()[0]["kind"] == "text"
c.post(f"/api/room/{code}/text", json={"text": "hello"})          # doublon ignoré
c.post(f"/api/room/{code}/text", json={"text": "monde"})
c.post(f"/api/room/{code}/text", json={"text": ""})               # vide ignoré
assert [x["text"] for x in items()] == ["monde", "hello"]

# texte et fichiers dans la même liste, par ordre chronologique inverse
mix = c.post(f"/api/room/{code}/file",
             data={"file": (io.BytesIO(b"pdf"), "doc.pdf")}).get_json()["id"]
c.post(f"/api/room/{code}/text", json={"text": "après le fichier"})
assert [x["kind"] for x in items()] == ["text", "file", "text", "text"], items()
assert items()[1]["name"] == "doc.pdf" and items()[1]["size"] == 3
assert "text" not in items()[1] and "data" not in items()[1]      # les octets ne transitent pas
assert c.get(f"/api/room/{code}/file/{mix}").data == b"pdf"

# un plafond unique pour les deux types
S.MAX_ITEMS = 3
for i in range(3):
    c.post(f"/api/room/{code}/text", json={"text": f"n{i}"})
assert [x.get("text") for x in items()] == ["n2", "n1", "n0"]
assert c.get(f"/api/room/{code}/file/{mix}").status_code == 404   # le fichier a été évincé
S.MAX_ITEM_CHARS = 10
c.post(f"/api/room/{code}/text", json={"text": "x" * 9})
assert len(items()) == 1
S.MAX_ITEMS, S.MAX_ITEM_CHARS = 30, 200_000

# suppression à l'unité, quel que soit le type
t1 = c.post(f"/api/room/{code}/text", json={"text": "à jeter"}).get_json()["id"]
f1 = c.post(f"/api/room/{code}/file",
            data={"file": (io.BytesIO(b"x"), "j.bin")}).get_json()["id"]
assert guest(REMOTE_ADDR="10.7.7.7").delete(f"/api/room/{code}/items/{t1}").status_code == 403
assert c.delete(f"/api/room/{code}/items/{t1}").status_code == 200
assert c.delete(f"/api/room/{code}/items/{t1}").status_code == 404
assert c.delete(f"/api/room/{code}/items/{f1}").status_code == 200
assert c.get(f"/api/room/{code}/file/{f1}").status_code == 404
assert t1 not in [x["id"] for x in items()]

# la version change à chaque mutation : c'est elle qui déclenche le rechargement
v0 = c.get(f"/api/room/{code}").get_json()["v"]
c.post(f"/api/room/{code}/text", json={"text": "bump"})
assert c.get(f"/api/room/{code}").get_json()["v"] > v0

assert g.delete(f"/api/room/{code}/items").status_code == 200     # un invité peut vider
assert items() == []
assert c.delete("/api/room/ZZZZZZ/items").status_code == 404
c.post(f"/api/room/{code}/text", json={"text": "hello"})

# --- fichiers : nom nettoyé, pas de traversée de chemin -------------------
r = c.post(f"/api/room/{code}/file",
           data={"file": (io.BytesIO(b"data"), "../../etc/passwd")})
assert r.status_code == 200 and "/" not in r.get_json()["name"], r.get_json()
fid = r.get_json()["id"]
dl = g.get(f"/api/room/{code}/file/{fid}")
assert dl.data == b"data" and dl.headers["Content-Type"] == "application/octet-stream"
assert g.get(f"/api/room/{code}/file/inconnu").status_code == 404

# les anciens fichiers restent téléchargeables
fid2 = c.post(f"/api/room/{code}/file",
              data={"file": (io.BytesIO(b"second"), "b.txt")}).get_json()["id"]
assert g.get(f"/api/room/{code}/file/{fid}").data == b"data"
assert g.get(f"/api/room/{code}/file/{fid2}").data == b"second"

# une entrée texte n'est pas téléchargeable comme un fichier
tid = c.post(f"/api/room/{code}/text", json={"text": "pas un fichier"}).get_json()["id"]
assert c.get(f"/api/room/{code}/file/{tid}").status_code == 404

# gros fichier : reste en RAM, aucun fichier temporaire sur disque
import tempfile, os, io as _io
before = set(os.listdir(tempfile.gettempdir()))
big = new()
r = c.post(f"/api/room/{big}/file", data={"file": (_io.BytesIO(b"z" * 2_000_000), "gros.bin")})
assert r.status_code == 200, r.status_code
assert c.get(f"/api/room/{big}/file/{r.get_json()['id']}").data == b"z" * 2_000_000
assert set(os.listdir(tempfile.gettempdir())) == before, "fichier temporaire laissé sur disque"

# quota global
S.MAX_TOTAL = 3
assert c.post(f"/api/room/{new()}/file",
              data={"file": (io.BytesIO(b"trop gros"), "x.bin")}).status_code == 507
S.MAX_TOTAL = 512 * 1024 * 1024

# --- rate-limit sur les joins ratés ---------------------------------------
S.fails.clear()
codes = [c.post("/api/join", json={"code": "000000"}).status_code for _ in range(12)]
assert 429 in codes, codes

# --- expiration -----------------------------------------------------------
S.fails.clear()
S.TTL = 1
old = new()
time.sleep(1.1)
assert c.get(f"/api/room/{old}").status_code == 404
S.TTL = 1_000_000
c.post("/api/join", json={})          # déclenche cleanup()
assert old not in S.rooms

# --- en-têtes de sécurité et cookie ---------------------------------------
h = c.get("/").headers
assert h["X-Content-Type-Options"] == "nosniff" and "frame-ancestors 'none'" in h["Content-Security-Policy"]
assert h["Cache-Control"] == "no-store", h["Cache-Control"]
sc = c.post("/api/join", json={}).headers["Set-Cookie"]
assert sc.startswith("share_"), sc
assert "HttpOnly" in sc and "SameSite=Strict" in sc, sc

print("ok")
