"""Typed Run result serialization contract."""

from app.results import (
    AccountRunResult,
    ExecutedOrder,
    FailureReason,
    NoteOut,
    PlannedOrderOut,
    PriceEvidence,
    RecordedAccountState,
    RecordedPosition,
    RunMode,
    RunOutcome,
    RunRecord,
    RunSource,
)


def test_complete_run_result_round_trips_losslessly():
    sell = PlannedOrderOut(symbol="AAA", side="SELL", quantity=2)
    buy = PlannedOrderOut(symbol="BBB", side="BUY", quantity=3, limit_price=12.34)
    executed = ExecutedOrder(
        symbol="AAA",
        side="SELL",
        quantity=2,
        filled_quantity=2,
        average_fill_price=10.0,
        status="FILLED",
    )
    note = NoteOut(level="warning", text="synthetic note")
    record = RunRecord(
        run_id="20261126-100000-U100001-abcd",
        account_id="U100001",
        strategy_name="strategy-one",
        mode=RunMode.LIVE,
        source=RunSource.DASHBOARD,
        started_at="2026-11-26T16:00:00Z",
        finished_at="2026-11-26T16:01:00Z",
        outcome=RunOutcome.FAILED,
        failure_reason=FailureReason.ORDERS_FAILED,
        error_message="synthetic failure",
        raw_weights={"AAA": 0.5, "BBB": 0.5},
        scaled_weights={"AAA": 0.5, "BBB": 0.5},
        targets={"AAA": 0.5, "BBB": 0.5},
        replacement_set="ira",
        state0=RecordedAccountState(
            cash=100.0,
            net_liquidation=1000.0,
            positions=[RecordedPosition(symbol="AAA", quantity=10)],
        ),
        prices={
            "AAA": PriceEvidence(
                bid=9.9,
                ask=10.1,
                last=10.0,
                close=9.8,
                bar_close=9.95,
                used_mark=10.0,
                mark_source="mid",
                buy_reference=10.1,
                buy_source="ask",
            )
        },
        V0=1000.0,
        reserve=2.5,
        planned_sells=[sell],
        cash_base=120.0,
        V1=999.0,
        planned_buys=[buy],
        trimmed=["CCC"],
        executed=[executed],
        notes=[note],
    )
    result = AccountRunResult(
        run_id=record.run_id,
        account_id=record.account_id,
        strategy_name=record.strategy_name,
        mode=record.mode,
        source=record.source,
        outcome=RunOutcome.FAILED,
        failure_reason=FailureReason.ORDERS_FAILED,
        error_message="synthetic failure",
        executed=[executed],
        planned_sells=[sell],
        planned_buys=[buy],
        notes=[note],
        value_end=999.0,
        cash_end=90.0,
        record=record,
    )

    restored = AccountRunResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.record.model_dump(mode="json") == record.model_dump(mode="json")
