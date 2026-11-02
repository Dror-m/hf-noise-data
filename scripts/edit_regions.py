#!/usr/bin/env python3
"""Add or remove a Geofabrik region in regions.json.

Usage: edit_regions.py add <geofabrik/path> [display name]
       edit_regions.py remove <geofabrik/path or id>
"""
import json
import re
import sys

PATH = "regions.json"


def main():
    op, path = sys.argv[1], sys.argv[2].strip().strip("/")
    if not re.fullmatch(r"[a-z0-9\-]+(/[a-z0-9\-]+)*", path):
        sys.exit(f"invalid region path: {path!r}")
    name = (sys.argv[3] if len(sys.argv) > 3 else "").strip() or path.split("/")[-1]
    regions = json.load(open(PATH, encoding="utf-8"))
    rid = path.split("/")[-1]
    if op == "add":
        if any(r["path"] == path for r in regions):
            print("already present")
            return
        regions.append({"id": rid, "path": path, "name": name})
    elif op == "remove":
        regions = [r for r in regions if r["path"] != path and r["id"] != path]
    else:
        sys.exit("op must be add or remove")
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(regions, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"{op}: {path}")


if __name__ == "__main__":
    main()
