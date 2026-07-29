/* J3 — Episode → vertical map for the SN21 basket-split trigger (Condition 2).
   Author: Jayesh, 2026-07-29 (delivered via Khurram). Rule: goal type primary
   (= Condition 1's value-based-bidding signal), retail taxonomy secondary,
   first match wins; untagged carried but excluded from the compare.
   Known definitional note: per-episode-campaign grain reads ~20.6% ecommerce
   vs the account-share instrumentation's 14-18% — same signal, different
   grain; reconciled at first review. */
SELECT
    ec.id AS episode_candidate_id,
    CASE
        WHEN EXISTS (SELECT 1 FROM campaign_daily_performance cdp
                     WHERE cdp.customer_id = r.customer_id AND cdp.campaign_id = ec.campaign_id
                       AND (cdp.bid_strategy_type IN ('MAXIMIZE_CONVERSION_VALUE','TARGET_ROAS')
                            OR (cdp.target_roas IS NOT NULL AND cdp.target_roas > 0)))
             THEN 'ecommerce'
        WHEN EXISTS (SELECT 1 FROM campaign_daily_performance cdp
                     WHERE cdp.customer_id = r.customer_id AND cdp.campaign_id = ec.campaign_id)
             THEN 'lead_gen'
        WHEN aha.lineage->>0 = 'retail' THEN 'ecommerce'
        WHEN aha.lineage IS NOT NULL     THEN 'lead_gen'
        ELSE 'untagged'
    END AS vertical,
    (now() AT TIME ZONE 'UTC') AS tagged_at
FROM bittensor_episode_candidates ec
LEFT JOIN bittensor_account_registry   r   ON r.id = ec.bittensor_account_id
LEFT JOIN account_hierarchy_assignment aha ON aha.customer_id = r.customer_id;
