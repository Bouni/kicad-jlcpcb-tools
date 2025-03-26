"""Init file for plugin."""

from .plugin import JLCPCBPlugin

if __name__ != "__main__":
    JLCPCBPlugin().register()
