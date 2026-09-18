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

## Variables d’environnement

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


## Sécurité

- taille des uploads limitée
- texte limité
- historique borné
- rooms bornées
- anti brute-force sur les codes
- cookies liés à l’IP et au navigateur
- entêtes de sécurité
- authentification obligatoire sur les routes protégées


