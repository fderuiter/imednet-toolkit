"""Example script that saves study records to CSV using :class:`RecordMapper`."""

import os

from imednet import ImednetSDK
from imednet_workflows.record_mapper import RecordMapper

api_key = os.getenv("IMEDNET_API_KEY") or None
security_key = os.getenv("IMEDNET_SECURITY_KEY") or None
base_url = os.getenv("IMEDNET_BASE_URL") or None
study_key = os.getenv("IMEDNET_STUDY_KEY") or "STUDY"

output_path = os.getenv(
    "IMEDNET_OUTPUT_PATH",
    os.path.join(os.getcwd(), "record_mapping_output", "records.csv"),
)

with ImednetSDK(api_key=api_key, security_key=security_key, base_url=base_url) as sdk:
    mapper = RecordMapper(sdk)
    df = mapper.dataframe(study_key=study_key)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"Saved {len(df)} records to {output_path}")
