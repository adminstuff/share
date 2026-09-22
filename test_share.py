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
for path, meth in [(f"/api/room/{code}", "get"), (f"/api/room/{code}/hist", "get"),
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

# --- corps non-JSON accepté sans crash ------------------------------------
assert c.post(f"/api/room/{code}/text", data="pas du json",
              content_type="application/json").status_code in (200, 400)

# --- texte ----------------------------------------------------------------
assert c.post(f"/api/room/{code}/text", json={"text": "hello"}).status_code == 200
assert g.get(f"/api/room/{code}").get_json()["text"] == "hello"     # l'invité accepté lit bien
assert c.post(f"/api/room/{code}/text", json={"text": "x" * (S.MAX_TEXT + 1)}).status_code == 400
assert c.post(f"/api/room/{code}/text", json={"text": 42}).status_code == 400

# --- historique -----------------------------------------------------------
h = c.get(f"/api/room/{code}/hist").get_json()["hist"]
assert [x["text"] for x in h] == ["hello"], h
c.post(f"/api/room/{code}/text", json={"text": "hello"})          # doublon ignoré
c.post(f"/api/room/{code}/text", json={"text": "monde"})
c.post(f"/api/room/{code}/text", json={"text": ""})               # vide ignoré
assert [x["text"] for x in c.get(f"/api/room/{code}/hist").get_json()["hist"]] == ["monde", "hello"]

S.MAX_HIST = 3
for i in range(6):
    c.post(f"/api/room/{code}/text", json={"text": f"n{i}"})
assert [x["text"] for x in c.get(f"/api/room/{code}/hist").get_json()["hist"]] == ["n5", "n4", "n3"]

S.MAX_HIST_CHARS = 10
c.post(f"/api/room/{code}/text", json={"text": "x" * 9})
assert len(c.get(f"/api/room/{code}/hist").get_json()["hist"]) == 1
S.MAX_HIST, S.MAX_HIST_CHARS = 20, 200_000

assert c.delete(f"/api/room/{code}/hist").status_code == 200
assert c.get(f"/api/room/{code}/hist").get_json()["hist"] == []
assert c.delete("/api/room/ZZZZZZ/hist").status_code == 404
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
assert g.get(f"/api/room/{code}/file/{fid}").data == b"data"      # le premier survit
assert g.get(f"/api/room/{code}/file/{fid2}").data == b"second"
lst = c.get(f"/api/room/{code}").get_json()["files"]
assert [f["name"] for f in lst] == ["b.txt", "etc_passwd"], lst       # plus récent en tête
assert lst[0]["size"] == 6 and lst[1]["size"] == 4

# suppression d'un fichier, puis de tous
tmp = c.post(f"/api/room/{code}/file",
             data={"file": (io.BytesIO(b"jetable"), "t.txt")}).get_json()["id"]
assert guest(REMOTE_ADDR="10.7.7.7").delete(f"/api/room/{code}/file/{tmp}").status_code == 403
assert c.delete(f"/api/room/{code}/file/{tmp}").status_code == 200
assert c.delete(f"/api/room/{code}/file/{tmp}").status_code == 404      # idempotent -> 404
assert g.get(f"/api/room/{code}/file/{tmp}").status_code == 404
assert [f["name"] for f in c.get(f"/api/room/{code}").get_json()["files"]] == ["b.txt", "etc_passwd"]
assert g.delete(f"/api/room/{code}/files").status_code == 200           # un invité peut vider
assert c.get(f"/api/room/{code}").get_json()["files"] == []
assert guest(REMOTE_ADDR="10.7.7.8").delete(f"/api/room/{code}/files").status_code == 403
fid = c.post(f"/api/room/{code}/file",
             data={"file": (io.BytesIO(b"data"), "a.txt")}).get_json()["id"]
c.post(f"/api/room/{code}/file", data={"file": (io.BytesIO(b"second"), "b.txt")})

# plafond par room : le plus ancien saute
S.MAX_FILES = 2
old = c.post(f"/api/room/{code}/file",
             data={"file": (io.BytesIO(b"troisieme"), "c.txt")}).get_json()["id"]
assert [f["name"] for f in c.get(f"/api/room/{code}").get_json()["files"]] == ["c.txt", "b.txt"]
assert g.get(f"/api/room/{code}/file/{fid}").status_code == 404   # évincé
assert g.get(f"/api/room/{code}/file/{old}").status_code == 200
S.MAX_FILES = 10

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
