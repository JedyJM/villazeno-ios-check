"""Vérification des pages dans le vrai Safari d'iOS (Simulateur Apple), via Appium.

Pour chaque page : capture, la page tient-elle sur un écran, pas de zoom.
Sur Restauration : vrais touchers sur + / −, toucher du champ Allergies,
frappe au vrai clavier iOS, défilement, sortie du champ, et contrôle de la
barre du bas à chaque étape.
"""
import json
import os
import sys
import time

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
PAGES = os.environ.get("PAGES", "tr-aeroport.html,tr-modifier.html,tr-ville.html,tr-location.html,exp-quad.html,exp-cheval.html,ms-massages.html,ms-mains.html,ms-epilation.html").split(",")
OUT = os.environ.get("OUT", "results")
LABEL = os.environ.get("SIZE", "device")
os.makedirs(OUT, exist_ok=True)

METRICS = r"""
const d = document.documentElement, vv = window.visualViewport;
const r = s => { const e = document.querySelector(s); return e ? e.getBoundingClientRect().toJSON() : null; };
const screen = document.querySelector('.screen');
return {
  scrollH: d.scrollHeight, innerH: window.innerHeight, innerW: window.innerWidth,
  scrollY: window.scrollY,
  vvH: vv ? vv.height : null, vvTop: vv ? vv.offsetTop : null, scale: vv ? vv.scale : 1, zoom: Math.round(d.clientWidth / window.innerWidth * 100) / 100, fonts: document.fonts ? document.fonts.status : null,
  screenOverflow: screen ? screen.scrollHeight - screen.clientHeight : 0,
  nav: r('.nav'), note: r('.note, .note2'), btn: r('.btn'),
  active: document.activeElement ? document.activeElement.className : null,
  debord: (() => { const out = []; document.querySelectorAll('.tile, .quick a').forEach(card => { const cb = card.getBoundingClientRect(); card.querySelectorAll('h3, p, strong, span').forEach(e => { const r = document.createRange(); r.selectNodeContents(e); const b = r.getBoundingClientRect(); if (b.width && (b.right > cb.right - 4 || b.bottom > cb.bottom - 2 || b.left < cb.left + 2)) out.push(e.textContent.trim()); }); }); return out; })(),
  overlaps: [...document.querySelectorAll('.tile')].filter(t => {
    const pl = t.querySelector('.plume, .ico'); if (!pl) return false; const a = pl.getBoundingClientRect();
    return [...t.querySelectorAll('h3, p')].some(e => { const r = document.createRange(); r.selectNodeContents(e); const b = r.getBoundingClientRect();
      return !(b.right <= a.left || b.left >= a.right || b.bottom <= a.top || b.top >= a.bottom); });
  }).map(t => t.querySelector('h3').textContent)
};
"""

opts = XCUITestOptions()
opts.platform_version = os.environ["IOS_VERSION"]
opts.device_name = os.environ["DEVICE_NAME"]
opts.udid = os.environ["UDID"]
opts.browser_name = "Safari"
opts.set_capability("appium:nativeWebTap", True)          # vrais touchers, pas des clics JavaScript
opts.set_capability("appium:connectHardwareKeyboard", False)
opts.set_capability("appium:forceSimulatorSoftwareKeyboardPresence", True)
opts.set_capability("appium:safariInitialUrl", f"{BASE}/{PAGES[0]}")
opts.set_capability("appium:wdaLaunchTimeout", 300000)
opts.set_capability("appium:simulatorStartupTimeout", 600000)
opts.set_capability("appium:wdaConnectionTimeout", 300000)
opts.set_capability("appium:webviewConnectTimeout", 90000)
opts.set_capability("appium:webviewConnectRetries", 20)
opts.set_capability("appium:newCommandTimeout", 600)

results = {"device": os.environ["DEVICE_NAME"], "ios": os.environ["IOS_VERSION"], "pages": {}}
failures = []


def web(d):
    for c in d.contexts:
        if c.startswith("WEBVIEW"):
            d.switch_to.context(c)
            return
    raise RuntimeError(f"Pas de contexte web : {d.contexts}")


def native(d):
    d.switch_to.context("NATIVE_APP")


def shot(d, name):
    path = f"{OUT}/{LABEL}-{name}.png"
    d.get_screenshot_as_file(path)
    return path


def keyboard_shown(d):
    try:
        return bool(d.is_keyboard_shown())
    except Exception:
        native(d)
        found = d.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeKeyboard")
        web(d)
        return bool(found)


def no_zoom(m):
    return m.get("scale") in (None, 1, 1.0) and abs((m.get("zoom") or 1) - 1) < 0.02


def dismiss_safari_tips(d):
    """Ferme les bulles d'aide de Safari (premier lancement), qui masquent le bas de l'écran."""
    native(d)
    pred = ("type == 'XCUIElementTypeButton' AND (name IN {'Close','Fermer','Not Now','Continue','OK','Dismiss','xmark','close'} "
            "OR name CONTAINS[c] 'close' OR name CONTAINS[c] 'dismiss' OR label CONTAINS[c] 'close' OR label CONTAINS[c] 'fermer')")
    for _ in range(4):
        tip = d.find_elements(AppiumBy.IOS_PREDICATE, "label CONTAINS[c] 'Bookmarks' OR label CONTAINS[c] 'Signets' OR label CONTAINS[c] 'Safari settings'")
        clicked = False
        for el in d.find_elements(AppiumBy.IOS_PREDICATE, pred):
            try:
                el.click(); clicked = True; time.sleep(0.6)
            except Exception:
                pass
        if not tip and not clicked:
            break
        if tip and not clicked:
            # Bulle sans bouton reconnu : on journalise sa structure et on touche hors bulle (en haut de l'écran)
            try:
                os.makedirs(OUT, exist_ok=True)
                open(os.path.join(OUT, f"{LABEL}-tip-source.xml"), "w").write(d.page_source)
            except Exception:
                pass
            r = d.get_window_size()
            d.execute_script("mobile: tap", {"x": r["width"] * 0.5, "y": 8})
            time.sleep(0.6)
        time.sleep(0.8)
    web(d)


def check(page, name, ok, detail=""):
    results["pages"].setdefault(page, {"checks": []})["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
    if not ok:
        failures.append(f"{page} : {name} {detail}")
    print(("OK   " if ok else "ÉCHEC"), page, "-", name, detail, flush=True)


try:
    from appium.webdriver.client_config import AppiumClientConfig
    _cfg = AppiumClientConfig(remote_server_addr="http://127.0.0.1:4723", timeout=900)
    d = webdriver.Remote(options=opts, client_config=_cfg)
except ImportError:
    d = webdriver.Remote("http://127.0.0.1:4723", options=opts)
try:
    for page in PAGES:
        web(d)
        d.get(f"{BASE}/{page}")
        time.sleep(2)
        dismiss_safari_tips(d)
        m0 = d.execute_script(METRICS)
        shot(d, page.replace(".html", "") + "-1-initial")
        check(page, "tient sur un écran", m0["scrollH"] <= m0["innerH"] + 1 and m0.get("screenOverflow", 0) <= 1,
              f"(page {m0['scrollH']} px, écran {m0['innerH']} px)")
        check(page, "pas de zoom au chargement", no_zoom(m0), f"(échelle {m0.get('scale')}, zoom {m0.get('zoom')})")
        if page.startswith("accueil"):
            check(page, "textes dans leurs cartes (aucun débordement)", not m0.get("debord"), f"({', '.join(m0.get('debord') or [])})")
            check(page, "dessins sans chevauchement avec les textes", not m0.get("overlaps"), f"({', '.join(m0.get('overlaps') or [])})")
        results["pages"][page]["initial"] = m0

        if not page.startswith(("restauration", "services", "piscine", "tr-", "exp-", "ms-")):
            continue
        stem = page.replace(".html", "")

        m1 = None
        # 1. Touchers rapides sur + puis − : aucun zoom ne doit se déclencher
        pl = d.find_elements(AppiumBy.CSS_SELECTOR, ".stepper button:last-child")
        mi = d.find_elements(AppiumBy.CSS_SELECTOR, ".stepper button:first-child")
        if pl and mi:
            for _ in range(3):
                pl[-1].click()
            mi[-1].click()
            time.sleep(1)
            m1 = d.execute_script(METRICS)
            check(page, "pas de zoom après + / −", no_zoom(m1), f"(échelle {m1.get('scale')}, zoom {m1.get('zoom')})")

        # 2. Toucher du champ Allergies : le vrai clavier iOS s'ouvre
        d.find_element(AppiumBy.CSS_SELECTOR, ".note, .note2").click()
        time.sleep(1.5)
        check(page, "clavier iOS ouvert", keyboard_shown(d))
        native(d)
        field = d.find_elements(AppiumBy.IOS_PREDICATE, "hasKeyboardFocus == 1")
        if field:
            target = field[0]
        else:
            cands = d.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeTextField") + d.find_elements(AppiumBy.CLASS_NAME, "XCUIElementTypeTextView")
            target = cands[0]
        target.send_keys("Sans gluten pour un enfant")   # frappe au vrai clavier
        web(d)
        time.sleep(1)
        m2 = d.execute_script(METRICS)
        shot(d, stem + "-2-clavier")
        check(page, "pas de zoom en tapant", no_zoom(m2), f"(échelle {m2.get('scale')}, zoom {m2.get('zoom')})")
        if m2.get("note") and m2.get("vvH") is not None:
            n = m2["note"]
            # Selon la version d'iOS, les coordonnées sont relatives à la zone visible ou à la page
            visible = (n["top"] >= -1 and n["bottom"] <= m2["vvH"] + 1) or \
                      (n["top"] >= m2["vvTop"] - 1 and n["bottom"] <= m2["vvTop"] + m2["vvH"] + 1)
            check(page, "champ Allergies visible au-dessus du clavier", visible,
                  f"(champ {m2['note']['top']:.0f}–{m2['note']['bottom']:.0f}, zone visible {m2['vvTop']:.0f}–{m2['vvTop'] + m2['vvH']:.0f})")
        if m2.get("note") and m2.get("btn"):
            check(page, "champ et bouton ne se chevauchent pas", m2["note"]["bottom"] <= m2["btn"]["top"],
                  f"(écart {m2['btn']['top'] - m2['note']['bottom']:.0f} px)")

        # 3. Défilement avec le clavier ouvert (vrai geste)
        native(d)
        d.execute_script("mobile: swipe", {"direction": "up"})
        time.sleep(1)
        d.execute_script("mobile: swipe", {"direction": "down"})
        time.sleep(1)
        web(d)
        m3 = d.execute_script(METRICS)
        shot(d, stem + "-3-defilement")

        # 4. Sortie du champ : toucher le titre, le clavier se ferme
        d.find_element(AppiumBy.CSS_SELECTOR, ".page-title").click()
        time.sleep(1.5)
        m4 = d.execute_script(METRICS)
        shot(d, stem + "-4-sortie")
        check(page, "clavier fermé après sortie du champ", not keyboard_shown(d))
        check(page, "pas de zoom après sortie du champ", no_zoom(m4), f"(échelle {m4.get('scale')}, zoom {m4.get('zoom')})")
        n0, n4 = m0["nav"], m4["nav"]
        same = abs(n0["top"] - n4["top"]) <= 1 and abs(n0["bottom"] - n4["bottom"]) <= 1
        check(page, "barre du bas revenue exactement à sa place", same,
              f"(avant {n0['top']:.0f}–{n0['bottom']:.0f}, après {n4['top']:.0f}–{n4['bottom']:.0f})")
        check(page, "page revenue en haut (pas de décalage)", abs(m4.get("scrollY") or 0) <= 1, f"(décalage {m4.get('scrollY')} px)")
        results["pages"][page].update({"apres_plus_moins": m1, "clavier": m2, "defilement": m3, "sortie": m4})
finally:
    results["failures"] = failures
    with open(f"{OUT}/{LABEL}-results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    d.quit()

print(f"\n{len(failures)} échec(s)")
sys.exit(1 if failures else 0)
