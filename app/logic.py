"""Calculs de statut / prochaine action (mêmes règles que le classeur Excel)."""

def machine_stats(order):
    ships = order.machines
    nb_exp = len(ships)
    nb_recu = sum(1 for m in ships if m.recu)
    nb_prob = sum(1 for m in ships if (m.probleme or "").upper() not in ("", "RAS"))
    return nb_exp, nb_recu, nb_prob


def statut_controle(order):
    nb_exp, nb_recu, nb_prob = machine_stats(order)
    if nb_exp == 0:
        return "En attente ASN"
    if order.reception == "Non recu":
        return "En attente reception"
    if nb_recu < nb_exp:
        return "Manquant a reception"
    if nb_prob > 0:
        return "Defaut(s) signale(s)"
    return "Conforme"


def prochaine_action(order):
    nb_exp, nb_recu, nb_prob = machine_stats(order)
    if order.paiement != "Paye":
        return (1, "1. À payer")
    if order.etiquette_ups != "Creee":
        return (2, "2. Créer étiquette UPS")
    if order.expedition != "Expedie":
        return (3, "3. Attente expédition")
    if order.reception != "Complet":
        return (4, "4. Réceptionner / contrôler")
    if nb_recu < nb_exp or nb_prob > 0:
        return (5, "5. Traiter les écarts")
    return (6, "✅ Terminé")


ACTION_META = {
    1: ("red", "Commande ferme non réglée"),
    2: ("amber", "Payée, étiquette UPS à générer"),
    3: ("blue", "UPS OK, attente départ fournisseur"),
    4: ("blue", "Expédiée, à réceptionner / vérifier"),
    5: ("amber", "Machine manquante ou défaut signalé"),
    6: ("green", "Reçue, complète et conforme"),
}


def order_view(order):
    """Dictionnaire enrichi pour l'affichage / l'API."""
    nb_exp, nb_recu, nb_prob = machine_stats(order)
    k, label = prochaine_action(order)
    return {
        "id": order.id,
        "bon_commande": order.bon_commande,
        "proforma": order.proforma,
        "facture": order.facture,
        "date_commande": order.date_commande,
        "fournisseur": order.fournisseur,
        "pays": order.pays,
        "description": order.description,
        "nb_machines": order.nb_machines,
        "montant_achat": order.montant_achat,
        "devise": order.devise,
        "tva_regime": order.tva_regime,
        "tva_montant": order.tva_montant,
        "montant_total": order.montant_total,
        "paiement": order.paiement,
        "date_paiement": order.date_paiement,
        "etiquette_ups": order.etiquette_ups,
        "tracking_ups": order.tracking_ups,
        "expedition": order.expedition,
        "date_expedition": order.date_expedition,
        "reception": order.reception,
        "date_reception": order.date_reception,
        "notes": order.notes,
        "nb_expedie": nb_exp,
        "nb_recu": nb_recu,
        "nb_probleme": nb_prob,
        "statut_controle": statut_controle(order),
        "action_k": k,
        "action": label,
        "action_cls": ACTION_META[k][0],
    }


def kpis(orders):
    ov = [order_view(o) for o in orders]
    def c(f): return sum(1 for o in ov if f(o))
    dues = {}
    for o in ov:
        if o["paiement"] == "A payer":
            dues[o["devise"] or "?"] = dues.get(o["devise"] or "?", 0) + (o["montant_total"] or 0)
    return {
        "total": len(ov),
        "a_payer": c(lambda o: o["paiement"] == "A payer"),
        "payees": c(lambda o: o["paiement"] == "Paye"),
        "ups_a_creer": c(lambda o: o["paiement"] == "Paye" and o["etiquette_ups"] == "A creer"),
        "attente_recep": c(lambda o: o["expedition"] == "Expedie" and o["reception"] != "Complet"),
        "ecarts": c(lambda o: o["statut_controle"] in ("Manquant a reception", "Defaut(s) signale(s)")),
        "terminees": c(lambda o: o["action_k"] == 6),
        "dues": dues,
    }
