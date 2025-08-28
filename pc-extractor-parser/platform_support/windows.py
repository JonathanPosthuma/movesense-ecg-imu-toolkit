# pc-extractor-parser/platform/windows.py
import sys, asyncio, logging

def init_event_loop_policy():
    """Make asyncio behave nicely on Windows for Bleak/PyQt."""
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            logging.debug("[win] WindowsSelectorEventLoopPolicy set.")
        except Exception as e:
            logging.debug(f"[win] could not set event loop policy: {e}")

def warmup_winrt():
    """
    Optional: pre-import WinRT namespaces that Bleak may use.
    If they’re already present it’s a no-op; if not, we just ignore import errors.
    """
    try:
        import winrt.windows.devices.bluetooth  # noqa: F401
        import winrt.windows.devices.bluetooth.genericattributeprofile  # noqa: F401
        import winrt.windows.devices.enumeration  # noqa: F401
        import winrt.windows.storage.streams  # noqa: F401
        logging.debug("[win] winrt namespaces imported.")
    except Exception as e:
        logging.debug(f"[win] winrt warmup skipped: {e}")

def init():
    """Call this once at startup on Windows."""
    init_event_loop_policy()
    warmup_winrt()