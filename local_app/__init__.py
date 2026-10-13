"""Local, single-owner host for the published MIRROW modules."""
from pathlib import Path
import sys

# Explicit public module root, never a neighbouring production deployment.
_memory_source = str(Path(__file__).resolve().parents[1] / 'memory-cognition' / 'src')
if _memory_source not in sys.path:
    sys.path.append(_memory_source)

# Memory's published projection helpers extend the shared package; root APIs
# retain precedence. Both roots belong to this public checkout.
import context_builder
context_builder.__path__.append(str(Path(_memory_source) / 'context_builder'))
import behavior_scheduler
behavior_scheduler.__path__.append(str(Path(_memory_source) / 'behavior_scheduler'))
