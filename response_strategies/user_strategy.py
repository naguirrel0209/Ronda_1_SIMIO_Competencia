"""Contestant response strategy.

This implementation focuses on four low-risk improvements:

1. Expected-time routing for every new shipment, with preventive avoidance
   and arrival-aware handling of temporarily closed destinations.
2. In-transit replanning for cargo whose remaining booking chain crosses an
   active or imminent disruption.
3. Expected-time booking costs that include sailing, service frequency,
   segment-specific capacity pressure, transshipments, and disruption delays.
4. Berth priority based on useful discharged TEU per handling hour, favoring
   final deliveries and near-term connections while preventing starvation.

Alternative routes are created conservatively only when a validated cycle is
at least 15% faster and an empty vessel can be spared by the source route.
"""

from dataclasses import dataclass
import datetime as dt
import math

from maritime_data_context import Booking


LOOKAHEAD_DAYS = 50.0
RECOVERY_BUFFER_DAYS = 3.0
DEFAULT_SAILING_SPEED_KNOTS = 18.0
TRANSFER_PENALTY_HOURS = 48.0
LARGE_SHIPMENT_TEU = 50.0
LARGE_SHIPMENT_TRANSFER_MULTIPLIER = 2.0
MIN_REBOOKING_IMPROVEMENT = 0.15
LARGE_SHIPMENT_MIN_REBOOKING_IMPROVEMENT = 0.10
IMMINENT_LEG_DELAY_HOURS = 14.0 * 24.0
IMMINENT_PORT_DELAY_HOURS = 21.0 * 24.0
ACTIVE_LEG_DELAY_HOURS = 45.0 * 24.0
MAX_CAPACITY_PRESSURE_HOURS = 7.0 * 24.0
BERTH_PRODUCTIVITY_WEIGHT = 0.70
BERTH_WAITING_WEIGHT = 0.30
BERTH_STARVATION_LIMIT_HOURS = 7.0 * 24.0
QUAY_CRANE_TEU_PER_HOUR = 45.0
MINIMUM_HANDLING_HOURS = 0.25
FINAL_DELIVERY_WEIGHT = 1.0
NEAR_CONNECTION_WEIGHT = 0.65
DELAYED_CONNECTION_WEIGHT = 0.25
NEAR_CONNECTION_HOURS = 7.0 * 24.0
MIN_ALTERNATIVE_ROUTE_IMPROVEMENT = 0.15
MIN_SOURCE_ROUTE_VESSELS = 2


@dataclass
class _CandidateBookingEdge:
    service_route: object
    departure_port: object
    arrival_port: object
    departure_segment_index: int
    arrival_segment_index: int
    total_distance: float
    sailing_hours: float
    expected_wait_hours: float
    capacity_pressure_hours: float
    segments: tuple


@dataclass
class _DisruptionWindow:
    active_closed_ports: object
    preventive_closed_ports: object
    active_congested_legs: object
    preventive_congested_legs: object


class UserStrategy:
    @staticmethod
    def select_vessel_for_berth(
        maritime_data_context,
        port,
        waiting_vessels,
        available_berths,
        current_time,
        waiting_since_by_vessel=None,
    ):
        """Select the vessel releasing the most useful TEU per berth-hour."""
        if not waiting_vessels:
            return None

        waiting_since_by_vessel = waiting_since_by_vessel or {}

        def waiting_hours(vessel):
            waiting_since = waiting_since_by_vessel.get(vessel, current_time)
            return max(0.0, (current_time - waiting_since).total_seconds() / 3600.0)

        def shipment_teu(shipments):
            return sum(getattr(shipment, "teu_size", 0) or 0 for shipment in shipments)

        def discharging_teu(vessel):
            try:
                return shipment_teu(vessel.get_discharging_shipments_at_current_segment())
            except (AttributeError, TypeError, ValueError):
                return 0.0

        def loading_teu(vessel):
            try:
                return shipment_teu(vessel.get_loading_shipments_at_next_segment())
            except (AttributeError, TypeError, ValueError):
                return 0.0

        def handling_hours(vessel):
            vessel_class = getattr(vessel, "vessel_class", None)
            loa = float(getattr(vessel_class, "loa", 0) or 0)
            crane_count = max(1, int(loa / 55.0))
            handled_teu = discharging_teu(vessel) + loading_teu(vessel)
            return max(
                MINIMUM_HANDLING_HOURS,
                handled_teu / (crane_count * QUAY_CRANE_TEU_PER_HOUR),
            )

        def connection_weight(shipment):
            demand = getattr(shipment, "demand", None)
            if demand is not None and demand.destination_port is port:
                return FINAL_DELIVERY_WEIGHT

            try:
                current_booking = shipment.get_current_booking()
            except (AttributeError, ValueError):
                return DELAYED_CONNECTION_WEIGHT

            next_booking = next(
                (
                    booking
                    for booking in shipment.associated_bookings
                    if booking.sequence_index == current_booking.sequence_index + 1
                ),
                None,
            )
            if next_booking is None or next_booking.service_route is None:
                return DELAYED_CONNECTION_WEIGHT

            route = next_booking.service_route
            segments = sorted(
                route.segments,
                key=lambda segment: segment.sequence_index,
            )
            departure_index = _find_segment_list_index(
                segments,
                next_booking.departure_segment_index,
            )
            if departure_index < 0:
                return DELAYED_CONNECTION_WEIGHT

            connection_wait = _route_expected_wait_hours(
                route,
                segments,
                departure_index,
                _route_average_speed(route),
                current_time,
            )
            if connection_wait <= NEAR_CONNECTION_HOURS:
                return NEAR_CONNECTION_WEIGHT
            return DELAYED_CONNECTION_WEIGHT

        def useful_discharging_teu(vessel):
            try:
                shipments = vessel.get_discharging_shipments_at_current_segment()
            except (AttributeError, TypeError, ValueError):
                return 0.0
            return sum(
                float(getattr(shipment, "teu_size", 0) or 0)
                * connection_weight(shipment)
                for shipment in shipments
            )

        def discharge_productivity(vessel):
            return useful_discharging_teu(vessel) / handling_hours(vessel)

        def normalize(values):
            minimum = min(values)
            span = max(values) - minimum
            if span == 0:
                return [0.0] * len(values)
            return [(value - minimum) / span for value in values]

        waits = [waiting_hours(vessel) for vessel in waiting_vessels]
        oldest_wait = max(waits)
        if oldest_wait >= BERTH_STARVATION_LIMIT_HOURS:
            return max(
                enumerate(waiting_vessels),
                key=lambda item: (waits[item[0]], -item[0]),
            )[1]

        productivity_scores = normalize(
            [discharge_productivity(vessel) for vessel in waiting_vessels]
        )
        waiting_scores = normalize(waits)
        scores = [
            BERTH_PRODUCTIVITY_WEIGHT * productivity
            + BERTH_WAITING_WEIGHT * waiting
            for productivity, waiting in zip(productivity_scores, waiting_scores)
        ]
        return max(
            enumerate(waiting_vessels),
            key=lambda item: (scores[item[0]], -item[0]),
        )[1]

    @staticmethod
    def create_alternative_service_routes(context, now, vessel=None):
        """Create only materially faster alternatives using an empty vessel."""
        from response_strategies import default_strategy as fallback_routes

        fallback_routes._restore_inactive_alternative_route_vessels(
            context,
            now,
            vessel,
        )
        _ensure_conservative_alternative_routes(context, now, fallback_routes)
        fallback_routes._try_switch_empty_vessel_to_pending_route(vessel)
        # A non-None result prevents the unconditional fallback from creating
        # routes that did not pass the conservative filters.
        return True

    @staticmethod
    def assign_associated_bookings(context, now, shipment):
        """Assign every initial booking using the lowest expected travel time.

        Sailing, next-vessel arrival, route pressure, transfers, and relevant
        disruptions are evaluated throughout the full simulation. Preventive
        hard avoidance remains limited to known disruption windows.
        """
        demand = shipment.demand
        origin_port = demand.origin_port
        destination_port = demand.destination_port
        if origin_port is destination_port:
            _clear_booking_chain(shipment)
            shipment.current_booking_index = 1
            return True

        window = _get_disruption_window(context, now)

        if origin_port.name.casefold() in window.active_closed_ports:
            return False

        candidate_bookings = _build_all_candidate_bookings(context, now)
        path = _find_lowest_cost_booking_path(
            context,
            origin_port,
            destination_port,
            candidate_bookings,
            window,
            hard_avoid_preventive=True,
            shipment_teu=shipment.teu_size,
        )
        if not path:
            path = _find_lowest_cost_booking_path(
                context,
                origin_port,
                destination_port,
                candidate_bookings,
                window,
                hard_avoid_preventive=False,
                shipment_teu=shipment.teu_size,
            )
        if not path:
            return None
        if _path_arrives_during_destination_closure(
            context,
            now,
            destination_port,
            path,
            shipment.teu_size,
        ):
            return False

        _assign_booking_path(shipment, path)
        return True

    @staticmethod
    def adjust_bookings_before_cargo_handling(context, now, vessel):
        """Replan carried shipments before cargo handling at the current port."""
        current_segment = vessel.current_segment
        if current_segment is None or current_segment.associated_leg is None:
            return None

        window = _get_disruption_window(context, now)
        if not _has_preventive_work(window):
            return None

        current_port = current_segment.associated_leg.arrival_port
        if current_port is None:
            return None

        candidate_bookings = _build_all_candidate_bookings(context, now)
        changed = False

        for shipment in list(vessel.carried_shipments):
            try:
                current_booking = shipment.get_current_booking()
            except ValueError:
                continue

            final_port = _get_final_booking_port(shipment)
            if final_port is None:
                continue
            if final_port.name.casefold() in window.active_closed_ports:
                continue

            if not _remaining_booking_chain_is_impacted(
                shipment,
                current_booking,
                current_segment,
                window,
            ):
                continue

            path = _find_lowest_cost_booking_path(
                context,
                current_port,
                final_port,
                candidate_bookings,
                window,
                hard_avoid_preventive=True,
                shipment_teu=shipment.teu_size,
            )
            if not path:
                path = _find_lowest_cost_booking_path(
                    context,
                    current_port,
                    final_port,
                    candidate_bookings,
                    window,
                    hard_avoid_preventive=False,
                    shipment_teu=shipment.teu_size,
                )
            if not path:
                continue

            current_path = _build_remaining_booking_path(
                shipment,
                current_booking,
                current_segment,
                candidate_bookings,
            )
            if not current_path:
                continue

            current_cost = _booking_path_cost(
                current_path,
                final_port,
                window,
                shipment.teu_size,
                current_route=current_booking.service_route,
            )
            alternative_cost = _booking_path_cost(
                path,
                final_port,
                window,
                shipment.teu_size,
                current_route=current_booking.service_route,
            )
            required_improvement = (
                LARGE_SHIPMENT_MIN_REBOOKING_IMPROVEMENT
                if shipment.teu_size >= LARGE_SHIPMENT_TEU
                else MIN_REBOOKING_IMPROVEMENT
            )
            if alternative_cost > current_cost * (1.0 - required_improvement):
                continue

            _replace_unfinished_bookings_from_current_port(
                shipment,
                current_booking,
                current_segment,
                path,
            )
            changed = True

        return True if changed else None


def _get_disruption_window(context, now):
    active_closed_ports = set()
    preventive_closed_ports = set()
    active_congested_legs = set()
    preventive_congested_legs = set()

    for plan in context.disruption_plans:
        if plan.start_offset_days is None or plan.duration_days is None:
            continue

        start = dt.datetime.min + dt.timedelta(days=plan.start_offset_days)
        end = start + dt.timedelta(days=plan.duration_days)
        active = start <= now < end
        near = (
            now < start
            and (start - now).total_seconds() / 86400.0 <= LOOKAHEAD_DAYS
        )
        recovery_buffer = (
            end <= now
            and (now - end).total_seconds() / 86400.0 <= RECOVERY_BUFFER_DAYS
        )
        relevant = active or near or recovery_buffer
        if not relevant:
            continue

        if plan.close_berth and plan.target_berth is not None:
            port_name = plan.target_berth.port.name.casefold()
            if active:
                active_closed_ports.add(port_name)
            else:
                preventive_closed_ports.add(port_name)

        if plan.multiplier > 1 and plan.target_leg is not None:
            if active:
                active_congested_legs.add(plan.target_leg)
            else:
                preventive_congested_legs.add(plan.target_leg)

    return _DisruptionWindow(
        active_closed_ports,
        preventive_closed_ports,
        active_congested_legs,
        preventive_congested_legs,
    )


def _has_preventive_work(window):
    return bool(
        window.active_closed_ports
        or window.preventive_closed_ports
        or window.active_congested_legs
        or window.preventive_congested_legs
    )


def _build_all_candidate_bookings(context, now):
    edges = []
    for service_route in context.service_routes:
        if not _route_has_available_vessel(service_route):
            continue
        route_speed = _route_average_speed(service_route)
        segments = sorted(
            service_route.segments,
            key=lambda segment: segment.sequence_index,
        )
        segment_count = len(segments)
        if segment_count < 2:
            continue
        booked_teu_by_segment = _route_booked_teu_by_segment(
            service_route,
            segments,
        )

        for start_index in range(segment_count):
            departure_port = segments[start_index].associated_leg.departure_port
            expected_wait_hours = _route_expected_wait_hours(
                service_route,
                segments,
                start_index,
                route_speed,
                now,
            )
            cumulative_distance = 0.0
            for step in range(1, segment_count):
                segment_index = (start_index + step - 1) % segment_count
                leg = segments[segment_index].associated_leg
                cumulative_distance += leg.sailing_distance
                arrival_port = leg.arrival_port
                if departure_port is arrival_port:
                    continue

                used_segments = tuple(
                    segments[(start_index + offset) % segment_count]
                    for offset in range(step)
                )
                edges.append(
                    _CandidateBookingEdge(
                        service_route=service_route,
                        departure_port=departure_port,
                        arrival_port=arrival_port,
                        departure_segment_index=start_index + 1,
                        arrival_segment_index=segment_index + 1,
                        total_distance=cumulative_distance,
                        sailing_hours=cumulative_distance / route_speed,
                        expected_wait_hours=expected_wait_hours,
                        capacity_pressure_hours=_edge_capacity_pressure_hours(
                            service_route,
                            used_segments,
                            booked_teu_by_segment,
                        ),
                        segments=used_segments,
                    )
                )
    return edges


def _route_has_available_vessel(route):
    if route.source_service_route is None:
        return True
    return bool(route.deployed_vessels)


def _route_average_speed(route):
    speeds = [
        float(getattr(getattr(vessel, "vessel_class", None), "sailing_speed", 0) or 0)
        for vessel in route.deployed_vessels
    ]
    positive_speeds = [speed for speed in speeds if speed > 0]
    if not positive_speeds:
        return DEFAULT_SAILING_SPEED_KNOTS
    return sum(positive_speeds) / len(positive_speeds)


def _route_expected_wait_hours(route, segments, departure_index, route_speed, now):
    """Estimate the earliest vessel arrival at one route departure port.

    Vessel positions provide a port-specific estimate. The half-headway is
    retained only as a fallback for vessels that have not entered the route.
    """
    if not route.deployed_vessels:
        return math.inf

    departure_port = segments[departure_index].associated_leg.departure_port
    arrival_estimates = []
    for vessel in route.deployed_vessels:
        if vessel.assigned_service_route is not route:
            continue

        vessel_speed = float(
            getattr(getattr(vessel, "vessel_class", None), "sailing_speed", 0) or 0
        )
        if vessel_speed <= 0:
            vessel_speed = route_speed

        if (
            vessel.current_berth is not None
            and vessel.current_berth.port is departure_port
        ):
            arrival_estimates.append(0.0)
            continue

        current_index = _segment_position_index(segments, vessel.current_segment)
        if current_index >= 0:
            include_current_leg = vessel.current_berth is None
            arrival_estimates.append(
                _hours_from_vessel_position_to_departure(
                    segments,
                    current_index,
                    departure_index,
                    vessel_speed,
                    include_current_leg,
                )
            )

    if arrival_estimates:
        return min(arrival_estimates)

    cycle_distance = sum(
        float(getattr(segment.associated_leg, "sailing_distance", 0) or 0)
        for segment in segments
    )
    half_headway = cycle_distance / route_speed / len(route.deployed_vessels) / 2.0
    scheduled_wait = _hours_until_route_start(route, now)
    return min(half_headway, scheduled_wait)


def _segment_position_index(segments, current_segment):
    if current_segment is None:
        return -1
    return next(
        (index for index, segment in enumerate(segments) if segment is current_segment),
        -1,
    )


def _hours_from_vessel_position_to_departure(
    segments,
    current_index,
    departure_index,
    vessel_speed,
    include_current_leg,
):
    """Return sailing hours from a vessel's observed position to a port."""
    hours = 0.0
    if include_current_leg:
        cursor = current_index
        while True:
            leg = segments[cursor].associated_leg
            hours += float(getattr(leg, "sailing_distance", 0) or 0) / vessel_speed
            cursor = (cursor + 1) % len(segments)
            if cursor == departure_index:
                break
    else:
        cursor = (current_index + 1) % len(segments)
        while cursor != departure_index:
            leg = segments[cursor].associated_leg
            hours += float(getattr(leg, "sailing_distance", 0) or 0) / vessel_speed
            cursor = (cursor + 1) % len(segments)
    return hours


def _hours_until_route_start(route, now):
    current_day = now.weekday() + (
        now.hour * 3600 + now.minute * 60 + now.second
    ) / 86400.0
    delay_days = (float(route.start_day_of_week) - current_day) % 7.0
    return delay_days * 24.0


def _route_booked_teu_by_segment(route, segments):
    """Count only unfinished bookings on the route segments they still use."""
    booked_teu = {segment: 0.0 for segment in segments}
    for booking in route.associated_bookings:
        shipment = getattr(booking, "shipment", None)
        if shipment is None or shipment.completion_time is not None:
            continue
        current_booking_index = getattr(shipment, "current_booking_index", None)
        if current_booking_index is None or booking.sequence_index < current_booking_index:
            continue

        start_index = _find_segment_list_index(
            segments,
            booking.departure_segment_index,
        )
        end_index = _find_segment_list_index(
            segments,
            booking.arrival_segment_index,
        )
        if start_index < 0 or end_index < 0:
            continue

        teu = float(getattr(shipment, "teu_size", 0) or 0)
        for segment in _iter_segments_between(segments, start_index, end_index):
            booked_teu[segment] += teu
    return booked_teu


def _edge_capacity_pressure_hours(route, used_segments, booked_teu_by_segment):
    """Penalize an edge according to its busiest unfinished segment."""
    total_capacity = sum(
        float(getattr(getattr(vessel, "vessel_class", None), "teu_capacity", 0) or 0)
        for vessel in route.deployed_vessels
    )
    if total_capacity <= 0:
        return MAX_CAPACITY_PRESSURE_HOURS

    peak_booked_teu = max(
        (booked_teu_by_segment.get(segment, 0.0) for segment in used_segments),
        default=0.0,
    )


def _ensure_conservative_alternative_routes(context, now, fallback_routes):
    close_plans, congested_plans = fallback_routes._get_active_disruption_plans(
        context,
        now,
    )
    avoid_port_names = fallback_routes._get_avoid_port_names(close_plans)
    congested_leg_keys = {
        fallback_routes._leg_key(plan.target_leg)
        for plan in congested_plans
        if plan.target_leg is not None
    }
    if not avoid_port_names and not congested_leg_keys:
        return []

    disruption_key = (
        tuple(sorted(avoid_port_names)),
        tuple(sorted(congested_leg_keys)),
    )
    alternatives = []
    for source_route in list(context.initial_service_routes):
        if len(source_route.deployed_vessels) < MIN_SOURCE_ROUTE_VESSELS:
            continue
        if not fallback_routes._service_route_is_disrupted(
            source_route,
            avoid_port_names,
            congested_leg_keys,
        ):
            continue
        if not _source_route_has_available_empty_vessel(source_route):
            continue

        alternative_route = next(
            (
                route
                for route in context.service_routes
                if route.source_service_route is source_route
                and route.disruption_key == disruption_key
            ),
            None,
        )
        if alternative_route is None:
            alternative_legs = _find_conservative_alternative_legs(
                context,
                source_route,
                avoid_port_names,
                congested_leg_keys,
                fallback_routes,
            )
            if not alternative_legs:
                continue
            if not _alternative_cycle_is_materially_faster(
                source_route,
                alternative_legs,
                close_plans,
                congested_plans,
                now,
            ):
                continue
            alternative_route = fallback_routes._build_alternative_service_route(
                context,
                source_route,
                avoid_port_names,
                congested_leg_keys,
                disruption_key,
            )
        if alternative_route is None:
            continue

        _reserve_one_empty_vessel(source_route, alternative_route)
        alternatives.append(alternative_route)
    return alternatives


def _source_route_has_available_empty_vessel(source_route):
    return any(
        vessel.assigned_service_route is source_route
        and vessel.pending_assigned_service_route is None
        and not vessel.carried_shipments
        for vessel in source_route.deployed_vessels
    )


def _find_conservative_alternative_legs(
    context,
    source_route,
    avoid_port_names,
    congested_leg_keys,
    fallback_routes,
):
    source_segments = sorted(
        source_route.segments,
        key=lambda segment: segment.sequence_index,
    )
    anchor_ports = []
    for segment in source_segments:
        port = segment.associated_leg.departure_port
        if port.name.casefold() in avoid_port_names:
            continue
        if not anchor_ports or anchor_ports[-1] is not port:
            anchor_ports.append(port)
    if len(anchor_ports) < 2:
        return None

    route_legs = []
    for index, departure_port in enumerate(anchor_ports):
        arrival_port = anchor_ports[(index + 1) % len(anchor_ports)]
        leg_path = fallback_routes._find_shortest_leg_path(
            context,
            departure_port,
            arrival_port,
            avoid_port_names,
            congested_leg_keys,
        )
        if not leg_path:
            return None
        route_legs.extend(leg_path)
    return route_legs


def _alternative_cycle_is_materially_faster(
    source_route,
    alternative_legs,
    close_plans,
    congested_plans,
    now,
):
    speed = _route_average_speed(source_route)
    normal_hours = sum(
        float(segment.associated_leg.sailing_distance or 0) / speed
        for segment in source_route.segments
    )
    disruption_hours = _source_route_disruption_delay_hours(
        source_route,
        close_plans,
        congested_plans,
        now,
        speed,
    )
    default_hours = normal_hours + disruption_hours
    alternative_hours = sum(
        float(leg.sailing_distance or 0) / speed
        for leg in alternative_legs
    )
    if default_hours <= 0:
        return False
    return alternative_hours <= default_hours * (
        1.0 - MIN_ALTERNATIVE_ROUTE_IMPROVEMENT
    )


def _source_route_disruption_delay_hours(
    source_route,
    close_plans,
    congested_plans,
    now,
    speed,
):
    route_legs = {segment.associated_leg for segment in source_route.segments}
    route_ports = {
        port
        for leg in route_legs
        for port in (leg.departure_port, leg.arrival_port)
    }
    delay_hours = 0.0
    closure_ends = []
    for plan in close_plans:
        if plan.target_berth is None or plan.target_berth.port not in route_ports:
            continue
        end = dt.datetime.min + dt.timedelta(
            days=plan.start_offset_days + plan.duration_days
        )
        closure_ends.append(max(0.0, (end - now).total_seconds() / 3600.0))
    if closure_ends:
        delay_hours += max(closure_ends)

    for plan in congested_plans:
        leg = plan.target_leg
        if leg not in route_legs:
            continue
        base_hours = float(leg.sailing_distance or 0) / speed
        delay_hours += base_hours * max(0.0, float(plan.multiplier or 1) - 1.0)
    return delay_hours


def _reserve_one_empty_vessel(source_route, alternative_route):
    if alternative_route.deployed_vessels:
        return
    if any(
        vessel.pending_assigned_service_route is alternative_route
        for vessel in source_route.deployed_vessels
    ):
        return
    candidates = sorted(source_route.deployed_vessels, key=lambda vessel: vessel.index)
    for vessel in candidates:
        if vessel.assigned_service_route is not source_route:
            continue
        if vessel.pending_assigned_service_route is not None:
            continue
        if vessel.carried_shipments:
            continue
        vessel.pending_assigned_service_route = alternative_route
        return
    pressure_ratio = min(1.0, peak_booked_teu / total_capacity)
    return pressure_ratio * MAX_CAPACITY_PRESSURE_HOURS


def _find_lowest_cost_booking_path(
    context,
    origin_port,
    destination_port,
    all_edges,
    window,
    hard_avoid_preventive,
    shipment_teu=0,
):
    outgoing = {}
    for edge in all_edges:
        if edge.departure_port is destination_port:
            continue
        if _edge_is_blocked(edge, origin_port, destination_port, window, hard_avoid_preventive):
            continue
        outgoing.setdefault(edge.departure_port, []).append(edge)

    distances = {port: math.inf for port in context.ports}
    previous_edge = {}
    unvisited = list(context.ports)
    distances[origin_port] = 0.0

    while unvisited:
        current = min(unvisited, key=lambda port: distances[port])
        if math.isinf(distances[current]) or current is destination_port:
            break
        unvisited.remove(current)

        for edge in outgoing.get(current, []):
            next_port = edge.arrival_port
            if next_port not in unvisited:
                continue
            alternative = distances[current] + _edge_cost(
                edge, destination_port, window, shipment_teu
            )
            if alternative < distances[next_port]:
                distances[next_port] = alternative
                previous_edge[next_port] = edge

    if destination_port not in previous_edge:
        return None

    path = []
    cursor = destination_port
    while cursor is not origin_port:
        edge = previous_edge.get(cursor)
        if edge is None:
            return None
        path.append(edge)
        cursor = edge.departure_port
    path.reverse()
    return path


def _edge_is_blocked(edge, origin_port, destination_port, window, hard_avoid_preventive):
    active_closed = window.active_closed_ports
    preventive_closed = window.preventive_closed_ports
    active_legs = window.active_congested_legs
    preventive_legs = window.preventive_congested_legs

    if any(segment.associated_leg in active_legs for segment in edge.segments):
        return True
    if hard_avoid_preventive and any(
        segment.associated_leg in preventive_legs for segment in edge.segments
    ):
        return True

    ports_to_check = _edge_ports(edge)
    for port in ports_to_check:
        name = port.name.casefold()
        if name in active_closed and port is not destination_port:
            return True
        if (
            hard_avoid_preventive
            and name in preventive_closed
            and port is not origin_port
            and port is not destination_port
        ):
            return True

    return False


def _edge_cost(edge, destination_port, window, shipment_teu=0):
    transfer_multiplier = 1.0 + min(
        LARGE_SHIPMENT_TRANSFER_MULTIPLIER - 1.0,
        max(0.0, float(shipment_teu or 0) / LARGE_SHIPMENT_TEU),
    )
    cost = (
        edge.sailing_hours
        + edge.expected_wait_hours
        + edge.capacity_pressure_hours
        + TRANSFER_PENALTY_HOURS * transfer_multiplier
    )

    for segment in edge.segments:
        leg = segment.associated_leg
        if leg in window.active_congested_legs:
            cost += ACTIVE_LEG_DELAY_HOURS
        elif leg in window.preventive_congested_legs:
            cost += IMMINENT_LEG_DELAY_HOURS

    for port in _edge_ports(edge):
        if port is destination_port:
            continue
        name = port.name.casefold()
        if name in window.preventive_closed_ports:
            cost += IMMINENT_PORT_DELAY_HOURS

    return cost


def _booking_path_cost(
    path,
    destination_port,
    window,
    shipment_teu,
    current_route=None,
):
    cost = sum(
        _edge_cost(edge, destination_port, window, shipment_teu)
        for edge in path
    )
    if path and path[0].service_route is current_route:
        transfer_multiplier = 1.0 + min(
            LARGE_SHIPMENT_TRANSFER_MULTIPLIER - 1.0,
            max(0.0, float(shipment_teu or 0) / LARGE_SHIPMENT_TEU),
        )
        cost -= path[0].expected_wait_hours
        cost -= path[0].capacity_pressure_hours
        cost -= TRANSFER_PENALTY_HOURS * transfer_multiplier
    return max(0.0, cost)


def _path_arrives_during_destination_closure(
    context,
    now,
    destination_port,
    path,
    shipment_teu,
):
    """Return whether the expected destination arrival falls inside a closure."""
    arrival_time = now + dt.timedelta(
        hours=_expected_path_elapsed_hours(path, shipment_teu)
    )
    for plan in context.disruption_plans:
        if not plan.close_berth or plan.target_berth is None:
            continue
        if plan.target_berth.port is not destination_port:
            continue
        if plan.start_offset_days is None or plan.duration_days is None:
            continue
        closure_start = dt.datetime.min + dt.timedelta(days=plan.start_offset_days)
        closure_end = closure_start + dt.timedelta(days=plan.duration_days)
        if closure_start <= arrival_time < closure_end:
            return True
    return False


def _expected_path_elapsed_hours(path, shipment_teu):
    """Estimate physical travel and connection time without avoidance penalties."""
    transfer_multiplier = 1.0 + min(
        LARGE_SHIPMENT_TRANSFER_MULTIPLIER - 1.0,
        max(0.0, float(shipment_teu or 0) / LARGE_SHIPMENT_TEU),
    )
    elapsed = 0.0
    previous_route = None
    for edge in path:
        elapsed += edge.sailing_hours
        elapsed += edge.expected_wait_hours
        elapsed += edge.capacity_pressure_hours
        if previous_route is not None and edge.service_route is not previous_route:
            elapsed += TRANSFER_PENALTY_HOURS * transfer_multiplier
        previous_route = edge.service_route
    return elapsed


def _build_remaining_booking_path(
    shipment,
    current_booking,
    current_segment,
    candidate_edges,
):
    """Map the unfinished booking chain to comparable candidate edges."""
    remaining = []
    for booking in sorted(
        shipment.associated_bookings,
        key=lambda item: item.sequence_index,
    ):
        if booking.sequence_index < current_booking.sequence_index:
            continue
        if booking.service_route is None:
            return None

        departure_index = booking.departure_segment_index
        if booking is current_booking:
            if current_segment.sequence_index == booking.arrival_segment_index:
                continue
            segments = sorted(
                booking.service_route.segments,
                key=lambda segment: segment.sequence_index,
            )
            position = _find_segment_list_index(
                segments,
                current_segment.sequence_index,
            )
            if position < 0:
                return None
            departure_index = segments[(position + 1) % len(segments)].sequence_index

        edge = next(
            (
                candidate
                for candidate in candidate_edges
                if candidate.service_route is booking.service_route
                and candidate.departure_segment_index == departure_index
                and candidate.arrival_segment_index == booking.arrival_segment_index
            ),
            None,
        )
        if edge is None:
            return None
        remaining.append(edge)
    return remaining


def _edge_ports(edge):
    ports = [edge.departure_port]
    ports.extend(segment.associated_leg.arrival_port for segment in edge.segments)
    return ports


def _assign_booking_path(shipment, path):
    _clear_booking_chain(shipment)
    for sequence_index, edge in enumerate(path, start=1):
        booking = Booking(
            sequence_index=sequence_index,
            shipment=shipment,
            service_route=edge.service_route,
            departure_segment_index=edge.departure_segment_index,
            arrival_segment_index=edge.arrival_segment_index,
        )
        shipment.associated_bookings.append(booking)
        edge.service_route.associated_bookings.append(booking)
    shipment.current_booking_index = 1


def _clear_booking_chain(shipment):
    _remove_bookings_from_service_routes(shipment.associated_bookings)
    shipment.associated_bookings = []
    shipment.current_booking_index = None


def _remove_bookings_from_service_routes(bookings):
    for booking in bookings:
        service_route = booking.service_route
        if service_route is None:
            continue
        while booking in service_route.associated_bookings:
            service_route.associated_bookings.remove(booking)


def _remaining_booking_chain_is_impacted(
    shipment,
    current_booking,
    current_segment,
    window,
):
    active_or_preventive_closed = set(window.active_closed_ports)
    active_or_preventive_closed.update(window.preventive_closed_ports)
    active_or_preventive_legs = set(window.active_congested_legs)
    active_or_preventive_legs.update(window.preventive_congested_legs)

    if not active_or_preventive_closed and not active_or_preventive_legs:
        return False

    for booking in sorted(shipment.associated_bookings, key=lambda item: item.sequence_index):
        if booking.sequence_index < current_booking.sequence_index:
            continue
        if booking.service_route is None:
            continue

        segments = sorted(
            booking.service_route.segments,
            key=lambda segment: segment.sequence_index,
        )
        if not segments:
            continue

        if booking is current_booking:
            current_index = _find_segment_list_index(
                segments,
                current_segment.sequence_index,
            )
            end_index = _find_segment_list_index(
                segments,
                booking.arrival_segment_index,
            )
            if current_index >= 0 and current_index == end_index:
                continue
            if current_index < 0:
                start_index = _find_segment_list_index(
                    segments,
                    booking.departure_segment_index,
                )
            else:
                start_index = (current_index + 1) % len(segments)
        else:
            start_index = _find_segment_list_index(
                segments,
                booking.departure_segment_index,
            )
            end_index = _find_segment_list_index(
                segments,
                booking.arrival_segment_index,
            )

        end_index = _find_segment_list_index(segments, booking.arrival_segment_index)
        if start_index < 0 or end_index < 0:
            continue

        for segment in _iter_segments_between(segments, start_index, end_index):
            leg = segment.associated_leg
            if leg in active_or_preventive_legs:
                return True
            if leg.departure_port.name.casefold() in active_or_preventive_closed:
                return True
            if leg.arrival_port.name.casefold() in active_or_preventive_closed:
                return True

    return False


def _replace_unfinished_bookings_from_current_port(
    shipment,
    current_booking,
    current_segment,
    path,
):
    original_bookings = list(shipment.associated_bookings)
    retained = sorted(
        (
            booking
            for booking in shipment.associated_bookings
            if booking.sequence_index < current_booking.sequence_index
        ),
        key=lambda booking: booking.sequence_index,
    )

    completed_booking = current_booking
    completed_booking.arrival_segment_index = current_segment.sequence_index

    next_sequence = completed_booking.sequence_index + 1
    new_bookings = []
    remaining_edges = path
    if path and path[0].service_route is completed_booking.service_route:
        completed_booking.arrival_segment_index = path[0].arrival_segment_index
        remaining_edges = path[1:]

    for edge in remaining_edges:
        booking = Booking(
            sequence_index=next_sequence,
            shipment=shipment,
            service_route=edge.service_route,
            departure_segment_index=edge.departure_segment_index,
            arrival_segment_index=edge.arrival_segment_index,
        )
        new_bookings.append(booking)
        next_sequence += 1

    retained_bookings = retained + [completed_booking]
    replaced_bookings = [
        booking for booking in original_bookings if booking not in retained_bookings
    ]
    _remove_bookings_from_service_routes(replaced_bookings)
    for booking in new_bookings:
        booking.service_route.associated_bookings.append(booking)

    shipment.associated_bookings.clear()
    shipment.associated_bookings.extend(retained_bookings + new_bookings)
    shipment.current_booking_index = completed_booking.sequence_index


def _get_final_booking_port(shipment):
    last_booking = max(
        shipment.associated_bookings,
        key=lambda booking: booking.sequence_index,
        default=None,
    )
    if last_booking is None or last_booking.service_route is None:
        return None

    final_segment = next(
        (
            segment
            for segment in last_booking.service_route.segments
            if segment.sequence_index == last_booking.arrival_segment_index
        ),
        None,
    )
    if final_segment is None:
        return None
    return final_segment.associated_leg.arrival_port


def _find_segment_list_index(segments, sequence_index):
    return next(
        (
            index
            for index, segment in enumerate(segments)
            if segment.sequence_index == sequence_index
        ),
        -1,
    )


def _iter_segments_between(segments, start_index, end_index):
    cursor = start_index
    while True:
        yield segments[cursor]
        if cursor == end_index:
            break
        cursor = (cursor + 1) % len(segments)
