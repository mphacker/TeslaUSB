"""conftest.py — Add scripts/web to the Python path for test imports."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts', 'web'))
