"""Curated knowledge base: cited coefficient tables the LLM reads and adapts.

This package holds *reference data*, never logic. The InVEST domain surface
(tools) stays deterministic; judgement -- which coefficient fits this site --
lives in the client, supported by what it reads here. See ``CLAUDE.md`` section 6
("Base de conocimiento").

``knowledge/coefficients/`` -- one JSON per biophysical-table parameter
(``usle_c``, ``usle_p``, ``ndr_nutrient``, ``curve_number``, ``kc``,
``root_depth``, ``carbon_pools``) plus ``sources.json`` (the bibliography) and
``profiles/`` (fully worked local tables kept as documented examples). Loaded by
``knowledge.coefficients`` and exposed as the ``invest://coefficients`` resources.
"""

from __future__ import annotations
