"""Build the verb-government curriculum for the speaking-activation drill.

Takes the ~N most frequent Russian verbs and has the model annotate each with
its government (case / preposition per argument) + example frames + the trap an
English speaker falls into. Writes app/data/activate/verbs.json.gz.

    ./.venv/bin/python build_activate_verbs.py [N]        # default 220
"""
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import db      # noqa: E402
import llm     # noqa: E402
import store   # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 220
BATCH = 22


def top_verbs(n):
    c = store.connect()
    out, seen = [], set()
    for r in c.execute("SELECT normalized_text, rank FROM freq ORDER BY rank LIMIT 6000"):
        w = r["normalized_text"]
        p = db._morph().parse(w)[0]
        if ("INFN" in str(p.tag) or "VERB" in str(p.tag)):
            inf = db.norm(p.normal_form)
            if inf not in seen and len(inf) > 2:
                seen.add(inf)
                out.append((inf, r["rank"]))
        if len(out) >= n:
            break
    c.close()
    return out


def main():
    verbs = top_verbs(N)
    ranks = {v: r for v, r in verbs}
    names = [v for v, _ in verbs]
    result = {}
    for i in range(0, len(names), BATCH):
        chunk = names[i:i + BATCH]
        for attempt in (1, 2):
            try:
                d = llm.verb_government(chunk)
                got = {r["verb"]: r for r in d.get("verbs", []) if r.get("verb")}
                if got:
                    break
            except Exception as e:  # noqa: BLE001
                print(f"  batch {i}: {e}")
                got = {}
        for v in chunk:
            r = got.get(v) or {"verb": v, "gloss": "", "patterns": []}
            r["rank"] = ranks.get(v)
            result[v] = r
        print(f"  {i + len(chunk)}/{len(names)} done")
        time.sleep(0.5)

    rows = [result[v] for v in names if v in result]
    dst = os.path.join("app", "data", "activate", "verbs.json.gz")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with gzip.open(dst, "wt", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
    withgov = sum(1 for r in rows if r.get("patterns"))
    print(f"\n{dst}: {len(rows)} verbs ({withgov} with government), "
          f"{os.path.getsize(dst)} bytes")
    for r in rows[:8]:
        print(" ", r["verb"], "→", "; ".join(p.get("gov", "") for p in r.get("patterns", [])))


if __name__ == "__main__":
    main()
