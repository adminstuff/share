# Share

Application web Flask pour partager rapidement du texte et des fichiers entre plusieurs postes via un **code à 6 caractères**.

Pensée pour :

- une utilisation simple en local ou sur un VPS,
- un **master** qui valide les nouveaux arrivants,
- du **copier/coller rapide** avec historique,
- un déploiement en **conteneur**.

## Fonctionnalités

- Création d’une room avec code court
- Rejoindre une room via un code
- Validation des nouveaux venus par le master
- Partage de texte
- Partage de fichiers
- Historique cliquable avec copie dans le presse-papier
- Liste des postes connectés
- Déconnexion forcée d’un poste
- Protection contre :
  - uploads trop volumineux
  - brute force sur les codes
  - historique et rooms illimités
  - accès non authentifiés
- Fonctionne en conteneur Docker/Podman

## Prérequis

- Python 3.12+ recommandé
- `pip`
- Optionnel :
  - Docker
  - Podman
  - Podman Compose / Docker Compose

---

## Lancement en local

Créer un environnement virtuel :

```bash
python -m venv .venv
source .venv/bin/activate
```

Installer les dépendances :

```bash
pip install -r requirements.txt
```

Lancer l’application :

```bash
python share.py
```

Puis ouvrir :

```text
http://127.0.0.1:5000
```

---

## Lancement avec Flask/Werkzeug

Si ton fichier `share.py` expose bien l’application Flask, tu peux aussi lancer :

```bash
flask --app share run --debug
```

Mais en production, il vaut mieux passer par un serveur WSGI.

---

## Lancement en conteneur

### Avec Podman

Build :

```bash
podman build -t share .
```

Run :

```bash
podman run -d \
  --name share \
  -p 127.0.0.1:5000:5000 \
  share
```

### Avec Podman Compose

```bash
podman-compose up -d --build --force-recreate
```

### Avec Docker Compose

```bash
docker compose up -d --build --force-recreate
```

---

## Image de conteneur

L’image est prévue pour :

- tourner **en non-root**
- avoir le système de fichiers en lecture seule
- limiter les capacités Linux
- ne pas embarquer le venv de développement
- fonctionner sans dépendre d’un environnement local

---

## Déploiement sur Hostinger

### Mutualisé / cloud basique
Non adapté.

Cette application a besoin de :

- un processus long vivant en mémoire,
- un port HTTP,
- idéalement un reverse proxy TLS devant.

### VPS
C’est le bon choix.

Sur un VPS Hostinger :

- installe Podman ou Docker
- expose le service sur `127.0.0.1:5000`
- mets **Caddy**, **Nginx** ou **Traefik** devant
- active HTTPS
- garde `SHARE_TRUST_PROXY=1` si tu passes par un reverse proxy

---

## Variables d’environnement

Selon ta configuration, tu peux utiliser :

- `SHARE_TRUST_PROXY=1`  
  Active la prise en compte des headers proxy pour l’IP réelle du client.

- `SHARE_TTL`  
  Durée de vie d’une room en secondes.

- `SHARE_MAX_HIST`  
  Nombre maximal d’entrées d’historique par room.

- `SHARE_MAX_HIST_CHARS`  
  Limite globale de caractères pour l’historique.

- `SHARE_MAX_PENDING`  
  Nombre max de demandes en attente.

- `SHARE_MAX_MEMBERS`  
  Nombre max de membres dans une room.

- `SHARE_PENDING_TTL`  
  Durée de validité d’une demande d’accès.

Exemple :

```bash
export SHARE_TRUST_PROXY=1
export SHARE_TTL=3600
```

---

## Sécurité

### Ce que l’application protège déjà

- taille des uploads limitée
- texte limité
- historique borné
- rooms bornées
- anti brute-force sur les codes
- cookies liés à l’IP et au navigateur
- entêtes de sécurité
- authentification obligatoire sur les routes protégées

### Ce qu’il faut ajouter en production

- **HTTPS obligatoire**
- reverse proxy devant le service
- pas d’exposition directe sur Internet sans TLS

---

## Structure du projet

Exemple :

```text
.
├── share.py
├── requirements.txt
├── Containerfile
├── compose.yml
├── .dockerignore
├── .gitignore
├── test_share.py
└── README.md
```

---

## Tests

Lancer les tests :

```bash
python test_share.py
```

Ou, si ton projet utilise le venv local :

```bash
share/bin/python test_share.py
```

---

## Nettoyage

Arrêter le conteneur :

```bash
podman stop share
podman rm share
```

Avec compose :

```bash
podman-compose down
```

---

## Remarques importantes

- L’état de l’application est **en RAM**
- Une seule instance est recommandée
- Le scale horizontal n’est pas prévu sans stockage partagé
- Si tu veux plusieurs instances plus tard, il faudra un backend partagé comme Redis

---

## Dépannage

### Le navigateur garde une ancienne version
Fais un **Ctrl+Shift+R**.

### `localhost` ne marche pas en POST avec Podman rootless
Utilise :

```text
127.0.0.1
```

au lieu de `localhost`.

### Build en conteneur lent ou bloqué
Relance :

```bash
podman-compose up -d --build --force-recreate
```
