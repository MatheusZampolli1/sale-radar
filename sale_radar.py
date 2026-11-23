"""Sale Radar — avisa no Telegram quando a Zara (masculino) coloca peças em sale.

Uso:
    python sale_radar.py            # roda uma checagem
    python sale_radar.py --dry      # não envia nada, só imprime

Variáveis (arquivo .env ou ambiente): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import gzip
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
BASE = "https://www.zara.com/br/pt"
MEN_KEY = "W2025-HOMBRE"          # categoria "HOMEM" da Zara
ALL_PRODUCTS_ID = 2510572          # HOMEM > VER TUDO
SALE_WORDS = ("SALE", "REBAJA", "PROMO", "OFERTA", "DESCONTO", "OUTLET")
MASS_SALE_MIN = 150                # nº de peças em sale que conta como "sale geral"
MIN_JUMP_PTS = 5                   # desconto precisa subir X pontos para avisar
MAX_LINES = 15
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Accept-Language": "pt-BR,pt;q=0.9",
        "Accept-Encoding": "gzip", "Connection": "close"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return json.loads(data.decode("utf8"))


def sale_categories():
    """Categorias do menu masculino cujo nome/chave sugere promoção."""
    tree = get_json(f"{BASE}/categories?ajax=true")
    men = next(c for c in tree["categories"] if c.get("key") == MEN_KEY)
    found = []

    def walk(c):
        label = (c.get("key", "") + " " + (c.get("name") or "")).upper()
        if c is not men and any(w in label for w in SALE_WORDS):
            found.append({"id": c["id"], "name": c.get("name") or c.get("key")})
        for s in c.get("subcategories", []):
            walk(s)

    walk(men)
    return found


def products(category_id):
    data = get_json(f"{BASE}/category/{category_id}/products?ajax=true")
    for g in data.get("productGroups", []):
        for e in g.get("elements", []):
            for c in e.get("commercialComponents", []):
                if c.get("id") and c.get("price"):
                    yield c


def brl(cents):
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def collect_sale_items(category_ids):
    items = {}
    for cid in category_ids:
        for c in products(cid):
            old, price = c.get("oldPrice"), c["price"]
            if old and old > price:
                seo = c.get("seo", {})
                url = f"{BASE}/{seo.get('keyword', 'x')}-p{seo.get('seoProductId', '')}.html"
                items[str(c["id"])] = {
                    "name": c.get("name", "?").title(),
                    "price": price,
                    "old": old,
                    "pct": round((1 - price / old) * 100),
                    "url": url,
                }
    return items


def send(text, dry):
    print(text, "\n" + "-" * 40)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not token or not chat:
        if not dry:
            print("(sem TELEGRAM_BOT_TOKEN/CHAT_ID: mensagem não enviada)")
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=30)


def fmt(item):
    return f"• {item['name']} — {brl(item['price'])} (de {brl(item['old'])}, -{item['pct']}%)\n  {item['url']}"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry" in sys.argv
    load_env()
    cats = sale_categories()
    cat_ids = [ALL_PRODUCTS_ID] + [c["id"] for c in cats]
    items = collect_sale_items(dict.fromkeys(cat_ids))

    first_run = not STATE.exists()
    old = json.loads(STATE.read_text(encoding="utf8")) if not first_run else {}
    old_items = old.get("items", {})
    old_cats = {str(c["id"]) for c in old.get("categories", [])}

    messages = []
    new_cats = [c for c in cats if str(c["id"]) not in old_cats]
    if new_cats and not first_run:
        names = ", ".join(c["name"] for c in new_cats)
        messages.append(f"🚨 Zara Homem abriu uma área de promoção: {names}\n{BASE}/")
    if len(items) >= MASS_SALE_MIN and len(old_items) < MASS_SALE_MIN and not first_run:
        messages.append(f"🔥 SALE GERAL na Zara Homem: {len(items)} peças com desconto agora!")

    novos = [i for k, i in items.items() if k not in old_items]
    subiu = [i for k, i in items.items()
             if k in old_items and i["pct"] - old_items[k]["pct"] >= MIN_JUMP_PTS]
    if not first_run and (novos or subiu):
        novos.sort(key=lambda i: -i["pct"])
        linhas = [fmt(i) for i in novos[:MAX_LINES]]
        msg = f"🛍️ Zara Homem: {len(novos)} peça(s) nova(s) em sale"
        if subiu:
            msg += f", {len(subiu)} com desconto maior"
        msg += "\n\n" + "\n".join(linhas)
        if len(novos) > MAX_LINES:
            msg += f"\n… e mais {len(novos) - MAX_LINES}"
        messages.append(msg)

    for m in messages:
        send(m, dry)
    if first_run:
        print(f"Primeira execução: {len(items)} peças em sale memorizadas (sem alerta). "
              f"Áreas de promoção: {[c['name'] for c in cats] or 'nenhuma'}.")
    elif not messages:
        print(f"Sem novidades. {len(items)} peças em sale.")

    if not dry:
        STATE.write_text(json.dumps({"items": items, "categories": cats},
                                    ensure_ascii=False), encoding="utf8")


if __name__ == "__main__":
    main()
