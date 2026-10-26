-- One row per Stripe customer. MRR is stored as integer cents.
WITH free_mail(domain) AS (
    VALUES ('gmail.com'), ('yahoo.com'), ('outlook.com'), ('hotmail.com'),
           ('icloud.com'), ('proton.me')
), customers AS (
    SELECT id AS stripe_customer_id,
        json_extract_string(payload, '$.name') AS customer_name,
        lower(split_part(json_extract_string(payload, '$.email'), '@', 2)) AS email_domain
    FROM raw_stripe_customers
), subscriptions AS (
    SELECT json_extract_string(payload, '$.customer') AS stripe_customer_id,
        json_extract_string(payload, '$.status') AS status, payload
    FROM raw_stripe_subscriptions
), subscription_statuses AS (
    -- Prefer active, then past_due, when a customer has several subscriptions.
    -- Do this before expanding items so empty item lists keep their status.
    SELECT stripe_customer_id,
        CASE
            WHEN count(*) FILTER (WHERE status = 'active') > 0 THEN 'active'
            WHEN count(*) FILTER (WHERE status = 'past_due') > 0 THEN 'past_due'
            ELSE max(status)
        END AS subscription_status
    FROM subscriptions GROUP BY stripe_customer_id
), subscription_items AS (
    SELECT stripe_customer_id, status, item
    FROM subscriptions,
        unnest(CAST(json_extract(payload, '$.items.data') AS JSON[])) AS items(item)
), item_prices AS (
    SELECT stripe_customer_id, status,
        CAST(json_extract_string(item, '$.price.unit_amount') AS BIGINT) AS price_cents,
        coalesce(CAST(json_extract_string(item, '$.quantity') AS BIGINT), 1) AS quantity,
        json_extract_string(item, '$.price.recurring.interval') AS billing_interval,
        -- interval_count is absent in seed_mock.py; default to one.
        coalesce(CAST(json_extract_string(item, '$.price.recurring.interval_count') AS BIGINT), 1)
            AS interval_count
    FROM subscription_items
), customer_mrr AS (
    SELECT stripe_customer_id,
        -- Sum all items and subscriptions, then round once to whole cents.
        CAST(round(sum(CASE WHEN status IN ('active', 'past_due') THEN
            CAST(price_cents AS DECIMAL(28, 0)) * quantity / nullif(
                CASE billing_interval
                    WHEN 'month' THEN interval_count
                    WHEN 'year' THEN 12 * interval_count
                    ELSE NULL -- Week/day prices do not count in the lesson.
                END, 0)
            ELSE 0 END)) AS BIGINT) AS mrr_cents
    FROM item_prices GROUP BY stripe_customer_id
), usage_by_domain AS (
    SELECT lower(split_part(user_email, '@', 2)) AS email_domain,
        count(DISTINCT lower(user_email)) FILTER (
            WHERE occurred_at >= current_timestamp - INTERVAL 30 DAY
              AND occurred_at <= current_timestamp
        ) AS active_users_30d,
        max(occurred_at) AS last_event_at
    FROM raw_usage_events GROUP BY 1
)
SELECT c.stripe_customer_id, c.customer_name,
    CASE WHEN c.email_domain IN (SELECT domain FROM free_mail)
         THEN NULL ELSE nullif(c.email_domain, '') END AS company_domain,
    coalesce(m.mrr_cents, 0)::BIGINT AS mrr_cents,
    coalesce(s.subscription_status, 'none') AS subscription_status,
    coalesce(u.active_users_30d, 0) AS active_users_30d,
    -- seed_mock.py stores UTC timestamps without timezone information.
    strftime(u.last_event_at, '%Y-%m-%dT%H:%M:%SZ') AS last_event_at
FROM customers c
LEFT JOIN customer_mrr m USING (stripe_customer_id)
LEFT JOIN subscription_statuses s USING (stripe_customer_id)
LEFT JOIN usage_by_domain u ON u.email_domain = c.email_domain;
