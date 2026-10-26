"""Small shared safety checks; no database, file, or network operations."""


def check_source_row_count(current_count, saved_count):
    """Stop when the source has strictly less than half the known customers.

    Count the entire source, not just NEW/CHANGED rows. An empty state permits
    the first sync. Integer multiplication keeps the boundary exact.
    """
    if current_count * 2 < saved_count:
        raise ValueError(
            f"Source row-count guard stopped the sync: {current_count} current rows "
            f"is below half of {saved_count} saved customers. "
            "Check the source data before retrying. No batches should be sent or state advanced."
        )
