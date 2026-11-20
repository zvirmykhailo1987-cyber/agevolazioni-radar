-- Version history of each act: a new content hash is a new version (SCD type 2 on the text).
-- An A -> B -> A revert is three versions, because the loader records every new hash in order.
select
    doc_id,
    content_hash,
    row_number() over (partition by doc_id order by first_seen_at)            as version,
    first_seen_at                                                             as valid_from,
    lead(first_seen_at) over (partition by doc_id order by first_seen_at)     as valid_to,
    lead(first_seen_at) over (partition by doc_id order by first_seen_at) is null as is_current
from {{ ref('stg_documents') }}
