import random


def get_random_int(min_val: int, max_val: int) -> int:
    """Returns a random integer between min and max (inclusive)."""
    return random.randint(int(min_val), int(max_val))


def get_random_float(min_val: float, max_val: float, fraction_digits: int = 1) -> float:
    """Returns a random float in [min, max) rounded to `fraction_digits`."""
    return round(random.random() * (max_val - min_val) + min_val, fraction_digits)


# Backward-compatible alias (upstream renamed getRandomNumber -> getRandomInt)
get_random_number = get_random_int
