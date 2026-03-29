# SDS Scoring Autoresearch Program

Optimize SDS scoring weights to better differentiate high-opportunity development sites. Weights must sum to 1.0. Prioritize zoning flexibility and location over infrastructure. Do not change dealbreaker thresholds (flood zone, environmental).

## Scoring
Score = weighted sum of parcel attributes, averaged across a random 10-parcel sample.

## Constraints
- All WEIGHT_* values must sum to exactly 1.0
- No single weight may exceed 0.40
- MIN_PARCEL_SIZE_ACRES and MAX_FLOOD_ZONE_PCT are dealbreakers — do not change them
- Only adjust weights, not threshold values
