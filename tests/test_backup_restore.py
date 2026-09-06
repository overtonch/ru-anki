"""The off-machine backup is only worth anything if it actually restores. These
guard the export→rebuild round-trip against silent schema drift."""
import json
import sqlite3

import backup
import rebuild_db
import store


def test_git_table_queries_are_valid_against_the_current_schema(db):
    """Every GIT_TABLES SELECT must still run — catches a renamed/dropped column
    in a backup query before it matters."""
    c = store.connect()
    for name, query in backup.GIT_TABLES:
        try:
            c.execute(query).fetchone()
        except sqlite3.OperationalError as e:
            raise AssertionError(f"backup query for {name!r} is broken: {e}")
    c.close()


def test_backup_and_rebuild_agree_on_the_table_list(db):
    exported = {name for name, _ in backup.GIT_TABLES}
    loaded = set(rebuild_db.TABLES)
    assert exported == loaded, (
        f"only exported: {exported - loaded}; only in rebuild: {loaded - exported}")


def test_round_trip_preserves_rows(client, db, tmp_path):
    # make some real data across the important tables
    import srs
    cid = srs.create_card("Он пьёт кофе каждое утро.", "кофе", "кофе", False,
                          "coffee")["id"]
    srs.review(cid, 3)
    sid = client.post("/reading/sessions", json={"topic": "город"}).json()["id"]
    client.post(f"/reading/sessions/{sid}/tap", json={"surface": "шумный"})
    client.get("/proficiency")                       # writes a snapshot

    # export exactly like the git backup does
    src = store.connect()
    counts = {}
    for name, query in backup.GIT_TABLES:
        rows = [dict(r) for r in src.execute(query)]
        counts[name] = len(rows)
        (tmp_path / f"{name}.ndjson").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
            encoding="utf-8")
    ddl = ";\n".join(r[0] for r in src.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND type IN ('table','index') "
        "AND name NOT LIKE 'sqlite_%'"))
    src.close()

    # rebuild a fresh DB from the exports (schema + rebuild_db.insert)
    out = sqlite3.connect(":memory:")
    out.executescript(ddl)
    out.execute("PRAGMA foreign_keys=OFF")
    for t in rebuild_db.TABLES:
        recs = rebuild_db.rows(str(tmp_path / f"{t}.ndjson"))
        rebuild_db.insert(out, t, recs)
    out.commit()

    for t in ("srs_cards", "srs_reviews", "reading_flow_sessions",
              "reading_flow_chunks", "reading_flow_unknown", "proficiency_snapshots"):
        got = out.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert got == counts[t] > 0 if t in ("srs_cards", "srs_reviews") else got == counts[t]
    assert out.execute("SELECT translation FROM srs_cards WHERE id=?", (cid,)).fetchone()[0] == "coffee"
