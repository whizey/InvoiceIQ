"""Fail if sensitive local/assistant-state file *paths* are tracked by Git.

This complements Gitleaks content scanning. It does not inspect previous
commits; old history needs separate review before a public release.
"""

import re
import subprocess
import sys


BLOCKED = re.compile(
    r"(^|/)(?:\.private|\.refact|\.claude|\.cursor|\.codex|"
    r"AGENTS\.md|CLAUDE\.md|chat_history|conversation_history|"
    r"memory_ops|runtime_queue|\.env(?:\.[^/]*)?)(?:/|$)",
    flags=re.IGNORECASE,
)


def main() -> int:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode(
        "utf-8", errors="surrogateescape"
    ).split("\0")
    violations = [
        path for path in paths
        if path and BLOCKED.search(path) and not path.endswith(".env.example")
        and not re.search(r"/\.env\.[^/]+\.example$", path, flags=re.IGNORECASE)
    ]
    if violations:
        for path in violations:
            print(f"BLOCKED TRACKED PATH: {path}")
        return 1
    print("PASS: no private/assistant-state paths tracked in current Git index")
    return 0


if __name__ == "__main__":
    sys.exit(main())
