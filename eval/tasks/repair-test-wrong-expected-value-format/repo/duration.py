"""Duration formatting. This module is CORRECT — do not change it."""


def format_duration(seconds):
    """Format *seconds* as 'Hh Mm Ss' (e.g. 3661 -> '1h 1m 1s')."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"
