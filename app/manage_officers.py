# -*- coding: utf-8 -*-
"""Create or update a named officer account.

    docker compose exec app python manage_officers.py <username> "<Full Name>" "<Block,Block|*>"

The password is read from OFFICER_PASSWORD, or asked for. Decisions are recorded against the username.
"""

import os
import sys
import getpass
from datetime import datetime

from werkzeug.security import generate_password_hash

import app as portal


def main(argv):
    if len(argv) != 4:
        print(__doc__)
        return 2
    username, full_name, blocks = argv[1].strip(), argv[2].strip(), argv[3].strip()
    if username == portal.ADMIN_USERNAME:
        print("%s is the administrator from the environment; choose another username" % username)
        return 2
    if not portal._blocks_from(blocks):
        print("blocks must be * or a comma-separated list of: " + ", ".join(portal.BLOCKS))
        return 2
    password = os.environ.get("OFFICER_PASSWORD") or getpass.getpass("Password for %s: " % username)
    if len(password) < 10:
        print("the password must be at least 10 characters")
        return 2
    conn = portal.get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO officers (username, full_name, password_hash, blocks, active, created_at) "
                "VALUES (%s, %s, %s, %s, TRUE, %s) ON CONFLICT (username) DO UPDATE SET "
                "full_name = EXCLUDED.full_name, password_hash = EXCLUDED.password_hash, "
                "blocks = EXCLUDED.blocks, active = TRUE",
                (username, full_name, generate_password_hash(password), blocks, datetime.now()))
    conn.commit()
    cur.close()
    conn.close()
    print("officer %s (%s) saved for: %s" % (username, full_name, ", ".join(portal._blocks_from(blocks))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
