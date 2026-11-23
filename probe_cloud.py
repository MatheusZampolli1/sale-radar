import sale_radar as s, re, time

def t(name, fn):
    a = time.time()
    try:
        r = fn()
        print(f"[{name}] OK ({time.time()-a:.1f}s) {r}")
    except Exception as e:
        print(f"[{name}] ERRO {type(e).__name__}: {str(e)[:100]}")

def hm_page():
    html = s.get_text("https://www.hm.com.br/oculos-de-sol-1255791001/p")
    m = s.HM_OFFER.search(html)
    return f"{len(html)} bytes | preco publico: {m.group(1) if m else 'NAO ACHOU'}"

def hm_api():
    return len(s.get_json(f"{s.HM}/api/catalog_system/pub/products/search?fq=C:/{s.HM_CAT['M']}/&_from=0&_to=9"))

t("H&M pagina do produto", hm_page)
t("H&M API", hm_api)
t("Zara Mulher", lambda: len(s.zara("F")[0]))
t("Renner Mulher", lambda: len(s.renner("F")[0]))
t("Riachuelo Mulher", lambda: len(s.riachuelo("F")[0]))
t("C&A Mulher", lambda: len(s.cea("F")[0]))
t("Dust", lambda: len(s.dust("M")[0]))
t("Monteleste", lambda: len(s.magazord(s.MONTELESTE, "M")[0]))
