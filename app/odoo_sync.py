"""Intégration Odoo : crée un bon d'achat (brouillon) depuis une commande.

Utilise l'API externe XML-RPC d'Odoo (stdlib xmlrpc.client, aucune dépendance).
v1 : création du bon d'achat en BROUILLON uniquement.
  - fournisseur rapproché par le nom (celui de la proforma)
  - référence = numéro de commande (bon_commande, sans le # éventuel)
  - une ligne par (SKU + prix d'achat identique), quantité = nb de machines
  - prix d'achat converti dans la devise -> EUR au taux du jour (BCE / frankfurter)
"""
import os
import json
import urllib.request
import xmlrpc.client

ODOO_URL = os.environ.get("ODOO_URL", "").rstrip("/")
ODOO_DB = os.environ.get("ODOO_DB", "")
ODOO_LOGIN = os.environ.get("ODOO_LOGIN", "")
ODOO_API_KEY = os.environ.get("ODOO_API_KEY", "")

_uid = None


def configured():
    return bool(ODOO_URL and ODOO_DB and ODOO_LOGIN and ODOO_API_KEY)


def fx_to_eur(devise):
    """Taux devise -> EUR (source publique BCE via frankfurter.app). None si indisponible."""
    d = (devise or "").upper().strip()
    if d in ("", "EUR", "€"):
        return 1.0
    d = {"$": "USD", "US$": "USD", "£": "GBP"}.get(d, d)
    for url in ("https://api.frankfurter.dev/v1/latest?base=%s&symbols=EUR" % d,
                "https://open.er-api.com/v6/latest/%s" % d):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.loads(r.read().decode())
            eur = (data.get("rates") or {}).get("EUR")
            if eur:
                return float(eur)
        except Exception:
            continue
    return None


def _connect():
    global _uid
    common = xmlrpc.client.ServerProxy("%s/xmlrpc/2/common" % ODOO_URL, allow_none=True)
    uid = common.authenticate(ODOO_DB, ODOO_LOGIN, ODOO_API_KEY, {})
    if not uid:
        raise RuntimeError("Authentification Odoo echouee (login, cle API ou base incorrects).")
    _uid = uid
    return uid


def _exec(model, method, *args, **kw):
    uid = _uid or _connect()
    models = xmlrpc.client.ServerProxy("%s/xmlrpc/2/object" % ODOO_URL, allow_none=True)
    return models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, model, method, list(args), kw)


def _line_uom_field():
    """Le champ UdM de la ligne d'achat a change de nom selon les versions Odoo."""
    try:
        f = _exec("purchase.order.line", "fields_get", [], attributes=["type"])
    except Exception:
        return None
    for cand in ("product_uom_id", "product_uom"):
        if cand in f:
            return cand
    return None


def test_connection():
    """Vérifie la connexion Odoo (pour diagnostic)."""
    if not configured():
        return {"ok": False, "error": "Variables ODOO_* manquantes."}
    try:
        uid = _connect()
        user = _exec("res.users", "read", [uid], fields=["name", "login"])
        return {"ok": True, "uid": uid, "user": user[0] if user else None}
    except Exception as e:
        return {"ok": False, "error": str(e)[:250]}


def _norm(s):
    import re
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_supplier(name):
    """Rapproche le fournisseur par nom, en tolérant les petites différences
    (ex. « Solutions » vs « Solution », ponctuation, accents). Renvoie l'id ou None."""
    import re
    import difflib
    name = (name or "").strip()
    if not name:
        return None
    # 1) exact puis ilike sur le nom complet
    pid = (_exec("res.partner", "search", [["name", "=", name]], limit=1)
           or _exec("res.partner", "search", [["name", "ilike", name]], limit=1))
    if pid:
        return pid[0]
    # 2) tolérant : candidats du 1er mot, comparaison normalisée
    tn = _norm(name)
    token = re.split(r"\s+", name)[0]
    cands = _exec("res.partner", "search_read", [["name", "ilike", token]],
                  fields=["id", "name", "supplier_rank"], limit=50) if token else []
    best, best_r = None, 0.0
    for c in cands:
        cn = _norm(c["name"])
        if cn and (cn == tn or cn in tn or tn in cn):
            return c["id"]
        r = difflib.SequenceMatcher(None, cn, tn).ratio()
        if c.get("supplier_rank", 0) > 0:
            r += 0.02
        if r > best_r:
            best, best_r = c, r
    if best and best_r >= 0.90:
        return best["id"]
    return None


def push_order_to_odoo(order):
    """Crée le bon d'achat brouillon dans Odoo. Renvoie un dict de résultat."""
    if not configured():
        return {"ok": False, "error": "Connexion Odoo non configuree (variables ODOO_* manquantes dans Coolify)."}
    machines = list(order.machines)
    if not machines:
        return {"ok": False, "error": "Aucune machine sur cette commande."}
    no_sku = sum(1 for m in machines if not (m.sku_scanned or "").strip())
    if no_sku:
        return {"ok": False, "error": "%d machine(s) sans SKU scanne. Genere d'abord les SKU manquants." % no_sku}
    try:
        _connect()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    try:
        # --- Fournisseur (par nom, tolérant) ---
        import re as _re
        name = (order.fournisseur or "").strip()
        partner_id = find_supplier(name)
        if not partner_id:
            token = (_re.split(r"\s+", name)[0] if name else "")
            cands = _exec("res.partner", "search_read", [["name", "ilike", token]],
                          fields=["name"], limit=6) if token else []
            hint = (" Noms proches dans Odoo : " + ", ".join("« %s »" % c["name"] for c in cands)) if cands else ""
            return {"ok": False, "error": "Fournisseur introuvable dans Odoo : « %s ».%s" % (name, hint)}

        # --- Idempotence : reutiliser un achat deja cree pour cette commande ---
        ref = (order.bon_commande or "").lstrip("#")
        if ref:
            exist = _exec("purchase.order", "search_read",
                          [["partner_ref", "=", ref], ["partner_id", "=", partner_id],
                           ["state", "!=", "cancel"]],
                          fields=["id", "name"], limit=1)
            if exist:
                return {"ok": True, "po_id": exist[0]["id"], "po_name": exist[0]["name"],
                        "existing": True, "nb_lignes": 0, "nb_machines": len(machines)}

        # --- Produits (par SKU = reference interne) ---
        skus = sorted({(m.sku_scanned or "").strip() for m in machines})
        try:
            prods = _exec("product.product", "search_read", [["default_code", "in", skus]],
                          fields=["id", "default_code", "display_name", "uom_id"])
        except xmlrpc.client.Fault:
            prods = _exec("product.product", "search_read", [["default_code", "in", skus]],
                          fields=["id", "default_code", "display_name"])
        by_sku = {p["default_code"]: p for p in prods}
        missing = [s for s in skus if s not in by_sku]
        if missing:
            return {"ok": False, "error": "SKU absents d'Odoo : " + ", ".join(missing), "missing": missing}

        # --- Taux de change -> EUR ---
        rate = fx_to_eur(order.devise)
        if rate is None:
            return {"ok": False, "error": "Taux de change %s->EUR indisponible pour le moment." % (order.devise or "?")}

        # --- Regroupement par (SKU, prix d'achat EUR) ---
        groups = {}
        for m in machines:
            sku = (m.sku_scanned or "").strip()
            price = round(float(m.unit_price or 0.0) * rate, 2)
            groups[(sku, price)] = groups.get((sku, price), 0) + 1

        uom_field = _line_uom_field()
        lines = []
        for (sku, price), qty in sorted(groups.items()):
            p = by_sku[sku]
            vals = {"product_id": p["id"], "product_qty": qty, "price_unit": price,
                    "name": p.get("display_name") or sku}
            if uom_field:
                uom = p.get("uom_id")
                if uom:
                    vals[uom_field] = uom[0] if isinstance(uom, (list, tuple)) else uom
            lines.append((0, 0, vals))

        po_vals = {
            "partner_id": partner_id,
            "partner_ref": (order.bon_commande or "").lstrip("#"),
            "order_line": lines,
        }
        eur = _exec("res.currency", "search", [["name", "=", "EUR"]], limit=1)
        if eur:
            po_vals["currency_id"] = eur[0]

        created = _exec("purchase.order", "create", [po_vals])
        po_id = created[0] if isinstance(created, (list, tuple)) else created
        po = _exec("purchase.order", "read", [po_id], fields=["name"])
        po_name = po[0]["name"] if po else str(po_id)
        return {"ok": True, "po_id": po_id, "po_name": po_name, "rate": rate,
                "nb_lignes": len(lines), "nb_machines": len(machines)}
    except xmlrpc.client.Fault as e:
        msg = (e.faultString or str(e)).strip()
        return {"ok": False, "error": "Odoo: " + (msg.splitlines()[-1] if msg else "erreur")[:250]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:250]}
