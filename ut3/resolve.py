"""Cross-package object resolution.

Cooked UT3 maps embed copies of the objects they use, but two things still live
elsewhere: objects referenced through the import table, and the streaming mip
payloads of textures (which sit in the content package that owns the texture --
see FORMAT.md). Both need an index of the installed packages.
"""

import os


class PackageIndex:
    """Finds and caches packages by name across a UT3 installation."""

    EXTENSIONS = (".upk", ".ut3", ".u")

    def __init__(self, roots):
        if isinstance(roots, str):
            roots = [roots]
        self.roots = list(roots)
        self._paths = None
        self._packages = {}

    @classmethod
    def for_map(cls, map_path):
        """Index the CookedPC tree containing `map_path`."""
        d = os.path.dirname(os.path.abspath(map_path))
        while d and os.path.basename(d) != "CookedPC":
            parent = os.path.dirname(d)
            if parent == d:
                return cls([os.path.dirname(os.path.abspath(map_path))])
            d = parent
        return cls([d])

    @property
    def paths(self):
        if self._paths is None:
            self._paths = {}
            for root in self.roots:
                for dirpath, _dirnames, filenames in os.walk(root):
                    for fn in filenames:
                        stem, ext = os.path.splitext(fn)
                        if ext.lower() in self.EXTENSIONS:
                            self._paths.setdefault(stem.lower(), os.path.join(dirpath, fn))
        return self._paths

    def path_for(self, package_name):
        return self.paths.get(package_name.lower())

    def package(self, package_name):
        """Open (and cache) a package by name; None if it is not installed."""
        key = package_name.lower()
        if key in self._packages:
            return self._packages[key]
        path = self.path_for(package_name)
        pkg = None
        if path:
            from .package import Package

            try:
                pkg = Package(path)
            except (ValueError, OSError, EOFError):
                pkg = None
        self._packages[key] = pkg
        return pkg

    def raw_bytes(self, package_name, offset, size):
        """Read raw (physical) bytes from a package file -- used for bulk data."""
        path = self.path_for(package_name)
        if not path:
            return None
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(size)
        return data if len(data) == size else None

    def resolve(self, pkg, ref):
        """Resolve an ObjRef to (Package, Export), following imports.

        Returns (None, None) when the owning package is not installed.
        """
        if ref is None or ref.is_null:
            return None, None
        if ref.is_export:
            return pkg, ref.export
        path = pkg.path_of(ref.index)
        parts = path.split(".")
        if len(parts) < 2:
            return None, None
        owner = self.package(parts[0])
        if owner is None:
            return None, None
        inner = ".".join(parts[1:])
        hits = [e for e in owner.exports if owner.path_of(e.index) == inner]
        if not hits:
            # Cooked packages sometimes keep the package name in the outer chain.
            hits = [e for e in owner.exports if owner.path_of(e.index) == path]
        if not hits:
            hits = [e for e in owner.exports if e.name == parts[-1]]
        if len(hits) != 1:
            return None, None
        return owner, hits[0]
