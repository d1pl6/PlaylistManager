"""Top-level plugin packages (see app/plugin_loader.py).

Each subdirectory is a platform integration declared by its plugin.json.
The core app discovers them at runtime; nothing here is imported at
startup unless a plugin's lazily-resolved code is actually used.
"""
