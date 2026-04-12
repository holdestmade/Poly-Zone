"""Constants for the Polygon Zone component."""

DOMAIN = "poly_zone"

# Approximate metres per degree of latitude (WGS-84 mean)
METERS_PER_DEGREE_LAT: float = 111320.0

# Minimum denominator magnitude used in the ray-casting algorithm to guard
# against near-horizontal edges causing division by very small numbers.
RAY_CAST_EPSILON: float = 1e-10
