from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from candidate_strategy import CandidateStrategy
from interfaces import (
    CarrierSnapshot,
    DecisionPolicy,
    DeliveryAttemptResult,
    DispatchContext,
    DispatchReceipt,
    FailureAttemptRate,
    FailurePolicy,
    FailureZoneProfile,
    MonthlyPlanningContext,
    PricingPolicy,
    RcpAudienceProfile,
    RcpExpectationRange,
    RcpPolicy,
    ScenarioCalendar,
    Shipment,
    SlaDefinition,
    UpgradeDefinition,
    ZoneDistance,
    ZonePrice,
)

LIMA = timezone(timedelta(hours=-5))


def make_carrier(
    now: datetime,
    carrier_id: str = "CARRIER_A",
    allowed_zones: Tuple[str, ...] = ("URBANA",),
    capabilities: Tuple[str, ...] = (),
    available_units: int = 2,
    total_units: int = 2,
    batch_capacity: int = 4,
    pickup_minutes: int = 10,
    speed_kmh: int = 40,
    base_fee_cents: int = 400,
    zone_surcharges: Optional[Dict[str, int]] = None,
    operates_now: bool = True,
    next_available_at: Optional[datetime] = None,
    operational_failure_bps: int = 100,
) -> CarrierSnapshot:
    surcharges = zone_surcharges or {z: 0 for z in allowed_zones}
    return CarrierSnapshot(
        carrier_id=carrier_id,
        allowed_zones=allowed_zones,
        capabilities=capabilities,
        supported_upgrades=("EXTRA_UNIT",),
        available_units=available_units,
        base_total_units=total_units,
        total_units=total_units,
        retiring_units=0,
        base_batch_capacity=batch_capacity,
        batch_capacity=batch_capacity,
        pickup_minutes=pickup_minutes,
        speed_kmh=speed_kmh,
        speed_multiplier_bps=10_000,
        base_fee_cents=base_fee_cents,
        zone_prices=tuple(ZonePrice(z, surcharges.get(z, 0)) for z in allowed_zones),
        night_multiplier_bps=15_000,
        holiday_multiplier_bps=18_000,
        current_time_multiplier_bps=10_000,
        operates_now=operates_now,
        next_available_at=next_available_at if next_available_at is not None else now,
        operational_failure_bps=operational_failure_bps,
        rolling_success_rate_bps=None,
        active_upgrades=(),
    )


def make_shipment(
    now: datetime,
    shipment_id: str = "S-1",
    product: str = "STANDARD",
    zone: str = "URBANA",
    cluster_id: str = "CL-1",
    ready_offset_min: int = 0,
    sla_hours: int = 48,
    attempt_number: int = 1,
    required_capabilities: Tuple[str, ...] = (),
    state: str = "READY",
) -> Shipment:
    return Shipment(
        shipment_id=shipment_id,
        available_at=now,
        ready_at=now + timedelta(minutes=ready_offset_min),
        sla_deadline=now + timedelta(hours=sla_hours),
        product=product,
        zone=zone,
        delivery_cluster_id=cluster_id,
        is_metal=False,
        state=state,
        attempt_number=attempt_number,
        required_capabilities=required_capabilities,
    )


def default_failure_policy() -> FailurePolicy:
    return FailurePolicy(
        attempts=(
            FailureAttemptRate(1, 700),
            FailureAttemptRate(2, 350),
            FailureAttemptRate(3, 150),
        ),
        zones=(
            FailureZoneProfile("URBANA", 0, 0),
            FailureZoneProfile("SUBURBANA", 50, 0),
            FailureZoneProfile("RURAL", 150, 400),
            FailureZoneProfile("REMOTA", 300, 1_200),
        ),
        sunday_operation_modifier_bps=100,
        holiday_operation_modifier_bps=200,
    )


def default_calendar() -> ScenarioCalendar:
    return ScenarioCalendar(
        timezone="America/Lima",
        holidays=(date(2026, 1, 29),),
    )


def default_rcp_policy() -> RcpPolicy:
    return RcpPolicy(
        influencer_rate_bps=300,
        undelivered_rating=1,
        delivered_floor_rating=2,
        on_time_boundary_rating=6,
        late_floor_ratio_bps=15_000,
        expectations=(
            RcpExpectationRange("VIP_BLACK", 2_500, 4_500),
            RcpExpectationRange("BUSINESS", 3_500, 6_500),
            RcpExpectationRange("SME", 4_500, 7_500),
            RcpExpectationRange("STANDARD", 5_500, 8_500),
        ),
        audiences=(
            RcpAudienceProfile("VIP", 7_000, 2_000, 20_000),
            RcpAudienceProfile("NORMAL", 1_800, 4_200, 10_000),
            RcpAudienceProfile("INFLUENCER", 9_000, 800, 80_000),
        ),
    )


def default_decision_policy() -> DecisionPolicy:
    return DecisionPolicy(
        admission_days=90,
        distances=(
            ZoneDistance("URBANA", 12),
            ZoneDistance("SUBURBANA", 35),
            ZoneDistance("RURAL", 80),
            ZoneDistance("REMOTA", 150),
        ),
        sla=(
            SlaDefinition("VIP_BLACK", 1440, "CONTINUOUS", (1, 2, 3, 4, 5, 6, 7), "00:00", "24:00"),
            SlaDefinition("STANDARD", 2880, "BUSINESS", (1, 2, 3, 4, 5, 6), "08:00", "20:00"),
        ),
        upgrades=(
            UpgradeDefinition("EXTRA_UNIT", 40_000, 3, None),
            UpgradeDefinition("SPEED_SHIFT", 60_000, 1, 11_500),
            UpgradeDefinition("EXPANDED_CAPACITY", 35_000, 1, None),
        ),
        pricing=PricingPolicy(
            additional_card_cents=120,
            metal_security_cents=180,
            terminal_penalty_cents=32_000,
            attempt_multipliers_bps=(10_000, 5_000, 10_000),
            max_voluntary_hold_minutes=45,
            dispatch_tick_minutes=15,
        ),
    )


class MockCarrierService:
    def __init__(
        self,
        now: datetime,
        carriers: Sequence[CarrierSnapshot],
        reject_first: bool = False,
    ) -> None:
        self.now = now
        self.carriers = tuple(carriers)
        self.dispatched: List[Tuple[str, Tuple[str, ...]]] = []
        self.reject_first = reject_first
        self._calls = 0

    def snapshot(self) -> Tuple[CarrierSnapshot, ...]:
        return self.carriers

    def dispatch_batch(
        self, carrier_id: str, shipment_ids: Sequence[str]
    ) -> DispatchReceipt:
        self._calls += 1
        ids = tuple(shipment_ids)
        if self.reject_first and self._calls == 1:
            return DispatchReceipt(
                accepted=False,
                carrier_id=carrier_id,
                shipment_ids=ids,
                dispatched_at=self.now,
                charged_cost_cents=0,
                rejection_reason="NO_AVAILABLE_UNIT",
            )
        self.dispatched.append((carrier_id, ids))
        return DispatchReceipt(
            accepted=True,
            carrier_id=carrier_id,
            shipment_ids=ids,
            dispatched_at=self.now,
            charged_cost_cents=500,
            batch_id=f"BATCH-{self._calls}",
            estimated_delivery_at=self.now + timedelta(minutes=30),
        )


def make_context(
    now: datetime,
    carriers: Sequence[CarrierSnapshot],
    decision_policy: Optional[DecisionPolicy] = default_decision_policy(),
) -> DispatchContext:
    return DispatchContext(
        now=now,
        reference_budget_cents=3_000_000,
        spent_cents=0,
        contingency_threshold_cents=3_300_000,
        carriers=tuple(carriers),
        calendar=default_calendar(),
        failure_policy=default_failure_policy(),
        rcp_policy=default_rcp_policy(),
        decision_policy=decision_policy,
    )


class CustomStrategyContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 1, 5, 10, 0, tzinfo=LIMA)

    def test_01_empty_shipments_and_idle_tick(self) -> None:
        c = make_carrier(self.now)
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        strat.on_shipments_available(self.now, ())
        strat.on_dispatch_tick(make_context(self.now, (c,)))
        self.assertEqual(svc.dispatched, [])
        self.assertEqual(strat.pending, ())

    def test_02_future_ready_at_not_dispatched_early(self) -> None:
        c = make_carrier(self.now)
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s = make_shipment(self.now, product="VIP_BLACK", ready_offset_min=30)
        strat.on_shipments_available(self.now, (s,))
        strat.on_dispatch_tick(make_context(self.now, (c,)))
        self.assertEqual(svc.dispatched, [])
        self.assertEqual(len(strat.pending), 1)

        later = self.now + timedelta(minutes=30)
        svc.now = later
        strat.on_dispatch_tick(make_context(later, (c,)))
        self.assertEqual(svc.dispatched, [("CARRIER_A", ("S-1",))])
        self.assertEqual(strat.pending, ())

    def test_03_terminal_shipments_ignored_on_admission(self) -> None:
        c = make_carrier(self.now)
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s1 = make_shipment(self.now, "S-1", state="DELIVERED")
        s2 = make_shipment(self.now, "S-2", state="RETURNED_TO_VAULT")
        s3 = make_shipment(self.now, "S-3", state="UNRESOLVED")
        strat.on_shipments_available(self.now, (s1, s2, s3))
        self.assertEqual(strat.pending, ())

    def test_04_terminal_failure_not_requeued(self) -> None:
        c = make_carrier(self.now)
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s = make_shipment(
            self.now, "S-TERM", attempt_number=3, state="RETURNED_TO_VAULT"
        )
        strat.on_delivery_failed(
            DeliveryAttemptResult(
                shipment=s,
                carrier_id="CARRIER_A",
                batch_id="B-1",
                attempt_number=3,
                occurred_at=self.now,
                terminal=True,
                failure_reason="CUSTOMER_ABSENT",
            )
        )
        self.assertEqual(strat.pending, ())

    def test_05_retryable_failure_requeued_for_future_ready_at(self) -> None:
        c = make_carrier(self.now)
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s_retry = make_shipment(
            self.now,
            "S-RETRY",
            attempt_number=2,
            ready_offset_min=1440,
            state="RETRY_WAIT",
        )
        strat.on_delivery_failed(
            DeliveryAttemptResult(
                shipment=s_retry,
                carrier_id="CARRIER_A",
                batch_id="B-1",
                attempt_number=1,
                occurred_at=self.now,
                terminal=False,
                failure_reason="CUSTOMER_ABSENT",
            )
        )
        # Should not dispatch before 24h retry cool-down
        strat.on_dispatch_tick(make_context(self.now + timedelta(minutes=60), (c,)))
        self.assertEqual(svc.dispatched, [])

        # Dispatched once ready_at is reached
        ready_time = self.now + timedelta(minutes=1440)
        svc.now = ready_time
        strat.on_dispatch_tick(make_context(ready_time, (c,)))
        self.assertEqual(svc.dispatched, [("CARRIER_A", ("S-RETRY",))])

    def test_06_hard_access_escalation_switches_carrier(self) -> None:
        c_eco = make_carrier(
            self.now, "ECO_POST", allowed_zones=("RURAL",), capabilities=()
        )
        c_hard = make_carrier(
            self.now,
            "LOGI_GLOBAL",
            allowed_zones=("RURAL",),
            capabilities=("HARD_ACCESS",),
        )
        svc = MockCarrierService(self.now, (c_eco, c_hard))
        strat = CandidateStrategy(svc)
        s = make_shipment(
            self.now,
            "S-RURAL",
            zone="RURAL",
            attempt_number=2,
            required_capabilities=("HARD_ACCESS",),
        )
        strat.on_shipments_available(self.now, (s,))
        strat.on_dispatch_tick(make_context(self.now, (c_eco, c_hard)))
        self.assertEqual(svc.dispatched, [("LOGI_GLOBAL", ("S-RURAL",))])

    def test_07_exact_zone_matching_no_substring_bug(self) -> None:
        c_sub = make_carrier(self.now, "SUB_ONLY", allowed_zones=("SUBURBANA",))
        c_urb = make_carrier(self.now, "URB_OK", allowed_zones=("URBANA",))
        svc = MockCarrierService(self.now, (c_sub, c_urb))
        strat = CandidateStrategy(svc)
        s = make_shipment(self.now, "S-URB", product="VIP_BLACK", zone="URBANA")
        strat.on_shipments_available(self.now, (s,))
        strat.on_dispatch_tick(make_context(self.now, (c_sub, c_urb)))
        self.assertEqual(svc.dispatched, [("URB_OK", ("S-URB",))])

    def test_08_homogeneous_batch_grouping(self) -> None:
        c = make_carrier(
            self.now,
            "MULTI",
            allowed_zones=("URBANA", "SUBURBANA"),
            capabilities=("HARD_ACCESS",),
            available_units=5,
            batch_capacity=4,
        )
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s1 = make_shipment(self.now, "S1", product="VIP_BLACK", cluster_id="C1", zone="URBANA")
        s2 = make_shipment(self.now, "S2", product="VIP_BLACK", cluster_id="C2", zone="URBANA")
        s3 = make_shipment(self.now, "S3", product="VIP_BLACK", cluster_id="C1", zone="URBANA", attempt_number=2)
        s4 = make_shipment(
            self.now,
            "S4",
            product="VIP_BLACK",
            cluster_id="C1",
            zone="URBANA",
            required_capabilities=("HARD_ACCESS",),
        )
        strat.on_shipments_available(self.now, (s1, s2, s3, s4))
        strat.on_dispatch_tick(make_context(self.now, (c,)))
        self.assertEqual(len(svc.dispatched), 4)
        for _, batch in svc.dispatched:
            self.assertEqual(len(batch), 1)

    def test_09_batch_capacity_splitting(self) -> None:
        c = make_carrier(
            self.now, "CARRIER_A", available_units=3, total_units=3, batch_capacity=4
        )
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        shipments = tuple(
            make_shipment(self.now, f"S-{i}", product="VIP_BLACK", cluster_id="CL-SAME")
            for i in range(10)
        )
        strat.on_shipments_available(self.now, shipments)
        strat.on_dispatch_tick(make_context(self.now, (c,)))
        self.assertEqual(
            [len(b) for _, b in svc.dispatched],
            [4, 4, 2],
        )
        self.assertEqual(strat.pending, ())

    def test_10_available_units_decremented_within_tick(self) -> None:
        c = make_carrier(
            self.now, "SINGLE_UNIT", available_units=1, total_units=1, batch_capacity=4
        )
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        shipments = tuple(
            make_shipment(self.now, f"S-{i}", product="VIP_BLACK", cluster_id="CL-1")
            for i in range(6)
        )
        strat.on_shipments_available(self.now, shipments)
        strat.on_dispatch_tick(make_context(self.now, (c,)))
        self.assertEqual(len(svc.dispatched), 1)
        self.assertEqual(len(svc.dispatched[0][1]), 4)
        self.assertEqual(len(strat.pending), 2)

    def test_11_non_operating_carrier_skipped(self) -> None:
        c_closed = make_carrier(self.now, "CLOSED", operates_now=False)
        svc = MockCarrierService(self.now, (c_closed,))
        strat = CandidateStrategy(svc)
        s = make_shipment(self.now, "S-1", product="VIP_BLACK")
        strat.on_shipments_available(self.now, (s,))
        strat.on_dispatch_tick(make_context(self.now, (c_closed,)))
        self.assertEqual(svc.dispatched, [])
        self.assertEqual(len(strat.pending), 1)

    def test_12_voluntary_hold_deadline_enforced(self) -> None:
        c = make_carrier(self.now, "SUB_CARRIER", allowed_zones=("SUBURBANA",))
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s = make_shipment(self.now, "S-HOLD", product="STANDARD", zone="SUBURBANA")
        strat.on_shipments_available(self.now, (s,))
        # At t=0, held for 15m suburban consolidation
        strat.on_dispatch_tick(make_context(self.now, (c,)))
        self.assertEqual(svc.dispatched, [])
        # At t=45m (max_voluntary_hold_minutes), guaranteed to dispatch
        t45 = self.now + timedelta(minutes=45)
        svc.now = t45
        strat.on_dispatch_tick(make_context(t45, (c,)))
        self.assertEqual(svc.dispatched, [("SUB_CARRIER", ("S-HOLD",))])

    def test_13_one_tick_lookahead_waits_for_hard_access(self) -> None:
        c_eco = make_carrier(
            self.now,
            "ECO_POST",
            allowed_zones=("RURAL",),
            capabilities=(),
            available_units=1,
            base_fee_cents=250,
            operational_failure_bps=350,
        )
        c_logi = make_carrier(
            self.now,
            "LOGI_GLOBAL",
            allowed_zones=("RURAL",),
            capabilities=("HARD_ACCESS",),
            available_units=0,
            base_fee_cents=550,
            operational_failure_bps=180,
            next_available_at=self.now + timedelta(minutes=15),
        )
        svc = MockCarrierService(self.now, (c_eco, c_logi))
        strat = CandidateStrategy(svc)
        # Shipments ready for 30 min (so suburban/rural 30m hold is satisfied, but < 45m must_send)
        s = make_shipment(
            self.now - timedelta(minutes=30),
            "S-RURAL-WAIT",
            product="STANDARD",
            zone="RURAL",
        )
        strat.on_shipments_available(self.now - timedelta(minutes=30), (s,))
        strat.on_dispatch_tick(make_context(self.now, (c_eco, c_logi)))
        # Should wait 1 tick for LOGI_GLOBAL rather than dispatching on ECO_POST
        self.assertEqual(svc.dispatched, [])

    def test_14_night_surcharge_boundary_flush_and_wait(self) -> None:
        # At 19:45, next tick is 20:00 (night surcharge), so flush immediately even if suburban hold_age == 0
        t_1945 = datetime(2026, 1, 5, 19, 45, tzinfo=LIMA)
        c = make_carrier(t_1945, "SUB_C", allowed_zones=("SUBURBANA",))
        svc = MockCarrierService(t_1945, (c,))
        strat = CandidateStrategy(svc)
        s = make_shipment(t_1945, "S-1945", product="STANDARD", zone="SUBURBANA")
        strat.on_shipments_available(t_1945, (s,))
        strat.on_dispatch_tick(make_context(t_1945, (c,)))
        self.assertEqual(svc.dispatched, [("SUB_C", ("S-1945",))])

        # At 05:30, next daytime is 06:00, so wait for discount if not must_send
        t_0530 = datetime(2026, 1, 6, 5, 30, tzinfo=LIMA)
        c2 = make_carrier(t_0530, "URB_C", allowed_zones=("URBANA",))
        svc2 = MockCarrierService(t_0530, (c2,))
        strat2 = CandidateStrategy(svc2)
        s2 = make_shipment(t_0530, "S-0530", product="STANDARD", zone="URBANA")
        strat2.on_shipments_available(t_0530, (s2,))
        strat2.on_dispatch_tick(make_context(t_0530, (c2,)))
        self.assertEqual(svc2.dispatched, [])

    def test_15_decision_policy_none_and_upgrades_disabled(self) -> None:
        c = make_carrier(self.now)
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s = make_shipment(self.now, "S-NONE", product="VIP_BLACK")
        strat.on_shipments_available(self.now, (s,))
        strat.on_dispatch_tick(make_context(self.now, (c,), decision_policy=None))
        self.assertEqual(svc.dispatched, [("CARRIER_A", ("S-NONE",))])

        mp_ctx = MonthlyPlanningContext(
            now=self.now,
            period_index=1,
            reference_budget_cents=100_000,
            spent_cents=0,
            contingency_threshold_cents=110_000,
            carriers=(c,),
            calendar=default_calendar(),
            failure_policy=default_failure_policy(),
            rcp_policy=default_rcp_policy(),
            upgrades_enabled=False,
            decision_policy=None,
        )
        self.assertEqual(strat.on_monthly_planning(mp_ctx), ())

    def test_16_dispatch_rejection_recovery_and_deduplication(self) -> None:
        c1 = make_carrier(self.now, "C1", base_fee_cents=300)
        c2 = make_carrier(self.now, "C2", base_fee_cents=400)
        svc = MockCarrierService(self.now, (c1, c2), reject_first=True)
        strat = CandidateStrategy(svc)
        s = make_shipment(self.now, "S-DUP", product="VIP_BLACK")
        # Admit duplicate shipment ID twice
        strat.on_shipments_available(self.now, (s, s))
        self.assertEqual(len(strat.pending), 1)
        # First dispatch on C1 is rejected, should seamlessly fall back to C2 in same tick
        strat.on_dispatch_tick(make_context(self.now, (c1, c2)))
        self.assertEqual(svc.dispatched, [("C2", ("S-DUP",))])
        self.assertEqual(strat.pending, ())

    def test_17_tight_voluntary_hold_policy_adaptation(self) -> None:
        c = make_carrier(self.now, "RURAL_C", allowed_zones=("RURAL",), capabilities=("HARD_ACCESS",))
        svc = MockCarrierService(self.now, (c,))
        strat = CandidateStrategy(svc)
        s = make_shipment(self.now, "S-TIGHT", product="STANDARD", zone="RURAL")
        strat.on_shipments_available(self.now, (s,))
        # When max_voluntary_hold_minutes == 15 (zero slack above 15m tick), safe_hold_min == 0,
        # so even a RURAL STANDARD shipment dispatches immediately at t=0 without holding.
        base_dp = default_decision_policy()
        tight_dp = DecisionPolicy(
            admission_days=base_dp.admission_days,
            distances=base_dp.distances,
            sla=base_dp.sla,
            upgrades=base_dp.upgrades,
            pricing=PricingPolicy(
                additional_card_cents=120,
                metal_security_cents=180,
                terminal_penalty_cents=32_000,
                attempt_multipliers_bps=(10_000, 5_000, 10_000),
                max_voluntary_hold_minutes=15,
                dispatch_tick_minutes=15,
            ),
        )
        strat.on_dispatch_tick(make_context(self.now, (c,), decision_policy=tight_dp))
        self.assertEqual(svc.dispatched, [("RURAL_C", ("S-TIGHT",))])


if __name__ == "__main__":
    unittest.main()
