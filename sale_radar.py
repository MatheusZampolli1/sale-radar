"""Sale Radar — avisa no Telegram quando lojas colocam peças em sale (masculino e feminino).

Lojas: Zara, Bershka, H&M, Youcom, C&A (e Renner/Riachuelo quando mapeadas).

Uso:
    python sale_radar.py                    # roda uma checagem
    python sale_radar.py --dry              # não envia nada nem salva estado
    python sale_radar.py --only "C&A"       # só lojas cujo nome contém o texto

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
OFF_WORD = re.compile(r"\bOFF\b")      # "OFF" só como palavra inteira (evita "HOME OFFICE")
MASS_SALE_MIN = 150     # nº de peças em sale que conta como "sale geral"
MIN_JUMP_PTS = 5        # desconto precisa subir X pontos para avisar
MAX_LINES = 12
MAX_PHOTOS = 8
GENDERS = {"M": "Homem", "F": "Mulher"}
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


def get_text(url, extra=None):
    headers = {"User-Agent": UA, "Accept": "text/html,*/*",
               "Accept-Language": "pt-BR,pt;q=0.9", "Accept-Encoding": "gzip",
               "Connection": "close"}
    headers.update(extra or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data.decode("utf8", "replace")


def is_sale_label(text):
    t = (text or "").upper()
    return any(w in t for w in SALE_WORDS) or bool(OFF_WORD.search(t))


def make_item(name, price, old, url, img=None, **extra):
    item = {"name": name.title(), "price": price, "old": old,
            "pct": round((1 - price / old) * 100), "url": url, "img": img}
    item.update(extra)
    return item


def to_cents(v):
    v = int(v)
    return v // 1000 if v >= 1_000_000 else v


def dig(obj, *path):
    """Acessa obj[a][b][c]... sem quebrar se faltar algo."""
    for k in path:
        try:
            obj = obj[k]
        except (KeyError, IndexError, TypeError):
            return None
    return obj


# ---------------------------------------------------------------- Zara
ZARA = "https://www.zara.com/br/pt"
ZARA_ROOT = {"M": "W2025-HOMBRE", "F": "W2025-MUJER"}


def zara_view_all(c):
    for s in c.get("subcategories", []):
        if (s.get("key") or "").endswith("VIEW_ALL_PRODUCTS"):
            return s["id"]
        found = zara_view_all(s)
        if found:
            return found
    return None


def zara(g):
    tree = get_json(f"{ZARA}/categories?ajax=true")
    root = next(c for c in tree["categories"] if c.get("key") == ZARA_ROOT[g])
    cats = []

    def walk(c):
        if c is not root and is_sale_label(c.get("key", "") + " " + (c.get("name") or "")):
            cats.append({"id": c["id"], "name": c.get("name") or c.get("key")})
        for s in c.get("subcategories", []):
            walk(s)

    walk(root)
    view_all = zara_view_all(root)
    items = {}
    for cid in dict.fromkeys([view_all] + [c["id"] for c in cats]):
        data = get_json(f"{ZARA}/category/{cid}/products?ajax=true")
        for grp in data.get("productGroups", []):
            for e in grp.get("elements", []):
                for c in e.get("commercialComponents", []):
                    price, old = c.get("price"), c.get("oldPrice")
                    if c.get("id") and price and old and old > price:
                        seo = c.get("seo", {})
                        url = f"{ZARA}/{seo.get('keyword', 'x')}-p{seo.get('seoProductId', '')}.html"
                        img = dig(c, "detail", "colors", 0, "xmedia", 0, "extraInfo", "deliveryUrl")
                        items[str(c["id"])] = make_item(
                            c.get("name", "?"), price, old, url, img,
                            color=dig(c, "detail", "colors", 0, "id"))
    return items, cats, f"{ZARA}/"


# ------------------------------------------------------------- Bershka
BSK = "https://www.bershka.com/itxrest"
BSK_STORE = "44009529/40259551"      # loja BR (achada no HTML do site: iCountryCode BR)
BSK_TOP = {"M": "MEN", "F": "WOMEN"}
BSK_HDR = {"Referer": "https://www.bershka.com/br/h-man.html",
           "Origin": "https://www.bershka.com",
           # cabecalhos de navegador: sem eles o IP do GitHub toma 403
           "sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130"',
           "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
           "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
           "Accept": "application/json, text/plain, */*",
           "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}


def bershka(g):
    tree = get_json(f"{BSK}/2/catalog/store/{BSK_STORE}/category?languageId=-63&appId=1", BSK_HDR)
    top = next(c for c in tree["categories"] if c.get("nameEn") == BSK_TOP[g])
    cats = []

    def walk(c):
        if c is not top and is_sale_label(f"{c.get('name', '')} {c.get('nameEn', '')}"):
            cats.append({"id": c["id"], "name": c.get("name")})
        for s in c.get("subcategories", []):
            walk(s)

    walk(top)
    ids = []
    for c in cats:
        d = get_json(f"{BSK}/3/catalog/store/{BSK_STORE}/category/{c['id']}/product"
                     f"?showProducts=false&languageId=-63&appId=1", BSK_HDR)
        ids += [i for i in d.get("productIds", []) if i not in ids]
    items = {}
    for i in range(0, len(ids), 20):
        chunk = ",".join(map(str, ids[i:i + 20]))
        d = get_json(f"{BSK}/3/catalog/store/{BSK_STORE}/productsArray?productIds={chunk}"
                     f"&languageId=-63&appId=1", BSK_HDR)
        for p in d.get("products", []):
            for s in p.get("bundleProductSummaries") or [p]:
                for col in (s.get("detail") or {}).get("colors", []):
                    prices = [(to_cents(z["price"]), to_cents(z["oldPrice"]))
                              for z in col.get("sizes", []) if z.get("price") and z.get("oldPrice")]
                    if not prices:
                        continue
                    price, old = min(prices)
                    if old > price:
                        visible = [z for z in col.get("sizes", [])
                                   if z.get("isBuyable") and z.get("visibilityValue", "SHOW") == "SHOW"]
                        items[f"{p['id']}-{col.get('id')}"] = make_item(
                            p.get("name") or col.get("name", "?"), price, old,
                            f"https://www.bershka.com/br/c0p{p['id']}.html",
                            dig(s, "detail", "xmedia", 0, "xmediaItems", 0, "medias", 0,
                                "extraInfo", "deliveryUrl"),
                            pid=p["id"],
                            skus={str(z["sku"]): z["name"] for z in visible},
                            hidden=[z["name"] for z in col.get("sizes", []) if z not in visible])
    return items, cats, "https://www.bershka.com/br/h-man.html"


# ------------------------------------------------- VTEX (H&M, C&A)
HM = "https://www.hm.com.br"
CEA = "https://www.cea.com.br"
HM_CAT = {"M": 2069073889, "F": 1275684304}
CEA_CAT = {"M": 1000083, "F": 1000003}
HM_CACHE = {}   # productId -> {"k": [preco_api, preco_lista], "p": preco_publico_ou_None}
HM_OFFER = re.compile(r'"offers":\{"availability":"[^"]*","itemCondition":"[^"]*","price":([0-9.]+)')


def vtex_sizes(p):
    ok, out = [], []
    for sk in p.get("items", []):
        name = (sk.get("TAMANHO") or sk.get("Tamanho") or ["?"])[0]
        offer = (sk.get("sellers") or [{}])[0].get("commertialOffer") or {}
        (ok if offer.get("AvailableQuantity", 0) > 0 else out).append(name)
    ok = list(dict.fromkeys(ok))
    return {"sizes": ok, "sold_out": [n for n in dict.fromkeys(out) if n not in ok]}


def public_price(pid, link, api_price, old):
    """A API da H&M devolve o preço de MEMBRO. Sale de verdade = preço da página pública < preço de lista."""
    key = [api_price, old]
    c = HM_CACHE.get(pid)
    if c and c["k"] == key:
        return c["p"]
    try:
        m = HM_OFFER.search(get_text(link))
    except Exception:
        return None                      # não conseguiu verificar: tenta de novo na próxima rodada
    if not m:
        return None
    pub = float(m.group(1))
    res = pub if pub < old - 0.005 else None
    HM_CACHE[pid] = {"k": key, "p": res}
    return res


def vtex(base, root_id, verify, min_pct=0):
    tree = get_json(f"{base}/api/catalog_system/pub/category/tree/3")
    cats = []

    def walk(c, top):
        if not top and is_sale_label(c.get("name")):
            cats.append({"id": c["id"], "name": c["name"]})
        for s in c.get("children", []):
            walk(s, False)

    for c in tree:
        if c["id"] == root_id:
            walk(c, True)
        elif is_sale_label(c.get("name")):      # categoria de promoção no topo do site
            cats.append({"id": c["id"], "name": c["name"]})
    items = {}
    for cid in dict.fromkeys([root_id] + [c["id"] for c in cats]):
        for start in range(0, 2500, 50):
            # maiores descontos primeiro: para de paginar quando a página não tem mais desconto
            page = get_json(f"{base}/api/catalog_system/pub/products/search?fq=C:/{cid}/"
                            f"&O=OrderByBestDiscountDESC&_from={start}&_to={start + 49}")
            found = 0
            for p in page:
                for it in p.get("items", [])[:1]:
                    o = (it.get("sellers") or [{}])[0].get("commertialOffer", {})
                    price, old = o.get("Price"), o.get("ListPrice")
                    if not (price and old and old > price):
                        continue
                    if round((1 - price / old) * 100) < min_pct:
                        continue
                    found += 1
                    pid = str(p["productId"])
                    link = p["link"] if p["link"].startswith("http") else base + p["link"]
                    real = public_price(pid, link, price, old) if verify else price
                    if real:
                        items[pid] = make_item(
                            p["productName"], round(real * 100), round(old * 100), link,
                            dig(it, "images", 0, "imageUrl"), **vtex_sizes(p))
            if len(page) < 50 or found == 0:
                break
    return items, cats, f"{base}/"


def hm(g):
    return vtex(HM, HM_CAT[g], verify=True)


CEA_MIN_PCT = 50    # a C&A tem milhares de peças em desconto: só avisa de 50% para cima


def cea(g):
    return vtex(CEA, CEA_CAT[g], verify=False, min_pct=CEA_MIN_PCT)


# -------------------------------------------------------------- Youcom
YOU = "https://www.youcom.com.br"
YOU_API = "https://apisite.youcom.com.br/graphql"
YOU_TERMS = {"M": ("promocao-roupas-masculinas", "masculino"),
             "F": ("promocao-roupas-femininas", "feminino")}


def you_post(q):
    req = urllib.request.Request(
        YOU_API, data=json.dumps({"query": q}).encode(),
        headers={"User-Agent": UA, "Content-Type": "application/json",
                 "Accept": "application/json", "Accept-Encoding": "gzip",
                 "Origin": YOU, "Referer": YOU + "/"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return json.loads(data.decode("utf8"))["data"]


def you_search(term):
    out, start, rows = [], 0, 100
    for _ in range(15):
        q = ('{ productSearch(query:"%s", start:%d, rows:%d){ data{ pagination{hasNextPage} '
             'products{ name price oldPrice productId link images sku colors{ label } } } } }'
             % (term, start, rows))
        d = you_post(q)["productSearch"]["data"]
        out += d["products"]
        start += rows
        if not d["pagination"]["hasNextPage"]:
            break
    return out


def youcom(g):
    items = {}
    for term in YOU_TERMS[g]:
        for p in you_search(term):
            price, old = p.get("price"), p.get("oldPrice")
            if price and old and old > price:
                link = (p.get("link") or "").split("?")[0]
                items[p["productId"]] = make_item(
                    p["name"], round(price * 100), round(old * 100), YOU + link,
                    (p.get("images") or [None])[0],
                    color=((p.get("colors") or [{}])[0] or {}).get("label"))
    return items, [], YOU + "/promocao-roupas-" + ("masculinas" if g == "M" else "femininas")


# ---------------------------------------------------------- Riachuelo
RIA = "https://www.riachuelo.com.br"
RIA_SLUG = {"M": "masculino", "F": "feminino"}
RIA_PAGES = 40          # páginas do /outlet (24 peças cada) ordenadas por desconto
RIA_MIN_PCT = 50        # só avisa de 50% para cima
RIA_HDR = {"sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130"',
           "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Windows"',
           "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
           "Upgrade-Insecure-Requests": "1",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def riachuelo(g):
    items = {}
    for page in range(1, RIA_PAGES + 1):
        html = get_text(f"{RIA}/outlet?sort=desc:discount&page={page}", RIA_HDR)
        show = json.loads(NEXT_DATA.search(html).group(1))["props"]["pageProps"]["plpShowcase"]
        for p in show.get("products", []):
            if (p.get("categoryTree") or [""])[0] != RIA_SLUG[g]:
                continue
            pr = p.get("price") or {}
            final, regular = pr.get("final"), pr.get("regular")
            if not (final and regular and regular > final) or (pr.get("discount") or 0) < RIA_MIN_PCT:
                continue
            ok, out = [], []
            for key, v in (p.get("variations") or {}).items():
                size = key.split(":")[1] if key.count(":") >= 2 else "?"
                (out if v.get("soldOut") or not v.get("remainingUnits") else ok).append(size)
            ok = list(dict.fromkeys(ok))
            items[str(p["sku"])] = make_item(
                p["name"], round(final * 100), round(regular * 100), f"{RIA}/{p['url']}",
                (p.get("images") or [None])[0],
                sizes=ok, sold_out=[x for x in dict.fromkeys(out) if x not in ok])
        if not show.get("nextPage"):
            break
    return items, [], f"{RIA}/outlet"


# -------------------------------------------------------------- Renner
RENNER = "https://www.lojasrenner.com.br"
RENNER_MIN_PCT = 50     # só avisa de 50% para cima
RENNER_CATS = {         # categorias (maiores descontos primeiro, 48 por categoria)
    "F": ["feminino/-/N-4zo6za", "feminino/blusas-e-camisetas/-/N-1w31qx8",
          "feminino/calcas/-/N-14t7lsy", "feminino/camisas/-/N-fvn7vn",
          "feminino/casacos-e-jaquetas/-/N-1qn5lks", "feminino/bermudas-e-shorts/-/N-comsxx",
          "feminino/macacoes-e-jardineiras/-/N-1gtdak8", "feminino/vestidos/-/N-cg003x",
          "feminino/moda-intima/-/N-15ps9mi", "feminino/moda-praia/-/N-1mglth9"],
    "M": ["masculino/-/N-1xeiyoy", "masculino/bermudas-e-shorts/-/N-1gu6hwo",
          "masculino/calcas/-/N-kyxeyg", "masculino/camisas/-/N-1qa4gj6",
          "masculino/camisetas/-/N-1gu70uy", "masculino/casacos-e-jaquetas/-/N-1bpn6of",
          "masculino/esportivo/-/N-1xwl3dh", "masculino/regatas/-/N-xoh8et"]}


def renner_records(o, out):
    if isinstance(o, dict):
        at = o.get("attributes")
        if isinstance(at, dict) and "prop.sku.activePrice" in at:
            out.append(at)
            return
        for v in o.values():
            renner_records(v, out)
    elif isinstance(o, list):
        for v in o:
            renner_records(v, out)


def renner(g):
    items = {}
    for path in RENNER_CATS[g]:
        data = get_json(f"{RENNER}/react/c/{path}?format=json&Ns=prop.product.mostDiscount%7C1",
                        {"Referer": f"{RENNER}/c/{path}", "Accept": "application/json, text/plain, */*"})
        recs = []
        renner_records(data, recs)
        for a in recs:
            try:
                price = float(a["prop.sku.activePrice"][0])
                old = float(a["prop.sku.listPrice"][0])
            except (KeyError, IndexError, ValueError):
                continue
            if not old > price or round((1 - price / old) * 100) < RENNER_MIN_PCT:
                continue
            try:
                gv = json.loads(a["prop.product.groupView"][0])
                sizes = [z["name"] for z in gv.get("groupTwo", [])]
            except (KeyError, IndexError, ValueError):
                sizes = None
            key = a["record.aggregationKey"][0]
            img = (a.get("prop.product.largeImageUrl") or [None])[0]
            items[key] = make_item(
                a["prop.product.displayName"][0], round(price * 100), round(old * 100),
                RENNER + a["prop.product.url"][0], ("https:" + img) if img else None,
                sizes=sizes, sold_out=[], sizes_unverified=True)
    return items, [], f"{RENNER}/c/" + ("feminino" if g == "F" else "masculino")


BASE_STORES = {"Zara": zara, "Bershka": bershka, "H&M": hm, "Youcom": youcom, "C&A": cea,
               "Riachuelo": riachuelo, "Renner": renner}
# (nome, gênero) -> função. Masculino mantém a chave antiga no estado; feminino ganha " (F)".
STORES = {(n, g): (lambda f=f, g=g: f(g)) for g in GENDERS for n, f in BASE_STORES.items()}


def state_key(name, g):
    return name if g == "M" else f"{name} (F)"


# ----------------------------------------------- tamanhos (só no alerta)
def zara_sizes(item):
    page = get_json(item["url"] + "?ajax=true", {
        "Referer": ZARA + "/", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin"})
    colors = dig(page, "product", "detail", "colors") or []
    col = next((c for c in colors if c.get("id") == item.get("color")), None)
    if col is None:
        return None
    sizes = col.get("sizes", [])
    return ([z["name"] for z in sizes if z.get("availability") != "out_of_stock"],
            [z["name"] for z in sizes if z.get("availability") == "out_of_stock"])


def bershka_sizes(item):
    skus = item.get("skus") or {}
    hidden = list(item.get("hidden") or [])
    try:
        st = get_json(f"{BSK}/2/catalog/store/{BSK_STORE}/product/{item['pid']}/stock"
                      f"?languageId=-63", BSK_HDR)
    except Exception:
        return list(skus.values()), hidden      # sem consulta de estoque: usa a lista visível
    avail = {str(z["id"]): z.get("availability")
             for p in st.get("stocks", []) for z in p.get("stocks", [])}
    ok = [n for k, n in skus.items() if avail.get(k, "in_stock") != "out_of_stock"]
    out = [n for k, n in skus.items() if avail.get(k, "in_stock") == "out_of_stock"]
    return ok, out + hidden


def youcom_sizes(item):
    pid = item["url"].rsplit("-p", 1)[-1].split("?")[0]
    q = ('{ productDetails(id:"%s"){ colorVariations{ color{ name } '
         'sizeVariations{ name available } } } }' % pid)
    cols = you_post(q)["productDetails"]["colorVariations"]
    want = str(item.get("color") or "").lower()
    col = next((c for c in cols if str((c.get("color") or {}).get("name", "")).lower() == want), None)
    if col is None:
        return None
    sizes = col["sizeVariations"]
    return ([z["name"] for z in sizes if z.get("available")],
            [z["name"] for z in sizes if not z.get("available")])


SIZE_FETCHERS = {"Zara": zara_sizes, "Bershka": bershka_sizes, "Youcom": youcom_sizes}


def fill_sizes(name, items):
    """Zara, Bershka e Youcom: consulta o estoque real só das peças que vão no alerta."""
    fetch = SIZE_FETCHERS.get(name)
    for it in items:
        if fetch:
            it["sizes"], it["sold_out"] = None, None     # nunca reaproveita tamanho velho
            try:
                res = fetch(it)
                if res is not None:
                    it["sizes"], it["sold_out"] = res
            except Exception as e:
                print(f"  (tamanhos {name} indisponíveis: {type(e).__name__})")


def sizes_line(item):
    """Padrão único em todas as lojas."""
    ok = item.get("sizes")
    if ok is None:
        return ""
    text = "📏 TAMANHOS: " + " · ".join(ok) if ok else "📏 Sem tamanho disponível agora"
    if ok and item.get("sizes_unverified"):
        text += " (grade da loja, estoque por tamanho não checado)"
    out = item.get("sold_out") or []
    if out:
        text += "\n🚫 Esgotado: " + " · ".join(out)
    return text


# --------------------------------------------------------- notificação
def brl(cents):
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt(item):
    line = sizes_line(item)
    return (f"• {item['name']} — {brl(item['price'])} "
            f"(de {brl(item['old'])}, -{item['pct']}%)\n"
            + (f"  {line.replace(chr(10), chr(10) + '  ')}\n" if line else "") + f"  {item['url']}")


def caption(item):
    line = sizes_line(item)
    head = f"{item['store']}\n" if item.get("store") else ""
    return (head + f"{item['name']}\n{brl(item['price'])} "
            f"(de {brl(item['old'])}, -{item['pct']}%)\n"
            + (f"{line}\n" if line else "") + item["url"])


CUR_GENDER = ["M"]      # gênero da loja que está sendo processada (define o chat do Telegram)


def creds():
    """Feminino usa TELEGRAM_BOT_TOKEN_F/TELEGRAM_CHAT_ID_F se os dois existirem; senão o bot padrão."""
    tok_f, chat_f = os.environ.get("TELEGRAM_BOT_TOKEN_F"), os.environ.get("TELEGRAM_CHAT_ID_F")
    if CUR_GENDER[0] == "F" and tok_f and chat_f:
        return tok_f, chat_f
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def tg(method, params, strict=False):
    token, chat = creds()
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
        print("  [foto]", caption(it).replace(chr(10), " | "))
    token, chat = creds()
    if dry or not token or not chat:
        if not dry:
            print("(sem TELEGRAM_BOT_TOKEN/CHAT_ID: mensagem não enviada)")
        return
    tg("sendMessage", {"text": msg["text"], "disable_web_page_preview": "true"}, strict=True)
    for it in msg.get("photos", []):
        cap = caption(it)
        if not (it.get("img") and tg("sendPhoto", {"photo": it["img"], "caption": cap})):
            tg("sendMessage", {"text": cap})        # sem foto: manda só o texto


def compare(name, title, home, items, cats, prev):
    """Devolve (mensagens, primeira_vez)."""
    first = prev is None
    prev = prev or {}
    old_items = prev.get("items", {})
    old_cats = {str(c["id"]) for c in prev.get("categories", [])}
    msgs = []
    if first:
        return msgs, True
    tag = f"🏬 {name.upper()} · {title[len(name) + 1:].upper()}"     # loja em destaque
    new_cats = [c for c in cats if str(c["id"]) not in old_cats]
    if new_cats:
        msgs.append(f"{tag}\n🚨 Abriu área de promoção: "
                    f"{', '.join(c['name'] for c in new_cats)}\n{home}")
    if len(items) >= MASS_SALE_MIN and len(old_items) < MASS_SALE_MIN:
        msgs.append(f"{tag}\n🔥 SALE GERAL: {len(items)} peças com desconto agora!\n{home}")
    novos = sorted((i for k, i in items.items() if k not in old_items), key=lambda i: -i["pct"])
    subiu = [i for k, i in items.items()
             if k in old_items and i["pct"] - old_items[k]["pct"] >= MIN_JUMP_PTS]
    if novos or subiu:
        msg = f"{tag}\n🛍️ {len(novos)} peça(s) nova(s) em sale"
        if subiu:
            msg += f", {len(subiu)} com desconto maior"
        shown = (novos + subiu)[:MAX_LINES]
        for i in shown:
            i["store"] = tag
        fill_sizes(name, shown)
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
    only = sys.argv[sys.argv.index("--only") + 1].lower() if "--only" in sys.argv else None
    load_env()
    state = json.loads(STATE.read_text(encoding="utf8")) if STATE.exists() else {}
    if "items" in state:                                   # formato antigo (só Zara)
        state = {"stores": {"Zara": state}}
    stores = state.setdefault("stores", {})
    HM_CACHE.update(state.get("hm_checked", {}))
    status = {}                                            # resumo por loja (visível no state.json)

    for (name, g), fn in STORES.items():
        if only and only not in name.lower():
            continue
        CUR_GENDER[0] = g
        key, title = state_key(name, g), f"{name} {GENDERS[g]}"
        try:
            items, cats, home = fn()
        except Exception as e:                              # uma loja caída não derruba as outras
            print(f"[{title}] ERRO: {type(e).__name__}: {str(e)[:120]}")
            status[title] = f"ERRO {type(e).__name__}: {str(e)[:60]}"
            if os.environ.get("RADAR_DEBUG"):
                traceback.print_exc()
            continue
        msgs, first = compare(name, title, home, items, cats, stores.get(key))
        for m in msgs:
            send(m, dry)
        areas = [c["name"] for c in cats] or "nenhuma"
        if first:
            print(f"[{title}] 1ª execução: {len(items)} peças em sale memorizadas. Áreas: {areas}")
        elif not msgs:
            print(f"[{title}] sem novidades ({len(items)} em sale)")
        stores[key] = {"items": items, "categories": cats}
        status[title] = len(items)

    state["hm_checked"] = HM_CACHE
    state["status"] = {**state.get("status", {}), **status}
    if not dry:
        STATE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf8")


if __name__ == "__main__":
    main()
