"""Pure source selection and destination planning."""
from placekeeper.errors import ReadFailure


def receipt_matches(observation, receipt):
    return (receipt and receipt['position'] == observation.native
            and receipt['stamp'] == observation.stamp)


def choose_source(observations, receipts, prefer_recent):
    # Preserve forward-only handoffs when the available readers use text maps.
    candidates = [key for key, position in observations.items()
                  if not position.own and not receipt_matches(position, receipts.get(key))]
    if not candidates:
        return None
    if not prefer_recent:
        return max(candidates, key=lambda key: observations[key].anchor)
    if any(observations[key].stamp is None for key in candidates):
        raise ReadFailure('A reader has no comparable timestamp. Sync the source reader and try again.')
    candidates.sort(key=lambda key: observations[key].stamp, reverse=True)
    if len(candidates) > 1:
        first, second = (observations[key] for key in candidates[:2])
        if first.stamp - second.stamp < max(60, first.precision_seconds, second.precision_seconds):
            raise ReadFailure('Reader timestamps are too close. Sync the intended source and try again.')
    return candidates[0]


def plan(readers, observations, receipts):
    if len(observations) < 2:
        return None, {}, 'Fewer than two readers have saved positions for this book.'
    prefer_recent = any(getattr(readers[key], 'prefer_recent', False) for key in observations)
    source = choose_source(observations, receipts, prefer_recent)
    if source is None:
        return None, {}, 'No new reader position.'
    anchor = observations[source].anchor
    targets = {}
    for key, current in observations.items():
        if key == source or (not prefer_recent and current.anchor >= anchor):
            continue
        if readers[source].equivalent(anchor, current.anchor):
            continue
        target = readers[key].target(anchor, current)
        if target is not None and target != current.native:
            targets[key] = target
    return source, targets, ''
