from __future__ import annotations

import heapq
from datetime import datetime, time, timedelta
from typing import Dict, FrozenSet, List, Optional, Tuple

from interfaces import (
    BaseDispatcherStrategy,
    CarrierService,
    DeliveryAttemptResult,
    DispatchContext,
    FleetUpgradeDecision,
    MonthlyPlanningContext,
    Shipment,
)

DEFAULT_DISTANCES: Dict[str, int] = {
    "URBANA": 12,
    "SUBURBANA": 35,
    "RURAL": 80,
    "REMOTA": 150,
}
DEFAULT_ATTEMPT_BPS: Tuple[int, ...] = (10_000, 5_000, 10_000)
DEFAULT_UPGRADE_LIMITS: Dict[str, int] = {
    "EXTRA_UNIT": 3,
    "SPEED_SHIFT": 1,
    "EXPANDED_CAPACITY": 1,
}
TERMINAL_STATES: FrozenSet[str] = frozenset(
    {"DELIVERED", "RETURNED_TO_VAULT", "UNRESOLVED"}
)

GroupKey = Tuple[str, str, int, Tuple[str, ...]]


class CandidateStrategy(BaseDispatcherStrategy):
    """Estrategia optimizada multi-objetivo para LOG-8092 (spec 0.4.0)."""

    def __init__(self, carrier_service: CarrierService) -> None:
        super().__init__(carrier_service)
        self.ready_groups: Dict[GroupKey, List[Shipment]] = {}
        self.future_heap: List[Tuple[datetime, int, Shipment]] = []
        self.cluster_first_seen: Dict[str, datetime] = {}
        self.last_carrier_by_shipment: Dict[str, str] = {}
        self._seq: int = 0
        self._next_prune_at: Optional[datetime] = None

        # Estructuras estáticas cacheadas por escenario
        self._cached_holidays_raw: Optional[Tuple[object, ...]] = None
        self._holidays_set: FrozenSet[object] = frozenset()
        self._distances: Dict[str, int] = DEFAULT_DISTANCES
        self._zone_inacc: Dict[str, int] = {}

        # Telemetría por periodo mensual para planificación adaptativa de flota
        self._period_admitted: int = 0
        self._period_sla_breaches: int = 0
        self._period_daytime_forced_waits: int = 0
        self._carrier_exhausted_ticks: Dict[str, int] = {}

    @property
    def pending(self) -> Tuple[Shipment, ...]:
        if not self.ready_groups and not self.future_heap:
            return ()
        res: List[Shipment] = []
        for grp in self.ready_groups.values():
            res.extend(grp)
        for _, _, s in self.future_heap:
            res.append(s)
        return tuple(res)

    def _add_ready(self, s: Shipment) -> None:
        key: GroupKey = (
            s.delivery_cluster_id,
            s.zone,
            s.attempt_number,
            s.required_capabilities,
        )
        grp = self.ready_groups.get(key)
        if grp is None:
            self.ready_groups[key] = [s]
        else:
            sid = s.shipment_id
            if all(x.shipment_id != sid for x in grp):
                grp.append(s)

    def _promote_future(self, now: datetime) -> None:
        heap = self.future_heap
        while heap and heap[0][0] <= now:
            _, _, s = heapq.heappop(heap)
            if s.state not in TERMINAL_STATES:
                self._add_ready(s)

    def on_shipments_available(
        self, now: datetime, shipments: Tuple[Shipment, ...]
    ) -> None:
        first_seen = self.cluster_first_seen
        self._period_admitted += len(shipments)
        for s in shipments:
            if s.state in TERMINAL_STATES:
                continue
            cid = s.delivery_cluster_id
            if cid not in first_seen:
                first_seen[cid] = s.available_at
            if s.ready_at <= now:
                self._add_ready(s)
            else:
                self._seq += 1
                heapq.heappush(self.future_heap, (s.ready_at, self._seq, s))

    def on_delivery_succeeded(self, result: DeliveryAttemptResult) -> None:
        s = result.shipment
        if self.last_carrier_by_shipment:
            self.last_carrier_by_shipment.pop(s.shipment_id, None)
        if result.occurred_at > s.sla_deadline:
            self._period_sla_breaches += 1

    def on_delivery_failed(self, result: DeliveryAttemptResult) -> None:
        s = result.shipment
        if not result.terminal and s.state not in TERMINAL_STATES:
            self._seq += 1
            heapq.heappush(self.future_heap, (s.ready_at, self._seq, s))
        elif self.last_carrier_by_shipment:
            self.last_carrier_by_shipment.pop(s.shipment_id, None)

    def on_monthly_planning(
        self, context: MonthlyPlanningContext
    ) -> Tuple[FleetUpgradeDecision, ...]:
        if not context.upgrades_enabled:
            self._reset_period_telemetry()
            return ()

        dp = context.decision_policy
        if dp is not None and dp.upgrades:
            limits = {u.upgrade_type: u.max_per_carrier_period for u in dp.upgrades}
            costs = {u.upgrade_type: u.cost_cents for u in dp.upgrades}
        else:
            limits = DEFAULT_UPGRADE_LIMITS
            costs = {
                "EXTRA_UNIT": 40_000,
                "SPEED_SHIFT": 60_000,
                "EXPANDED_CAPACITY": 35_000,
            }

        decisions: List[FleetUpgradeDecision] = []
        budget_left = context.reference_budget_cents - context.spent_cents
        unit_cost = costs.get("EXTRA_UNIT", 40_000)
        max_units = limits.get("EXTRA_UNIT", 3)

        # Solo adquirir EXTRA_UNIT si existe saturación diurna severa o riesgo de SLA (>2.5%)
        high_sla_risk = (
            self._period_admitted > 0
            and self._period_sla_breaches * 40 >= self._period_admitted
        )
        severe_daytime_shortage = self._period_daytime_forced_waits >= 60

        if (
            context.period_index > 0
            and (high_sla_risk or severe_daytime_shortage)
            and max_units > 0
            and budget_left >= unit_cost * 3
        ):
            ranked = sorted(
                context.carriers,
                key=lambda c: (
                    1
                    if self._carrier_exhausted_ticks.get(c.carrier_id, 0) >= 50
                    else 0,
                    1
                    if ("HARD_ACCESS" in c.capabilities and c.base_fee_cents < 1000)
                    else 0,
                    len(c.allowed_zones),
                    -c.base_fee_cents,
                ),
                reverse=True,
            )
            for c in ranked:
                if "EXTRA_UNIT" not in c.supported_upgrades:
                    continue
                if self._carrier_exhausted_ticks.get(c.carrier_id, 0) < 50:
                    continue
                decisions.append(
                    FleetUpgradeDecision(
                        carrier_id=c.carrier_id,
                        upgrade_type="EXTRA_UNIT",
                        quantity=1,
                    )
                )
                break

        self._reset_period_telemetry()
        return tuple(decisions)

    def _reset_period_telemetry(self) -> None:
        self._period_admitted = 0
        self._period_sla_breaches = 0
        self._period_daytime_forced_waits = 0
        self._carrier_exhausted_ticks.clear()

    def on_dispatch_tick(self, context: DispatchContext) -> None:
        now = context.now
        if self.future_heap and self.future_heap[0][0] <= now:
            self._promote_future(now)
        if not self.ready_groups:
            return

        # Poda periódica de cluster_first_seen (una vez por hora simulada)
        if self._next_prune_at is None or now >= self._next_prune_at:
            cutoff_seen = now - timedelta(minutes=60)
            if self.cluster_first_seen:
                self.cluster_first_seen = {
                    k: v
                    for k, v in self.cluster_first_seen.items()
                    if v >= cutoff_seen
                }
            self._next_prune_at = now + timedelta(minutes=60)

        # Caché de políticas estáticas del escenario
        cal_holidays = context.calendar.holidays
        if self._cached_holidays_raw is not cal_holidays:
            self._cached_holidays_raw = cal_holidays
            self._holidays_set = frozenset(cal_holidays)
            self._zone_inacc = {
                z.zone: z.inaccessible_route_bps for z in context.failure_policy.zones
            }
            if context.decision_policy is not None:
                self._distances = {
                    d.zone: d.distance_km for d in context.decision_policy.distances
                }

        dp = context.decision_policy
        if dp is not None:
            max_hold_min = dp.pricing.max_voluntary_hold_minutes
            tick_min = dp.pricing.dispatch_tick_minutes
            attempt_mult = dp.pricing.attempt_multipliers_bps
        else:
            max_hold_min = 45
            tick_min = 15
            attempt_mult = DEFAULT_ATTEMPT_BPS

        op_carriers = [c for c in context.carriers if c.operates_now]
        if not op_carriers:
            return
        avail_units = {c.carrier_id: c.available_units for c in op_carriers}
        total_avail_units = sum(avail_units.values())
        if total_avail_units <= 0:
            for c in op_carriers:
                self._carrier_exhausted_ticks[c.carrier_id] = (
                    self._carrier_exhausted_ticks.get(c.carrier_id, 0) + 1
                )
            return

        hold_delta = timedelta(minutes=max_hold_min)
        tick_delta = timedelta(minutes=tick_min)
        next_tick = now + tick_delta
        holidays = self._holidays_set
        is_sunday = now.isoweekday() == 7
        is_holiday = now.date() in holidays
        is_cal = is_sunday or is_holiday

        next_is_cal = (next_tick.isoweekday() == 7) or (next_tick.date() in holidays)
        clock = now.timetz().replace(tzinfo=None)
        next_clock = next_tick.timetz().replace(tzinfo=None)
        is_night = clock >= time(20, 0) or clock < time(6, 0)
        next_is_night = next_clock >= time(20, 0) or next_clock < time(6, 0)

        flush_before_surcharge = (not is_night and next_is_night) or (
            not is_cal and next_is_cal
        )
        wait_for_discount = (time(5, 15) <= clock < time(6, 0)) or (
            is_cal and not next_is_cal and next_tick.date() != now.date()
        )

        sun_mod = (
            context.failure_policy.sunday_operation_modifier_bps if is_sunday else 0
        )
        hol_mod = (
            context.failure_policy.holiday_operation_modifier_bps if is_holiday else 0
        )
        zone_inacc = self._zone_inacc
        distances = self._distances

        carrier_map = {c.carrier_id: c for c in op_carriers}
        zone_price_map = {
            c.carrier_id: {zp.zone: zp.surcharge_cents for zp in c.zone_prices}
            for c in op_carriers
        }
        hard_total_units = sum(
            c.total_units
            for c in context.carriers
            if "HARD_ACCESS" in c.capabilities
        )

        high_congestion = len(self.ready_groups) * 2 > total_avail_units
        safe_hold_min = max(0, max_hold_min - tick_min)
        long_hold = timedelta(minutes=min(30, safe_hold_min))
        short_hold = timedelta(minutes=min(15, safe_hold_min))

        to_dispatch = []
        for key, items in list(self.ready_groups.items()):
            cid, zone, att, req_caps = key
            earliest_ready = min(s.ready_at for s in items)
            must_send = now >= earliest_ready + hold_delta
            has_vip = any(s.product == "VIP_BLACK" for s in items)
            min_sla = min(s.sla_deadline for s in items)
            first_seen = self.cluster_first_seen.get(cid, earliest_ready)
            cluster_age = now - first_seen
            hold_age = now - earliest_ready

            if not must_send and not has_vip and att == 1 and not flush_before_surcharge:
                if wait_for_discount and hold_age < long_hold:
                    continue
                if (
                    (len(items) >= 2 or first_seen < earliest_ready)
                    and len(items) < 4
                    and cluster_age < long_hold
                    and hold_age < long_hold
                ):
                    continue
                if (
                    zone in ("RURAL", "REMOTA")
                    and len(items) < 4
                    and hold_age < long_hold
                ):
                    continue
                if (
                    zone == "SUBURBANA"
                    and len(items) < 4
                    and hold_age < short_hold
                ):
                    continue
                if (
                    high_congestion
                    and zone == "URBANA"
                    and len(items) < 4
                    and hold_age < short_hold
                ):
                    continue

            priority = (
                0 if must_send else 1,
                0 if has_vip else 1,
                0 if att > 1 else 1,
                0 if zone == "REMOTA" else (1 if zone == "RURAL" else 2),
                min_sla,
                -len(items),
            )
            to_dispatch.append((priority, key, items, must_send, has_vip))

        if not to_dispatch:
            return
        to_dispatch.sort(key=lambda x: x[0])

        for _, key, items, must_send, has_vip in to_dispatch:
            cid, zone, att, req_caps = key
            req_set = set(req_caps)
            dist = distances.get(zone, 35)
            att_bps = attempt_mult[min(att - 1, len(attempt_mult) - 1)]
            needs_hard = ("HARD_ACCESS" in req_set) or (zone in ("RURAL", "REMOTA"))

            idx = 0
            n_items = len(items)
            if n_items > 1:
                items.sort(key=lambda s: (s.ready_at, s.sla_deadline, s.shipment_id))

            while idx < n_items:
                rem = n_items - idx
                best_carrier = None
                best_score = None
                best_is_wait = False

                hard_units_left = sum(
                    avail_units.get(c_id, 0)
                    for c_id, c in carrier_map.items()
                    if "HARD_ACCESS" in c.capabilities
                )
                hard_scarce = hard_units_left * 5 <= hard_total_units * 2

                for c_id, c in carrier_map.items():
                    if zone not in c.allowed_zones:
                        continue
                    if not req_set.issubset(c.capabilities):
                        continue

                    u_avail = avail_units.get(c_id, 0)
                    is_wait = False
                    wait_min = 0
                    if u_avail <= 0:
                        self._carrier_exhausted_ticks[c_id] = (
                            self._carrier_exhausted_ticks.get(c_id, 0) + 1
                        )
                        if (
                            not must_send
                            and not flush_before_surcharge
                            and c.next_available_at is not None
                            and c.next_available_at <= next_tick
                            and next_tick.date() == now.date()
                        ):
                            is_wait = True
                            wait_min = tick_min
                        else:
                            continue

                    take = min(rem, c.batch_capacity)
                    if take <= 0:
                        continue

                    lh = c.base_fee_cents + zone_price_map[c_id][zone]
                    n_bps = c.night_multiplier_bps if is_night else 10_000
                    cal_bps = c.holiday_multiplier_bps if is_cal else 10_000
                    den = 1_000_000_000_000
                    adj = (lh * att_bps * n_bps * cal_bps + den // 2) // den

                    denom_speed = c.speed_kmh * c.speed_multiplier_bps
                    out_min = (dist * 60 * 10_000 + denom_speed - 1) // denom_speed
                    eta_min = wait_min + c.pickup_minutes + out_min
                    return_min = c.pickup_minutes + 2 * out_min

                    acc_bps = (
                        0
                        if "HARD_ACCESS" in c.capabilities
                        else zone_inacc.get(zone, 0)
                    )
                    op_bps = min(
                        10_000, c.operational_failure_bps + sun_mod + hol_mod
                    )
                    fail_bps = acc_bps + op_bps

                    exp_retry_cost = take * fail_bps * 0.05
                    n_zones = len(c.allowed_zones)

                    if has_vip:
                        lam = 18.0 if att == 1 else 28.0
                        score = (adj + exp_retry_cost) / take + lam * (
                            eta_min + fail_bps * 0.5
                        )
                    elif att > 1:
                        lam = 7.0 if dist <= 15 else 4.0
                        score = (adj + exp_retry_cost) / take + lam * (
                            eta_min + fail_bps * 0.45
                        )
                        if not needs_hard and "HARD_ACCESS" in c.capabilities:
                            score += 1000.0 if hard_scarce else 450.0
                        if c.operational_failure_bps <= 75 and not is_wait:
                            score += 500.0 if needs_hard else 900.0
                    else:
                        if dist >= 60:
                            score = (
                                (adj + exp_retry_cost) / take
                                + 1.8 * eta_min
                                + 2.8 * fail_bps
                            )
                            if c.operational_failure_bps <= 75:
                                score += (
                                    600.0
                                    if u_avail * 2 <= c.total_units
                                    else 300.0
                                )
                        elif dist >= 25:
                            lockup_pen = (
                                0.0
                                if n_zones <= 1
                                else (
                                    2.0 * return_min / take
                                    if n_zones >= 3
                                    else 0.5 * return_min / take
                                )
                            )
                            score = (
                                (adj + exp_retry_cost) / take
                                + 3.8 * (eta_min + fail_bps * 0.30)
                                + lockup_pen
                            )
                            if "HARD_ACCESS" in c.capabilities:
                                score += 1200.0 if hard_scarce else 500.0
                        else:
                            lockup_pen = (
                                1.8 * return_min / take
                                if n_zones >= 3
                                else 0.4 * return_min / take
                            )
                            score = (
                                (adj + exp_retry_cost) / take
                                + 4.65 * (eta_min + fail_bps * 0.30)
                                + lockup_pen
                            )
                            if "HARD_ACCESS" in c.capabilities:
                                score += 1200.0 if hard_scarce else 500.0

                    if best_score is None or score < best_score:
                        best_score = score
                        best_carrier = c
                        best_is_wait = is_wait

                if best_carrier is None or best_is_wait:
                    if best_carrier is None and must_send and not is_night and not is_cal:
                        self._period_daytime_forced_waits += 1
                    break

                take = min(rem, best_carrier.batch_capacity)
                batch_slice = items[idx : idx + take]
                batch_ids = tuple(s.shipment_id for s in batch_slice)
                receipt = self.carrier_service.dispatch_batch(
                    best_carrier.carrier_id, batch_ids
                )
                if receipt.accepted:
                    avail_units[best_carrier.carrier_id] -= 1
                    idx += take
                else:
                    avail_units[best_carrier.carrier_id] = 0

            if idx >= n_items:
                self.ready_groups.pop(key, None)
            elif idx > 0:
                self.ready_groups[key] = items[idx:]
