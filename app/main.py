import os, base64, secrets, tempfile
from fastapi import FastAPI, Request, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, Order, Machine, init_db
from . import logic, extract, odoo_sync

BASE = os.path.dirname(__file__)
app = FastAPI(title="Suivi Commandes Fournisseur")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))

API_TOKEN = os.environ.get("API_TOKEN", "")
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.on_event("startup")
def _startup():
    init_db()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _check(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if API_TOKEN and secrets.compare_digest(auth, f"Bearer {API_TOKEN}"):
        return True
    if auth.startswith("Basic ") and APP_PASSWORD:
        try:
            u, p = base64.b64decode(auth[6:]).decode().split(":", 1)
        except Exception:
            u, p = "", ""
        if secrets.compare_digest(u, APP_USER) and secrets.compare_digest(p, APP_PASSWORD):
            return True
    return False


def require_ui(request: Request):
    if not APP_PASSWORD:
        return
    if not _check(request):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def require_api(request: Request):
    if not _check(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ---------------- ingestion partagée ----------------
def _digits(s):
    return extract.digits(s)


def ingest_proforma_file(db: Session, tmp_path: str, filename: str):
    rec = extract.extract_proforma(tmp_path, filename)
    bon = rec.get("bon_commande", "")
    if not bon:
        return {"file": filename, "status": "ignore", "reason": "bon de commande introuvable"}
    existing = db.scalar(select(Order).where(Order.bon_commande == bon))
    if existing:
        return {"file": filename, "status": "doublon", "bon_commande": bon}
    o = Order(
        bon_commande=bon, proforma=rec["proforma"], date_commande=rec["date_commande"],
        fournisseur=rec["fournisseur"], pays=rec["pays"], description=rec["description"],
        nb_machines=rec["nb_machines"], montant_achat=rec["montant_achat"], devise=rec["devise"],
        tva_regime=rec["tva_regime"], tva_montant=rec["tva_montant"], montant_total=rec["montant_total"],
        notes=rec["notes"],
    )
    db.add(o); db.commit()
    return {"file": filename, "status": "cree", "bon_commande": bon}


def ingest_asn_bytes(db: Session, content: bytes, filename: str):
    res = extract.parse_asn(content, filename)
    ref = res["ref"]; ship_date = res["ship_date"]
    order = None
    if ref:
        d = _digits(ref)
        for o in db.scalars(select(Order)).all():
            if _digits(o.bon_commande) == d:
                order = o; break
        if not order:
            for o in db.scalars(select(Order)).all():
                if _digits(o.proforma) == d:
                    order = o; break
    added = skipped = 0
    # TVA sur marge : les n° de série doivent se terminer par /TVM (ajout automatique).
    marge = bool(order and (order.tva_regime or "") == "Marge")
    seen = set()
    if order:
        for m in order.machines:
            seen.add(m.serial or m.imei)
    for md in res["machines"]:
        serial = md.get("serial", "")
        if marge and serial and not serial.endswith("/TVM"):
            serial += "/TVM"
        key = serial or md.get("imei")
        if key and key in seen:
            skipped += 1; continue
        machine = Machine(
            order_id=order.id if order else None,
            bon_commande=order.bon_commande if order else (ref or ""),
            item_id=md.get("item_id", ""), product_name=md.get("product_name", ""),
            sku_requested=md.get("sku_requested", ""), sku_scanned=md.get("sku_scanned", ""),
            imei=md.get("imei", ""), serial=serial,
            physical_status=md.get("physical_status", ""), location=md.get("location", ""),
            carton=md.get("carton", ""), category=md.get("category", ""),
            manufacturer=md.get("manufacturer", ""), model=md.get("model", ""),
            capacity=md.get("capacity", ""), colour=md.get("colour", ""),
            network=md.get("network", ""), variant=md.get("variant", ""),
            grade=md.get("grade", ""), unit_price=md.get("unit_price", 0.0),
            recu=False, probleme="RAS", commentaire=f"Import ASN {ship_date}".strip(),
        )
        if order:
            order.machines.append(machine)
        else:
            db.add(machine)
        if key:
            seen.add(key)
        added += 1
    if order:
        order.expedition = "Expedie"
        if ship_date:
            order.date_expedition = ship_date
    db.commit()
    return {"file": filename, "ref": ref, "bon_commande": order.bon_commande if order else None,
            "order_found": order is not None, "added": added, "skipped": skipped,
            "ship_date": ship_date}


def ingest_facture_file(db: Session, tmp_path: str, filename: str):
    rec = extract.parse_invoice(tmp_path, filename)
    inv = rec.get("invoice_no", "")
    orders = db.scalars(select(Order)).all()
    order = None
    # 1) Sales Order exact (fournisseurs type PCS dont le bon = SOxxxxxx)
    so = rec.get("sales_order")
    if so:
        order = next((o for o in orders if o.bon_commande == so or _digits(o.bon_commande) == _digits(so)), None)
    # 2) Callisto -> bon de commande (#chiffres)
    if not order and rec.get("callisto"):
        d = _digits(rec["callisto"])
        order = next((o for o in orders if _digits(o.bon_commande) == d), None)
    # 3) Order No -> proforma
    if not order and rec.get("proforma"):
        d = _digits(rec["proforma"])
        order = next((o for o in orders if _digits(o.proforma) == d), None)
    if not order:
        return {"file": filename, "status": "commande introuvable", "invoice_no": inv}
    if not inv:
        return {"file": filename, "status": "n° de facture introuvable", "bon_commande": order.bon_commande}
    order.facture = inv
    db.commit()
    return {"file": filename, "status": "facture renseignee", "bon_commande": order.bon_commande, "invoice_no": inv}


# ---------------- UI ----------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), _=Depends(require_ui)):
    orders = db.scalars(select(Order)).all()
    views = sorted([logic.order_view(o) for o in orders], key=lambda v: v["action_k"])
    k = logic.kpis(orders)
    groups = {}
    for v in views:
        groups.setdefault(v["action_k"], 0)
        groups[v["action_k"]] += 1
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "kpis": k, "views": views, "groups": groups,
        "action_meta": logic.ACTION_META,
        "dues": " · ".join(f"{v:,.0f} {c}" for c, v in k["dues"].items()) or "0",
    })


@app.get("/orders", response_class=HTMLResponse)
def orders_list(request: Request, db: Session = Depends(get_db), _=Depends(require_ui),
                q: str = "", paiement: str = "", reception: str = ""):
    orders = db.scalars(select(Order)).all()
    views = [logic.order_view(o) for o in orders]
    if q:
        ql = q.lower()
        views = [v for v in views if ql in " ".join(str(v[x]) for x in
                 ("bon_commande", "proforma", "facture", "fournisseur", "pays", "description")).lower()]
    if paiement:
        views = [v for v in views if v["paiement"] == paiement]
    if reception:
        views = [v for v in views if v["reception"] == reception]
    views.sort(key=lambda v: v["action_k"])
    return templates.TemplateResponse("orders.html",
        {"request": request, "views": views, "q": q, "paiement": paiement, "reception": reception})


@app.get("/orders/{oid}", response_class=HTMLResponse)
def order_detail(oid: int, request: Request, db: Session = Depends(get_db), _=Depends(require_ui)):
    o = db.get(Order, oid)
    if not o:
        raise HTTPException(404)
    return templates.TemplateResponse("order_detail.html",
        {"request": request, "o": o, "v": logic.order_view(o),
         "odoo_base": os.environ.get("ODOO_URL", "").rstrip("/")})


@app.post("/orders/{oid}/update")
def order_update(oid: int, request: Request, db: Session = Depends(get_db), _=Depends(require_ui),
                 paiement: str = Form(""), date_paiement: str = Form(""),
                 etiquette_ups: str = Form(""), tracking_ups: str = Form(""),
                 expedition: str = Form(""), date_expedition: str = Form(""),
                 reception: str = Form(""), date_reception: str = Form(""),
                 facture: str = Form(""), notes: str = Form("")):
    o = db.get(Order, oid)
    if not o:
        raise HTTPException(404)
    o.paiement = paiement or o.paiement
    o.date_paiement = date_paiement
    o.etiquette_ups = etiquette_ups or o.etiquette_ups
    o.tracking_ups = tracking_ups
    o.expedition = expedition or o.expedition
    o.date_expedition = date_expedition
    o.reception = reception or o.reception
    o.date_reception = date_reception
    o.facture = facture
    o.notes = notes
    db.commit()
    return RedirectResponse(f"/orders/{oid}", status_code=303)


@app.post("/orders/{oid}/stock")
def order_stock(oid: int, db: Session = Depends(get_db), _=Depends(require_ui),
                etat: str = Form("")):
    import datetime
    o = db.get(Order, oid)
    if not o:
        raise HTTPException(404)
    o.stock_odoo = etat in ("on", "true", "1", "Oui")
    o.date_stock = datetime.date.today().strftime("%d/%m/%Y") if o.stock_odoo else ""
    db.commit()
    return RedirectResponse(f"/orders/{oid}", status_code=303)


@app.post("/orders/{oid}/odoo")
def order_odoo(oid: int, db: Session = Depends(get_db), _=Depends(require_ui)):
    import urllib.parse
    o = db.get(Order, oid)
    if not o:
        raise HTTPException(404)
    if getattr(o, "odoo_po_id", ""):
        return RedirectResponse(f"/orders/{oid}?odoo=exists", status_code=303)
    res = odoo_sync.push_order_to_odoo(o)
    if res.get("ok"):
        o.odoo_po_id = str(res["po_id"])
        o.odoo_po_name = res.get("po_name", "")
        db.commit()
        return RedirectResponse(f"/orders/{oid}?odoo=ok", status_code=303)
    return RedirectResponse(
        f"/orders/{oid}?odoo_err=" + urllib.parse.quote(res.get("error", "Erreur inconnue")[:300]),
        status_code=303)


@app.post("/orders/{oid}/delete")
def order_delete(oid: int, db: Session = Depends(get_db), _=Depends(require_ui)):
    o = db.get(Order, oid)
    if o:
        db.delete(o); db.commit()
    return RedirectResponse("/orders", status_code=303)


@app.post("/machines/{mid}")
def machine_update(mid: int, request: Request, db: Session = Depends(get_db), _=Depends(require_ui),
                   product_name: str = Form(""), manufacturer: str = Form(""), model: str = Form(""),
                   variant: str = Form(""), capacity: str = Form(""), colour: str = Form(""),
                   network: str = Form(""), item_id: str = Form(""), sku_requested: str = Form(""),
                   sku_scanned: str = Form(""), serial: str = Form(""), imei: str = Form(""),
                   grade: str = Form(""), unit_price: str = Form(""),
                   physical_status: str = Form(""), location: str = Form(""),
                   recu: str = Form(""), probleme: str = Form(""), commentaire: str = Form("")):
    m = db.get(Machine, mid)
    if not m:
        raise HTTPException(404)
    m.product_name = product_name
    m.manufacturer = manufacturer
    m.model = model
    m.variant = variant
    m.capacity = capacity
    m.colour = colour
    m.network = network
    m.item_id = item_id
    m.sku_requested = sku_requested
    m.sku_scanned = sku_scanned
    m.serial = serial
    m.imei = imei
    m.grade = grade
    try:
        m.unit_price = float(str(unit_price).replace(",", ".").strip() or 0)
    except ValueError:
        pass
    m.physical_status = physical_status
    m.location = location
    m.recu = (recu in ("on", "true", "Oui", "1"))
    m.probleme = (probleme or "").strip() or "RAS"
    m.commentaire = commentaire
    db.commit()
    return RedirectResponse(f"/orders/{m.order_id}", status_code=303)


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, _=Depends(require_ui), msg: str = ""):
    return templates.TemplateResponse("import.html", {"request": request, "msg": msg})


@app.post("/import/proforma")
async def import_proforma(request: Request, db: Session = Depends(get_db), _=Depends(require_ui),
                          files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        data = await f.read()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data); path = tmp.name
        try:
            results.append(ingest_proforma_file(db, path, f.filename))
        finally:
            os.unlink(path)
    msg = " | ".join(f"{r['file']}: {r['status']}" for r in results)
    return RedirectResponse(f"/import?msg={msg}", status_code=303)


@app.post("/import/asn")
async def import_asn(request: Request, db: Session = Depends(get_db), _=Depends(require_ui),
                     file: UploadFile = File(...)):
    data = await file.read()
    r = ingest_asn_bytes(db, data, file.filename)
    msg = (f"{r['file']}: {r['added']} machine(s) ajoutée(s), rattachée(s) à "
           f"{r['bon_commande'] or 'AUCUNE commande'} ; {r['skipped']} doublon(s)")
    return RedirectResponse(f"/import?msg={msg}", status_code=303)


@app.post("/import/facture")
async def import_facture(request: Request, db: Session = Depends(get_db), _=Depends(require_ui),
                         files: list[UploadFile] = File(...)):
    results = []
    for f in files:
        data = await f.read()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data); path = tmp.name
        try:
            results.append(ingest_facture_file(db, path, f.filename))
        finally:
            os.unlink(path)
    msg = " | ".join(f"{r['file']}: {r['status']}"
                     + (f" ({r.get('invoice_no')} → {r.get('bon_commande')})" if r['status'] == 'facture renseignee' else "")
                     for r in results)
    return RedirectResponse(f"/import?msg={msg}", status_code=303)


# ---------------- API JSON ----------------
@app.get("/api/orders")
def api_orders(db: Session = Depends(get_db), _=Depends(require_api)):
    return [logic.order_view(o) for o in db.scalars(select(Order)).all()]


@app.post("/api/proforma")
async def api_proforma(db: Session = Depends(get_db), _=Depends(require_api),
                       files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        data = await f.read()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data); path = tmp.name
        try:
            out.append(ingest_proforma_file(db, path, f.filename))
        finally:
            os.unlink(path)
    return {"results": out}


@app.post("/api/asn")
async def api_asn(db: Session = Depends(get_db), _=Depends(require_api),
                  file: UploadFile = File(...)):
    data = await file.read()
    return ingest_asn_bytes(db, data, file.filename)


@app.post("/api/facture")
async def api_facture(db: Session = Depends(get_db), _=Depends(require_api),
                      files: list[UploadFile] = File(...)):
    out = []
    for f in files:
        data = await f.read()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(data); path = tmp.name
        try:
            out.append(ingest_facture_file(db, path, f.filename))
        finally:
            os.unlink(path)
    return {"results": out}


@app.patch("/api/orders/{bon}")
async def api_patch(bon: str, request: Request, db: Session = Depends(get_db), _=Depends(require_api)):
    o = db.scalar(select(Order).where(Order.bon_commande == bon))
    if not o:
        raise HTTPException(404, "commande introuvable")
    payload = await request.json()
    for k, v in payload.items():
        if hasattr(o, k):
            setattr(o, k, v)
    db.commit()
    return logic.order_view(o)


@app.post("/api/import")
async def api_import(request: Request, db: Session = Depends(get_db), _=Depends(require_api)):
    """Import en masse depuis le Google Sheet.

    Corps JSON : {"reset": bool, "orders": [ {...} ], "machines": [ {...} ]}.
    Les commandes sont upsert par bon_commande ; les machines sont rattachées
    à leur commande via bon_commande. Seuls les champs stockés sont pris en compte.
    """
    payload = await request.json()
    order_fields = {c.name for c in Order.__table__.columns} - {"id", "created_at"}
    machine_fields = {c.name for c in Machine.__table__.columns} - {"id", "order_id"}

    if payload.get("reset"):
        for m in db.scalars(select(Machine)).all():
            db.delete(m)
        for o in db.scalars(select(Order)).all():
            db.delete(o)
        db.commit()

    n_orders = 0
    for od in payload.get("orders", []):
        bon = (od.get("bon_commande") or "").strip()
        if not bon:
            continue
        o = db.scalar(select(Order).where(Order.bon_commande == bon))
        if not o:
            o = Order(bon_commande=bon)
            db.add(o)
        for k, v in od.items():
            if k in order_fields and k != "bon_commande":
                setattr(o, k, v)
        n_orders += 1
    db.commit()

    n_machines = 0
    for md in payload.get("machines", []):
        bon = (md.get("bon_commande") or "").strip()
        o = db.scalar(select(Order).where(Order.bon_commande == bon)) if bon else None
        if not o:
            continue
        m = Machine(order_id=o.id, bon_commande=bon)
        for k, v in md.items():
            if k in machine_fields:
                setattr(m, k, v)
        db.add(m)
        n_machines += 1
    db.commit()

    return {"reset": bool(payload.get("reset")), "orders": n_orders, "machines": n_machines}


@app.get("/health")
def health():
    return {"ok": True}
