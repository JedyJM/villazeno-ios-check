"""Expurgation : adresse, nom d'hôte, jeton, clé et /exec remplacés par <masqué>.

Deux couches : les valeurs exactes des secrets E2E_* (lues dans l'environnement,
jamais affichées) ET des motifs génériques (toute URL Apps Script, tout k=,
_vercel_share=, _vercel_jwt=, tout hôte *.vercel.app), pour ne pas dépendre
des valeurs exactes.

Usages :
  python3 scripts/redact.py --stream            filtre stdin → stdout (journal du job)
  python3 scripts/redact.py <src> <dst> <dossier>  copie expurgée d'un journal + dossier expurgé
"""
import os
import re
import sys
import urllib.parse

PATTERNS = [
    (re.compile(r"https?://script\.google(?:usercontent)?\.com/\S*"), "<masqué:apps-script>"),
    (re.compile(r"AKfycb[0-9A-Za-z_\-]+"), "<masqué:exec>"),
    (re.compile(r"macros/s/[0-9A-Za-z_\-]+"), "macros/s/<masqué>"),
    (re.compile(r"_vercel_share=[^&\s\"']*"), "_vercel_share=<masqué>"),
    (re.compile(r"_vercel_jwt=[^;&\s\"']*"), "_vercel_jwt=<masqué>"),
    (re.compile(r"([?&])k=[^&\s\"']*"), r"\1k=<masqué>"),
    (re.compile(r"[0-9A-Za-z\-\.]+\.vercel\.app"), "<masqué:vercel-app>"),
    (re.compile(r"(stayKey['\"]?\s*[,:=]\s*['\"])[^'\"]+"), r"\1<masqué>"),
]


def values():
    base = os.environ.get("E2E_BASE_URL", "").strip().rstrip("/")
    exec_url = os.environ.get("E2E_EXEC_URL", "").strip()
    vals = [base, os.environ.get("E2E_VERCEL_SHARE", "").strip(), os.environ.get("E2E_GUEST_K", "").strip(), exec_url,
            os.environ.get("E2E_ART_PASS", "").strip(), urllib.parse.urlparse(base).hostname or ""]
    if "/s/" in exec_url:
        vals.append(exec_url.split("/s/")[1].split("/")[0])
    out = set()
    for v in vals:
        if v and len(v) >= 6:
            out |= {v, urllib.parse.quote(v, safe=""), urllib.parse.quote(v)}
    return sorted(out, key=len, reverse=True)


def red(text, vals):
    for v in vals:
        text = text.replace(v, "<masqué>")
    for rx, rep in PATTERNS:
        text = rx.sub(rep, text)
    return text


def stream():
    vals = values()
    for line in sys.stdin:
        if line.startswith("::"):  # aucune commande de workflow ne passe (add-mask, set-output…)
            continue
        sys.stdout.write(red(line, vals))
        sys.stdout.flush()


def files(src, dst, folder):
    vals = values()
    if os.path.exists(src):
        with open(src, encoding="utf-8", errors="replace") as f:
            data = f.read()
        with open(dst, "w", encoding="utf-8") as f:
            f.write(red(data, vals))
    for root, _, names in os.walk(folder):
        for name in names:
            p = os.path.join(root, name)
            if name.lower().endswith((".json", ".txt", ".log", ".xml")):
                with open(p, encoding="utf-8", errors="replace") as f:
                    data = f.read()
                clean = red(data, vals)
                if clean != data:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(clean)
            elif not name.lower().endswith(".png"):
                os.remove(p)  # ni vidéo ni autre fichier


if __name__ == "__main__":
    if sys.argv[1:2] == ["--stream"]:
        stream()
    else:
        files(*sys.argv[1:4])
