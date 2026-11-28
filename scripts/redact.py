"""Expurge les journaux avant artefact : remplace adresse, nom d'hôte, jeton, clé et /exec par <masqué>.

Usage : python3 scripts/redact.py <journal source> <journal expurgé> <dossier à expurger>
Les valeurs viennent de l'environnement (secrets E2E_*), rien n'est affiché.
"""
import os
import sys
import urllib.parse


def values():
    base = os.environ.get("E2E_BASE_URL", "").strip().rstrip("/")
    exec_url = os.environ.get("E2E_EXEC_URL", "").strip()
    vals = [base, os.environ.get("E2E_VERCEL_SHARE", "").strip(), os.environ.get("E2E_GUEST_K", "").strip(), exec_url,
            urllib.parse.urlparse(base).hostname or ""]
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
    return text


def main():
    src, dst, folder = sys.argv[1:4]
    vals = values()
    if os.path.exists(src):
        with open(src, encoding="utf-8", errors="replace") as f:
            data = f.read()
        with open(dst, "w", encoding="utf-8") as f:
            f.write(red(data, vals))
    for root, _, files in os.walk(folder):
        for name in files:
            p = os.path.join(root, name)
            if name.lower().endswith((".json", ".txt", ".log", ".xml")):
                with open(p, encoding="utf-8", errors="replace") as f:
                    data = f.read()
                clean = red(data, vals)
                if clean != data:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(clean)
            elif not name.lower().endswith(".png"):
                os.remove(p)  # rien d'autre ne part en artefact (pas de vidéo : elle montrerait la barre d'adresse)


if __name__ == "__main__":
    main()
