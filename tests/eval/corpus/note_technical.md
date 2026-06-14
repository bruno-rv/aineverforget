APIGateway Rate Limiting — Design Note
Author: Diana Okonkwo
Date: 2026-06-11
Service: APIGateway

---

Rate limiting approach for APIGateway. This is a design note, not a finalized spec — details may shift during implementation.

Algorithm choice: token bucket. Considered sliding window counter as well, but token bucket wins for this use case because it natively handles burst tolerance. With sliding window you'd need a separate burst allowance mechanism bolted on; token bucket gives you burst for free by design. The key requirement is that users can spike briefly above the steady-state rate, so token bucket is the right fit.

Parameters:
- Steady-state limit: 100 requests per minute per authenticated user
- Burst allowance: up to 200 requests in the first 10 seconds of a new window (tokens start full)
- Unauthenticated requests: not subject to per-user limits; separate IP-level limiting handled upstream

Backend: Redis. Token state is stored per user per window. Key pattern: `ratelimit:{user_id}:{window}` where `{window}` is the current minute epoch (Unix timestamp integer-divided by 60). Redis DECR + TTL for atomic decrement and automatic expiry. No external rate-limit service — Redis is already in the stack.

Why not sliding window? Sliding window would require storing per-request timestamps or a rolling counter, which is heavier in Redis memory and more complex to implement correctly under high concurrency. Token bucket with Redis DECR is O(1) per request and requires only two keys per user (current token count + last refill timestamp).

Implementation ownership: Diana Okonkwo owns the full implementation — Redis key management, token bucket logic, middleware integration into the APIGateway request pipeline, and load test validation.

Open questions before implementation starts:
- What happens if Redis is unavailable? Fail open (allow request) or fail closed (reject)? Currently leaning fail open with a circuit breaker, but needs sign-off from security.
- Should the burst allowance reset mid-minute if a user drops below 100 req/min? Standard token bucket behavior is yes; confirm this is acceptable.

Diana to bring both questions to the next architecture sync.
