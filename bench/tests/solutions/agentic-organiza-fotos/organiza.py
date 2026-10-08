import csv
import os
import re
import shutil

PADROES = [
    re.compile(r"^(?:IMG|PXL)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),
    re.compile(r"^WhatsApp Image (\d{4})-(\d{2})-(\d{2}) at (\d{2})\.(\d{2})\.(\d{2})"),
]
linhas = []
for nome in sorted(os.listdir("fotos")):
    if not nome.lower().endswith((".jpg", ".jpeg")):
        continue
    m = next((p.match(nome) for p in PADROES if p.match(nome)), None)
    if not m:
        continue
    a, mes, d, h, mi, s = m.groups()
    pasta = os.path.join("organizadas", a, mes)
    os.makedirs(pasta, exist_ok=True)
    base = f"{a}-{mes}-{d}_{h}-{mi}-{s}"
    destino, n = os.path.join(pasta, base + ".jpg"), 2
    while os.path.exists(destino):
        destino, n = os.path.join(pasta, f"{base}_{n}.jpg"), n + 1
    shutil.move(os.path.join("fotos", nome), destino)
    linhas.append((os.path.join("fotos", nome), destino))
with open("indice.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["original", "novo"])
    w.writerows(linhas)
