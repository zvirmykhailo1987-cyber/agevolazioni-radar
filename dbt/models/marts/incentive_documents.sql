-- Current text of every incentive-related act, with its listing metadata.
select
    d.doc_id,
    d.content_hash,
    v.version,
    v.valid_from,
    d.doc_type,
    d.number,
    d.published_on,
    d.subject,
    d.senders,
    d.url,
    d.body_html
from {{ ref('stg_documents') }} d
join {{ ref('doc_versions') }} v
  on v.doc_id = d.doc_id and v.content_hash = d.content_hash and v.is_current
qualify row_number() over (partition by d.doc_id order by d.first_seen_at desc) = 1
