import hashlib
import tempfile
import os
import stat

from zipfile import ZipFile, BadZipfile

from juju.charm.base import CharmBase, get_revision
from juju.charm.config import ConfigOptions
from juju.charm.metadata import MetaData
from juju.errors import CharmError
from juju.lib.filehash import compute_file_hash


class CharmBundle(CharmBase):
    """ZIP-archive that contains charm directory content."""

    type = "bundle"

    def __init__(self, path):
        self.path = isinstance(path, file) and path.name or path
        try:
            zf = ZipFile(path, 'r')
        except BadZipfile, exc:
            raise CharmError(path, "must be a zip file (%s)" % exc)

        if "metadata.yaml" not in zf.namelist():
            raise CharmError(
                path, "charm does not contain required file 'metadata.yaml'")
        self.metadata = MetaData()
        self.metadata.parse(zf.read("metadata.yaml"))

        try:
            revision_content = zf.read("revision")
        except KeyError:
            revision_content = None
        self._revision = get_revision(
            revision_content, self.metadata, self.path)
        if self._revision is None:
            raise CharmError(self.path, "has no revision")

        self.config = ConfigOptions()
        if "config.yaml" in zf.namelist():
            self.config.parse(zf.read("config.yaml"))

    def get_revision(self):
        return self._revision

    def compute_sha256(self):
        """Return the SHA256 digest for this charm bundle.

        The digest is extracted out of the final bundle file itself.
        """
        return compute_file_hash(hashlib.sha256, self.path)

    def extract_to(self, directory_path):
        """Extract the bundle to directory path and return a
        CharmDirectory handle"""
        from .directory import CharmDirectory
        zf = ZipFile(self.path, "r")
        for info in zf.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                source = zf.read(info.filename)
                target = os.path.join(directory_path, info.filename)
                # Support extracting over existing charm.
                # TODO: a directory changed to a file needs install manifests
                if os.path.exists(target):
                    os.remove(target)
                os.symlink(source, target)
                continue

            # Preserve mode
            extract_path = zf.extract(info, directory_path)
            os.chmod(extract_path, mode)
        return CharmDirectory(directory_path)

    def as_bundle(self):
        return self

    def as_directory(self):
        """Returns the bundle as a CharmDirectory using a temporary
        path"""
        dn = tempfile.mkdtemp(prefix="tmp-charm-")
        return self.extract_to(dn)
