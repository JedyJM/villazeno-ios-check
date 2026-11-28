"""Pop-up ZENO2027 (?zeno=1) dans le vrai Safari iOS (simulateur Apple), via Appium.

Vérifie, en FR et en EN : visibilité, titre et sous-titre sur une ligne,
copie de ZENO2027, href WhatsApp, fermeture, aucun débordement.
"""
import json
import os
import sys
import time
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from appium import webdriver
from appium.options.ios import XCUITestOptions
from appium.webdriver.common.appiumby import AppiumBy

BASE = os.environ.get("BASE_URL", "").rstrip("/")
BYPASS = os.environ.get("VERCEL_PROTECTION_BYPASS", "")
OUT = os.environ.get("OUT", "results")
LABEL = os.environ.get("SIZE", "device")  # big | small
os.makedirs(OUT, exist_ok=True)

# href WhatsApp identiques au commit 0626d5c (js/zeno-promo.js)
HREF = {
    "fr": "https://wa.me/212661147610?text=Bonjour%20Villa%20Zen'O%2C%20j'ai%20le%20code%20ZENO2027.%20Mes%20dates%20%3A%20%E2%80%A6%20Nombre%20de%20personnes%20%3A%20%E2%80%A6",
    "en": "https://wa.me/212661147610?text=Hello%20Villa%20Zen'O%2C%20I%20have%20the%20code%20ZENO2027.%20My%20dates%3A%20%E2%80%A6%20Number%20of%20people%3A%20%E2%80%A6",
}
PATHS = {"fr": "/", "en": "/en/"}
COPY_LABEL = {"fr": "Copier le code", "en": "Copy code"}
COPIED_LABEL = {"fr": "Copié", "en": "Copied"}

MEASURE = r"""
const d = document.getElementById("zeno-promo");
const inner = d ? d.querySelector(".zeno-promo-inner") : null;
function lines(el) {
  if (!el) return 0;
  const range = document.createRange();
  range.selectNodeContents(el);
  const tops = [];
  for (const r of range.getClientRects()) {
    if (!r.width && !r.height) continue;
    const t = Math.round(r.top);
    if (!tops.some((x) => Math.abs(x - t) <= 1)) tops.push(t);
  }
  return tops.length;
}
if (!d) return { exists: false, open: false, visible: false };
const r = d.getBoundingClientRect();
const vw = window.innerWidth, vh = window.innerHeight;
const outside = [];
for (const n of d.querySelectorAll("*")) {
  const b = n.getBoundingClientRect();
  if (!b.width && !b.height) continue;
  if (b.left < r.left - 0.5 || b.right > r.right + 0.5 || b.left < -0.5 || b.right > vw + 0.5 || b.top < -0.5 || b.bottom > vh + 0.5) {
    outside.push(n.tagName + "." + (n.className || ""));
  }
}
let minFont = Infinity;
for (const n of d.querySelectorAll("*")) {
  if (![...n.childNodes].some((c) => c.nodeType === 3 && c.textContent.trim())) continue;
  const f = parseFloat(getComputedStyle(n).fontSize);
  if (f < minFont) minFont = f;
}
const targets = [...d.querySelectorAll("button, a[href]")].map((n) => {
  const b = n.getBoundingClientRect();
  return { el: n.className, w: b.width, h: b.height };
});
return {
  exists: true,
  open: !!d.open,
  visible: !!(d.open && r.height > 0),
  title: (d.querySelector("#zeno-promo-title") || {}).textContent || "",
  lead: (d.querySelector(".zeno-promo-lead") || {}).textContent || "",
  titleLines: lines(d.querySelector("#zeno-promo-title")),
  leadLines: lines(d.querySelector(".zeno-promo-lead")),
  href: (d.querySelector(".zeno-promo-cta") || {}).getAttribute("href") || "",
  copyText: (d.querySelector(".zeno-promo-copy") || {}).textContent || "",
  minFont,
  targets,
  innerOverflowY: inner ? (inner.scrollHeight - inner.clientHeight) : 0,
  innerOverflowX: inner ? (inner.scrollWidth - inner.clientWidth) : 0,
  outside,
  bodyHasProtection: /Protected by Vercel|vercel.com\/login/i.test(document.body.innerText || ""),
};
"""

INSTALL_COPY_SPY = r"""
window.__copied = [];
if (navigator.clipboard && navigator.clipboard.writeText) {
  const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
  navigator.clipboard.writeText = function (t) {
    window.__copied.push(String(t));
    return orig(t);
  };
}
return true;
"""


def clean_url(lang):
    return BASE + PATHS[lang] + "?zeno=1"


def boot_url(lang):
    u = clean_url(lang)
    if not BYPASS:
        return u
    # Set the bypass cookie, then we navigate to the clean URL before any screenshot.
    sep = "&" if "?" in u else "?"
    return u + sep + "x-vercel-protection-bypass=" + BYPASS + "&x-vercel-set-bypass-cookie=samesitenone"


def web(d):
    for c in d.contexts:
        if c.startswith("WEBVIEW"):
            d.switch_to.context(c)
            return
    raise RuntimeError(f"Pas de contexte web : {d.contexts}")


def native(d):
    d.switch_to.context("NATIVE_APP")


def shot(d, name):
    path = os.path.join(OUT, f"{LABEL}-{name}.png")
    d.get_screenshot_as_file(path)
    return path


def dismiss_safari_tips(d):
    native(d)
    pred = (
        "type == 'XCUIElementTypeButton' AND (name IN {'Close','Fermer','Not Now','Continue','OK','Dismiss','xmark','close'} "
        "OR name CONTAINS[c] 'close' OR name CONTAINS[c] 'dismiss' OR label CONTAINS[c] 'close' OR label CONTAINS[c] 'fermer')"
    )
    for _ in range(4):
        tip = d.find_elements(
            AppiumBy.IOS_PREDICATE,
            "label CONTAINS[c] 'Bookmarks' OR label CONTAINS[c] 'Signets' OR label CONTAINS[c] 'Safari settings'",
        )
        clicked = False
        for el in d.find_elements(AppiumBy.IOS_PREDICATE, pred):
            try:
                el.click()
                clicked = True
                time.sleep(0.6)
            except Exception:
                pass
        if not tip and not clicked:
            break
        if tip and not clicked:
            r = d.get_window_size()
            d.execute_script("mobile: tap", {"x": r["width"] * 0.5, "y": 8})
            time.sleep(0.6)
        time.sleep(0.8)
    web(d)


results = {
    "device": os.environ.get("DEVICE_NAME"),
    "ios": os.environ.get("IOS_VERSION"),
    "size": LABEL,
    "cases": {},
}
failures = []


def check(case, name, ok, detail=""):
    results["cases"].setdefault(case, {"checks": []})["checks"].append(
        {"check": name, "ok": bool(ok), "detail": detail}
    )
    if not ok:
        failures.append(f"{case} : {name} {detail}")
    print(("OK   " if ok else "ÉCHEC"), case, "-", name, detail, flush=True)


if not BASE:
    print("BASE_URL manquante")
    sys.exit(2)

opts = XCUITestOptions()
opts.platform_version = os.environ["IOS_VERSION"]
opts.device_name = os.environ["DEVICE_NAME"]
opts.udid = os.environ["UDID"]
opts.browser_name = "Safari"
opts.set_capability("appium:nativeWebTap", True)
opts.set_capability("appium:connectHardwareKeyboard", False)
opts.set_capability("appium:safariInitialUrl", boot_url("fr"))
opts.set_capability("appium:wdaLaunchTimeout", 300000)
opts.set_capability("appium:simulatorStartupTimeout", 600000)
opts.set_capability("appium:wdaConnectionTimeout", 300000)
opts.set_capability("appium:webviewConnectTimeout", 90000)
opts.set_capability("appium:webviewConnectRetries", 20)
opts.set_capability("appium:newCommandTimeout", 600)

try:
    from appium.webdriver.client_config import AppiumClientConfig
    _cfg = AppiumClientConfig(remote_server_addr="http://127.0.0.1:4723", timeout=900)
    d = webdriver.Remote(options=opts, client_config=_cfg)
except ImportError:
    d = webdriver.Remote("http://127.0.0.1:4723", options=opts)

try:
    web(d)
    time.sleep(2)
    dismiss_safari_tips(d)
    # Cookie de bypass posé (si secret présent) : on enchaîne sur l'URL propre.
    first = True
    for lang in ("fr", "en"):
        case = f"{lang}-{LABEL}"
        web(d)
        if first and BYPASS:
            # Revenir à l'URL sans le paramètre de bypass avant les captures.
            d.get(clean_url(lang))
            first = False
        else:
            d.get(clean_url(lang))
        time.sleep(2.5)
        dismiss_safari_tips(d)

        # Attendre la pop-up
        deadline = time.time() + 20
        m = {"exists": False}
        while time.time() < deadline:
            try:
                m = d.execute_script(MEASURE) or {}
            except Exception as e:
                m = {"exists": False, "error": str(e)}
            if m.get("visible"):
                break
            time.sleep(0.5)

        shot(d, f"{lang}-open")
        check(case, "pop-up visible", m.get("visible"), f"(exists={m.get('exists')} open={m.get('open')} protection={m.get('bodyHasProtection')})")
        check(case, "titre sur une ligne", m.get("titleLines") == 1, f"(lignes={m.get('titleLines')} texte={m.get('title')!r})")
        check(case, "sous-titre sur une ligne", m.get("leadLines") == 1, f"(lignes={m.get('leadLines')} texte={m.get('lead')!r})")
        check(case, "href WhatsApp identique", m.get("href") == HREF[lang], f"(obtenu={m.get('href')!r})")
        check(case, "aucun débordement", (m.get("innerOverflowY") or 0) <= 1 and (m.get("innerOverflowX") or 0) <= 1 and not m.get("outside"),
              f"(overflowY={m.get('innerOverflowY')} overflowX={m.get('innerOverflowX')} outside={m.get('outside')})")
        check(case, "texte ≥ 12 px", (m.get("minFont") or 0) >= 12, f"(minFont={m.get('minFont')})")
        small = [t for t in (m.get("targets") or []) if t["w"] < 44 or t["h"] < 44]
        check(case, "cibles ≥ 44 px", not small, f"{small}")

        # Copie du code
        copy_method = "aucun"
        copied_ok = False
        try:
            d.execute_script(INSTALL_COPY_SPY)
            btn = d.find_element(AppiumBy.CSS_SELECTOR, "#zeno-promo .zeno-promo-copy")
            btn.click()
            time.sleep(1.2)
            after = d.execute_script(
                "return { copied: window.__copied || [], label: (document.querySelector('#zeno-promo .zeno-promo-copy')||{}).textContent || '' };"
            ) or {}
            hooked = after.get("copied") or []
            label = after.get("label") or ""
            if hooked and hooked[0] == "ZENO2027":
                copied_ok = True
                copy_method = "valeur passée à navigator.clipboard.writeText"
            elif label.strip() == COPIED_LABEL[lang]:
                # iOS simulateur peut interdire la lecture du presse-papiers ; le libellé confirme le succès côté page.
                copied_ok = True
                copy_method = "retour visuel (« " + COPIED_LABEL[lang] + " »), presse-papiers non lisible"
            results["cases"].setdefault(case, {})["copy"] = {"method": copy_method, "hooked": hooked, "label": label}
        except Exception as e:
            copy_method = "erreur: " + type(e).__name__
            results["cases"].setdefault(case, {})["copy"] = {"method": copy_method, "error": str(e)}
        check(case, "copie ZENO2027", copied_ok, f"({copy_method})")
        results["copy_method"] = results.get("copy_method") or copy_method
        shot(d, f"{lang}-after-copy")

        # Fermeture
        try:
            d.find_element(AppiumBy.CSS_SELECTOR, "#zeno-promo .zeno-promo-close").click()
            time.sleep(1)
            gone = d.execute_script("return !document.getElementById('zeno-promo');")
        except Exception as e:
            gone = False
            results["cases"].setdefault(case, {})["close_error"] = str(e)
        shot(d, f"{lang}-closed")
        check(case, "fermeture", gone)
        results["cases"].setdefault(case, {})["measure"] = m
finally:
    results["failures"] = failures
    results["copy_methods"] = {
        c: (results["cases"].get(c) or {}).get("copy", {})
        for c in results["cases"]
    }
    with open(os.path.join(OUT, f"{LABEL}-results.json"), "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    try:
        d.quit()
    except Exception:
        pass

print(f"\n{len(failures)} échec(s)")
sys.exit(1 if failures else 0)
