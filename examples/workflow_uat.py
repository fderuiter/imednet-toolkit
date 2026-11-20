"""Demonstrate instantiating UATWorkflow with environment-loaded SDK configuration."""

import os

from imednet import ImednetSDK, load_config
from imednet_workflows.uat import UATWorkflow


def main() -> None:
    """Initialize ImednetSDK and UATWorkflow."""
    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Skipping execution: {e}")
        return

    study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"

    try:
        with ImednetSDK(
            api_key=cfg.api_key,
            security_key=cfg.security_key,
            base_url=cfg.base_url,
        ) as sdk:
            workflow = UATWorkflow(sdk)
            # Inspects Study, builds spec, generates synthetic Records, submits, and monitors Jobs.
            result = workflow.run(study_key)
            print(result.summary())
    except Exception as e:
        print(f"Failed: {e}")


if __name__ == "__main__":
    main()
