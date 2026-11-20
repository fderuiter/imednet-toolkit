"""Example script demonstrating how to retrieve subjects from iMednet studies.

Required environment variables (unless ``IMEDNET_SIMULATION_MODE=true``):
    - ``IMEDNET_API_KEY``: Your iMednet API key
    - ``IMEDNET_SECURITY_KEY``: Your iMednet security key
    - ``IMEDNET_BASE_URL`` (optional): Custom base URL for the API endpoint
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch and print up to five subjects for the first accessible study."""
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
            subjects = list(client.subjects.list(study_key=study_key))
            print(f"Subjects for study '{study_key}': {len(subjects)}")
            for subject in subjects[:5]:
                print(f"- Subject Key: {subject.subject_key}, Status: {subject.subject_status}")


if __name__ == "__main__":
    main()
