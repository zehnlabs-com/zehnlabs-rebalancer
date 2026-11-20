# IBKR connections must fetch open orders, cancel only own orders, and never global-cancel

The own-order-cancel step at the start of every live run only works if open orders
are fetched: the connect call must pass `fetchFields=... | StartupFetch.ORDERS_OPEN`
required, or the cancel step sees nothing and lets a duplicate order through (PRD §9
IB-1). Order status is read **only from the stored `Trade` objects returned by the
place calls**, never by scanning `ib.trades()` by order number, because order numbers
repeat across client IDs (IB-10). `reqGlobalCancel` is forbidden — it cancels every
order of every account on the login (PRD §3.2). A live run cancels only this
account's own working orders before trading; if any foreign working order exists the
run stops and the user is told rather than competing with it.
