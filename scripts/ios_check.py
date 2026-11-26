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
PAGES = os.environ.get("PAGES", "accueil.html,accueil-plume.html,restauration.html,villa.html").split(",")
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
  nav: r('.nav'), note: r('.note'), btn: r('.btn'),
  active: document.activeElement ? document.activeElement.className : null
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
    for label in ("Close", "Fermer", "Not Now", "Continue", "OK"):
        for el in d.find_elements(AppiumBy.ACCESSIBILITY_ID, label):
            try:
                el.click()
                time.sleep(0.5)
            except Exception:
                pass
    web(d)


def check(page, name, ok, detail=""):
    results["pages"].setdefault(page, {"checks": []})["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
    if not ok:
        failures.append(f"{page} : {name} {detail}")
    print(("OK   " if ok else "ÉCHEC"), page, "-", name, detail, flush=True)


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
        results["pages"][page]["initial"] = m0

        if page != "restauration.html":
            continue

        # 1. Touchers rapides sur + puis − : aucun zoom ne doit se déclencher
        plus = d.find_element(AppiumBy.CSS_SELECTOR, ".dish .stepper button:last-child")
        minus = d.find_element(AppiumBy.CSS_SELECTOR, ".dish .stepper button:first-child")
        for _ in range(3):
            plus.click()
        minus.click()
        time.sleep(1)
        m1 = d.execute_script(METRICS)
        check(page, "pas de zoom après + / −", no_zoom(m1), f"(échelle {m1.get('scale')}, zoom {m1.get('zoom')})")

        # 2. Toucher du champ Allergies : le vrai clavier iOS s'ouvre
        d.find_element(AppiumBy.CSS_SELECTOR, ".note").click()
        time.sleep(1.5)
        check(page, "clavier iOS ouvert", keyboard_shown(d))
        native(d)
        field = d.find_elements(AppiumBy.IOS_PREDICATE, "hasKeyboardFocus == 1")
        target = field[0] if field else d.find_element(AppiumBy.CLASS_NAME, "XCUIElementTypeTextField")
        target.send_keys("Sans gluten pour un enfant")   # frappe au vrai clavier
        web(d)
        time.sleep(1)
        m2 = d.execute_script(METRICS)
        shot(d, "restauration-2-clavier")
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
        shot(d, "restauration-3-defilement")

        # 4. Sortie du champ : toucher le titre, le clavier se ferme
        d.find_element(AppiumBy.CSS_SELECTOR, ".page-title").click()
        time.sleep(1.5)
        m4 = d.execute_script(METRICS)
        shot(d, "restauration-4-sortie")
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
