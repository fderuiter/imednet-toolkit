"""Demonstrate instantiating UATWorkflow with environment-loaded SDK configuration."""

from imednet import ImednetSDK, load_config
from imednet_workflows.uat import UATWorkflow


def main() -> None:
    """Initialize ImednetSDK and UATWorkflow."""
    try:
        cfg = load_config()
    except ValueError as e:
        print(f"Skipping execution: {e}")
        return

    # In a real environment, you'd execute against an active study.
    # Here we show how to instantiate and run the workflow.
    try:
        with ImednetSDK(
            api_key=cfg.api_key,
            security_key=cfg.security_key,
            base_url=cfg.base_url,
        ) as sdk:
            UATWorkflow(sdk)
            # Inspects study, builds spec, generates synthetic data, submits, and monitors.
            # Replace "MY_STUDY" with a real study key.
            # result = workflow.run("MY_STUDY")
            # print(result.summary())
    except Exception as e:
        print(f"Failed: {e}")


if __name__ == "__main__":
    main()
