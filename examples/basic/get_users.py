"""Retrieve study users from an iMednet study using the iMednet SDK.

Required environment variables (unless ``IMEDNET_SIMULATION_MODE=true``):
    - ``IMEDNET_API_KEY``: API key for authentication
    - ``IMEDNET_SECURITY_KEY``: Security key for authentication
    - ``IMEDNET_BASE_URL`` (optional): Custom base URL if needed
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch and print up to five users for the first accessible study."""
    configure_json_logging()

    cfg = load_config()
    with ImednetClient(
        api_key=cfg.api_key, security_key=cfg.security_key, base_url=cfg.base_url
    ) as client:
        studies = list(client.studies.list())
        if not studies:
            print("No studies returned from API.")
            return

        for study in studies[:1]:
            study_key = study.study_key
            users = list(client.users.list(study_key=study_key))
            print(f"Users for study '{study_key}': {len(users)}")
            for user in users[:5]:
                print(f"- User Login: {user.login}, Name: {user.name}")


if __name__ == "__main__":
    main()
