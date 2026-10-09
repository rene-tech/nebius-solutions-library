-- Most scientific batches are terminal with their result already published.
-- A claim poll must not repeatedly decompress their potentially large state
-- documents just to discover that no controller work remains. Keep exactly
-- the existing claim eligibility predicate; leases, policy, priority, operation
-- status, row locking and fencing remain checked by the original claim query.
-- IF NOT EXISTS permits the same reviewed index to be built concurrently before
-- this transactional migration during a rolling production upgrade.
CREATE INDEX IF NOT EXISTS fs2_scientific_batches_pending_claim_idx
    ON fs2_scientific_batches(status, lease_expires_at, operation_id)
    WHERE status IN ('queued','running')
       OR (status IN ('succeeded','failed','cancelled')
           AND (state->>'result_published')::boolean=false);
