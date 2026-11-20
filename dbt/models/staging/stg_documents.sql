-- One row per (act, text version). The loader appends a row only when the text hash is new.
select
    doc_id,
    content_hash,
    doc_type,
    cast(number as varchar)          as number,
    cast(published_on as date)       as published_on,
    subject,
    senders,
    url,
    body_html,
    summary_html,
    cast(first_seen_at as timestamp) as first_seen_at
from {{ source('raw', 'documents') }}
qualify row_number() over (partition by doc_id, content_hash order by first_seen_at) = 1
