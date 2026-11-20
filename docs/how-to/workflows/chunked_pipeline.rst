Chunked Record Pipeline Guide
=============================

:class:`~imednet_workflows.chunked_pipeline.ChunkedRecordPipeline` and
:func:`~imednet_workflows.chunked_pipeline.iter_chunks` process large ``Study``
``Record`` streams in bounded-memory slices (default ``5,000`` items per chunk)
so analytical transformations and Parquet exports never materialize an entire
multi-hundred-thousand-``Record`` dataset at once.

Streaming Transformations and Parquet Chunks
--------------------------------------------

* ``iter_chunks(items, *, chunk_size=5000)`` yields bounded ``list[T]`` slices
  from any ``Record`` iterator.
* ``pipeline.map_chunks(items, mapper)`` applies a transformation function lazily
  per chunk and yields transformed lists.
* ``pipeline.write_parquet_chunks(rows, *, output_dir, filename_prefix="records")``
  writes numbered Parquet files (``<prefix>_chunk_00001.parquet``, ...) and
  returns the written ``Path`` list.

.. testcode::

   import tempfile
   from pathlib import Path
   from imednet import ImednetSDK
   from imednet_workflows import ChunkedRecordPipeline, iter_chunks

   sdk = ImednetSDK(simulation_mode=True)
   pipeline = ChunkedRecordPipeline(chunk_size=2)

   raw_batches = list(iter_chunks(sdk.records.list(study_key="STUDY"), chunk_size=2))
   mapped_chunks = pipeline.map_chunks(
       sdk.records.list(study_key="STUDY"),
       lambda rec: {
           "study_key": rec.study_key,
           "record_id": rec.record_id,
           "form_key": rec.form_key,
           "subject_key": rec.subject_key,
       },
   )

   with tempfile.TemporaryDirectory() as tmp_dir:
       parquet_files = pipeline.write_parquet_chunks(
           (row for chunk in mapped_chunks for row in chunk),
           output_dir=Path(tmp_dir),
           filename_prefix="study_records",
       )
       assert len(parquet_files) == len(raw_batches)
       print(len(parquet_files))
