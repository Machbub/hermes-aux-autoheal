"""Keep Hermes Agent's auxiliary task routes pointed at models that answer.

Hermes lets you pin a provider/model per auxiliary task (compression,
summarization, vision, ...) plus a ``fallback_chain`` of runners-up. That
config is static: you write it once, and nothing checks whether those entries
still work. When a model is retired upstream or a provider key is revoked, the
route keeps naming a corpse and the task fails at the worst possible moment.

This package health-probes the models you actually have configured, drops the
dead ones, and rewrites the route from what is verified alive — safely enough
to run on a timer next to other processes writing the same config file.

Not affiliated with Nous Research.
"""

__version__ = '0.7.3'
__all__ = ['config_io', 'context', 'discovery', 'health', 'router']
