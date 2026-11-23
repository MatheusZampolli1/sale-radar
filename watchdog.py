"""Vigia do Sale Radar: se nenhuma rodada estiver ativa (a corrente quebrou) ou se nenhuma volta
terminar há muito tempo, religa o monitor e avisa no Telegram."""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

TESTE = "--teste-parado" in sys.argv        # finge que não há rodada ativa (não religa nada)


def gh(*a):
    return subprocess.run(["gh", *a], capture_output=True, text=True).stdout


def avisar(texto):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    print(texto)
    if not token or not chat:
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=30)


runs = json.loads(gh("run", "list", "--workflow", "radar.yml", "--limit", "12",
                     "--json", "status,conclusion,updatedAt") or "[]")
now = datetime.now(timezone.utc)
parse = lambda t: datetime.strptime(t, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
active = [r for r in runs if r["status"] in ("in_progress", "pending", "queued", "waiting")]
done = [parse(r["updatedAt"]) for r in runs if r["status"] == "completed" and r["conclusion"] == "success"]
age = (now - max(done)).total_seconds() / 60 if done else None
print(f"rodadas ativas: {len(active)} | última volta concluída há: {age and round(age)} min")

if not active or TESTE:
    if not TESTE:
        gh("workflow", "run", "radar.yml")
    avisar(("🧪 TESTE do vigia — " if TESTE else "") +
           f"⚠️ O monitor estava parado (nenhuma rodada ativa; última volta concluída há "
           f"{round(age) if age is not None else '?'} min)." + ("" if TESTE else " Já religuei."))
elif age is not None and age > 45:
    avisar(f"⚠️ Nenhuma volta do monitor foi concluída há {round(age)} min (rodada travada?).")
else:
    print("ok: monitor vivo")
