from __future__ import annotations

import json

from app.database import SessionLocal, init_db
from app.services.maintenance import prune_runtime_artifacts, vacuum_analyze_database


def main() -> None:
    init_db()
    with SessionLocal() as db:
        summary = prune_runtime_artifacts(db)
        db.commit()

    vacuum_analyze_database()
    print(json.dumps({**summary, "vacuum_analyze_ran": True}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
