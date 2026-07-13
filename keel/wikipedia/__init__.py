"""The Wikipedia contribution target (Phase 1, the only target).

This package holds everything Wikipedia-specific: the domain models (Locator, Payload,
snapshot, draft) and the `WikipediaTarget` implementation of `ContributionTarget`.
The core imports nothing from here; the wiring happens at the composition root
(cli.py). That is what makes a second target a sibling package, not a rewrite.
"""
