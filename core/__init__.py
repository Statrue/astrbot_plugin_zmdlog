"""Everything the plugin does, without AstrBot: import the modules directly.

``main.py`` is the only AstrBot-aware file; nothing under ``core`` imports
the host, which is what keeps it unit-testable.
"""
