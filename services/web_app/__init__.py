"""HTTP application boundary for the Web notebook."""

from .asgi import NotebookAsgiApp

__all__ = ["NotebookAsgiApp"]
