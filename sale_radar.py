"""Sale Radar — avisa no Telegram quando lojas colocam peças em sale (masculino e feminino).

Lojas: Zara, Bershka, H&M, Youcom, C&A (e Renner/Riachuelo quando mapeadas).

Uso:
    python sale_radar.py                    # roda uma checagem
    python sale_radar.py --dry              # não envia nada nem salva estado
    python sale_radar.py --only "C&A"       # só lojas cujo nome contém o texto
    python sale_radar.py --stores "A,B"     # só essas lojas (nome exato); --skip "A,B" pula essas
    python sale_radar.py --state arq.json   # usa outro arquivo de memória

Variáveis (arquivo .env ou ambiente): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import gzip
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
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
MAX_LINES = 15
MAX_PHOTOS = 15
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


def zara_view_all(root):
    """Categorias 'ver tudo'. Masculino tem uma só (VIEW_ALL_PRODUCTS); feminino tem uma por tipo (VER_TODO)."""
    found = []

    def walk(c):
        for s in c.get("subcategories", []):
            k = (s.get("key") or "").upper()
            if k.endswith("VIEW_ALL_PRODUCTS"):
                found.append(("A", s["id"]))
            elif k.endswith("VER_TODO") and not any(w in k for w in ("PERFUMES", "ZARA_HOME")):
                found.append(("B", s["id"]))
            walk(s)

    walk(root)
    single = [i for t, i in found if t == "A"]
    return single[:1] or [i for t, i in found if t == "B"]


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
    for cid in dict.fromkeys(view_all + [c["id"] for c in cats]):
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


BSK_IMG_PREF = ("b1", "s1", "a1t", "a1", "p1")      # "r1" é a amostra de tecido (borrada)


def bsk_image(summary, col_id):
    """Foto principal da COR (não a amostra de tecido)."""
    sets = dig(summary, "detail", "xmedia") or []
    xs = next((x for x in sets
               if str(x.get("colorCode") or str(x.get("path", "")).strip("/")) == str(col_id)), None)
    if xs is None and sets:
        xs = sets[0]
    if not xs:
        return None
    medias = [m for it in xs.get("xmediaItems", []) for m in it.get("medias", [])
              if dig(m, "extraInfo", "deliveryUrl")]
    by_name = {dig(m, "extraInfo", "originalName"): m for m in medias}
    for n in BSK_IMG_PREF:
        if n in by_name:
            return by_name[n]["extraInfo"]["deliveryUrl"]
    rest = [m for m in medias if not str(dig(m, "extraInfo", "originalName") or "").startswith("r")]
    return dig((rest or medias or [None])[0], "extraInfo", "deliveryUrl")


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
                cols = (s.get("detail") or {}).get("colors", [])
                col_names = [c.get("name") for c in cols]
                for col in cols:
                    prices = [(to_cents(z["price"]), to_cents(z["oldPrice"]))
                              for z in col.get("sizes", []) if z.get("price") and z.get("oldPrice")]
                    if not prices:
                        continue
                    price, old = min(prices)
                    if old > price:
                        visible = [z for z in col.get("sizes", [])
                                   if z.get("isBuyable") and z.get("visibilityValue", "SHOW") == "SHOW"]
                        cname = col.get("name") or ""
                        if col_names.count(col.get("name")) > 1:      # duas cores com o mesmo nome
                            cname += f" (cód. {col.get('id')})"
                        label = f"{p.get('name') or '?'} — {cname}" if cname else (p.get("name") or "?")
                        items[f"{p['id']}-{col.get('id')}"] = make_item(
                            label, price, old,
                            f"https://www.bershka.com/br/c0p{p['id']}.html?colorId={col.get('id')}",
                            bsk_image(s, col.get("id")),
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
                RENNER + a["prop.product.url"][0], norm_img(img),
                sizes=sizes, sold_out=[], sizes_unverified=True)
    return items, [], f"{RENNER}/c/" + ("feminino" if g == "F" else "masculino")


# ------------------------------------------- Magazord (Monteleste, Comp Supply)
MONTELESTE = "https://www.monteleste.com.br"
COMPSUPPLY = "https://www.compsupply.com.br"


def magazord_sizes(x):
    ok, out = [], []

    def walk(o):
        if isinstance(o, dict):
            if o.get("deri_nome") == "Tamanho":
                (ok if o.get("estoque") else out).append(str(o.get("derivacao")))
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(x.get("derivacoesItem"))
    ok = list(dict.fromkeys(ok))
    return {"sizes": ok, "sold_out": [n for n in dict.fromkeys(out) if n not in ok]}


def magazord(base, g):
    """Página /sale da loja; os produtos vêm em JSON dentro do HTML (itens: [...])."""
    items = {}
    for page in range(1, 21):
        html = get_text(f"{base}/sale?p={page}", RIA_HDR)
        i = html.find("itens: ")
        if i < 0:
            break
        arr, _ = json.JSONDecoder().raw_decode(html[i + 7:])
        for x in arr:
            try:
                price = float(x["valor"])
                old = float(x["valor_de"]) if x.get("valor_de") else 0
            except (KeyError, TypeError, ValueError):
                continue
            if not old > price:
                continue
            items[str(x["codigo"])] = make_item(
                x.get("titulo") or x["nome"], round(price * 100), round(old * 100),
                base + x["link"].split("#")[0], x.get("midia_url"), **magazord_sizes(x))
        if not arr or '<link rel="next"' not in html:
            break
    return items, [], f"{base}/sale"


# ---------------------------------------------------- The Dust Company (Shoppub)
DUST = "https://www.thedustcompany.com.br"
DUST_CATS = ("ofertas", "flashblack")
DUST_CARD = re.compile(r'<li[^>]*class="[^"]*product-item[^"]*"')


def br_cents(txt):
    return round(float(txt.replace(".", "").replace(",", ".")) * 100)


def dust(g):
    items = {}
    for cat in DUST_CATS:
        seen = set()
        for page in range(1, 31):
            try:
                html = get_text(f"{DUST}/categoria/{cat}/?page={page}", RIA_HDR)
            except Exception:
                if page == 1:
                    raise                       # falha na 1ª página: erro visível, e não "0 peças"
                break
            new = 0
            for c in DUST_CARD.split(html)[1:]:
                sku = re.search(r'data-product-sku="([^"]+)"', c)
                de = re.search(r'data-preco-de="([^"]+)"', c)
                por = re.search(r'data-preco-por="([^"]+)"', c)
                name = re.search(r'data-product-name="([^"]*)"', c)
                href = re.search(r'href="(/produto/[^"$]+)"', c)
                if not (sku and de and por and name and href) or sku.group(1) in seen:
                    continue
                seen.add(sku.group(1))
                new += 1
                old_c, price_c = br_cents(de.group(1)), br_cents(por.group(1))
                if not old_c > price_c:
                    continue
                img = re.search(r'data-src="(https://[^"]+)"', c)
                items[sku.group(1)] = make_item(
                    name.group(1), price_c, old_c, DUST + href.group(1),
                    img.group(1) if img else None, sizes_unverified=True)
            if new == 0:
                break
    return items, [], f"{DUST}/categoria/ofertas/"


def dust_sizes(item):
    html = get_text(item["url"], RIA_HDR)
    ok, out = [], []
    for m in re.finditer(r'<input type="radio"([^>]*)data-variation-name="Tamanho"([^>]*)>\s*'
                         r'<label([^>]*)>\s*<span>\s*([^<]+?)\s*</span>', html):
        flags = (m.group(1) + m.group(2) + m.group(3)).lower()
        (out if ("disabled" in flags or "esgotado" in flags or "indispon" in flags) else ok).append(m.group(4))
    if not ok and not out:
        return None
    return list(dict.fromkeys(ok)), list(dict.fromkeys(out))


BASE_STORES = {"Zara": zara, "Bershka": bershka, "H&M": hm, "Youcom": youcom, "C&A": cea,
               "Riachuelo": riachuelo, "Renner": renner}
# (nome, gênero) -> função. Masculino mantém a chave antiga no estado; feminino ganha " (F)".
STORES = {(n, g): (lambda f=f, g=g: f(g)) for g in GENDERS for n, f in BASE_STORES.items()}


# Lojas só masculinas (pedido do usuário): (nome) -> função(gênero)
MEN_ONLY = {"The Dust Company": dust,
            "Monteleste": lambda g: magazord(MONTELESTE, g),
            "Comp Supply": lambda g: magazord(COMPSUPPLY, g)}
STORES.update({(n, "M"): (lambda f=f: f("M")) for n, f in MEN_ONLY.items()})


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


SIZE_FETCHERS = {"Zara": zara_sizes, "Bershka": bershka_sizes, "Youcom": youcom_sizes,
                 "The Dust Company": dust_sizes}


FILLED = set()          # peças cujo tamanho já foi consultado nesta volta
NOVOS_COUNT = [0]       # peças avisadas (para o resumo diário)
CONFIG = {}             # config.json (filtros pessoais)
BRT = timezone(timedelta(hours=-3))
FILTER_CAP = 60         # máximo de peças consultadas por loja quando há filtro de tamanho


def fill_sizes(name, items):
    """Zara, Bershka e Youcom: consulta o estoque real só das peças que vão no alerta."""
    fetch = SIZE_FETCHERS.get(name)
    for it in items:
        if fetch and id(it) not in FILLED:
            FILLED.add(id(it))
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


def price_note(i):
    pm = i.get("prev_min")
    if not pm:
        return ""
    if i["price"] < pm:
        return f"🔻 Menor preço que já vi (antes {brl(pm)})"
    if i["price"] > pm:
        return f"ℹ️ Já vi mais barato: {brl(pm)}"
    return ""


def fmt(item):
    extra = "".join(f"  {x.replace(chr(10), chr(10) + '  ')}\n"
                    for x in (price_note(item), sizes_line(item)) if x)
    return (f"• {item['name']} — {brl(item['price'])} "
            f"(de {brl(item['old'])}, -{item['pct']}%)\n" + extra + f"  {item['url']}")


def caption(item):
    head = f"{item['store']}\n" if item.get("store") else ""
    extra = "".join(f"{x}\n" for x in (price_note(item), sizes_line(item)) if x)
    return (head + f"{item['name']}\n{brl(item['price'])} "
            f"(de {brl(item['old'])}, -{item['pct']}%)\n" + extra + item["url"])


CUR_GENDER = ["M"]      # gênero da loja que está sendo processada (define o chat do Telegram)


def creds():
    """Feminino usa TELEGRAM_BOT_TOKEN_F/TELEGRAM_CHAT_ID_F se os dois existirem; senão o bot padrão."""
    tok_f, chat_f = os.environ.get("TELEGRAM_BOT_TOKEN_F"), os.environ.get("TELEGRAM_CHAT_ID_F")
    if CUR_GENDER[0] == "F" and tok_f and chat_f:
        return tok_f, chat_f
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def tg(method, params, strict=False, quiet=False):
    token, chat = creds()
    body = urllib.parse.urlencode({"chat_id": chat, **params}).encode()
    for attempt in (1, 2):
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", body, timeout=30)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 1:           # limite de envios: espera e tenta de novo
                try:
                    wait = json.loads(e.read().decode()).get("parameters", {}).get("retry_after", 5)
                except Exception:
                    wait = 5
                time.sleep(min(int(wait) + 1, 30))
                continue
            if strict:
                raise
            if not quiet:
                print(f"  (telegram {method} falhou: HTTP {e.code})")
            return False
        except Exception as e:
            if strict:
                raise
            if not quiet:
                print(f"  (telegram {method} falhou: {type(e).__name__})")
            return False
    return False


# ------------------------------------------------ foto garantida em todo alerta
def norm_img(u):
    if not u:
        return None
    u = str(u).strip()
    while u.startswith("https:https:") or u.startswith("https:http"):
        u = u[6:]
    if u.startswith("//"):
        u = "https:" + u
    return u if u.startswith("http") else None


def to_jpeg(data):
    """Telegram aceita JPEG/PNG/GIF; converte WEBP/AVIF se o Pillow estiver instalado."""
    if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"GIF8":
        return data
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        return buf.getvalue()
    except Exception:
        return data


def fetch_image(url, referer=None):
    h = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
         "Accept-Language": "pt-BR,pt;q=0.9"}
    if referer:
        h["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
        return to_jpeg(r.read(12_000_000))


def page_image(url):
    """og:image da página do produto (quando a lista não trouxe imagem utilizável)."""
    try:
        html = get_text(url, RIA_HDR)
    except Exception:
        return None
    m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
         or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html))
    return norm_img(m.group(1)) if m else None


def tg_upload_photo(photo, caption_text, markup):
    token, chat = creds()
    bd = "----radar" + str(int(time.time() * 1000))
    parts = []

    def field(n, v):
        parts.append(f'--{bd}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode("utf8"))

    field("chat_id", chat)
    field("caption", caption_text)
    if markup:
        field("reply_markup", markup)
    parts.append(f'--{bd}\r\nContent-Disposition: form-data; name="photo"; filename="foto.jpg"\r\n'
                 f'Content-Type: image/jpeg\r\n\r\n'.encode())
    parts.append(photo)
    parts.append(f"\r\n--{bd}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto",
                                 data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={bd}"})
    urllib.request.urlopen(req, timeout=60)


def send_photo(it, cap, mk):
    """Tenta: link da imagem -> baixar e enviar o arquivo -> imagem da página do produto."""
    extra = {"reply_markup": mk} if mk else {}
    img = norm_img(it.get("img"))
    if img and (tg("sendPhoto", {"photo": img, "caption": cap, **extra}, quiet=True)
                or tg("sendPhoto", {"photo": img, "caption": cap}, quiet=True)):
        return True
    for src in dict.fromkeys([img, None]):
        src = src or page_image(it["url"])
        if not src:
            continue
        try:
            tg_upload_photo(fetch_image(src, it["url"]), cap, mk)
            return True
        except Exception as e:
            print(f"  (foto por upload falhou: {type(e).__name__})")
    return False


def buttons(it):
    row = []
    if str(it.get("url", "")).startswith("http"):
        row.append({"text": "🔗 Abrir", "url": it["url"]})
    if it.get("skey"):
        row.append({"text": "🔕 Silenciar loja", "callback_data": "mute:" + it["skey"]})
    return json.dumps({"inline_keyboard": [row]}) if row else None


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
        mk = buttons(it)
        if not send_photo(it, cap, mk):                 # último recurso: só o texto
            tg("sendMessage", {"text": cap, "disable_web_page_preview": "true",
                               **({"reply_markup": mk} if mk else {})}) \
                or tg("sendMessage", {"text": cap})
        time.sleep(0.4)                                 # respeita o limite de envios do Telegram


def notify_admin(text, dry):
    """Avisos do próprio bot (falhas, resumo) vão para o destino masculino (o seu)."""
    CUR_GENDER[0] = "M"
    send({"text": text}, dry)


def tg_call(token, method, params):
    body = urllib.parse.urlencode(params).encode()
    return json.load(urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}",
                                            body, timeout=30))


def process_updates(state):
    """Lê os toques nos botões (silenciar / reativar loja) desde a última volta."""
    muted = state.setdefault("muted", [])
    offs = state.setdefault("tg_offset", {})
    seen = set()
    for g in ("M", "F"):
        CUR_GENDER[0] = g
        token, _ = creds()
        if not token or token in seen:
            continue
        seen.add(token)
        tid = token.split(":")[0]
        try:
            r = tg_call(token, "getUpdates", {"offset": offs.get(tid, 0), "timeout": 0,
                                              "allowed_updates": json.dumps(["callback_query"])})
        except Exception as e:
            print(f"  (getUpdates falhou: {type(e).__name__})")
            continue
        for u in r.get("result", []):
            offs[tid] = u["update_id"] + 1
            cq = u.get("callback_query") or {}
            data, chat = cq.get("data", ""), dig(cq, "message", "chat", "id")
            try:
                if data.startswith("mute:"):
                    k = data[5:]
                    if k not in muted:
                        muted.append(k)
                    tg_call(token, "answerCallbackQuery", {"callback_query_id": cq["id"],
                                                           "text": f"🔕 {k} silenciada"})
                    tg_call(token, "sendMessage", {
                        "chat_id": chat, "text": f"🔕 {k} silenciada. Para voltar a receber:",
                        "reply_markup": json.dumps({"inline_keyboard": [[
                            {"text": f"🔔 Reativar {k}", "callback_data": "unmute:" + k}]]})})
                elif data.startswith("unmute:"):
                    k = data[7:]
                    if k in muted:
                        muted.remove(k)
                    tg_call(token, "answerCallbackQuery", {"callback_query_id": cq["id"],
                                                           "text": f"🔔 {k} reativada"})
                    tg_call(token, "sendMessage", {"chat_id": chat, "text": f"🔔 {k} reativada."})
            except Exception as e:
                print(f"  (botão falhou: {type(e).__name__})")


# ---------------------------------------------------------------- filtros
def load_config():
    try:
        return json.loads((ROOT / "config.json").read_text(encoding="utf8"))
    except Exception:
        return {}


def get_filters(g):
    flt = dict((CONFIG.get("filters") or {}).get("all") or {})
    flt.update((CONFIG.get("filters") or {}).get(g) or {})
    return flt


def basic_ok(i, flt):
    if i["pct"] < (flt.get("min_pct") or 0):
        return False
    if flt.get("max_price") and i["price"] > flt["max_price"] * 100:
        return False
    name = i["name"].lower()
    inc = [w.lower() for w in flt.get("include") or []]
    if inc and not any(w in name for w in inc):
        return False
    return not any(w.lower() in name for w in flt.get("exclude") or [])


def size_ok(i, wanted):
    ok = i.get("sizes")
    if ok is None:                      # tamanho desconhecido: não esconde a peça
        return True
    have = {x.lower() for x in ok}
    return any(w.lower() in have for w in wanted)


def compare(name, title, home, items, cats, prev, hist, skey):
    """Devolve (mensagens, primeira_vez). `hist` guarda o menor preço já visto de cada peça."""
    first = prev is None
    prev = prev or {}
    old_items = prev.get("items", {})
    old_cats = {str(c["id"]) for c in prev.get("categories", [])}
    for k, i in items.items():
        i["k"], i["skey"] = k, skey
    msgs = []
    if not first:
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
        flt = get_filters(CUR_GENDER[0])
        cand = [i for i in novos + subiu if basic_ok(i, flt)]
        wanted = flt.get("sizes") or []
        if wanted:                                     # filtro de tamanho: consulta o estoque antes
            cand = cand[:FILTER_CAP]
            fill_sizes(name, cand)
            cand = [i for i in cand if size_ok(i, wanted)]
        if cand:
            for i in cand:
                i["prev_min"], i["store"] = hist.get(i["k"]), tag
            n_new = sum(1 for i in cand if i["k"] not in old_items)
            msg = f"{tag}\n🛍️ {n_new} peça(s) nova(s) em sale"
            if len(cand) - n_new:
                msg += f", {len(cand) - n_new} com desconto maior"
            shown = cand[:MAX_LINES]
            fill_sizes(name, shown)
            photos = shown[:MAX_PHOTOS]
            sem_foto = shown[MAX_PHOTOS:]
            if sem_foto:
                msg += "\n\n" + "\n".join(fmt(i) for i in sem_foto)
            if len(cand) > len(shown):
                msg += f"\n… e mais {len(cand) - len(shown)}"
            NOVOS_COUNT[0] += len(cand)
            msgs.append({"text": msg, "photos": photos})
    for k, i in items.items():                         # atualiza o menor preço visto
        old = hist.get(k)
        hist[k] = min(old, i["price"]) if old else i["price"]
    return msgs, first


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    dry = "--dry" in sys.argv

    def arg(flag):
        return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else None

    only = (arg("--only") or "").lower() or None
    stores_only = {x.strip().lower() for x in (arg("--stores") or "").split(",") if x.strip()}
    stores_skip = {x.strip().lower() for x in (arg("--skip") or "").split(",") if x.strip()}
    state_path = Path(arg("--state")) if arg("--state") else STATE
    full_run = not (only or stores_only)
    load_env()
    CONFIG.update(load_config())
    state = json.loads(state_path.read_text(encoding="utf8")) if state_path.exists() else {}
    if "items" in state:                                   # formato antigo (só Zara)
        state = {"stores": {"Zara": state}}
    stores = state.setdefault("stores", {})
    HM_CACHE.update(state.get("hm_checked", {}))
    hist_all = state.setdefault("hist", {})
    health = state.setdefault("health", {})
    if not dry:
        process_updates(state)                             # toques em "silenciar/reativar"
    muted = set(state.get("muted", []))
    status = {}                                            # resumo por loja (visível no state.json)
    n_ok = n_err = 0

    for (name, g), fn in STORES.items():
        if only and only not in name.lower():
            continue
        if stores_only and name.lower() not in stores_only:
            continue
        if name.lower() in stores_skip:
            continue
        CUR_GENDER[0] = g
        key, title = state_key(name, g), f"{name} {GENDERS[g]}"
        try:
            t0 = time.time()
            items, cats, home = fn()
            print(f"  ({title}: coletado em {time.time() - t0:.0f}s)", flush=True)
        except Exception as e:                              # uma loja caída não derruba as outras
            err = f"{type(e).__name__}: {str(e)[:100]}"
            print(f"[{title}] ERRO: {err}")
            status[title] = f"ERRO {err[:70]}"
            if os.environ.get("RADAR_DEBUG"):
                traceback.print_exc()
            h = health.setdefault(title, {"fails": 0, "alerted": False})
            h["fails"] += 1
            if h["fails"] == 3 and not h["alerted"]:       # 3 voltas seguidas com erro
                notify_admin(f"⚠️ {title}: erro há 3 voltas seguidas.\n{err}", dry)
                h["alerted"] = True
            n_err += 1
            continue
        h = health.get(title)
        if h and h.get("fails"):
            if h.get("alerted"):
                notify_admin(f"✅ {title} voltou a funcionar.", dry)
            health[title] = {"fails": 0, "alerted": False}
        n_ok += 1
        msgs, first = compare(name, title, home, items, cats, stores.get(key),
                              hist_all.setdefault(key, {}), key)
        if key in muted:
            msgs = []                                       # loja silenciada pelo botão
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

    # resumo diário (uma vez por dia, depois das 9h de Brasília, só em volta completa)
    now = datetime.now(BRT)
    today = now.strftime("%Y-%m-%d")
    daily = state.setdefault("daily", {"novos": 0, "sent": ""})
    daily["novos"] += NOVOS_COUNT[0]
    if full_run and not dry and now.hour >= 9 and daily.get("sent") != today:
        totals = {"Homem": 0, "Mulher": 0}
        for t, v in state["status"].items():
            for gname in totals:
                if t.endswith(gname) and isinstance(v, int):
                    totals[gname] += v
        erros = [t for t, v in state["status"].items() if isinstance(v, str)]
        txt = (f"📊 Resumo {today}: {n_ok} lojas ok, {n_err} com erro nesta volta.\n"
               f"Peças novas avisadas desde o último resumo: {daily['novos']}.\n"
               f"Em sale agora: Homem {totals['Homem']} · Mulher {totals['Mulher']}.")
        if erros:
            txt += "\n⚠️ Com erro: " + ", ".join(erros)
        if state.get("muted"):
            txt += "\n🔕 Silenciadas: " + ", ".join(state["muted"])
        notify_admin(txt, dry)
        daily["sent"], daily["novos"] = today, 0
    if not dry:
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf8")


if __name__ == "__main__":
    main()
