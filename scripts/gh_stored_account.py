"""Run gh using its OS credential store, without inherited token overrides.

Does not read or print credentials and does not change the parent environment.
"""
import os
import subprocess
import sys


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    if args[:2] in (["auth", "token"], ["auth", "setup-git"]):
        raise SystemExit("Credential export/setup is outside this wrapper's scope")
    env = os.environ.copy()
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(name, None)
    return subprocess.call(["gh", *args], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
