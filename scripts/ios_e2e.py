"""Parcours de bout en bout (niveau 2) dans le vrai Safari d'iOS, contre le STAGING.

Reprend les parcours de tests/e2e/staging.e2e.test.js du site (G invité,
A admin, S sécurité), adaptés au vrai Safari : vrais touchers (nativeWebTap),
vrai clavier iOS pour les champs texte, captures par étape.

Toutes les valeurs sensibles viennent de l'environnement (secrets GitHub
Actions), jamais d'un fichier du dépôt ni d'une entrée du workflow :
  E2E_BASE_URL      aperçu Vercel branché sur le /exec de staging
  E2E_VERCEL_SHARE  jeton de partage Vercel (aperçu protégé)
  E2E_GUEST_K       clé du séjour de démonstration FAMDEMO2812
  E2E_EXEC_URL      /exec de staging (contrôles côté serveur uniquement)
  E2E_ADMIN_TOKEN   « test » (mode test du script de staging)

Règles :
  - aucune valeur sensible n'est affichée, écrite ou capturée : tout texte
    sortant passe par red(), le nom d'hôte est masqué dans les journaux
    (::add-mask::) et la barre d'adresse de Safari est noircie sur chaque
    capture ; on n'ouvre jamais d'adresse contenant ?k= (la clé est posée
    dans le localStorage, comme le fait la page elle-même) ;
  - au plus MAX_SENDS demandes envoyées par appareil (quota du staging :
    30 demandes / heure / séjour) ; tout le reste est lu sans envoi ;
  - avant tout envoi, la page doit avoir choisi le /exec de STAGING
    (VZApi.env == "staging") : sinon rien n'est envoyé ;
  - aucun WhatsApp : window.open est neutralisé avant chaque envoi.
"""
import http.client
import json
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy

BASE = os.environ.get("E2E_BASE_URL", "").strip().rstrip("/")
SHARE = os.environ.get("E2E_VERCEL_SHARE", "").strip()
GUEST_K = os.environ.get("E2E_GUEST_K", "").strip()
EXEC = os.environ.get("E2E_EXEC_URL", "").strip()
ADMIN_TOKEN = os.environ.get("E2E_ADMIN_TOKEN", "").strip()
OUT = os.environ.get("OUT", "results")
LABEL = os.environ.get("SIZE", "device")
DEMO = "FAMDEMO2812"
MAX_SENDS = 4
MAXS = [MAX_SENDS]  # passe à 0 si le séjour démo n'est pas propre
# E2E_ONLY (non secret, posé par le workflow) : "A1,A2,A3" = P0 puis ces tests seulement
# (aucune remise à zéro du séjour démo, aucun envoi invité). Vide = suite complète.
ONLY = [x.strip().upper() for x in os.environ.get("E2E_ONLY", "").split(",") if x.strip()]
AUD_IDS = ("AUD1", "AUD2", "AUD4", "AUD5", "AUD6", "AUD8")
DES_IDS = ("DES1", "DES2", "DES3", "DES4", "DES5")  # nouveau design : La Villa, Wi-Fi, Loisirs, Nos adresses, brochure
ONLY_OK = ("A1", "A2", "A3") + AUD_IDS + DES_IDS
if "AUD" in ONLY:  # « AUD » = tous les tests de l'audit du 28/11
    ONLY = [x for x in ONLY if x != "AUD"] + list(AUD_IDS)
if "DES" in ONLY:  # « DES » = tous les tests du nouveau design
    ONLY = [x for x in ONLY if x != "DES"] + list(DES_IDS)
# Mode audit : invité seulement, aucun appel à l'API admin (ADMIN_TEST_MODE coupé sur staging)
NO_ADMIN = bool(ONLY) and all(x.startswith(("AUD", "DES")) for x in ONLY)
os.makedirs(OUT, exist_ok=True)

HOST = urllib.parse.urlparse(BASE).hostname or ""
JWT = ""  # cookie d'accès Vercel (rempli plus bas, jamais affiché)

# Dates dans le séjour de démonstration (1–15 déc. 2028), différentes par
# appareil : les deux iPhone tournent en parallèle sur le même séjour, la
# date sert à retrouver SA demande côté serveur.
OFFS = {"big": {"V": 13, "G4": 2, "G1": 3, "G10": 10},
        "small": {"V": 14, "G4": 6, "G1": 5, "G10": 11}}.get(LABEL, {"V": 13, "G4": 2, "G1": 3, "G10": 10})


def D(n):
    return f"2028-12-{n:02d}"


# ---------------------------------------------------------------- secrets
def _secret_values():
    vals = [BASE, SHARE, GUEST_K, EXEC, HOST, JWT]
    if EXEC:
        parts = EXEC.split("/s/")
        if len(parts) > 1:
            vals.append(parts[1].split("/")[0])
    out = []
    for v in vals:
        if v and len(v) >= 6:
            out += [v, urllib.parse.quote(v, safe=""), urllib.parse.quote(v)]
    return sorted(set(out), key=len, reverse=True)


def red(s):
    s = "" if s is None else str(s)
    for v in _secret_values():
        s = s.replace(v, "<masqué>")
    return s


def mask_in_logs(*vals):
    # Volontairement vide : aucune commande ::add-mask:: n'est émise (elle
    # contiendrait la valeur). Tout le stdout passe par red() puis par
    # scripts/redact.py --stream dans le workflow ; GitHub masque en plus
    # les valeurs exactes des secrets.
    return None


def log(*a):
    print(red(" ".join(str(x) for x in a)), flush=True)


# ---------------------------------------------------------------- résultats
results = {"device": os.environ.get("DEVICE_NAME", "?"), "ios": os.environ.get("IOS_VERSION", "?"),
           "size": LABEL, "submitted": 0, "session": False, "tests": {}, "notes": []}
CUR = {"id": None}


def save():
    with open(f"{OUT}/{LABEL}-results.json", "w") as f:
        f.write(red(json.dumps(results, ensure_ascii=False, indent=1)))


def note(msg):
    results["notes"].append(red(msg))
    log("NOTE", msg)


def check(name, ok, detail=""):
    t = results["tests"][CUR["id"]]
    t["checks"].append({"check": name, "ok": bool(ok), "detail": red(detail)})
    log("OK   " if ok else "ÉCHEC", CUR["id"], "-", name, detail)
    if not ok:
        raise AssertionError(f"{name} {detail}".strip())


class Skip(Exception):
    pass


class Blocked(Exception):
    """Préalable non prouvé (ex. barre Vercel présente) : le test ne touche à rien."""


def run(tid, title, fn, cause_hint="à analyser"):
    CUR["id"] = tid
    t = results["tests"][tid] = {"group": tid[0], "title": title, "status": "?", "checks": [], "shots": [], "cause": ""}
    t0 = time.time()
    log(f"\n=== {tid} {title}")
    try:
        fn()
        t["status"] = "pass"
    except Skip as e:
        t["status"] = "skip"
        t["cause"] = red(e)
    except Blocked as e:
        t["status"] = "blocked"
        t["cause"] = "BLOQUÉ : " + red(e)[:400]
        safe_shot(f"{tid}-bloque")
    except AssertionError as e:
        t["status"] = "fail"
        t["cause"] = red(e)[:400]
        safe_shot(f"{tid}-echec")
    except Exception as e:  # noqa: BLE001
        t["status"] = "fail"
        t["cause"] = red(f"{type(e).__name__}: {e}")[:400] + f" [{cause_hint}]"
        t["trace"] = red(traceback.format_exc()[-1200:])
        safe_shot(f"{tid}-echec")
    t["secondes"] = round(time.time() - t0)
    log(f"--- {tid} : {t['status']} {t['cause']}")
    save()


# ---------------------------------------------------------------- serveur (depuis le runner)
def vercel_cookie():
    """Jeton de partage → cookie _vercel_jwt (sans suivre la redirection)."""
    u = urllib.parse.urlparse(BASE)
    c = http.client.HTTPSConnection(u.hostname, timeout=30)
    c.request("GET", "/?_vercel_share=" + urllib.parse.quote(SHARE, safe=""))
    r = c.getresponse()
    r.read()
    for k, v in r.getheaders():
        if k.lower() == "set-cookie" and v.startswith("_vercel_jwt="):
            return v.split(";", 1)[0].split("=", 1)[1], r.status
    return "", r.status


def base_get(path):
    u = urllib.parse.urlparse(BASE)
    c = http.client.HTTPSConnection(u.hostname, timeout=30)
    c.request("GET", path, headers={"Cookie": f"_vercel_jwt={JWT}", "Cache-Control": "no-store"})
    r = c.getresponse()
    return r.status, r.read().decode("utf-8", "replace")


def exec_get(params, token=None):
    q = dict(params)
    q.setdefault("token", ADMIN_TOKEN if token is None else token)
    if token is None:
        q.setdefault("test", "1")
    url = EXEC + "?" + urllib.parse.urlencode(q)
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise RuntimeError(f"API admin staging injoignable ({type(last).__name__}) [backend]")


def exec_post(body):
    data = json.dumps(dict({"token": ADMIN_TOKEN, "test": "1"}, **body)).encode("utf-8")
    req = urllib.request.Request(EXEC, data=data, headers={"Content-Type": "text/plain;charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=90) as r:  # 302 Apps Script → GET suivi par urllib
        return json.loads(r.read().decode("utf-8"))


def demo_stay():
    return next((x for x in (exec_get({"action": "stays"}).get("stays") or []) if x.get("id") == DEMO), None)


def demandes():
    r = exec_get({"action": "demandes"})
    if not isinstance(r, dict) or "items" not in r:
        raise RuntimeError(f"API admin : réponse inattendue ({str(r)[:120]}) [backend]")
    return r["items"]


def new_item(before_ids, service, date):
    for _ in range(10):
        for it in demandes():
            if it.get("id") not in before_ids and it.get("stay") == DEMO and it.get("service") == service and it.get("date") == date:
                return it
        time.sleep(3)
    return None


def item_by_id(iid):
    return next((x for x in demandes() if x.get("id") == iid), None)


# ---------------------------------------------------------------- Safari
d = None


def web():
    for _ in range(10):
        for c in d.contexts:
            if c.startswith("WEBVIEW"):
                d.switch_to.context(c)
                return
        time.sleep(1)
    raise RuntimeError("pas de contexte web Safari [rig]")


def native():
    d.switch_to.context("NATIVE_APP")


def js(script, *args):
    return d.execute_script(script, *args)


def wait_js(expr, timeout=30, what="", *args):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            if js("return !!(" + expr + ");", *args):
                return True
        except Exception as e:  # noqa: BLE001  (page en cours de chargement)
            last = e
        time.sleep(0.7)
    raise AssertionError(f"délai dépassé ({timeout}s) : {what or expr[:80]}" + (f" ({type(last).__name__})" if last else ""))


def go(path):
    """Navigation sur l'aperçu (chemin relatif, jamais de ?k= ni de jeton)."""
    assert "k=" not in path and "_vercel_share" not in path
    web()
    d.get(BASE + path)
    time.sleep(1.5)
    on_vercel_login()
    hide_toolbar()


# Barre d'outils Vercel (injectée par Vercel sur les aperçus, absente en production) :
# son script ne la monte pas si sessionStorage["vercel-live-feedback-optout"] == "1"
# (option « Disable for session » de Vercel), ni ne la rattache. La clé vaut pour
# l'onglet et l'origine, donc pour toute la session. En plus, à chaque page : retrait
# de tout élément déjà monté et style qui le masque. Rien n'est changé dans l'app.
TOOLBAR_SEL = "vercel-live-feedback, #vercel-live-feedback, [data-vercel-toolbar]"


def hide_toolbar():
    js("try{sessionStorage.setItem('vercel-live-feedback-optout','1');}catch(e){}"
       "document.querySelectorAll(arguments[0]).forEach(function(e){e.remove();});"
       "if(!document.getElementById('e2e-no-toolbar') && document.head){var s=document.createElement('style'); s.id='e2e-no-toolbar';"
       "s.textContent='vercel-live-feedback{display:none!important;pointer-events:none!important}'; document.head.appendChild(s);}", TOOLBAR_SEL)


def toolbar_state():
    return js("return {barre: !!document.querySelector(arguments[0]), optout: sessionStorage.getItem('vercel-live-feedback-optout')};", TOOLBAR_SEL)


def on_vercel_login():
    host = js("return location.hostname;")
    if host != HOST:
        raise AssertionError("page hors de l'aperçu (connexion Vercel ?) : jeton de partage expiré ou refusé [SSO]")


def els(sel):
    return d.find_elements(AppiumBy.CSS_SELECTOR, sel)


def visible(sel):
    for e in els(sel):
        try:
            if js("const r=arguments[0].getBoundingClientRect(); return r.width>0 && r.height>0 && getComputedStyle(arguments[0]).visibility!=='hidden';", e):
                return e
        except Exception:  # noqa: BLE001
            pass
    return None


def tap(sel, timeout=15, guard=False):
    """Vrai toucher (nativeWebTap) sur le premier élément visible.
    guard=True : avant de toucher, l'élément au point touché doit être la cible
    (rien par-dessus, ex. la barre Vercel) ; sinon BLOQUÉ, aucun toucher."""
    # La page peut se redessiner (réponse fraîche après le cache) entre la recherche
    # et le toucher : sur élément périmé, on recherche l'élément et on recommence
    # (3 essais), sans jamais toucher un élément périmé.
    for attempt in range(3):
        end = time.time() + timeout
        e = None
        while time.time() < end and e is None:
            e = visible(sel)
            if e is None:
                time.sleep(0.5)
        if e is None:
            raise AssertionError(f"élément introuvable : {sel}")
        try:
            js("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", e)
            time.sleep(0.4)
            if guard:
                hit = js("var el=arguments[0], r=el.getBoundingClientRect(), h=document.elementFromPoint(r.left+r.width/2, r.top+r.height/2);"
                         "return h ? ((h===el || el.contains(h)) ? '' : (h.tagName||'').toLowerCase()) : 'rien';", e)
                if hit:
                    raise Blocked(f"{sel} recouvert par <{hit}> au point de toucher : aucun toucher")
            e.click()
        except Exception as ex:  # noqa: BLE001
            if type(ex).__name__ != "StaleElementReferenceException" or attempt == 2:
                raise
            log(f"élément périmé ({sel}), nouvelle recherche")
            time.sleep(1)
            continue
        time.sleep(0.6)
        return e


def js_click(sel):
    """Pour les cases radio masquées (stylées par leur étiquette) : clic DOM."""
    ok = js("const e=document.querySelector(arguments[0]); if(!e) return false; e.click(); return true;", sel)
    if not ok:
        raise AssertionError(f"élément introuvable : {sel}")


def set_field(sel, value):
    """Champs date / heure / select : sélecteurs natifs d'iOS (roues) non pilotables, valeur posée comme le test Playwright."""
    ok = js("const e=document.querySelector(arguments[0]); if(!e) return false; e.value=arguments[1];"
            "e.dispatchEvent(new Event('input',{bubbles:true})); e.dispatchEvent(new Event('change',{bubbles:true})); return true;", sel, value)
    if not ok:
        raise AssertionError(f"champ introuvable : {sel}")


def keyboard_shown():
    try:
        return bool(d.is_keyboard_shown())
    except Exception:  # noqa: BLE001
        native()
        found = d.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeKeyboard")
        web()
        return bool(found)


def type_real(sel, text):
    """Toucher le champ puis taper au vrai clavier iOS."""
    js("const e=document.querySelector(arguments[0]); if(e){e.value=''; e.dispatchEvent(new Event('input',{bubbles:true}));}", sel)
    tap(sel)
    time.sleep(1.2)
    native()
    try:
        f = d.find_elements(AppiumBy.IOS_PREDICATE, "hasKeyboardFocus == 1")
        target = f[0] if f else (d.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeTextField") + d.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeTextView"))[0]
        target.send_keys(text)
    finally:
        web()
    time.sleep(0.5)
    got = js("const e=document.querySelector(arguments[0]); return e ? e.value : null;", sel)
    if got != text:
        # repli : la frappe réelle n'a pas abouti, on pose la valeur et on le note
        note(f"{CUR['id']} : frappe iOS incomplète dans {sel} (lu {got!r}), valeur posée en JS [harnais]")
        set_field(sel, text)


def blur_active():
    js("if(document.activeElement && document.activeElement.blur) document.activeElement.blur();")
    time.sleep(0.8)


def stay_ready(timeout=40):
    wait_js("window.GuestStay && window.GuestStay.snapshot().state === 'ok'", timeout, "séjour chargé (GuestStay ok)")


def staging_guard():
    """La page doit parler au /exec de STAGING avant toute action réseau voulue."""
    r = d.execute_async_script(
        "const done=arguments[arguments.length-1];"
        "if(!window.VZApi){done({ok:false,why:'VZApi absent'});return;}"
        "const u=VZApi.urlReady; const p = typeof u==='function' ? u() : (u && typeof u.then==='function' ? u : Promise.resolve(VZApi.URL));"
        "const t0=Date.now(); const fin=()=>done({ok:true, env:VZApi.env, prod: VZApi.URL===VZApi.PROD_URL, same: VZApi.URL===arguments[0]});"
        "const poll=()=>{ if(VZApi.env==='staging' || Date.now()-t0>25000) fin(); else setTimeout(poll,500); };"
        "p.then(poll, poll);", EXEC)
    if not (r and r.get("ok") and r.get("env") == "staging" and not r.get("prod") and r.get("same")):
        raise AssertionError(f"la page n'a pas choisi le /exec de staging ({red(r)}) : rien n'est envoyé [app : repli vers la production]")


def no_wa():
    js("window.__waOpened=[]; window.open=function(u){ window.__waOpened.push(String(u)); return {closed:false, focus:function(){}}; };")


def h_overflow():
    return js("return document.documentElement.scrollWidth - window.innerWidth;")


def scale():
    return js("return window.visualViewport ? window.visualViewport.scale : 1;")


# ---------------------------------------------------------------- captures
_bar_cache = {}


def bar_rects():
    """Rectangles (en points) de la barre d'adresse de Safari, à noircir."""
    native()
    try:
        key = HOST[:12]
        found = d.find_elements(AppiumBy.IOS_PREDICATE, f"label CONTAINS[c] '{key}' OR value CONTAINS[c] '{key}'") if key else []
        if not found:
            found = d.find_elements(AppiumBy.IOS_PREDICATE,
                                    "name IN {'URL','Address','TabBarItemTitle','UnifiedTabBarItemView'} OR "
                                    "(type == 'XCUIElementTypeTextField' AND (name CONTAINS[c] 'url' OR name CONTAINS[c] 'address'))")
        size = d.get_window_size()
        # un élément qui couvrirait l'écran n'est pas la barre d'adresse
        rects = [r for r in (e.rect for e in found) if 0 < r["height"] <= size["height"] * 0.2]
    finally:
        web()
    return rects, size


def mask_text_in_page():
    """Noircit dans la page tout texte contenant le nom d'hôte ou une clé (fiche séjour admin)."""
    js("const h=arguments[0]; const w=document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;"
       "while((n=w.nextNode())){ const t=n.data||''; if((h && t.indexOf(h)>=0) || /[?&]k=/.test(t) || /script\\.google(usercontent)?\\.com|macros\\/s\\//.test(t)){"
       " const p=n.parentElement; if(p){ p.style.background='#000'; p.style.color='#000'; } } }", HOST)


def shot(name):
    from PIL import Image, ImageDraw  # installé par le workflow
    mask_text_in_page()
    path = f"{OUT}/{LABEL}-{name}.png"
    d.get_screenshot_as_file(path)
    rects, size = bar_rects()
    img = Image.open(path)
    sx = img.width / max(1, size["width"])
    sy = img.height / max(1, size["height"])
    dr = ImageDraw.Draw(img)
    for r in rects:
        dr.rectangle([(r["x"] - 4) * sx, (r["y"] - 4) * sy, (r["x"] + r["width"] + 4) * sx, (r["y"] + r["height"] + 4) * sy], fill="black")
    img.save(path)
    if not rects:
        note(f"{name} : barre d'adresse non trouvée, capture NON publiée (risque d'afficher le nom d'hôte)")
        os.remove(path)
        return None
    if CUR["id"] in results["tests"]:
        results["tests"][CUR["id"]]["shots"].append(os.path.basename(path))
    return path


def safe_shot(name):
    try:
        web()
        shot(name)
    except Exception as e:  # noqa: BLE001
        log("capture impossible", name, type(e).__name__)


# ---------------------------------------------------------------- envois (quota)
def can_send():
    if results["submitted"] >= MAXS[0]:
        raise Skip(f"quota du harnais atteint ({MAX_SENDS} envois par appareil)")


def wait_sent():
    wait_js("document.querySelector('[data-send-status].is-ok, [data-send-status].is-error')", 60, "« Demande reçue » ou message d'erreur")
    err = visible("[data-send-status].is-error")
    if err is not None:
        msg = js("return arguments[0].innerText;", err)
        raise AssertionError(f"envoi refusé, écran : {msg!r} [backend : quota rate_limited ?]" if "heure" in msg or "limit" in msg.lower() else f"envoi refusé, écran : {msg!r}")


def open_guest(page):
    go("/guest/" + page)
    stay_ready()
    staging_guard()


SENT = {}


def chain(tid, page, service, date, fill, submit_sel, total=None, trip=None):
    """Étapes 1–4 de TESTS-E2E pour une demande : formulaire → envoi → « Demande reçue » → ligne Demandes."""
    can_send()
    open_guest(page)
    no_wa()
    before = {x["id"] for x in demandes()}
    fill()
    shot(f"{tid}-01-formulaire")
    staging_guard()
    no_wa()
    results["submitted"] += 1
    save()
    tap(submit_sel)
    extra = getattr(fill, "after_submit", None)
    if extra:
        extra()
    wait_sent()
    blur_active()
    shot(f"{tid}-02-recue")
    it = new_item(before, service, date)
    check("nouvelle ligne dans Demandes (API admin staging)", it is not None, f"(service {service}, date {date})")
    check("nom réel de l'invité", it.get("guest") == "Famille Démo", f"({it.get('guest')!r})")
    check("statut Envoyée", it.get("status") == "Envoyée", f"({it.get('status')!r})")
    if total is not None:
        check("montant", str(it.get("total")) not in ("", "None") and float(it.get("total")) == total, f"({it.get('total')!r}, attendu {total})")
    if trip:
        check("type de trajet", it.get("trip") == trip, f"({it.get('trip')!r})")
    wa = js("return (window.__waOpened||[]).length;")
    SENT[tid] = {"id": it["id"], "service": service, "date": date, "wa": wa}
    return it


# ---------------------------------------------------------------- remise à zéro du séjour démo
CLEAR_JS = """
const done = arguments[arguments.length-1], id = arguments[0];
(async () => {
  await new Promise((ok) => window.AdminStays.load(() => ok(), true));
  const s = window.AdminStays.find(id);
  if (!s) return "missing";
  if (!s.encaisse_le) return "clean";
  const copy = Object.assign({}, s, { encaisse_total_eur: "", encaisse_mode: "", encaisse_le: "", offerts: "" });
  const r = await window.AdminStays.post({ action: "save-stay", test: window.AdminStays.testFlag(), token: window.AdminShell.session(), stay: window.AdminStays.sheet(copy) });
  return r && r.ok ? "cleared" : "failed:" + (r && r.kind);
})().then(done, (e) => done("error:" + e));
"""


def t_reset():
    """iPhone big seulement : même remise à zéro que resetDemo() du harnais Playwright du site
    (anciennes demandes du séjour → « Annulée » par l'API admin, Encaissement effacé par la page admin).
    Aucune demande créée. Le small attend que ce soit fait (sinon il annulerait les envois du big)."""
    items = [x for x in demandes() if x.get("stay") == DEMO and x.get("status") != "Annulée"]
    for it in items:
        r = exec_post({"action": "set-demande", "id": it["id"], "statut": "Annulée"})
        check(f"ancienne demande annulée", bool(r and r.get("ok")), f"({str(r)[:80]})")
    admin_login()
    go("/admin/sejours.html")
    staging_guard()
    wait_js("window.AdminStays && window.AdminShell", 45, "pages admin prêtes")
    d.set_script_timeout(120)
    unpaid = d.execute_async_script(CLEAR_JS, DEMO)
    d.set_script_timeout(60)
    check("Encaissement du séjour démo effacé", unpaid in ("clean", "cleared"), f"({unpaid})")
    left = [x for x in demandes() if x.get("stay") == DEMO and x.get("status") != "Annulée"]
    s = demo_stay()
    check("séjour démo propre (API)", not left and s and not s.get("encaisse_le"), f"({len(left)} demande(s) non annulée(s), encaissé {bool(s and s.get('encaisse_le'))})")
    note(f"remise à zéro : {len(items)} ancienne(s) demande(s) annulée(s), Encaissement {unpaid}")


def t_wait_reset():
    """iPhone small : attend la remise à zéro faite par le big (plus aucune « Terminée », plus d'encaissement)."""
    end = time.time() + 25 * 60
    while time.time() < end:
        term = [x for x in demandes() if x.get("stay") == DEMO and x.get("status") == "Terminée"]
        s = demo_stay()
        if not term and s and not s.get("encaisse_le"):
            note("remise à zéro du big constatée")
            return
        time.sleep(20)
    raise AssertionError("le séjour démo n'a pas été remis à zéro par l'iPhone big en 25 min : aucun envoi [harnais/rig]")


# ---------------------------------------------------------------- parcours invité
def t_setup():
    global JWT
    if not (BASE and SHARE and GUEST_K and EXEC and ADMIN_TOKEN):
        raise AssertionError("secret E2E_* manquant [harnais/secrets]")
    mask_in_logs(BASE, HOST, SHARE, GUEST_K, EXEC)
    JWT, status = vercel_cookie()
    mask_in_logs(JWT)
    check("jeton de partage Vercel → cookie d'accès (runner)", bool(JWT), f"(HTTP {status}) [SSO : jeton expiré ?]")
    st, body = base_get("/api/vz-env")
    env = {}
    try:
        env = json.loads(body)
    except Exception:  # noqa: BLE001
        pass
    check("/api/vz-env répond « staging » (runner)", st == 200 and env.get("env") == "staging", f"(HTTP {st}, env {env.get('env')!r})")
    check("l'aperçu est branché sur le même /exec de staging que les contrôles", env.get("execUrl") == EXEC)
    if NO_ADMIN:
        note("mode audit : aucun appel à l'API admin ; séjour démo vérifié par la page invitée")
    else:
        r = exec_get({"action": "stays"})
        s = next((x for x in (r.get("stays") or []) if x.get("id") == DEMO), None)
        check("séjour de démonstration présent dans le staging", s is not None)
        check("clé du séjour de démonstration à jour", s.get("k") in (None, "", GUEST_K), "(E2E_GUEST_K différente de celle du staging)")
    # Safari : cookie d'accès par le lien de partage, puis /api/vz-env chauffé.
    web()
    d.get(BASE + "/?_vercel_share=" + urllib.parse.quote(SHARE, safe=""))
    time.sleep(3)
    cur = d.current_url or ""
    if "_vercel_share" in cur:
        note("après le lien de partage, l'adresse contient encore le jeton : navigation immédiate ailleurs, aucune capture prise")
    # Contrôle par fetch depuis une page de l'aperçu : la réponse JSON brute (qui contient le /exec)
    # n'est jamais affichée à l'écran, donc jamais capturée.
    go("/guest/acces.html")  # go() pose la clé « barre Vercel désactivée » pour la session
    env2 = d.execute_async_script(
        "const done=arguments[arguments.length-1];"
        "fetch('/api/vz-env',{cache:'no-store'}).then(r=>r.json()).then(j=>done({env:j.env, same:j.execUrl===arguments[0]}), e=>done({err:String(e)}));", EXEC) or {}
    check("Safari : /api/vz-env chauffé et « staging »", env2.get("env") == "staging" and env2.get("same"), f"({red(env2)})")
    # Le séjour : posé dans le localStorage comme le fait la page avec ?stay=&k= (la clé n'apparaît jamais dans l'adresse).
    go("/guest/acces.html")
    js("localStorage.setItem('villazeno.stay', arguments[0]); localStorage.setItem('villazeno.stayKey', arguments[1]);", DEMO, GUEST_K)
    prod = js("return window.VZApi ? VZApi.PROD_URL : '';")
    check("le /exec des contrôles n'est PAS celui de la production", prod and prod != EXEC)
    if NO_ADMIN:
        go("/guest/")
        stay_ready()
        staging_guard()
        check("séjour démo ouvert par le lien invité (clé en localStorage, aucune API admin)", js("return window.GuestStay.snapshot().id;") == DEMO)


def t_G0():
    go("/guest/")
    stay_ready()
    staging_guard()
    title = js("return document.title;")
    check("pas une page d'accès", not any(w in title for w in ("Vercel", "Log in", "Sign in", "Connexion")), f"(titre {title!r})")
    check("aucun défilement horizontal", h_overflow() <= 1, f"({h_overflow()} px)")
    check("pas de zoom", scale() in (1, 1.0), f"(échelle {scale()})")
    shot("G0-accueil")


def t_V():
    """Erreur de validation : numéro de vol vide → message, aria-invalid, AUCUN envoi."""
    if MAXS[0] == 0:
        raise Skip("séjour démo non remis à zéro")
    open_guest("transfert.html")
    no_wa()
    date = D(OFFS["V"])
    before = {x["id"] for x in demandes()}
    set_field("[data-transfer-form] [name='date']", date)
    set_field("[data-transfer-form] [name='time']", "15:30")
    set_field("[name='flight_number']", "")
    set_field("[name='origin_city']", "Paris")
    tap("[data-transfer-submit]")
    time.sleep(1.5)
    msg = js("const e=[...document.querySelectorAll('[data-transfer-form] [data-errors]')].find(x=>!x.hidden); return e ? e.innerText : '';")
    inv = js("const e=document.querySelector(\"[name='flight_number']\"); return e ? e.getAttribute('aria-invalid') : null;")
    blur_active()
    shot("V-erreur-validation")
    check("message d'erreur affiché", "vol" in (msg or ""), f"({msg!r})")
    check("champ vol marqué invalide", inv == "true", f"(aria-invalid {inv!r})")
    check("aucune fenêtre WhatsApp", js("return (window.__waOpened||[]).length;") == 0)
    time.sleep(4)
    leaked = [x for x in demandes() if x["id"] not in before and x.get("stay") == DEMO and x.get("date") == date]
    check("aucune demande envoyée au serveur", not leaked, f"({len(leaked)} ligne(s))")


def t_G4():
    date = D(OFFS["G4"])

    def fill():
        js_click("input[name='trip'][value='arrival']")
        set_field("[data-transfer-form] [name='date']", date)
        set_field("[data-transfer-form] [name='time']", "15:30")
        type_real("[name='flight_number']", "AT800")
        type_real("[name='origin_city']", "Paris")
        blur_active()

    chain("G4", "transfert.html", "transfert", date, fill, "[data-transfer-submit]", trip="arrival")
    check("le WhatsApp de l'invité aurait été ouvert (lien Confirmer), neutralisé", SENT["G4"]["wa"] >= 1, f"({SENT['G4']['wa']} ouverture(s))")
    ok = js("return (window.__waOpened||[]).some(u=>/\\/c\\/[0-9a-z]+-[0-9a-z]+/.test(decodeURIComponent(u)));")
    check("message WhatsApp avec lien /c/ Confirmer", ok)


def t_G1():
    date = D(OFFS["G1"])

    def fill():
        tap("[data-meal='breakfast']")
        set_field("[name='date']", date)
        set_field("[name='time']", "10:00")
        txt = js("const e=document.querySelector('[data-guests-text]'); return e ? e.innerText.trim() : '';")
        check("convives préremplis", txt == "2 adultes · 1 enfant · 1 bébé", f"({txt!r})")
        tap("[data-bf-tab='supplements']")
        tap("[data-extra='fruit_bowl']")
        tap("[data-extra-step='tajine'][data-dir='1']")
        tap("[data-extra-step='tajine'][data-dir='1']")
        tap(".meal-send")  # sans œuf : la page bascule sur « Vos œufs », rien n'est envoyé
        time.sleep(1)
        sel = js("const e=document.querySelector(\"[data-bf-tab='oeufs']\"); return e ? e.getAttribute('aria-selected') : null;")
        check("sans œuf → onglet « Vos œufs »", sel == "true", f"(aria-selected {sel!r})")
        shot("G1-01b-vos-oeufs")
        tap("[data-egg='dur'][data-dir='1']")

    chain("G1", "restauration.html", "restauration", date, fill, ".meal-send", total=50)


def t_G10():
    date = D(OFFS["G10"])

    def fill():
        js_click("input[name='scope']")
        set_field("[name='date']", date)
        set_field("[name='time']", "10:00")

    chain("G10", "menage.html", "menage", date, fill, "[data-menage-form] button[type='submit']")


def t_mes_demandes():
    if not SENT:
        raise Skip("aucune demande envoyée")
    go("/guest/demandes.html")
    wait_js("(document.querySelector('[data-demand-list]')||{}).innerText && /Envoyée/.test(document.querySelector('[data-demand-list]').innerText)", 40, "« Envoyée » dans Mes demandes")
    txt = js("return document.querySelector('main').innerText;")
    raw = [w for w in ("departure", "arrival", "menage", "breakfast", "location_voiture") if w in txt]
    check("libellés en français (aucune valeur brute)", not raw, f"({raw})")
    check("aucun défilement horizontal", h_overflow() <= 1, f"({h_overflow()} px)")
    shot("G-03-mes-demandes")


def t_S5():
    open_guest("restauration.html")
    small = js("return [...document.querySelectorAll('input:not([type=hidden]), select, textarea')].filter(e=>e.offsetParent && parseFloat(getComputedStyle(e).fontSize)<16).map(e=>e.name||e.id);")
    check("champs ≥ 16 px (pas de zoom iOS)", not small, f"({small})")
    check("barre du bas présente", visible(".bottom-nav") is not None)
    type_real("#allergies", "Sans gluten")
    check("vrai clavier iOS ouvert", keyboard_shown())
    check("pas de zoom en tapant", scale() in (1, 1.0), f"(échelle {scale()})")
    shot("S5-clavier")
    js("const e=document.querySelector('#allergies'); if(e){e.value=''; e.dispatchEvent(new Event('input',{bubbles:true}));}")
    blur_active()
    check("pas de zoom après sortie du champ", scale() in (1, 1.0), f"(échelle {scale()})")


def t_S1():
    go("/guest/acces.html")
    js("localStorage.setItem('villazeno.stay', arguments[0]); localStorage.setItem('villazeno.stayKey', '0000000000000000'); localStorage.removeItem('villazeno.stayPack');", DEMO)
    try:
        go("/guest/demandes.html")
        wait_js("document.querySelector(\"[data-demand-state][data-state='invalid']\")", 40, "état « lien invalide »")
        shot("S1-lien-invalide")
    finally:
        go("/guest/acces.html")
        js("localStorage.setItem('villazeno.stay', arguments[0]); localStorage.setItem('villazeno.stayKey', arguments[1]);", DEMO, GUEST_K)


def t_recap():
    go("/guest/recap.html")
    stay_ready()
    wait_js("document.querySelector('[data-bill-card], [data-settled], main h1')", 30, "récapitulatif affiché")
    check("aucun défilement horizontal", h_overflow() <= 1, f"({h_overflow()} px)")
    shot("G-recap")


# ---------------------------------------------------------------- admin (mode test)
ADMIN = {"n0": None}


def admin_login():
    go("/admin/demandes.html")
    js("sessionStorage.setItem('vz.admin', arguments[0]); sessionStorage.setItem('vz.admin.email', 'e2e'); localStorage.setItem('vz.admin.jeu_test', '1');", ADMIN_TOKEN)


def admin_demandes_ready(refresh=False):
    go("/admin/demandes.html")
    staging_guard()
    wait_js("document.querySelector('[data-sync] [data-refresh]:not([disabled])')", 60, "liste admin chargée")
    if refresh:
        tap("[data-sync] [data-refresh]")
        time.sleep(1)
        wait_js("document.querySelector('[data-sync] [data-refresh]:not([disabled])')", 60, "Actualiser terminé")
    tap("[data-filter='toutes']")
    time.sleep(1)


def rows():
    return js("return document.querySelectorAll('[data-list] .req-row').length;")


def t_A0():
    admin_login()
    admin_demandes_ready()
    ADMIN["n0"] = rows()
    check("liste Demandes affichée (mode test)", ADMIN["n0"] is not None and ADMIN["n0"] >= 0, f"({ADMIN['n0']} lignes)")
    shot("A0-demandes-avant")


def t_A4():
    if ADMIN["n0"] is None or not SENT:
        raise Skip("pas de comptage initial ou aucune demande envoyée")
    admin_demandes_ready(refresh=True)
    n = rows()
    check("les nouvelles demandes apparaissent après « Actualiser »", n >= ADMIN["n0"] + len(SENT), f"({ADMIN['n0']} → {n}, envoyées {len(SENT)})")
    shot("A4-actualiser")


def admin_confirm(tid):
    iid = SENT[tid]["id"]
    tap(f"[data-open='{iid}']")
    tap(f"[data-statut='{iid}']")
    tap("[data-set='Confirmée']")
    wait_js(f"document.querySelector(\"[data-open='{iid}'] .status-confirmee\")", 40, f"{tid} confirmée à l'écran")


def t_A_confirm():
    todo = [t for t in ("G10", "G4") if t in SENT]
    if not todo:
        raise Skip("aucune demande à confirmer")
    admin_demandes_ready()
    for t in todo:
        admin_confirm(t)
    shot("A-06-admin-confirmee")
    for t in todo:
        it = item_by_id(SENT[t]["id"])
        check(f"{t} : statut Confirmée côté serveur", it and it.get("status") == "Confirmée", f"({it and it.get('status')!r})")


def t_A_aujourdhui():
    go("/admin/aujourdhui.html")
    staging_guard()
    wait_js("document.querySelector('[data-sync] [data-refresh]:not([disabled])')", 60, "Aujourd'hui chargé")
    check("aucun défilement horizontal", h_overflow() <= 1, f"({h_overflow()} px)")
    shot("A-aujourdhui")


def t_G_confirmee():
    if not any(t in SENT for t in ("G10", "G4")):
        raise Skip("aucune demande confirmée")
    go("/guest/demandes.html")
    wait_js("(document.querySelector('[data-demand-list]')||{}).innerText && /Confirmée/.test(document.querySelector('[data-demand-list]').innerText)", 40, "« Confirmée » côté invité")
    shot("G-09-confirmee")


NOM = f"E2E iOS {LABEL} {int(time.time()) % 100000}"
A1S = {}


def t_A1():
    admin_login()
    go("/admin/nouveau.html")
    staging_guard()
    time.sleep(4)  # laisse au script Vercel le temps de (ne pas) monter la barre
    st = toolbar_state()
    shot("A1-00-barre-absente")
    if st.get("barre") or st.get("optout") != "1":
        raise Blocked(f"barre d'outils Vercel présente ou non désactivée ({st}) : ni « + » ni « Enregistrer » touchés")
    check("barre d'outils Vercel absente du DOM (preuve avant tout toucher)", True, f"({st})")
    type_real("[name='nom']", NOM)
    set_field("[name='telephone']", "+33 6 00 00 00 11")
    set_field("[name='arrival']", "2028-11-10")
    set_field("[name='departure']", "2028-11-14")
    tap("[data-source='Direct']", guard=True)
    tap("[data-step='kids4to11'][data-dir='1']", guard=True)
    kids = js("const e=document.querySelector('output[data-kids4to11]'); return e ? e.textContent.trim() : null;")
    check("« + » enfants 4–11 ans pris en compte", kids == "1", f"({kids!r})")
    set_field("[name='tarif']", "1000")
    blur_active()
    shot("A1-01-nouveau")
    if toolbar_state().get("barre"):
        raise Blocked("barre d'outils Vercel réapparue avant « Enregistrer » : pas de toucher")
    tap("[data-save]", guard=True)
    wait_js("document.querySelector('.form-ok')", 60, "séjour enregistré (.form-ok)")
    s = None
    for _ in range(10):
        s = next((x for x in (exec_get({"action": "stays"}).get("stays") or []) if x.get("nom") == NOM), None)
        if s:
            break
        time.sleep(3)
    check("séjour créé côté serveur", s is not None)
    # le jeton « test » ne voit QUE les séjours origine=test : sa présence le prouve ; on vérifie aussi le champ s'il est renvoyé
    check("séjour de TEST (origine test)", s.get("origine", "test") == "test", f"(origine {s.get('origine', 'non renvoyée, implicite par le jeton test')!r})")
    A1S.update({"id": s["id"], "k": s.get("k", "")})
    mask_in_logs(A1S["k"])
    go("/admin/sejour.html?id=" + urllib.parse.quote(s["id"]))
    wait_js("/k=/.test((document.querySelector('.link-url')||{}).textContent||'')", 40, "lien invité affiché")
    shot("A1-02-fiche")  # le lien est noirci par mask_text_in_page()
    go("/guest/acces.html")
    js("localStorage.setItem('villazeno.stay', arguments[0]); localStorage.setItem('villazeno.stayKey', arguments[1]);", A1S["id"], A1S["k"])
    open_guest("restauration.html")
    txt = js("const e=document.querySelector('[data-guests-text]'); return e ? e.innerText.trim() : '';")
    check("convives préremplis depuis la fiche", txt == "2 adultes · 1 enfant", f"({txt!r})")
    shot("A1-03-convives")


def t_A2():
    if not A1S:
        raise Skip("A1 n'a pas créé de séjour")
    go("/admin/sejour.html?id=" + urllib.parse.quote(A1S["id"]))
    check("fiche ouverte = séjour créé par A1", js("return new URLSearchParams(location.search).get('id');") == A1S["id"])
    tap("[data-stop]", guard=True)
    wait_js("/désactivé/i.test((document.querySelector('.link-until')||{}).textContent||'')", 40, "lien désactivé")
    shot("A2-01-desactive")
    go("/guest/demandes.html")  # localStorage = séjour A1
    wait_js("/expire\\.html/.test(location.pathname)", 40, "redirection vers expire.html")
    shot("A2-02-lien-refuse")


def t_A3():
    if not A1S:
        raise Skip("A1 n'a pas créé de séjour")
    go("/admin/sejour.html?id=" + urllib.parse.quote(A1S["id"]))
    check("fiche ouverte = séjour créé par A1", js("return new URLSearchParams(location.search).get('id');") == A1S["id"])
    tap("[data-prolong]", guard=True)
    wait_js("/actif jusqu/i.test((document.querySelector('.link-until')||{}).textContent||'')", 40, "lien actif jusqu'au …")
    shot("A3-prolonge")
    s = next((x for x in (exec_get({"action": "stays"}).get("stays") or []) if x.get("id") == A1S["id"]), None)
    check("lien actif côté serveur", s and s.get("lien_actif") is True, f"({s and s.get('lien_actif')!r})")


def t_A5():
    go("/admin/depense.html")
    wait_js("document.querySelector('form, [data-depense-form]')", 40, "formulaire Dépense")
    shot("A5-depense")


def t_A6():
    go("/admin/reglages.html")
    wait_js("document.querySelector('[data-jeu-test]')", 40, "Réglages (jeu de test)")
    shot("A6-reglages")


def t_A7():
    stats = exec_get({"action": "stays-stats"}).get("stays") or []
    check("« Famille Démo » hors des statistiques réelles (API)", not any(x.get("id") == DEMO for x in stats))
    go("/admin/stats.html")
    wait_js("document.querySelector('[data-refresh]:not([disabled])')", 60, "Statistiques chargées")
    shot("A7-stats")


def t_A8():
    r = exec_get({"action": "stays"}, token="forged")
    check("jeton forgé refusé par l'API", r == {"ok": False, "error": "forbidden"}, f"({str(r)[:80]})")
    go("/admin/demandes.html")
    js("sessionStorage.removeItem('vz.admin'); sessionStorage.removeItem('vz.admin.email');")
    go("/admin/demandes.html")
    wait_js("document.querySelector('[data-gate]')", 40, "porte d'accès admin")
    shot("A8-porte")


# ---------------------------------------------------------------- audit du 28/11 (invité, 0 envoi, 0 admin)
def layout_ok(tag):
    check(f"{tag} : aucun défilement horizontal", h_overflow() <= 1, f"({h_overflow()} px)")
    check(f"{tag} : pas de zoom", scale() in (1, 1.0), f"(échelle {scale()})")


def text_of(sel):
    return js("const e=document.querySelector(arguments[0]); return e ? e.textContent.trim() : null;", sel)


def norm(t):
    """Espaces insécables (U+00A0, U+202F) et autres blancs ramenés à une espace simple."""
    return " ".join(str(t or "").replace("\u00a0", " ").replace("\u202f", " ").split())


def demo_travelers():
    """Adultes + enfants du séjour démo, lus côté invité (même règle que la page)."""
    return js("const s=(window.GuestStay.snapshot()||{}).stay||{}; if(!(Number(s.adults)>0)) return 0;"
              "const k=Math.max(Number(s.children)||0,(Number(s.children_4_11)||0)+(Number(s.under_4)||0)); return Number(s.adults)+k;")


def t_AUD1():
    open_guest("transfert.html")
    n = demo_travelers()
    check("voyageurs du séjour démo lus côté invité", n > 0, f"({n})")
    pax, town = min(n, 16), min(n, 8)
    wait_js(f"document.querySelector(\"[data-transfer-form][data-prefilled='{pax}']\")", 20, "Aéroport prérempli")
    check("Aéroport : passagers = adultes + enfants", text_of("[data-passengers-count]") == str(pax), f"({text_of('[data-passengers-count]')!r}, attendu {pax})")
    shot("AUD1-01-aeroport-prerempli")
    layout_ok("Aéroport")
    if pax < 16:
        tap("[data-step='passengers'][data-dir='1']", guard=True)
        check("Aéroport : « + » au vrai toucher", text_of("[data-passengers-count]") == str(pax + 1), f"({text_of('[data-passengers-count]')!r})")
    tap("[data-show='city']", guard=True)
    check("En ville : personnes préremplies", text_of("[data-city-count]") == str(town), f"({text_of('[data-city-count]')!r}, attendu {town})")
    tap("[data-city-step='-1']", guard=True)
    check("En ville : « − » au vrai toucher", text_of("[data-city-count]") == str(max(1, town - 1)), f"({text_of('[data-city-count]')!r})")
    shot("AUD1-02-en-ville")
    layout_ok("En ville")


def price_total():
    t = text_of("[data-transfer-form] [data-price-total]") or ""
    digits = "".join(c for c in t if c.isdigit())
    return (int(digits) if digits else None), t


def t_AUD2():
    open_guest("transfert.html")
    wait_js("document.querySelector('[data-transfer-form][data-prefilled]')", 20, "Aéroport prérempli")
    check("plus d'interrupteur siège bébé", js("return document.querySelectorAll(\"[data-transfer-form] [name='baby_seat'], [data-transfer-form] .baby-row .switch\").length;") == 0)
    check("compteur siège bébé à 0", text_of("[data-baby-count]") == "0", f"({text_of('[data-baby-count]')!r})")
    check("« − » désactivé à 0", js("const b=document.querySelector(\"[data-step='baby'][data-dir='-1']\"); return !!(b && b.disabled);"))
    hint = js("const e=document.querySelector('.baby-hint'); if(!e) return ''; const r=e.getBoundingClientRect(); return r.width>0 && r.height>0 ? e.textContent.trim() : '(cachée)';")
    check("ligne « 50 MAD le siège » visible", norm(hint) == "50 MAD le siège", f"({hint!r})")
    t0, txt0 = price_total()
    tap("[data-step='baby'][data-dir='1']", guard=True)
    check("« + » au vrai toucher → 1 siège", text_of("[data-baby-count]") == "1", f"({text_of('[data-baby-count]')!r})")
    t1, txt1 = price_total()
    check("total + 50 MAD", t0 is not None and t1 == t0 + 50 and "MAD" in norm(txt1), f"({txt0!r} → {txt1!r})")
    brk = text_of("[data-transfer-form] [data-price-break]") or ""
    check("détail « 1 siège bébé 50 MAD »", "1 siège bébé 50 MAD" in norm(brk), f"({brk!r})")
    shot("AUD2-siege-bebe-1")
    layout_ok("Transfert siège bébé")


def t_AUD4():
    open_guest("acces.html")
    tap(".bottom-nav [data-open-contacts]", guard=True)
    wait_js("(document.querySelector('#contacts-dialog')||{}).open", 15, "feuille Contacts utiles ouverte")
    txt = js("return document.querySelector('#contacts-dialog').innerText;")
    check("pas de « depuis un fixe »", "depuis un fixe" not in txt)
    check("pas de sous-titre « WhatsApp » (wa-label)", js("return document.querySelectorAll('#contacts-dialog .wa-label').length;") == 0)
    who = js("return [...document.querySelectorAll('#contacts-dialog .who')].map(e=>e.innerText.trim());")
    check("Mehdi et Hicham sans sous-titre", "Mehdi" in who and "Hicham" in who, f"({who[:3]})")
    shot("AUD4-contacts")
    tap("[data-contacts-close]", guard=True)


DETECT_JS = ("const m=document.querySelector('meta[name=\"format-detection\"]');"
             "const auto=[...document.querySelectorAll('a[x-apple-data-detectors], a[href^=\"tel:\"]')].filter(a=>a.hasAttribute('x-apple-data-detectors') || !a.closest('#contacts-dialog, .contact-list, [data-contacts]'));"
             "return {meta: m ? m.getAttribute('content') : null, auto: auto.length, sample: auto.slice(0,2).map(a=>a.textContent.trim().slice(0,20))};")


def t_AUD5():
    for page in ("acces.html", "demandes.html", "recap.html", "expire.html", "confirmation.html"):
        go("/guest/" + page)
        time.sleep(2)
        r = js(DETECT_JS)
        check(f"{page} : balise format-detection telephone=no", r.get("meta") == "telephone=no", f"({r.get('meta')!r})")
        check(f"{page} : aucun numéro transformé en lien par Safari", r.get("auto") == 0, f"({r})")
        if page == "acces.html":
            shot("AUD5-acces")
            layout_ok("Accès")


def massage_selection():
    return js("const c=document.querySelector(\"[data-soin='decouverte']\");"
              "return {picked: c && c.querySelector('.soin-pick') ? c.querySelector('.soin-pick').getAttribute('aria-pressed') : null,"
              "date: (document.querySelector(\"[data-massage-form] [name='date']\")||{}).value || '',"
              "total: (document.querySelector('[data-massage-form] [data-price-total]')||{}).textContent || '', path: location.pathname};")


def brochure_open():
    return js("const d=document.querySelector('[data-brochure]'); if(!d) return false; const r=d.getBoundingClientRect(); return (d.open || !d.hidden) && r.width>0 && r.height>0 && getComputedStyle(d).display!=='none';")


def t_AUD6():
    open_guest("massage.html")
    tap("[data-soin='decouverte'] .soin-pick", guard=True)
    set_field("[data-massage-form] [name='date']", D(9))
    before = massage_selection()
    check("sélection faite (1 soin + date)", before.get("picked") == "true" and before.get("date") == D(9), f"({before})")
    tap("[data-brochure-open]", guard=True)
    wait_js("(" + "(()=>{const d=document.querySelector('[data-brochure]'); return d && (d.open || !d.hidden);})()" + ")", 15, "visionneuse ouverte")
    check("visionneuse ouverte, page 1 sur 4", (text_of("[data-brochure-live]") or "") == "Page 1 sur 4", f"({text_of('[data-brochure-live]')!r})")
    tap("[data-brochure-go='1']", guard=True)
    wait_js("(document.querySelector('[data-brochure-live]')||{}).textContent === 'Page 2 sur 4'", 10, "page 2 (flèche)")
    # balayage réel vers la gauche, puis flèche en repli si besoin
    how = []
    for _ in range(4):
        if text_of("[data-brochure-live]") == "Page 4 sur 4":
            break
        native()
        try:
            d.execute_script("mobile: swipe", {"direction": "left"})
        finally:
            web()
        time.sleep(1.2)
        how.append("geste")
    if text_of("[data-brochure-live]") != "Page 4 sur 4":
        for _ in range(3):
            if text_of("[data-brochure-live]") == "Page 4 sur 4":
                break
            tap("[data-brochure-go='1']")
            how.append("flèche")
            time.sleep(0.8)
    check("pages 2 à 4 parcourues", text_of("[data-brochure-live]") == "Page 4 sur 4", f"({text_of('[data-brochure-live]')!r}, moyens {how})")
    note(f"AUD6 : pages 3–4 atteintes par {how or ['flèche']}")
    shot("AUD6-01-visionneuse")
    tap("[data-brochure-close]", guard=True)
    wait_js("!(" + "(()=>{const d=document.querySelector('[data-brochure]'); return d && (d.open || !d.hidden) && getComputedStyle(d).display!=='none';})()" + ")", 10, "visionneuse fermée (Fermer)")
    after = massage_selection()
    check("« Fermer » : sélection conservée", after == before, f"({after})")
    tap("[data-brochure-open]", guard=True)
    wait_js("(" + "(()=>{const d=document.querySelector('[data-brochure]'); return d && (d.open || !d.hidden);})()" + ")", 15, "visionneuse rouverte")
    d.back()  # bouton / geste retour de Safari
    time.sleep(1.5)
    wait_js("!(" + "(()=>{const d=document.querySelector('[data-brochure]'); return d && (d.open || !d.hidden) && getComputedStyle(d).display!=='none';})()" + ")", 10, "visionneuse fermée (retour)")
    after2 = massage_selection()
    check("retour : on reste sur la page, sélection conservée", after2 == before, f"({after2})")
    shot("AUD6-02-apres-fermeture")
    layout_ok("Massages")


def t_AUD8():
    open_guest("massage.html")
    main = js("return document.querySelector('main').innerText;")
    check("massages : MAD affiché", "MAD" in main)
    check("massages : aucun « DH »", not __import__("re").search(r"\bDH\b", main))
    shot("AUD8-01-massages")
    go("/guest/activites.html")
    stay_ready()
    main = js("return document.querySelector('main').innerText;")
    check("expériences : MAD affiché", "MAD" in main)
    check("expériences : aucun « DH »", not __import__("re").search(r"\bDH\b", main))
    layout_ok("Expériences")
    go("/guest/recap.html")
    stay_ready()
    time.sleep(2)
    rate = norm(text_of(".recap-rate"))
    check("récap : « 1 € = 10 MAD »", "1 € = 10 MAD" in rate, f"({rate!r})")
    bill = js("return [...document.querySelectorAll('[data-bill] li, [data-paid] li, [data-settled-lines] li')].map(e=>e.innerText).join(' | ');") or ""
    main = js("return document.querySelector('main').innerText;")
    check("récap : aucun « DH »", not __import__("re").search(r"\bDH\b", main))
    if not bill.strip():
        note("AUD8 : récapitulatif démo vide (aucune ligne), « soit … MAD » non vérifiable : documenté, pas un échec")
    else:
        b = norm(bill)
        check("récap : montants en €", "€" in b, f"({b[:120]!r})")
        check("récap : « soit … MAD »", bool(__import__("re").search(r"soit [\d ]+ MAD", norm(main))), f"({main[:160]!r})")
    shot("AUD8-02-recap")
    layout_ok("Récapitulatif")


# ---------------------------------------------------------------- nouveau design (invité, 0 envoi, 0 admin)
# Critères : specs « § 10 Critères d'acceptation (iPhone 16 et SE) » de La Villa v2h, Wi-Fi & TV v3c,
# Loisirs v3d, Nos adresses v3b. Mesures dans le DOM réel de Safari ; captures pour le jugement visuel.
SCREEN_JS = """
const s=document.querySelector('.screen'), nav=document.querySelector('.nav, .bottom-nav'), act=document.querySelector('.action');
const navTop = nav ? nav.getBoundingClientRect().top : window.innerHeight;
const fonts=[...document.querySelectorAll('main *')].filter(e=>e.offsetParent && e.childNodes.length && [...e.childNodes].some(n=>n.nodeType===3 && n.data.trim())).map(e=>parseFloat(getComputedStyle(e).fontSize));
const one=[...document.querySelectorAll('[data-one]')].filter(e=>e.offsetParent).filter(e=>{const r=document.createRange(); r.selectNodeContents(e); const rects=[...r.getClientRects()].filter(x=>x.width>0); const tops=new Set(rects.map(x=>Math.round(x.top))); return tops.size>1;}).map(e=>e.textContent.trim().slice(0,30));
return {screenScroll: s ? s.scrollHeight - s.clientHeight : null, scrollTop: s ? s.scrollTop : null,
        hOverflow: document.documentElement.scrollWidth - window.innerWidth, navTop: navTop,
        actionTop: act ? act.getBoundingClientRect().top : null, innerH: window.innerHeight,
        fontMin: fonts.length ? Math.min.apply(null, fonts) : null, titresSurPlusieursLignes: one,
        scale: window.visualViewport ? window.visualViewport.scale : 1};
"""


def screen_metrics(tag):
    m = js(SCREEN_JS)
    results["tests"][CUR["id"]].setdefault("mesures", {})[tag] = m
    return m


def no_hscroll(tag, m):
    check(f"{tag} : aucun débordement horizontal", (m.get("hOverflow") or 0) <= 1, f"({m.get('hOverflow')} px)")


def go_page(path):
    go(path)
    wait_js("document.fonts ? document.fonts.status === 'loaded' : true", 15, "polices chargées")
    time.sleep(0.8)


OPEN_SEC_JS = """
const open=[...document.querySelectorAll('.guide-list details')].filter(d=>d.open);
const el=open[0]; if(!el) return {open: 0};
const nav=document.querySelector('.nav, .bottom-nav'), stick=document.querySelector('.stick');
const body=el.querySelector('.guide-body'), last=body ? body.lastElementChild : null;
return {open: open.length, sec: el.getAttribute('data-sec'),
        sousBarre: last && nav ? Math.max(0, Math.round(last.getBoundingClientRect().bottom - nav.getBoundingClientRect().top)) : null,
        titreCouvert: stick ? Math.max(0, Math.round(stick.getBoundingClientRect().bottom - el.querySelector('summary').getBoundingClientRect().top)) : 0};
"""


def t_DES1():
    go_page("/guest/villa.html")
    n = js("return document.querySelectorAll('.guide-list details').length;")
    check("La Villa : 10 sections", n == 10, f"({n})")
    m = screen_metrics("ferme")
    check("liste fermée : aucun défilement de l'écran", (m.get("screenScroll") or 0) <= 1, f"({m.get('screenScroll')} px)")
    no_hscroll("liste fermée", m)
    shot("DES1-01-liste-fermee")
    for sec, name, tag in (("quotidien", "Au quotidien", "DES1-02-au-quotidien"), ("depart", "Votre départ", "DES1-03-votre-depart")):
        tap(f".guide-list details[data-sec='{sec}'] > summary", guard=True)
        wait_js(f"document.querySelector(\".guide-list details[data-sec='{sec}']\").open", 10, f"« {name} » ouvert")
        time.sleep(1.2)  # place() (requestAnimationFrame) puis défilement
        o = js(OPEN_SEC_JS)
        results["tests"][CUR["id"]].setdefault("mesures", {})[sec] = o
        check(f"« {name} » : une seule section ouverte", o.get("open") == 1 and o.get("sec") == sec, f"({o})")
        check(f"« {name} » : texte ouvert au-dessus de la barre du bas", (o.get("sousBarre") or 0) <= 1, f"({o.get('sousBarre')} px sous la barre)")
        check(f"« {name} » : titre non recouvert par l'en-tête", (o.get("titreCouvert") or 0) <= 1, f"({o.get('titreCouvert')} px)")
        no_hscroll(f"« {name} »", screen_metrics(sec))
        shot(tag)


def t_DES2():
    go_page("/guest/wifi.html")
    wait_js("document.querySelector('[data-wifi-qr-svg] svg, [data-wifi-qr-svg] canvas, [data-wifi-qr-svg] img')", 15, "QR affiché")
    qr = js("const b=document.querySelector('[data-wifi-qr-svg]'); const g=b.querySelector('svg, canvas, img'); const r=g.getBoundingClientRect();"
            "return {tag: g.tagName.toLowerCase(), w: Math.round(r.width), h: Math.round(r.height), modules: g.tagName.toLowerCase()==='svg' ? g.querySelectorAll('rect, path').length : null};")
    results["tests"][CUR["id"]]["mesures"] = {"qr": qr}
    check("QR présent et non vide", qr.get("w", 0) > 40 and qr.get("h", 0) > 40 and (qr.get("modules") is None or qr["modules"] > 0), f"({qr})")
    net = norm(text_of("[data-wifi-network]"))
    check("nom du réseau affiché", bool(net))
    m = screen_metrics("page")
    check("Wi-Fi : aucun défilement de l'écran", (m.get("screenScroll") or 0) <= 1, f"({m.get('screenScroll')} px)")
    under = js("const nav=document.querySelector('.nav, .bottom-nav'); const last=document.querySelector('.wf-block'); return nav && last ? Math.max(0, Math.round(last.getBoundingClientRect().bottom - nav.getBoundingClientRect().top)) : null;")
    check("Wi-Fi : contenu au-dessus de la barre", (under or 0) <= 1, f"({under} px)")
    no_hscroll("Wi-Fi", m)
    shot("DES2-wifi-tv")


def t_DES3():
    go_page("/guest/loisirs.html")
    tiles = js("const nav=document.querySelector('.nav, .bottom-nav'); const nt=nav ? nav.getBoundingClientRect().top : innerHeight;"
               "return [...document.querySelectorAll('.tiles .tile')].map(t=>{const r=t.getBoundingClientRect(); return {w: Math.round(r.width), h: Math.round(r.height), rogne: r.top < 0 || r.bottom > nt + 1 || r.left < 0 || r.right > innerWidth + 1 || t.scrollHeight > t.clientHeight + 1};});")
    results["tests"][CUR["id"]]["mesures"] = {"cartes": tiles}
    check("Loisirs : 8 cartes", len(tiles) == 8, f"({len(tiles)})")
    check("aucune carte rognée", not any(t["rogne"] for t in tiles), f"({[i + 1 for i, t in enumerate(tiles) if t['rogne']]})")
    m = screen_metrics("page")
    check("Loisirs : aucun défilement de l'écran", (m.get("screenScroll") or 0) <= 1, f"({m.get('screenScroll')} px)")
    no_hscroll("Loisirs", m)
    shot("DES3-loisirs")


def t_DES4():
    go_page("/guest/marrakech.html")
    m = screen_metrics("ferme")
    check("accordéon fermé : aucun défilement de l'écran", (m.get("screenScroll") or 0) <= 1, f"({m.get('screenScroll')} px)")
    no_hscroll("fermé", m)
    shot("DES4-01-ferme")
    btns = js("return [...document.querySelectorAll('a, button')].filter(e=>/Réserver un chauffeur/.test(e.textContent)).map(e=>e.getAttribute('href'));")
    check("un seul « Réserver un chauffeur » vers transfert.html#ville", len(btns) == 1 and (btns[0] or "").endswith("transfert.html#ville"), f"({btns})")
    tap("#btn-medina", guard=True)
    wait_js("document.querySelector('#btn-medina').getAttribute('aria-expanded') === 'true' && !document.querySelector('#p-medina').hidden", 10, "« La médina » ouverte")
    opened = js("return document.querySelectorAll(\".acc-btn[aria-expanded='true']\").length;")
    check("un seul panneau ouvert", opened == 1, f"({opened})")
    js("const s=document.querySelector('.screen'); s.scrollTop = s.scrollHeight;")  # en bas de défilement
    time.sleep(1)
    under = js("const lim=Math.min((document.querySelector('.action')||{getBoundingClientRect:()=>({top:innerHeight})}).getBoundingClientRect().top,"
               "(document.querySelector('.nav, .bottom-nav')||{getBoundingClientRect:()=>({top:innerHeight})}).getBoundingClientRect().top);"
               "const last=document.querySelector('#p-medina .sec-body').lastElementChild; return Math.max(0, Math.round(last.getBoundingClientRect().bottom - lim));")
    mo = screen_metrics("medina-bas")
    check("médina en bas : contenu au-dessus du bouton et de la barre", (under or 0) <= 1, f"({under} px)")
    no_hscroll("médina ouverte", mo)
    shot("DES4-02-medina-bas")
    tap(".action a.btn", guard=True)
    wait_js("/transfert\\.html$/.test(location.pathname) && location.hash === '#ville'", 15, "transfert.html#ville ouvert")
    on_vercel_login()
    hide_toolbar()
    wait_js("(document.querySelector(\"[data-show='city']\")||{getAttribute:()=>null}).getAttribute('aria-selected') === 'true'", 15, "onglet En ville actif")
    check("transfert : onglet « En ville » actif", True)
    shot("DES4-03-transfert-en-ville")


def webview_id():
    native()
    try:
        els = d.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeWebView")
        return els[0].id if els else None
    finally:
        web()


def try_zoom():
    """Zoom réel dans Safari : pincement XCUITest, sinon double-toucher. Renvoie (échelle, moyen)."""
    s0 = scale() or 1
    wid = webview_id()
    for how, cmd, args in (("pincement", "mobile: pinch", {"scale": 3.0, "velocity": 2.5}),
                           ("double-toucher", "mobile: doubleTap", {})):
        if not wid:
            break
        native()
        try:
            d.execute_script(cmd, dict(args, elementId=wid))
        except Exception as e:  # noqa: BLE001
            log(f"zoom {how} indisponible ({type(e).__name__})")
        finally:
            web()
        time.sleep(1.5)
        s1 = scale() or 1
        if s1 > s0 * 1.2:
            return s1, how
    return scale() or 1, "aucun"


def unzoom():
    wid = webview_id()
    for _ in range(3):
        if (scale() or 1) <= 1.05 or not wid:
            return
        native()
        try:
            d.execute_script("mobile: pinch", {"elementId": wid, "scale": 0.2, "velocity": -3.0})
        except Exception:  # noqa: BLE001
            pass
        finally:
            web()
        time.sleep(1.2)


def t_DES5():
    go_page("/guest/massage.html")
    tap("[data-brochure-open]", guard=True)
    wait_js("(()=>{const d=document.querySelector('[data-brochure]'); return d && (d.open || !d.hidden);})()", 15, "visionneuse ouverte")
    wait_js("[...document.querySelectorAll('[data-brochure] img')].every(i=>i.complete && i.naturalWidth>0)", 30, "4 pages chargées")
    imgs = js("return [...document.querySelectorAll('[data-brochure] img')].map(i=>({naturel: i.naturalWidth, affiche: Math.round(i.getBoundingClientRect().width)}));")
    pages = []
    for i in range(1, len(imgs) + 1):
        if i > 1:
            tap("[data-brochure-go='1']", guard=True)
            wait_js(f"(document.querySelector('[data-brochure-live]')||{{}}).textContent === 'Page {i} sur 4'", 10, f"page {i}")
        time.sleep(0.6)
        z, how = try_zoom()
        im = imgs[i - 1]
        # pixels d'écran (points CSS × échelle) par pixel de l'image d'origine
        rendu = round(im["affiche"] * z / im["naturel"], 3) if im["naturel"] else None
        pages.append({"page": i, "largeur_naturelle_px": im["naturel"], "largeur_affichee_px": im["affiche"], "zoom": round(z, 2), "moyen": how, "points_par_px_image": rendu})
        shot(f"DES5-0{i}-page-{i}-zoom")
        unzoom()
    results["tests"][CUR["id"]]["mesures"] = {"pages": pages}
    note("DES5 : " + "; ".join(f"p{p['page']} {p['largeur_naturelle_px']} px, zoom ×{p['zoom']} ({p['moyen']})" for p in pages))
    check("4 pages de brochure", len(pages) == 4, f"({len(pages)})")
    check("zoom obtenu sur au moins une page", any(p["zoom"] > 1.2 for p in pages), f"({[(p['page'], p['zoom'], p['moyen']) for p in pages]})")
    try:
        tap("[data-brochure-close]", guard=True)
    except Exception as e:  # noqa: BLE001
        note(f"DES5 : fermeture de la visionneuse non faite ({type(e).__name__})")


def t_restore_demo():
    go("/guest/acces.html")
    js("localStorage.setItem('villazeno.stay', arguments[0]); localStorage.setItem('villazeno.stayKey', arguments[1]);", DEMO, GUEST_K)


# ---------------------------------------------------------------- session
def start_session():
    opts = XCUITestOptions()
    opts.platform_version = os.environ["IOS_VERSION"]
    opts.device_name = os.environ["DEVICE_NAME"]
    opts.udid = os.environ["UDID"]
    opts.browser_name = "Safari"
    opts.set_capability("appium:nativeWebTap", True)
    opts.set_capability("appium:connectHardwareKeyboard", False)
    opts.set_capability("appium:forceSimulatorSoftwareKeyboardPresence", True)
    opts.set_capability("appium:wdaLaunchTimeout", 300000)
    opts.set_capability("appium:simulatorStartupTimeout", 600000)
    opts.set_capability("appium:wdaConnectionTimeout", 300000)
    opts.set_capability("appium:webviewConnectTimeout", 90000)
    opts.set_capability("appium:webviewConnectRetries", 20)
    opts.set_capability("appium:newCommandTimeout", 900)
    opts.set_capability("appium:safariIgnoreFraudWarning", True)
    try:
        from appium.webdriver.client_config import AppiumClientConfig
        cfg = AppiumClientConfig(remote_server_addr="http://127.0.0.1:4723", timeout=900)
        drv = webdriver.Remote(options=opts, client_config=cfg)
    except ImportError:
        drv = webdriver.Remote("http://127.0.0.1:4723", options=opts)
    drv.set_script_timeout(60)
    return drv


def dismiss_safari_tips():
    native()
    pred = ("type == 'XCUIElementTypeButton' AND (name IN {'Close','Fermer','Not Now','Continue','OK','Dismiss','xmark','close'} "
            "OR name CONTAINS[c] 'close' OR name CONTAINS[c] 'dismiss' OR label CONTAINS[c] 'close' OR label CONTAINS[c] 'fermer')")
    for _ in range(3):
        clicked = False
        for el in d.find_elements(AppiumBy.IOS_PREDICATE, pred):
            try:
                el.click()
                clicked = True
                time.sleep(0.6)
            except Exception:  # noqa: BLE001
                pass
        if not clicked:
            break
    web()


def main():
    global d
    try:
        d = start_session()
        results["session"] = True
    except Exception as e:  # noqa: BLE001
        results["session_error"] = red(f"{type(e).__name__}: {e}")[:400]
        save()
        log("Session Appium impossible [rig]")
        return 3
    try:
        web()
        run("P0", "préparation : accès Vercel, /api/vz-env staging, séjour de démonstration", t_setup, "harnais/SSO")
        if results["tests"]["P0"]["status"] != "pass":
            note("préparation en échec : aucun parcours lancé (aucun envoi)")
            return 1
        dismiss_safari_tips()
        if ONLY:
            ignored = [x for x in ONLY if x not in ONLY_OK]
            if ignored:
                note(f"E2E_ONLY : {ignored} non pris en charge, ignorés (seuls {list(ONLY_OK)})")
            note(f"mode E2E_ONLY={','.join(ONLY)} : aucune remise à zéro du séjour démo, aucun envoi invité")
            MAXS[0] = 0
            table = {"A1": ("admin : créer un séjour de test, convives préremplis côté invité", t_A1),
                     "A2": ("admin : Désactiver le lien → refusé côté invité", t_A2),
                     "A3": ("admin : Prolonger le lien", t_A3),
                     "AUD1": ("audit 1 : voyageurs Aéroport / En ville préremplis, modifiables", t_AUD1),
                     "AUD2": ("audit 2 : siège bébé en compteur 0–3, +50 MAD", t_AUD2),
                     "AUD4": ("audit 4 : Contacts utiles sans « depuis un fixe » ni sous-titre WhatsApp", t_AUD4),
                     "AUD5": ("audit 5 : format-detection, aucun numéro lié par Safari", t_AUD5),
                     "AUD6": ("audit 6 : brochure du spa (visionneuse, Fermer, retour, sélection conservée)", t_AUD6),
                     "AUD8": ("audit 8 : MAD sur les services, récap en € « soit … MAD »", t_AUD8),
                     "DES1": ("design : La Villa, accordéon (Au quotidien, Votre départ)", t_DES1),
                     "DES2": ("design : Wi-Fi & TV (QR local, réseau, un seul écran)", t_DES2),
                     "DES3": ("design : Loisirs (8 cartes, un seul écran)", t_DES3),
                     "DES4": ("design : Nos adresses (La médina, Réserver un chauffeur → En ville)", t_DES4),
                     "DES5": ("design : brochure du spa zoomée (lisibilité)", t_DES5)}
            for tid in ONLY_OK:
                if tid in ONLY:
                    run(tid, table[tid][0], table[tid][1])
            return 0
        if LABEL == "big":
            run("R0", "remise à zéro du séjour démo (annule les anciennes demandes, efface l'encaissement)", t_reset, "backend/harnais")
        else:
            run("R0", "attente de la remise à zéro faite par l'iPhone big", t_wait_reset, "harnais")
        if results["tests"]["R0"]["status"] != "pass":
            note("séjour démo non remis à zéro : aucun envoi")
            MAXS[0] = 0
        run("G0", "accueil invité depuis le séjour (sans page d'accès ni erreur)", t_G0)
        run("A0", "admin (mode test) : liste Demandes avant envois", t_A0)
        run("V1", "erreur de validation (vol vide) : message, aucun envoi", t_V)
        run("G4", "transfert aéroport, arrivée (vol tapé au vrai clavier)", t_G4)
        run("G1", "petit-déjeuner : suppléments, bascule « Vos œufs », 50 €", t_G1)
        run("G10", "ménage", t_G10)
        run("G3m", "Mes demandes : « Envoyée », libellés français", t_mes_demandes)
        run("A4", "admin : nouvelles demandes visibles après « Actualiser »", t_A4)
        run("A6c", "admin : confirmer G10 et G4 (Changer statut → Confirmée)", t_A_confirm)
        run("A9a", "admin : Aujourd'hui (lecture)", t_A_aujourdhui)
        run("G9c", "invité : « Confirmée » dans Mes demandes", t_G_confirmee)
        run("G12r", "récapitulatif (lecture seule, sans encaissement)", t_recap)
        run("S5", "téléphone : champs ≥ 16 px, vrai clavier, pas de zoom, barre du bas", t_S5)
        run("S1", "clé invalide refusée", t_S1)
        run("A1", "admin : créer un séjour, convives préremplis côté invité", t_A1)
        run("A2", "admin : Désactiver le lien → refusé côté invité", t_A2)
        run("A3", "admin : Prolonger le lien", t_A3)
        try:
            t_restore_demo()
        except Exception as e:  # noqa: BLE001
            note(f"retour au séjour de démonstration impossible ({type(e).__name__})")
        run("A5", "admin : Dépense (lecture)", t_A5)
        run("A6", "admin : Réglages (lecture)", t_A6)
        run("A7", "admin : Statistiques sans « Famille Démo »", t_A7)
        run("A8", "admin : jeton forgé refusé, porte d'accès", t_A8)
        for sid, why in (("S2", "limite 30/h : non rejouée sur le staging (consommerait le quota du séjour)"),
                         ("S3", "aucun WhatsApp équipe sur le staging (pas de TEST_WA_NUMBER)"),
                         ("S4", "réseau lent simulé : impossible sans maquette du serveur")):
            results["tests"][sid] = {"group": "S", "title": sid, "status": "skip", "cause": why, "checks": [], "shots": []}
        return 0
    finally:
        save()
        try:
            d.quit()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    code = main()
    fails = [k for k, v in results["tests"].items() if v["status"] in ("fail", "blocked")]
    log(f"\nEnvois : {results['submitted']} / {MAX_SENDS} — échecs : {fails or 'aucun'}")
    sys.exit(code if code else (1 if fails else 0))
