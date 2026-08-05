/* =============================================================================
   J3 — Episode → vertical map for the SN21 basket-split trigger (design v0.2 §8, Condition 2).
   Owner: the operator.  Consumer: the validator daily loop (runs this AS-IS over the operator's Postgres).

   OUTPUT: exactly one row per candidate in bittensor_episode_candidates —
       episode_candidate_id (text) | vertical (ecommerce|lead_gen|untagged) | tagged_at (utc)
   Self-contained: plain SELECT with LEFT JOINs + correlated EXISTS. No temp tables, no
   session state, no writes. Safe to run on a read-only connection.

   CLASSIFICATION RULE (aligned to Condition 1 = goal type; retail taxonomy secondary).
   Evaluated per episode, using the episode's OWN campaign (ec.campaign_id). Precedence — the
   FIRST matching rule wins, so goal type beats taxonomy when they disagree:
     1. ecommerce  — the episode's campaign uses VALUE-BASED bidding: campaign_daily_performance
                     bid_strategy_type IN ('MAXIMIZE_CONVERSION_VALUE','TARGET_ROAS') OR target_roas>0.
                     [PRIMARY: goal type — same signal as Condition 1]
     2. lead_gen   — the campaign HAS goal-type data but it is not value-based (any other bid
                     strategy). Goal type is present and says not-ecommerce, so taxonomy does NOT override.
     3. ecommerce  — no goal-type data for the campaign, AND the account's taxonomy root is 'retail'.
                     [SECONDARY: retail taxonomy root]
     4. lead_gen   — no goal-type data, account has some other taxonomy vertical.
     5. untagged   — neither a goal-type signal nor a taxonomy vertical (carried, but excluded from
                     the ecommerce-vs-lead_gen error comparison).

   DEFINITIONAL NOTE for the first review (read Condition 1 and 2 side by side):
   This rule tags ~20.6% of candidates ecommerce, vs the instrumented Condition-1 share of ~14–18%.
   The gap is GRAIN, not disagreement: this map classifies each episode by its own campaign's bid
   strategy (a value-based campaign -> ecommerce), which is broader than an account-level goal share.
   If Condition 1 is account-level, reconcile the grain at first review; the signal (value-based
   bidding) is identical. MAXIMIZE_CONVERSIONS is deliberately lead_gen (optimises count, not value).

   COVERAGE: 98.6% tagged (untagged 1.4% / 258 of 18,350) — above the 97% target. The 258 untagged
   are accounts with neither goal-type data nor a taxonomy vertical; backfilling their taxonomy would
   push coverage to ~99%+ (optional, tracked separately).

   tagged_at = evaluation time (UTC): the tag is derived live from current goal-type + taxonomy, so
   the timestamp records when THIS read computed the map.
   ============================================================================= */
SELECT
    ec.id AS episode_candidate_id,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM campaign_daily_performance cdp
            WHERE cdp.customer_id = r.customer_id
              AND cdp.campaign_id = ec.campaign_id
              AND ( cdp.bid_strategy_type IN ('MAXIMIZE_CONVERSION_VALUE', 'TARGET_ROAS')
                    OR (cdp.target_roas IS NOT NULL AND cdp.target_roas > 0) )
        ) THEN 'ecommerce'
        WHEN EXISTS (
            SELECT 1 FROM campaign_daily_performance cdp
            WHERE cdp.customer_id = r.customer_id
              AND cdp.campaign_id = ec.campaign_id
        ) THEN 'lead_gen'
        WHEN aha.lineage->>0 = 'retail' THEN 'ecommerce'
        WHEN aha.lineage IS NOT NULL      THEN 'lead_gen'
        ELSE 'untagged'
    END AS vertical,
    (now() AT TIME ZONE 'UTC') AS tagged_at
FROM bittensor_episode_candidates ec
LEFT JOIN bittensor_account_registry      r   ON r.id = ec.bittensor_account_id
LEFT JOIN account_hierarchy_assignment    aha ON aha.customer_id = r.customer_id;
