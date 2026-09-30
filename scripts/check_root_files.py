#!/usr/bin/env python3
import os
import sys


def main():
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    approved_files = {
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        ".pre-commit-config.yaml",
        ".release-please-manifest.json",
        "release-please-config.json",
        "docker-compose.yml",
        "CONTEXT.md",
        "CONTEXT-MAP.md",
        "imednet.postman_collection.json",
        ".gitignore",
        "pyproject.toml",
        "uv.lock",
        ".env",
        ".env.example",
        ".coverage",
        ".secrets.baseline",
        "LICENSE",
        "pytest.ini",
        "typos.toml",
        "vulture_whitelist.py",
    }

    # Files that might actually be there like .env etc might trigger this, so we should be careful.
    # Let's list what is there right now:
    # .env.example, .gitignore, .pre-commit-config.yaml, .release-please-manifest.json
    # .secrets.baseline, AGENTS.md, CHANGELOG.md, CODE_OF_CONDUCT.md, CONTRIBUTING.md
    # LICENSE, README.md, a11y_exemptions.json, docker-compose.yml
    # imednet.postman_collection.json, pyproject.toml, pytest.ini, release-please-config.json, typos.toml, uv.lock

    unapproved_found = False

    for item in os.listdir(root_dir):
        item_path = os.path.join(root_dir, item)
        if (
            os.path.isfile(item_path)
            and item not in approved_files
            and not item.startswith(".coverage.")
        ):
            print(f"Error: Unapproved file found in repository root: {item}")
            unapproved_found = True

    if unapproved_found:
        print("\nPlease clean up or safely relocate these outputs before committing.")
        print(
            "Root directory should be kept clean of transient output logs, test results, or unapproved scripts."
        )
        sys.exit(1)


if __name__ == '__main__':
    main()
