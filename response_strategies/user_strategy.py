"""Contestant response strategy.

This implementation focuses on two low-risk improvements:

1. Preventive routing for shipments generated shortly before known
   disruptions.
2. In-transit replanning for cargo whose remaining booking chain crosses an
   active or imminent disruption.

It intentionally does not create alternative service routes. The default
strategy can still create validated alternatives during active disruptions.
"""

from dataclasses import dataclass
import datetime as dt
import math

from maritime_data_context import Booking


LOOKAHEAD_DAYS = 50.0
RECOVERY_BUFFER_DAYS = 3.0
TRANSFER_PENALTY_NM = 900.0
IMMINENT_LEG_PENALTY_NM = 12000.0
IMMINENT_PORT_PENALTY_NM = 16000.0
ACTIVE_LEG_PENALTY_NM = 50000.0


@dataclass
class _CandidateBookingEdge:
    service_route: object
    departure_port: object
    arrival_port: object
    departure_segment_index: int
    arrival_segment_index: int
    total_distance: float
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
        """PortResponseStrategy.

        Return ``None`` so the default berth-priority strategy remains active.
        The requested changes are focused on preventive routing and in-transit
        replanning.
        """
        return None

    @staticmethod
    def create_alternative_service_routes(context, now, vessel=None):
        """ShippingLineResponseStrategy.

        Do not create routes here. Returning ``None`` lets the validated default
        implementation create active-disruption alternatives when appropriate.
        """
        return None

    @staticmethod
    def assign_associated_bookings(context, now, shipment):
        """Assign an initial booking chain that avoids near-term disruptions.

        The default strategy reacts once disruptions are active. This strategy
        adds a preventive window so shipments generated shortly before a known
        closure or congested leg avoid that risk when a viable path exists.
        """
        demand = shipment.demand
        origin_port = demand.origin_port
        destination_port = demand.destination_port
        if origin_port is destination_port:
            _clear_booking_chain(shipment)
            shipment.current_booking_index = 1
            return True

        window = _get_disruption_window(context, now)
        if not _has_preventive_work(window):
            return None

        if origin_port.name.casefold() in window.active_closed_ports:
            return False
        if destination_port.name.casefold() in window.active_closed_ports:
            return False

        candidate_bookings = _build_all_candidate_bookings(context)
        path = _find_lowest_cost_booking_path(
            context,
            origin_port,
            destination_port,
            candidate_bookings,
            window,
            hard_avoid_preventive=True,
        )
        if not path:
            path = _find_lowest_cost_booking_path(
                context,
                origin_port,
                destination_port,
                candidate_bookings,
                window,
                hard_avoid_preventive=False,
            )
        if not path:
            return None

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

        candidate_bookings = _build_all_candidate_bookings(context)
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
            )
            if not path:
                path = _find_lowest_cost_booking_path(
                    context,
                    current_port,
                    final_port,
                    candidate_bookings,
                    window,
                    hard_avoid_preventive=False,
                )
            if not path:
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


def _build_all_candidate_bookings(context):
    edges = []
    for service_route in context.service_routes:
        if not _route_has_available_vessel(service_route):
            continue
        segments = sorted(
            service_route.segments,
            key=lambda segment: segment.sequence_index,
        )
        segment_count = len(segments)
        if segment_count < 2:
            continue

        for start_index in range(segment_count):
            departure_port = segments[start_index].associated_leg.departure_port
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
                        segments=used_segments,
                    )
                )
    return edges


def _route_has_available_vessel(route):
    if route.source_service_route is None:
        return True
    return bool(route.deployed_vessels)


def _find_lowest_cost_booking_path(
    context,
    origin_port,
    destination_port,
    all_edges,
    window,
    hard_avoid_preventive,
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
            alternative = distances[current] + _edge_cost(edge, destination_port, window)
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
        if name in active_closed:
            return True
        if (
            hard_avoid_preventive
            and name in preventive_closed
            and port is not origin_port
            and port is not destination_port
        ):
            return True

    return False


def _edge_cost(edge, destination_port, window):
    cost = edge.total_distance + TRANSFER_PENALTY_NM

    for segment in edge.segments:
        leg = segment.associated_leg
        if leg in window.active_congested_legs:
            cost += ACTIVE_LEG_PENALTY_NM
        elif leg in window.preventive_congested_legs:
            cost += IMMINENT_LEG_PENALTY_NM

    for port in _edge_ports(edge):
        if port is destination_port:
            continue
        name = port.name.casefold()
        if name in window.preventive_closed_ports:
            cost += IMMINENT_PORT_PENALTY_NM

    return cost


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
