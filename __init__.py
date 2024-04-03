"""Init file for pligin."""
from .plugin import JLCPCBPlugin

if __name__ != "__main__":
    JLCPCBPlugin().register()
