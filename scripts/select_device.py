"""Choisit un iPhone simulé disponible sur le Mac GitHub et écrit UDID/NAME/IOS dans $GITHUB_ENV.

Usage : python3 scripts/select_device.py big|small
"""
import json
import os
import subprocess
import sys

PREFS = {
    "big": ["iPhone 15", "iPhone 16", "iPhone 16 Pro", "iPhone 17", "iPhone 15 Pro"],
    "small": ["iPhone SE (3rd generation)", "iPhone SE (2nd generation)"],
}

size = sys.argv[1]
data = json.loads(subprocess.check_output(["xcrun", "simctl", "list", "devices", "available", "-j"]))
candidates = []
for runtime, devices in data["devices"].items():
    if "iOS" not in runtime:
        continue
    version = runtime.split("iOS-")[-1].replace("-", ".")
    for d in devices:
        candidates.append((d["name"], tuple(int(x) for x in version.split(".")), version, d["udid"]))

for wanted in PREFS[size]:
    matches = sorted([c for c in candidates if c[0] == wanted], key=lambda c: c[1], reverse=True)
    if matches:
        name, _, version, udid = matches[0]
        break
else:
    print("Aucun simulateur trouvé. Disponibles :", sorted({c[0] for c in candidates}))
    sys.exit(1)

print(f"Simulateur choisi : {name} (iOS {version}) {udid}")
with open(os.environ["GITHUB_ENV"], "a") as f:
    f.write(f"UDID={udid}\nDEVICE_NAME={name}\nIOS_VERSION={version}\n")
