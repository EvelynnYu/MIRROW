"""Compatibility port; replace with a host UI projection when embedding."""
from local_host import projection
def get_global_session_manager(): return projection
