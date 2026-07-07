"""
ASTRA Brain — modular composite-scoring decision engine.

Graded factor scores → normalized → conviction-weighted composite → regime-aware entry, a layered exit
stack, and vol-scaled conviction-weighted sizing. See README.md for the full method.

Public surface used by strategy.py:
    conviction.get_ticker_guidance / is_excluded / conviction_weight
    score.screen_position           — one position → Signal (screen_position contract)
    sizing.allocate                 — iterative constrained allocation across BUYs
"""
