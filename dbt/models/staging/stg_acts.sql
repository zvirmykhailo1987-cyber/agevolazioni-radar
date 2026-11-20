select
    doc_id,
    doc_type,
    cast(number as varchar)          as number,
    cast(published_on as date)       as published_on,
    subject,
    url,
    relevant
from {{ source('raw', 'acts') }}
