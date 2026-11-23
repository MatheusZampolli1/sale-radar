"""Sale Radar — avisa no Telegram quando lojas (masculino) colocam peças em sale.

Lojas: Zara, Bershka, H&M (e Youcom quando a fonte de dados for mapeada).

Uso:
    python sale_radar.py            # roda uma checagem
    python sale_radar.py --dry      # não envia nada nem salva estado

Variáveis (arquivo .env ou ambiente): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import gzip
import json
import os
import re
import sys
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STATE = ROOT / "state.json"
SALE_WORDS = ("SALE", "REBAJA", "PROMO", "OFERTA", "DESCONTO", "OUTLET",
              "SPECIAL PRICE", "PREÇOS ESPECIAIS", "LIQUIDA")
OFF_WORD = re.compile(r"\bOFF\b")  # so palavra inteira (evita HOME OFFICE)
MASS_SALE_MIN = 150     # nº de peças em sale que conta como "sale geral"
MIN_JUMP_PTS = 5        # desconto precisa subir X pontos para avisar
MAX_LINES = 12
MAX_PHOTOS = 8
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def get_json(url, extra=None):
    headers = {"User-Agent": UA, "Accept": "application/json, */*",
               "Accept-Language": "pt-BR,pt;q=0.9", "Accept-Encoding": "gzip",
               "Connection": "close"}
    headers.update(extra or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return json.loads(data.decode("utf8"))


def is_sale_label(text):
    t = (text or "").upper()
    return any(w in t for w in SALE_WORDS) or bool(OFF_WORD.search(t))


def make_item(name, price, old, url, img=None):
    return {"name": name.title(), "price": price, "old": old,
            "pct": round((1 - price / old) * 100), "url": url, "img": img}


def dig(obj, *path):
    """Acessa obj[a][b][c]... sem quebrar se faltar algo."""
    for k in path:
        try:
            obj = obj[k]
        except (KeyError, IndexError, TypeError):
            return None
    return obj


def get_text(url):
    headers = {"User-Agent": UA, "Accept": "text/html,*/*",
               "Accept-Language": "pt-BR,pt;q=0.9", "Accept-Encoding": "gzip",
               "Connection": "close"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data.decode("utf8", "replace")


# ---------------------------------------------------------------- Zara
ZARA = "https://www.zara.com/br/pt"


def zara():
    tree = get_json(f"{ZARA}/categories?ajax=true")
    men = next(c for c in tree["categories"] if c.get("key") == "W2025-HOMBRE")
    cats = []

    def walk(c):
        if c is not men and is_sale_label(c.get("key", "") + " " + (c.get("name") or "")):
            cats.append({"id": c["id"], "name": c.get("name") or c.get("key")})
        for s in c.get("subcategories", []):
            walk(s)

    walk(men)
    items = {}
    for cid in dict.fromkeys([2510572] + [c["id"] for c in cats]):   # 2510572 = HOMEM > VER TUDO
        data = get_json(f"{ZARA}/category/{cid}/products?ajax=true")
        for g in data.get("productGroups", []):
            for e in g.get("elements", []):
                for c in e.get("commercialComponents", []):
                    price, old = c.get("price"), c.get("oldPrice")
                    if c.get("id") and price and old and old > price:
                        seo = c.get("seo", {})
                        url = f"{ZARA}/{seo.get('keyword', 'x')}-p{seo.get('seoProductId', '')}.html"
                        img = dig(c, "detail", "colors", 0, "xmedia", 0, "extraInfo", "deliveryUrl")
                        items[str(c["id"])] = make_item(c.get("name", "?"), price, old, url, img)
    return items, cats, f"{ZARA}/"


# ------------------------------------------------------------- Bershka
BSK = "https://www.bershka.com/itxrest"
BSK_STORE = "45109565/40259535"
BSK_HDR = {"Referer": "https://www.bershka.com/br/h-man.html",
           "Origin": "https://www.bershka.com",
           # cabecalhos de navegador: sem eles o IP do GitHub toma 403
           "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130"',
           "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
           "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
           "Accept": "application/json, text/plain, */*",
           "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}


def bershka():
    tree = get_json(f"{BSK}/2/catalog/store/{BSK_STORE}/category?languageId=-300&appId=1", BSK_HDR)
    men = next(c for c in tree["categories"] if c.get("nameEn") == "MEN" or c.get("name") == "MEN")
    cats = []

    def walk(c):
        if c is not men and is_sale_label(f"{c.get('name', '')} {c.get('nameEn', '')}"):
            cats.append({"id": c["id"], "name": c.get("name")})
        for s in c.get("subcategories", []):
            walk(s)

    walk(men)
    ids = []
    for c in cats:
        d = get_json(f"{BSK}/3/catalog/store/{BSK_STORE}/category/{c['id']}/product"
                     f"?showProducts=false&languageId=-300&appId=1", BSK_HDR)
        ids += [i for i in d.get("productIds", []) if i not in ids]
    items = {}
    for i in range(0, len(ids), 20):
        chunk = ",".join(map(str, ids[i:i + 20]))
        d = get_json(f"{BSK}/3/catalog/store/{BSK_STORE}/productsArray?productIds={chunk}"
                     f"&languageId=-300&appId=1", BSK_HDR)
        for p in d.get("products", []):
            for s in p.get("bundleProductSummaries") or [p]:
                for col in (s.get("detail") or {}).get("colors", []):
                    prices = [(int(z["price"]) // 1000, int(z["oldPrice"]) // 1000)
                              for z in col.get("sizes", []) if z.get("price") and z.get("oldPrice")]
                    if not prices:
                        continue
                    price, old = min(prices)
                    if old > price:
                        url = f"https://www.bershka.com/br/{p.get('name', 'x')}-c0p{p['id']}.html"
                        items[f"{p['id']}-{col.get('id')}"] = make_item(
                            p.get("name") or col.get("name", "?"), price, old,
                            f"https://www.bershka.com/br/c0p{p['id']}.html",
                            dig(s, "detail", "xmedia", 0, "xmediaItems", 0, "medias", 0,
                                "extraInfo", "deliveryUrl"))
    return items, cats, "https://www.bershka.com/br/h-man.html"


# ----------------------------------------------------------------- H&M
HM = "https://www.hm.com.br"
HM_MEN = 2069073889
HM_CACHE = {}   # productId -> {"k": [preco_api, preco_lista], "p": preco_publico_ou_None}
HM_OFFER = re.compile(r'"offers":\{"availability":"[^"]*","itemCondition":"[^"]*","price":([0-9.]+)')


def hm_public_price(pid, link, api_price, old):
    """A API da H&M devolve o preco de MEMBRO. Sale de verdade = preco da pagina publica < preco de lista."""
    key = [api_price, old]
    c = HM_CACHE.get(pid)
    if c and c["k"] == key:
        return c["p"]
    try:
        m = HM_OFFER.search(get_text(link))
    except Exception:
        return None                      # nao conseguiu verificar: tenta de novo na proxima rodada
    if not m:
        return None
    pub = float(m.group(1))
    res = pub if pub < old - 0.005 else None
    HM_CACHE[pid] = {"k": key, "p": res}
    return res


def hm():
    tree = get_json(f"{HM}/api/catalog_system/pub/category/tree/3")
    cats = []

    def walk(c, top):
        if not top and is_sale_label(c.get("name")):
            cats.append({"id": c["id"], "name": c["name"]})
        for s in c.get("children", []):
            walk(s, False)

    for c in tree:
        if c["id"] == HM_MEN:
            walk(c, True)
        elif is_sale_label(c.get("name")):      # categoria de promoção no topo do site
            cats.append({"id": c["id"], "name": c["name"]})
    items = {}
    for cid in dict.fromkeys([HM_MEN] + [c["id"] for c in cats]):
        for start in range(0, 2500, 50):
            page = get_json(f"{HM}/api/catalog_system/pub/products/search?fq=C:/{cid}/"
                            f"&_from={start}&_to={start + 49}")
            for p in page:
                for it in p.get("items", [])[:1]:
                    o = (it.get("sellers") or [{}])[0].get("commertialOffer", {})
                    price, old = o.get("Price"), o.get("ListPrice")
                    if price and old and old > price:
                        pid = str(p["productId"])
                        real = hm_public_price(pid, p["link"], price, old)
                        if real:
                            items[pid] = make_item(
                                p["productName"], round(real * 100), round(old * 100), p["link"],
                                dig(it, "images", 0, "imageUrl"))
            if len(page) < 50:
                break
    return items, cats, f"{HM}/masculino"


# -------------------------------------------------------------- Youcom
YOU = "https://www.youcom.com.br"
YOU_API = "https://apisite.youcom.com.br/graphql"


def you_search(term):
    out, start, rows = [], 0, 100
    for _ in range(15):
        q = ('{ productSearch(query:"%s", start:%d, rows:%d){ data{ pagination{hasNextPage} '
             'products{ name price oldPrice productId link images } } } }') % (term, start, rows)
        req = urllib.request.Request(
            YOU_API, data=json.dumps({"query": q}).encode(),
            headers={"User-Agent": UA, "Content-Type": "application/json",
                     "Accept": "application/json", "Accept-Encoding": "gzip",
                     "Origin": YOU, "Referer": YOU + "/"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
        d = json.loads(data.decode("utf8"))["data"]["productSearch"]["data"]
        out += d["products"]
        start += rows
        if not d["pagination"]["hasNextPage"]:
            break
    return out


def youcom():
    items = {}
    # pagina oficial de promocao masculina + busca geral "masculino"
    for term in ("promocao-roupas-masculinas", "masculino"):
        for p in you_search(term):
            price, old = p.get("price"), p.get("oldPrice")
            if price and old and old > price:
                link = (p.get("link") or "").split("?")[0]
                items[p["productId"]] = make_item(
                    p["name"], round(price * 100), round(old * 100), YOU + link,
                    (p.get("images") or [None])[0])
    return items, [], YOU + "/promocao-roupas-masculinas"


STORES = {"Zara": zara, "Bershka": bershka, "H&M": hm, "Youcom": youcom}


# --------------------------------------------------------- notificação
def brl(cents):
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt(item):
    return (f"• {item['name']} — {brl(item['price'])} "
            f"(de {brl(item['old'])}, -{item['pct']}%)\n  {item['url']}")


def caption(item):
    return (f"{item['name']}\n{brl(item['price'])} "
            f"(de {brl(item['old'])}, -{item['pct']}%)\n{item['url']}")


def tg(method, params, strict=False):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    body = urllib.parse.urlencode({"chat_id": chat, **params}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", body, timeout=30)
        return True
    except Exception as e:
        if strict:
            raise
        print(f"  (telegram {method} falhou: {type(e).__name__})")
        return False


def send(msg, dry):
    if isinstance(msg, str):
        msg = {"text": msg}
    print(msg["text"], "\n" + "-" * 40)
    for it in msg.get("photos", []):
        print("  [foto]", it["name"], it.get("img"))
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if dry or not token or not chat:
        if not dry:
            print("(sem TELEGRAM_BOT_TOKEN/CHAT_ID: mensagem não enviada)")
        return
    tg("sendMessage", {"text": msg["text"], "disable_web_page_preview": "true"}, strict=True)
    for it in msg.get("photos", []):
        cap = caption(it)
        if not (it.get("img") and tg("sendPhoto", {"photo": it["img"], "caption": cap})):
            tg("sendMessage", {"text": cap})        # sem foto: manda so o texto


def compare(store, home, items, cats, prev):
    """Devolve (mensagens, primeira_vez)."""
    first = prev is None
    prev = prev or {}
    old_items = prev.get("items", {})
    old_cats = {str(c["id"]) for c in prev.get("categories", [])}
    msgs = []
    if first:
        return msgs, True
    new_cats = [c for c in cats if str(c["id"]) not in old_cats]
    if new_cats:
        msgs.append(f"🚨 {store} Homem abriu área de promoção: "
                    f"{', '.join(c['name'] for c in new_cats)}\n{home}")
    if len(items) >= MASS_SALE_MIN and len(old_items) < MASS_SALE_MIN:
        msgs.append(f"🔥 SALE GERAL na {store} Homem: {len(items)} peças com desconto agora!\n{home}")
    novos = sorted((i for k, i in items.items() if k not in old_items), key=lambda i: -i["pct"])
    subiu = [i for k, i in items.items()
             if k in old_items and i["pct"] - old_items[k]["pct"] >= MIN_JUMP_PTS]
    if novos or subiu:
        msg = f"🛍️ {store} Homem: {len(novos)} peça(s) nova(s) em sale"
        if subiu:
            msg += f", {len(subiu)} com desconto maior"
        shown = (novos + subiu)[:MAX_LINES]
        photos = [i for i in shown if i.get("img")][:MAX_PHOTOS]
        sem_foto = [i for i in shown if i not in photos]
        if sem_foto:
            msg += "\n\n" + "\n".join(fmt(i) for i in sem_foto)
        extra = len(novos) + len(subiu) - len(shown)
        if extra > 0:
            msg += f"\n… e mais {extra}"
        msgs.append({"text": msg, "photos": photos})
    return msgs, False


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry" in sys.argv
    load_env()
    state = json.loads(STATE.read_text(encoding="utf8")) if STATE.exists() else {}
    if "items" in state:                                   # formato antigo (só Zara)
        state = {"stores": {"Zara": state}}
    stores = state.setdefault("stores", {})
    HM_CACHE.update(state.get("hm_checked", {}))

    for name, fn in STORES.items():
        try:
            items, cats, home = fn()
        except Exception as e:                              # uma loja caída não derruba as outras
            print(f"[{name}] ERRO: {type(e).__name__}: {str(e)[:120]}")
            if os.environ.get("RADAR_DEBUG"):
                traceback.print_exc()
            continue
        msgs, first = compare(name, home, items, cats, stores.get(name))
        for m in msgs:
            send(m, dry)
        areas = [c["name"] for c in cats] or "nenhuma"
        if first:
            print(f"[{name}] 1ª execução: {len(items)} peças em sale memorizadas. Áreas: {areas}")
        elif not msgs:
            print(f"[{name}] sem novidades ({len(items)} em sale)")
        stores[name] = {"items": items, "categories": cats}

    state["hm_checked"] = HM_CACHE
    if not dry:
        STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf8")


if __name__ == "__main__":
    main()
