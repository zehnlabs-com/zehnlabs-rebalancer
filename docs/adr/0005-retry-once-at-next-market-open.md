# Eligible failed Runs retry once at the next market open

An eligible failed or market-closed live Signal, immediate Dashboard Run, or
immediate Initial setup Run creates one durable `retry` rebalance Work Item for
the next configured market open, using fresh data. A pending rebalance already
satisfies the retry requirement and is reused. Scheduled and Retry Runs never
create another retry. Preview never retries and may coexist with the one pending
rebalance. All decisions and persistence remain in the parent process.
