import contextlib
import json
import logging
import os
from io import BytesIO
from multiprocessing import Process

import pytest

atheris = pytest.importorskip("atheris")

pytestmark = pytest.mark.fuzzing

# Suppress logging
logging.disable(logging.CRITICAL)


def run_fuzzer():
    import atheris

    with atheris.instrument_imports():
        from imednet_sinks.warehouse import _records_to_arrow_table

        import imednet
        import imednet.models.engine
        from imednet.datadict import load_from_csv
        from imednet.models.engine import get_contract

    def fuzz_dynamic_model(data: bytes):
        postman_path = "/tmp/fuzz_postman.json"
        os.environ["IMEDNET_POSTMAN_PATH"] = postman_path

        with open(postman_path, "wb") as f:
            f.write(data)

        imednet.models.engine._CONTRACT_CACHE = None

        with contextlib.suppress(Exception):
            get_contract()

    def fuzz_data_dictionary(data: bytes):
        with contextlib.suppress(Exception):
            load_from_csv(BytesIO(data))

    def fuzz_warehouse_transformation(data: bytes):
        try:
            text = data.decode('utf-8', errors='ignore')
            if not text:
                return

            try:
                records_data = json.loads(text)
            except json.JSONDecodeError:
                records_data = [{"record_data": {"field": text[:50]}}]

            if not isinstance(records_data, list):
                records_data = [records_data]

            class DummyRecord:
                def __init__(self, d):
                    self.record_id = str(d.get("id", "1"))
                    self.form_id = str(d.get("form_id", "1"))
                    self.visit_id = str(d.get("visit_id", "1"))
                    self.subject_key = str(d.get("subject_key", "1"))
                    self.record_data = d.get("data", {})

            records = []
            for rd in records_data:
                if isinstance(rd, dict):
                    records.append(DummyRecord(rd))

            _records_to_arrow_table(records)

        except Exception:
            pass

    def TestOneInput(data):
        if len(data) < 1:
            return

        mode = data[0] % 3
        if mode == 0:
            fuzz_dynamic_model(data)
        elif mode == 1:
            fuzz_data_dictionary(data)
        elif mode == 2:
            fuzz_warehouse_transformation(data)

    atheris.Setup(["", "-runs=200"], TestOneInput)
    atheris.Fuzz()


def test_fuzzing():
    """Fuzz the core models and data pipelines using Atheris."""
    p = Process(target=run_fuzzer)
    p.start()
    p.join()
    assert p.exitcode == 0, f"Fuzzer process exited with code {p.exitcode}"
