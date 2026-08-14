"""Temperature conversion utilities."""


def f_to_c(fahrenheit):
    """Convert Fahrenheit to Celsius."""
    return (fahrenheit - 32) * 5 / 9


def c_to_f(celsius):
    """Convert Celsius to Fahrenheit."""
    return celsius * 9 / 5 + 32
