"""TODO: Add docstring."""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging

"""
Example script demonstrating how to retrieve subjects from iMednet studies using the iMednet SDK.

This script:
1. Initializes the iMednet client with API credentials loaded from environment variables
2. Retrieves a list of available studies
3. For the first study, retrieves and displays information about its subjects
4. Prints the subject key and status for up to 5 subjects

Required environment variables:
    - IMEDNET_API_KEY: Your iMednet API key
    - IMEDNET_SECURITY_KEY: Your iMednet security key
    - IMEDNET_BASE_URL (optional): Custom base URL for the API endpoint

Returns:
    None. Prints subject information to standard output.

Raises:
    Exception: Any errors that occur during API communication
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
            subjects = list(client.subjects.list(study_key=study_key))
            print(f"Subjects for study '{study_key}': {len(subjects)}")
            for subject in subjects[:5]:
                print(f"- Subject Key: {subject.subject_key}, Status: {subject.subject_status}")

    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
