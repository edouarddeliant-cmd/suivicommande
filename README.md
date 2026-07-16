# Suivi des commandes fournisseur

Application web auto-hébergée pour suivre les commandes (paiement → étiquette UPS →
expédition → réception/contrôle), avec import automatique des **proformas (PDF)** et
des **ASN (CSV)**. Base PostgreSQL, interface web, et API sécurisée.

## Fonctionnalités
- Tableau de bord (KPIs, file d'actions prioritaires).
- Liste des commandes filtrable, fiche détail avec mise à jour des statuts.
- Contrôle de réception machine par machine (reçu / problème), écarts signalés.
- **Import proforma** : dépose le PDF → la commande est créée (numéros, montant, TVA marge/autoliquidation…).
- **Import ASN** : dépose le CSV → machines ajoutées et rattachées à la bonne commande (via le n° dans le nom de fichier), commande passée en « Expédié ».
- **API** (jeton Bearer) : `POST /api/proforma`, `POST /api/asn`, `GET /api/orders`, `PATCH /api/orders/{bon}`.

## Déploiement sur Coolify (recommandé)

1. **Pousse ce dossier sur un repo GitHub** (privé de préférence).
2. Dans Coolify : **+ New → Resource → Docker Compose** (ou « Application » basée sur ce repo).
   Sélectionne ton repo GitHub. Coolify détecte le `docker-compose.yml` (app + PostgreSQL).
3. Dans **Environment Variables**, définis :
   - `APP_PASSWORD` : mot de passe d'accès à l'interface (utilisateur = `admin`, modifiable via `APP_USER`).
   - `API_TOKEN` : jeton long et aléatoire (pour l'API).
   - `DB_PASSWORD` : mot de passe PostgreSQL.
4. **Deploy**. L'app écoute sur le port **8000**.
5. **Domaine** : dans Coolify, associe ton (sous-)domaine OVH au service `app` (ex. `suivi.tondomaine.fr`).
   Coolify gère le HTTPS (Let's Encrypt) automatiquement. Pense à créer l'enregistrement DNS
   `A` (ou `CNAME`) chez OVH pointant vers l'IP de ton serveur Coolify.

> Variante : si tu utilises une base PostgreSQL gérée par Coolify (hors compose), crée une
> ressource PostgreSQL, puis déploie seulement l'app (Dockerfile) en renseignant `DATABASE_URL`.

## Générer des secrets
```bash
openssl rand -hex 24   # pour API_TOKEN et DB_PASSWORD
```

## Lancer en local (test)
```bash
pip install -r requirements.txt
export APP_PASSWORD=test API_TOKEN=test-token   # sinon interface ouverte
uvicorn app.main:app --reload
# http://localhost:8000   (SQLite par défaut : ./suivi.db)
```

## Utiliser l'API (exemples)
```bash
# Importer une proforma
curl -H "Authorization: Bearer $API_TOKEN" -F "files=@proforma.pdf" \
     https://suivi.tondomaine.fr/api/proforma

# Importer un ASN
curl -H "Authorization: Bearer $API_TOKEN" -F "file=@SO112836ASN....csv" \
     https://suivi.tondomaine.fr/api/asn

# Lister les commandes
curl -H "Authorization: Bearer $API_TOKEN" https://suivi.tondomaine.fr/api/orders
```
C'est cette API que Claude utilise pour ajouter tes commandes quand tu lui déposes un document.

## Règles métier
- **TVA** : mention « Marginal » sur la proforma → *Marge* ; sinon → *Autoliquidation*.
- **Rattachement ASN** : numéro Callisto (bon de commande) détecté dans le nom du fichier.
- **Prochaine action** : À payer → Créer étiquette UPS → Attente expédition → Réceptionner/contrôler → Traiter écarts → Terminé.
