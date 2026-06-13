"""conftest.py -- isolation des stubs entre modules de tests.

Probleme : test_phase7_supervisor.py injecte un stub xgboost vide dans
sys.modules (quand xgboost n'est pas installe). Si pytest collecte ensuite
test_phase4_models.py dans la meme session, joblib tente de depickler un
Booster xgboost mais ne trouve que le stub => ModuleNotFoundError.

Solution : supprimer les stubs injectes par test_phase7 avant chaque module
de test qui en a besoin, via un autouse fixture de session.
"""
from __future__ import annotations

import sys
import pytest

# Modules que test_phase7 peut stuber et qui doivent etre vrais ailleurs
_PHASE7_STUBS = ["xgboost", "xgboost.core", "xgboost.sklearn"]


@pytest.fixture(autouse=True)
def _remove_xgboost_stubs_if_fake(request):
    """Avant chaque test, si xgboost est un stub vide (pas de __version__),
    le retirer de sys.modules pour laisser le vrai import se faire."""
    xgb = sys.modules.get("xgboost")
    if xgb is not None and not hasattr(xgb, "__version__"):
        for name in list(sys.modules):
            if name == "xgboost" or name.startswith("xgboost."):
                del sys.modules[name]
    yield
