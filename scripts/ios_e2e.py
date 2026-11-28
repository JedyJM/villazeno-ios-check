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
    # Commande officielle de GitHub Actions : la ligne n'est pas affichée,
    # la valeur est ensuite remplacée par *** dans tout le journal.
    for v in vals:
        if v:
            print(f"::add-mask::{v}", flush=True)


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


def tap(sel, timeout=15):
    """Vrai toucher (nativeWebTap) sur le premier élément visible."""
    end = time.time() + timeout
    e = None
    while time.time() < end and e is None:
        e = visible(sel)
        if e is None:
            time.sleep(0.5)
    if e is None:
        raise AssertionError(f"élément introuvable : {sel}")
    js("arguments[0].scrollIntoView({block:'center', inline:'nearest'});", e)
    time.sleep(0.4)
    e.click()
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
        "const p = typeof VZApi.urlReady==='function' ? VZApi.urlReady() : Promise.resolve(VZApi.URL);"
        "p.then(()=>done({ok:true, env:VZApi.env, prod: VZApi.URL===VZApi.PROD_URL, same: VZApi.URL===arguments[0]}))"
        ".catch(e=>done({ok:false, why:String(e)}));", EXEC)
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
       "while((n=w.nextNode())){ const t=n.data||''; if((h && t.indexOf(h)>=0) || /[?&]k=/.test(t)){"
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
    d.get(BASE + "/api/vz-env")
    time.sleep(1.5)
    on_vercel_login()
    txt = js("return document.body ? document.body.innerText : '';") or ""
    try:
        env2 = json.loads(txt)
    except Exception:  # noqa: BLE001
        env2 = {}
    check("Safari : /api/vz-env chauffé et « staging »", env2.get("env") == "staging", "(page d'accès Vercel ?)")
    # Le séjour : posé dans le localStorage comme le fait la page avec ?stay=&k= (la clé n'apparaît jamais dans l'adresse).
    go("/guest/acces.html")
    js("localStorage.setItem('villazeno.stay', arguments[0]); localStorage.setItem('villazeno.stayKey', arguments[1]);", DEMO, GUEST_K)
    prod = js("return window.VZApi ? VZApi.PROD_URL : '';")
    check("le /exec des contrôles n'est PAS celui de la production", prod and prod != EXEC)


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
    type_real("[name='nom']", NOM)
    set_field("[name='telephone']", "+33 6 00 00 00 11")
    set_field("[name='arrival']", "2028-11-10")
    set_field("[name='departure']", "2028-11-14")
    tap("[data-source='Direct']")
    tap("[data-step='kids4to11'][data-dir='1']")
    set_field("[name='tarif']", "1000")
    blur_active()
    shot("A1-01-nouveau")
    tap("[data-save]")
    wait_js("document.querySelector('.form-ok')", 60, "séjour enregistré (.form-ok)")
    s = None
    for _ in range(10):
        s = next((x for x in (exec_get({"action": "stays"}).get("stays") or []) if x.get("nom") == NOM), None)
        if s:
            break
        time.sleep(3)
    check("séjour créé côté serveur", s is not None)
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
    tap("[data-stop]")
    wait_js("/désactivé/i.test((document.querySelector('.link-until')||{}).textContent||'')", 40, "lien désactivé")
    shot("A2-01-desactive")
    go("/guest/demandes.html")  # localStorage = séjour A1
    wait_js("/expire\\.html/.test(location.pathname)", 40, "redirection vers expire.html")
    shot("A2-02-lien-refuse")


def t_A3():
    if not A1S:
        raise Skip("A1 n'a pas créé de séjour")
    go("/admin/sejour.html?id=" + urllib.parse.quote(A1S["id"]))
    tap("[data-prolong]")
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
    fails = [k for k, v in results["tests"].items() if v["status"] == "fail"]
    log(f"\nEnvois : {results['submitted']} / {MAX_SENDS} — échecs : {fails or 'aucun'}")
    sys.exit(code if code else (1 if fails else 0))
