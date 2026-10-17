"""Fail if sensitive local/assistant-state paths are indexed or release candidates.

This complements Gitleaks content scanning. It does not inspect previous
commits; old history needs separate review before a public release.
"""

import re
import subprocess
import sys


BLOCKED = re.compile(
    r"(^|/)(?:\.private|\.refact|\.cursor|\.codex|"
    r"AGENTS\.md|chat_history|conversation_history|"
    r"memory_ops|runtime_queue|\.env(?:\.[^/]*)?|"
    r"recoveriq_(?:final18_pretrain_audit|payment_risk_audit_bundle)\.txt|"
    r"gate_patch\.py)(?:/|$)",
    flags=re.IGNORECASE,
)


def main() -> int:
    paths = []
    for command in (["git", "ls-files", "-z"],
                    ["git", "ls-files", "--others", "--exclude-standard", "-z"]):
        paths.extend(subprocess.check_output(command).decode(
            "utf-8", errors="surrogateescape"
        ).split("\0"))
    violations = [
        path for path in paths
        if path and BLOCKED.search(path) and not path.endswith(".env.example")
        and not re.search(r"/\.env\.[^/]+\.example$", path, flags=re.IGNORECASE)
    ]
    if violations:
        for path in violations:
            print(f"BLOCKED TRACKED PATH: {path}")
        return 1
    print("PASS: no private/assistant-state paths in Git index or untracked release candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
