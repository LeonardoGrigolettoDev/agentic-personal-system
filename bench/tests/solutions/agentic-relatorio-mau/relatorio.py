import csv
import glob
import json

usuarios = {}
for arquivo in sorted(glob.glob("eventos-*.csv")):
    with open(arquivo, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["user_id"].startswith("nitro-"):
                continue
            usuarios.setdefault(r["timestamp"][:7], set()).add(r["user_id"])
out, anterior = {}, None
for mes in sorted(usuarios):
    if mes > "2026-09":
        continue
    mau = len(usuarios[mes])
    out[mes] = {"mau": mau, "crescimento_pct": None if anterior is None else round((mau - anterior) / anterior * 100, 1)}
    anterior = mau
with open("relatorio.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
