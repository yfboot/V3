#!/usr/bin/env python3
"""Query npm registry for @babel/plugin-transform-named-capturing-groups-regex versions."""
import json
import urllib.request

url = "https://registry.npmjs.org/@babel/plugin-transform-named-capturing-groups-regex"
with urllib.request.urlopen(url, timeout=15) as r:
    d = json.loads(r.read().decode())

versions = list(d.get("versions", {}).keys())

def key(v):
    try:
        parts = v.replace("-", ".").split(".")
        nums = [int(x) for x in parts[:3] if x.isdigit()]
        return tuple(nums + [0] * (3 - len(nums)))
    except Exception:
        return (0, 0, 0)

versions.sort(key=key)
print("Total versions:", len(versions))
print("Last 20 versions:", versions[-20:])
print("Has 7.29.0:", "7.29.0" in versions)
print("Any 7.29.x:", [v for v in versions if v.startswith("7.29")])
print("dist-tags:", d.get("dist-tags", {}))
