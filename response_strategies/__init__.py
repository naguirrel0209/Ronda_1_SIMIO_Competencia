"""Contestant and fallback response strategies.

Exports are resolved lazily to avoid circular imports while the simulation
model imports strategy modules during startup.
"""

__all__ = ["DefaultStrategy", "UserStrategy"]


def __getattr__(name):
    if name == "DefaultStrategy":
        from .default_strategy import DefaultStrategy

        return DefaultStrategy
    if name == "UserStrategy":
        from .user_strategy import UserStrategy

        return UserStrategy
    raise AttributeError(name)
