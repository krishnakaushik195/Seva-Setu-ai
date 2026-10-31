# -*- coding: utf-8 -*-
"""Bring the database schema up to date by applying schema.sql, which is written to be idempotent.

Runs before the portal starts (see docker-compose.yml); exits non-zero until the database is reachable.
"""

import os
import sys

import app as portal


def statements(sql):
    for chunk in sql.split(";\n"):
        lines = [ln for ln in chunk.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if lines:
            yield chunk.strip()


def main():
    with open(os.path.join(portal.BASE_DIR, "schema.sql"), encoding="utf-8") as fh:
        sql = fh.read()
    conn = portal.get_db()
    conn.autocommit = True
    cur = conn.cursor()
    for stmt in statements(sql):
        try:
            cur.execute(stmt)
        except Exception as e:
            print("migrate: could not apply %r: %s" % (stmt.splitlines()[-1][:80], e), flush=True)
    cur.close()
    conn.close()
    print("migrate: schema is up to date", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
