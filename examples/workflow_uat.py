"""Demonstrate running UATWorkflow with environment-loaded or simulated SDK configuration."""

import os

from imednet import ImednetSDK, load_config
from imednet_workflows.uat import UATWorkflow


def main() -> None:
    """Initialize ImednetSDK and run UATWorkflow end to end."""
    try:
        cfg = load_config()
    except ValueError:
        cfg = load_config(simulation_mode=True)

    study_key = os.environ.get("IMEDNET_STUDY_KEY", "STUDY")

    with ImednetSDK(
        api_key=cfg.api_key,
        security_key=cfg.security_key,
        base_url=cfg.base_url,
        simulation_mode=cfg.simulation_mode,
    ) as sdk:
        workflow = UATWorkflow(sdk, poll_interval=0.05, seed=42)
        # Inspects Study, builds specification, generates synthetic Records, submits, and monitors Jobs.
        result = workflow.run(study_key, subject_count=2)
        print(result.summary())
        if not result.overall_success:
            raise RuntimeError(f"UAT workflow did not succeed for Study {study_key}")


if __name__ == "__main__":
    main()
