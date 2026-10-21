"""Multiuser management CLI (intranet, passwordless).

Run on the machine that hosts the data directory:

  python -m backend.manage_users list
  python -m backend.manage_users create "Customer One" "Customer Two"
  python -m backend.manage_users assign "Customer One" --all          # adopt all ownerless books
  python -m backend.manage_users assign "Customer One" <book-id-1> <book-id-2>
  python -m backend.manage_users set-default "Customer One"

The FIRST user ever created becomes the default user (fallback for
header-less requests and the one-shot legacy adoption at server start).
Respect STORYFORGE_DATA if the server uses a custom data root.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.store.project import BookStore  # noqa: E402
from backend.store.users import UserStore  # noqa: E402


def _data_root() -> Path:
    import os

    root = os.environ.get("STORYFORGE_DATA")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[1] / "data"


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] not in {"list", "create", "assign", "set-default"}:
        print(__doc__)
        return 2

    data = _data_root()
    users = UserStore(data / "users.json")
    store = BookStore(data)
    cmd = argv[1]

    if cmd == "list":
        all_books = store.list_books()
        counts: dict = {}
        ownerless: list = []
        for row in all_books:
            owner = store.load_book(row["id"]).get("owner_id")
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
            else:
                ownerless.append(row["id"])
        for user in users.list():
            print(f"{user['id']:>10}  {user['name']:<30} books: {counts.get(user['id'], 0)}")
        if ownerless or not counts:
            print(f"{'—':>10}  (no owner)".ljust(43) + f"books: {len(ownerless)}: {', '.join(ownerless)}")
        return 0

    if cmd == "create":
        if not argv[2:]:
            print("create: at least one user name required")
            return 2
        for name in argv[2:]:
            try:
                user = users.create(name)
                was_first = users.default_id() == user["id"]
            except ValueError as exc:
                print(f"SKIP {name}: {exc}")
                continue
            print(f"created {user['id']} {user['name']}" + ("  (default user)" if was_first else ""))
        return 0

    if cmd == "assign":
        if len(argv) < 3:
            print("assign: user name (or id) required")
            return 2
        ident = argv[2]
        user = users.get(ident) or users.find_by_name(ident)
        if user is None:
            user = users.create(ident)
            print(f"created {user['id']} {user['name']}")
        rest = argv[3:]
        if not rest:
            print("assign: pass --all or explicit book ids")
            return 2
        candidates = store.list_books() if "--all" in rest else None
        if candidates is None:
            ids = [a for a in rest if a != "--all"]
            candidates = []
            for bid in ids:
                try:
                    candidates.append(next(c for c in store.list_books() if c["id"] == bid))
                except StopIteration:
                    print(f"SKIP {bid}: no such book")
        changed = 0
        for row in candidates:
            book = store.load_book(row["id"])
            if book.get("owner_id") == user["id"]:
                continue
            book["owner_id"] = user["id"]
            store.save_book(book)
            changed += 1
            print(f"{row['id']} '{row['title']}' -> {user['name']}")
        print(f"assigned {changed} book(s) to {user['name']}")
        return 0

    if cmd == "set-default":
        if len(argv) < 3:
            print("set-default: user name (or id) required")
            return 2
        ident = argv[2]
        user = users.get(ident) or users.find_by_name(ident)
        if user is None:
            print(f"no such user: {ident}")
            return 1
        data_file = users._load()
        data_file["default_id"] = user["id"]
        users._save(data_file)
        print(f"default user is now {user['name']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))