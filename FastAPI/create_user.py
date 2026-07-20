"""Create (or add another) login user for TaskSnap.

There's no web route for this — same reasoning as portfolio-management
and the other three apps: auth material, admin-only, console/SSH access
only. Run this once to create the first user before relying on the login
gate (AuthGuard stays open until at least one active user exists — see
main.py's AuthGuard / crud.any_active_users).

Usage:
    /home/node/venvs/tasksnap/bin/python create_user.py
"""

import getpass

import crud
import database


def main():
    database.init_db()
    username = input("Username: ").strip()
    if not username:
        print("Username cannot be empty.")
        return
    password = getpass.getpass("Password: ")
    if not password:
        print("Password cannot be empty.")
        return
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords didn't match.")
        return

    conn = database.get_connection()
    try:
        existing = conn.execute(
            "SELECT 1 FROM user_record_table WHERE user_name = ?", (username,)
        ).fetchone()
        if existing:
            print(f"A user named '{username}' already exists.")
            return
        user_id = crud.create_user(conn, username, password)
    finally:
        conn.close()

    print(f"Created user '{username}' ({user_id}). Login is now enforced for this app.")


if __name__ == "__main__":
    main()
