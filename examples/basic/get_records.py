"""Example script demonstrating record listing with the iMednet SDK.

Showcases:
- Initializing the ``ImednetSDK`` client using configuration loading.
- Listing studies using ``client.studies.list()``.
- Listing records for a specific study using ``client.records.list()``.
"""

from imednet import ImednetSDK as ImednetClient
from imednet import load_config
from imednet.utils import configure_json_logging


def main() -> None:
    """Fetch and print up to five records for the first accessible study."""
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
            records = list(client.records.list(study_key=study_key))
            print(f"Records for study '{study_key}': {len(records)}")
            for record in records[:5]:
                print(f"- Record ID: {record.record_id}, Subject Key: {record.subject_key}")


if __name__ == "__main__":
    main()
