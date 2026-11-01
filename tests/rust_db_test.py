import os
import subprocess
import sys

import harness


def main():
    binary = harness.runfile("MPAC_RUST_DB_TEST_BIN")
    with harness.postgres() as pg:
        env = os.environ | {
            "MPAC_TEST_PG_HOST": pg.host,
            "MPAC_TEST_PG_PORT": str(pg.port),
            "MPAC_TEST_PG_USER": pg.user,
            "MPAC_TEST_PG_PASSWORD": pg.password,
        }
        return subprocess.run([binary, *sys.argv[1:]], env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
