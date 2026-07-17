"""Extraction serveur : proforma (PDF Alchemy) et ASN (CSV). Repris des moteurs validés."""
import os, re, csv, subprocess, datetime, io

COUNTRIES = ["United States", "United Kingdom", "Canada", "Ireland", "Germany", "Spain",
             "Netherlands", "Belgium", "Poland", "Sweden", "Denmark", "Norway", "Italy",
             "Portugal", "Switzerland", "Austria", "Finland", "Czech", "Hungary", "France"]

ASN_ORDER = ["item_id", "product_name", "sku_requested", "sku_scanned", "imei", "serial",
             "physical_status", "location", "shipping_carton_id", "category", "manufacturer",
             "model", "capacity", "colour", "network", "variant", "grade", "unit_price"]


def _pdftotext(path):
    return subprocess.run(["pdftotext", "-layout", path, "-"],
                          capture_output=True, text=True).stdout


def norm_amount(s):
    s = str(s).replace("€", "").replace("$", "").replace(" ", "").replace(" ", "").strip()
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{2}$", s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def _norm_date(raw, country):
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw)
    if not m:
        return ""
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a > 12:
        d, mo = a, b
    elif b > 12:
        mo, d = a, b
    else:
        mo, d = (a, b) if (country and "United States" in country) else (b, a)
    try:
        return f"{d:02d}/{mo:02d}/{y}"
    except ValueError:
        return ""


def _grab(pattern, text, flags=0, g=1):
    m = re.search(pattern, text, flags)
    return m.group(g).strip() if m else None


def digits(s):
    return re.sub(r"\D", "", str(s or ""))


def extract_proforma(path, filename=None):
    txt = _pdftotext(path)
    lines = [l.rstrip() for l in txt.splitlines()]
    fname = filename or os.path.basename(path)
    r = {"flags": []}
    head = txt.split("Invoice To")[0] if "Invoice To" in txt else txt

    r["proforma"] = _grab(r"Order No\.?\s+(SO\d+)", txt) or ""
    callisto = _grab(r"Callisto No\.?\s+(\d+)", txt)
    if not callisto:
        callisto = _grab(r"SO[_ ]?(\d{5,7})", fname)
        if callisto:
            r["flags"].append("Callisto repris du nom de fichier")
    r["bon_commande"] = f"#{callisto}" if callisto else ""
    r["devise"] = _grab(r"Currency\s+([A-Z]{3})", txt) or ""
    r["po_no"] = _grab(r"PO No\.?\s+(\S+)", txt) or ""

    LABELS = ("Date", "Order No", "Callisto", "Payment Method", "Terms", "PO No", "Delivery Terms",
              "Currency", "Incoterm", "Tax", "Bank", "VAT Registration", "Invoice To", "Ship To", "Customer VAT")
    seller = ""
    for l in lines:
        left = re.split(r"\s{2,}", l.strip())[0].strip()
        if not left or "Proforma Invoice" in left or left.startswith("Marginal"):
            continue
        if any(left.startswith(lb) for lb in LABELS):
            continue
        if re.search(r"[A-Za-z]", left) and len(left) > 3:
            seller = left
            break
    r["fournisseur"] = seller
    r["pays"] = next((c for c in COUNTRIES[:-1] if c in head), None) or ("France" if "France" in head else "")

    raw_date = _grab(r"Date\s+(\d{1,2}\.\d{1,2}\.\d{4})", txt)
    r["date_commande"] = _norm_date(raw_date, r["pays"]) if raw_date else ""

    marginal = ("Marginal" in txt) or ("Marginal VAT" in txt)
    r["tva_regime"] = "Marge" if marginal else "Autoliquidation"
    r["tva_montant"] = 0.0

    subtotal = _grab(r"Subtotal[:\s]+[€$]?\s*([\d.,]+)", txt)
    total = None
    for m in re.finditer(r"(?<![A-Za-z])Total[:\s]+[€$]?\s*([\d.,]+)", txt, re.IGNORECASE):
        total = m.group(1)
    st = norm_amount(subtotal) if subtotal else None
    tt = norm_amount(total) if total else None
    r["montant_achat"] = st if st is not None else tt
    r["montant_total"] = tt if tt is not None else st
    if r["montant_achat"] is None:
        r["flags"].append("Montant non détecté")
        r["montant_achat"] = 0.0
        r["montant_total"] = 0.0

    qty = _grab(r"Tot Qty:?\s*(\d+)", txt)
    if not qty:
        s = 0
        found = False
        for l in lines:
            mm = re.search(r"\s(\d{1,4})\s+[€$]?\s*([\d.,]+)\s+[€$]?\s*([\d.,]+)\s*$", l)
            if mm:
                q = int(mm.group(1)); u = norm_amount(mm.group(2)); a = norm_amount(mm.group(3))
                if u and a and abs(q * u - a) < max(1.0, a * 0.02):
                    s += q; found = True
        qty = str(s) if found else "0"
    r["nb_machines"] = int(qty)

    desc = ""
    seg = re.split(r"Sales Description", txt)
    if len(seg) > 1:
        block = re.split(r"Tot Qty|Subtotal|TOTAL|\(Marginal", seg[1])[0]
        items = []
        for l in block.splitlines():
            if not l.strip():
                continue
            m = re.match(r"^(?P<d>.*?\S)\s{2,}(?P<q>\d{1,4})\s+[€$]?[\d.,]+", l)
            if m and not m.group("d").startswith(("Quantity", "Unit")):
                items.append([m.group("q"), re.sub(r"\s{2,}", " ", m.group("d")).strip()])
            elif items:
                cont = re.split(r"\s{2,}", l.strip())[0].strip()
                if cont and not re.search(r"Quantity|Unit Price|Tax", cont):
                    items[-1][1] += " " + cont
        parts = []
        for q, d in items:
            d = re.sub(r"\s+", " ", d).strip(" -")
            parts.append(f"{q}x {d}")
        desc = " ; ".join(parts)
    r["description"] = desc
    r["notes"] = (f"PO/offre: {r['po_no']}. Import proforma {fname}."
                  + (" [" + " ; ".join(r["flags"]) + "]" if r["flags"] else ""))
    return r


def detect_ref(name):
    m = re.search(r"SO[_ ]?(\d{5,7})", os.path.basename(name), re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d{5,7})", os.path.basename(name))
    return m.group(1) if m else None


def detect_asn_date(name):
    m = re.search(r"ASN(\d{8})", os.path.basename(name), re.IGNORECASE) or re.search(r"(20\d{6})", os.path.basename(name))
    if m:
        try:
            d = datetime.datetime.strptime(m.group(1), "%Y%m%d")
            return f"{d.day:02d}/{d.month:02d}/{d.year}"
        except ValueError:
            return ""
    return ""


def parse_invoice(path, filename=None):
    """Extrait le n° de facture finale + la référence de commande (Callisto / Order No / Sales Order)."""
    txt = _pdftotext(path)
    fname = filename or os.path.basename(path)
    r = {"flags": []}
    inv = (_grab(r"Invoice\s+N(?:o|umber|°)?\.?\s+([A-Z]{2,}[\w./-]*\d[\w./-]*)", txt, re.IGNORECASE)
           or _grab(r"\b(INVNL\d+)\b", txt)
           or _grab(r"\b(IN\d{5,})\b", txt))
    r["invoice_no"] = inv or ""
    r["callisto"] = _grab(r"Callisto No\.?\s+(\d+)", txt)
    r["proforma"] = _grab(r"Order No\.?\s+(SO\d+)", txt)
    r["sales_order"] = _grab(r"Sales Order\s+(SO?\d+)", txt)
    if not (r["callisto"] or r["proforma"] or r["sales_order"]):
        c2 = _grab(r"SO[_ ]?(\d{5,7})", fname)
        if c2:
            r["callisto"] = c2
            r["flags"].append("réf reprise du nom de fichier")
    return r


def parse_asn(content_bytes, filename):
    text = content_bytes.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    machines = []
    for row in rows:
        lower = {(k or "").strip().lower(): v for k, v in row.items()}
        rec = {c: lower.get(c, "") for c in ASN_ORDER}
        if all(v == "" for v in rec.values()):
            vals = list(row.values())
            for i, c in enumerate(ASN_ORDER):
                rec[c] = vals[i] if i < len(vals) else ""
        if not (rec.get("serial") or rec.get("imei") or rec.get("item_id")):
            continue
        rec["unit_price"] = norm_amount(rec.get("unit_price")) or 0.0
        rec["carton"] = rec.pop("shipping_carton_id", "")
        machines.append(rec)
    return {"ref": detect_ref(filename), "ship_date": detect_asn_date(filename), "machines": machines}
