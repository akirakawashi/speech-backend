"""Tables, one module per owning service.

Intentionally empty. Importing a submodule of a package runs this file first,
so anything placed here is loaded by every owner module -- which is exactly
how the aggregate used to defeat the split. The aggregate is `all.py`.
"""
