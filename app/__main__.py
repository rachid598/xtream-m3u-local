"""Point d'entrée : ``python -m app``.

Évite de lancer ``python -m app.server`` qui chargerait le module deux fois
(une fois en ``__main__``, une fois en ``app.server`` via ``app.web``).
"""
from .server import main

main()
