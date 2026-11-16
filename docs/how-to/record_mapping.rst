Record Mapping Process
======================

:class:`~imednet_workflows.record_mapper.RecordMapper` turns raw record payloads
into a :class:`pandas.DataFrame`. It loads variable metadata, builds a dynamic
model, parses the records, and assembles the final table.

.. mermaid:: /diagrams/record_mapping_13.mmd

Explicit normalization
----------------------

General RecordMapper exports retain source field strings and analytical types.
Use ``CategoricalNormalizer(date_fields={"assessmentDate"}, date_order="MDY")``
for an explicit normalization step. Only the named date fields are converted;
ambiguous slash dates require MDY or DMY, and invalid/partial/unknown dates remain
unchanged. Timezone offsets and string fractional precision are retained.

Pass a study-approved vocabulary to ``CategoricalNormalizer(cdisc_ct=...)`` to
opt into case-insensitive term matching. The normalizer snapshots the vocabulary
and rejects ambiguous case collisions. Explicit terminology lookups take
precedence, including in readiness scoring. Out-of-set values are preserved and
reported without echoing their contents in new terminology diagnostics.

``NORMALIZATION_TERMINOLOGY`` is a project reporting vocabulary, not a dated
CDISC Controlled Terminology release. The compatibility name
``CDISC_CONTROLLED_TERMINOLOGY`` refers to that same project vocabulary. Neither
is enabled by default. Callers own the selected terminology version and its
extensibility policy; full versioned conformance remains tracked in issue #1592.

Existing USUBJID values are preserved. An explicit ``study_key`` argument may
fill an existing empty USUBJID placeholder from study/subject keys, preserving
case. This convenience does not allocate a globally unique subject identity;
use the study's identity mapping for pooled data and repeat participants.

MappingRule/StudyConfiguration ``strictCdash`` (also local ``strict`` or
``strictMode``) and ``IMEDNET_STRICT_CDASH`` enable a local naming/prefix
heuristic. SDK ``IMEDNET_STRICT_MODE`` retains its separate response-drift
meaning. The heuristic accepts reporting aliases and does not certify membership
in any official CDASH/SDTM version.

Evidence and limits: CDISC CDASHIG v2.1 permits partial-date collection;
CDISC SDTM v1.7 defines USUBJID across studies; CDISC terminology publications
are versioned and some codelists are extensible. Source references:

* ``https://www.cdisc.org/standards/foundational/cdashig-v2-1``
* ``https://www.cdisc.org/standards/foundational/sdtm/sdtm-v1-7/html``
* ``https://www.cdisc.org/kb/articles/controlled-terminology-faqs``
