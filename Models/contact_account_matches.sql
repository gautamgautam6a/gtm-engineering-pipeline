-- Day 5: exact email-domain matching, not fuzzy person/company matching.
-- Inputs: raw_crm_contacts.email (lesson sample fixture) and the existing
-- account_billing.stripe_customer_id / company_domain fields.
-- This SELECT creates nothing and changes no contacts or CRM associations.
-- Free-mail list matches account_billing.sql. Never group Gmail users together.
WITH free_mail(domain) AS (
    VALUES ('gmail.com'), ('yahoo.com'), ('outlook.com'), ('hotmail.com'),
           ('icloud.com'), ('proton.me')
), contacts AS (
    -- Exclude free mail before the LEFT JOIN, preserving unmatched contacts.
    SELECT email,
        CASE WHEN lower(split_part(email, '@', 2)) IN (SELECT domain FROM free_mail)
             THEN NULL ELSE lower(split_part(email, '@', 2)) END AS company_domain
    FROM raw_crm_contacts
)
SELECT
    c.email,
    a.stripe_customer_id,
    CASE WHEN a.stripe_customer_id IS NULL THEN 'no account'
         ELSE 'domain match' END AS match_type
FROM contacts c
LEFT JOIN account_billing a
    ON a.company_domain = c.company_domain
-- Keep every contact. Shared corporate domains can produce several candidates;
-- this simple lesson rule does not silently choose one customer over another.
ORDER BY c.email, a.stripe_customer_id;
