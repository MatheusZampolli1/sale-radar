import subprocess, urllib.request
U = "https://www.bershka.com/itxrest/2/catalog/store/45109565/40259535/category?languageId=-300&appId=1"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
HX = {"Referer": "https://www.bershka.com/br/h-man.html", "Origin": "https://www.bershka.com"}
BR = {"sec-ch-ua": '"Chromium";v="130", "Google Chrome";v="130"', "sec-ch-ua-mobile": "?0",
      "sec-ch-ua-platform": '"Windows"', "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
      "Sec-Fetch-Site": "same-origin", "Accept": "application/json, text/plain, */*",
      "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}
def py(name, h):
    try:
        r = urllib.request.urlopen(urllib.request.Request(U, headers=h), timeout=30)
        print(name, r.status)
    except Exception as e:
        print(name, str(e)[:40])
py("py-basico", {"User-Agent": UA, **HX})
py("py-navegador", {"User-Agent": UA, **HX, **BR})
def cu(name, extra):
    r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "30", "-A", UA, *extra, U],
                       capture_output=True, text=True)
    print(name, r.stdout)
hh = [x for k, v in {**HX, **BR}.items() for x in ("-H", f"{k}: {v}")]
cu("curl-basico", ["-H", "Referer: https://www.bershka.com/br/h-man.html"])
cu("curl-navegador", hh)
cu("curl-http2", ["--http2", *hh])
cu("home-html", [])
