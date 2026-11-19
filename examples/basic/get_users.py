"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
Example script demonstrating how to retrieve users from an iMednet study using the iMednet SDK.

This script shows how to:
1. Initialize the iMednet SDK client with authentication credentials from environment variables
2. List available studies
3. Get users for the first study
4. Print basic user information for up to 5 users

Required Environment Variables:
    IMEDNET_API_KEY: API key for authentication
    IMEDNET_SECURITY_KEY: Security key for authentication
    IMEDNET_BASE_URL (optional): Custom base URL if needed

Returns:
    Prints user information to console including:
    - Number of users in the study
    - Login and name details for up to 5 users

Raises:
    Exception: Any errors during API communication or data retrieval
"""


def main() -> None:
    """TODO: Add docstring."""
    configure_json_logging()

    try:
        cfg = load_config()
        client = ImednetClient(
            api_key=cfg.api_key, security_key=cfg.security_key, base_url=cfg.base_url
        )

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

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
